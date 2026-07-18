"""Freedom-model input parsing and tier-2 scratch validators.

Structure-only parsers (they never judge legality) plus the striqt scratch
validators that judge a proposed config by running the real analysis pipeline
on a tiny synthetic buffer. Extracted verbatim from striqt_web_server.py.
"""
from __future__ import annotations

import math
import warnings
from fractions import Fraction

import numpy as np

from .constants import (
    DEFAULT_WINDOW, DEFAULT_FRACTIONAL_OVERLAP, DEFAULT_WINDOW_FILL,
    DEFAULT_INTEGRATION_BW, DEFAULT_LO_BANDSTOP, DEFAULT_TRIM_STOPBAND,
    DEFAULT_PSD_TIME_STATISTIC, SSB_SUBCARRIER_SPACING, SSB_SAMPLE_RATE,
    SSB_DISCOVERY_PERIOD, SSB_WINDOW, SSB_LO_BANDSTOP,
)
from .striqt_compat import (
    analysis_specs, striqt_measurements, striqt_shared, _ANALYSIS_OK,
)
from .dsp import (
    aligned_nfft, analysis_hop, calibrated_sample_count, make_analysis_spec,
    make_psd_kwargs, make_ssb_kwargs, ssb_geometry, ssb_block_samples,
)

# ---------------------------------------------------------------------------
# Freedom-model input parsing (P2a-2)
# ---------------------------------------------------------------------------
#
# DAN mode has no input guardrail — the user can type anything — so these
# parsers only normalize *structure* (they never judge legality). Legality is
# decided by tier 1 (knowable rules → snap and tell) and tier 2 (striqt itself,
# via scratch_validate_analysis) in SharedConfig._validate_analysis.

# Freedom-model analysis targets (P2b-1). Each target names one striqt analysis
# whose parameter block is editable from the DAN-mode Analysis panel; the same
# three tiers (snap & tell / scratch-validate / compute backstop) govern all of
# them. A control message routes with {"analysis": {"target": <name>, ...}};
# no target means "spectrogram" (the P2a wire format, unchanged).
#   fields:  message field name -> RadioConfig attribute
#   virtual: message fields validated here that map onto a non-analysis cfg key
#            (frequency_resolution is the second view of nfft)
#   order:   tier-2 one-at-a-time application order (RadioConfig keys)
ANALYSIS_TARGETS = {
    "spectrogram": {
        "fields": {
            "window":                "window",
            "fractional_overlap":    "fractional_overlap",
            "window_fill":           "window_fill",
            "integration_bandwidth": "integration_bandwidth",
            "lo_bandstop":           "lo_bandstop",
            "trim_stopband":         "trim_stopband",
            "time_aperture":         "time_aperture",
        },
        "virtual": ("frequency_resolution",),
        # time_aperture goes last: its legality depends on the overlap/nfft this
        # same message may be changing (the hop grid).
        "order": ("nfft", "window", "fractional_overlap", "window_fill",
                  "integration_bandwidth", "lo_bandstop", "trim_stopband",
                  "time_aperture"),
        # Cleared on the tier-2 working copy while earlier fields probe, when a
        # replacement value is accepted: time_aperture rides the hop grid that
        # nfft/overlap define, so probing those with the STALE aperture attached
        # would falsely reject them; the fresh aperture re-probes at its own turn.
        "probe_reset": ("time_aperture",),
    },
    # striqt power_spectral_density (P2b-3): the Welch-method statistic traces.
    # Own parameter block (psd_* cfg keys) so tuning the PSD view never
    # disturbs the spectrogram recipe, per-analysis-panel intent.
    "psd": {
        "fields": {
            "window":                "psd_window",
            "fractional_overlap":    "psd_fractional_overlap",
            "window_fill":           "psd_window_fill",
            "integration_bandwidth": "psd_integration_bandwidth",
            "lo_bandstop":           "psd_lo_bandstop",
            "trim_stopband":         "psd_trim_stopband",
            "time_statistic":        "psd_time_statistic",
        },
        "virtual": ("frequency_resolution",),
        "order": ("nfft", "psd_window", "psd_fractional_overlap",
                  "psd_window_fill", "psd_integration_bandwidth",
                  "psd_lo_bandstop", "psd_trim_stopband", "psd_time_statistic"),
    },
    # striqt cellular_5g_ssb_spectrogram (P2b-5): the symbol-aligned SSB burst
    # view. subcarrier_spacing goes first — it defines the grid every other
    # field (and the capture sample-rate retune) is judged against.
    "ssb": {
        "fields": {
            "subcarrier_spacing":    "ssb_subcarrier_spacing",
            "sample_rate":           "ssb_sample_rate",
            "discovery_periodicity": "ssb_discovery_periodicity",
            "frequency_offset":      "ssb_frequency_offset",
            "max_block_count":       "ssb_max_block_count",
            "window":                "ssb_window",
            "lo_bandstop":           "ssb_lo_bandstop",
        },
        "virtual": (),
        "order": ("ssb_subcarrier_spacing", "ssb_sample_rate",
                  "ssb_discovery_periodicity", "ssb_frequency_offset",
                  "ssb_max_block_count", "ssb_window", "ssb_lo_bandstop"),
    },
}

# RadioConfig fields that are only settable through the validated "analysis"
# block (the union across targets). Stripped from the top level of every
# control message so no client can bypass the freedom model.
ANALYSIS_CFG_KEYS = frozenset(
    cfg_key
    for target in ANALYSIS_TARGETS.values()
    for cfg_key in target["fields"].values()
)

# Hard-default analysis values — the final revert target for the P2a-3 backstop
# (identical to the RadioConfig field defaults).
ANALYSIS_DEFAULTS = {
    "window":                DEFAULT_WINDOW,
    "fractional_overlap":    DEFAULT_FRACTIONAL_OVERLAP,
    "window_fill":           DEFAULT_WINDOW_FILL,
    "integration_bandwidth": DEFAULT_INTEGRATION_BW,
    "lo_bandstop":           DEFAULT_LO_BANDSTOP,
    "trim_stopband":         DEFAULT_TRIM_STOPBAND,
    "time_aperture":         None,
    "psd_window":                DEFAULT_WINDOW,
    "psd_fractional_overlap":    DEFAULT_FRACTIONAL_OVERLAP,
    "psd_window_fill":           DEFAULT_WINDOW_FILL,
    "psd_integration_bandwidth": DEFAULT_INTEGRATION_BW,
    "psd_lo_bandstop":           DEFAULT_LO_BANDSTOP,
    "psd_trim_stopband":         DEFAULT_TRIM_STOPBAND,
    "psd_time_statistic":        DEFAULT_PSD_TIME_STATISTIC,
    "ssb_subcarrier_spacing":    SSB_SUBCARRIER_SPACING,
    "ssb_sample_rate":           SSB_SAMPLE_RATE,
    "ssb_discovery_periodicity": SSB_DISCOVERY_PERIOD,
    "ssb_frequency_offset":      0.0,
    "ssb_max_block_count":       None,
    "ssb_window":                SSB_WINDOW,
    "ssb_lo_bandstop":           SSB_LO_BANDSTOP,
}


def _parse_window(value):
    """Normalize a window spec to what scipy get_window accepts: a name string
    or a (name, float parameter) tuple. Accepts "kaiser, 11.88" shorthand and
    the JSON list form ["kaiser", 11.88]. Raises ValueError on bad structure."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("window must not be empty")
        if "," in text:
            name, _, param = text.partition(",")
            name, param = name.strip(), param.strip()
            try:
                return (name, float(param))
            except ValueError:
                raise ValueError(f"window parameter {param!r} is not a number")
        return text
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str):
        try:
            return (str(value[0]), float(value[1]))
        except (TypeError, ValueError):
            raise ValueError(f"window parameter {value[1]!r} is not a number")
    raise ValueError("window must be a name or name,parameter (scipy get_window spec)")


def _parse_fraction(value) -> Fraction:
    """Parse "13/28", a float, or an int into a Fraction. Raises ValueError."""
    if isinstance(value, str):
        value = value.strip()
    try:
        return Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError):
        raise ValueError(f"{value!r} is not a fraction (use e.g. 13/28 or 0.464)")


def _parse_optional_hz(value, *, auto_ok: bool = False):
    """Parse a nullable Hz field: None/""/"none"/"off"/0 → None; "auto" → "auto"
    (when allowed); otherwise a float Hz value. Raises ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "null", "off"):
            return None
        if auto_ok and text == "auto":
            return "auto"
        value = text
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a bandwidth in Hz"
                         + (" (or 'auto'/'none')" if auto_ok else " (or 'none')"))
    if value == 0:
        return None
    return value


def _parse_time_statistic(value):
    """Parse the PSD time_statistic surface: a list (or comma string) of named
    statistics ('mean', 'max', …) and/or quantiles in [0, 1], e.g.
    "mean, 0.5, 0.95, max". Returns a de-duplicated tuple of str/float.
    Structure and quantile range are judged here (knowable); unknown statistic
    NAMES are left for striqt itself to judge in tier 2. Raises ValueError."""
    if isinstance(value, str):
        tokens = [t.strip() for t in value.split(",")]
    elif isinstance(value, (list, tuple)):
        tokens = list(value)
    else:
        raise ValueError("time_statistic must be a list like mean, 0.95, max")
    out = []
    for tok in tokens:
        if isinstance(tok, str):
            tok = tok.strip().lower()
            if not tok:
                continue
            try:
                tok = float(tok)
            except ValueError:
                out.append(tok)
                continue
        if isinstance(tok, bool) or not isinstance(tok, (int, float)):
            raise ValueError(f"{tok!r} is not a statistic name or quantile")
        q = float(tok)
        if not (0.0 <= q <= 1.0):
            raise ValueError(
                f"quantile {q!r} is out of range — entries must be statistic "
                f"names (mean/max/…) or quantiles in [0, 1]"
            )
        out.append(q)
    seen, dedup = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    if not dedup:
        raise ValueError("time_statistic needs at least one entry (e.g. mean)")
    return tuple(dedup)


def _parse_optional_seconds(value):
    """Parse a nullable seconds field: None/""/"none"/"off"/0 → None; otherwise
    a positive, finite float in seconds. Raises ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "null", "off"):
            return None
        value = text
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a duration in seconds (or 'none')")
    if value == 0:
        return None
    if not (value > 0 and math.isfinite(value)):
        raise ValueError("must be a positive, finite duration in seconds (or 'none')")
    return value


def scratch_validate_spectrogram(cfg: "RadioConfig"):
    """
    Tier 2 of the freedom model: judge a proposed analysis config the only way
    that is always right — by asking striqt. Builds the exact Spectrogram spec
    the live Computer would run and evaluates it on a tiny synthetic buffer
    (2 STFT rows of zeros, single channel) WITHOUT touching the live ring or
    acquirer. Returns the striqt error text when the config is illegal, or None
    when it is safe to swap into the live stream.
    """
    if not _ANALYSIS_OK:
        return None   # nothing to judge without striqt (quicklook-only install)
    try:
        sample_rate = float(cfg.sample_rate)
        nfft   = aligned_nfft(cfg.nfft)
        hop    = analysis_hop(nfft, cfg.fractional_overlap)
        # Give the scratch run enough STFT rows that a configured time_aperture
        # produces at least one averaged output row — otherwise a legal aperture
        # would be judged on an empty result instead of striqt's real verdict.
        rows_scratch = 2
        if cfg.time_aperture:
            rows_scratch = max(2, round(float(cfg.time_aperture) * sample_rate / hop))
        needed = calibrated_sample_count(nfft, rows_scratch, hop)
        spec   = make_analysis_spec(cfg, nfft, sample_rate)   # construction may raise
        capture = analysis_specs.Capture(
            sample_rate=sample_rate,
            duration=needed / sample_rate,
            analysis_bandwidth=float(cfg.analysis_bandwidth),
        )
        tiny = np.zeros((1, needed), dtype=np.complex64)
        with warnings.catch_warnings():
            # The 2-row zero buffer is degenerate on purpose; numeric warnings
            # (empty-slice means etc.) are expected noise, not verdicts.
            warnings.simplefilter("ignore")
            striqt_shared.evaluate_spectrogram(tiny, capture, spec, dtype="float32", dB=True)
    except Exception as e:
        return str(e).strip() or type(e).__name__
    return None


def scratch_validate_psd(cfg: "RadioConfig"):
    """
    Tier-2 judge for the PSD target (P2b-3): run striqt's real
    power_spectral_density on a tiny synthetic buffer (2 STFT rows, single
    channel) with the exact kwargs the live compute would use. Returns the
    striqt error text on an illegal config (e.g. an unknown statistic name),
    or None when it is safe to go live.
    """
    if not _ANALYSIS_OK:
        return None
    try:
        sample_rate = float(cfg.sample_rate)
        nfft   = aligned_nfft(cfg.nfft)
        hop    = analysis_hop(nfft, cfg.psd_fractional_overlap)
        needed = calibrated_sample_count(nfft, 2, hop)
        kwargs = make_psd_kwargs(cfg, nfft, sample_rate)   # construction may raise
        capture = analysis_specs.Capture(
            sample_rate=sample_rate,
            duration=needed / sample_rate,
            analysis_bandwidth=float(cfg.analysis_bandwidth),
        )
        tiny = np.zeros((1, needed), dtype=np.complex64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            striqt_measurements.power_spectral_density(
                tiny, capture, as_xarray=False, **kwargs
            )
    except Exception as e:
        return str(e).strip() or type(e).__name__
    return None


def scratch_validate_ssb(cfg: "RadioConfig"):
    """
    Tier-2 judge for the SSB target (P2b-5): run striqt's real
    cellular_5g_ssb_spectrogram on a one-burst-set synthetic buffer with the
    exact kwargs the live compute would use. cfg.sample_rate must already be
    on the SSB grid for cfg's subcarrier spacing (the tier-1 branch retunes
    the effective rate before probing). Returns the striqt error text when a
    param combination is illegal, or None when it is safe to go live.
    """
    if not _ANALYSIS_OK:
        return None
    try:
        sample_rate = float(cfg.sample_rate)
        geo = ssb_geometry(cfg)   # off-grid raises → worded rejection
        needed = ssb_block_samples(geo, 1)
        kwargs = make_ssb_kwargs(cfg)
        capture = analysis_specs.Capture(
            sample_rate=sample_rate,
            duration=needed / sample_rate,
            analysis_bandwidth=float("inf"),
        )
        tiny = np.zeros((1, needed), dtype=np.complex64)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            striqt_measurements.cellular_5g_ssb_spectrogram(
                tiny, capture, as_xarray=False, **kwargs
            )
    except Exception as e:
        return str(e).strip() or type(e).__name__
    return None


# Tier-2 scratch validators, one per analysis target (P2b-1). Each judges a
# proposed RadioConfig by running the target's real striqt pipeline on a tiny
# synthetic buffer — never the live ring.
SCRATCH_VALIDATORS = {
    "spectrogram": scratch_validate_spectrogram,
    "psd":         scratch_validate_psd,
    "ssb":         scratch_validate_ssb,
}


def scratch_validate_analysis(cfg: "RadioConfig", target: str = "spectrogram"):
    fn = SCRATCH_VALIDATORS.get(target)
    return fn(cfg) if fn else None

