#!/usr/bin/env python3
"""
striqt-backed AIR8201 live server.

Runs on the Deepwave AIR-T / AIR8201-B and streams spectrogram frames to the Mac
viewer over TCP.

Keep this CPU-only for now. CuPy/GPU is intentionally disabled because importing
CuPy before/around SoapySDR caused a libstdc++ / GLIBCXX conflict on the AIR-T.
"""

import json
import math
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

import numpy as np

from striqt.sensor import specs
from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source

# Calibrated spectrogram backend (striqt.analysis). Imported defensively so the
# default "quicklook" path keeps working even if the analysis package fails to
# import on this box. evaluate_spectrogram is NOT re-exported at the
# striqt.analysis top level -- it lives in measurements.shared.
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

# Runtime backend toggle, read once at startup. A/B by restarting with
# SPEC_BACKEND=quicklook (hand-rolled FFT) or SPEC_BACKEND=calibrated (striqt).
SPEC_BACKEND = os.environ.get("SPEC_BACKEND", "quicklook").strip().lower()

HOST = "0.0.0.0"
PORT = 5005

CHANNELS = (0, 1)

DEFAULT_CENTER = 1955e6
DEFAULT_SAMPLE_RATE = 15.36e6
DEFAULT_GAIN = 0.0
DEFAULT_NFFT = 1024
DEFAULT_ROWS = 12

MASTER_CLOCK_RATE = 125e6
MAX_TAIL = 1 << 22
READ_SIZE = 1 << 18

# CPU-only on purpose.
_cp = None
USE_GPU = False


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


class SharedConfig:
    def __init__(self):
        self._lock = threading.Lock()
        self._cfg = RadioConfig()
        self._dirty = False
        self._stop = False

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
        # Soapy complex float buffers are interleaved float32 I/Q.
        buffers.append(samples[ch_index].view(np.float32))

    return buffers, ports


def db_spectrogram(samples, nfft, rows):
    """
    Convert complex samples with shape (channels, N) to dB spectrogram blocks:
    shape (channels, rows, nfft), dtype float32.
    """
    samples = np.asarray(samples, dtype=np.complex64)

    needed = rows * nfft
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    x = samples.reshape(samples.shape[0], rows, nfft)

    window = np.hanning(nfft).astype(np.float32)
    x = x * window[None, None, :]

    spec = np.fft.fftshift(np.fft.fft(x, axis=-1), axes=-1)
    power = (np.abs(spec) ** 2) / max(float(nfft), 1.0)
    db = 10.0 * np.log10(power + 1e-20)

    return db.astype(np.float32)


def calibrated_spectrogram(samples, nfft, rows, sample_rate):
    """
    striqt-calibrated dB spectrogram. Returns (spg, attrs) where spg has the
    same output contract as db_spectrogram: shape (channels, rows, nfft),
    dtype float32, fftshifted (DC in the middle), earliest time bin first.

    Only the spectrogram math differs from the quicklook path. striqt's
    evaluate_spectrogram does the same STFT (window -> FFT -> fftshift) but with
    PSD/ENBW normalization and dB units. It reads only sample_rate and
    analysis_bandwidth from the Capture (gain/center are not used), so this is a
    dB-scaled spectrogram, not a fully absolute-dBm calibration.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(
            "SPEC_BACKEND=calibrated but striqt.analysis import failed: "
            f"{_ANALYSIS_ERR!r}"
        )

    samples = np.asarray(samples, dtype=np.complex64)

    # Feed exactly rows*nfft samples so the STFT yields exactly `rows` time bins
    # (fractional_overlap=0, window_fill=1). Same slice/pad as db_spectrogram.
    needed = rows * nfft
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    sample_rate = float(sample_rate)

    # Capture reads only sample_rate + analysis_bandwidth. duration*sample_rate
    # == rows*nfft (integer) satisfies Capture validation; analysis_bandwidth=inf
    # leaves the frequency axis untrimmed at exactly nfft bins.
    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=needed / sample_rate,
        analysis_bandwidth=float("inf"),
    )
    # nfft = round(sample_rate / frequency_resolution) internally, so set
    # frequency_resolution = sample_rate / nfft to recover the viewer's nfft.
    spec = analysis_specs.Spectrogram(
        window="hann",
        frequency_resolution=sample_rate / float(nfft),
    )

    # The (capture, spec) result cache is disabled by default for standalone
    # calls, but cfg is constant frame-to-frame here -- clear defensively so an
    # enabled cache could never freeze the display on the first frame.
    striqt_shared.spectrogram_cache.clear()

    spg, attrs = striqt_shared.evaluate_spectrogram(
        samples, capture, spec, dtype="float32", dB=True
    )

    spg = np.asarray(spg, dtype=np.float32)

    # Guarantee the (channels, rows, nfft) wire contract regardless.
    if spg.shape[1] != rows:
        spg = spg[:, -rows:, :]
    if spg.shape[2] != nfft:
        raise RuntimeError(
            f"calibrated spectrogram freq bins {spg.shape[2]} != nfft {nfft}; "
            "the JSON header promises nfft -- check analysis_bandwidth/trim."
        )

    return spg, attrs


def compute_blocks(samples, cfg):
    """
    Dispatch to the selected spectrogram backend. Returns (blocks, attrs) where
    blocks is (channels, rows, nfft) float32 and attrs is the striqt attrs dict
    (calibrated) or None (quicklook).
    """
    if SPEC_BACKEND == "calibrated":
        return calibrated_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate)

    return db_spectrogram(samples, cfg.nfft, cfg.rows), None


class Acquirer(threading.Thread):
    def __init__(self, shared):
        super().__init__(daemon=True)
        self.shared = shared

        self.source = None
        self.stream_mtu = None
        self.stream_ports = CHANNELS

        self._lock = threading.Lock()
        self._latest_header = None
        self._latest_blocks = None

        self._read_count = 0

    def latest(self):
        with self._lock:
            if self._latest_header is None or self._latest_blocks is None:
                return None, None

            header = dict(self._latest_header)
            blocks = [b.copy() for b in self._latest_blocks]

        return header, blocks

    def publish(self, cfg, blocks):
        header = {
            "center": float(cfg.center),
            "fs": float(cfg.sample_rate),
            "gain": float(cfg.gain),
            "nfft": int(cfg.nfft),
            "rows": int(cfg.rows),
            "shape": [int(cfg.rows), int(cfg.nfft)],
            "channels": list(CHANNELS),
            "time": time.time(),
        }

        with self._lock:
            self._latest_header = header
            self._latest_blocks = [np.asarray(b, dtype=np.float32) for b in blocks]

    def open_radio(self, cfg):
        self.source = make_source()
        open_stream(self.source)

        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)

        self.stream_mtu = get_stream_mtu(self.source)
        self.stream_ports = get_stream_ports(self.source)

        max_read_chunk = min(self.stream_mtu or READ_SIZE, READ_SIZE)

        print(
            f"Radio armed through installed striqt: center {cfg.center / 1e6:.2f} MHz, "
            f"{cfg.sample_rate / 1e6:.3f} MS/s, channels {CHANNELS}"
        )
        print(
            f"source={type(self.source).__name__}, capture=SoapyCapture, "
            f"stream_ports={self.stream_ports}, stream_mtu={self.stream_mtu}"
        )
        print(
            f"[initial] center={cfg.center / 1e6:.2f} MHz, "
            f"sample_rate={cfg.sample_rate / 1e6:.3f} MS/s, gain={cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}, "
            f"requested_capture_samples={cfg.rows * cfg.nfft}, "
            f"ring_capacity={MAX_TAIL}, max_read_chunk={max_read_chunk}, "
            f"stream_ports={self.stream_ports}"
        )
        print("FFT backend: CPU (numpy, batched)")
        print(
            f"SPEC_BACKEND={SPEC_BACKEND} "
            f"({'striqt calibrated dB spectrogram' if SPEC_BACKEND == 'calibrated' else 'quicklook relative-dB FFT'})"
        )

    def rearm(self, cfg):
        if self.source is None:
            self.open_radio(cfg)
            return

        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)

        print(
            f"[retune] center={cfg.center / 1e6:.2f} MHz, "
            f"sample_rate={cfg.sample_rate / 1e6:.3f} MS/s, gain={cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}"
        )

    def run(self):
        cfg = self.shared.snapshot()

        try:
            self.open_radio(cfg)

            read_size = min(self.stream_mtu or READ_SIZE, READ_SIZE)
            tmp = np.empty((len(CHANNELS), read_size), dtype=np.complex64)
            buffers, _ = stream_buffers_for(self.source, tmp)

            last_log = 0.0

            while not self.shared.stopped():
                dirty, new_cfg = self.shared.take_dirty()
                if dirty:
                    cfg = new_cfg
                    self.rearm(cfg)

                # Read a full chunk (READ_SIZE / stream MTU bound) each pass and
                # let db_spectrogram() slice the last rows*nfft samples from it.
                count = read_size

                got, _ = self.source._read_stream(
                    buffers,
                    offset=0,
                    count=count,
                    timeout_sec=count / cfg.sample_rate + 0.1,
                    on_overflow="log",
                )

                if got <= 0:
                    time.sleep(0.001)
                    continue

                iq = tmp[:, :got].copy()
                blocks, attrs = compute_blocks(iq, cfg)
                self.publish(cfg, [blocks[i] for i in range(blocks.shape[0])])

                self._read_count += 1
                now = time.time()
                if now - last_log > 5.0:
                    print(f"striqt returned sample shape/dtype: {iq.shape} {iq.dtype}")
                    if SPEC_BACKEND == "calibrated":
                        units = attrs.get("units") if attrs else None
                        print(
                            f"[calibrated] block min/max = "
                            f"{float(blocks.min()):.2f}/{float(blocks.max()):.2f} dB, "
                            f"units={units!r} -- set viewer color/PSD scale to this range"
                        )
                    last_log = now

        finally:
            if self.source is not None:
                close_source(self.source)


def send_frame(sock, header, blocks):
    payload = json.dumps(header).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)))
    sock.sendall(payload)

    for block in blocks:
        arr = np.asarray(block, dtype=np.float32, order="C")
        sock.sendall(arr.tobytes(order="C"))


def read_control_nonblocking(sock):
    """
    Read one optional control message from viewer.
    Returns dict or None.
    """
    ready, _, _ = select.select([sock], [], [], 0)

    if not ready:
        return None

    try:
        raw_len = sock.recv(4, socket.MSG_DONTWAIT)
    except BlockingIOError:
        return None

    if not raw_len:
        raise ConnectionError("viewer closed")

    if len(raw_len) < 4:
        raw_len += recvall_socket(sock, 4 - len(raw_len))

    n = struct.unpack(">I", raw_len)[0]
    payload = recvall_socket(sock, n)

    return json.loads(payload.decode("utf-8"))


def recvall_socket(sock, n):
    chunks = []
    got = 0

    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ConnectionError("viewer closed")
        chunks.append(chunk)
        got += len(chunk)

    return b"".join(chunks)


def serve(shared, acquirer):
    print(f"Listening on {HOST}:{PORT} -- start live_viewer_mac.py on the Mac.")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(1)

        while not shared.stopped():
            conn, addr = server.accept()
            print(f"Viewer connected from {addr}")

            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                try:
                    while not shared.stopped():
                        ctrl = read_control_nonblocking(conn)
                        if ctrl:
                            shared.update(ctrl)

                        header, blocks = acquirer.latest()
                        if header is None or blocks is None:
                            time.sleep(0.02)
                            continue

                        send_frame(conn, header, blocks)
                        time.sleep(0.02)

                except Exception as e:
                    print(f"Viewer disconnected: {e}")
                    print("Waiting for the viewer to reconnect ...")


def main():
    shared = SharedConfig()
    acquirer = Acquirer(shared)
    acquirer.start()

    try:
        # Give acquisition enough time to open/arm before accepting clients.
        time.sleep(1.0)
        serve(shared, acquirer)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        shared.stop()
        acquirer.join(timeout=3.0)


if __name__ == "__main__":
    main()
