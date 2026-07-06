#!/usr/bin/env python3
"""
Terminal interface AIR8201 live spectrogram + PSD monitor.

Meant to run over plain SSH.

Example:
    python live/airt_live_terminal.py --center-mhz 1955 --rate-msps 15.36 \
        --nfft 1024 --rows 40 --fps 3
"""

import argparse
import curses
import math
import signal
import threading
import time
from dataclasses import dataclass

import numpy as np

from striqt.sensor import specs
from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source

try:
    from striqt.sensor.lib.sources.soapy import ReceiveStreamError
except Exception:  # pragma: no cover - installed striqt version dependent
    try:
        from striqt.sensor.lib.sources.base import ReceiveStreamError
    except Exception:
        ReceiveStreamError = OSError

try:
    from striqt.analysis import specs as analysis_specs
    from striqt.analysis.measurements import shared as striqt_shared

    _ANALYSIS_OK = True
    _ANALYSIS_ERR = None
except Exception as e:  # pragma: no cover - depends on target install
    analysis_specs = None
    striqt_shared = None
    _ANALYSIS_OK = False
    _ANALYSIS_ERR = e


CHANNELS = (0, 1)

DEFAULT_CENTER = 1955e6
DEFAULT_SAMPLE_RATE = 15.36e6
DEFAULT_GAIN = 0.0
DEFAULT_NFFT = 1024
DEFAULT_ROWS = 40
DEFAULT_FPS = 3.0

MASTER_CLOCK_RATE = 125e6
MAX_TAIL = 1 << 22
READ_SIZE = 1 << 18
DATA_STALE_SEC = 1.0

RATES_MHZ = [3.84, 7.68, 15.36, 30.72, 61.44]
NFFTS = [256, 512, 1024, 2048, 4096]
GAIN_STEP_DB = 1.0

ASCII_RAMP = " .:-=+*#%@"
FIXED_DB_RANGE = (-120.0, -20.0)
_QUICK_WINDOWS = {}


@dataclass
class RadioConfig:
    center: float = DEFAULT_CENTER
    sample_rate: float = DEFAULT_SAMPLE_RATE
    gain: float = DEFAULT_GAIN
    nfft: int = DEFAULT_NFFT
    rows: int = DEFAULT_ROWS

    def snapshot(self):
        return RadioConfig(
            center=float(self.center),
            sample_rate=float(self.sample_rate),
            gain=float(self.gain),
            nfft=int(self.nfft),
            rows=int(self.rows),
        )


@dataclass
class FrameTiming:
    fetch_ms: float = 0.0
    compute_ms: float = 0.0
    draw_ms: float = 0.0
    total_ms: float = 0.0
    actual_fps: float = 0.0


class SharedConfig:
    def __init__(self, cfg):
        self._lock = threading.Lock()
        self._cfg = cfg.snapshot()
        self._dirty = True
        self._stop = False
        self._recover_requested = False

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

    def update(self, update):
        valid = {"center", "sample_rate", "gain", "nfft", "rows"}
        with self._lock:
            for key, value in update.items():
                if key not in valid:
                    continue
                if key in {"nfft", "rows"}:
                    value = int(value)
                else:
                    value = float(value)
                setattr(self._cfg, key, value)
            self._dirty = True

    def take_dirty(self):
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            return dirty, self._cfg.snapshot()

    def request_recover(self):
        with self._lock:
            self._recover_requested = True

    def take_recover_requested(self):
        with self._lock:
            requested = self._recover_requested
            self._recover_requested = False
            return requested

    def stop(self):
        with self._lock:
            self._stop = True

    def stopped(self):
        with self._lock:
            return self._stop


def get_device(source):
    return getattr(source, "_device", getattr(source, "device", None))


def get_rx_stream(source):
    return getattr(source, "_rx_stream", getattr(source, "rx_stream", None))


def get_stream_ports(source):
    rx_stream = get_rx_stream(source)
    return tuple(getattr(rx_stream, "ports", CHANNELS))


def get_stream_mtu(source):
    rx_stream = get_rx_stream(source)
    if rx_stream is None:
        return None

    for name in ("mtu", "_mtu", "stream_mtu"):
        val = getattr(rx_stream, name, None)
        if val is not None:
            try:
                return int(val)
            except Exception:
                pass

    stream = getattr(rx_stream, "stream", None)
    dev = get_device(source)
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
    rx_stream = get_rx_stream(source)
    dev = get_device(source)
    if rx_stream is None or dev is None:
        raise RuntimeError("striqt source has no RX stream/device")
    if getattr(rx_stream, "stream", None) is None:
        rx_stream.open(dev)


def enable_stream(source, enabled):
    rx_stream = get_rx_stream(source)
    if rx_stream is None:
        return

    dev = get_device(source)
    stream = getattr(rx_stream, "stream", None)
    if dev is None or stream is None:
        return

    if enabled:
        for meth in ("activateStream", "activate_stream"):
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
    else:
        for meth in ("deactivateStream", "deactivate_stream"):
            fn = getattr(dev, meth, None)
            if fn is not None:
                try:
                    fn(stream)
                    return
                except Exception:
                    pass


def close_source(source):
    try:
        enable_stream(source, False)
    except Exception:
        pass

    try:
        rx_stream = get_rx_stream(source)
        if rx_stream is not None:
            dev = get_device(source)
            if dev is not None and getattr(rx_stream, "stream", None) is not None:
                try:
                    rx_stream.close(dev)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        source.close()
    except Exception:
        pass


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


def stream_buffers_for(source, samples):
    rx_stream = get_rx_stream(source)
    ports = tuple(getattr(rx_stream, "ports", CHANNELS))

    buffers = []
    for port in ports:
        ch_index = CHANNELS.index(port)
        buffers.append(samples[ch_index].view(np.float32))

    return buffers, ports


def calibrated_spectrogram(samples, nfft, rows, sample_rate):
    """
    Return striqt-calibrated spectrogram blocks as (channels, rows, nfft)
    float32 dB values, fftshifted with DC in the middle.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(
            "calibrated spectrogram requires striqt.analysis, which failed to "
            f"import: {_ANALYSIS_ERR!r}"
        )

    samples = np.asarray(samples, dtype=np.complex64)
    needed = int(rows) * int(nfft)
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
    spg, attrs = striqt_shared.evaluate_spectrogram(
        samples, capture, spec, dtype="float32", dB=True
    )

    spg = np.asarray(spg, dtype=np.float32)
    c = spg.shape[-1] // 2
    dc_null = 2
    spg[:, :, c - dc_null : c + dc_null + 1] = spg.min(axis=-1, keepdims=True)

    if spg.shape[1] != rows:
        spg = spg[:, -rows:, :]
    if spg.shape[2] != nfft:
        raise RuntimeError(
            f"calibrated spectrogram freq bins {spg.shape[2]} != nfft {nfft}"
        )

    return spg, attrs


def quick_spectrogram(samples, nfft, rows, sample_rate):
    """
    Return an uncalibrated NumPy FFT spectrogram as (channels, rows, nfft)
    float32 dB values, fftshifted with DC in the middle.
    """
    del sample_rate
    samples = np.asarray(samples, dtype=np.complex64)
    nfft = int(nfft)
    rows = int(rows)
    needed = rows * nfft
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    blocks = samples.reshape(samples.shape[0], rows, nfft)
    window = _QUICK_WINDOWS.get(nfft)
    if window is None:
        window = np.hanning(nfft).astype(np.float32)
        _QUICK_WINDOWS[nfft] = window
    fft_blocks = np.fft.fft(blocks * window[None, None, :], axis=-1)
    fft_blocks = np.fft.fftshift(fft_blocks, axes=-1)
    power = (np.abs(fft_blocks) ** 2) / max(float(nfft), 1.0)
    db = (10.0 * np.log10(power + 1e-20)).astype(np.float32)
    return db, {"backend": "quick", "units": "quick/uncalibrated dB"}


class Acquirer(threading.Thread):
    def __init__(self, shared):
        super().__init__(daemon=True)
        self.shared = shared
        self.source = None
        self.stream_mtu = None
        self.stream_ports = CHANNELS

        self._lock = threading.Lock()
        self._ring = np.zeros((len(CHANNELS), MAX_TAIL), dtype=np.complex64)
        self._write = 0
        self._count = 0
        self._last_write = 0.0
        self._healthy = False
        self._status = "starting acquisition"
        self._last_error = ""

    def _set_status(self, status, healthy=None, error=None):
        with self._lock:
            self._status = str(status)
            if healthy is not None:
                self._healthy = bool(healthy)
            if error is not None:
                self._last_error = str(error)

    def status(self):
        with self._lock:
            status = self._status
            if self._healthy and self._last_write:
                age = time.time() - self._last_write
                if age > DATA_STALE_SEC:
                    status = f"waiting for fresh samples ({age:.1f}s stale)"
            return status

    def last_error(self):
        with self._lock:
            return self._last_error

    def stats(self):
        with self._lock:
            return {
                "healthy": bool(self._healthy),
                "ring_fill": int(min(self._count, MAX_TAIL)),
                "last_write": float(self._last_write),
                "ports": tuple(self.stream_ports),
                "mtu": self.stream_mtu,
            }

    def _clear_ring_locked(self):
        self._write = 0
        self._count = 0
        self._last_write = 0.0

    def _ring_write(self, iq):
        n = iq.shape[1]
        if n <= 0:
            return

        with self._lock:
            cap = MAX_TAIL
            if n >= cap:
                self._ring[:, :] = iq[:, -cap:]
                self._write = 0
                self._count = cap
                self._last_write = time.time()
                self._healthy = True
                self._status = "acquiring"
                return

            end = self._write + n
            if end <= cap:
                self._ring[:, self._write:end] = iq
            else:
                first = cap - self._write
                self._ring[:, self._write:] = iq[:, :first]
                self._ring[:, : n - first] = iq[:, first:]

            self._write = end % cap
            self._count = min(self._count + n, cap)
            self._last_write = time.time()
            self._healthy = True
            self._status = "acquiring"

    def clear_samples(self, status="waiting for fresh samples"):
        with self._lock:
            self._clear_ring_locked()
            self._healthy = False
            self._status = status

    def get_latest(self, n):
        n = int(n)
        if n <= 0:
            return None

        with self._lock:
            if (
                not self._healthy
                or self._count == 0
                or time.time() - self._last_write > DATA_STALE_SEC
            ):
                return None

            cap = MAX_TAIL
            avail = min(self._count, cap)
            take = min(n, avail)
            out = np.zeros((len(CHANNELS), n), dtype=np.complex64)

            start = (self._write - take) % cap
            end = start + take
            if end <= cap:
                out[:, n - take:] = self._ring[:, start:end]
            else:
                first = cap - start
                out[:, n - take : n - take + first] = self._ring[:, start:]
                out[:, n - take + first :] = self._ring[:, : take - first]

        return out

    def open_radio(self, cfg):
        self._set_status("opening radio", healthy=False)
        self.source = make_source()
        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)

        self.stream_mtu = get_stream_mtu(self.source)
        self.stream_ports = get_stream_ports(self.source)
        self._set_status("waiting for fresh samples", healthy=False)

    def rearm(self, cfg):
        if self.source is None:
            self.open_radio(cfg)
            return

        self._set_status("rearming radio", healthy=False)
        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)
        self.clear_samples("waiting for fresh samples after retune")

    def _make_read_buffers(self):
        read_size = min(self.stream_mtu or READ_SIZE, READ_SIZE)
        tmp = np.empty((len(CHANNELS), read_size), dtype=np.complex64)
        buffers, _ = stream_buffers_for(self.source, tmp)
        return read_size, tmp, buffers

    def recover_radio(self, cfg, reason):
        self._set_status(f"recovering acquisition: {reason}", healthy=False, error=reason)
        if self.source is not None:
            close_source(self.source)
            self.source = None
        self.clear_samples("recovering acquisition")
        time.sleep(0.25)
        self.open_radio(cfg)
        self._set_status("waiting for fresh samples after recovery", healthy=False)
        return self._make_read_buffers()

    def run(self):
        cfg = self.shared.snapshot()
        read_size = None
        tmp = None
        buffers = None

        try:
            while not self.shared.stopped():
                dirty, new_cfg = self.shared.take_dirty()
                recover_requested = self.shared.take_recover_requested()

                if dirty:
                    cfg = new_cfg
                    try:
                        self.rearm(cfg)
                        read_size, tmp, buffers = self._make_read_buffers()
                    except Exception as e:
                        try:
                            read_size, tmp, buffers = self.recover_radio(cfg, str(e))
                        except Exception as recover_err:
                            self._set_status(
                                f"acquisition recovery failed: {recover_err}",
                                healthy=False,
                                error=recover_err,
                            )
                            time.sleep(1.0)
                    continue

                if recover_requested or self.source is None or buffers is None:
                    try:
                        read_size, tmp, buffers = self.recover_radio(
                            cfg, "manual restart" if recover_requested else "not open"
                        )
                    except Exception as recover_err:
                        self._set_status(
                            f"acquisition recovery failed: {recover_err}",
                            healthy=False,
                            error=recover_err,
                        )
                        time.sleep(1.0)
                    continue

                count = read_size
                try:
                    got, _ = self.source._read_stream(
                        buffers,
                        offset=0,
                        count=count,
                        timeout_sec=count / cfg.sample_rate + 0.1,
                        on_overflow="log",
                    )
                except (ReceiveStreamError, OverflowError, OSError, RuntimeError) as e:
                    try:
                        read_size, tmp, buffers = self.recover_radio(cfg, str(e))
                    except Exception as recover_err:
                        self._set_status(
                            f"acquisition recovery failed: {recover_err}",
                            healthy=False,
                            error=recover_err,
                        )
                        time.sleep(1.0)
                    continue

                if got <= 0:
                    time.sleep(0.001)
                    continue

                self._ring_write(tmp[:, :got].copy())

        finally:
            if self.source is not None:
                close_source(self.source)


class AutoScaler:
    def __init__(self):
        self.low = None
        self.high = None

    def reset(self):
        self.low = None
        self.high = None

    def range_for(self, blocks):
        finite = np.asarray(blocks, dtype=np.float32)
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return FIXED_DB_RANGE

        low = float(np.percentile(finite, 5.0))
        high = float(np.percentile(finite, 99.0))
        if high - low < 20.0:
            mid = 0.5 * (low + high)
            low = mid - 10.0
            high = mid + 10.0

        if self.low is None or self.high is None:
            self.low = low
            self.high = high
        else:
            alpha = 0.15
            self.low = (1.0 - alpha) * self.low + alpha * low
            self.high = (1.0 - alpha) * self.high + alpha * high

        return self.low, self.high


def clamp_int(value, low, high):
    return max(low, min(high, int(value)))


def nearest_index(values, current):
    return min(range(len(values)), key=lambda i: abs(values[i] - current))


def format_db(value):
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):6.1f} dB"


def add_line(stdscr, y, text):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h:
        return
    stdscr.addnstr(y, 0, text.ljust(max(0, w - 1)), max(0, w - 1))


def downsample_row(row, width):
    row = np.asarray(row, dtype=np.float32)
    width = int(width)
    if width <= 0:
        return np.empty((0,), dtype=np.float32)
    if row.size == width:
        return row
    if row.size < width:
        x_old = np.linspace(0.0, 1.0, row.size)
        x_new = np.linspace(0.0, 1.0, width)
        return np.interp(x_new, x_old, row).astype(np.float32)

    edges = np.linspace(0, row.size, width + 1).astype(int)
    out = np.empty((width,), dtype=np.float32)
    for i in range(width):
        start = edges[i]
        stop = max(edges[i + 1], start + 1)
        out[i] = float(np.nanmean(row[start:stop]))
    return out


def block_to_ascii(block, width, height, db_low, db_high):
    if width <= 0 or height <= 0:
        return []

    block = np.asarray(block, dtype=np.float32)
    if block.ndim != 2 or block.size == 0:
        return [" " * width for _ in range(height)]

    block = block[-height:]
    if block.shape[0] < height:
        pad = np.full((height - block.shape[0], block.shape[1]), db_low, dtype=np.float32)
        block = np.vstack([pad, block])

    scale = max(float(db_high) - float(db_low), 1.0)
    chars = []
    for row in block:
        ds = downsample_row(row, width)
        norm = np.clip((ds - db_low) / scale, 0.0, 1.0)
        idx = np.rint(norm * (len(ASCII_RAMP) - 1)).astype(int)
        chars.append("".join(ASCII_RAMP[i] for i in idx))
    return chars


def summarize_channel(block, center, sample_rate):
    block = np.asarray(block, dtype=np.float32)
    if block.ndim != 2 or block.size == 0:
        return {
            "peak_freq": None,
            "peak_power": None,
            "span_avg": None,
            "center_avg": None,
            "psd": None,
        }

    psd = np.nanmean(block, axis=0)
    freqs = center + np.fft.fftshift(np.fft.fftfreq(psd.size, 1.0 / sample_rate))
    peak_idx = int(np.nanargmax(psd))
    band_bins = max(3, psd.size // 20)
    c = psd.size // 2
    lo = max(0, c - band_bins // 2)
    hi = min(psd.size, lo + band_bins)

    return {
        "peak_freq": float(freqs[peak_idx]),
        "peak_power": float(psd[peak_idx]),
        "span_avg": float(np.nanmean(psd)),
        "center_avg": float(np.nanmean(psd[lo:hi])),
        "psd": psd,
    }


def draw_screen(
    stdscr,
    cfg,
    acquirer,
    blocks,
    summaries,
    render_fps,
    compute_error,
    autoscale,
    paused,
    db_range,
    backend,
    timing,
    ascii_width,
):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    text_w = max(10, w - 1)
    stats = acquirer.stats()
    status = acquirer.status()

    if h < 12 or w < 40:
        add_line(stdscr, 0, "AIR-T terminal viewer: terminal too small")
        add_line(stdscr, 1, f"size {w}x{h}, need at least 40x12")
        stdscr.refresh()
        return

    y = 0
    mode = "paused" if paused else status
    add_line(
        stdscr,
        y,
        "AIR-T STRIQT terminal live viewer | "
        "q quit  a autoscale  p pause  r recover  +/- gain  [] fft  1/2 rate",
    )
    y += 1
    add_line(
        stdscr,
        y,
        f"status: {mode} | center {cfg.center / 1e6:.3f} MHz | "
        f"span {cfg.sample_rate / 1e6:.3f} MS/s | gain {cfg.gain:.1f} dB | "
        f"FFT {cfg.nfft} | rows {cfg.rows} | fps {render_fps:.1f} | "
        f"{backend}",
    )
    y += 1
    add_line(
        stdscr,
        y,
        f"ports {stats['ports']} | mtu {stats['mtu']} | "
        f"ring {stats['ring_fill']}/{MAX_TAIL} | "
        f"scale {'auto' if autoscale else 'fixed'} {db_range[0]:.1f}..{db_range[1]:.1f} dB",
    )
    y += 1
    add_line(
        stdscr,
        y,
        f"timing ms fetch {timing.fetch_ms:5.1f} | compute {timing.compute_ms:5.1f} | "
        f"draw {timing.draw_ms:5.1f} | total {timing.total_ms:5.1f} | "
        f"actual {timing.actual_fps:4.1f} fps",
    )
    y += 1

    if compute_error:
        add_line(stdscr, y, f"compute: {compute_error}"[:text_w])
    else:
        s1 = summaries[0] if summaries else None
        s2 = summaries[1] if summaries and len(summaries) > 1 else None
        if s1 and s2 and s1["peak_freq"] is not None and s2["peak_freq"] is not None:
            delta = None
            if s1["peak_power"] is not None and s2["peak_power"] is not None:
                delta = s1["peak_power"] - s2["peak_power"]
            add_line(
                stdscr,
                y,
                "RX1 peak "
                f"{s1['peak_freq'] / 1e6:10.3f} MHz {format_db(s1['peak_power'])} | "
                f"RX2 peak {s2['peak_freq'] / 1e6:10.3f} MHz {format_db(s2['peak_power'])} | "
                f"RX1-RX2 {format_db(delta)}",
            )
        else:
            add_line(stdscr, y, f"waiting for {backend} spectrogram")
    y += 1

    if summaries and len(summaries) >= 2:
        s1, s2 = summaries[0], summaries[1]
        add_line(
            stdscr,
            y,
            f"RX1 avg {format_db(s1['span_avg'])} center-band {format_db(s1['center_avg'])} | "
            f"RX2 avg {format_db(s2['span_avg'])} center-band {format_db(s2['center_avg'])}",
        )
    else:
        err = acquirer.last_error()
        add_line(stdscr, y, f"last error: {err}" if err else "")
    y += 1

    remaining = h - y - 1
    pane_height = max(1, (remaining - 2) // 2)
    pane_width = text_w if ascii_width is None else min(text_w, int(ascii_width))

    if blocks is None:
        add_line(stdscr, y, "RX1")
        for i in range(pane_height):
            add_line(stdscr, y + 1 + i, " " * pane_width)
        y += pane_height + 1
        add_line(stdscr, y, "RX2")
        for i in range(pane_height):
            add_line(stdscr, y + 1 + i, " " * pane_width)
        stdscr.refresh()
        return

    for ch_idx, label in enumerate(("RX1", "RX2")):
        if y >= h - 1:
            break
        add_line(stdscr, y, label)
        y += 1
        lines = block_to_ascii(blocks[ch_idx], pane_width, pane_height, db_range[0], db_range[1])
        for line in lines:
            if y >= h - 1:
                break
            add_line(stdscr, y, line)
            y += 1

    stdscr.refresh()


def handle_key(key, shared, cfg, state):
    if key in (-1, None):
        return True
    if key in (ord("q"), ord("Q")):
        return False
    if key in (ord("a"), ord("A")):
        state["autoscale"] = not state["autoscale"]
        state["scaler"].reset()
    elif key in (ord("p"), ord("P")):
        state["paused"] = not state["paused"]
    elif key in (ord("r"), ord("R")):
        shared.request_recover()
        state["blocks"] = None
        state["summaries"] = None
    elif key == ord("["):
        idx = nearest_index(NFFTS, cfg.nfft)
        shared.update({"nfft": NFFTS[max(0, idx - 1)]})
        state["blocks"] = None
        state["summaries"] = None
        state["scaler"].reset()
    elif key == ord("]"):
        idx = nearest_index(NFFTS, cfg.nfft)
        shared.update({"nfft": NFFTS[min(len(NFFTS) - 1, idx + 1)]})
        state["blocks"] = None
        state["summaries"] = None
        state["scaler"].reset()
    elif key in (ord("-"), ord("_")):
        shared.update({"gain": cfg.gain - GAIN_STEP_DB})
        state["blocks"] = None
        state["summaries"] = None
    elif key in (ord("+"), ord("=")):
        shared.update({"gain": cfg.gain + GAIN_STEP_DB})
        state["blocks"] = None
        state["summaries"] = None
    elif key == ord("1"):
        current = cfg.sample_rate / 1e6
        idx = nearest_index(RATES_MHZ, current)
        shared.update({"sample_rate": RATES_MHZ[max(0, idx - 1)] * 1e6})
        state["blocks"] = None
        state["summaries"] = None
        state["scaler"].reset()
    elif key == ord("2"):
        current = cfg.sample_rate / 1e6
        idx = nearest_index(RATES_MHZ, current)
        shared.update({"sample_rate": RATES_MHZ[min(len(RATES_MHZ) - 1, idx + 1)] * 1e6})
        state["blocks"] = None
        state["summaries"] = None
        state["scaler"].reset()
    return True


def curses_main(stdscr, shared, acquirer, target_fps, backend, ascii_width, autoscale):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(0)

    state = {
        "autoscale": bool(autoscale),
        "paused": False,
        "blocks": None,
        "summaries": None,
        "scaler": AutoScaler(),
    }
    compute_error = ""
    render_fps = 0.0
    timing = FrameTiming()
    frame_count = 0
    fps_t0 = time.time()
    frame_period = 1.0 / max(float(target_fps), 0.25)

    while not shared.stopped():
        frame_t0 = time.perf_counter()
        fetch_ms = 0.0
        compute_ms = 0.0
        cfg = shared.snapshot()
        try:
            while True:
                key = stdscr.getch()
                if key == -1:
                    break
                if not handle_key(key, shared, cfg, state):
                    shared.stop()
                    break
                cfg = shared.snapshot()
        except curses.error:
            pass

        if shared.stopped():
            break

        if not state["paused"]:
            fetch_t0 = time.perf_counter()
            samples = acquirer.get_latest(cfg.nfft * cfg.rows)
            fetch_ms = (time.perf_counter() - fetch_t0) * 1000.0
            if samples is not None:
                try:
                    compute_t0 = time.perf_counter()
                    if backend == "calibrated":
                        blocks, _ = calibrated_spectrogram(
                            samples, cfg.nfft, cfg.rows, cfg.sample_rate
                        )
                    else:
                        blocks, _ = quick_spectrogram(
                            samples, cfg.nfft, cfg.rows, cfg.sample_rate
                        )
                    state["blocks"] = blocks
                    state["summaries"] = [
                        summarize_channel(blocks[0], cfg.center, cfg.sample_rate),
                        summarize_channel(blocks[1], cfg.center, cfg.sample_rate),
                    ]
                    compute_ms = (time.perf_counter() - compute_t0) * 1000.0
                    compute_error = ""
                except Exception as e:
                    compute_ms = (time.perf_counter() - compute_t0) * 1000.0
                    compute_error = str(e)

        if state["autoscale"] and state["blocks"] is not None:
            db_range = state["scaler"].range_for(state["blocks"])
        else:
            db_range = FIXED_DB_RANGE

        frame_count += 1
        now = time.time()
        if now - fps_t0 >= 1.0:
            render_fps = frame_count / (now - fps_t0)
            frame_count = 0
            fps_t0 = now

        draw_t0 = time.perf_counter()
        draw_screen(
            stdscr,
            cfg,
            acquirer,
            state["blocks"],
            state["summaries"],
            render_fps,
            compute_error,
            state["autoscale"],
            state["paused"],
            db_range,
            "calibrated dB" if backend == "calibrated" else "quick/uncalibrated dB",
            timing,
            ascii_width,
        )
        draw_ms = (time.perf_counter() - draw_t0) * 1000.0
        total_ms = (time.perf_counter() - frame_t0) * 1000.0
        timing = FrameTiming(
            fetch_ms=fetch_ms,
            compute_ms=compute_ms,
            draw_ms=draw_ms,
            total_ms=total_ms,
            actual_fps=render_fps,
        )

        elapsed = time.perf_counter() - frame_t0
        time.sleep(max(0.01, frame_period - elapsed))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Terminal-only AIR-T STRIQT live waterfall/PSD monitor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Terminal performance tip:\n"
            "  If total frame time exceeds the FPS budget, try --nfft 512 "
            "--rows 20.\n"
            "  Use --backend quick to isolate terminal drawing from STRIQT "
            "calibrated compute cost."
        ),
    )
    parser.add_argument("--center-mhz", type=float, default=DEFAULT_CENTER / 1e6)
    parser.add_argument("--rate-msps", type=float, default=DEFAULT_SAMPLE_RATE / 1e6)
    parser.add_argument("--gain", type=float, default=DEFAULT_GAIN)
    parser.add_argument("--nfft", type=int, default=DEFAULT_NFFT)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument(
        "--backend",
        choices=("calibrated", "quick"),
        default="calibrated",
        help="spectrogram backend: STRIQT calibrated dB or NumPy quick/uncalibrated dB",
    )
    parser.add_argument(
        "--ascii-width",
        type=int,
        default=None,
        help="maximum waterfall character width; defaults to terminal width",
    )
    parser.add_argument(
        "--no-autoscale",
        action="store_true",
        help="start with fixed dB scale instead of autoscale",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = RadioConfig(
        center=float(args.center_mhz) * 1e6,
        sample_rate=float(args.rate_msps) * 1e6,
        gain=float(args.gain),
        nfft=min(NFFTS, key=lambda x: abs(x - args.nfft)),
        rows=clamp_int(args.rows, 1, 200),
    )
    target_fps = min(max(float(args.fps), 0.5), 10.0)
    ascii_width = None
    if args.ascii_width is not None:
        ascii_width = clamp_int(args.ascii_width, 10, 400)

    shared = SharedConfig(cfg)
    acquirer = Acquirer(shared)

    def stop_now(signum, frame):
        del signum, frame
        shared.stop()

    signal.signal(signal.SIGINT, stop_now)
    signal.signal(signal.SIGTERM, stop_now)

    acquirer.start()
    try:
        curses.wrapper(
            curses_main,
            shared,
            acquirer,
            target_fps,
            args.backend,
            ascii_width,
            not args.no_autoscale,
        )
    finally:
        shared.stop()
        acquirer.join(timeout=3.0)


if __name__ == "__main__":
    main()
