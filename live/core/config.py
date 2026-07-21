"""RadioConfig + SharedConfig: the thread-safe control brain.

Extracted verbatim from striqt_web_server.py; device-dependent globals are
read through core.state at call time so main() can configure them once at
startup.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from fractions import Fraction

from . import state
from .constants import (
    DEVICE_PROFILES, DEFAULT_CENTER, DEFAULT_SAMPLE_RATE, DEFAULT_GAIN,
    DEFAULT_NFFT, DEFAULT_ROWS, DEFAULT_WINDOW, DEFAULT_FRACTIONAL_OVERLAP,
    DEFAULT_WINDOW_FILL, DEFAULT_INTEGRATION_BW, DEFAULT_LO_BANDSTOP,
    DEFAULT_TRIM_STOPBAND, DEFAULT_PSD_TIME_STATISTIC,
    SSB_SUBCARRIER_SPACING, SSB_SAMPLE_RATE, SSB_DISCOVERY_PERIOD,
    SSB_WINDOW, SSB_LO_BANDSTOP, SSB_MAX_RATE, MAX_TAIL, RING_ROW_FILL,
    NFFT_CHOICES, BACKENDS, CALIBRATED_GRID_BACKENDS,
)
from .striqt_compat import _ANALYSIS_OK
from .dsp import (
    _snap, allowed_rates, aligned_nfft, analysis_hop, max_live_rows, row_hop,
    ssb_grid_compatible, ssb_compatible_rate,
)
from .parsing import (
    ANALYSIS_TARGETS, ANALYSIS_CFG_KEYS, ANALYSIS_DEFAULTS,
    _parse_window, _parse_fraction, _parse_optional_hz,
    _parse_time_statistic, _parse_optional_seconds,
    scratch_validate_analysis,
)
from .operations import OPERATIONS, fmt_value

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
    backend:     str   = state.SPEC_BACKEND
    lo_null:     bool  = True
    # Displayed time span in seconds (P2a-4). When > 0, duration OWNS rows: they
    # are re-derived hop-aware (duration·fs / row_hop) on every change to nfft/
    # backend/overlap/sample_rate. 0 = legacy rows-driven mode (an explicit
    # top-level {"rows": N} control reclaims ownership by zeroing this).
    duration:    float = 0.0
    # Capture knobs surfaced by the schema editor (P1-2). Defaults reproduce the
    # values make_capture used to hardcode, so behaviour is unchanged until the
    # user edits them. backend_sample_rate == 0 means "track sample_rate".
    analysis_bandwidth:  float = float("inf")
    lo_shift:            str   = "none"
    host_resample:       bool  = False
    backend_sample_rate: float = 0.0
    # Applied source-spec overrides (verified-reconnect path): keys merged
    # over the adapter's default source spec at open. Only reachable through
    # SharedConfig.update's source block; None/empty = pure defaults.
    source_config:       object = None
    # striqt Spectrogram analysis params (P2a-1) — drive the calibrated backend's
    # spec instead of the old hardcodes. All immutable values (str/tuple/Fraction/
    # None), so snapshot() can pass them through. Only validated values may land
    # here (the freedom model in SharedConfig.update guards every write).
    #   window:                scipy get_window spec — a name or (name, param)
    #   fractional_overlap:    Fraction of each FFT window shared with its neighbor
    #   window_fill:           Fraction of the window filled by the taper (rest zeros)
    #   integration_bandwidth: "auto" (freq_res × averaging_factor), None, or Hz
    #   lo_bandstop:           None or Hz nulled at DC by striqt
    #   trim_stopband:         trim the frequency axis to analysis_bandwidth
    window:                object = DEFAULT_WINDOW
    fractional_overlap:    Fraction = DEFAULT_FRACTIONAL_OVERLAP
    window_fill:           Fraction = DEFAULT_WINDOW_FILL
    integration_bandwidth: object = DEFAULT_INTEGRATION_BW
    lo_bandstop:           object = DEFAULT_LO_BANDSTOP
    trim_stopband:         bool   = DEFAULT_TRIM_STOPBAND
    #   time_aperture:         None, or seconds of binned RMS averaging along the
    #                          time axis (striqt requires a multiple of hop/fs)
    time_aperture:         object = None
    # striqt PowerSpectralDensity analysis params (P2b-3) — an independent block
    # so tuning the PSD view never disturbs the spectrogram recipe. Same field
    # semantics as the spectrogram block, plus:
    #   psd_time_statistic:  tuple of named statistics/quantiles evaluated along
    #                        the time axis — one PSD trace per entry
    psd_window:                object = DEFAULT_WINDOW
    psd_fractional_overlap:    Fraction = DEFAULT_FRACTIONAL_OVERLAP
    psd_window_fill:           Fraction = DEFAULT_WINDOW_FILL
    psd_integration_bandwidth: object = DEFAULT_INTEGRATION_BW
    psd_lo_bandstop:           object = DEFAULT_LO_BANDSTOP
    psd_trim_stopband:         bool   = DEFAULT_TRIM_STOPBAND
    psd_time_statistic:        tuple  = DEFAULT_PSD_TIME_STATISTIC
    # striqt Cellular5GNRSSBSpectrogram analysis params (P2b-5) — defaults are
    # the exact values the SSB path hardcoded before, so behaviour is unchanged
    # until edited from the Analysis panel.
    #   ssb_subcarrier_spacing:    3GPP SCS in Hz (15e3/30e3/60e3 …)
    #   ssb_sample_rate:           output rate of the recentered SSB band (S/s)
    #   ssb_discovery_periodicity: time between synchronization bursts (s)
    #   ssb_frequency_offset:      SSB center offset from the capture center (Hz)
    #   ssb_max_block_count:       None, or a cap on bursts evaluated per frame
    ssb_subcarrier_spacing:    float  = SSB_SUBCARRIER_SPACING
    ssb_sample_rate:           float  = SSB_SAMPLE_RATE
    ssb_discovery_periodicity: float  = SSB_DISCOVERY_PERIOD
    ssb_frequency_offset:      float  = 0.0
    ssb_max_block_count:       object = None
    ssb_window:                object = SSB_WINDOW
    ssb_lo_bandstop:           object = SSB_LO_BANDSTOP

    def snapshot(self):
        return RadioConfig(
            center=float(self.center),
            sample_rate=float(self.sample_rate),
            gain=float(self.gain),
            nfft=int(self.nfft),
            rows=int(self.rows),
            backend=str(self.backend),
            lo_null=bool(self.lo_null),
            duration=float(self.duration),
            analysis_bandwidth=float(self.analysis_bandwidth),
            lo_shift=str(self.lo_shift),
            host_resample=bool(self.host_resample),
            backend_sample_rate=float(self.backend_sample_rate),
            source_config=dict(self.source_config or {}),
            window=self.window,
            fractional_overlap=self.fractional_overlap,
            window_fill=self.window_fill,
            integration_bandwidth=self.integration_bandwidth,
            lo_bandstop=self.lo_bandstop,
            trim_stopband=bool(self.trim_stopband),
            time_aperture=self.time_aperture,
            psd_window=self.psd_window,
            psd_fractional_overlap=self.psd_fractional_overlap,
            psd_window_fill=self.psd_window_fill,
            psd_integration_bandwidth=self.psd_integration_bandwidth,
            psd_lo_bandstop=self.psd_lo_bandstop,
            psd_trim_stopband=bool(self.psd_trim_stopband),
            psd_time_statistic=tuple(self.psd_time_statistic),
            ssb_subcarrier_spacing=float(self.ssb_subcarrier_spacing),
            ssb_sample_rate=float(self.ssb_sample_rate),
            ssb_discovery_periodicity=float(self.ssb_discovery_periodicity),
            ssb_frequency_offset=float(self.ssb_frequency_offset),
            ssb_max_block_count=self.ssb_max_block_count,
            ssb_window=self.ssb_window,
            ssb_lo_bandstop=self.ssb_lo_bandstop,
        )


class SharedConfig:
    def __init__(self):
        self._lock  = threading.Lock()
        # Seed the radio knobs from the active device profile (P3-1). For
        # air8201b/demo the profile defaults equal the DEFAULT_* constants,
        # so behaviour is unchanged there.
        _prof = DEVICE_PROFILES[state.DEVICE]["defaults"]
        self._cfg   = RadioConfig(
            backend=state.SPEC_BACKEND,
            center=_prof["center"],
            sample_rate=_prof["sample_rate"],
            gain=_prof["gain"],
        )
        # Capability envelope (P3-3): tier-1 clamp bounds. Starts as the
        # profile fallback; when the profile opts in (query_envelope) the
        # Acquirer merges the live device's queried ranges over it after open.
        self._envelope = dict(DEVICE_PROFILES[state.DEVICE]["envelope"])
        self._dirty = False
        self._stop  = False
        # P2a-3 backstop state: the analysis params of the last config that
        # demonstrably computed a frame, and notices queued for the viewers.
        self._last_good_analysis = None
        self._notices = []
        # Tier-2 probe handoff (P2a-5): striqt's persistent window cache is a
        # process-wide shelf that (on dbm.sqlite3 Pythons) is bound to the
        # thread that first used it — the compute thread. Scratch validations
        # therefore run THERE, posted through this single-slot mailbox.
        self._probe_lock = threading.Lock()   # serializes probers
        self._probe_req  = None               # (seq, RadioConfig) or None
        self._probe_res  = None               # (seq, verdict)
        self._probe_seq  = 0
        self._probe_done = threading.Event()
        # Verified-operations pipeline: the op id awaiting hardware apply +
        # readback (consumed by take_dirty → Acquirer.rearm).
        self._pending_op = None
        # Source-spec changes can only apply by closing + reopening the
        # device; take_dirty hands this flag to the Acquirer.
        self._reconnect = False
        # Exact fields awaiting application.  The hardware thread uses this to
        # distinguish radio-facing changes from compute/display-only changes;
        # rebuilding an SDR DMA stream for rows, FFT, or PSD toggles is both
        # unnecessary and unsafe on drivers that retain an exclusive handle.
        self._changed_fields = set()

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

    # --- Capability envelope (P3-3) -------------------------------------------

    def set_envelope(self, env: dict):
        """Merge queried device bounds over the profile fallback. Partial
        dicts are fine — unanswered keys keep their fallback values."""
        clean = {}
        for key, value in (env or {}).items():
            if key not in self._envelope or value is None:
                continue
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        with self._lock:
            self._envelope.update(clean)
        print(f"[device] capability envelope updated: {clean}")

    def envelope(self):
        with self._lock:
            return dict(self._envelope)

    # --- Compute backstop (P2a-3) ---------------------------------------------

    def note_good_analysis(self, cfg: "RadioConfig"):
        """Remember the analysis params that just computed a frame successfully —
        the revert target if a later config slips past validation and throws."""
        good = {k: getattr(cfg, k) for k in ANALYSIS_CFG_KEYS}
        with self._lock:
            self._last_good_analysis = good

    def revert_analysis(self, reason: str):
        """
        Backstop (belt and suspenders): the compute path caught an exception even
        though tiers 1–2 should have prevented it. Revert the analysis params to
        the last-good set (or the shipped defaults), keep streaming, and queue a
        notice for the viewers. Returns the sorted reverted field names, or None
        when the current params already match every revert target — i.e. the
        error is not analysis-induced and reverting would change nothing.
        """
        with self._lock:
            current = {k: getattr(self._cfg, k) for k in ANALYSIS_CFG_KEYS}
            target = None
            for candidate in (self._last_good_analysis, ANALYSIS_DEFAULTS):
                if candidate and any(candidate[k] != current[k] for k in ANALYSIS_CFG_KEYS):
                    target = candidate
                    break
            if target is None:
                return None
            changed = []
            for key in ANALYSIS_CFG_KEYS:
                if current[key] != target[key]:
                    setattr(self._cfg, key, target[key])
                    changed.append(key)
            # The reverted overlap may have widened the per-row hop — re-clamp
            # rows so the Computer's avail >= need gate stays reachable.
            max_rows = max_live_rows(self._cfg)
            if self._cfg.rows > max_rows:
                self._cfg.rows = max_rows
            changed = sorted(changed)
        self.push_notice(
            f"analysis error: {reason} — reverted {', '.join(changed)} to last-good values"
        )
        return changed

    def probe_analysis(self, trial_cfg: "RadioConfig", target: str = "spectrogram"):
        """
        Tier-2 scratch validation, executed on the compute thread. striqt's
        get_window carries a persistent on-disk cache whose handle is bound to
        the thread that first used it (dbm.sqlite3 refuses cross-thread use);
        the compute thread is that owner, so verdicts from anywhere else could
        report a spurious threading error instead of the real one. Falls back
        to an inline judgement if no compute thread services the request in
        time (startup) — the tier-3 backstop still protects the stream.
        """
        if not _ANALYSIS_OK:
            return None
        with self._probe_lock:
            self._probe_seq += 1
            seq = self._probe_seq
            self._probe_done.clear()
            self._probe_req = (seq, trial_cfg, target)
            if self._probe_done.wait(2.0):
                res = self._probe_res
                if res and res[0] == seq:
                    return res[1]
            self._probe_req = None
            return scratch_validate_analysis(trial_cfg, target)

    def service_probe(self):
        """Called by the compute thread every loop: run a pending tier-2 probe."""
        job = self._probe_req
        if job is None:
            return
        self._probe_req = None
        seq, trial_cfg, target = job
        self._probe_res = (seq, scratch_validate_analysis(trial_cfg, target))
        self._probe_done.set()

    def push_notice(self, message: str):
        with self._lock:
            self._notices.append(str(message))
            del self._notices[:-20]   # keep only the newest if no viewer drains

    def drain_notices(self):
        with self._lock:
            notices, self._notices = self._notices, []
            return notices

    def _effective_radio(self, update: dict):
        """
        Effective radio params for THIS message (LV-R9b): validation must see
        the nfft/sample_rate/backend the message itself is applying (already
        mapped to the top level by the capture branch), not the stale cfg.
        """
        eff = self.snapshot()
        env = self.envelope()
        try:
            if update.get("sample_rate") is not None:
                eff.sample_rate = float(
                    max(env["rate_min"],
                        min(_snap(float(update["sample_rate"]), allowed_rates(env)),
                            env["rate_max"]))
                )
            if update.get("nfft") is not None:
                eff.nfft = int(_snap(int(update["nfft"]), NFFT_CHOICES))
            if update.get("backend") is not None:
                backend = str(update["backend"]).strip().lower()
                if backend in BACKENDS:
                    eff.backend = backend
        except (TypeError, ValueError):
            pass
        return eff

    def _tier1_freq_fields(self, req, eff, *, cfg_prefix, ack_prefix,
                           on_calibrated_grid, rounded, rejected):
        """
        Tier-1 snap rules (knowable constraints → round and tell) for the
        FrequencyAnalysisSpecBase fields shared by the spectrogram and PSD
        analyses: window / frequency_resolution / fractional_overlap /
        window_fill / integration_bandwidth / lo_bandstop / trim_stopband.
        `cfg_prefix` maps the message field onto the target's RadioConfig
        attribute (e.g. "psd_" + "window"); `ack_prefix` labels the ack entries.
        Returns (accepted, ack_field, requested_map) keyed by RadioConfig key.
        """
        accepted = {}          # cfg key -> validated value
        ack_field = {}         # cfg key -> field name reported in the ack
        requested_map = {}     # cfg key -> the raw requested value

        def tell(field, requested, used, reason):
            rounded.append({
                "field": ack_prefix + field, "requested": requested,
                "used": used, "reason": reason,
            })

        def reject(field, requested, reason):
            rejected.append({"field": ack_prefix + field,
                             "requested": requested, "reason": reason})

        # --- frequency_resolution: the second view of nfft (tier 1) ----------
        # cfg.nfft owns this quantity (P2a-1); an edit here snaps to the nearest
        # FFT size and the executed resolution is reported back.
        if req.get("frequency_resolution") is not None:
            requested = req["frequency_resolution"]
            try:
                fr = float(requested)
                if not (fr > 0 and math.isfinite(fr)):
                    raise ValueError("frequency_resolution must be a positive, finite Hz value")
                nfft_snap = int(_snap(eff.sample_rate / fr, NFFT_CHOICES))
                eff.nfft = nfft_snap
                accepted["nfft"] = nfft_snap
                ack_field["nfft"] = ack_prefix + "frequency_resolution"
                requested_map["nfft"] = requested
                executed_nfft = aligned_nfft(nfft_snap) if on_calibrated_grid else nfft_snap
                used = eff.sample_rate / executed_nfft
                if abs(used - fr) > 1e-6 * max(fr, 1.0):
                    reason = f"FFT size owns this quantity; snapped to nfft {nfft_snap}"
                    if executed_nfft != nfft_snap:
                        reason += (f" (calibrated grid runs {executed_nfft}, "
                                   f"a 28-multiple, for window_fill integrality)")
                    tell("frequency_resolution", fr, used, reason)
            except (TypeError, ValueError) as e:
                reject("frequency_resolution", requested, str(e))

        # Denominator grid for fraction snapping: the FFT size striqt executes.
        nfft_axis = aligned_nfft(eff.nfft) if on_calibrated_grid else int(eff.nfft)
        freq_res  = eff.sample_rate / nfft_axis

        # --- fractional_overlap / window_fill: snap to k/nfft (tier 1) -------
        for key, lo_k, hi_k, why in (
            ("fractional_overlap", 0, nfft_axis - 1,
             "overlap must be an integer sample count (k/nfft) below 1"),
            ("window_fill", 1, nfft_axis,
             "(1 - window_fill) x nfft must be an integer zero-fill (k/nfft)"),
        ):
            if req.get(key) is None:
                continue
            requested = req[key]
            try:
                frac = _parse_fraction(requested)
                k = min(max(round(frac * nfft_axis), lo_k), hi_k)
                snapped = Fraction(k, nfft_axis)
                accepted[cfg_prefix + key] = snapped
                ack_field[cfg_prefix + key] = ack_prefix + key
                requested_map[cfg_prefix + key] = requested
                if snapped != frac:
                    tell(key, str(requested), str(snapped), why)
            except ValueError as e:
                reject(key, requested, str(e))

        # --- integration_bandwidth: multiple of freq_res, "auto", or none ----
        if "integration_bandwidth" in req and req["integration_bandwidth"] is not None:
            requested = req["integration_bandwidth"]
            try:
                v = _parse_optional_hz(requested, auto_ok=True)
                if v is None or isinstance(v, str):
                    accepted[cfg_prefix + "integration_bandwidth"] = v
                else:
                    if v < 0:
                        raise ValueError("integration_bandwidth must be positive, 'auto', or 'none'")
                    factor = min(max(1, round(v / freq_res)), nfft_axis)
                    used = factor * freq_res
                    accepted[cfg_prefix + "integration_bandwidth"] = used
                    if abs(used - v) > 1e-6 * max(v, 1.0):
                        tell("integration_bandwidth", v, used,
                             f"must be an integer multiple of the {freq_res:.1f} Hz "
                             f"frequency resolution (striqt); using {factor} bins")
                ack_field[cfg_prefix + "integration_bandwidth"] = ack_prefix + "integration_bandwidth"
                requested_map[cfg_prefix + "integration_bandwidth"] = requested
            except ValueError as e:
                reject("integration_bandwidth", requested, str(e))

        # --- lo_bandstop: positive Hz within the sampled span, or none --------
        if "lo_bandstop" in req and req["lo_bandstop"] is not None:
            requested = req["lo_bandstop"]
            try:
                v = _parse_optional_hz(requested)
                if v is not None:
                    if v < 0:
                        raise ValueError("lo_bandstop must be positive or 'none'")
                    if v > eff.sample_rate:
                        tell("lo_bandstop", v, eff.sample_rate,
                             "cannot exceed the sampled span (sample_rate)")
                        v = float(eff.sample_rate)
                accepted[cfg_prefix + "lo_bandstop"] = v
                ack_field[cfg_prefix + "lo_bandstop"] = ack_prefix + "lo_bandstop"
                requested_map[cfg_prefix + "lo_bandstop"] = requested
            except ValueError as e:
                reject("lo_bandstop", requested, str(e))

        # --- trim_stopband / window --------------------------------------------
        if "trim_stopband" in req and req["trim_stopband"] is not None:
            accepted[cfg_prefix + "trim_stopband"] = bool(req["trim_stopband"])
            ack_field[cfg_prefix + "trim_stopband"] = ack_prefix + "trim_stopband"
            requested_map[cfg_prefix + "trim_stopband"] = req["trim_stopband"]
        if req.get("window") is not None:
            try:
                accepted[cfg_prefix + "window"] = _parse_window(req["window"])
                ack_field[cfg_prefix + "window"] = ack_prefix + "window"
                requested_map[cfg_prefix + "window"] = req["window"]
            except ValueError as e:
                reject("window", req["window"], str(e))

        return accepted, ack_field, requested_map

    def _validate_analysis(self, update: dict):
        """
        Freedom-model gate (P2a-2, generalized across analysis targets in
        P2b-1) for the "analysis" block of a control message. The block's
        optional "target" key routes to the analysis being configured
        (spectrogram is the default — the P2a wire format is unchanged).
        Never mutates the live config — returns (survivors, rounded, rejected,
        ignored) where `survivors` maps RadioConfig keys to values that passed
        tier 1 (knowable rules → snap and tell) AND tier 2 (striqt scratch
        validation on a tiny buffer). `rounded`/`rejected` are the ack entries:
        [{field, requested, used, reason}] / [{field, requested, reason}].
        """
        req = dict(update.get("analysis") or {})
        target = str(req.pop("target", "spectrogram") or "spectrogram").strip().lower()
        rounded, rejected = [], []
        if target not in ANALYSIS_TARGETS:
            known = ", ".join(sorted(ANALYSIS_TARGETS))
            rejected.append({"field": "target", "requested": target,
                             "reason": f"unknown analysis target (known: {known})"})
            return {}, rounded, rejected, []
        spec = ANALYSIS_TARGETS[target]

        eff = self._effective_radio(update)
        # The spectrogram/PSD analysis pipelines ALWAYS execute on the aligned
        # 28-multiple grid (their scratch validators and compute paths use
        # aligned_nfft unconditionally), so tier-1 fraction snapping must use
        # that grid regardless of which backend happens to be displayed —
        # otherwise a value snapped to k/1024 in quicklook would break the
        # window_fill integrality check the moment the calibrated view returns.
        on_calibrated_grid = target in {"spectrogram", "psd"} or (
            eff.backend in CALIBRATED_GRID_BACKENDS
        )

        # --- Tier 1: knowable rules, routed per target ------------------------
        accepted, ack_field, requested_map = self._tier1_target(
            target, req, eff, on_calibrated_grid, rounded, rejected
        )

        supported = set(spec["fields"]) | set(spec["virtual"])
        ignored = sorted(
            f"analysis.{k}" for k, v in req.items()
            if v is not None and k not in supported
        )

        # --- Tier 2: only striqt can judge — scratch-validate off-line -------
        # Apply the accepted fields one at a time onto a working copy so a
        # failure is attributed to the field that caused it; survivors keep
        # applying. The live config is untouched until update() commits the
        # survivors (never the rejects).
        candidate = eff.snapshot()
        for reset_key in spec.get("probe_reset", ()):
            if reset_key in accepted:
                setattr(candidate, reset_key, None)
        survivors = {}
        for key in (k for k in spec["order"] if k in accepted):
            trial = candidate.snapshot()
            setattr(trial, key, accepted[key])
            err = self.probe_analysis(trial, target)
            if err is None:
                candidate = trial
                survivors[key] = accepted[key]
            else:
                field = ack_field.get(key, key)
                rejected.append({"field": field,
                                 "requested": requested_map.get(key), "reason": err})
                rounded[:] = [r for r in rounded if r["field"] != field]
        return survivors, rounded, rejected, ignored

    def _tier1_target(self, target, req, eff, on_calibrated_grid, rounded, rejected):
        """Dispatch tier-1 validation for one analysis target. Returns
        (accepted, ack_field, requested_map) keyed by RadioConfig key."""
        if target == "spectrogram":
            accepted, ack_field, requested_map = self._tier1_freq_fields(
                req, eff, cfg_prefix="", ack_prefix="",
                on_calibrated_grid=on_calibrated_grid,
                rounded=rounded, rejected=rejected,
            )
            self._tier1_time_aperture(
                req, eff, on_calibrated_grid,
                accepted, ack_field, requested_map, rounded, rejected,
            )
            return accepted, ack_field, requested_map
        if target == "psd":
            accepted, ack_field, requested_map = self._tier1_freq_fields(
                req, eff, cfg_prefix="psd_", ack_prefix="psd.",
                on_calibrated_grid=on_calibrated_grid,
                rounded=rounded, rejected=rejected,
            )
            if req.get("time_statistic") is not None:
                requested = req["time_statistic"]
                try:
                    accepted["psd_time_statistic"] = _parse_time_statistic(requested)
                    ack_field["psd_time_statistic"] = "psd.time_statistic"
                    requested_map["psd_time_statistic"] = requested
                except ValueError as e:
                    rejected.append({"field": "psd.time_statistic",
                                     "requested": requested, "reason": str(e)})
            return accepted, ack_field, requested_map
        if target == "ssb":
            return self._tier1_ssb(req, eff, rounded, rejected)
        raise RuntimeError(f"no tier-1 validator for analysis target {target!r}")

    def _tier1_ssb(self, req, eff, rounded, rejected):
        """
        Tier-1 snap rules for the SSB target (P2b-5). Knowable constraints:
        the subcarrier spacing must admit a compatible capture rate (14·scs ≤
        SSB_MAX_RATE); the output rate can't exceed the sampled span; the
        discovery periodicity must cover at least one burst set (2 ms of
        symbols for every SCS) and one period must fit the IQ ring; the
        frequency offset must stay inside the sampled span; max_block_count is
        a whole number of burst sets or none. Everything subtler goes to the
        tier-2 scratch run. eff.sample_rate is moved onto the SSB grid the
        retune would pick, so the probes judge the config that would go live.
        """
        accepted, ack_field, requested_map = {}, {}, {}

        def tell(field, requested, used, reason):
            rounded.append({"field": "ssb." + field, "requested": requested,
                            "used": used, "reason": reason})

        def reject(field, requested, reason):
            rejected.append({"field": "ssb." + field,
                             "requested": requested, "reason": reason})

        def take(field, cfg_key, value):
            accepted[cfg_key] = value
            ack_field[cfg_key] = "ssb." + field
            requested_map[cfg_key] = req.get(field)

        if req.get("subcarrier_spacing") is not None:
            requested = req["subcarrier_spacing"]
            try:
                v = float(requested)
                if not (v > 0 and math.isfinite(v)):
                    raise ValueError("subcarrier spacing must be a positive, finite Hz value")
                snapped = min(max(v, 1e3), SSB_MAX_RATE / 14.0)
                if snapped != v:
                    tell("subcarrier_spacing", v, snapped,
                         f"the compatible capture rate 14·scs must stay within "
                         f"{SSB_MAX_RATE / 1e6:g} MS/s, and scs ≥ 1 kHz keeps the "
                         f"live FFT tractable")
                take("subcarrier_spacing", "ssb_subcarrier_spacing", float(snapped))
            except (TypeError, ValueError) as e:
                reject("subcarrier_spacing", requested, str(e))

        scs_eff = float(accepted.get("ssb_subcarrier_spacing",
                                     eff.ssb_subcarrier_spacing))

        # Probe at the capture rate the SSB retune would arm (update() commits
        # the real retune and reports it), so tier-2 judges the true config.
        if not ssb_grid_compatible(eff.sample_rate, scs_eff):
            compatible = ssb_compatible_rate(eff.sample_rate, scs_eff)
            if compatible:
                eff.sample_rate = float(compatible)

        if req.get("sample_rate") is not None:
            requested = req["sample_rate"]
            try:
                v = float(requested)
                if not (v > 0 and math.isfinite(v)):
                    raise ValueError("SSB output rate must be a positive, finite S/s value")
                if v > eff.sample_rate:
                    tell("sample_rate", v, eff.sample_rate,
                         "the SSB output band cannot exceed the sampled span")
                    v = float(eff.sample_rate)
                take("sample_rate", "ssb_sample_rate", float(v))
            except (TypeError, ValueError) as e:
                reject("sample_rate", requested, str(e))

        if req.get("discovery_periodicity") is not None:
            requested = req["discovery_periodicity"]
            try:
                v = float(requested)
                if not (v > 0 and math.isfinite(v)):
                    raise ValueError("discovery periodicity must be a positive, finite duration in seconds")
                burst_span = 2e-3   # symbol_rows·hop/fs == 2 ms for every SCS
                ring_cap = int(MAX_TAIL * RING_ROW_FILL) / eff.sample_rate
                snapped = min(max(v, burst_span), ring_cap)
                if snapped != v:
                    tell("discovery_periodicity", v, snapped,
                         "must cover at least one 2 ms burst set and one period "
                         "must fit the IQ ring")
                take("discovery_periodicity", "ssb_discovery_periodicity", float(snapped))
            except (TypeError, ValueError) as e:
                reject("discovery_periodicity", requested, str(e))

        if req.get("frequency_offset") is not None:
            requested = req["frequency_offset"]
            try:
                v = float(requested)
                if not math.isfinite(v):
                    raise ValueError("frequency offset must be a finite Hz value")
                # The truncated SSB band must fit the sampled span:
                # |offset| + ssb_rate/2 ≤ fs/2 (striqt raises otherwise).
                ssb_rate_eff = min(
                    float(accepted.get("ssb_sample_rate", eff.ssb_sample_rate)),
                    float(eff.sample_rate),
                )
                half = max(0.0, (eff.sample_rate - ssb_rate_eff) / 2.0)
                clamped = min(max(v, -half), half)
                # striqt's truncate_freqs also requires the offset on the
                # averaged subcarrier grid: a multiple of scs (knowable →
                # snap & tell; confirmed against the striqt error text).
                bin_hz = scs_eff
                snapped = round(clamped / bin_hz) * bin_hz
                if abs(snapped - v) > 1e-6 * max(abs(v), 1.0):
                    tell("frequency_offset", v, snapped,
                         f"must be a multiple of the subcarrier spacing "
                         f"{bin_hz / 1e3:g} kHz and keep the {ssb_rate_eff / 1e6:g} "
                         f"MS/s SSB band inside the sampled span")
                take("frequency_offset", "ssb_frequency_offset", float(snapped))
            except (TypeError, ValueError) as e:
                reject("frequency_offset", requested, str(e))

        if "max_block_count" in req and req["max_block_count"] is not None:
            requested = req["max_block_count"]
            try:
                v = requested
                if isinstance(v, str):
                    text = v.strip().lower()
                    v = None if text in ("", "none", "null", "off") else float(text)
                elif isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError("max_block_count must be a whole number of burst sets or 'none'")
                if v is not None:
                    if not math.isfinite(v) or v <= 0:
                        v = None
                    else:
                        k = max(1, round(v))
                        if k != v:
                            tell("max_block_count", v, k,
                                 "must be a whole number of burst sets")
                        v = int(k)
                take("max_block_count", "ssb_max_block_count", v)
            except (TypeError, ValueError) as e:
                reject("max_block_count", requested, str(e))

        if req.get("window") is not None:
            try:
                take("window", "ssb_window", _parse_window(req["window"]))
            except ValueError as e:
                reject("window", req["window"], str(e))

        if "lo_bandstop" in req and req["lo_bandstop"] is not None:
            requested = req["lo_bandstop"]
            try:
                v = _parse_optional_hz(requested)
                if v is not None:
                    if v < 0:
                        raise ValueError("lo_bandstop must be positive or 'none'")
                    if v > eff.sample_rate:
                        tell("lo_bandstop", v, eff.sample_rate,
                             "cannot exceed the sampled span (sample_rate)")
                        v = float(eff.sample_rate)
                take("lo_bandstop", "ssb_lo_bandstop", v)
            except ValueError as e:
                reject("lo_bandstop", requested, str(e))

        return accepted, ack_field, requested_map

    def _tier1_time_aperture(self, req, eff, on_calibrated_grid,
                             accepted, ack_field, requested_map, rounded, rejected):
        """
        Tier-1 rule for the spectrogram time_aperture (P2b-2): striqt requires an
        integer multiple of the row hop period hop/fs — where hop follows the
        overlap/nfft THIS message may also be changing. Snaps a requested value
        to the nearest hop multiple within one frame; when the message moves the
        hop grid under an existing aperture, the aperture is re-snapped to the
        new grid (reported), instead of letting the next frame throw.
        """
        nfft_eff = accepted.get("nfft", eff.nfft)
        nfft_axis = aligned_nfft(nfft_eff) if on_calibrated_grid else int(nfft_eff)
        overlap = accepted.get("fractional_overlap", eff.fractional_overlap)
        hop = analysis_hop(nfft_axis, overlap)
        hop_period = hop / float(eff.sample_rate)

        requested = req.get("time_aperture")
        if requested is not None:
            try:
                v = _parse_optional_seconds(requested)
                if v is None:
                    accepted["time_aperture"] = None
                else:
                    k = min(max(1, round(v / hop_period)), max(1, int(eff.rows)))
                    used = k * hop_period
                    accepted["time_aperture"] = used
                    if abs(used - v) > 1e-9 * max(v, hop_period):
                        rounded.append({
                            "field": "time_aperture", "requested": v, "used": used,
                            "reason": (f"must be an integer multiple of the row hop "
                                       f"(1-overlap)·nfft/fs = {hop_period * 1e3:.4f} ms, "
                                       f"within one frame; using {k} rows"),
                        })
                ack_field["time_aperture"] = "time_aperture"
                requested_map["time_aperture"] = requested
            except ValueError as e:
                rejected.append({"field": "time_aperture",
                                 "requested": requested, "reason": str(e)})
        elif eff.time_aperture and ("nfft" in accepted or "fractional_overlap" in accepted):
            # Follow-along: the hop grid moved and the standing aperture no longer
            # divides it — re-snap (and tell) rather than let the live frame throw.
            samples = round(float(eff.time_aperture) * float(eff.sample_rate))
            if samples % hop != 0:
                k = max(1, round(samples / hop))
                used = k * hop_period
                accepted["time_aperture"] = used
                ack_field["time_aperture"] = "time_aperture"
                requested_map["time_aperture"] = eff.time_aperture
                rounded.append({
                    "field": "time_aperture",
                    "requested": float(eff.time_aperture), "used": used,
                    "reason": "this message changed the row hop; re-snapped the "
                              "standing time_aperture to the new hop grid",
                })

    def update(self, update: dict) -> dict:
        """
        Apply key/value updates. Returns an ack
        {applied, ignored, reconnect, rounded, rejected}.
        """
        # Analysis params are only settable through the validated "analysis"
        # block — strip top-level occurrences so nothing bypasses the freedom
        # model (P2a-2).
        update = {k: v for k, v in update.items() if k not in ANALYSIS_CFG_KEYS}
        ignored = []
        reconnect = []
        rounded = []
        rejected = []
        # An explicit top-level {"rows": N} control reclaims rows ownership from
        # duration (P2a-4). Recorded before the capture branch merges its own
        # duration-derived keys into the update.
        explicit_rows = update.get("rows") is not None
        # Capture fields that map to a live radio parameter; the rest are rendered
        # by the editor but cannot be applied live — reported, not dropped (LV-F6).
        # The four capture knobs below share their name with the cfg field, so they
        # pass straight through (P1-2); they take effect on the next re-arm.
        # `duration` is now a first-class cfg field (P2a-4): it maps straight
        # through, and rows are derived from it hop-aware AFTER all of this
        # message's changes land (see the post-loop derivation below) — so the
        # mapping always uses the effective backend/nfft/overlap (LV-R9b).
        passthru_capture = {
            "analysis_bandwidth", "lo_shift", "host_resample", "backend_sample_rate",
            "duration",
        }
        capture_mapped = {"center_frequency", "sample_rate", "gain", "nfft"} | passthru_capture
        if "capture" in update and isinstance(update["capture"], dict):
            capture = update["capture"]
            mapped = {}
            if capture.get("center_frequency") is not None:
                mapped["center"] = capture["center_frequency"]
            if capture.get("sample_rate") is not None:
                mapped["sample_rate"] = capture["sample_rate"]
            if capture.get("gain") is not None:
                mapped["gain"] = capture["gain"]
            if capture.get("nfft") is not None:
                mapped["nfft"] = capture["nfft"]
            # Clearing an optional field has an explicit meaning (reset to its
            # default); an explicit JSON null must not vanish as "ignored".
            passthru_null_defaults = {
                "analysis_bandwidth": float("inf"),
                "lo_shift":           "none",
                "host_resample":      False,
                "backend_sample_rate": 0.0,
                "duration":           0.0,
            }
            for key in passthru_capture:
                if key not in capture:
                    continue
                value = capture[key]
                mapped[key] = passthru_null_defaults[key] if value is None else value
            ignored = sorted(
                k for k, v in capture.items() if v is not None and k not in capture_mapped
            )
            update = dict(update)
            update.update(mapped)

        # Freedom-model analysis block (P2a-2): tier-1 snap + tier-2 striqt
        # scratch validation. Only the survivors are merged into the update; the
        # live cfg never sees a rejected value. Runs after the capture branch so
        # it sees the nfft/sample_rate this same message is applying.
        if "analysis" in update and isinstance(update["analysis"], dict):
            survivors, rounded, rejected, analysis_ignored = self._validate_analysis(update)
            ignored = sorted(set(ignored) | set(analysis_ignored))
            update = dict(update)
            update.update(survivors)

        # Source-spec changes now genuinely APPLY, via a verified device
        # reconnect (close → rebuild spec with the overrides → reopen →
        # readback). Explicit nulls CLEAR an override back to the adapter
        # default. Merged into cfg.source_config under the lock below.
        source_requested = {}
        if "source" in update and isinstance(update["source"], dict):
            source_requested = {str(k): v for k, v in update["source"].items()}

        valid = {
            "center", "sample_rate", "gain", "nfft", "rows", "backend", "lo_null",
            "analysis_bandwidth", "lo_shift", "host_resample", "backend_sample_rate",
            "duration",
        } | ANALYSIS_CFG_KEYS
        changes = []
        with self._lock:
            # Device capability bounds for this message's clamps (P3-3).
            # Read directly — self._lock is already held (envelope() would
            # re-take it).
            env = self._envelope
            # Effective backend/SCS for THIS message: an SSB-grid rate (e.g. the
            # retuned 13.44 MS/s coming back from a server-seeded form) must not
            # be snapped onto the LTE list only for the SSB retune to undo it —
            # that round trip would dirty the config and re-arm the radio on
            # every bare Apply.
            eff_backend = str(update.get("backend", self._cfg.backend)).strip().lower()
            if eff_backend not in BACKENDS:
                eff_backend = self._cfg.backend
            try:
                eff_scs = float(update.get("ssb_subcarrier_spacing",
                                           self._cfg.ssb_subcarrier_spacing))
            except (TypeError, ValueError):
                eff_scs = float(self._cfg.ssb_subcarrier_spacing)
            if source_requested:
                source_cfg = dict(self._cfg.source_config or {})
                for key, value in source_requested.items():
                    old_val = source_cfg.get(key)
                    if value is None:
                        if key not in source_cfg:
                            continue
                        source_cfg.pop(key)
                    else:
                        if old_val == value:
                            continue
                        source_cfg[key] = value
                    changes.append(("source." + key, old_val, value))
                    reconnect.append(key)
                if reconnect:
                    self._cfg.source_config = source_cfg
            for key, value in update.items():
                if key not in valid:
                    continue
                if key == "backend":
                    value = str(value).strip().lower()
                    if value not in BACKENDS:
                        continue
                elif key in {"lo_null", "host_resample"}:
                    value = bool(value)
                elif key == "lo_shift":
                    # striqt LOShift is Literal['left','right','none'].
                    value = str(value).strip().lower()
                    if value not in {"left", "right", "none"}:
                        continue
                elif key == "analysis_bandwidth":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if not (math.isinf(value) or value > 0):
                        continue   # must be a positive bandwidth or inf (no limit)
                elif key == "backend_sample_rate":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if value < 0:
                        continue   # 0 == track sample_rate; otherwise a positive rate
                elif key in ANALYSIS_CFG_KEYS:
                    # Already validated by _validate_analysis — only its
                    # survivors reach this loop (top-level copies are stripped).
                    pass
                elif key == "duration":
                    try:
                        value = max(0.0, float(value))   # seconds; 0 = rows-driven
                    except (TypeError, ValueError):
                        continue
                else:
                    value = int(value) if key in {"nfft", "rows"} else float(value)
                # Clamp rows to what the ring can supply for the current backend/
                # nfft (P1-5). nfft, if changed in this same message, is applied
                # earlier in the loop, so self._cfg already reflects it here.
                if key == "rows":
                    value = int(max(1, min(value, max_live_rows(self._cfg))))
                elif key == "center":
                    value = float(max(env["freq_min"], min(value, env["freq_max"])))
                elif key == "sample_rate":
                    value = float(value)
                    if not (eff_backend == "ssb" and ssb_grid_compatible(value, eff_scs)):
                        value = float(_snap(value, allowed_rates(env)))
                    value = float(max(env["rate_min"], min(value, env["rate_max"])))
                elif key == "gain":
                    value = float(max(env["gain_min"], min(value, env["gain_max"])))
                elif key == "nfft":
                    value = int(_snap(value, NFFT_CHOICES))
                    value = int(max(128, min(value, 8192)))
                old = getattr(self._cfg, key)
                if old == value:
                    continue
                setattr(self._cfg, key, value)
                changes.append((key, old, value))
            if changes:
                # Rows ownership (P2a-4): an explicit top-level rows control
                # reclaims rows-driven mode; otherwise a positive duration owns
                # rows and re-derives them hop-aware from the FINAL state of
                # this update (duration·fs / row_hop) — matching the client's
                # time-axis label for any backend/nfft/overlap combination.
                changed_keys = {k for k, _, _ in changes}
                if explicit_rows and "duration" not in changed_keys and self._cfg.duration:
                    changes.append(("duration", self._cfg.duration, 0.0))
                    self._cfg.duration = 0.0
                # SSB honesty (P2b-5): the symbol-aligned SSB view only exists
                # on the 14·scs capture grid. When this message leaves the SSB
                # backend at an incompatible rate (selecting SSB, changing the
                # SCS, or picking an off-grid rate), retune to the nearest
                # compatible rate and REPORT it — never a phantom SSB. Runs
                # before the duration→rows derivation so rows follow the new
                # rate/geometry.
                if self._cfg.backend == "ssb" and not ssb_grid_compatible(
                        self._cfg.sample_rate, self._cfg.ssb_subcarrier_spacing):
                    new_rate = ssb_compatible_rate(
                        self._cfg.sample_rate, self._cfg.ssb_subcarrier_spacing
                    )
                    if new_rate and new_rate != self._cfg.sample_rate:
                        rounded.append({
                            "field": "sample_rate",
                            "requested": float(self._cfg.sample_rate),
                            "used": float(new_rate),
                            "reason": "SSB needs 2·sample_rate/subcarrier_spacing "
                                      "to be a multiple of 28 (striqt symbol "
                                      "grid); retuned the capture rate",
                        })
                        changes.append(("sample_rate", self._cfg.sample_rate, new_rate))
                        self._cfg.sample_rate = float(new_rate)
                if self._cfg.duration > 0:
                    rows_new = int(max(1, min(
                        round(self._cfg.duration * self._cfg.sample_rate / row_hop(self._cfg)),
                        max_live_rows(self._cfg),
                    )))
                    if rows_new != self._cfg.rows:
                        changes.append(("rows", self._cfg.rows, rows_new))
                        self._cfg.rows = rows_new
                # A new overlap/nfft/backend changes the per-row hop, which can
                # push samples_needed(rows) past what the ring can supply — the
                # Computer's avail >= need gate would then never pass and the
                # display would starve. Re-clamp rows against the new hop.
                max_rows = max_live_rows(self._cfg)
                if self._cfg.rows > max_rows:
                    old_rows = self._cfg.rows
                    self._cfg.rows = max_rows
                    changes.append(("rows", old_rows, max_rows))
                self._dirty = True
                self._changed_fields.update(k for k, _, _ in changes)
                if reconnect:
                    self._reconnect = True
        # Operation record (outside the lock — logging does I/O). Every net
        # config change becomes a tracked operation whose hardware apply +
        # readback stages are appended by the Acquirer once it consumes the
        # dirty flag (take_dirty → rearm → read_back → data-path check).
        op_id = None
        if changes or rounded or rejected:
            summary = ", ".join(
                f"{k} → {fmt_value(k, v)}" for k, _, v in changes
            ) or "no net change"
            op_id = OPERATIONS.begin("config", summary)
            OPERATIONS.set_fields(op_id, [k for k, _, _ in changes])
            for key, old, value in changes:
                OPERATIONS.stage(
                    op_id, "validated",
                    f"{key}: {fmt_value(key, old)} → {fmt_value(key, value)}",
                )
            for entry in rounded:
                OPERATIONS.stage(
                    op_id, "rounded",
                    f"{entry['field']}: {entry['requested']} → {entry['used']} "
                    f"({entry['reason']})",
                )
            for entry in rejected:
                OPERATIONS.stage(op_id, "rejected",
                                 f"{entry['field']}: {entry['reason']}",
                                 level="warn")
            if ignored:
                OPERATIONS.stage(op_id, "ignored", ", ".join(ignored))
            if reconnect:
                OPERATIONS.stage(op_id, "reconnect",
                                 "source fields apply via a device reconnect: "
                                 + ", ".join(sorted(reconnect)))
            if changes:
                # Hand the op to the Acquirer for the hardware stages. A prior
                # op still waiting for hardware apply is superseded honestly.
                with self._lock:
                    stale, self._pending_op = self._pending_op, op_id
                if stale is not None:
                    OPERATIONS.finish(stale, "superseded",
                                      f"replaced by op #{op_id} before "
                                      f"hardware apply")
            else:
                OPERATIONS.finish(op_id, "success",
                                  "nothing changed on the radio")
        return {
            "applied":   [k for k, _, _ in changes],
            "ignored":   ignored,
            "reconnect": reconnect,
            "rounded":   rounded,
            "rejected":  rejected,
            "op_id":     op_id,
        }

    def take_dirty(self):
        """Returns (dirty, cfg_snapshot, op_id, reconnect, changed_fields).

        ``op_id`` is the
        pending operation awaiting hardware apply/verification (or None);
        reconnect means source-spec overrides changed and the device must be
        closed and reopened rather than rearmed. ``changed_fields`` lets the
        acquisition loop avoid touching the radio for compute-only changes.
        """
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            op_id, self._pending_op = self._pending_op, None
            reconnect, self._reconnect = self._reconnect, False
            changed, self._changed_fields = set(self._changed_fields), set()
            return dirty, self._cfg.snapshot(), op_id, reconnect, changed

    def restore_source(self, source_config, reason=""):
        """Backstop for a failed reconnect: revert the source overrides to the
        last set that demonstrably opened, notify viewers, and return a fresh
        snapshot to recover with."""
        with self._lock:
            self._cfg.source_config = dict(source_config or {})
            snap = self._cfg.snapshot()
        self.push_notice(
            "source settings failed to apply"
            + (" ({})".format(reason) if reason else "")
            + " — reverted to the last working source configuration"
        )
        return snap

    def restore_config(self, config: RadioConfig, reason=""):
        """Roll back a recipe that validated but failed at hardware apply."""
        with self._lock:
            self._cfg = config.snapshot()
            self._dirty = False
            self._reconnect = False
            self._changed_fields.clear()
            self._pending_op = None
            snap = self._cfg.snapshot()
        self.push_notice(
            "hardware rejected the requested configuration"
            + (" ({})".format(reason) if reason else "")
            + " — restored the last working recipe"
        )
        return snap

    def stop(self):
        with self._lock:
            self._stop = True

    def stopped(self):
        with self._lock:
            return self._stop
