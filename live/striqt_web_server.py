#!/usr/bin/env python3
"""
Web-based live spectrogram + PSD viewer server.

Replaces the TCP client/server pair with a WebSocket server that any browser
can connect to. Static assets are served from live/web/.

Usage:
    # Real AIR8201B radio
    python live/striqt_web_server.py

    # Synthetic IQ (no hardware — develop/test on a laptop)
    python live/striqt_web_server.py --demo

    # uint8 waterfall encoding (~4x smaller frames, good for slow links)
    python live/striqt_web_server.py --quantize

    # Combined: demo + capped fps
    python live/striqt_web_server.py --demo --fps 10

For internet access via Cloudflare Tunnel (run in a second terminal):
    cloudflared tunnel --url http://localhost:8000

Or use the convenience launcher:
    bash live/run_web.sh
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# striqt hardware imports (only needed for real radio mode)
try:
    from striqt.sensor import specs
    from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source
    try:
        from striqt.sensor.lib.sources.soapy import ReceiveStreamError
    except Exception:
        try:
            from striqt.sensor.lib.sources.base import ReceiveStreamError
        except Exception:
            ReceiveStreamError = OSError
    _SENSOR_OK = True
except Exception as _sensor_err:
    _SENSOR_OK = False
    specs = None
    Air8201BSourceSpec = None
    Airstack1Source = None
    ReceiveStreamError = OSError

# striqt analysis (calibrated spectrogram — optional, falls back to quicklook)
try:
    from striqt.analysis import specs as analysis_specs
    from striqt.analysis.measurements import shared as striqt_shared
    _ANALYSIS_OK = True
    _ANALYSIS_ERR = None
except Exception as e:
    analysis_specs = None
    striqt_shared = None
    _ANALYSIS_OK = False
    _ANALYSIS_ERR = e

# FastAPI
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print(
        "FastAPI not installed. Run:\n"
        "  pip install fastapi 'uvicorn[standard]'",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHANNELS = (0, 1)

DEFAULT_CENTER      = 1955e6
DEFAULT_SAMPLE_RATE = 15.36e6
DEFAULT_GAIN        = 0.0
DEFAULT_NFFT        = 1024
DEFAULT_ROWS        = 12      # rows per frame (window_ms drives this from browser)

MASTER_CLOCK_RATE   = 125e6
READ_SIZE           = 1 << 18   # max IQ samples per _read_stream call (262144)

BROADCAST_FPS       = 15        # default max frames/sec to browsers
SCROLL_ROWS         = 12        # rows per frame in Cool (scroll/waterfall) mode
MAX_LIVE_ROWS       = 300       # safety cap on requested rows

WEB_DIR = Path(__file__).parent / "web"

# Backend: "calibrated" (striqt PSD/ENBW dB) or "quicklook" (simple FFT dB)
SPEC_BACKEND = os.environ.get("SPEC_BACKEND", "calibrated").strip().lower()


# ---------------------------------------------------------------------------
# Shared radio config (thread-safe)
# ---------------------------------------------------------------------------

@dataclass
class RadioConfig:
    center:      float = DEFAULT_CENTER
    sample_rate: float = DEFAULT_SAMPLE_RATE
    gain:        float = DEFAULT_GAIN
    nfft:        int   = DEFAULT_NFFT
    rows:        int   = DEFAULT_ROWS

    def snapshot(self):
        return RadioConfig(
            center=float(self.center),
            sample_rate=float(self.sample_rate),
            gain=float(self.gain),
            nfft=int(self.nfft),
            rows=int(self.rows),
        )


class SharedConfig:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cfg   = RadioConfig()
        self._dirty = False
        self._stop  = False

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

    def update(self, update: dict) -> bool:
        """Apply key/value updates. Returns True if anything changed."""
        valid = {"center", "sample_rate", "gain", "nfft", "rows"}
        changes = []
        with self._lock:
            for key, value in update.items():
                if key not in valid:
                    continue
                value = int(value) if key in {"nfft", "rows"} else float(value)
                # Clamp rows so a misbehaving browser can't overload the radio
                if key == "rows":
                    value = int(max(1, min(value, MAX_LIVE_ROWS)))
                old = getattr(self._cfg, key)
                if old == value:
                    continue
                setattr(self._cfg, key, value)
                changes.append((key, old, value))
            if changes:
                self._dirty = True
        # Print outside the lock to avoid I/O inside a mutex
        for key, old, value in changes:
            print(f"[config] {key}: {old} -> {value}")
        return bool(changes)

    def take_dirty(self):
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            return dirty, self._cfg.snapshot()

    def stop(self):
        with self._lock:
            self._stop = True

    def stopped(self):
        with self._lock:
            return self._stop


# ---------------------------------------------------------------------------
# striqt hardware shims
# (match the getattr pattern used in striqt_server_TCP.py so this works
#  against the installed striqt build which may differ from the vendored source)
# ---------------------------------------------------------------------------

def get_device(source):
    return getattr(source, "_device", getattr(source, "device", None))

def get_rx_stream(source):
    return getattr(source, "_rx_stream", getattr(source, "rx_stream", None))

def get_stream_ports(source):
    return tuple(getattr(get_rx_stream(source), "ports", CHANNELS))

def get_stream_mtu(source):
    rx = get_rx_stream(source)
    if rx is None:
        return None
    for name in ("mtu", "_mtu", "stream_mtu"):
        val = getattr(rx, name, None)
        if val is not None:
            try:
                return int(val)
            except Exception:
                pass
    stream = getattr(rx, "stream", None)
    dev    = get_device(source)
    if dev is not None and stream is not None:
        for meth in ("getStreamMTU", "get_stream_mtu"):
            fn = getattr(dev, meth, None)
            if fn is not None:
                try:
                    return int(fn(stream))
                except Exception:
                    pass
    return None

def open_stream(source):
    rx  = get_rx_stream(source)
    dev = get_device(source)
    if rx is None or dev is None:
        raise RuntimeError("striqt source has no RX stream/device")
    if getattr(rx, "stream", None) is None:
        rx.open(dev)

def enable_stream(source, enabled):
    rx     = get_rx_stream(source)
    if rx is None:
        return
    dev    = get_device(source)
    stream = getattr(rx, "stream", None)
    if dev is None or stream is None:
        return
    methods = (("activateStream", "activate_stream") if enabled
               else ("deactivateStream", "deactivate_stream"))
    for meth in methods:
        fn = getattr(dev, meth, None)
        if fn is not None:
            try:
                fn(stream)
                return
            except TypeError:
                try:
                    fn(stream, 0, 0, 0)
                    return
                except Exception:
                    pass
            except Exception:
                pass

def close_source(source):
    for action in [lambda: enable_stream(source, False),
                   lambda: _close_rx_stream(source),
                   lambda: source.close()]:
        try:
            action()
        except Exception:
            pass

def _close_rx_stream(source):
    rx = get_rx_stream(source)
    if rx is not None:
        dev = get_device(source)
        if dev is not None and getattr(rx, "stream", None) is not None:
            rx.close(dev)

def stream_buffers_for(source, samples):
    rx    = get_rx_stream(source)
    ports = tuple(getattr(rx, "ports", CHANNELS))
    return [samples[CHANNELS.index(p)].view(np.float32) for p in ports], ports


# ---------------------------------------------------------------------------
# Source / capture factories
# ---------------------------------------------------------------------------

def make_source():
    source_spec = Air8201BSourceSpec(
        master_clock_rate=MASTER_CLOCK_RATE,
        array_backend="numpy",
        time_source="host",
        time_sync_at="open",
        clock_source="internal",
        gapless=True,
        receive_retries=0,
    )
    return Airstack1Source.from_spec(source_spec)

def make_capture(cfg):
    return specs.SoapyCapture(
        port=CHANNELS,
        center_frequency=cfg.center,
        gain=tuple([cfg.gain] * len(CHANNELS)),
        duration=max(cfg.rows * cfg.nfft / cfg.sample_rate, 1e-3),
        sample_rate=cfg.sample_rate,
        backend_sample_rate=cfg.sample_rate,
        host_resample=False,
        analysis_bandwidth=float("inf"),
        lo_shift="none",
    )


# ---------------------------------------------------------------------------
# Spectrogram compute backends
# ---------------------------------------------------------------------------

def db_spectrogram(samples: np.ndarray, nfft: int, rows: int) -> np.ndarray:
    """
    Quicklook: Hann window → FFT → normalized power dB.
    Returns (channels, rows, nfft) float32, fftshifted, oldest-row-first.
    """
    samples = np.asarray(samples, dtype=np.complex64)
    needed  = rows * nfft
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]
    x      = samples.reshape(samples.shape[0], rows, nfft)
    window = np.hann(nfft).astype(np.float32)
    x      = x * window[None, None, :]
    spec   = np.fft.fftshift(np.fft.fft(x, axis=-1), axes=-1)
    # Normalize by window power (proper PSD estimate)
    power  = (np.abs(spec) ** 2) / max(float(np.sum(window ** 2)), 1.0)
    return (10.0 * np.log10(power + 1e-20)).astype(np.float32)


def calibrated_spectrogram(
    samples: np.ndarray, nfft: int, rows: int, sample_rate: float
) -> np.ndarray:
    """
    striqt-calibrated PSD spectrogram (ENBW-normalized dB).
    Returns (channels, rows, nfft) float32, fftshifted, oldest-row-first.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"calibrated backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    needed  = rows * nfft
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    sample_rate = float(sample_rate)
    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=needed / sample_rate,
        analysis_bandwidth=float("inf"),
    )
    spec = analysis_specs.Spectrogram(
        window="hann",
        frequency_resolution=sample_rate / float(nfft),
    )
    striqt_shared.spectrogram_cache.clear()
    spg, _ = striqt_shared.evaluate_spectrogram(
        samples, capture, spec, dtype="float32", dB=True
    )
    spg = np.asarray(spg, dtype=np.float32)

    # Null DC/LO leakage spike (same treatment as striqt_standalone.py)
    c = spg.shape[-1] // 2
    spg[:, :, c - 2 : c + 3] = spg.min(axis=-1, keepdims=True)

    # Guarantee (channels, rows, nfft) shape contract
    if spg.shape[1] != rows:
        spg = spg[:, -rows:, :]
        if spg.shape[1] < rows:
            fill = float(spg.min()) if spg.size > 0 else -200.0
            pad  = np.full(
                (spg.shape[0], rows - spg.shape[1], nfft), fill, dtype=np.float32
            )
            spg = np.concatenate([pad, spg], axis=1)
    if spg.shape[2] != nfft:
        raise RuntimeError(
            f"calibrated_spectrogram: freq bins {spg.shape[2]} != nfft {nfft}"
        )
    return spg


def compute_blocks(samples: np.ndarray, cfg: RadioConfig) -> np.ndarray:
    """
    Dispatch to the configured backend.
    Returns (channels, rows, nfft) float32.
    """
    if SPEC_BACKEND == "calibrated":
        return calibrated_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate)
    return db_spectrogram(samples, cfg.nfft, cfg.rows)


# ---------------------------------------------------------------------------
# Acquirer thread (real AIR8201B hardware)
# ---------------------------------------------------------------------------

class Acquirer(threading.Thread):
    """
    Reads raw IQ from the AIR8201B into a ring buffer, computes spectrogram
    blocks, and stores the latest frame in a single-slot newest-wins slot.
    The broadcaster reads latest() at BROADCAST_FPS to fan out to all clients.
    """

    def __init__(self, shared: SharedConfig):
        super().__init__(daemon=True)
        self.shared       = shared
        self.source       = None
        self.stream_mtu   = None
        self.stream_ports = CHANNELS
        self._lock            = threading.Lock()
        self._latest_header   = None
        self._latest_blocks   = None

    # --- Public interface (thread-safe) ---

    def latest(self):
        """Return (header_dict, [block_array, ...]) of the most recent frame."""
        with self._lock:
            if self._latest_header is None:
                return None, None
            return dict(self._latest_header), [b.copy() for b in self._latest_blocks]

    def publish(self, cfg: RadioConfig, blocks: list):
        header = {
            "center":   float(cfg.center),
            "fs":       float(cfg.sample_rate),
            "gain":     float(cfg.gain),
            "nfft":     int(cfg.nfft),
            "rows":     int(cfg.rows),
            "shape":    [int(cfg.rows), int(cfg.nfft)],
            "channels": list(CHANNELS),
            "time":     time.time(),
        }
        with self._lock:
            self._latest_header = header
            self._latest_blocks = [np.asarray(b, dtype=np.float32) for b in blocks]

    # --- Hardware management ---

    def open_radio(self, cfg: RadioConfig):
        self.source = make_source()
        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)
        self.stream_mtu   = get_stream_mtu(self.source)
        self.stream_ports = get_stream_ports(self.source)
        print(
            f"[radio] armed: center {cfg.center/1e6:.2f} MHz, "
            f"{cfg.sample_rate/1e6:.3f} MS/s, channels {CHANNELS}, "
            f"backend={SPEC_BACKEND}"
        )

    def rearm(self, cfg: RadioConfig):
        if self.source is None:
            self.open_radio(cfg)
            return
        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)
        print(
            f"[radio] retune: center {cfg.center/1e6:.2f} MHz, "
            f"{cfg.sample_rate/1e6:.3f} MS/s, gain {cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}"
        )

    def _make_read_buffers(self):
        read_size     = min(self.stream_mtu or READ_SIZE, READ_SIZE)
        tmp           = np.empty((len(CHANNELS), read_size), dtype=np.complex64)
        buffers, _    = stream_buffers_for(self.source, tmp)
        return read_size, tmp, buffers

    def _recover(self, cfg: RadioConfig, reason: str):
        """Close and reopen the radio. Returns new (read_size, tmp, buffers)."""
        print(f"[radio] recovering after: {reason}")
        if self.source is not None:
            close_source(self.source)
            self.source = None
        time.sleep(0.25)
        self.open_radio(cfg)
        return self._make_read_buffers()

    # --- Main loop ---

    def run(self):
        cfg = self.shared.snapshot()
        try:
            self.open_radio(cfg)
            read_size, tmp, buffers = self._make_read_buffers()
            last_log = 0.0

            while not self.shared.stopped():
                dirty, new_cfg = self.shared.take_dirty()
                if dirty:
                    cfg = new_cfg
                    try:
                        self.rearm(cfg)
                        read_size, tmp, buffers = self._make_read_buffers()
                    except Exception as e:
                        try:
                            read_size, tmp, buffers = self._recover(cfg, str(e))
                        except Exception as re:
                            print(f"[radio] recovery failed: {re}; retry in 1s")
                            time.sleep(1.0)
                        continue

                # Guard: if source is None (recovery failed and we slept), retry
                if self.source is None:
                    time.sleep(0.1)
                    continue

                try:
                    got, _ = self.source._read_stream(
                        buffers,
                        offset=0,
                        count=read_size,
                        timeout_sec=read_size / cfg.sample_rate + 0.1,
                        on_overflow="log",
                    )
                except (ReceiveStreamError, OverflowError, OSError) as e:
                    try:
                        read_size, tmp, buffers = self._recover(cfg, str(e))
                    except Exception as re:
                        print(f"[radio] recovery failed: {re}; retry in 1s")
                        time.sleep(1.0)
                    continue

                if got <= 0:
                    time.sleep(0.001)
                    continue

                iq = tmp[:, :got].copy()
                try:
                    blocks = compute_blocks(iq, cfg)
                    self.publish(cfg, [blocks[i] for i in range(blocks.shape[0])])
                except Exception as e:
                    print(f"[compute] error: {e}")

                now = time.time()
                if now - last_log > 5.0:
                    print(
                        f"[radio] IQ {iq.shape} {iq.dtype}  "
                        f"backend={SPEC_BACKEND}"
                    )
                    last_log = now

        finally:
            if self.source is not None:
                close_source(self.source)


# ---------------------------------------------------------------------------
# Demo acquirer (synthetic IQ — no hardware needed)
# ---------------------------------------------------------------------------

class DemoAcquirer(threading.Thread):
    """
    Generates synthetic IQ data (Gaussian noise + CW tones) and feeds it
    through the same compute_blocks path as the real Acquirer.
    Exposes the same latest()/publish() interface.
    """

    def __init__(self, shared: SharedConfig):
        super().__init__(daemon=True)
        self.shared           = shared
        self._lock            = threading.Lock()
        self._latest_header   = None
        self._latest_blocks   = None

    def latest(self):
        with self._lock:
            if self._latest_header is None:
                return None, None
            return dict(self._latest_header), [b.copy() for b in self._latest_blocks]

    def _publish(self, cfg: RadioConfig, blocks: list):
        header = {
            "center":   float(cfg.center),
            "fs":       float(cfg.sample_rate),
            "gain":     float(cfg.gain),
            "nfft":     int(cfg.nfft),
            "rows":     int(cfg.rows),
            "shape":    [int(cfg.rows), int(cfg.nfft)],
            "channels": list(CHANNELS),
            "time":     time.time(),
            "demo":     True,
        }
        with self._lock:
            self._latest_header = header
            self._latest_blocks = [np.asarray(b, dtype=np.float32) for b in blocks]

    def run(self):
        rng = np.random.default_rng(42)
        print("[demo] Synthetic IQ mode — no radio hardware used.")
        print("[demo] Two CW tones per channel + noise. Controls work normally.")

        while not self.shared.stopped():
            cfg = self.shared.snapshot()
            n   = cfg.rows * cfg.nfft
            t   = np.arange(n, dtype=np.float32) / cfg.sample_rate

            # Channel 0: two tones offset from center
            sig0 = (
                0.30 * np.exp(2j * np.pi *  2.5e6 * t) +
                0.12 * np.exp(2j * np.pi * -1.8e6 * t)
            ).astype(np.complex64)
            noise0 = (rng.standard_normal(n) + 1j * rng.standard_normal(n)
                      ).astype(np.complex64) * 0.04

            # Channel 1: different tones
            sig1 = (
                0.20 * np.exp(2j * np.pi * -3.2e6 * t) +
                0.08 * np.exp(2j * np.pi *  4.1e6 * t)
            ).astype(np.complex64)
            noise1 = (rng.standard_normal(n) + 1j * rng.standard_normal(n)
                      ).astype(np.complex64) * 0.04

            samples = np.stack([sig0 + noise0, sig1 + noise1])
            try:
                blocks = compute_blocks(samples, cfg)
                self._publish(cfg, [blocks[i] for i in range(blocks.shape[0])])
            except Exception as e:
                print(f"[demo] compute error: {e}")

            time.sleep(max(1.0 / BROADCAST_FPS, 0.02))


# ---------------------------------------------------------------------------
# Frame serialization (browser-friendly binary WebSocket message)
# ---------------------------------------------------------------------------

def serialize_frame(header: dict, blocks: list, quantize: bool = False) -> bytes:
    """
    Pack a complete spectrogram frame into a single binary WebSocket message:

        [4-byte LE uint32 : header JSON byte length]
        [UTF-8 JSON header bytes]
        [block-0 raw bytes]   (float32 LE, or uint8 if quantize=True)
        [block-1 raw bytes]
        ...

    With quantize=True the header gains:
        "dtype": "uint8"
        "scale": [vmin_dB, vmax_dB]
    and each block is a uint8 array (0=vmin, 255=vmax). ~4× smaller payload.
    PSD accuracy is unaffected because the browser recomputes PSD from the
    dequantized blocks, which differ from float32 by at most 1/255 of the dB range.
    """
    if quantize and blocks:
        # Use per-frame global range so quantization is consistent across channels
        all_vals = np.concatenate([b.ravel() for b in blocks])
        vmin = float(np.percentile(all_vals, 1))
        vmax = float(np.percentile(all_vals, 99))
        if vmax - vmin < 1.0:
            vmax = vmin + 1.0
        hdr       = dict(header, dtype="uint8", scale=[vmin, vmax])
        hdr_bytes = json.dumps(hdr).encode("utf-8")
        parts     = [struct.pack("<I", len(hdr_bytes)), hdr_bytes]
        rng       = vmax - vmin
        for block in blocks:
            u8 = ((np.asarray(block, dtype=np.float32) - vmin) / rng * 255
                  ).clip(0, 255).astype(np.uint8)
            parts.append(u8.tobytes(order="C"))
    else:
        hdr_bytes = json.dumps(header).encode("utf-8")
        parts     = [struct.pack("<I", len(hdr_bytes)), hdr_bytes]
        for block in blocks:
            parts.append(np.asarray(block, dtype=np.float32, order="C").tobytes())
    return b"".join(parts)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

# Module-level globals set in main() before uvicorn starts
_acquirer: "Acquirer | DemoAcquirer | None" = None
_shared:   "SharedConfig | None"             = None
_quantize: bool                              = False
_connections: set                            = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the acquirer thread and broadcaster on startup; clean up on shutdown."""
    _acquirer.start()
    # Give the radio (or demo) a moment to produce the first frame
    await asyncio.sleep(1.2)
    task = asyncio.create_task(_broadcaster())
    print(f"[ws] broadcaster running at {BROADCAST_FPS} fps")
    try:
        yield
    finally:
        _shared.stop()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except Exception:
            pass
        _acquirer.join(timeout=3.0)


app = FastAPI(title="striqt live viewer", lifespan=lifespan)


async def _broadcaster():
    """
    Polls acquirer.latest() at BROADCAST_FPS, serializes the frame once, and
    fans it out to all connected WebSocket clients. Dropped connections are
    pruned from the set.
    """
    interval = 1.0 / max(BROADCAST_FPS, 1)
    last_t   = 0.0

    while True:
        await asyncio.sleep(interval)

        if not _connections:
            continue

        # latest() is fast (threading.Lock + numpy copy) — no executor needed
        header, blocks = _acquirer.latest()
        if header is None:
            continue
        frame_t = header.get("time", 0.0)
        if frame_t == last_t:
            continue   # no new frame since last broadcast
        last_t = frame_t

        try:
            msg = serialize_frame(header, blocks, _quantize)
        except Exception as e:
            print(f"[ws] serialize error: {e}")
            continue

        dead = set()
        for ws in list(_connections):
            try:
                await ws.send_bytes(msg)
            except Exception:
                dead.add(ws)
        _connections -= dead


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    WebSocket endpoint. Receives control messages as text JSON:
        {"center": Hz, "sample_rate": Hz, "gain": dB, "nfft": int, "rows": int}
    Sends spectrogram frames as binary (see serialize_frame).
    """
    await ws.accept()
    _connections.add(ws)
    client = ws.client
    print(f"[ws] client connected: {client}")
    try:
        while True:
            try:
                text = await asyncio.wait_for(ws.receive_text(), timeout=15.0)
                ctrl = json.loads(text)
                _shared.update(ctrl)
            except asyncio.TimeoutError:
                # Keepalive: connection stays open, no data in 15 s is normal
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws] client {client} error: {e}")
    finally:
        _connections.discard(ws)
        print(f"[ws] client disconnected: {client}")


# Mount static files last so the /ws route takes priority
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
else:
    @app.get("/")
    async def root():
        return {
            "error": f"Web assets not found at {WEB_DIR}",
            "hint": "Did you create live/web/index.html?",
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _acquirer, _shared, _quantize, BROADCAST_FPS, SPEC_BACKEND

    parser = argparse.ArgumentParser(
        description="striqt WebSocket live viewer server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--demo",     action="store_true",
                        help="Use synthetic IQ (no radio hardware)")
    parser.add_argument("--quantize", action="store_true",
                        help="Encode waterfall as uint8 (~4x smaller frames)")
    parser.add_argument("--fps",      type=float, default=BROADCAST_FPS,
                        help="Max broadcast frame rate (fps)")
    parser.add_argument("--backend",  default=SPEC_BACKEND,
                        choices=["calibrated", "quicklook"],
                        help="Spectrogram backend")
    parser.add_argument("--host",     default="0.0.0.0",
                        help="Bind address")
    parser.add_argument("--port",     type=int, default=8000,
                        help="Listen port")
    args = parser.parse_args()

    if args.demo and not _ANALYSIS_OK and SPEC_BACKEND == "calibrated":
        print("[demo] striqt.analysis unavailable; falling back to quicklook backend")
        SPEC_BACKEND = "quicklook"
    else:
        SPEC_BACKEND = args.backend

    if not args.demo and not _SENSOR_OK:
        print(
            "ERROR: striqt.sensor not importable (radio hardware deps missing).\n"
            "  Run with --demo for synthetic IQ, or install the striqt radio stack.",
            file=sys.stderr,
        )
        sys.exit(1)

    BROADCAST_FPS = max(args.fps, 0.5)
    _quantize     = args.quantize
    _shared       = SharedConfig()
    _acquirer     = DemoAcquirer(_shared) if args.demo else Acquirer(_shared)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn not installed. Run:\n  pip install 'uvicorn[standard]'",
            file=sys.stderr,
        )
        sys.exit(1)

    mode    = "DEMO (synthetic IQ)" if args.demo else "AIR8201B radio"
    q_note  = " + uint8 quantization" if _quantize else ""
    print(f"\nstriqt web viewer — {mode}")
    print(f"  backend={SPEC_BACKEND}, fps={BROADCAST_FPS:.0f}{q_note}")
    print(f"  listening on http://{args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  local:    http://localhost:{args.port}")
    print(
        f"  tunnel:   cloudflared tunnel --url http://localhost:{args.port}\n"
        f"            (or run:  bash live/run_web.sh)\n"
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
