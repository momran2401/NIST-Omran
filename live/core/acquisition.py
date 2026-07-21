"""Acquisition threads: hardware Acquirer, Computer, DemoAcquirer.

The Acquirer drains raw IQ into a ring buffer; the Computer turns ring
samples into published frames; the DemoAcquirer synthesizes IQ with no
hardware. Extracted verbatim from striqt_web_server.py.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from . import devices, state
from .config import RadioConfig, SharedConfig
from .constants import (
    DEVICE_PROFILES, MAX_TAIL, READ_SIZE, DATA_STALE_SEC, DEMO_TONES,
    DEFAULT_CENTER,
)
from .dsp import build_header, compute_blocks, samples_needed
from .operations import OPERATIONS, verdict_state
from .shims import (
    _close_rx_stream, close_source, enable_stream, get_stream_mtu, get_stream_ports,
    get_rx_stream, open_stream, query_device_envelope, stream_buffers_for,
)
from .striqt_compat import ReceiveStreamError, specs

def make_capture(cfg):
    # port stays fixed at state.CHANNELS — the two-waterfall UI depends on both RX ports
    # (P1-2). The other four knobs are now driven by the schema editor / cfg.
    # When cfg.duration owns the time axis (P2a-4) it drives the armed capture
    # duration honestly; snapped to an integer sample count because striqt's
    # Capture validation requires duration·sample_rate to be an integer.
    duration = cfg.duration if cfg.duration > 0 else cfg.rows * cfg.nfft / cfg.sample_rate
    duration = max(duration, 1e-3)
    duration = round(duration * cfg.sample_rate) / cfg.sample_rate
    return specs.SoapyCapture(
        port=state.CHANNELS,
        center_frequency=cfg.center,
        gain=tuple([cfg.gain] * len(state.CHANNELS)),
        duration=duration,
        sample_rate=cfg.sample_rate,
        backend_sample_rate=(cfg.backend_sample_rate or cfg.sample_rate),
        host_resample=cfg.host_resample,
        analysis_bandwidth=cfg.analysis_bandwidth,
        lo_shift=cfg.lo_shift,
    )
# ---------------------------------------------------------------------------
# Acquirer thread (real AIR8201B hardware)
# ---------------------------------------------------------------------------

class Acquirer(threading.Thread):
    """
    Drains raw IQ from the AIR8201B into a per-channel ring buffer in a tight
    read loop (no spectrogram math here). The separate Computer thread pulls the
    latest samples via get_latest(), computes blocks, and calls publish(); the
    broadcaster reads latest() at state.BROADCAST_FPS to fan out to all clients.

    Keeping compute off this loop is what prevents DMA overflow: while a frame is
    being computed, _read_stream keeps draining the radio. This mirrors the
    Acquirer/LocalReceiver split in striqt_standalone.py.
    """

    def __init__(self, shared: SharedConfig):
        super().__init__(daemon=True)
        self.shared       = shared
        self.source       = None
        self.stream_mtu   = None
        self.stream_ports = state.CHANNELS

        # Latest computed-frame slot (written by Computer, read by broadcaster).
        self._pub_lock        = threading.Lock()
        self._latest_header   = None
        self._latest_blocks   = None

        # Raw IQ ring buffer (complex64). One write pointer + sample count shared
        # across channels since every read fills all channels equally.
        self._lock        = threading.Lock()
        self._ring        = np.zeros((len(state.CHANNELS), MAX_TAIL), dtype=np.complex64)
        self._write       = 0      # next write index (mod MAX_TAIL)
        self._count       = 0      # total samples written (saturates at MAX_TAIL)
        self._last_write  = 0.0
        self._healthy     = False
        self._gen         = 0      # bumped on every ring clear (retune/recover) — LV-R5

        # Last source_config that demonstrably opened the device — the
        # revert target when a source-spec reconnect fails.
        self._last_good_source = {}
        # Verified-operations handoff: (op_id, ring_generation, verdict_state)
        # set by rearm/open_radio after readback; the Computer finishes the op
        # when the first frame of that generation is actually computed.
        self._verify_lock = threading.Lock()
        self._verify      = None
        self._pause_requested = threading.Event()
        self._paused = threading.Event()

    def pause_and_release(self, timeout=10.0):
        """Ask the acquisition loop to close the device, then wait for it."""
        self._pause_requested.set()
        return self._paused.wait(timeout)

    def resume(self):
        self._pause_requested.clear()

    def is_paused(self):
        return self._paused.is_set()

    # --- Latest-frame slot (thread-safe) ---

    def latest(self):
        """Return (header_dict, [block_array, ...]) of the most recent frame."""
        with self._pub_lock:
            if self._latest_header is None:
                return None, None
            return dict(self._latest_header), [b.copy() for b in self._latest_blocks]

    def publish(self, cfg: RadioConfig, blocks: list, meta: dict):
        header = build_header(cfg, blocks, meta, demo=False)
        with self._pub_lock:
            self._latest_header = header
            self._latest_blocks = [np.asarray(b, dtype=np.float32) for b in blocks]

    # --- Ring buffer (thread-safe; ported from striqt_standalone.py) ---

    def _clear_ring_locked(self):
        self._write      = 0
        self._count      = 0
        self._last_write = 0.0
        self._healthy    = False
        self._gen       += 1   # invalidate frames straddling this retune/recover (LV-R5)

    def _ring_write(self, iq):
        """Append raw IQ (channels, n) into the ring buffer with wraparound."""
        n = iq.shape[1]
        if n <= 0:
            return
        with self._lock:
            cap = MAX_TAIL
            if n >= cap:
                # Only the newest `cap` samples can survive.
                self._ring[:, :]  = iq[:, -cap:]
                self._write       = 0
                self._count       = cap
                self._last_write  = time.time()
                self._healthy     = True
                return
            end = self._write + n
            if end <= cap:
                self._ring[:, self._write:end] = iq
            else:
                first = cap - self._write
                self._ring[:, self._write:] = iq[:, :first]
                self._ring[:, : n - first]  = iq[:, first:]
            self._write      = end % cap
            self._count      = min(self._count + n, cap)
            self._last_write = time.time()
            self._healthy    = True

    def generation(self):
        with self._lock:
            return self._gen

    def get_latest(self, n):
        """
        Return (out, gen, avail): the most recent `n` complex samples per channel,
        shape (channels, n) complex64, chronological (oldest -> newest), front-padded
        with zeros if fewer than `n` exist; `gen` is the ring generation and `avail`
        the real sample count. Returns None if the ring is empty or stale (so frames
        never mix old-tuning samples after a retune).
        """
        n = int(n)
        if n <= 0:
            return None
        with self._lock:
            if (not self._healthy or self._count == 0
                    or time.time() - self._last_write > DATA_STALE_SEC):
                return None
            cap   = MAX_TAIL
            avail = min(self._count, cap)
            take  = min(n, avail)
            out   = np.zeros((len(state.CHANNELS), n), dtype=np.complex64)
            start = (self._write - take) % cap
            end   = start + take
            if end <= cap:
                out[:, n - take:] = self._ring[:, start:end]
            else:
                first = cap - start
                out[:, n - take:n - take + first] = self._ring[:, start:]
                out[:, n - take + first:]         = self._ring[:, : take - first]
            gen = self._gen
        return out, gen, avail

    # --- Verified operations (readback + data-path) ---

    def ring_status(self):
        """Radio/stream liveness for /health."""
        with self._lock:
            age = (time.time() - self._last_write) if self._last_write else None
            return {
                "open":             self.source is not None,
                "healthy":          bool(self._healthy),
                "last_write_age_s": round(age, 3) if age is not None else None,
                "ring_fill":        round(min(self._count, MAX_TAIL) / MAX_TAIL, 4),
            }

    # Which config fields make each hardware aspect worth judging. An op
    # that touched none of them (rows, backend, analysis…) is proven by
    # validation + the data-path frame alone — an unrelated missing driver
    # getter must not downgrade it to "unverified".
    _FREQ_FIELDS = frozenset({"center", "lo_shift"})
    _RATE_FIELDS = frozenset({"sample_rate", "backend_sample_rate",
                              "host_resample"})
    _GAIN_FIELDS = frozenset({"gain"})

    def _readback_and_verify(self, cfg: RadioConfig, op_id):
        """Ask the live driver what it ACTUALLY applied and judge it against
        the request (adapter tolerances), SCOPED to the aspects this
        operation changed (full recipe check when the field list is unknown,
        e.g. radio open/recovery or a source reconnect). Logs one readback
        stage per judged field and returns the op's collapsed verdict."""
        fields = OPERATIONS.fields(op_id)
        if fields is None or any(f.startswith("source.") for f in fields):
            check_freq = check_rate = check_gain = True     # full recipe
        else:
            fset = set(fields)
            check_freq = bool(fset & self._FREQ_FIELDS)
            check_rate = bool(fset & self._RATE_FIELDS)
            check_gain = bool(fset & self._GAIN_FIELDS)
        if not (check_freq or check_rate or check_gain):
            OPERATIONS.stage(op_id, "readback",
                             "not applicable — no hardware-facing field "
                             "changed (validated + frame-confirmed only)")
            return "success"
        adapter = devices.get_adapter()
        try:
            actuals  = adapter.read_back(self.source, cfg)
            # Expected values come from striqt's own resampler/LO design when
            # discoverable — an intentional lo_shift or backend_sample_rate
            # must not read as a mismatch.
            expected = adapter.hardware_expectations(
                self.source, make_capture(cfg), cfg)
            verdicts = adapter.verify(cfg, actuals, expected)
            verdicts = [v for v in verdicts
                        if (v["field"] == "center" and check_freq)
                        or (v["field"] == "sample_rate" and check_rate)
                        or (v["field"].startswith("gain") and check_gain)]
            if check_freq and abs(expected["center"] - float(cfg.center)) > 1.0:
                OPERATIONS.stage(
                    op_id, "readback",
                    f"note: hardware LO intentionally offset to "
                    f"{expected['center']/1e6:.6g} MHz (lo_shift={cfg.lo_shift})")
        except Exception as e:
            OPERATIONS.stage(op_id, "readback", f"query failed: {e}", level="warn")
            return "unverified"
        for v in verdicts:
            if v["state"] == "readback_unsupported":
                OPERATIONS.stage(op_id, "readback",
                                 f"{v['field']}: driver gave no answer",
                                 level="warn")
            else:
                mark = "OK" if v["state"] == "verified" else "MISMATCH"
                OPERATIONS.stage(
                    op_id, "readback",
                    f"{v['field']}: requested {v['requested']:.6g}, "
                    f"actual {v['actual']:.6g} — {mark}",
                    level=("info" if v["state"] == "verified" else "warn"),
                )
        return verdict_state(verdicts)

    def _arm_verification(self, op_id, vstate):
        """Hand the op to the Computer: it finishes when the first frame of
        the current ring generation is actually computed (data-path proof)."""
        # Recovery/resume rearms don't own an operation.  They must never
        # replace a real user operation that is still awaiting its fresh-frame
        # proof (the old behavior marked every such operation superseded).
        if op_id is None:
            return
        with self._lock:
            gen = self._gen
        with self._verify_lock:
            stale = self._verify
            self._verify = (op_id, gen, vstate)
        if stale is not None and stale[0] != op_id:
            OPERATIONS.finish(stale[0], "superseded",
                              "a newer apply replaced this operation before a "
                              "frame confirmed its data path")

    def complete_verification(self, gen):
        """Called by the compute side after each successfully published frame."""
        with self._verify_lock:
            if self._verify is None or self._verify[1] != gen:
                return
            op_id, _, vstate = self._verify
            self._verify = None
        OPERATIONS.stage(op_id, "data-path",
                         "fresh IQ received and first frame computed with the "
                         "new configuration")
        OPERATIONS.finish(op_id, vstate)

    # --- Hardware management ---

    def open_radio(self, cfg: RadioConfig, op_id=None):
        own_op = op_id is None
        if own_op:
            op_id = OPERATIONS.begin(
                "radio", f"open {state.DEVICE_LABEL} "
                         f"(center {cfg.center/1e6:.6g} MHz, "
                         f"{cfg.sample_rate/1e6:.6g} MS/s)")
        OPERATIONS.stage(op_id, "applying", "creating source + opening stream"
                         + (f" (source overrides: {sorted(cfg.source_config)})"
                            if cfg.source_config else ""))
        try:
            self.source = devices.make_source(cfg.source_config)
            open_stream(self.source)
            self.source.arm_spec(make_capture(cfg))
            enable_stream(self.source, True)
        except Exception:
            if own_op:
                OPERATIONS.finish(op_id, "failed", "source open/arm raised")
            raise
        OPERATIONS.stage(op_id, "applied", "arm_spec completed, stream enabled")
        self.stream_mtu   = get_stream_mtu(self.source)
        self.stream_ports = get_stream_ports(self.source)
        # Capability envelope (P3-3): profiles that opt in get their tier-1
        # clamp bounds from the live device. Failure is non-fatal — the
        # profile fallback stays in force. _recover() reopens through here,
        # so the envelope survives recovery cycles.
        if DEVICE_PROFILES[state.DEVICE].get("query_envelope"):
            try:
                self.shared.set_envelope(query_device_envelope(self.source))
            except Exception as e:
                print(f"[device] envelope query failed (profile fallback kept): {e}")
        print(
            f"[radio] armed: center {cfg.center/1e6:.2f} MHz, "
            f"{cfg.sample_rate/1e6:.3f} MS/s, channels {state.CHANNELS}, "
            f"backend={cfg.backend}"
        )
        vstate = self._readback_and_verify(cfg, op_id)
        self._arm_verification(op_id, vstate)

    def rearm(self, cfg: RadioConfig, op_id=None):
        if self.source is None:
            self.open_radio(cfg, op_id)
            return
        OPERATIONS.stage(
            op_id, "applying",
            f"rearm: center {cfg.center/1e6:.6g} MHz, "
            f"{cfg.sample_rate/1e6:.6g} MS/s, gain {cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}")
        # Apply the capture recipe to the existing stream.  striqt's Soapy
        # backend disables the stream, programs gain/frequency/rate, and then
        # this method re-enables it.  Closing and immediately recreating the
        # DMA stream here used to leave AIR-T's /dev/xdma0_c2h_0 handle busy;
        # every setting change then blocked for ~6.5 s and entered recovery.
        # The same in-place arm path is portable to Pluto/generic Soapy.
        enable_stream(self.source, False)
        rx = get_rx_stream(self.source)
        # Recording deliberately closes the live stream before handing the
        # source to the sweep runner.  If it is still closed on resume, reopen
        # exactly once; ordinary settings changes never enter this branch.
        if rx is not None and getattr(rx, "stream", None) is None:
            open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        # AIR-T's activation opens an exclusive XDMA channel.  A rapid
        # deactivate/reconfigure/activate can transiently return EBUSY while
        # the kernel releases the prior activation.  Retry activation on the
        # same stream (never rebuild the device); other radios get one attempt.
        attempts = 6 if state.DEVICE in ("air7101b", "air7201b", "air8201b") else 1
        activate_error = None
        for attempt in range(attempts):
            try:
                enable_stream(self.source, True)
                activate_error = None
                break
            except Exception as exc:
                activate_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.05 * (attempt + 1))
        if activate_error is not None:
            raise activate_error
        # Drop stale samples from the old tuning so they never mix into a frame.
        with self._lock:
            self._clear_ring_locked()
        OPERATIONS.stage(op_id, "applied",
                         "arm_spec completed, ring cleared of old-tuning IQ")
        print(
            f"[radio] retune: center {cfg.center/1e6:.2f} MHz, "
            f"{cfg.sample_rate/1e6:.3f} MS/s, gain {cfg.gain:.1f} dB, "
            f"nfft={cfg.nfft}, rows={cfg.rows}"
        )
        vstate = self._readback_and_verify(cfg, op_id)
        self._arm_verification(op_id, vstate)

    def _make_read_buffers(self):
        read_size     = min(self.stream_mtu or READ_SIZE, READ_SIZE)
        tmp           = np.empty((len(state.CHANNELS), read_size), dtype=np.complex64)
        buffers, _    = stream_buffers_for(self.source, tmp)
        return read_size, tmp, buffers

    def _recover(self, cfg: RadioConfig, reason: str):
        """Close and reopen the radio. Returns new (read_size, tmp, buffers)."""
        print(f"[radio] recovering after: {reason}")
        if state.DEVICE == "air8201b" and self.source is not None:
            # source.close() deinitializes the AD9371 management sensors for
            # this process. Recover AIR-T by replacing only its RX stream.
            with self._lock:
                self._clear_ring_locked()
            time.sleep(0.1)
            self.rearm(cfg)
            return self._make_read_buffers()
        if self.source is not None:
            close_source(self.source)
            self.source = None
        with self._lock:
            self._clear_ring_locked()
        time.sleep(0.25)
        self.open_radio(cfg)
        return self._make_read_buffers()

    # --- Main loop ---

    def run(self):
        cfg = self.shared.snapshot()
        try:
            self.open_radio(cfg)
            self._last_good_source = dict(cfg.source_config or {})
            last_good_cfg = cfg.snapshot()
            read_size, tmp, buffers = self._make_read_buffers()
            last_log = 0.0

            while not self.shared.stopped():
                if self._pause_requested.is_set():
                    if self.source is not None:
                        # Keep the process-lifetime AIR-T device initialized.
                        # source.close() deinitializes its AD9371 management
                        # sensors and the driver cannot rebuild them without a
                        # process restart. The recording runner takes ownership
                        # of this exact source object while live reads pause.
                        enable_stream(self.source, False)
                        _close_rx_stream(self.source)
                    with self._lock:
                        self._clear_ring_locked()
                    self._paused.set()
                    while (self._pause_requested.is_set()
                           and not self.shared.stopped()):
                        time.sleep(0.05)
                    self._paused.clear()
                    if self.shared.stopped():
                        break
                    cfg = self.shared.snapshot()
                    if self.source is not None:
                        try:
                            self.rearm(cfg, None)
                            read_size, tmp, buffers = self._make_read_buffers()
                            continue
                        except Exception as exc:
                            print(f"[radio] resume rearm failed: {exc}; reopening")
                            close_source(self.source)
                            self.source = None
                    # AIR-T management sensors can remain unavailable briefly
                    # after another process closes the device. A transient
                    # reopen error must not kill this long-lived thread and
                    # leave the web viewer permanently degraded.
                    while (not self._pause_requested.is_set()
                           and not self.shared.stopped()):
                        try:
                            self.open_radio(cfg)
                            read_size, tmp, buffers = self._make_read_buffers()
                            break
                        except Exception as exc:
                            if self.source is not None:
                                close_source(self.source)
                                self.source = None
                            print(f"[radio] resume open failed: {exc}; retry in 1s")
                            time.sleep(1.0)
                    continue
                dirty, new_cfg, op_id, reconnect, changed_fields = self.shared.take_dirty()
                if dirty:
                    cfg = new_cfg
                    try:
                        if reconnect:
                            # Source-spec overrides only take effect at open:
                            # verified reconnect (close → rebuild → reopen).
                            OPERATIONS.stage(
                                op_id, "applying",
                                "source settings changed — closing and "
                                "reopening the device")
                            if self.source is not None:
                                close_source(self.source)
                                self.source = None
                            with self._lock:
                                self._clear_ring_locked()
                            self.open_radio(cfg, op_id)
                        elif changed_fields & (
                            self._FREQ_FIELDS | self._RATE_FIELDS |
                            self._GAIN_FIELDS |
                            {"analysis_bandwidth", "host_resample",
                             "backend_sample_rate"}
                        ):
                            self.rearm(cfg, op_id)
                        else:
                            # FFT/rows/backend/analysis/LO-display changes only
                            # affect the compute path.  Invalidate any in-flight
                            # old-config frame, but leave the SDR stream alone.
                            OPERATIONS.stage(
                                op_id, "applying",
                                "compute/display settings changed — radio stream kept open")
                            with self._lock:
                                self._clear_ring_locked()
                            OPERATIONS.stage(
                                op_id, "readback",
                                "not applicable — no hardware-facing field changed")
                            self._arm_verification(op_id, "success")
                        if reconnect or changed_fields & (
                            self._FREQ_FIELDS | self._RATE_FIELDS |
                            self._GAIN_FIELDS |
                            {"analysis_bandwidth", "host_resample",
                             "backend_sample_rate"}
                        ):
                            read_size, tmp, buffers = self._make_read_buffers()
                        self._last_good_source = dict(cfg.source_config or {})
                        last_good_cfg = cfg.snapshot()
                    except Exception as e:
                        OPERATIONS.finish(op_id, "failed",
                                          f"hardware apply raised: {e}")
                        if reconnect:
                            # A bad source override would loop recovery forever
                            # — revert to the last set that actually opened.
                            cfg = self.shared.restore_source(
                                self._last_good_source, reason=str(e))
                        else:
                            # A rejected arm must not destroy AIR-T's
                            # process-lifetime device singleton. Restore the
                            # last recipe on the same initialized source.
                            cfg = self.shared.restore_config(
                                last_good_cfg, reason=str(e))
                            try:
                                self.rearm(cfg, None)
                                read_size, tmp, buffers = self._make_read_buffers()
                                continue
                            except Exception as rollback_error:
                                print(f"[radio] rollback rearm failed: "
                                      f"{rollback_error}; full recovery needed")
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
                except (ReceiveStreamError, OverflowError, OSError, RuntimeError) as e:
                    try:
                        read_size, tmp, buffers = self._recover(cfg, str(e))
                    except Exception as re:
                        print(f"[radio] recovery failed: {re}; retry in 1s")
                        time.sleep(1.0)
                    continue

                if got <= 0:
                    time.sleep(0.001)
                    continue

                # Drain-only: push raw IQ into the ring and loop back to read
                # again immediately. The Computer thread does the spectrogram.
                iq = tmp[:, :got].copy()
                self._ring_write(iq)

                now = time.time()
                if now - last_log > 5.0:
                    print(
                        f"[radio] IQ {iq.shape} {iq.dtype}  "
                        f"ring {min(self._count, MAX_TAIL)}/{MAX_TAIL}  "
                        f"backend={cfg.backend}"
                    )
                    last_log = now

        finally:
            if self.source is not None:
                close_source(self.source)


# ---------------------------------------------------------------------------
# Compute thread (spectrogram worker, decoupled from the DMA drain)
# ---------------------------------------------------------------------------

class Computer(threading.Thread):
    """
    Pulls the latest raw IQ from the Acquirer's ring buffer, computes the
    spectrogram, and publishes the frame — all off the DMA drain loop so the
    radio keeps draining while a frame is being computed. Paced to ~state.BROADCAST_FPS
    so it doesn't compute frames the broadcaster would only drop.
    """

    def __init__(self, acquirer: "Acquirer", shared: SharedConfig):
        super().__init__(daemon=True)
        self.acquirer = acquirer
        self.shared   = shared
        self._last_err_notice = 0.0

    def run(self):
        interval = 1.0 / max(state.BROADCAST_FPS, 1.0)
        next_t   = time.time()
        while not self.shared.stopped():
            # Serve any pending tier-2 validation probe first: this thread owns
            # striqt's thread-bound persistent window cache (P2a-5).
            self.shared.service_probe()
            cfg     = self.shared.snapshot()
            need    = samples_needed(cfg)
            g0      = self.acquirer.generation()
            latest  = self.acquirer.get_latest(need)
            if latest is None:
                # Ring empty/stale (startup or just after a retune) — wait.
                time.sleep(0.03)
                next_t = time.time()
                continue
            samples, gen, avail = latest
            # Skip frames straddling a retune: the ring was cleared (gen bumped) or
            # hasn't refilled yet (avail < need). Either would publish zero-padded
            # dark rows or mislabel old-band energy with the new header (LV-R5).
            if gen != g0 or avail < need:
                time.sleep(0.03)
                next_t = time.time()
                continue

            try:
                blocks, meta = compute_blocks(samples, cfg)
                self.acquirer.publish(cfg, [blocks[i] for i in range(blocks.shape[0])], meta)
                self.shared.note_good_analysis(cfg)
                # Data-path proof for the pending verified operation (if any):
                # a frame of this ring generation actually computed.
                self.acquirer.complete_verification(gen)
            except Exception as e:
                # Backstop (P2a-3): even if a bad analysis param somehow reached
                # the live compute, catch it, revert to the last-good analysis
                # config, keep streaming, and surface the reason — the viewer
                # must never freeze.
                print(f"[compute] error: {e}")
                reverted = self.shared.revert_analysis(str(e))
                if reverted:
                    print(f"[compute] reverted analysis params: {reverted}")
                elif time.time() - self._last_err_notice > 5.0:
                    # Not analysis-induced (nothing to revert) — tell the viewer
                    # anyway, throttled so a persistent fault can't spam.
                    self.shared.push_notice(f"compute error: {e}")
                    self._last_err_notice = time.time()
                time.sleep(0.1)

            # Pace to the broadcast rate; never busy-spin if compute outran it.
            next_t += interval
            dt = next_t - time.time()
            if dt > 0:
                time.sleep(dt)
            else:
                next_t = time.time()


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
        self._pause_requested = threading.Event()
        self._paused          = threading.Event()

    def pause_and_release(self, timeout=10.0):
        self._pause_requested.set()
        return self._paused.wait(timeout)

    def resume(self):
        self._pause_requested.clear()

    def is_paused(self):
        return self._paused.is_set()

    def latest(self):
        with self._lock:
            if self._latest_header is None:
                return None, None
            return dict(self._latest_header), [b.copy() for b in self._latest_blocks]

    def _publish(self, cfg: RadioConfig, blocks: list, meta: dict):
        header = build_header(cfg, blocks, meta, demo=True)
        with self._lock:
            self._latest_header = header
            self._latest_blocks = [np.asarray(b, dtype=np.float32) for b in blocks]

    def run(self):
        rng = np.random.default_rng(42)
        last_err_notice = 0.0
        pending_op = None
        print("[demo] Synthetic IQ mode — no radio hardware used.")
        print("[demo] Two CW tones per channel + noise. Controls work normally.")
        print("[demo] Tones are fixed STATIONS near the default center — "
              "retuning moves them across the band like a real signal.")

        interval = 1.0 / max(state.BROADCAST_FPS, 1.0)
        next_t = time.time()
        while not self.shared.stopped():
            if self._pause_requested.is_set():
                self._paused.set()
                while (self._pause_requested.is_set()
                       and not self.shared.stopped()):
                    time.sleep(0.05)
                self._paused.clear()
                next_t = time.time()
                continue
            # This is the compute thread in demo mode — serve tier-2 probes here
            # for the same thread-bound-cache reason as the Computer (P2a-5).
            self.shared.service_probe()
            dirty, cfg, op_id, _reconnect, _changed = self.shared.take_dirty()
            if dirty and op_id is not None:
                if pending_op is not None:
                    OPERATIONS.finish(pending_op, "superseded",
                                      f"replaced by op #{op_id}")
                OPERATIONS.stage(op_id, "applying",
                                 "demo device — synthetic source retunes "
                                 "instantly (no hardware)")
                OPERATIONS.stage(op_id, "readback",
                                 "demo device has no driver to query")
                pending_op = op_id
            n   = samples_needed(cfg)
            t   = np.arange(n, dtype=np.float32) / cfg.sample_rate

            # One tone set + noise per channel (P3-2). Tones model fixed
            # STATIONS at absolute RF (DEFAULT_CENTER + offset), so demo
            # retunes behave like real hardware: the tones shift across the
            # band and leave it entirely when tuned far away — the tuning
            # data-path is therefore actually exercised in demo mode.
            detune = float(DEFAULT_CENTER) - float(cfg.center)
            chans = []
            for i in range(len(state.CHANNELS)):
                tones = DEMO_TONES[i % len(DEMO_TONES)]
                sig = np.zeros(n, dtype=np.complex64)
                for amp, offset_hz in tones:
                    off = offset_hz + detune
                    if abs(off) <= 0.48 * cfg.sample_rate:
                        sig += (amp * np.exp(2j * np.pi * off * t)
                                ).astype(np.complex64)
                noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)
                         ).astype(np.complex64) * 0.04
                chans.append(sig + noise)

            samples = np.stack(chans)
            try:
                blocks, meta = compute_blocks(samples, cfg)
                self._publish(cfg, [blocks[i] for i in range(blocks.shape[0])], meta)
                self.shared.note_good_analysis(cfg)
                if pending_op is not None:
                    OPERATIONS.stage(pending_op, "data-path",
                                     "first frame computed with the new "
                                     "configuration")
                    OPERATIONS.finish(pending_op, "success",
                                      "demo apply confirmed by frame "
                                      "(no hardware readback in demo)")
                    pending_op = None
            except Exception as e:
                # Same backstop as the hardware Computer (P2a-3): revert to the
                # last-good analysis config and keep the demo stream alive.
                print(f"[demo] compute error: {e}")
                reverted = self.shared.revert_analysis(str(e))
                if reverted:
                    print(f"[demo] reverted analysis params: {reverted}")
                elif time.time() - last_err_notice > 5.0:
                    self.shared.push_notice(f"compute error: {e}")
                    last_err_notice = time.time()

            next_t += interval
            dt = next_t - time.time()
            if dt > 0:
                time.sleep(dt)
            else:
                next_t = time.time()
