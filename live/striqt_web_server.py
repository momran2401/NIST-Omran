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
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import struct
import sys
import threading
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np


def _ensure_pixi_runtime_libs():
    """
    The AIR-T pixi env ships a newer libstdc++ needed by scipy/striqt waveform
    extensions. Re-exec once with that lib dir in LD_LIBRARY_PATH when needed.
    """
    if os.name != "posix":
        return
    try:
        lib_dir = Path(sys.executable).resolve().parents[1] / "lib"
    except Exception:
        return
    if not (lib_dir / "libstdc++.so.6").exists():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in current.split(":") if p]
    lib_s = str(lib_dir)
    if lib_s in parts:
        return
    os.environ["LD_LIBRARY_PATH"] = ":".join([lib_s] + parts)
    if os.environ.get("RADIO_WEB_LD_REEXEC") == "1":
        return
    os.environ["RADIO_WEB_LD_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_pixi_runtime_libs()

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
    from striqt.analysis import measurements as striqt_measurements
    from striqt.analysis.measurements import shared as striqt_shared
    _ANALYSIS_OK = True
    _ANALYSIS_ERR = None
except Exception as e:
    analysis_specs = None
    striqt_measurements = None
    striqt_shared = None
    _ANALYSIS_OK = False
    _ANALYSIS_ERR = e

# FastAPI
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
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
MAX_TAIL            = 1 << 22   # per-channel ring buffer capacity (4M samples)
DATA_STALE_SEC      = 1.0       # get_latest() returns None if the ring is older

BROADCAST_FPS       = 15        # default max frames/sec to browsers
SCROLL_ROWS         = 12        # rows per frame in Cool (scroll/waterfall) mode
# Rows are bounded by what the IQ ring can actually supply (see max_live_rows()),
# not a flat cap. The old MAX_LIVE_ROWS=300 pinned every long duration to 300 rows
# and made the Duration control inert past ~10-20 ms (P1-5). MAX_ROWS_ABS is an
# absolute ceiling protecting browser render + ring depth; RING_ROW_FILL leaves
# headroom so the Computer's avail>=need gate is reached promptly.
MAX_ROWS_ABS        = 4096      # absolute safety ceiling on requested rows
RING_ROW_FILL       = 0.9       # fraction of MAX_TAIL usable for one frame's need

# Allowed sample rates (LTE/5G-NR multiples of 1.92 MHz) and FFT sizes. Incoming
# control values are snapped to the nearest of these so an off-list value can't
# reach arm_spec or trip the calibrated ValueError guard (LV-R2).
RATES_HZ      = (3.84e6, 7.68e6, 15.36e6, 30.72e6)
NFFT_CHOICES  = (256, 512, 1024, 2048, 4096)


def _snap(value, choices):
    return min(choices, key=lambda c: abs(c - value))


WEB_DIR = Path(__file__).parent / "web"

# Backend: "calibrated" (striqt PSD/ENBW dB spectrogram), "quicklook" (simple
# FFT dB), "psd" (striqt power_spectral_density statistic traces, P2b-3), or
# "ssb" (striqt 5G SSB spectrogram).
SPEC_BACKEND = os.environ.get("SPEC_BACKEND", "calibrated").strip().lower()
BACKENDS = {"calibrated", "quicklook", "ssb", "psd"}
if SPEC_BACKEND not in BACKENDS:
    SPEC_BACKEND = "calibrated"

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


# ---------------------------------------------------------------------------
# HTTP Basic Auth (single shared credential, read from the environment)
# ---------------------------------------------------------------------------
#
# Set RADIO_USER and RADIO_PASS in the environment to put the whole viewer
# (static page, assets, and the /ws WebSocket) behind one shared login. If
# either is unset, auth is DISABLED so --demo / local dev keeps working — a
# loud warning is printed at startup in that case.

_AUTH_USER  = os.environ.get("RADIO_USER") or ""
_AUTH_PASS  = os.environ.get("RADIO_PASS") or ""
AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)
AUTH_REALM   = "striqt live viewer"


def check_basic_auth(auth_header) -> bool:
    """
    Validate an HTTP `Authorization` header against RADIO_USER/RADIO_PASS using
    constant-time comparison. Returns True when auth is disabled (no creds set)
    so local/demo runs are unaffected.

    `auth_header` may be a str (Starlette Request) or bytes (raw ASGI scope).
    """
    if not AUTH_ENABLED:
        return True
    if not auth_header:
        return False
    if isinstance(auth_header, bytes):
        auth_header = auth_header.decode("latin-1")

    scheme, _, param = auth_header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, pw = base64.b64decode(param).decode("utf-8").partition(":")
    except Exception:
        return False

    # Compare BOTH fields every time (no short-circuit) to avoid timing leaks.
    user_ok = secrets.compare_digest(user, _AUTH_USER)
    pw_ok   = secrets.compare_digest(pw, _AUTH_PASS)
    return user_ok and pw_ok


# ---------------------------------------------------------------------------
# Signed session cookie
# ---------------------------------------------------------------------------
#
# Safari and every iOS browser refuse to replay HTTP Basic credentials on the
# WebSocket upgrade handshake, so a Basic-Auth-only gate locks those clients out
# of /ws even after they log in for the page. To fix this, once an HTTP request
# authenticates we hand the browser a signed "radio_auth" cookie; the cookie is
# carried automatically on the subsequent WS handshake and accepted there.
#
# The signing secret is derived from the existing RADIO_USER/RADIO_PASS — no new
# env var — so rotating the password invalidates outstanding tokens.

_SESSION_SECRET = hashlib.sha256(
    (_AUTH_USER + ":" + _AUTH_PASS).encode()
).digest()
SESSION_TTL = 86400


def make_session_token(ttl_seconds: int = SESSION_TTL) -> str:
    """
    Build a signed session token "<exp>.<hex_hmac>" where exp is an int unix
    expiry and hex_hmac = HMAC-SHA256(secret, str(exp)). Unused when auth is
    disabled.
    """
    exp = int(time.time()) + ttl_seconds
    mac = hmac.new(_SESSION_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def verify_session_token(token) -> bool:
    """
    Validate a "<exp>.<hex_hmac>" session token: recompute the HMAC with a
    constant-time comparison and confirm the expiry is still in the future.
    Returns False on any malformed input. A no-op (unused) when auth is disabled.
    """
    if not token:
        return False
    if isinstance(token, bytes):
        token = token.decode("latin-1")

    exp_str, _, mac = token.partition(".")
    if not mac:
        return False
    try:
        exp = int(exp_str)
    except ValueError:
        return False

    expected = hmac.new(
        _SESSION_SECRET, exp_str.encode(), hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(mac, expected):
        return False
    return exp > int(time.time())


def _session_cookie_from_scope(scope) -> bool:
    """
    Parse the request's Cookie header from a raw ASGI scope and return True when
    a "radio_auth" cookie is present and passes verify_session_token.
    """
    headers = dict(scope.get("headers") or [])
    raw_cookie = headers.get(b"cookie")
    if not raw_cookie:
        return False
    cookie_str = raw_cookie.decode("latin-1")
    for part in cookie_str.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "radio_auth":
            return verify_session_token(value)
    return False


class BasicAuthMiddleware:
    """
    Pure-ASGI middleware that gates EVERY http and websocket request behind a
    single shared Basic-Auth credential. Mounted static files and the /ws
    endpoint are all covered because it wraps the entire app.

    On failure:
      - http      → 401 + `WWW-Authenticate: Basic` so the browser shows the
                    standard username/password popup.
      - websocket → the handshake is rejected (browsers replay the page's
                    cached Basic credentials on the WS upgrade, so a viewer that
                    authenticated for the page connects fine; anyone else is
                    refused before `accept()`).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _set_cookie_send(scope, send):
        """
        Wrap `send` to append a Set-Cookie header carrying a fresh session token
        on the HTTP response start. Only the success path uses this, so the
        cookie is never attached to a 401. The `Secure` attribute is omitted over
        plain HTTP (LAN) so Safari/iOS — which refuse to store a Secure cookie
        without TLS and won't replay Basic on the WS upgrade — can still reach /ws
        (LV-R8). HttpOnly and SameSite=Lax are always set.
        """
        headers_in = dict(scope.get("headers") or [])
        is_https = (
            scope.get("scheme") == "https"
            or headers_in.get(b"x-forwarded-proto") == b"https"
        )
        secure_attr = "Secure; " if is_https else ""

        async def wrapped(message):
            if message["type"] == "http.response.start":
                cookie = (
                    f"radio_auth={make_session_token()}; Path=/; HttpOnly; "
                    f"{secure_attr}SameSite=Lax; Max-Age={SESSION_TTL}"
                )
                headers = list(message.get("headers") or [])
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    async def __call__(self, scope, receive, send):
        if not AUTH_ENABLED or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        # Authenticated via Basic Auth header OR a valid signed session cookie.
        # The cookie path lets browsers that drop Basic creds on the WS upgrade
        # (Safari / all iOS browsers) still connect to /ws after logging in.
        if check_basic_auth(headers.get(b"authorization")) or _session_cookie_from_scope(scope):
            if scope["type"] == "http":
                # Refresh the session cookie so the browser carries it on the
                # WS handshake. Never set it on websocket scopes.
                await self.app(scope, receive, self._set_cookie_send(scope, send))
            else:
                await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Reject the upgrade before accept(); no credentials means no frames.
            await send({"type": "websocket.close", "code": 1008})
            return

        body = b"401 Unauthorized"
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", f'Basic realm="{AUTH_REALM}"'.encode("latin-1")),
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class NoCacheMiddleware:
    """
    Pure-ASGI middleware that stamps no-store cache headers on every HTTP
    response so browsers always refetch the page and assets. WebSocket and
    other scope types pass straight through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for (k, v) in message.get("headers") or []
                    if k.lower() not in (b"cache-control", b"expires", b"pragma")
                ]
                headers.append((b"cache-control", b"no-store, max-age=0"))
                headers.append((b"pragma", b"no-cache"))
                headers.append((b"expires", b"0"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
    backend:     str   = SPEC_BACKEND
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
        self._cfg   = RadioConfig(backend=SPEC_BACKEND)
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

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

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
        try:
            if update.get("sample_rate") is not None:
                eff.sample_rate = float(
                    max(1e6, min(_snap(float(update["sample_rate"]), RATES_HZ), 125e6))
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
            for key in passthru_capture:
                if capture.get(key) is not None:
                    mapped[key] = capture[key]
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

        if "source" in update and isinstance(update["source"], dict):
            reconnect = sorted(k for k, v in update["source"].items() if v is not None and k not in {
                "receive_retries", "adc_overload_limit", "if_overload_limit", "gapless",
            })
            if reconnect:
                print(f"[config] source changes require reconnect: {reconnect}")

        valid = {
            "center", "sample_rate", "gain", "nfft", "rows", "backend", "lo_null",
            "analysis_bandwidth", "lo_shift", "host_resample", "backend_sample_rate",
            "duration",
        } | ANALYSIS_CFG_KEYS
        changes = []
        with self._lock:
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
                    value = float(max(300e6, min(value, 6e9)))
                elif key == "sample_rate":
                    value = float(value)
                    if not (eff_backend == "ssb" and ssb_grid_compatible(value, eff_scs)):
                        value = float(_snap(value, RATES_HZ))
                    value = float(max(1e6, min(value, 125e6)))
                elif key == "gain":
                    value = float(max(-60.0, min(value, 10.0)))
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
        # Print outside the lock to avoid I/O inside a mutex
        for key, old, value in changes:
            print(f"[config] {key}: {old} -> {value}")
        for entry in rounded:
            print(f"[config] rounded {entry['field']}: "
                  f"{entry['requested']} -> {entry['used']} ({entry['reason']})")
        for entry in rejected:
            print(f"[config] rejected {entry['field']}: {entry['reason']}")
        return {
            "applied":   [k for k, _, _ in changes],
            "ignored":   ignored,
            "reconnect": reconnect,
            "rounded":   rounded,
            "rejected":  rejected,
        }

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
    # port stays fixed at CHANNELS — the two-waterfall UI depends on both RX ports
    # (P1-2). The other four knobs are now driven by the schema editor / cfg.
    # When cfg.duration owns the time axis (P2a-4) it drives the armed capture
    # duration honestly; snapped to an integer sample count because striqt's
    # Capture validation requires duration·sample_rate to be an integer.
    duration = cfg.duration if cfg.duration > 0 else cfg.rows * cfg.nfft / cfg.sample_rate
    duration = max(duration, 1e-3)
    duration = round(duration * cfg.sample_rate) / cfg.sample_rate
    return specs.SoapyCapture(
        port=CHANNELS,
        center_frequency=cfg.center,
        gain=tuple([cfg.gain] * len(CHANNELS)),
        duration=duration,
        sample_rate=cfg.sample_rate,
        backend_sample_rate=(cfg.backend_sample_rate or cfg.sample_rate),
        host_resample=cfg.host_resample,
        analysis_bandwidth=cfg.analysis_bandwidth,
        lo_shift=cfg.lo_shift,
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
    window = np.hanning(nfft).astype(np.float32)
    x      = x * window[None, None, :]
    spec   = np.fft.fftshift(np.fft.fft(x, axis=-1), axes=-1)
    # Normalize by window power (proper PSD estimate)
    power  = (np.abs(spec) ** 2) / max(float(np.sum(window ** 2)), 1.0)
    spg = (10.0 * np.log10(power + 1e-20)).astype(np.float32)
    # Quicklook is a plain fftshifted per-bin FFT: fft_nfft = nfft, no averaging,
    # non-overlapping rows (hop = nfft).
    return spg, {"fft_nfft": int(nfft), "bin_avg": 1, "hop_size": int(nfft)}


def analysis_hop(nfft: int, fractional_overlap=DEFAULT_FRACTIONAL_OVERLAP) -> int:
    """
    Samples the STFT advances per displayed row: nfft − noverlap, where noverlap
    is computed exactly as striqt does (`round(fractional_overlap * nfft)` on the
    Fraction). At the default 13/28 overlap this is the familiar nfft·15/28.
    """
    nfft = int(nfft)
    noverlap = round(Fraction(fractional_overlap) * nfft)
    return max(1, nfft - int(noverlap))


def resolve_integration_bandwidth(value, nfft: int, sample_rate: float):
    """
    Map the cfg integration_bandwidth ("auto" | None | Hz) to the value striqt
    receives. "auto" reproduces the pre-P2a behaviour: frequency_resolution ×
    averaging_factor(nfft), the only choice that tracks nfft changes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return (sample_rate / float(nfft)) * averaging_factor(nfft)   # "auto"
    return float(value)


def make_analysis_spec(cfg: "RadioConfig", nfft: int, sample_rate: float):
    """Build the striqt Spectrogram spec from cfg's analysis params (P2a-1)."""
    frequency_resolution = float(sample_rate) / float(nfft)
    integration = resolve_integration_bandwidth(
        cfg.integration_bandwidth, nfft, sample_rate
    )
    lo = cfg.lo_bandstop
    aperture = cfg.time_aperture
    return analysis_specs.Spectrogram(
        window=cfg.window,
        frequency_resolution=frequency_resolution,
        fractional_overlap=Fraction(cfg.fractional_overlap),
        window_fill=Fraction(cfg.window_fill),
        integration_bandwidth=integration,
        trim_stopband=bool(cfg.trim_stopband),
        lo_bandstop=(float(lo) if lo else None),
        time_aperture=(float(aperture) if aperture else None),
    )


def time_aperture_bins(cfg: "RadioConfig", hop: int) -> int:
    """STFT rows striqt averages into one output row for cfg.time_aperture
    (1 = no time averaging). Mirrors striqt's round(time_aperture/hop_period)."""
    if not cfg.time_aperture:
        return 1
    return max(1, round(float(cfg.time_aperture) * float(cfg.sample_rate) / max(1, hop)))


def calibrated_spectrogram(samples: np.ndarray, cfg: "RadioConfig") -> tuple:
    """
    striqt-calibrated PSD spectrogram driven by cfg's analysis params (P2a-1) —
    window, overlap, fill, integration bandwidth, LO bandstop, stopband trim.
    Returns (blocks, meta) — blocks (channels, rows, bins) float32, meta
    {fft_nfft, bin_avg, hop_size, freqs_hz_f0, freqs_hz_step}.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"calibrated backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    rows        = int(cfg.rows)
    sample_rate = float(cfg.sample_rate)
    nfft        = aligned_nfft(cfg.nfft)
    hop         = analysis_hop(nfft, cfg.fractional_overlap)
    # Right-size to exactly `rows` STFT rows under the configured overlap, rather
    # than computing extra rows and discarding all but the last `rows` (LV-W2).
    needed = calibrated_sample_count(nfft, rows, hop)
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    # Carry the real analysis_bandwidth so trim_stopband=True has something to
    # trim to; with the default trim=False / bandwidth=inf this is inert.
    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=needed / sample_rate,
        analysis_bandwidth=float(cfg.analysis_bandwidth),
    )
    spec = make_analysis_spec(cfg, nfft, sample_rate)
    integration = resolve_integration_bandwidth(
        cfg.integration_bandwidth, nfft, sample_rate
    )
    average_bins = (
        1 if integration is None
        else max(1, round(integration / (sample_rate / nfft)))
    )
    striqt_shared.spectrogram_cache.clear()
    spg, _ = striqt_shared.evaluate_spectrogram(
        samples, capture, spec, dtype="float32", dB=True
    )
    # time_aperture averages time_bins STFT rows into one output row (P2b-2):
    # fewer rows come back, and each spans time_bins hops of signal. Fit to the
    # honest averaged count and disclose the widened hop so the client's time
    # labels stay exact.
    time_bins = time_aperture_bins(cfg, hop)
    rows_out  = max(1, rows // time_bins) if time_bins > 1 else rows
    blocks = fit_display_rows(
        np.asarray(spg, dtype=np.float32), rows_out,
        bin_avg=average_bins, fft_nfft=nfft, sample_rate=sample_rate,
        lo_null=cfg.lo_null, lo_bandstop=cfg.lo_bandstop,
    )
    meta = {"fft_nfft": int(nfft), "bin_avg": int(average_bins),
            "hop_size": int(hop * time_bins)}
    # Ship striqt's own frequency coordinates so the header axis is exact for ANY
    # analysis params (trim/averaging change the bin grid in ways the header's
    # symmetric-about-DC fallback can only approximate). Additive: build_header
    # uses these when present, keeping the LV-F1 axis contract.
    try:
        freqs = striqt_shared.spectrogram_freqs(capture, spec)
        freqs = np.asarray(freqs, dtype=np.float64)
        if freqs.size >= 2:
            meta["freqs_hz_f0"]   = float(freqs[0])
            meta["freqs_hz_step"] = float(freqs[1] - freqs[0])
    except Exception:
        pass   # fall back to build_header's symmetric axis
    return blocks, meta


def make_psd_kwargs(cfg: "RadioConfig", nfft: int, sample_rate: float) -> dict:
    """Keyword arguments for striqt's power_spectral_density from cfg's PSD
    param block (P2b-3) — the exact spec the live compute and the tier-2
    scratch validator both use."""
    integration = resolve_integration_bandwidth(
        cfg.psd_integration_bandwidth, nfft, sample_rate
    )
    lo = cfg.psd_lo_bandstop
    return dict(
        window=cfg.psd_window,
        frequency_resolution=float(sample_rate) / float(nfft),
        fractional_overlap=Fraction(cfg.psd_fractional_overlap),
        window_fill=Fraction(cfg.psd_window_fill),
        integration_bandwidth=integration,
        trim_stopband=bool(cfg.psd_trim_stopband),
        lo_bandstop=(float(lo) if lo else None),
        time_statistic=tuple(cfg.psd_time_statistic),
    )


def psd_traces(samples: np.ndarray, cfg: "RadioConfig") -> tuple:
    """
    striqt power_spectral_density backend (P2b-3): Welch-method statistic
    traces over the frame's time span, one row per configured time_statistic
    entry. Returns (blocks, meta) — blocks (channels, n_statistics, bins)
    float32 dB; meta discloses the statistic list (psd_stats) and the true
    integrated span (time_span_ms) alongside the usual axis params.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"PSD backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    rows        = int(cfg.rows)
    sample_rate = float(cfg.sample_rate)
    nfft        = aligned_nfft(cfg.nfft)
    hop         = analysis_hop(nfft, cfg.psd_fractional_overlap)
    needed      = calibrated_sample_count(nfft, rows, hop)
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=needed / sample_rate,
        analysis_bandwidth=float(cfg.analysis_bandwidth),
    )
    kwargs = make_psd_kwargs(cfg, nfft, sample_rate)
    integration = kwargs["integration_bandwidth"]
    average_bins = (
        1 if integration is None
        else max(1, round(integration / (sample_rate / nfft)))
    )
    psd, _ = striqt_measurements.power_spectral_density(
        samples, capture, as_xarray=False, **kwargs
    )
    psd = np.asarray(psd, dtype=np.float32)   # (channels, n_stats, bins), dB
    blocks = fit_display_rows(
        psd, psd.shape[1],
        bin_avg=average_bins, fft_nfft=nfft, sample_rate=sample_rate,
        lo_null=cfg.lo_null, lo_bandstop=cfg.psd_lo_bandstop,
    )
    meta = {
        "fft_nfft": int(nfft), "bin_avg": int(average_bins), "hop_size": int(hop),
        "psd_stats": [str(s) for s in cfg.psd_time_statistic],
        "time_span_ms": 1e3 * needed / sample_rate,
    }
    # Exact striqt frequency coordinates, same contract as the calibrated path.
    try:
        spg_kwargs = {k: v for k, v in kwargs.items() if k != "time_statistic"}
        spg_spec = analysis_specs.Spectrogram(**spg_kwargs)
        freqs = np.asarray(striqt_shared.spectrogram_freqs(capture, spg_spec),
                           dtype=np.float64)
        if freqs.size >= 2:
            meta["freqs_hz_f0"]   = float(freqs[0])
            meta["freqs_hz_step"] = float(freqs[1] - freqs[0])
    except Exception:
        pass   # fall back to build_header's symmetric axis
    return blocks, meta


def ssb_spectrogram(samples: np.ndarray, cfg: "RadioConfig") -> tuple:
    """
    True symbol-aligned 5G SSB spectrogram (P2b-5): striqt's
    cellular_5g_ssb_spectrogram driven by cfg's SSB param block, one row per
    OFDM symbol of each burst set, flattened (blocks·symbols) to the dashboard
    row contract. Only reachable on the SSB grid (compute_blocks pre-checks
    and runs calibrated honestly otherwise); grid errors here propagate to the
    tier-3 backstop rather than silently substituting another analysis.
    Returns (blocks, meta).
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"SSB backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    sample_rate = float(cfg.sample_rate)
    geo = ssb_geometry(cfg)   # raises off-grid — backstop-visible, never phantom

    # Trim to whole burst sets: striqt keeps the first symbol_rows of every
    # discovery period, and its blockwise reshape needs the kept row count to
    # be an exact multiple of symbol_rows.
    q = 1 + max(0, (samples.shape[1] - ssb_block_samples(geo, 1))
                // (geo["discovery_rows"] * geo["hop"]))
    q = min(q, ssb_max_blocks(cfg, geo))
    needed = ssb_block_samples(geo, q)
    if samples.shape[1] < needed:
        pad = np.zeros((samples.shape[0], needed - samples.shape[1]), dtype=np.complex64)
        samples = np.concatenate([samples, pad], axis=1)
    else:
        samples = samples[:, -needed:]

    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=needed / sample_rate,
        analysis_bandwidth=float("inf"),
    )
    kwargs = make_ssb_kwargs(cfg)
    spg, _ = striqt_measurements.cellular_5g_ssb_spectrogram(
        samples, capture, as_xarray=False, **kwargs
    )
    spg = np.asarray(spg, dtype=np.float32)
    if spg.ndim == 4:
        spg = spg.reshape(spg.shape[0], spg.shape[1] * spg.shape[2], spg.shape[3])

    # Axis disclosure: the STFT runs nfft = 2·fs/scs at scs/2 resolution and
    # integrates pairs of bins (integration_bandwidth = scs), so bin_avg = 2.
    # The display-side LO null assumes DC sits at the band center, which only
    # holds at zero frequency_offset — striqt's own lo_bandstop (NaN-nulled,
    # then scrubbed) covers the true LO region in the offset case.
    blocks = fit_display_rows(
        spg, spg.shape[1],
        bin_avg=2, fft_nfft=geo["nfft"], sample_rate=sample_rate,
        lo_null=(cfg.lo_null and not cfg.ssb_frequency_offset),
        lo_bandstop=cfg.ssb_lo_bandstop,
    )
    # One display row = one OFDM symbol (hop samples). Rows across burst-set
    # boundaries jump a discovery period — the view is a burst montage, so the
    # hop labels the signal time actually shown.
    meta = {"fft_nfft": int(geo["nfft"]), "bin_avg": 2, "hop_size": int(geo["hop"])}
    # Exact striqt frequency coordinates for the truncated, offset SSB band.
    # The coordinate factory lives in a private module whose path may differ in
    # the installed striqt build — fall back to the symmetric header axis then.
    try:
        from striqt.analysis.measurements import (
            _cellular_5g_ssb_spectrogram as _ssb_mod,
        )
        spec_obj = analysis_specs.Cellular5GNRSSBSpectrogram(**kwargs)
        freqs = np.asarray(
            _ssb_mod.cellular_ssb_baseband_frequency(capture, spec_obj),
            dtype=np.float64,
        )
        if freqs.size >= 2:
            meta["freqs_hz_f0"]   = float(freqs[0])
            meta["freqs_hz_step"] = float(freqs[1] - freqs[0])
    except Exception:
        pass
    return blocks, meta


# Snap the requested FFT size to a smooth multiple of 28 that is ALSO divisible
# by 12 (so averaging_factor returns 12 consistently) and 7-smooth (2^a·3^b·7 —
# fast scipy/pocketfft sizes). Avoids the slow non-power-of-2 sizes the old
# round(n/28)·28 produced (1024→1036=2^2·7·37, 2048→2044=2^2·7·73), which drove
# the calibrated cadence and made the bin-averaging factor non-monotonic.
ALIGNED_NFFTS = (252, 504, 1008, 2016, 4032)   # 28·{9,18,36,72,144}


def aligned_nfft(nfft: int) -> int:
    return min(ALIGNED_NFFTS, key=lambda n: abs(n - int(nfft)))


def averaging_factor(nfft: int) -> int:
    for factor in range(min(AVG_BIN_GROUPS, nfft), 1, -1):
        if nfft % factor == 0:
            return factor
    return 1


def calibrated_sample_count(nfft: int, rows: int, hop=None) -> int:
    """
    Samples needed to produce exactly `rows` STFT rows under the configured
    overlap. Each displayed row advances the STFT by `hop` samples (nfft·15/28 at
    the default 13/28 overlap), so rows·hop + (nfft-hop) samples suffice — instead
    of the ~1.87× that rows·nfft would compute and then discard (see
    AUDIT_REPORT.md LV-W2). The count reproduces striqt's own row formula
    int((nfft/hop)·(N/nfft-1)+1) == rows for any hop that divides its terms.
    """
    nfft = int(nfft)
    if hop is None:
        hop = (nfft * 15) // 28
    hop = max(1, int(hop))
    return int(rows * hop + (nfft - hop))


def backend_overlap(cfg: RadioConfig):
    """The fractional_overlap the executing backend's STFT uses (P2b-3): the
    PSD backend runs its own param block; calibrated/ssb share the spectrogram
    block."""
    return cfg.psd_fractional_overlap if cfg.backend == "psd" else cfg.fractional_overlap


def row_hop(cfg: RadioConfig) -> int:
    """Samples of signal one display row spans for cfg's backend (P2a-1). For
    the PSD backend a "row" is one STFT row feeding the statistics, so the
    duration→rows mapping controls the integrated time span (P2b-3). For the
    SSB view, symbol_rows display rows come from every discovery period, so
    the duration→rows mapping picks the burst count (P2b-5)."""
    if cfg.backend == "ssb" and ssb_grid_compatible(cfg.sample_rate,
                                                    cfg.ssb_subcarrier_spacing):
        geo = ssb_geometry(cfg)
        return max(1, round(geo["discovery_rows"] * geo["hop"] / geo["symbol_rows"]))
    if cfg.backend in CALIBRATED_GRID_BACKENDS:
        nfft = aligned_nfft(cfg.nfft)
        return analysis_hop(nfft, backend_overlap(cfg))
    return max(1, int(cfg.nfft))


def max_live_rows(cfg: RadioConfig) -> int:
    """
    Largest number of display rows the IQ ring can actually supply for `cfg`'s
    backend and FFT size (P1-5). Replaces the old flat 300-row clamp, which pinned
    every long duration to 300 rows and made the Duration control inert past
    ~10-20 ms. The bound is honest, not cosmetic: `samples_needed(rows)` must stay
    within `RING_ROW_FILL·MAX_TAIL` so the Computer's `avail >= need` gate is
    reached promptly (otherwise a too-large request would starve the display), and
    never exceed the absolute `MAX_ROWS_ABS` ceiling. A longer duration therefore
    renders more rows (and, on the calibrated path, costs more FFTs → fps may fall,
    which is expected and left honest — the cap protects the radio, not the fps).
    """
    limit = int(MAX_TAIL * RING_ROW_FILL)
    if cfg.backend == "ssb" and ssb_grid_compatible(cfg.sample_rate,
                                                    cfg.ssb_subcarrier_spacing):
        geo = ssb_geometry(cfg)
        rows = ssb_max_blocks(cfg, geo) * geo["symbol_rows"]
    elif cfg.backend in CALIBRATED_GRID_BACKENDS:
        nfft = aligned_nfft(cfg.nfft)
        hop  = analysis_hop(nfft, backend_overlap(cfg))
        rows = (limit - (nfft - hop)) // hop
    else:
        rows = limit // max(1, int(cfg.nfft))
    return int(max(1, min(rows, MAX_ROWS_ABS)))


def fit_display_rows(
    spg: np.ndarray,
    rows: int,
    *,
    bin_avg: int = 1,
    fft_nfft=None,
    sample_rate=None,
    lo_null: bool = True,
    lo_bandstop=SSB_LO_BANDSTOP,
) -> np.ndarray:
    """Crop/pad a striqt spectrogram to the dashboard row contract."""
    spg = np.asarray(spg, dtype=np.float32)
    if spg.ndim != 3:
        raise RuntimeError(f"spectrogram shape {spg.shape} is not channels x rows x bins")
    if spg.shape[1] != rows:
        spg = spg[:, -rows:, :]
        if spg.shape[1] < rows:
            fill = float(np.nanmin(spg)) if spg.size > 0 else -200.0
            pad = np.full(
                (spg.shape[0], rows - spg.shape[1], spg.shape[2]),
                fill,
                dtype=np.float32,
            )
            spg = np.concatenate([pad, spg], axis=1)

    # Null the LO leakage region, sized to the configured striqt bandstop instead
    # of a fixed ±2 bins (which hid up to ~3.7 MHz of real spectrum at coarse
    # FFTs). Optional via the lo_null flag so the center can be revealed (LV-F8).
    # With lo_bandstop None ("none" in the Analysis panel) there is no bandstop to
    # size, so the display null is skipped too — the raw DC leak shows, honestly.
    if lo_null and lo_bandstop and spg.shape[2] >= 3 and fft_nfft and sample_rate:
        step = max(1, bin_avg) * float(sample_rate) / float(fft_nfft)   # Hz per averaged bin
        half = max(1, math.ceil((float(lo_bandstop) / 2) / step))
        c = spg.shape[-1] // 2
        lo = max(0, c - half)
        hi = min(spg.shape[-1], c + half + 1)
        spg[:, :, lo:hi] = np.nanmin(spg, axis=-1, keepdims=True)

    # ALWAYS scrub remaining NaNs (striqt's null_lo leaves an all-NaN DC group) to
    # the per-row min so the quantizer and client never see NaN garbage (LV-F8/R4).
    if np.isnan(spg).any():
        row_min = np.nanmin(np.where(np.isnan(spg), np.float32(np.inf), spg), axis=-1, keepdims=True)
        row_min = np.where(np.isfinite(row_min), row_min, np.float32(-200.0))
        spg = np.where(np.isnan(spg), row_min, spg).astype(np.float32)
    return spg


def samples_needed(cfg: RadioConfig) -> int:
    if cfg.backend == "ssb" and ssb_grid_compatible(cfg.sample_rate,
                                                    cfg.ssb_subcarrier_spacing):
        # Whole burst sets only (P2b-5): striqt keeps symbol_rows rows per
        # discovery period and reshapes blockwise, so the supplied span must
        # end exactly at a burst boundary. cfg.rows (duration-derived at
        # discovery_periodicity/symbol_rows per row) picks the burst count.
        geo = ssb_geometry(cfg)
        q = max(1, round(cfg.rows / geo["symbol_rows"]))
        q = min(q, ssb_max_blocks(cfg, geo))
        return ssb_block_samples(geo, q)
    if cfg.backend in CALIBRATED_GRID_BACKENDS:
        # Overlapped STFT: only rows·hop + (nfft-hop) samples are needed to
        # produce cfg.rows display rows (LV-W2), not the full nfft·rows.
        nfft = aligned_nfft(cfg.nfft)
        return calibrated_sample_count(
            nfft, cfg.rows, analysis_hop(nfft, backend_overlap(cfg))
        )
    return int(cfg.nfft * cfg.rows)


def ssb_grid_compatible(sample_rate: float,
                        subcarrier_spacing: float = SSB_SUBCARRIER_SPACING) -> bool:
    """
    True when the capture rate supports the symbol-aligned SSB view at this
    subcarrier spacing: the SSB spectrogram runs at frequency_resolution scs/2
    with window_fill 15/28, so nfft = 2·fs/scs must be an integer AND a
    multiple of 28 ((1-15/28)·nfft integrality — the audit's "30 kHz grid").
    Equivalently: fs must be a multiple of 14·scs.
    """
    ratio = 2.0 * float(sample_rate) / float(subcarrier_spacing)
    nfft = round(ratio)
    return nfft >= 28 and abs(ratio - nfft) < 1e-6 and nfft % 28 == 0


def ssb_compatible_rate(sample_rate: float, subcarrier_spacing: float):
    """
    Nearest capture sample rate that satisfies the SSB grid for this
    subcarrier spacing — the retune target when the SSB view is selected at an
    incompatible rate (P2b-5). Candidates are multiples of 14·scs, preferring
    those also on the radio's 1.92 MHz LTE-family grid (most plausibly armable
    — e.g. 13.44 MS/s = 7·1.92 MHz for all standard SCS), clamped to
    SSB_MAX_RATE. Returns None when no such rate exists (scs too large).
    """
    base = 14.0 * float(subcarrier_spacing)
    if not (base > 0 and math.isfinite(base)) or base > SSB_MAX_RATE:
        return None
    step = base
    if abs(base - round(base)) < 1e-6:
        g = math.gcd(int(round(base)), 1920000)
        lcm = int(round(base)) * (1920000 // g)
        if lcm <= SSB_MAX_RATE:
            step = float(lcm)
    k = max(1, round(float(sample_rate) / step))
    while k > 1 and k * step > SSB_MAX_RATE:
        k -= 1
    rate = k * step
    return float(rate) if rate <= SSB_MAX_RATE else None


def make_ssb_kwargs(cfg: "RadioConfig") -> dict:
    """Keyword arguments for striqt's cellular_5g_ssb_spectrogram from cfg's
    SSB param block (P2b-5) — shared by the live compute and the tier-2
    scratch validator."""
    return dict(
        subcarrier_spacing=float(cfg.ssb_subcarrier_spacing),
        # striqt truncates the frequency axis to this output rate; it can never
        # exceed the sampled span.
        sample_rate=min(float(cfg.ssb_sample_rate), float(cfg.sample_rate)),
        discovery_periodicity=float(cfg.ssb_discovery_periodicity),
        frequency_offset=float(cfg.ssb_frequency_offset),
        max_block_count=(int(cfg.ssb_max_block_count)
                         if cfg.ssb_max_block_count else None),
        window=cfg.ssb_window,
        lo_bandstop=(float(cfg.ssb_lo_bandstop) if cfg.ssb_lo_bandstop else None),
    )


def ssb_geometry(cfg: "RadioConfig", sample_rate=None) -> dict:
    """
    Row/sample geometry of the symbol-aligned SSB spectrogram (P2b-5). striqt
    runs the STFT at frequency_resolution scs/2 with a 13/28 overlap, making
    one row per OFDM symbol; each discovery period contributes the first
    `symbol_rows` symbols (one burst set, always 2 ms of signal).
      nfft:           STFT size 2·fs/scs
      hop:            samples per symbol row (nfft·15/28)
      symbol_rows:    rows kept per burst set (28·scs/15e3)
      discovery_rows: rows spanning one discovery period
    Raises ValueError when the rate/scs combination is off the grid.
    """
    fs  = float(sample_rate if sample_rate is not None else cfg.sample_rate)
    scs = float(cfg.ssb_subcarrier_spacing)
    if not ssb_grid_compatible(fs, scs):
        raise ValueError(
            f"sample rate {fs/1e6:g} MS/s is not on the SSB grid for "
            f"subcarrier spacing {scs/1e3:g} kHz (2·fs/scs must be a 28-multiple)"
        )
    nfft = round(2.0 * fs / scs)
    hop  = (nfft * 15) // 28
    symbol_rows = max(1, round(28.0 * scs / 15e3))
    discovery_rows = max(symbol_rows,
                         round(float(cfg.ssb_discovery_periodicity) * fs / hop))
    return {"nfft": nfft, "hop": hop,
            "symbol_rows": symbol_rows, "discovery_rows": discovery_rows}


def ssb_block_samples(geo: dict, blocks: int) -> int:
    """Samples that yield exactly `blocks` complete burst sets: (q-1) full
    discovery periods plus the final burst's symbol rows, plus STFT tail."""
    q = max(1, int(blocks))
    return int((q - 1) * geo["discovery_rows"] * geo["hop"]
               + geo["symbol_rows"] * geo["hop"]
               + (geo["nfft"] - geo["hop"]))


def ssb_max_blocks(cfg: "RadioConfig", geo: dict) -> int:
    """Most burst sets one frame can hold: bounded by the ring (same
    RING_ROW_FILL budget as max_live_rows) and cfg.ssb_max_block_count."""
    limit = int(MAX_TAIL * RING_ROW_FILL)
    per_extra = geo["discovery_rows"] * geo["hop"]
    q = 1 + max(0, (limit - ssb_block_samples(geo, 1)) // max(1, per_extra))
    if cfg.ssb_max_block_count:
        q = min(q, max(1, int(cfg.ssb_max_block_count)))
    return int(max(1, q))


def compute_blocks(samples: np.ndarray, cfg: RadioConfig):
    """
    Dispatch to the configured backend.
    Returns (blocks, meta): blocks is (channels, rows, bins) float32; meta carries
    the per-frame axis parameters (fft_nfft, bin_avg) and the executed backend,
    used by build_header to ship an honest frame header (LV-F1/F2).
    """
    requested = cfg.backend
    if requested == "ssb" and not ssb_grid_compatible(cfg.sample_rate,
                                                      cfg.ssb_subcarrier_spacing):
        # SSB needs the capture rate on the 14·scs grid. Selecting SSB retunes
        # to a compatible rate (P2b-5), so this only covers the transient (or a
        # rate the retune could not reach): run calibrated and REPORT it via
        # backend/backend_requested — never a phantom SSB view (LV-F2).
        blocks, meta = calibrated_spectrogram(samples, cfg)
        executed = "calibrated"
    elif requested == "calibrated":
        blocks, meta = calibrated_spectrogram(samples, cfg)
        executed = "calibrated"
    elif requested == "psd":
        blocks, meta = psd_traces(samples, cfg)
        executed = "psd"
    elif requested == "ssb":
        blocks, meta = ssb_spectrogram(samples, cfg)
        executed = "ssb"
    else:
        blocks, meta = db_spectrogram(samples, cfg.nfft, cfg.rows)
        executed = "quicklook"
    meta["backend"] = executed
    meta["backend_requested"] = requested
    return blocks, meta


def build_header(cfg: RadioConfig, blocks: list, meta: dict, demo: bool = False) -> dict:
    """
    Assemble the frame header from cfg + the per-frame backend meta. Ships the
    TRUE frequency axis (freqs_hz_f0/freqs_hz_step) and the executed backend so
    the client never has to guess it (LV-F1/F2). fft_nfft/bin_avg disclose the
    real FFT size and bin-averaging behind the reported `nfft` bin count.
    """
    first = np.asarray(blocks[0], dtype=np.float32)
    rows, bins = first.shape
    fs = float(cfg.sample_rate)
    executed = str(meta.get("backend", cfg.backend))
    fft_nfft = int(meta.get("fft_nfft", bins)) or int(bins)
    bin_avg  = int(meta.get("bin_avg", 1)) or 1

    # Frequency axis. Prefer the exact coordinates striqt computed for the
    # executed spec (calibrated path, P2a-1) — correct for any overlap/averaging/
    # trim combination. Fallback: quicklook is a plain fftshifted FFT (bin 0 =
    # -fs/2); the calibrated/ssb path DC-centers bin_avg-wide averaged groups, so
    # their centers are symmetric about DC with step = bin_avg*fs/fft_nfft.
    if meta.get("freqs_hz_f0") is not None and meta.get("freqs_hz_step") is not None:
        f0   = float(meta["freqs_hz_f0"])
        step = float(meta["freqs_hz_step"])
    else:
        step = bin_avg * fs / fft_nfft
        if executed == "quicklook":
            f0 = -fs / 2.0
        else:
            f0 = -(bins - 1) / 2.0 * step

    header = {
        "center":        float(cfg.center),
        "fs":            fs,
        "gain":          float(cfg.gain),
        "nfft":          int(bins),
        "rows":          int(rows),
        "shape":         [int(rows), int(bins)],
        "channels":      list(CHANNELS),
        "backend":       executed,
        "fft_nfft":      fft_nfft,
        "bin_avg":       bin_avg,
        "freqs_hz_f0":   float(f0),
        "freqs_hz_step": float(step),
        # Samples of signal one display row spans (additive, P2a-1). Lets the
        # client label the time axis exactly for any fractional_overlap instead
        # of assuming the 15/28 hop.
        "hop_size":      int(meta.get("hop_size", fft_nfft) or fft_nfft),
        "time":          time.time(),
    }
    # PSD-backend extras (P2b-3, additive): the statistic behind each block row
    # and the true integrated time span (block rows are statistics, not time,
    # so the hop-based window label doesn't apply).
    if meta.get("psd_stats") is not None:
        header["psd_stats"] = list(meta["psd_stats"])
    if meta.get("time_span_ms") is not None:
        header["time_span_ms"] = float(meta["time_span_ms"])
    requested = str(meta.get("backend_requested", executed))
    if requested != executed:
        header["backend_requested"] = requested
    if demo:
        header["demo"] = True
    return header


# ---------------------------------------------------------------------------
# Acquirer thread (real AIR8201B hardware)
# ---------------------------------------------------------------------------

class Acquirer(threading.Thread):
    """
    Drains raw IQ from the AIR8201B into a per-channel ring buffer in a tight
    read loop (no spectrogram math here). The separate Computer thread pulls the
    latest samples via get_latest(), computes blocks, and calls publish(); the
    broadcaster reads latest() at BROADCAST_FPS to fan out to all clients.

    Keeping compute off this loop is what prevents DMA overflow: while a frame is
    being computed, _read_stream keeps draining the radio. This mirrors the
    Acquirer/LocalReceiver split in striqt_standalone.py.
    """

    def __init__(self, shared: SharedConfig):
        super().__init__(daemon=True)
        self.shared       = shared
        self.source       = None
        self.stream_mtu   = None
        self.stream_ports = CHANNELS

        # Latest computed-frame slot (written by Computer, read by broadcaster).
        self._pub_lock        = threading.Lock()
        self._latest_header   = None
        self._latest_blocks   = None

        # Raw IQ ring buffer (complex64). One write pointer + sample count shared
        # across channels since every read fills all channels equally.
        self._lock        = threading.Lock()
        self._ring        = np.zeros((len(CHANNELS), MAX_TAIL), dtype=np.complex64)
        self._write       = 0      # next write index (mod MAX_TAIL)
        self._count       = 0      # total samples written (saturates at MAX_TAIL)
        self._last_write  = 0.0
        self._healthy     = False
        self._gen         = 0      # bumped on every ring clear (retune/recover) — LV-R5

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
            out   = np.zeros((len(CHANNELS), n), dtype=np.complex64)
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
            f"backend={cfg.backend}"
        )

    def rearm(self, cfg: RadioConfig):
        if self.source is None:
            self.open_radio(cfg)
            return
        open_stream(self.source)
        self.source.arm_spec(make_capture(cfg))
        enable_stream(self.source, True)
        # Drop stale samples from the old tuning so they never mix into a frame.
        with self._lock:
            self._clear_ring_locked()
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
    radio keeps draining while a frame is being computed. Paced to ~BROADCAST_FPS
    so it doesn't compute frames the broadcaster would only drop.
    """

    def __init__(self, acquirer: "Acquirer", shared: SharedConfig):
        super().__init__(daemon=True)
        self.acquirer = acquirer
        self.shared   = shared
        self._last_err_notice = 0.0

    def run(self):
        interval = 1.0 / max(BROADCAST_FPS, 1.0)
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
        print("[demo] Synthetic IQ mode — no radio hardware used.")
        print("[demo] Two CW tones per channel + noise. Controls work normally.")

        interval = 1.0 / max(BROADCAST_FPS, 1.0)
        next_t = time.time()
        while not self.shared.stopped():
            # This is the compute thread in demo mode — serve tier-2 probes here
            # for the same thread-bound-cache reason as the Computer (P2a-5).
            self.shared.service_probe()
            cfg = self.shared.snapshot()
            n   = samples_needed(cfg)
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
                blocks, meta = compute_blocks(samples, cfg)
                self._publish(cfg, [blocks[i] for i in range(blocks.shape[0])], meta)
                self.shared.note_good_analysis(cfg)
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
        # Use per-frame global range so quantization is consistent across channels.
        # NaN-safe: a single NaN would make np.percentile return NaN and turn the
        # whole uint8 frame to garbage (LV-R4).
        all_vals = np.concatenate([b.ravel() for b in blocks])
        vmin = float(np.nanpercentile(all_vals, 1))
        vmax = float(np.nanpercentile(all_vals, 99))
        if not (np.isfinite(vmin) and np.isfinite(vmax)):
            vmin, vmax = -100.0, 0.0   # all-NaN block fallback
        if vmax - vmin < 1.0:
            vmax = vmin + 1.0
        hdr       = dict(header, dtype="uint8", scale=[vmin, vmax])
        hdr_bytes = json.dumps(hdr).encode("utf-8")
        parts     = [struct.pack("<I", len(hdr_bytes)), hdr_bytes]
        rng       = vmax - vmin
        for block in blocks:
            u8 = ((np.nan_to_num(np.asarray(block, dtype=np.float32), nan=vmin) - vmin) / rng * 255
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
_computer: "Computer | None"                 = None
_shared:   "SharedConfig | None"             = None
_quantize: bool                              = False
_connections: set                            = set()
_slot_lock                                   = asyncio.Lock()  # guards the single-viewer slot


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the acquirer (+ compute) threads and broadcaster; clean up on shutdown."""
    _acquirer.start()
    if _computer is not None:
        _computer.start()
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
        if _computer is not None:
            _computer.join(timeout=3.0)
        _acquirer.join(timeout=3.0)


app = FastAPI(title="striqt live viewer", lifespan=lifespan)

# Gate the whole app (static page, assets, and /ws) behind shared Basic Auth.
# A no-op at request time when RADIO_USER/RADIO_PASS are unset.
app.add_middleware(BasicAuthMiddleware)
app.add_middleware(NoCacheMiddleware)


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def capture_editor_schema():
    from striqt.sensor import bindings
    from striqt.analysis.specs.helpers import json_schema

    binding = bindings.air8201b
    sweep_cls = getattr(binding, "sweep_spec", None)
    if sweep_cls is None:
        sensor = getattr(binding, "sensor", None)
        sweep_cls = getattr(sensor, "sweep_spec_cls", None)
    if sweep_cls is None:
        raise RuntimeError("Unable to locate air8201b sweep schema")
    return _json_safe(json_schema(sweep_cls))


@app.get("/schema")
async def schema_endpoint():
    return JSONResponse(capture_editor_schema())


def current_config():
    """
    JSON view of the live RadioConfig (P2a-5). The browser seeds its forms from
    this instead of the striqt schema defaults, so a bare Apply re-sends the
    server's own values — no more silent flips of untouched fields whose schema
    default differs from the server default (e.g. host_resample true vs false).
    Also the re-sync source after every settings/analysis ack.
    """
    cfg = _shared.snapshot()
    # The analysis pipelines always execute on the aligned 28-multiple grid, so
    # the resolutions reported for their blocks use it regardless of backend.
    nfft_exec = aligned_nfft(cfg.nfft)
    window = list(cfg.window) if isinstance(cfg.window, tuple) else cfg.window
    integration = cfg.integration_bandwidth
    if not (integration is None or isinstance(integration, str)):
        integration = float(integration)
    psd_window = (list(cfg.psd_window) if isinstance(cfg.psd_window, tuple)
                  else cfg.psd_window)
    psd_integration = cfg.psd_integration_bandwidth
    if not (psd_integration is None or isinstance(psd_integration, str)):
        psd_integration = float(psd_integration)
    return _json_safe({
        "capture": {
            "center_frequency":    float(cfg.center),
            "sample_rate":         float(cfg.sample_rate),
            "gain":                float(cfg.gain),
            "analysis_bandwidth":  float(cfg.analysis_bandwidth),
            "lo_shift":            str(cfg.lo_shift),
            "host_resample":       bool(cfg.host_resample),
            "backend_sample_rate": float(cfg.backend_sample_rate),
            "duration":            float(cfg.duration),
            "nfft":                int(cfg.nfft),
        },
        "analysis": {
            "window":                window,
            "frequency_resolution":  float(cfg.sample_rate) / nfft_exec,
            "fractional_overlap":    str(cfg.fractional_overlap),
            "window_fill":           str(cfg.window_fill),
            "integration_bandwidth": integration,
            "lo_bandstop":           float(cfg.lo_bandstop) if cfg.lo_bandstop else None,
            "trim_stopband":         bool(cfg.trim_stopband),
            "time_aperture":         float(cfg.time_aperture) if cfg.time_aperture else None,
        },
        "analysis_psd": {
            "window":                psd_window,
            "frequency_resolution":  float(cfg.sample_rate) / nfft_exec,
            "fractional_overlap":    str(cfg.psd_fractional_overlap),
            "window_fill":           str(cfg.psd_window_fill),
            "integration_bandwidth": psd_integration,
            "lo_bandstop":           float(cfg.psd_lo_bandstop) if cfg.psd_lo_bandstop else None,
            "trim_stopband":         bool(cfg.psd_trim_stopband),
            "time_statistic":        [s if isinstance(s, str) else float(s)
                                      for s in cfg.psd_time_statistic],
        },
        "analysis_ssb": {
            "subcarrier_spacing":    float(cfg.ssb_subcarrier_spacing),
            "sample_rate":           float(cfg.ssb_sample_rate),
            "discovery_periodicity": float(cfg.ssb_discovery_periodicity),
            "frequency_offset":      float(cfg.ssb_frequency_offset),
            "max_block_count":       (int(cfg.ssb_max_block_count)
                                      if cfg.ssb_max_block_count else None),
            "window":                (list(cfg.ssb_window)
                                      if isinstance(cfg.ssb_window, tuple)
                                      else cfg.ssb_window),
            "lo_bandstop":           (float(cfg.ssb_lo_bandstop)
                                      if cfg.ssb_lo_bandstop else None),
        },
        "backend": str(cfg.backend),
        "rows":    int(cfg.rows),
        "lo_null": bool(cfg.lo_null),
    })


@app.get("/config")
async def config_endpoint():
    return JSONResponse(current_config())


async def _broadcaster():
    """
    Polls acquirer.latest() at BROADCAST_FPS, serializes the frame once, and
    fans it out to all connected WebSocket clients. Dropped connections are
    pruned from the set.
    """
    interval   = 1.0 / max(BROADCAST_FPS, 1)
    last_t     = 0.0
    last_diag  = 0.0   # throttle the heartbeat log to ~once/sec

    while True:
        await asyncio.sleep(interval)

        if not _connections:
            continue

        # Flush queued server notices (compute-backstop reverts etc.) to every
        # viewer — even on ticks with no new frame, so a stalled compute still
        # reports its reason (P2a-3).
        for notice in _shared.drain_notices():
            text = json.dumps({"message": f"[server] {notice}"})
            for ws in list(_connections):
                try:
                    await ws.send_text(text)
                except Exception:
                    pass   # dropped clients are pruned by the frame loop below

        # latest() is fast (threading.Lock + numpy copy) — no executor needed
        header, blocks = _acquirer.latest()

        now    = time.time()
        diag   = now - last_diag > 1.0   # throttled heartbeat this tick?
        if diag:
            last_diag = now

        if header is None:
            if diag:
                print(f"[ws] tick: latest()=None (no frame yet)  clients={len(_connections)}")
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
        sent = 0
        for ws in list(_connections):
            try:
                await ws.send_bytes(msg)
                sent += 1
            except Exception as e:
                print(f"[ws] send failed, dropping client: {e}")
                dead.add(ws)

        if diag:
            print(
                f"[ws] tick: frame t={frame_t:.3f}  blocks={len(blocks)}  "
                f"bytes={len(msg)}  sent={sent}/{len(_connections)}"
            )
        # NOTE: mutate in place. Using `_connections -= dead` here rebinds the
        # name, which (with no `global` decl) makes `_connections` a function
        # local for the WHOLE function — so the `if not _connections:` read at
        # the top of the loop raises UnboundLocalError on the first tick and the
        # broadcaster task dies silently, sending zero frames. (This was the bug.)
        if dead:
            _connections.difference_update(dead)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    WebSocket endpoint. Receives control messages as text JSON:
        {"center": Hz, "sample_rate": Hz, "gain": dB, "nfft": int, "rows": int}
    Sends spectrogram frames as binary (see serialize_frame).
    """
    # Single-viewer slot, guarded so two interleaving handshakes can't both pass
    # the empty check (LV-R3). Busy refusals use a distinct 4001 code (vs 1008 for
    # auth) so the client can tell "another viewer connected" from "unauthorized".
    async with _slot_lock:
        if _connections:
            await ws.accept()
            await ws.close(code=4001)
            print(f"[ws] refused extra client (slot busy): {ws.client}")
            return
        await ws.accept()
        _connections.add(ws)
    client = ws.client
    print(f"[ws] client connected: {client}")
    misses = 0
    try:
        while True:
            try:
                text = await asyncio.wait_for(ws.receive_text(), timeout=15.0)
            except asyncio.TimeoutError:
                # Liveness probe: if the client is gone, free the slot promptly (so a
                # waiting viewer's reconnect can take over) instead of holding it until
                # TCP times out minutes later (LV-R3).
                try:
                    await ws.send_text('{"message":"ping"}')
                    misses = 0
                except Exception:
                    misses += 1
                    if misses >= 2:
                        print(f"[ws] client {client} unresponsive; dropping")
                        break
                continue
            try:
                ctrl = json.loads(text)
                # Run in a worker thread: an analysis apply blocks on tier-2
                # probes serviced by the compute thread (up to ~0.1 s per
                # field), which must not stall the event loop / broadcaster.
                ack = await asyncio.get_running_loop().run_in_executor(
                    None, _shared.update, ctrl
                )
                # Acknowledge settings/analysis applies so the UI can show what
                # took effect vs what was rounded, rejected, ignored, or needs a
                # reconnect (LV-F6, P2a-2). The structured ack rides along so
                # app.js can surface rounded/rejected in the status line.
                # Also ack any message the freedom model adjusted (rounded/
                # rejected) — e.g. a bare {"backend":"ssb"} that retuned the
                # sample rate (P2b-5) must be reported, not just applied.
                if isinstance(ctrl, dict) and (
                    "capture" in ctrl or "source" in ctrl or "analysis" in ctrl
                    or ack.get("rounded") or ack.get("rejected")
                ):
                    parts = [f"applied {ack['applied']}"]
                    for r in ack.get("rounded", []):
                        parts.append(
                            f"rounded {r['field']}: {r['requested']} → {r['used']} ({r['reason']})"
                        )
                    for r in ack.get("rejected", []):
                        parts.append(f"rejected {r['field']}: {r['reason']}")
                    if ack.get("ignored"):
                        parts.append(f"ignored {ack['ignored']}")
                    if ack.get("reconnect"):
                        parts.append(f"reconnect-only {ack['reconnect']}")
                    await ws.send_text(json.dumps(
                        {"message": "settings — " + "; ".join(parts), "ack": ack}
                    ))
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
                # A single malformed control message must never drop the (only)
                # viewer connection (LV-R2).
                await ws.send_text(json.dumps({"message": f"bad control ignored: {e}"}))
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
    global _acquirer, _computer, _shared, _quantize, BROADCAST_FPS, SPEC_BACKEND

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
                        choices=sorted(BACKENDS),
                        help="Spectrogram backend")
    parser.add_argument("--host",     default="0.0.0.0",
                        help="Bind address")
    parser.add_argument("--port",     type=int, default=8000,
                        help="Listen port")
    args = parser.parse_args()

    if args.demo and not _ANALYSIS_OK and args.backend in CALIBRATED_GRID_BACKENDS:
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
    if args.demo:
        # DemoAcquirer generates synthetic IQ and self-publishes — no DMA to
        # overflow, so it keeps the inline-compute path and needs no Computer.
        _acquirer = DemoAcquirer(_shared)
        _computer = None
    else:
        _acquirer = Acquirer(_shared)
        _computer = Computer(_acquirer, _shared)

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

    # Report auth status loudly so an unintentionally-open public server is obvious.
    if AUTH_ENABLED:
        print(f"  auth:     Basic Auth ENABLED (user '{_AUTH_USER}', from RADIO_USER/RADIO_PASS)")
    elif os.environ.get("RADIO_USER") or os.environ.get("RADIO_PASS"):
        print(
            "  auth:     *** WARNING: only one of RADIO_USER / RADIO_PASS set — "
            "auth DISABLED. Set BOTH to enable the login. ***"
        )
    else:
        print(
            "  auth:     *** WARNING: auth DISABLED — server is OPEN to anyone. "
            "Set RADIO_USER and RADIO_PASS to require a login. ***"
        )

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
