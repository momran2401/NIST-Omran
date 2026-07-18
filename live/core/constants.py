"""Shared constants and device profile data.

Pure data — no imports from other core modules, so anything can import this
without cycles. DEVICE_PROFILES is consumed by core.devices (which wraps each
profile in an adapter) and by SharedConfig (capability envelopes).
"""
from __future__ import annotations

from fractions import Fraction

# ---------------------------------------------------------------------------
# Device profiles (P3-1). One entry per supported SDR; data only — the source
# factories live in core.devices. DEVICE/DEVICE_LABEL/CHANNELS are resolved
# once at startup (core.state.configure_device) before any thread or
# SharedConfig exists; every later read is runtime, so set-once is safe.
#
#   channels        RX port tuple the acquirer streams
#   defaults        RadioConfig seeds (center / sample_rate / gain)
#   envelope        capability fallback: tier-1 clamp bounds (P3-3)
#   query_envelope  True → ask the live SoapySDR device for its real ranges
#                   after open and merge them over the fallback. False for
#                   air8201b/demo: their fallback IS today's exact clamp
#                   numbers (the −60..10 gain window is a striqt calibrated-
#                   gain convention, not the raw SoapyAIRT range — querying
#                   would shift legal bounds on the existing deployment).
# ---------------------------------------------------------------------------

DEVICE_PROFILES = {
    "air8201b": {
        "label": "AIR8201B",
        "channels": (0, 1),
        "defaults": {"center": 1955e6, "sample_rate": 15.36e6, "gain": 0.0},
        "envelope": {
            "freq_min": 300e6, "freq_max": 6e9,
            "gain_min": -60.0, "gain_max": 10.0,
            "rate_min": 1e6,   "rate_max": 125e6,
        },
        "query_envelope": False,
    },
    # Other Deepwave AIR-T models: same SoapyAIRT driver + AirStack stack.
    # Their striqt spec classes are used when the installed build ships them
    # (see core.striqt_compat); the AIR8201B numbers are the safe fallback
    # envelope until the live device is queried.
    "air7101b": {
        "label": "AIR7101B",
        "channels": (0, 1),
        "defaults": {"center": 1955e6, "sample_rate": 15.36e6, "gain": 0.0},
        "envelope": {
            "freq_min": 300e6, "freq_max": 6e9,
            "gain_min": -60.0, "gain_max": 10.0,
            "rate_min": 1e6,   "rate_max": 125e6,
        },
        "query_envelope": True,
    },
    "air7201b": {
        "label": "AIR7201B",
        "channels": (0, 1),
        "defaults": {"center": 1955e6, "sample_rate": 15.36e6, "gain": 0.0},
        "envelope": {
            "freq_min": 300e6, "freq_max": 6e9,
            "gain_min": -60.0, "gain_max": 10.0,
            "rate_min": 1e6,   "rate_max": 125e6,
        },
        "query_envelope": True,
    },
    "pluto": {
        "label": "PlutoSDR",
        "channels": (0,),
        # AD936x reference — NOT the AIR-T's 125 MHz (bug_report P-1). The
        # plutosdr Soapy driver may ignore the field entirely; a correct value
        # is harmless either way.
        "master_clock_rate": 61.44e6,
        # 3.84 MS/s default: sustained 15.36 MS/s over the Pluto's USB link is
        # optimistic; start on the safe LTE grid point and let the user go up.
        "defaults": {"center": 1955e6, "sample_rate": 3.84e6, "gain": 0.0},
        "envelope": {
            "freq_min": 325e6,  "freq_max": 3.8e9,
            "gain_min": 0.0,    "gain_max": 73.0,
            "rate_min": 0.52e6, "rate_max": 61.44e6,
        },
        "query_envelope": True,
    },
    "soapy": {
        # Generic SoapySDR device (best-effort): channels and capability
        # ranges are discovered from the live driver after open; these
        # fallbacks only exist so the UI has sane bounds until then.
        "label": "SoapySDR device",
        "channels": (0,),
        "defaults": {"center": 1955e6, "sample_rate": 3.84e6, "gain": 0.0},
        "envelope": {
            "freq_min": 1e6,   "freq_max": 6e9,
            "gain_min": 0.0,   "gain_max": 76.0,
            "rate_min": 0.25e6, "rate_max": 61.44e6,
        },
        "query_envelope": True,
    },
    "demo": {
        "label": "Demo (synthetic IQ)",
        "channels": (0, 1),
        "defaults": {"center": 1955e6, "sample_rate": 15.36e6, "gain": 0.0},
        "envelope": {
            "freq_min": 300e6, "freq_max": 6e9,
            "gain_min": -60.0, "gain_max": 10.0,
            "rate_min": 1e6,   "rate_max": 125e6,
        },
        "query_envelope": False,
    },
}

DEFAULT_CENTER      = 1955e6
DEFAULT_SAMPLE_RATE = 15.36e6
DEFAULT_GAIN        = 0.0
DEFAULT_NFFT        = 1024
DEFAULT_ROWS        = 12      # rows per frame (window_ms drives this from browser)

MASTER_CLOCK_RATE   = 125e6
READ_SIZE           = 1 << 18   # max IQ samples per _read_stream call (262144)
MAX_TAIL            = 1 << 22   # per-channel ring buffer capacity (4M samples)
DATA_STALE_SEC      = 1.0       # get_latest() returns None if the ring is older

SCROLL_ROWS         = 12        # rows per frame in Cool (scroll/waterfall) mode
# Rows are bounded by what the IQ ring can actually supply (see max_live_rows()),
# not a flat cap. MAX_ROWS_ABS is an absolute ceiling protecting browser render
# + ring depth; RING_ROW_FILL leaves headroom so the Computer's avail>=need gate
# is reached promptly.
MAX_ROWS_ABS        = 4096      # absolute safety ceiling on requested rows
RING_ROW_FILL       = 0.9       # fraction of MAX_TAIL usable for one frame's need

# Allowed sample rates (LTE/5G-NR multiples of 1.92 MHz) and FFT sizes. Incoming
# control values are snapped to the nearest of these so an off-list value can't
# reach arm_spec or trip the calibrated ValueError guard (LV-R2).
RATES_HZ      = (3.84e6, 7.68e6, 15.36e6, 30.72e6)
NFFT_CHOICES  = (256, 512, 1024, 2048, 4096)

# Demo tone plan (P3-2): per-channel CW tone sets of (amplitude, offset_hz),
# cycled when the demo runs with more channels than entries. Entries 0/1 are
# the historical two-channel tone sets, unchanged.
DEMO_TONES = (
    ((0.30,  2.5e6), (0.12, -1.8e6)),
    ((0.20, -3.2e6), (0.08,  4.1e6)),
    ((0.25,  1.1e6), (0.10, -4.6e6)),
    ((0.15, -0.9e6), (0.09,  3.3e6)),
)

# Spectrogram backends. Selection lives in core.state.SPEC_BACKEND.
BACKENDS = {"calibrated", "quicklook", "ssb", "psd"}

# Backends whose STFT runs on the 28-multiple aligned_nfft grid.
CALIBRATED_GRID_BACKENDS = frozenset({"calibrated", "ssb", "psd"})

AVG_BIN_GROUPS = 12
SSB_SUBCARRIER_SPACING = 30e3
SSB_SAMPLE_RATE = 7.68e6
SSB_DISCOVERY_PERIOD = 20e-3
SSB_LO_BANDSTOP = 120e3
SSB_WINDOW = "blackmanharris"
# Ceiling for SSB-grid capture retunes (P2b-5): the top of the radio's LTE-rate
# family. The grid rule (2·fs/scs a 28-multiple) admits no rate above this that
# we would trust the AIR8201B to arm.
SSB_MAX_RATE = 30.72e6

# Default striqt Spectrogram recipe — the exact values calibrated_spectrogram
# hardcoded before P2a-1. These seed the editable analysis params in RadioConfig,
# so behaviour is unchanged until the user edits them from the Analysis panel.
# integration_bandwidth "auto" reproduces the old frequency_resolution ×
# averaging_factor(nfft) coupling (the only value that tracks nfft changes).
DEFAULT_WINDOW             = ("kaiser", 11.88)
DEFAULT_FRACTIONAL_OVERLAP = Fraction(13, 28)
DEFAULT_WINDOW_FILL        = Fraction(15, 28)
DEFAULT_INTEGRATION_BW     = "auto"
DEFAULT_LO_BANDSTOP        = SSB_LO_BANDSTOP
DEFAULT_TRIM_STOPBAND      = False

# Default PSD time_statistic (P2b-3) — reproduces the mean+max trace pair the
# client has always drawn, so behaviour is unchanged until the user edits it.
DEFAULT_PSD_TIME_STATISTIC = ("mean", "max")
