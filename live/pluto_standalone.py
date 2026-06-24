#!/usr/bin/env python3
"""
PlutoSDR version of live spectrogram + PSD viewer -- single machine, no network.

"""

import csv
import os
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

from striqt.sensor import specs
from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source
from striqt.sensor.lib.sources.soapy import SoapySource as _SoapySource
try:
    from striqt.sensor.lib.sources.soapy import ReceiveStreamError
except Exception:  # pragma: no cover - installed striqt version dependent
    try:
        from striqt.sensor.lib.sources.base import ReceiveStreamError
    except Exception:
        ReceiveStreamError = OSError

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

# PyQt5 (PyQt6 is not available on the target machine). Pin pyqtgraph's binding
# before importing it so it can't latch onto a different Qt wrapper.
os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"
from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import pyqtgraph.exporters


# ---------------------------------------------------------------------------
# PlutoSource adapter
#
# Subclasses Airstack1Source to reuse all of its striqt stream/arm/read
# machinery, but overrides __init__ to call SoapySource.__init__ directly.
# This skips two things in Airstack1Source.__init__ that crash on a Pluto:
#   1. driver='SoapyAIRT'  -- replaced with driver='plutosdr'
#   2. _set_jesd_sysref_delay()  -- AIR-T FPGA register write, not present on Pluto
# deepwave.py is not modified.
# ---------------------------------------------------------------------------

class PlutoSource(Airstack1Source):
    def __init__(self, spec, **kwargs):
        _SoapySource.__init__(self, spec, driver='plutosdr', **kwargs)

    def get_id(self):
        try:
            return self.device.getHardwareKey()
        except Exception:
            return 'pluto'

    def read_peripherals(self):
        return {}


# ------------------------------ radio settings -----------------------------
CHANNELS = (0,)

DEFAULT_CENTER = 1955e6
DEFAULT_SAMPLE_RATE = 15.36e6
DEFAULT_GAIN = 0.0
DEFAULT_NFFT = 1024
DEFAULT_ROWS = 12

MASTER_CLOCK_RATE = 125e6
MAX_TAIL = 1 << 22
READ_SIZE = 1 << 18
DATA_STALE_SEC = 1.0

# ------------------------------- viewer settings ---------------------------
SCROLL_ROWS = 12                 # rows/update used in Cool (waterfall) mode
WINDOW_MS_DEFAULT = 20          # time-scale menu default (milliseconds)
WINDOW_PRESETS = [10, 20, 50, 100, 200, 500, 1000]
DEFAULT_FS = 15.36e6             # default span until the first frame arrives
MAX_ROWS = 20000                 # safety cap on buffer depth / snapshot rows
MAX_LIVE_ROWS = 300              # live GUI stability cap, not a hardware limit
LIVE_GUI_MAX_RATE_MHZ = 30.72
ALLOW_UNSAFE_61_44 = os.environ.get("AIR_LIVE_ALLOW_UNSAFE_61_44") == "1"
PSD_YRANGE = (-80.0, 20.0)       # fixed PSD power axis (dB): -80 at bottom, +20 at top
# Span presets are the standard LTE/5G-NR sample rates (multiples of 1.92 MHz:
# x4=7.68, x8=15.36, x16=30.72, x32=61.44), which line up with the cellular
# bands this rig looks at. 61.44 MS/s overflows in the current Python live GUI
# path even with rows capped; keep it out of normal GUI mode unless explicitly
# enabled for unsafe developer testing.
RATES_MHZ = [3.84, 7.68, 15.36, 30.72]
if ALLOW_UNSAFE_61_44:
    RATES_MHZ.append(61.44)
RATE_MIN_MHZ = 3.90625
RATE_MAX_MHZ = 61.44 if ALLOW_UNSAFE_61_44 else LIVE_GUI_MAX_RATE_MHZ
NFFTS = [256, 512, 1024, 2048, 4096]
# PSD pens: RX1 (cyan/yellow) and RX2 (orange/magenta), mean vs max per channel.
RX1_MEAN_PEN = pg.mkPen(color=(80, 220, 220), width=2)    # cyan
RX1_MAX_PEN = pg.mkPen(color=(245, 215, 80), width=2)     # yellow
RX2_MEAN_PEN = pg.mkPen(color=(255, 150, 70), width=2)    # orange
RX2_MAX_PEN = pg.mkPen(color=(235, 120, 235), width=2)    # magenta
HOLD1_PEN = pg.mkPen(color=(80, 220, 220, 110), width=1,   # faint cyan, dashed
                     style=QtCore.Qt.DashLine)
HOLD2_PEN = pg.mkPen(color=(255, 150, 70, 110), width=1,   # faint orange, dashed
                     style=QtCore.Qt.DashLine)
DIFF_PEN = pg.mkPen(color=(235, 235, 235), width=2)        # white
MIN1_PEN = pg.mkPen(color=(80, 220, 220, 140), width=1,    # dotted cyan
                    style=QtCore.Qt.DotLine)
MIN2_PEN = pg.mkPen(color=(255, 150, 70, 140), width=1,    # dotted orange
                    style=QtCore.Qt.DotLine)
# Display-rate presets for the "JEEZ SLOW DOWN" control -> max visual fps.
SPEED_PRESETS = [("Off (full speed)", 0.0), ("15 fps", 15.0), ("8 fps", 8.0),
                 ("4 fps", 4.0), ("2 fps", 2.0), ("1 fps", 1.0)]
# ---------------------------------------------------------------------------


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
        self._changes = []
        self._stop = False

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

    def update(self, update):
        valid = {"center", "sample_rate", "gain", "nfft", "rows"}
        labels = {
            "center": "radio",
            "sample_rate": "radio",
            "gain": "radio",
            "nfft": "analysis/display",
            "rows": "analysis/display",
        }

        with self._lock:
            changes = []
            for key, value in update.items():
                if key not in valid:
                    continue

                if key in {"nfft", "rows"}:
                    value = int(value)
                else:
                    value = float(value)

                old = getattr(self._cfg, key)
                if old == value:
                    continue

                setattr(self._cfg, key, value)
                changes.append((key, old, value, labels[key]))

            if not changes:
                print("[config] no-op update ignored")
                return []

            for key, old, value, label in changes:
                print(
                    f"[config] changed ({label}): {key} "
                    f"{format_config_value(key, old)} -> {format_config_value(key, value)}"
                )
            self._changes.extend(changes)
            self._dirty = True
            return changes

    def take_dirty(self):
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            changes = self._changes
            self._changes = []
            return dirty, self._cfg.snapshot(), changes

    def stop(self):
        with self._lock:
            self._stop = True

    def stopped(self):
        with self._lock:
            return self._stop


def format_config_value(key, value):
    if key in {"center", "sample_rate"}:
        return f"{float(value) / 1e6:.3f}e6"
    if key == "gain":
        return f"{float(value):.1f}"
    return str(int(value))


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

    source = PlutoSource(source_spec)
    source.setup()
    return source


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


def calibrated_spectrogram(samples, nfft, rows, sample_rate):
    """
    striqt-calibrated dB spectrogram. Returns (spg, attrs) where spg has shape
    (channels, rows, nfft), dtype float32, fftshifted (DC in the middle),
    earliest time bin first.

    striqt's evaluate_spectrogram does an STFT (window -> FFT -> fftshift) with
    PSD/ENBW normalization and dB units. It reads only sample_rate and
    analysis_bandwidth from the Capture (gain/center are not used), so this is a
    dB-scaled spectrogram, not a fully absolute-dBm calibration.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(
            "calibrated spectrogram requires striqt.analysis, which failed to "
            f"import: {_ANALYSIS_ERR!r}"
        )

    samples = np.asarray(samples, dtype=np.complex64)

    # Feed exactly rows*nfft samples so the STFT yields exactly `rows` time bins
    # (fractional_overlap=0, window_fill=1): slice/pad to rows*nfft samples.
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

    # Null the DC/LO leakage spike: replace the 5 center frequency bins with the
    # per-row minimum so the bright center column doesn't dominate the display.
    c = spg.shape[-1] // 2
    dc_null = 2
    spg[:, :, c - dc_null : c + dc_null + 1] = spg.min(axis=-1, keepdims=True)

    # Guarantee the (channels, rows, nfft) contract regardless.
    if spg.shape[1] != rows:
        spg = spg[:, -rows:, :]
    if spg.shape[2] != nfft:
        raise RuntimeError(
            f"calibrated spectrogram freq bins {spg.shape[2]} != nfft {nfft}; "
            "the header promises nfft -- check analysis_bandwidth/trim."
        )

    return spg, attrs


def compute_blocks(samples, cfg):
    """
    Compute calibrated spectrogram blocks. Returns (blocks, attrs) where blocks
    is (channels, rows, nfft) float32 and attrs is the striqt attrs dict.
    """
    return calibrated_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate)


class Acquirer(threading.Thread):
    """
    Reads raw IQ from the radio into a per-channel ring buffer. Block
    computation now lives in LocalReceiver, which pulls samples via get_latest().
    """

    def __init__(self, shared):
        super().__init__(daemon=True)
        self.shared = shared

        self.source = None
        self.stream_mtu = None
        self.stream_ports = CHANNELS

        # Per-channel raw IQ ring buffer (complex64). A single write pointer and
        # sample count are shared across channels since every read fills all
        # channels with the same number of samples.
        self._lock = threading.Lock()
        self._ring = np.zeros((len(CHANNELS), MAX_TAIL), dtype=np.complex64)
        self._write = 0          # next write index (mod MAX_TAIL)
        self._count = 0          # total samples written (saturates at MAX_TAIL)
        self._last_write = 0.0
        self._healthy = False
        self._status = "starting acquisition"
        self._events = []

    def _set_status(self, status, healthy=None):
        with self._lock:
            self._status = status
            if healthy is not None:
                self._healthy = bool(healthy)

    def _push_event(self, message, level="INFO"):
        with self._lock:
            self._events.append((message, level))
            self._events = self._events[-100:]

    def take_events(self):
        with self._lock:
            events = self._events
            self._events = []
            return events

    def status(self):
        with self._lock:
            if self._healthy and self._last_write:
                age = time.time() - self._last_write
                if age > DATA_STALE_SEC:
                    return f"waiting for fresh samples ({age:.1f}s stale)"
            return self._status

    def _clear_ring_locked(self):
        self._write = 0
        self._count = 0
        self._last_write = 0.0

    def _ring_write(self, iq):
        """Append raw IQ (channels, n) into the ring buffer with wraparound."""
        n = iq.shape[1]
        if n <= 0:
            return

        with self._lock:
            cap = MAX_TAIL
            if n >= cap:
                # Only the newest `cap` samples can survive.
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

    def get_latest(self, n):
        """
        Return the most recent `n` complex samples per channel, shape
        (channels, n) complex64, chronological (oldest -> newest). Front-padded
        with zeros if fewer than `n` samples exist yet. Returns None if the ring
        is still empty.
        """
        n = int(n)
        if n <= 0:
            return None

        with self._lock:
            if (not self._healthy or self._count == 0
                    or time.time() - self._last_write > DATA_STALE_SEC):
                return None

            cap = MAX_TAIL
            avail = min(self._count, cap)
            take = min(n, avail)

            out = np.zeros((len(CHANNELS), n), dtype=np.complex64)
            # The newest sample sits just before self._write; walk back `take`.
            start = (self._write - take) % cap
            end = start + take
            if end <= cap:
                out[:, n - take:] = self._ring[:, start:end]
            else:
                first = cap - start
                out[:, n - take:n - take + first] = self._ring[:, start:]
                out[:, n - take + first:] = self._ring[:, : take - first]

        return out

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
        print("FFT backend: striqt calibrated")
        self._push_event(
            f"radio armed: center {cfg.center / 1e6:.2f} MHz, "
            f"{cfg.sample_rate / 1e6:.3f} MS/s, channels {CHANNELS}"
        )

    def rearm(self, cfg):
        if self.source is None:
            self.open_radio(cfg)
            return

        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)

        # Drop stale samples captured at the old tuning so they never mix into a
        # frame alongside samples from the new center/rate.
        with self._lock:
            self._clear_ring_locked()
            self._healthy = False
            self._status = "waiting for fresh samples after retune"

        print(
            f"[retune] center={cfg.center / 1e6:.2f} MHz, "
            f"sample_rate={cfg.sample_rate / 1e6:.3f} MS/s, gain={cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}"
        )
        self._push_event(
            f"retune: center {cfg.center / 1e6:.2f} MHz, "
            f"sample_rate {cfg.sample_rate / 1e6:.3f} MS/s, "
            f"gain {cfg.gain:.1f} dB, nfft {cfg.nfft}, rows {cfg.rows}"
        )

    def _make_read_buffers(self):
        read_size = min(self.stream_mtu or READ_SIZE, READ_SIZE)
        tmp = np.empty((len(CHANNELS), read_size), dtype=np.complex64)
        buffers, _ = stream_buffers_for(self.source, tmp)
        return read_size, tmp, buffers

    def recover_radio(self, cfg, reason):
        print(f"acquirer: receive stream error ({reason}); rebuilding radio")
        self._push_event(f"receive stream error: {reason}; rebuilding radio", "ERROR")
        self._set_status(f"recovering acquisition: {reason}", healthy=False)
        if self.source is not None:
            close_source(self.source)
            self.source = None
        with self._lock:
            self._clear_ring_locked()
        time.sleep(0.25)
        self.open_radio(cfg)
        self._set_status("waiting for fresh samples after recovery", healthy=False)
        self._push_event("recovery complete; waiting for fresh samples", "WARN")
        return self._make_read_buffers()

    def run(self):
        cfg = self.shared.snapshot()

        try:
            self.open_radio(cfg)

            read_size, tmp, buffers = self._make_read_buffers()

            last_log = 0.0

            while not self.shared.stopped():
                dirty, new_cfg, changes = self.shared.take_dirty()
                if dirty:
                    cfg = new_cfg
                    if changes:
                        why = ", ".join(
                            f"{key} ({label})" for key, _, _, label in changes
                        )
                        print(f"[config] rearm requested after changes: {why}")
                        self._push_event(f"rearm requested after changes: {why}")
                    try:
                        self.rearm(cfg)
                        read_size, tmp, buffers = self._make_read_buffers()
                    except Exception as e:
                        try:
                            read_size, tmp, buffers = self.recover_radio(cfg, str(e))
                        except Exception as recover_err:
                            self._set_status(
                                f"acquisition recovery failed: {recover_err}",
                                healthy=False)
                            print(f"acquirer: recovery failed: {recover_err}")
                            self._push_event(f"acquisition recovery failed: {recover_err}",
                                             "ERROR")
                            time.sleep(1.0)
                        continue

                # Read a full chunk (READ_SIZE / stream MTU bound) each pass and
                # push the raw IQ into the ring. LocalReceiver slices the last
                # rows*nfft samples it needs from get_latest().
                count = read_size

                try:
                    got, _ = self.source._read_stream(
                        buffers,
                        offset=0,
                        count=count,
                        timeout_sec=count / cfg.sample_rate + 0.1,
                        on_overflow="log",
                    )
                except (ReceiveStreamError, OverflowError, OSError) as e:
                    try:
                        read_size, tmp, buffers = self.recover_radio(cfg, str(e))
                    except Exception as recover_err:
                        self._set_status(f"acquisition recovery failed: {recover_err}",
                                         healthy=False)
                        print(f"acquirer: recovery failed: {recover_err}")
                        self._push_event(f"acquisition recovery failed: {recover_err}",
                                         "ERROR")
                        time.sleep(1.0)
                    continue

                if got <= 0:
                    time.sleep(0.001)
                    continue

                iq = tmp[:, :got].copy()
                self._ring_write(iq)

                now = time.time()
                if now - last_log > 5.0:
                    print(
                        f"striqt read sample shape/dtype: {iq.shape} {iq.dtype}; "
                        f"ring fill {min(self._count, MAX_TAIL)}/{MAX_TAIL} samples"
                    )
                    last_log = now

        finally:
            if self.source is not None:
                close_source(self.source)


class LocalReceiver(QtCore.QThread):
    """
    In-process replacement for the TCP Receiver. Pulls the latest raw IQ from
    the Acquirer, computes spectrogram blocks, and emits the same header dict
    that on_frame expects -- no socket, no wire protocol.
    """

    frameReady = QtCore.pyqtSignal(object)
    statusChanged = QtCore.pyqtSignal(str)
    logMessage = QtCore.pyqtSignal(str, str)

    def __init__(self, acquirer, shared):
        super().__init__()
        self.acquirer = acquirer
        self.shared = shared
        self._running = True
        self.gui_busy = False
        self._last_log = 0.0
        self._last_status = ""
        self._last_noop_log = 0.0

    def stop(self):
        self._running = False

    def send_control(self, d):
        # Same call sites as the networked viewer; now updates shared config
        # directly so the Acquirer rearms the radio in-process.
        changes = self.shared.update(d)
        if changes:
            for key, old, value, label in changes:
                self.logMessage.emit(
                    f"config changed ({label}): {key} "
                    f"{format_config_value(key, old)} -> {format_config_value(key, value)}",
                    "INFO",
                )
        else:
            now = time.time()
            if now - self._last_noop_log > 2.0:
                self.logMessage.emit("config no-op update ignored", "DEBUG")
                self._last_noop_log = now

    def _emit_status_once(self, status):
        if status != self._last_status:
            self.statusChanged.emit(status)
            self._last_status = status

    def run(self):
        self._emit_status_once("running locally (no network)")
        while self._running:
            try:
                for message, level in self.acquirer.take_events():
                    self.logMessage.emit(message, level)

                cfg = self.shared.snapshot()
                samples = self.acquirer.get_latest(cfg.nfft * cfg.rows)
                if samples is None:
                    self._emit_status_once(self.acquirer.status())
                    self.msleep(33)
                    continue

                blocks, attrs = compute_blocks(samples, cfg)

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
                header["blocks"] = [blocks[i] for i in range(blocks.shape[0])]

                now = time.time()
                if now - self._last_log > 5.0:
                    units = attrs.get("units") if attrs else None
                    print(
                        f"[calibrated] block min/max = "
                        f"{float(blocks.min()):.2f}/{float(blocks.max()):.2f} dB, "
                        f"units={units!r}"
                    )
                    self._last_log = now

                if not self.gui_busy:
                    self.gui_busy = True
                    self.frameReady.emit(header)
                self._emit_status_once("running locally (no network)")
            except Exception as e:
                self._emit_status_once(f"compute error: {e}")

            self.msleep(33)


class LiveViewer(QtWidgets.QMainWindow):
    def __init__(self, acquirer, shared):
        super().__init__()
        self.setWindowTitle("PlutoSDR Live Spectrogram + PSD (standalone)")
        self.resize(1280, 900)

        self.replace_mode = True        # flicker is the main view; waterfall optional
        self.nfft = None
        self.center = None
        self.fs = None
        self.window_ms = WINDOW_MS_DEFAULT
        self.buffers = {}
        self.images = {}
        self.specplots = {}
        self.paused = False
        self.absolute = True
        self.auto_scale = True
        self.levels = (-90.0, -10.0)
        self.min_interval = 0.0         # min seconds between visual updates (0 = full)
        self._last_render = 0.0
        self._refit_pending = False     # set by span/window changes -> autoRange next frame
        self.show_diff = False          # RX1-RX2 difference view
        self.peak_marker = True         # label the strongest bin
        self.peak_hold = False          # hold max-ever envelope
        self.show_min = False           # min-over-time (noise floor) traces
        self.psd_yspan = None           # fixed PSD y-axis span in dB (None = auto)
        self.crosshair = True           # mouse readout on the PSD
        self.hold1 = self.hold2 = None  # running peak-hold arrays
        self._frames, self._t_fps, self._fps = 0, time.time(), 0.0
        self._geom = self._freqs = None
        self._row_cap_message = ""
        self._last_row_cap_log = ""

        self._build_ui()

        # Single-channel: grey out RX2 panels and disable controls that need 2 channels.
        if len(CHANNELS) == 1:
            self._suppress_rx2()

        self.log_event("app starting")
        self.receiver = LocalReceiver(acquirer, shared)
        self.receiver.frameReady.connect(self.on_frame)
        self.receiver.statusChanged.connect(self._on_status_changed)
        self.receiver.logMessage.connect(self.log_event)
        self.receiver.start()
        # Start in flicker mode: request a snapshot of this many rows (using the
        # defaults until the first frame tells us the real fs / nfft).
        self.receiver.send_control(
            {"rows": self._rows_for_window(DEFAULT_FS, DEFAULT_NFFT)})

    # --------------------------------------------------- rows <-> time helper
    def _rows_for_window(self, fs, nfft):
        """How many FFT rows make up `window_ms` at this fs / nfft."""
        rows = int(max(1, min(round(self.window_ms / 1000.0 * fs / nfft), MAX_ROWS)))
        capped = min(rows, MAX_LIVE_ROWS)
        if capped != rows:
            self._row_cap_message = (
                f"rows capped from {rows} to {capped} for live stability"
            )
            if self._row_cap_message != self._last_row_cap_log:
                print(self._row_cap_message)
                self.log_event(self._row_cap_message, "WARN")
                self._last_row_cap_log = self._row_cap_message
        else:
            self._row_cap_message = ""
            self._last_row_cap_log = ""
        return capped

    def log_event(self, message, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {level:<5} {message}"
        self.log_widget.appendPlainText(line)
        self.log_widget.verticalScrollBar().setValue(
            self.log_widget.verticalScrollBar().maximum()
        )
        if level in {"WARN", "ERROR"}:
            self.status_label.setText(message)

    def _on_status_changed(self, status):
        self.status_label.setText(status)
        level = "ERROR" if "error" in status.lower() or "failed" in status.lower() else "INFO"
        self.log_event(status, level)

    # ----------------------------------------------------------------- UI
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.addWidget(self._controls())

        self.status_label = QtWidgets.QLabel("starting ...")
        self.status_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.status_label)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setWordWrap(True)
        outer.addWidget(self.meta_label)

        # Pinned band-power monitor -- fixed on screen so the level stays in one
        # place (Eric's flicker ask), readable even while the spectrogram updates.
        self.band_label = QtWidgets.QLabel("Band monitor: --")
        self.band_label.setStyleSheet(
            "font-family: Menlo, Consolas, monospace; font-size: 15px; "
            "font-weight: bold; color: #ffd24a; background: #1b2733; "
            "padding: 5px; border-radius: 4px;")
        outer.addWidget(self.band_label)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        outer.addWidget(split, stretch=1)

        self.graphics = pg.GraphicsLayoutWidget()
        titles = {0: "Spectrogram Port 0 -- RX1",
                  1: "Spectrogram Port 1 -- RX2 (unavailable on this device)"}
        col = 0
        for ch in (0, 1):
            plot = self.graphics.addPlot(row=0, col=col)
            plot.setTitle(titles[ch])
            plot.setLabel("bottom", "Frequency (MHz)")
            plot.setLabel("left", "Time (ms, 0 = now at top)")
            plot.showGrid(x=True, y=True, alpha=0.2)
            # invert Y so the time axis reads 0 at the top (newest) and the
            # window length at the bottom (oldest) -- newest data sits on top.
            plot.getViewBox().invertY(True)
            img = pg.ImageItem()
            plot.addItem(img)
            hist = pg.HistogramLUTItem()
            hist.setImageItem(img)
            hist.gradient.loadPreset("viridis")
            self.graphics.addItem(hist, row=0, col=col + 1)
            self.specplots[ch] = plot
            self.images[ch] = img
            col += 2
        split.addWidget(self.graphics)

        self.psd_plot = pg.PlotWidget()
        self.psd_plot.setTitle("Power Spectral Density (RX1 + RX2)")
        self.psd_plot.setLabel("bottom", "Frequency (MHz)")
        self.psd_plot.setLabel("left", "Power (dB)")
        self.psd_plot.setYRange(*PSD_YRANGE, padding=0)   # fixed -80..+20 dB
        self.psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self.psd_plot.addLegend(offset=(20, 20))
        self.psd_rx1_mean = self.psd_plot.plot(name="RX1 Mean", pen=RX1_MEAN_PEN)
        self.psd_rx1_max = self.psd_plot.plot(name="RX1 Max", pen=RX1_MAX_PEN)
        self.psd_rx2_mean = self.psd_plot.plot(name="RX2 Mean", pen=RX2_MEAN_PEN)
        self.psd_rx2_max = self.psd_plot.plot(name="RX2 Max", pen=RX2_MAX_PEN)
        # peak-hold envelopes (faint, dashed) -- max-ever per bin while held
        self.psd_hold1 = self.psd_plot.plot(name="RX1 hold", pen=HOLD1_PEN)
        self.psd_hold2 = self.psd_plot.plot(name="RX2 hold", pen=HOLD2_PEN)
        self.psd_hold1.setVisible(False)
        self.psd_hold2.setVisible(False)
        # RX1 - RX2 difference (directivity), hidden until toggled on
        self.psd_diff = self.psd_plot.plot(name="RX1-RX2 (dB)", pen=DIFF_PEN)
        self.psd_diff.setVisible(False)
        # min-over-time traces (noise floor), hidden until toggled on
        self.psd_min1 = self.psd_plot.plot(name="RX1 min", pen=MIN1_PEN)
        self.psd_min2 = self.psd_plot.plot(name="RX2 min", pen=MIN2_PEN)
        self.psd_min1.setVisible(False)
        self.psd_min2.setVisible(False)
        # Single measurement band (green): defines the band-power monitor AND is
        # what "Tune to selection" tunes to. No fill -- just two thin draggable
        # edge lines, so it doesn't sit as a box over the PSD.
        self.meas_region = pg.LinearRegionItem(
            brush=(120, 255, 160, 0),
            pen=pg.mkPen((120, 255, 160), width=2))
        self.meas_region.setZValue(-4)
        self.meas_region.sigRegionChanged.connect(lambda *_: self._update_band())
        self.psd_plot.addItem(self.meas_region)
        # peak marker: dot + label on the strongest bin of the displayed max curve
        self.peak_dot = pg.ScatterPlotItem(size=10, brush=pg.mkBrush(245, 215, 80),
                                           pen=pg.mkPen("k"))
        self.peak_text = pg.TextItem(color=(245, 215, 80), anchor=(0.5, 1.2))
        self.psd_plot.addItem(self.peak_dot)
        self.psd_plot.addItem(self.peak_text)
        # crosshair + readout
        cpen = pg.mkPen((150, 150, 150), width=1,
                        style=QtCore.Qt.DashLine)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=cpen)
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=cpen)
        self.cursor_text = pg.TextItem(color=(210, 210, 210), anchor=(0, 1))
        for it in (self.vline, self.hline, self.cursor_text):
            self.psd_plot.addItem(it, ignoreBounds=True)
        self._mouse_proxy = pg.SignalProxy(self.psd_plot.scene().sigMouseMoved,
                                           rateLimit=60, slot=self._on_mouse_moved)
        split.addWidget(self.psd_plot)

        self.log_widget = QtWidgets.QPlainTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setMaximumBlockCount(400)
        self.log_widget.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.log_widget.setFont(QtGui.QFont("Menlo", 10))
        self.log_widget.setStyleSheet(
            "background: #0d1117; color: #d0d0d0; border: 1px solid #30363d;"
        )
        split.addWidget(self.log_widget)
        split.setSizes([520, 420, 160])      # keep plots primary, log visible below

    def _suppress_rx2(self):
        """Hide/disable all RX2 display elements when running single-channel."""
        self.psd_rx2_mean.setVisible(False)
        self.psd_rx2_max.setVisible(False)
        self.psd_plot.setTitle("Power Spectral Density (RX1)")
        self.diff_chk.setEnabled(False)
        self.diff_chk.setToolTip("RX1-RX2 diff requires two channels")

    def _controls(self):
        box = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(4, 4, 4, 4)

        radio = QtWidgets.QGroupBox("Radio (PlutoSDR)")
        rl = QtWidgets.QHBoxLayout(radio)
        rl.addWidget(QtWidgets.QLabel("Center (MHz):"))
        self.center_spin = QtWidgets.QDoubleSpinBox()
        self.center_spin.setRange(300.0, 6000.0)
        self.center_spin.setDecimals(3)
        self.center_spin.setValue(1955.0)
        self.center_spin.setSingleStep(5.0)
        self.center_spin.editingFinished.connect(
            lambda: self.receiver.send_control({"center": self.center_spin.value() * 1e6}))
        rl.addWidget(self.center_spin)
        rl.addWidget(QtWidgets.QLabel("Span (MS/s):"))
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.setEditable(True)
        self.rate_combo.setValidator(
            QtGui.QDoubleValidator(RATE_MIN_MHZ, RATE_MAX_MHZ, 5, self))
        self.rate_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        for r in RATES_MHZ:
            self.rate_combo.addItem(f"{r:.2f}", r)
        self.rate_combo.setCurrentText("15.36")
        self.rate_combo.activated.connect(self._change_rate)
        self.rate_combo.lineEdit().editingFinished.connect(self._change_rate)
        rl.addWidget(self.rate_combo)
        rl.addWidget(QtWidgets.QLabel("Gain (dB):"))
        self.gain_spin = QtWidgets.QDoubleSpinBox()
        self.gain_spin.setRange(-30.0, 0.0)
        self.gain_spin.setSingleStep(0.5)
        self.gain_spin.setValue(0.0)
        self.gain_spin.editingFinished.connect(
            lambda: self.receiver.send_control({"gain": self.gain_spin.value()}))
        rl.addWidget(self.gain_spin)
        rl.addWidget(QtWidgets.QLabel("FFT:"))
        self.nfft_combo = QtWidgets.QComboBox()
        for n in NFFTS:
            self.nfft_combo.addItem(str(n), n)
        self.nfft_combo.setCurrentText("1024")
        self.nfft_combo.activated.connect(self._change_nfft)
        rl.addWidget(self.nfft_combo)
        self.tune_btn = QtWidgets.QPushButton("Tune to selection")
        self.tune_btn.clicked.connect(self._tune_to_selection)
        rl.addWidget(self.tune_btn)
        grid.addWidget(radio, 0, 0)

        disp = QtWidgets.QGroupBox("Display")
        dl = QtWidgets.QHBoxLayout(disp)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        dl.addWidget(self.pause_btn)
        dl.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Boring Mode 🥱", "Cool Mode 😎"])
        self.mode_combo.setCurrentText("Boring Mode 🥱")   # main view = flicker
        self.mode_combo.currentTextChanged.connect(self._toggle_mode)
        dl.addWidget(self.mode_combo)
        dl.addWidget(QtWidgets.QLabel("Window (ms):"))
        self.win_combo = QtWidgets.QComboBox()
        self.win_combo.setEditable(True)
        self.win_combo.setValidator(QtGui.QIntValidator(5, 60000, self))
        self.win_combo.addItems([str(v) for v in WINDOW_PRESETS])
        self.win_combo.setCurrentText(str(WINDOW_MS_DEFAULT))
        self.win_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.win_combo.activated.connect(self._change_window)             # picked
        self.win_combo.lineEdit().editingFinished.connect(self._change_window)  # typed
        dl.addWidget(self.win_combo)
        dl.addWidget(QtWidgets.QLabel("JEEZ SLOW DOWN:"))
        self.speed_combo = QtWidgets.QComboBox()
        for label, _ in SPEED_PRESETS:
            self.speed_combo.addItem(label)
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.activated.connect(self._change_speed)
        dl.addWidget(self.speed_combo)
        self.auto_chk = QtWidgets.QCheckBox("Auto color")
        self.auto_chk.setChecked(True)
        self.auto_chk.stateChanged.connect(lambda s: setattr(self, "auto_scale", bool(s)))
        dl.addWidget(self.auto_chk)
        self.abs_chk = QtWidgets.QCheckBox("Absolute RF")
        self.abs_chk.setChecked(True)
        self.abs_chk.stateChanged.connect(self._toggle_absolute)
        dl.addWidget(self.abs_chk)
        self.reset_btn = QtWidgets.QPushButton("Reset view")
        self.reset_btn.clicked.connect(self._reset_view)
        dl.addWidget(self.reset_btn)
        self.csv_btn = QtWidgets.QPushButton("Save PSD CSV")
        self.csv_btn.clicked.connect(self._save_csv)
        dl.addWidget(self.csv_btn)
        self.png_btn = QtWidgets.QPushButton("Export PNG")
        self.png_btn.clicked.connect(self._export_png)
        dl.addWidget(self.png_btn)
        grid.addWidget(disp, 1, 0)

        psd = QtWidgets.QGroupBox("PSD tools")
        pl = QtWidgets.QHBoxLayout(psd)
        self.diff_chk = QtWidgets.QCheckBox("RX1−RX2 diff")
        self.diff_chk.stateChanged.connect(self._toggle_diff)
        pl.addWidget(self.diff_chk)
        self.peak_chk = QtWidgets.QCheckBox("Peak marker")
        self.peak_chk.setChecked(True)
        self.peak_chk.stateChanged.connect(
            lambda s: (setattr(self, "peak_marker", bool(s)), self._update_psd()))
        pl.addWidget(self.peak_chk)
        self.hold_chk = QtWidgets.QCheckBox("Peak hold")
        self.hold_chk.stateChanged.connect(self._toggle_hold)
        pl.addWidget(self.hold_chk)
        self.holdclear_btn = QtWidgets.QPushButton("Clear hold")
        self.holdclear_btn.clicked.connect(self._clear_hold)
        pl.addWidget(self.holdclear_btn)
        self.min_chk = QtWidgets.QCheckBox("Min trace")
        self.min_chk.stateChanged.connect(self._toggle_min)
        pl.addWidget(self.min_chk)
        self.cross_chk = QtWidgets.QCheckBox("Crosshair")
        self.cross_chk.setChecked(True)
        self.cross_chk.stateChanged.connect(self._toggle_crosshair)
        pl.addWidget(self.cross_chk)
        pl.addWidget(QtWidgets.QLabel("Y span (dB):"))
        self.yspan_combo = QtWidgets.QComboBox()
        self.yspan_combo.addItems(["Auto", "10", "20", "40", "60"])
        self.yspan_combo.setCurrentText("Auto")
        self.yspan_combo.activated.connect(self._change_yspan)
        pl.addWidget(self.yspan_combo)
        pl.addStretch(1)
        grid.addWidget(psd, 2, 0)
        return box

    # ------------------------------------------------- handlers
    def _change_rate(self, *args):
        try:
            mhz = float(self.rate_combo.currentText())
        except (ValueError, TypeError):
            return
        if mhz > RATE_MAX_MHZ:
            mhz = RATE_MAX_MHZ
            self.rate_combo.setCurrentText(f"{mhz:.2f}")
            msg = f"span capped to {mhz:.2f} MS/s for live GUI stability"
            print(msg)
            self.log_event(msg, "WARN")
        fs = max(RATE_MIN_MHZ, min(mhz, RATE_MAX_MHZ)) * 1e6
        d = {"sample_rate": fs}
        if self.replace_mode:           # keep the snapshot == window_ms at the new fs
            d["rows"] = self._rows_for_window(fs, self.nfft or DEFAULT_NFFT)
        self.receiver.send_control(d)
        self._refit_pending = True      # span changed -> refit the x-axis next frame

    def _change_speed(self, *args):
        label = self.speed_combo.currentText()
        fps = dict(SPEED_PRESETS).get(label, 0.0)
        self.min_interval = 0.0 if fps <= 0 else 1.0 / fps

    def _toggle_diff(self, state):
        # Diff view shows RX1-RX2 (directivity) on its own scale; hide the four
        # raw curves so autoRange fits the difference, not the -90..-10 spectra.
        self.show_diff = bool(state)
        for c in (self.psd_rx1_mean, self.psd_rx1_max,
                  self.psd_rx2_mean, self.psd_rx2_max):
            c.setVisible(not self.show_diff)
        self.psd_diff.setVisible(self.show_diff)
        self.psd_hold1.setVisible(self.peak_hold and not self.show_diff)
        self.psd_hold2.setVisible(self.peak_hold and not self.show_diff)
        self.psd_min1.setVisible(self.show_min and not self.show_diff)
        self.psd_min2.setVisible(self.show_min and not self.show_diff)
        if self.show_diff:                      # the marker tracks raw maxima
            self.peak_dot.clear()
            self.peak_text.setText("")
        self._update_psd()
        self._fit_psd()

    def _toggle_hold(self, state):
        self.peak_hold = bool(state)
        self.psd_hold1.setVisible(self.peak_hold and not self.show_diff)
        self.psd_hold2.setVisible(self.peak_hold and not self.show_diff)
        if not self.peak_hold:
            self.hold1 = self.hold2 = None
        self._update_psd()

    def _clear_hold(self):
        self.hold1 = self.hold2 = None
        self._update_psd()

    def _toggle_min(self, state):
        self.show_min = bool(state)
        self.psd_min1.setVisible(self.show_min and not self.show_diff)
        self.psd_min2.setVisible(self.show_min and not self.show_diff)
        self._update_psd()

    def _toggle_crosshair(self, state):
        self.crosshair = bool(state)
        for it in (self.vline, self.hline, self.cursor_text):
            it.setVisible(self.crosshair)

    def _change_yspan(self, *args):
        t = self.yspan_combo.currentText()
        if t == "Auto":
            self.psd_yspan = None
            self.psd_plot.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        else:
            try:
                self.psd_yspan = float(t)
            except ValueError:
                return
            self._apply_psd_yspan()

    def _apply_psd_yspan(self):
        # Lock the PSD y-axis to a fixed dB span, tracking the strongest signal
        # so it stays in view. Auto (None) leaves the normal auto-range alone.
        if self.psd_yspan is None or self.show_diff:
            return
        peak = None
        for ch in (0, 1):
            b = self.buffers.get(ch)
            if (b is not None and self._freqs is not None
                    and b.shape[1] == self._freqs.size):
                v = float(b.max())
                peak = v if peak is None else max(peak, v)
        if peak is not None:
            head = 0.1 * self.psd_yspan          # small headroom above the peak
            self.psd_plot.getViewBox().setYRange(
                peak - self.psd_yspan + head, peak + head, padding=0)

    def _on_mouse_moved(self, evt):
        if not self.crosshair:
            return
        pos = evt[0]
        vb = self.psd_plot.getPlotItem().vb
        if not self.psd_plot.sceneBoundingRect().contains(pos):
            return
        mp = vb.mapSceneToView(pos)
        self.vline.setPos(mp.x())
        self.hline.setPos(mp.y())
        self.cursor_text.setText(f"{mp.x():.3f} MHz, {mp.y():.1f} dB")
        self.cursor_text.setPos(mp.x(), mp.y())

    def _change_nfft(self):
        nfft = self.nfft_combo.currentData()
        d = {"nfft": nfft}
        if self.replace_mode:           # keep the snapshot == window_ms at the new nfft
            d["rows"] = self._rows_for_window(self.fs or DEFAULT_FS, nfft)
        self.receiver.send_control(d)

    def _tune_to_selection(self):
        lo, hi = self.meas_region.getRegion()
        sel = 0.5 * (lo + hi)
        if self.absolute:
            new_c = sel * 1e6
        else:
            base = self.center if self.center else self.center_spin.value() * 1e6
            new_c = base + sel * 1e6
        new_c = max(300e6, min(6e9, new_c))
        self.center_spin.setValue(new_c / 1e6)
        self.receiver.send_control({"center": new_c})

    def _toggle_pause(self, checked):
        self.paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")
        for plot in self.specplots.values():
            plot.setMouseEnabled(x=checked, y=checked)
        self.psd_plot.setMouseEnabled(x=True, y=True)

    def _toggle_mode(self, mode):
        # Boring Mode 🥱 == capture length == window: a full window of rows per
        # frame, so the image refreshes wholesale. Cool Mode 😎 == the old
        # scrolling view (SCROLL_ROWS rows/frame, buffer = window of memory).
        self.replace_mode = (mode == "Boring Mode 🥱")
        if self.replace_mode:
            rows = self._rows_for_window(self.fs or DEFAULT_FS, self.nfft or DEFAULT_NFFT)
        else:
            rows = SCROLL_ROWS
        self._clear_buffers()           # wipe residue; on_frame rebuilds to the new depth
        self.receiver.send_control({"rows": int(rows)})

    def _clear_buffers(self):
        # Reset both panes so a mode switch starts clean -- no half-scrolled rows
        # left behind.
        if self.nfft is None or not self.buffers:
            return
        for ch in self.buffers:
            self.buffers[ch][:] = -150.0
            self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                     levels=self.levels)

    def _toggle_absolute(self, state):
        self.absolute = bool(state)
        self._geom = None
        if self.fs is not None and self.buffers:
            self._apply_geometry()
            self._update_psd()      # push the new baseband/RF x-data to the curves
        # The x extent just jumped (RF center <-> baseband), so re-fit the view
        # AFTER the curves hold the new data; otherwise the PSD condenses into a
        # sliver where the data no longer is.
        for plot in self.specplots.values():
            plot.getViewBox().autoRange()
        self._fit_psd()

    def _change_window(self, *args):
        try:
            ms = int(round(float(self.win_combo.currentText())))
        except (ValueError, TypeError):
            return
        ms = max(5, min(ms, 60000))
        if ms == self.window_ms:
            return
        self.window_ms = ms
        self._refit_pending = True      # time span changed -> refit y next frame
        if self.replace_mode:
            # flicker: ask for a snapshot of the new length. The buffer resizes
            # to whatever actually returns on the next frame.
            self.receiver.send_control(
                {"rows": self._rows_for_window(self.fs or DEFAULT_FS,
                                               self.nfft or DEFAULT_NFFT)})
        elif self.fs and self.nfft and self.buffers:
            # waterfall: resize the memory buffer now, preserving the newest rows.
            depth = self._rows_for_window(self.fs, self.nfft)
            newb = {}
            for ch in self.buffers:
                old = self.buffers[ch]
                buf = np.full((depth, self.nfft), -150.0, np.float32)
                k = min(depth, old.shape[0])
                buf[:k] = old[:k]       # newest live at the top (row 0)
                newb[ch] = buf
            self.buffers = newb
            self._geom = None
            self._apply_geometry()

    def _reset_view(self):
        for plot in self.specplots.values():
            plot.getViewBox().enableAutoRange(True)
            plot.autoRange()
        self._fit_psd()

    def _fit_psd(self):
        # Fit the frequency (x) axis to the span; keep power (y) pinned at the
        # fixed -80..+20 dB range -- except in diff view, where the difference
        # has its own small scale and we let it auto-fit.
        vb = self.psd_plot.getViewBox()
        if self.show_diff:
            vb.enableAutoRange(x=True, y=True)
            return
        if self._freqs is not None and self._freqs.size:
            vb.setXRange(float(self._freqs.min()), float(self._freqs.max()),
                         padding=0.01)
        vb.setYRange(*PSD_YRANGE, padding=0)

    # ------------------------------------------------- geometry / frames
    def _freqs_mhz(self):
        base = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs))
        return ((self.center + base) if self.absolute else base) / 1e6

    def _edges_mhz(self):
        if self.absolute:
            return ((self.center - self.fs/2)/1e6, (self.center + self.fs/2)/1e6)
        return (-self.fs/2/1e6, self.fs/2/1e6)

    def _apply_geometry(self):
        if not self.buffers or self.fs is None or self.nfft is None:
            return
        depth = next(iter(self.buffers.values())).shape[0]
        geom = (self.center, self.fs, self.nfft, depth, self.absolute)
        if geom == self._geom:
            return
        self._geom = geom
        self._freqs = self._freqs_mhz()
        f0, f1 = self._edges_mhz()
        # Time axis in real milliseconds: each row is one FFT over nfft samples
        # (nfft/fs s); the pane holds `depth` rows -> spans depth*nfft/fs.
        t_ms = depth * self.nfft / self.fs * 1e3
        for ch in self.buffers:
            self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                     levels=self.levels)
            self.images[ch].setRect(QtCore.QRectF(f0, 0, f1 - f0, t_ms))
        # Keep the measurement band where the user put it, unless a retune /
        # mode flip pushed it off-screen -- then drop it on a narrow middle band.
        ml, mh = sorted(self.meas_region.getRegion())
        if mh <= f0 or ml >= f1 or (mh - ml) <= 0:
            self.meas_region.setRegion([f0 + 0.45 * (f1 - f0), f0 + 0.55 * (f1 - f0)])

    def on_frame(self, header):
        try:
            if self.paused:
                self._meta(header, live=False)
                return

            # "JEEZ SLOW DOWN": cap the visual update rate. We drop the frame's
            # render (and, in waterfall, its rows) when it arrives too soon. This
            # slows the display, not the radio -- flicker just refreshes less
            # often; waterfall scrolls slower but skips the dropped rows.
            now2 = time.time()
            if self.min_interval > 0.0 and (now2 - self._last_render) < self.min_interval:
                self._meta(header, live=True)
                return
            self._last_render = now2

            # fps counts rendered frames, so it tracks the actual display rate.
            self._frames += 1
            now = time.time()
            if now - self._t_fps >= 1.0:
                self._fps = self._frames / (now - self._t_fps)
                self._frames, self._t_fps = 0, now

            nfft = header["nfft"]
            center = header["center"]
            fs = header["fs"]
            n_rows = header["rows"]
            chans = header["channels"]

            # Target display depth. Boring Mode 🥱 shows exactly what arrived; waterfall
            # holds `window_ms` worth of memory regardless of the per-frame count.
            if self.replace_mode:
                depth = int(min(n_rows, MAX_ROWS))
            else:
                depth = self._rows_for_window(fs, nfft)

            cur_depth = (next(iter(self.buffers.values())).shape[0]
                         if self.buffers else -1)
            if (nfft != self.nfft or center != self.center or fs != self.fs
                    or not self.buffers or cur_depth != depth):
                self.nfft, self.center, self.fs = nfft, center, fs
                self.buffers = {ch: np.full((depth, nfft), -150.0, np.float32)
                                for ch in chans}
                self.hold1 = self.hold2 = None   # peak-hold is freq-specific
                self._geom = None
                self._apply_geometry()

            for ch, block in zip(chans, header["blocks"]):
                buf = self.buffers[ch]
                d = buf.shape[0]
                blk = block[::-1]                 # blocks are oldest-first; flip so
                                                  # row 0 == newest (top, with invertY)
                if self.replace_mode:
                    buf[:] = -150.0
                    k = min(blk.shape[0], d)
                    buf[:k, :] = blk[:k, :]
                else:
                    m = min(blk.shape[0], d)
                    buf = np.roll(buf, m, axis=0)  # push older rows down
                    buf[:m, :] = blk[:m, :]        # newest at the top
                    self.buffers[ch] = buf

            if self.auto_scale:
                samp = np.concatenate([b[::3, ::4].ravel() for b in self.buffers.values()])
                vmin, vmax = float(np.percentile(samp, 5)), float(np.percentile(samp, 99))
                if vmax - vmin < 5:
                    vmax = vmin + 5
                self.levels = (vmin, vmax)
            else:
                lv = self.images[0].getLevels()
                if lv is not None:
                    try:
                        self.levels = (float(lv[0]), float(lv[1]))
                    except (TypeError, IndexError):
                        pass

            for ch in self.buffers:
                self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                         levels=self.levels)
            self._update_psd()
            self._update_band()
            if self._refit_pending:     # span/window changed -> fit to new extent
                for plot in self.specplots.values():
                    plot.getViewBox().autoRange()
                self._fit_psd()
                self._refit_pending = False
            self._apply_psd_yspan()     # keep the PSD y-axis locked if requested
            self._meta(header, live=True)
        finally:
            self.receiver.gui_busy = False

    def _update_band(self):
        if self._freqs is None or not self.buffers:
            self.band_label.setText("Band monitor: --")
            return
        lo, hi = sorted(self.meas_region.getRegion())
        mask = (self._freqs >= lo) & (self._freqs <= hi)
        nb = int(mask.sum())
        if nb == 0:
            self.band_label.setText(
                f"Band {lo:.3f}–{hi:.3f} MHz: no bins in range")
            return
        band, qual = {}, {}
        for ch in (0, 1):
            b = self.buffers.get(ch)
            if b is None or b.shape[1] != self._freqs.size:
                continue
            lin = 10.0 ** (b.mean(axis=0) / 10.0)     # time-avg PSD, dB -> linear
            band[ch] = 10.0 * np.log10(lin[mask].mean())          # in-band level
            qual[ch] = band[ch] - 10.0 * np.log10(lin.mean())     # vs span avg (RSRQ-ish)
        seg = [f"Band {lo:.3f}–{hi:.3f} MHz ({nb} bins)"]
        if 0 in band:
            seg.append(f"RX1 {band[0]:.1f} dB")
        if 1 in band:
            seg.append(f"RX2 {band[1]:.1f} dB")
        if 0 in band and 1 in band:
            seg.append(f"Δ {band[0] - band[1]:+.1f} dB")     # directivity
        qseg = [f"{'RX1' if ch == 0 else 'RX2'} {qual[ch]:+.1f}"
                for ch in (0, 1) if ch in qual]
        if qseg:
            seg.append("Q " + " ".join(qseg) + " dB")
        self.band_label.setText("   |   ".join(seg))

    def _update_psd(self):
        if self._freqs is None:
            return
        b0 = self.buffers.get(0)
        b1 = self.buffers.get(1)
        ok0 = b0 is not None and b0.shape[1] == self._freqs.size
        ok1 = b1 is not None and b1.shape[1] == self._freqs.size
        m0 = b0.mean(axis=0) if ok0 else None
        x0 = b0.max(axis=0) if ok0 else None
        m1 = b1.mean(axis=0) if ok1 else None
        x1 = b1.max(axis=0) if ok1 else None

        if self.show_diff:
            if m0 is not None and m1 is not None:
                self.psd_diff.setData(self._freqs, m0 - m1)
            return                      # diff view hides everything else

        if ok0:
            self.psd_rx1_mean.setData(self._freqs, m0)
            self.psd_rx1_max.setData(self._freqs, x0)
        if ok1:
            self.psd_rx2_mean.setData(self._freqs, m1)
            self.psd_rx2_max.setData(self._freqs, x1)

        if self.show_min:
            if ok0:
                self.psd_min1.setData(self._freqs, b0.min(axis=0))
            if ok1:
                self.psd_min2.setData(self._freqs, b1.min(axis=0))

        if self.peak_hold:
            if x0 is not None:
                self.hold1 = x0 if self.hold1 is None or self.hold1.size != x0.size \
                    else np.maximum(self.hold1, x0)
                self.psd_hold1.setData(self._freqs, self.hold1)
            if x1 is not None:
                self.hold2 = x1 if self.hold2 is None or self.hold2.size != x1.size \
                    else np.maximum(self.hold2, x1)
                self.psd_hold2.setData(self._freqs, self.hold2)

        if self.peak_marker and x0 is not None:
            i = int(np.argmax(x0))
            fpk, ppk = float(self._freqs[i]), float(x0[i])
            self.peak_dot.setData([fpk], [ppk])
            self.peak_text.setText(f"{fpk:.3f} MHz\n{ppk:.1f} dB")
            self.peak_text.setPos(fpk, ppk)
        else:
            self.peak_dot.clear()
            self.peak_text.setText("")

    def _meta(self, h, live):
        depth = next(iter(self.buffers.values())).shape[0] if self.buffers else 0
        win_ms = depth * h["nfft"] / h["fs"] * 1e3 if depth and h["fs"] else 0.0
        mode = "flicker" if self.replace_mode else "waterfall"
        self.meta_label.setText(
            f"{'LIVE' if live else 'PAUSED'} | center {h['center']/1e6:.3f} MHz | "
            f"span {h['fs']/1e6:.2f} MS/s | gain {h.get('gain',0):.1f} dB | "
            f"FFT {h['nfft']} | {mode} | window {win_ms:.0f} ms ({depth} rows) | "
            f"scale {'auto' if self.auto_scale else 'manual'} "
            f"[{self.levels[0]:.0f},{self.levels[1]:.0f}] | "
            f"{'absolute RF' if self.absolute else 'baseband'} | {self._fps:.0f} fps"
            f"{' | ' + self._row_cap_message if self._row_cap_message else ''}")

    # ------------------------------------------------- exports
    def _save_csv(self):
        if self.nfft is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save PSD CSV", "live_psd.csv", "CSV (*.csv)")
        if not path:
            return
        freqs = self._freqs if self._freqs is not None else self._freqs_mhz()
        b0 = self.buffers.get(0)
        b1 = self.buffers.get(1)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["freq_mhz", "rx1_mean_db", "rx1_max_db",
                        "rx2_mean_db", "rx2_max_db"])
            m0, x0 = b0.mean(axis=0), b0.max(axis=0)
            if b1 is not None:
                m1, x1 = b1.mean(axis=0), b1.max(axis=0)
            else:
                m1 = x1 = np.full(freqs.shape, np.nan)
            for i, fr in enumerate(freqs):
                w.writerow([f"{fr:.6f}", f"{m0[i]:.3f}", f"{x0[i]:.3f}",
                            f"{m1[i]:.3f}", f"{x1[i]:.3f}"])
        self.status_label.setText(f"saved {path}")
        self.log_event(f"CSV saved: {path}")

    def _settings_caption(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        mode = "flicker" if self.replace_mode else "waterfall"
        c = self.center / 1e6 if self.center else self.center_spin.value()
        fs = self.fs / 1e6 if self.fs else 0.0
        depth = next(iter(self.buffers.values())).shape[0] if self.buffers else 0
        win_ms = (depth * self.nfft / self.fs * 1e3
                  if depth and self.fs and self.nfft else self.window_ms)
        return (f"{ts}   center {c:.3f} MHz   span {fs:.2f} MS/s   "
                f"gain {self.gain_spin.value():.1f} dB   FFT {self.nfft}   "
                f"{mode}   window {win_ms:.0f} ms   "
                f"{'absolute RF' if self.absolute else 'baseband'}   "
                f"color {'auto' if self.auto_scale else 'manual'} "
                f"[{self.levels[0]:.0f},{self.levels[1]:.0f}] dB")

    def _stamp_png(self, path, caption):
        img = QtGui.QImage(path)
        if img.isNull():
            return
        strip = 30
        out = QtGui.QImage(img.width(), img.height() + strip,
                           QtGui.QImage.Format_ARGB32)
        out.fill(QtGui.QColor("#101418"))
        p = QtGui.QPainter(out)
        p.drawImage(0, 0, img)
        p.setPen(QtGui.QColor("#d0d0d0"))
        font = QtGui.QFont("Menlo")
        font.setPointSize(10)
        p.setFont(font)
        p.drawText(QtCore.QRectF(10, img.height(), img.width() - 20, strip),
                   int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft),
                   caption)
        p.end()
        out.save(path)

    def _export_png(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export PNG", "live_view.png", "PNG (*.png)")
        if not path:
            return
        pg.exporters.ImageExporter(self.graphics.scene()).export(path)
        self._stamp_png(path, self._settings_caption())   # caption with settings
        self.status_label.setText(f"exported {path}")
        self.log_event(f"PNG exported: {path}")

    def closeEvent(self, event):
        self.log_event("app closing")
        self.receiver.stop()
        self.receiver.wait(2000)
        super().closeEvent(event)


def main():
    shared = SharedConfig()
    acquirer = Acquirer(shared)
    acquirer.start()

    # Give acquisition enough time to open/arm before the UI starts pulling.
    time.sleep(1.0)

    pg.setConfigOptions(antialias=False, imageAxisOrder="row-major",
                        background="#101418", foreground="#d0d0d0")
    app = QtWidgets.QApplication(sys.argv[:1])
    viewer = LiveViewer(acquirer, shared)
    viewer.showMaximized()
    try:
        app.exec_()
    finally:
        shared.stop()
        acquirer.join(timeout=3.0)


if __name__ == "__main__":
    main()

