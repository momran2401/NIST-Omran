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
MAX_LIVE_ROWS       = 300       # safety cap on requested rows

# Allowed sample rates (LTE/5G-NR multiples of 1.92 MHz) and FFT sizes. Incoming
# control values are snapped to the nearest of these so an off-list value can't
# reach arm_spec or trip the calibrated ValueError guard (LV-R2).
RATES_HZ      = (3.84e6, 7.68e6, 15.36e6, 30.72e6)
NFFT_CHOICES  = (256, 512, 1024, 2048, 4096)


def _snap(value, choices):
    return min(choices, key=lambda c: abs(c - value))


WEB_DIR = Path(__file__).parent / "web"

# Backend: "calibrated" (striqt PSD/ENBW dB) or "quicklook" (simple FFT dB)
SPEC_BACKEND = os.environ.get("SPEC_BACKEND", "calibrated").strip().lower()
BACKENDS = {"calibrated", "quicklook", "ssb"}
if SPEC_BACKEND not in BACKENDS:
    SPEC_BACKEND = "calibrated"

AVG_BIN_GROUPS = 12
SSB_SUBCARRIER_SPACING = 30e3
SSB_SAMPLE_RATE = 7.68e6
SSB_DISCOVERY_PERIOD = 20e-3
SSB_LO_BANDSTOP = 120e3


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

    def snapshot(self):
        return RadioConfig(
            center=float(self.center),
            sample_rate=float(self.sample_rate),
            gain=float(self.gain),
            nfft=int(self.nfft),
            rows=int(self.rows),
            backend=str(self.backend),
            lo_null=bool(self.lo_null),
        )


class SharedConfig:
    def __init__(self):
        self._lock  = threading.Lock()
        self._cfg   = RadioConfig(backend=SPEC_BACKEND)
        self._dirty = False
        self._stop  = False

    def snapshot(self):
        with self._lock:
            return self._cfg.snapshot()

    def update(self, update: dict) -> dict:
        """Apply key/value updates. Returns an ack {applied, ignored, reconnect}."""
        ignored = []
        reconnect = []
        # Capture fields that map to a live radio parameter; the rest are rendered
        # by the editor but cannot be applied live — reported, not dropped (LV-F6).
        capture_mapped = {"center_frequency", "sample_rate", "gain", "duration"}
        if "capture" in update and isinstance(update["capture"], dict):
            capture = update["capture"]
            mapped = {}
            if capture.get("center_frequency") is not None:
                mapped["center"] = capture["center_frequency"]
            if capture.get("sample_rate") is not None:
                mapped["sample_rate"] = capture["sample_rate"]
            if capture.get("gain") is not None:
                mapped["gain"] = capture["gain"]
            if capture.get("duration") is not None:
                try:
                    with self._lock:
                        cur_nfft = self._cfg.nfft
                        cur_rate = self._cfg.sample_rate
                    # Map duration→rows using values from THIS message when it also
                    # changes them, not the pre-update cfg (LV-R9b).
                    nfft        = capture.get("nfft") or cur_nfft
                    sample_rate = capture.get("sample_rate") or cur_rate
                    rows = round(float(capture["duration"]) * float(sample_rate) / max(int(nfft), 1))
                    mapped["rows"] = max(1, min(rows, MAX_LIVE_ROWS))
                except Exception:
                    pass
            ignored = sorted(
                k for k, v in capture.items() if v is not None and k not in capture_mapped
            )
            update = dict(update)
            update.update(mapped)

        if "source" in update and isinstance(update["source"], dict):
            reconnect = sorted(k for k, v in update["source"].items() if v is not None and k not in {
                "receive_retries", "adc_overload_limit", "if_overload_limit", "gapless",
            })
            if reconnect:
                print(f"[config] source changes require reconnect: {reconnect}")

        valid = {"center", "sample_rate", "gain", "nfft", "rows", "backend", "lo_null"}
        changes = []
        with self._lock:
            for key, value in update.items():
                if key not in valid:
                    continue
                if key == "backend":
                    value = str(value).strip().lower()
                    if value not in BACKENDS:
                        continue
                elif key == "lo_null":
                    value = bool(value)
                else:
                    value = int(value) if key in {"nfft", "rows"} else float(value)
                # Clamp rows so a misbehaving browser can't overload the radio
                if key == "rows":
                    value = int(max(1, min(value, MAX_LIVE_ROWS)))
                elif key == "center":
                    value = float(max(300e6, min(value, 6e9)))
                elif key == "sample_rate":
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
                self._dirty = True
        # Print outside the lock to avoid I/O inside a mutex
        for key, old, value in changes:
            print(f"[config] {key}: {old} -> {value}")
        return {"applied": [k for k, _, _ in changes], "ignored": ignored, "reconnect": reconnect}

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
    window = np.hanning(nfft).astype(np.float32)
    x      = x * window[None, None, :]
    spec   = np.fft.fftshift(np.fft.fft(x, axis=-1), axes=-1)
    # Normalize by window power (proper PSD estimate)
    power  = (np.abs(spec) ** 2) / max(float(np.sum(window ** 2)), 1.0)
    spg = (10.0 * np.log10(power + 1e-20)).astype(np.float32)
    # Quicklook is a plain fftshifted per-bin FFT: fft_nfft = nfft, no averaging.
    return spg, {"fft_nfft": int(nfft), "bin_avg": 1}


def calibrated_spectrogram(
    samples: np.ndarray, nfft: int, rows: int, sample_rate: float, lo_null: bool = True
) -> tuple:
    """
    striqt-calibrated PSD spectrogram with symbol-style overlap/fill and
    frequency-bin averaging. Returns (blocks, meta) — blocks (channels, rows,
    bins) float32, meta {fft_nfft, bin_avg}.
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"calibrated backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    nfft = aligned_nfft(nfft)
    # Right-size to exactly `rows` STFT rows under the 13/28 overlap, rather than
    # computing ~1.87×rows rows and discarding all but the last `rows` (LV-W2).
    needed  = calibrated_sample_count(nfft, rows)
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
    frequency_resolution = sample_rate / float(nfft)
    average_bins = averaging_factor(nfft)
    spec = analysis_specs.Spectrogram(
        window=("kaiser", 11.88),
        frequency_resolution=frequency_resolution,
        fractional_overlap=Fraction(13, 28),
        window_fill=Fraction(15, 28),
        integration_bandwidth=frequency_resolution * average_bins,
        trim_stopband=False,
        lo_bandstop=SSB_LO_BANDSTOP,
    )
    striqt_shared.spectrogram_cache.clear()
    spg, _ = striqt_shared.evaluate_spectrogram(
        samples, capture, spec, dtype="float32", dB=True
    )
    blocks = fit_display_rows(
        np.asarray(spg, dtype=np.float32), rows,
        bin_avg=average_bins, fft_nfft=nfft, sample_rate=sample_rate, lo_null=lo_null,
    )
    return blocks, {"fft_nfft": int(nfft), "bin_avg": int(average_bins)}


def ssb_spectrogram(
    samples: np.ndarray, nfft: int, rows: int, sample_rate: float, lo_null: bool = True
) -> tuple:
    """
    5G SSB spectrogram path from striqt. The returned SSB block and symbol axes
    are flattened to the dashboard's existing rows x bins frame contract.
    Returns (blocks, meta).
    """
    if not _ANALYSIS_OK:
        raise RuntimeError(f"SSB backend unavailable: {_ANALYSIS_ERR!r}")

    samples = np.asarray(samples, dtype=np.complex64)
    nfft = aligned_nfft(nfft)
    sample_rate = float(sample_rate)
    capture = analysis_specs.Capture(
        sample_rate=sample_rate,
        duration=samples.shape[1] / sample_rate,
        analysis_bandwidth=float("inf"),
    )
    try:
        spg, _ = striqt_measurements.cellular_5g_ssb_spectrogram(
            samples,
            capture,
            as_xarray=False,
            subcarrier_spacing=SSB_SUBCARRIER_SPACING,
            sample_rate=min(SSB_SAMPLE_RATE, sample_rate),
            discovery_periodicity=SSB_DISCOVERY_PERIOD,
            frequency_offset=0.0,
            max_block_count=None,
            window="blackmanharris",
            lo_bandstop=SSB_LO_BANDSTOP,
        )
    except ValueError as exc:
        if "counting-number" not in str(exc):
            raise
        # The live viewer's power-of-two sample-rate/FFT controls are not
        # always compatible with the strict 30 kHz SSB grid. Keep the selector
        # useful by falling back to the same 13/28, 15/28 averaged grid.
        return calibrated_spectrogram(samples, nfft, rows, sample_rate, lo_null=lo_null)
    spg = np.asarray(spg, dtype=np.float32)
    if spg.ndim == 4:
        spg = spg.reshape(spg.shape[0], spg.shape[1] * spg.shape[2], spg.shape[3])
    # Real SSB uses 15 kHz (subcarrier_spacing/2) resolution; best-effort axis
    # params (this path is unreachable at the selectable sample rates — LV-F2).
    ssb_nfft = max(1, round(sample_rate / (SSB_SUBCARRIER_SPACING / 2)))
    blocks = fit_display_rows(
        spg, rows,
        bin_avg=1, fft_nfft=ssb_nfft, sample_rate=sample_rate, lo_null=lo_null,
    )
    return blocks, {"fft_nfft": int(ssb_nfft), "bin_avg": 1}


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


def calibrated_sample_count(nfft: int, rows: int) -> int:
    """
    Samples needed to produce exactly `rows` STFT rows under the 13/28 overlap.
    Each displayed row advances the STFT by hop = nfft·15/28 samples, so
    rows·hop + (nfft-hop) samples suffice — instead of the ~1.87× that rows·nfft
    would compute and then discard (see AUDIT_REPORT.md LV-W2). `nfft` must be an
    aligned FFT size (multiple of 28) so the hop divides evenly; the count then
    reproduces striqt's own row formula int((nfft/hop)·(N/nfft-1)+1) == rows.
    """
    nfft = int(nfft)
    hop = (nfft * 15) // 28
    return int(rows * hop + (nfft - hop))


def fit_display_rows(
    spg: np.ndarray,
    rows: int,
    *,
    bin_avg: int = 1,
    fft_nfft=None,
    sample_rate=None,
    lo_null: bool = True,
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

    # Null the LO leakage region, sized to the striqt bandstop instead of a fixed
    # ±2 bins (which hid up to ~3.7 MHz of real spectrum at coarse FFTs). Optional
    # via the lo_null flag so the center can be revealed (LV-F8).
    if lo_null and spg.shape[2] >= 3 and fft_nfft and sample_rate:
        step = max(1, bin_avg) * float(sample_rate) / float(fft_nfft)   # Hz per averaged bin
        half = max(1, math.ceil((SSB_LO_BANDSTOP / 2) / step))
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
    if cfg.backend in {"calibrated", "ssb"}:
        # Overlapped STFT: only rows·hop + (nfft-hop) samples are needed to
        # produce cfg.rows display rows (LV-W2), not the full nfft·rows.
        base = calibrated_sample_count(aligned_nfft(cfg.nfft), cfg.rows)
    else:
        base = int(cfg.nfft * cfg.rows)
    if cfg.backend == "ssb" and ssb_grid_compatible(cfg.sample_rate):
        ssb = int(math.ceil(SSB_DISCOVERY_PERIOD * cfg.sample_rate))
        return max(base, ssb)
    return base


def ssb_grid_compatible(sample_rate: float) -> bool:
    nfft = round(2 * float(sample_rate) / SSB_SUBCARRIER_SPACING)
    return nfft > 0 and (13 * nfft) % 28 == 0


def compute_blocks(samples: np.ndarray, cfg: RadioConfig):
    """
    Dispatch to the configured backend.
    Returns (blocks, meta): blocks is (channels, rows, bins) float32; meta carries
    the per-frame axis parameters (fft_nfft, bin_avg) and the executed backend,
    used by build_header to ship an honest frame header (LV-F1/F2).
    """
    requested = cfg.backend
    if requested == "ssb" and not ssb_grid_compatible(cfg.sample_rate):
        # SSB requires a sample rate on the 420 kHz grid; none of the selectable
        # rates qualify, so skip the striqt call (which would raise and fall back
        # every frame) and run calibrated directly, reporting it honestly (LV-F2).
        blocks, meta = calibrated_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate, lo_null=cfg.lo_null)
        executed = "calibrated"
    elif requested == "calibrated":
        blocks, meta = calibrated_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate, lo_null=cfg.lo_null)
        executed = "calibrated"
    elif requested == "ssb":
        blocks, meta = ssb_spectrogram(samples, cfg.nfft, cfg.rows, cfg.sample_rate, lo_null=cfg.lo_null)
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

    # Frequency axis. Quicklook is a plain fftshifted FFT (bin 0 = -fs/2); the
    # calibrated/ssb path DC-centers bin_avg-wide averaged groups, so their
    # centers are symmetric about DC with step = bin_avg*fs/fft_nfft.
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
        "time":          time.time(),
    }
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

    def run(self):
        interval = 1.0 / max(BROADCAST_FPS, 1.0)
        next_t   = time.time()
        while not self.shared.stopped():
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
            except Exception as e:
                print(f"[compute] error: {e}")

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
        print("[demo] Synthetic IQ mode — no radio hardware used.")
        print("[demo] Two CW tones per channel + noise. Controls work normally.")

        interval = 1.0 / max(BROADCAST_FPS, 1.0)
        next_t = time.time()
        while not self.shared.stopped():
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
            except Exception as e:
                print(f"[demo] compute error: {e}")

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
                ack = _shared.update(ctrl)
                # Acknowledge settings-editor applies so the UI can show what took
                # effect vs what was ignored or needs a reconnect (LV-F6).
                if isinstance(ctrl, dict) and ("capture" in ctrl or "source" in ctrl):
                    await ws.send_text(json.dumps({"message":
                        f"settings — applied {ack['applied']}; "
                        f"ignored {ack['ignored']}; reconnect-only {ack['reconnect']}"}))
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

    if args.demo and not _ANALYSIS_OK and args.backend in {"calibrated", "ssb"}:
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
