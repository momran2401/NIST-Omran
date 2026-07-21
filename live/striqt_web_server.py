#!/usr/bin/env python3
"""
Web-based live spectrogram + PSD viewer server (frontend).

All radio/DSP/config logic lives in live/core/ — this file is the web-facing
frontend: authentication, HTTP routes, the WebSocket broadcaster, and startup
wiring. Any other frontend (terminal, kiosk standalone) drives the same core.

Usage:
    python live/striqt_web_server.py                     # AIR8201B radio
    python live/striqt_web_server.py --demo              # synthetic IQ
    python live/striqt_web_server.py --device auto       # enumerate SoapySDR
    python live/striqt_web_server.py --device pluto      # PlutoSDR
    python live/striqt_web_server.py --device driver=plutosdr,serial=XYZ
    python live/striqt_web_server.py --quantize          # uint8 frames

Convenience launcher (adds optional Cloudflare Tunnel):
    bash live/run_web.sh
"""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

# live/core is importable relative to this file, regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# core.striqt_compat (imported first, via the package) handles the AIR-T pixi
# LD_LIBRARY_PATH re-exec before any scipy/striqt import.
from core import devices, health, state
from core.acquisition import Acquirer, Computer, DemoAcquirer
from core.config import SharedConfig
from core.constants import BACKENDS, CALIBRATED_GRID_BACKENDS, DEVICE_PROFILES
from core.dsp import aligned_nfft
from core.operations import OPERATIONS
from core.recording import RecordingManager
from core.serialization import serialize_frame
from core.striqt_compat import _ANALYSIS_OK, _SENSOR_OK

# FastAPI
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import (
        HTMLResponse,
        JSONResponse,
        PlainTextResponse,
        RedirectResponse,
    )
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print(
        "FastAPI not installed. Run:\n"
        "  pip install fastapi 'uvicorn[standard]'",
        file=sys.stderr,
    )
    sys.exit(1)

WEB_DIR = Path(__file__).parent / "web"

# ---------------------------------------------------------------------------
# HTTP Basic Auth (three role-bearing credentials, read from the environment)
# ---------------------------------------------------------------------------
#
# The viewer (static page, assets, and the /ws WebSocket) is gated behind one of
# three logins, each mapping to a role:
#
#   admin   → full control (only ONE admin connected at a time)
#   viewer  → read-only; every control shows an "access denied" popup
#   interns → read-only; same, with a different popup message
#
# Each user/pass is overridable via env vars (ADMIN_USER/ADMIN_PASS,
# VIEWER_USER/VIEWER_PASS, INTERN_USER/INTERN_PASS) and falls back to a built-in
# default when unset. Because defaults always exist, auth is effectively ALWAYS
# ENABLED. Set RADIO_AUTH_DISABLE=1 to turn it off for --demo / local dev, in
# which case everyone is granted DEFAULT_ROLE. A loud warning prints at startup
# whenever default passwords or a disabled gate are in effect.

_ROLE_CREDS = {
    "admin":   (os.environ.get("ADMIN_USER")  or "admin",
                os.environ.get("ADMIN_PASS")  or "mustafaroxx4321"),
    "viewer":  (os.environ.get("VIEWER_USER") or "viewer",
                os.environ.get("VIEWER_PASS") or "aricsfavinternmadethis"),
    "interns": (os.environ.get("INTERN_USER") or "intern",
                os.environ.get("INTERN_PASS") or "mustafashandsome"),
}
WRITE_ROLES   = frozenset({"admin"})            # roles allowed to mutate config
AUTH_DISABLED = os.environ.get("RADIO_AUTH_DISABLE") == "1"
DEFAULT_ROLE  = "admin"                          # role granted when auth disabled
AUTH_ENABLED  = not AUTH_DISABLED
AUTH_REALM    = "striqt live viewer"

# systemd unit the "Reset Radio" admin action restarts (overridable per host).
RADIO_SERVICE_NAME = os.environ.get("RADIO_SERVICE_NAME") or "radio-web"


def match_credentials(user, pw) -> "str | None":
    """
    Resolve an explicit username/password pair to a role name, or None when it
    matches no known login. Constant-time across all three credentials: the
    supplied user/pass is compared against EVERY row using bitwise `&` (no `and`
    short-circuit) and no early return, so timing never reveals which usernames
    exist or which row matched. Used by both the HTTP Basic path and the login
    form POST.
    """
    matched_role = None
    for role, (u, p) in _ROLE_CREDS.items():
        # Evaluate BOTH digests every iteration (bitwise &, never short-circuit)
        # and never break/return early, so total time is independent of which
        # row — if any — matches.
        ok = bool(secrets.compare_digest(user, u)) & bool(secrets.compare_digest(pw, p))
        if ok:
            matched_role = role
    return matched_role


def authenticate(auth_header) -> "str | None":
    """
    Resolve an HTTP `Authorization` header to a role name, or None when the
    credentials match no known login. Returns DEFAULT_ROLE when auth is disabled
    so --demo / local dev keeps full control.

    Constant-time across all three credentials: the supplied user/pass is
    compared against EVERY row on every call, using bitwise `&` (no `and`
    short-circuit) and no early return, so response time never reveals which
    usernames exist or which row matched.

    `auth_header` may be a str (Starlette Request) or bytes (raw ASGI scope).
    """
    if AUTH_DISABLED:
        return DEFAULT_ROLE
    if not auth_header:
        return None
    if isinstance(auth_header, bytes):
        auth_header = auth_header.decode("latin-1")

    scheme, _, param = auth_header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, pw = base64.b64decode(param).decode("utf-8").partition(":")
    except Exception:
        return None

    return match_credentials(user, pw)


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
# The token now carries the authenticated ROLE (not just an expiry) so the role
# survives the cookie-only path that Safari/iOS use for the WS upgrade. The role
# is inside the HMAC, so a viewer cannot self-elevate by editing the cookie.
#
# The signing secret comes from RADIO_SESSION_SECRET when set; otherwise it is
# derived deterministically from ALL three role credentials (not any single
# password). NOTE: with the built-in default passwords the derived secret is
# predictable to anyone who reads the source — a real deployment should set
# RADIO_SESSION_SECRET and override the default passwords so cookies can't be
# forged (a startup warning nags about this).

_SESSION_SECRET = hashlib.sha256(
    (os.environ.get("RADIO_SESSION_SECRET")
     or "|".join(f"{r}:{u}:{p}" for r, (u, p) in _ROLE_CREDS.items())
    ).encode()
).digest()
SESSION_TTL = 86400


def make_session_token(role: str, ttl_seconds: int = SESSION_TTL) -> str:
    """
    Build a signed session token "<role>.<exp>.<hex_hmac>" where exp is an int
    unix expiry and hex_hmac = HMAC-SHA256(secret, "<role>.<exp>"). The role is
    covered by the MAC so it cannot be tampered with.
    """
    exp = int(time.time()) + ttl_seconds
    payload = f"{role}.{exp}"
    mac = hmac.new(_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"

def verify_session_token(token) -> "str | None":
    """
    Validate a "<role>.<exp>.<hex_hmac>" session token: recompute the HMAC with a
    constant-time comparison, confirm the role is known, and confirm the expiry
    is still in the future. Returns the role on success, else None on any
    malformed / tampered / expired input.
    """
    if not token:
        return None
    if isinstance(token, bytes):
        token = token.decode("latin-1")

    role, _, rest = token.partition(".")
    exp_str, _, mac = rest.partition(".")
    if not role or not mac:
        return None
    if role not in _ROLE_CREDS:          # reject forged / unknown roles
        return None
    try:
        exp = int(exp_str)
    except ValueError:
        return None

    payload = f"{role}.{exp_str}"
    expected = hmac.new(
        _SESSION_SECRET, payload.encode(), hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(mac, expected):
        return None
    if exp <= int(time.time()):
        return None
    return role


def _session_cookie_from_scope(scope) -> "str | None":
    """
    Parse the request's Cookie header from a raw ASGI scope and return the role
    when a "radio_auth" cookie is present and passes verify_session_token, else
    None.
    """
    headers = dict(scope.get("headers") or [])
    raw_cookie = headers.get(b"cookie")
    if not raw_cookie:
        return None
    cookie_str = raw_cookie.decode("latin-1")
    for part in cookie_str.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "radio_auth":
            return verify_session_token(value)
    return None


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
    def _set_cookie_send(scope, send, role):
        """
        Wrap `send` to append a Set-Cookie header carrying a fresh role-bearing
        session token on the HTTP response start. Only the success path uses
        this, so the cookie is never attached to a 401. The `Secure` attribute is
        omitted over plain HTTP (LAN) so Safari/iOS — which refuse to store a
        Secure cookie without TLS and won't replay Basic on the WS upgrade — can
        still reach /ws (LV-R8). HttpOnly and SameSite=Lax are always set.
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
                    f"radio_auth={make_session_token(role)}; Path=/; HttpOnly; "
                    f"{secure_attr}SameSite=Lax; Max-Age={SESSION_TTL}"
                )
                headers = list(message.get("headers") or [])
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    # Paths that must be reachable WITHOUT authentication so the login flow can
    # work: the login form/handler and the logout endpoint. Everything else is
    # gated. (The WS 1008 path and page redirect below both skip these.)
    # /health is public for monitoring + restart polling, but the endpoint
    # returns only the minimal liveness triple when no role resolved.
    _PUBLIC_PATHS = frozenset({"/login", "/logout", "/health"})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if AUTH_DISABLED:
            # Auth off (demo/local): everyone gets DEFAULT_ROLE so the endpoint
            # always sees a role and controls aren't silently locked out.
            scope["role"] = DEFAULT_ROLE
            scope["user"] = DEFAULT_ROLE
            await self.app(scope, receive, send)
            return

        # The login/logout routes are always reachable so an unauthenticated (or
        # signing-out) browser can complete the flow. They set/clear the cookie
        # themselves; the middleware just gets out of the way. A role is still
        # resolved opportunistically so /health can answer richly for
        # authenticated callers while staying reachable for anonymous ones.
        if scope["type"] == "http" and scope.get("path") in self._PUBLIC_PATHS:
            headers = dict(scope.get("headers") or [])
            role = (authenticate(headers.get(b"authorization"))
                    or _session_cookie_from_scope(scope))
            if role:
                scope["role"] = role
                scope["user"] = role
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        # Resolve the role from the Basic Auth header, falling back to a valid
        # signed session cookie. The cookie path lets browsers that drop Basic
        # creds on the WS upgrade (Safari / all iOS) still connect to /ws after
        # logging in for the page.
        role = authenticate(headers.get(b"authorization")) or _session_cookie_from_scope(scope)
        if role:
            # The same dict is ws.scope / request.scope in the endpoint, so this
            # is how the role reaches ws_endpoint.
            scope["role"] = role
            scope["user"] = role
            if scope["type"] == "http":
                # Refresh the role-bearing cookie so the browser carries it on
                # the WS handshake. Never set it on websocket scopes.
                await self.app(scope, receive, self._set_cookie_send(scope, send, role))
            else:
                await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Reject the upgrade before accept(); no credentials means no frames.
            await send({"type": "websocket.close", "code": 1008})
            return

        # Unauthenticated page/asset request. Browsers get redirected to the
        # login FORM (303) instead of a Basic 401 challenge — that way browsers
        # never cache Basic credentials and the signed cookie becomes their sole
        # credential, which makes sign-out / switch-user reliable. A Basic header
        # is still ACCEPTED above (so `curl -u` and API clients keep working); we
        # just no longer CHALLENGE with it. Non-GET / API-ish requests get a plain
        # 401 rather than a redirect they can't follow.
        method = (scope.get("method") or "GET").upper()
        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        wants_html = method == "GET" and ("text/html" in accept or accept in ("", "*/*"))
        if wants_html:
            await send({
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", b"/login"),
                    (b"content-length", b"0"),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
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
# FastAPI application
# ---------------------------------------------------------------------------

# Module-level globals set in main() before uvicorn starts
_acquirer = None            # Acquirer | DemoAcquirer
_computer = None            # Computer | None
_shared   = None            # SharedConfig
_quantize = False
_connections: set = set()   # ALL clients (broadcast fan-out set)
_slot_lock = asyncio.Lock() # guards the single-admin slot
_admin_ws  = None           # the one active admin socket, or None
_recording = None           # RecordingManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the acquirer (+ compute) threads and broadcaster; clean up on
    shutdown. Cancellation is swallowed explicitly: CancelledError is a
    BaseException, so a bare `except Exception` here used to let Ctrl-C
    surface as "Application shutdown failed" with a traceback."""
    _acquirer.start()
    if _computer is not None:
        _computer.start()
    # Give the radio (or demo) a moment to produce the first frame
    await asyncio.sleep(1.2)
    task = asyncio.create_task(_broadcaster())
    print(f"[ws] broadcaster running at {state.BROADCAST_FPS} fps")
    try:
        yield
    finally:
        if _recording is not None:
            await _recording.shutdown()
        _shared.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError,
                                 Exception):
            await asyncio.wait_for(task, timeout=0.5)
        if _computer is not None:
            _computer.join(timeout=3.0)
        _acquirer.join(timeout=3.0)


app = FastAPI(title="striqt live viewer", lifespan=lifespan)

# Gate the whole app (static page, assets, and /ws) behind the auth middleware.
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

    binding_name = state.DEVICE if state.DEVICE.startswith("air") else "air8201b"
    binding = getattr(bindings, binding_name, bindings.air8201b)
    sweep_cls = getattr(binding, "sweep_spec", None)
    if sweep_cls is None:
        sensor = getattr(binding, "sensor", None)
        sweep_cls = getattr(sensor, "sweep_spec_cls", None)
    if sweep_cls is None:
        raise RuntimeError("Unable to locate air8201b sweep schema")
    return _json_safe(json_schema(sweep_cls))


@app.get("/schema")
async def schema_endpoint():
    # striqt may be absent when running --demo on a machine without the SDR
    # stack; answer with a clean 503 (the client logs it and skips the capture
    # editor) instead of an unhandled-500 traceback on every page load.
    try:
        return JSONResponse(capture_editor_schema())
    except Exception as exc:
        return JSONResponse(
            {"error": f"capture schema unavailable: {exc}"}, status_code=503
        )


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
        "source": dict(cfg.source_config or {}),
        "device": devices.get_adapter().describe_capabilities(),
        "envelope": _shared.envelope(),
        "backend": str(cfg.backend),
        "rows":    int(cfg.rows),
        "lo_null": bool(cfg.lo_null),
    })


@app.get("/config")
async def config_endpoint():
    return JSONResponse(current_config())


@app.get("/health")
async def health_endpoint(request: Request):
    """
    Liveness + identity. boot_id changes on every process start, which is the
    browser's proof that Reset Radio ACTUALLY restarted the service. /health
    is reachable without auth (monitoring, restart polling from a login page),
    but anonymous callers only get the minimal liveness triple.
    """
    snap = health.health_snapshot()
    snap["service"] = RADIO_SERVICE_NAME
    if request.scope.get("role") is None and not AUTH_DISABLED:
        snap = {"status": snap["status"], "boot_id": snap["boot_id"],
                "uptime_s": snap["uptime_s"]}
    return JSONResponse(_json_safe(snap))


@app.get("/operations")
async def operations_endpoint():
    """Recent verified-operations history (any authenticated role)."""
    return JSONResponse(_json_safe({"operations": OPERATIONS.recent(50)}))


@app.get("/record")
async def record_status_endpoint():
    """Recording state plus a form seed derived from the current live view."""
    return JSONResponse(_json_safe({
        "recording": _recording.status(),
        "defaults": _recording.defaults(),
        "config": current_config(),
    }))


@app.post("/record")
async def record_start_endpoint(request: Request):
    if request.scope.get("role", DEFAULT_ROLE) not in WRITE_ROLES:
        return JSONResponse({"error": "admin privileges required"}, status_code=403)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        result = await _recording.start(payload)
        return JSONResponse({"recording": _json_safe(result)}, status_code=202)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409 if _recording.active() else 400)


@app.post("/record/stop")
async def record_stop_endpoint(request: Request):
    if request.scope.get("role", DEFAULT_ROLE) not in WRITE_ROLES:
        return JSONResponse({"error": "admin privileges required"}, status_code=403)
    return JSONResponse({"recording": _json_safe(await _recording.stop())}, status_code=202)


@app.post("/config")
async def config_apply_endpoint(request: Request):
    """
    HTTP twin of the WebSocket control path (admin only) — same validated
    SharedConfig.update, same ack (incl. op_id). Exists for scripted clients:
    live/radioctl.py drives its `set` and `self-test` commands through this.
    """
    if request.scope.get("role", DEFAULT_ROLE) not in WRITE_ROLES:
        return JSONResponse({"error": "admin privileges required"}, status_code=403)
    if _recording.active():
        return JSONResponse({"error": "controls are locked while recording"}, status_code=409)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
    except Exception as exc:  # malformed body
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        ack = await asyncio.get_running_loop().run_in_executor(
            None, _shared.update, payload
        )
        return JSONResponse({"ack": _json_safe(ack)})
    except (ValueError, TypeError, AttributeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.websocket("/ws/logs")
async def logs_ws_endpoint(ws: WebSocket):
    """
    Admin-only journal tail for the OPS tab: streams `journalctl -fu
    <service>` lines as {"journal": ...} text messages. Operation events
    already arrive on the main /ws socket; this adds the raw service log the
    user asked for — without ever exposing a shell.
    """
    role = ws.scope.get("role", DEFAULT_ROLE)
    if role not in WRITE_ROLES:
        await ws.close(code=1008)
        return
    await ws.accept()
    journalctl = shutil.which("journalctl")
    if not journalctl:
        await ws.send_text(json.dumps(
            {"journal": "(journalctl not available on this host — "
                        "operation events above are the full log)"}))
        # Keep the socket open but idle so the client doesn't reconnect-spin.
        try:
            while True:
                await asyncio.sleep(30)
                await ws.send_text(json.dumps({"journal_ping": True}))
        except Exception:
            return
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            journalctl, "-u", RADIO_SERVICE_NAME, "-n", "200", "-f",
            "--no-pager", "-o", "short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # The AIR-T driver does not mark every XDMA descriptor CLOEXEC.
            # Without this explicit boundary the journal follower can retain
            # /dev/xdma0_c2h_0 after a retune closes the live RX stream,
            # causing the replacement stream to fail with EBUSY.
            close_fds=True,
        )
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                if proc.returncode is not None:
                    await ws.send_text(json.dumps(
                        {"journal": f"(journal tail ended, rc={proc.returncode})"}))
                    break
                await asyncio.sleep(0.3)
                continue
            await ws.send_text(json.dumps(
                {"journal": raw.decode("utf-8", "replace").rstrip()}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"journal": f"journal unavailable: {exc}"}))
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)

# ---------------------------------------------------------------------------
# Login / logout (cookie-based session; see BasicAuthMiddleware)
# ---------------------------------------------------------------------------
#
# The browser path is cookie-only: unauthenticated page loads are redirected to
# /login (by the middleware) instead of a Basic-Auth 401 challenge, so browsers
# never cache Basic credentials. That makes sign-out / switch-user reliable —
# /logout just clears the cookie. A Basic header is still accepted for curl/API.

def _login_page(error: str = "") -> str:
    """Minimal, self-contained dark login form (styled inline because the app's
    style.css lives behind the auth gate this page is in front of)."""
    err_html = (
        f'<p class="err">{error}</p>' if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Sign in · SDR LIVE Viewer - Div. 675</title>
<style>
  :root {{ --bg:#0b0f14; --panel:#111823; --border:#22303f; --text:#e6edf3;
          --dim:#8aa0b3; --accent:#4ea3ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
          justify-content:center; background:var(--bg); color:var(--text);
          font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .card {{ width:min(92vw,360px); background:var(--panel);
           border:1px solid var(--border); border-radius:14px; padding:26px 24px;
           box-shadow:0 12px 48px rgba(0,0,0,0.5); }}
  h1 {{ font-size:19px; margin:0 0 2px; letter-spacing:0.01em; }}
  .sub {{ color:var(--dim); font-size:12px; margin:0 0 20px; }}
  label {{ display:block; font-size:12px; color:var(--dim); margin:14px 0 5px; }}
  input {{ width:100%; padding:10px 12px; background:var(--bg);
           border:1px solid var(--border); border-radius:8px; color:var(--text);
           font-size:15px; }}
  input:focus {{ outline:none; border-color:var(--accent); }}
  button {{ width:100%; margin-top:20px; padding:11px; background:var(--accent);
            border:none; border-radius:8px; color:#04121f; font-size:15px;
            font-weight:700; cursor:pointer; }}
  .err {{ background:rgba(255,96,96,0.12); border:1px solid #ff6060; color:#ffb3b3;
          padding:8px 10px; border-radius:8px; font-size:13px; margin:0 0 4px; }}
</style></head><body>
  <form class="card" method="post" action="/login" autocomplete="off">
    <h1>SDR LIVE Viewer - Div. 675</h1>
    <p class="sub">National Institute of Standards and Technology</p>
    {err_html}
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password">
    <button type="submit">Sign in</button>
  </form>
</body></html>"""


def _cookie_kwargs(request: "Request") -> dict:
    """Cookie attributes matching BasicAuthMiddleware._set_cookie_send: HttpOnly,
    SameSite=Lax, and Secure only over HTTPS (omitted on plain-HTTP LAN so
    Safari/iOS still store it)."""
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
    )
    return dict(
        path="/", httponly=True, samesite="lax",
        secure=is_https, max_age=SESSION_TTL,
    )


@app.get("/login")
async def login_form(request: "Request"):
    # Auth off: nothing to sign into — send them straight to the viewer.
    if AUTH_DISABLED:
        return RedirectResponse("/", status_code=303)
    # Already signed in (valid cookie)? Skip the form.
    if _session_cookie_from_scope(request.scope):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_page())


@app.post("/login")
async def login_submit(request: "Request"):
    if AUTH_DISABLED:
        return RedirectResponse("/", status_code=303)
    # Parse the urlencoded form body directly (avoids a python-multipart
    # dependency that request.form() would pull in; the login form posts
    # application/x-www-form-urlencoded).
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(raw, keep_blank_values=True)
    role = match_credentials(
        (form.get("username") or [""])[0], (form.get("password") or [""])[0]
    )
    if not role:
        return HTMLResponse(
            _login_page("Incorrect username or password."), status_code=401
        )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("radio_auth", make_session_token(role), **_cookie_kwargs(request))
    return resp


@app.get("/logout")
async def logout(request: "Request"):
    resp = RedirectResponse("/login", status_code=303)
    # Clear the session cookie (empty value + immediate expiry).
    resp.delete_cookie("radio_auth", path="/")
    return resp


@app.post("/admin/reset-radio")
async def reset_radio(request: Request):
    """
    Admin-only: restart the radio systemd service — as a VERIFIED operation.

    The old implementation spawned `sudo -n systemctl restart …` with both
    pipes on /dev/null and answered 202 unconditionally, which proved only
    that Popen() worked. Now:

      1. The command's stderr is captured, and the process is given ~1.2 s to
         fail fast — a missing sudoers rule or unknown unit comes back as a
         500 WITH the actual sudo/systemctl error text instead of silence.
      2. The 202 response carries an operation id and THIS process's boot_id.
      3. The browser polls /health until it sees a DIFFERENT boot_id (proof
         the service really restarted and came back), or times out and says
         exactly which stage failed.

    The restart still detaches (start_new_session) because it tears down this
    very process — final confirmation necessarily happens in the NEW process,
    via the boot_id change.
    """
    role = request.scope.get("role", DEFAULT_ROLE)
    if role not in WRITE_ROLES:
        return JSONResponse({"error": "admin privileges required"}, status_code=403)

    sudo_path = shutil.which("sudo")
    systemctl_path = shutil.which("systemctl")
    op_id = OPERATIONS.begin("reset", f"restart service {RADIO_SERVICE_NAME}")
    if not sudo_path or not systemctl_path:
        OPERATIONS.finish(op_id, "failed", "sudo/systemctl not found on this host")
        return JSONResponse(
            {"error": "sudo/systemctl not found on this host", "op_id": op_id},
            status_code=500,
        )
    cmd = [sudo_path, "-n", systemctl_path, "restart", RADIO_SERVICE_NAME]
    OPERATIONS.stage(op_id, "applying",
                     f"{' '.join(cmd)} (requested by {request.client})")

    # Preflight the exact sudoers permission WITHOUT executing the restart —
    # the most common failure (missing/wrong sudoers rule) is caught here
    # synchronously, with sudo's real error text, before anything is killed.
    def _preflight():
        try:
            return subprocess.run(
                [sudo_path, "-n", "-l", systemctl_path, "restart",
                 RADIO_SERVICE_NAME],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=3,
            )
        except Exception as e:  # noqa: BLE001
            return e

    pre = await asyncio.get_running_loop().run_in_executor(None, _preflight)
    if isinstance(pre, Exception) or pre.returncode != 0:
        reason = (str(pre) if isinstance(pre, Exception)
                  else (pre.stdout or "").strip() or "sudoers preflight failed")
        OPERATIONS.finish(op_id, "failed", f"sudo preflight: {reason}")
        return JSONResponse(
            {"error": f"not permitted: {reason} — run "
                      f"live/install_radio_web_sudoers.sh on the host",
             "op_id": op_id},
            status_code=500,
        )
    OPERATIONS.stage(op_id, "validated", "sudoers rule allows the restart")

    # stderr goes to a PERSISTENT log (survives this process being replaced):
    # RADIO_RESET_LOG, set by the systemd unit to /var/log/radio-web/reset.log.
    log_path = Path(os.environ.get(
        "RADIO_RESET_LOG",
        "/tmp/{}-reset.log".format(RADIO_SERVICE_NAME.replace("/", "_")),
    ))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
        log_file.close()
    except Exception as e:  # noqa: BLE001 — surface any spawn failure
        OPERATIONS.finish(op_id, "failed", f"spawn failed: {e}")
        return JSONResponse({"error": f"restart failed: {e}", "op_id": op_id},
                            status_code=500)

    # Give the command a short window to fail fast. If it is still running
    # after the window, the restart is genuinely in flight (and about to kill
    # this process) — that is the success path.
    def _probe():
        try:
            rc = proc.wait(timeout=1.2)
        except subprocess.TimeoutExpired:
            return None, b""
        err = b""
        try:
            err = log_path.read_bytes()[-2000:]
        except Exception:
            pass
        return rc, err

    rc, err_bytes = await asyncio.get_running_loop().run_in_executor(None, _probe)
    err_text = err_bytes.decode("utf-8", "replace").strip()

    if rc is not None and rc != 0:
        detail = f"systemctl exited {rc}" + (f": {err_text}" if err_text else "")
        OPERATIONS.finish(op_id, "failed", detail)
        return JSONResponse({"error": detail, "op_id": op_id}, status_code=500)

    if rc == 0:
        # systemctl returned success but this process is still alive — the
        # restarted unit may not be the one serving this page. The client's
        # boot_id poll settles it either way; disclose the ambiguity.
        OPERATIONS.stage(op_id, "applied",
                         "systemctl returned 0 while this process is still "
                         "alive — if the boot_id below never changes, "
                         "RADIO_SERVICE_NAME does not match this server's unit",
                         level="warn")
    else:
        OPERATIONS.stage(op_id, "detached",
                         "restart in flight; this process is about to be "
                         "replaced — the browser verifies via /health boot_id")
    return JSONResponse(
        {
            "message": f"restarting {RADIO_SERVICE_NAME}…",
            "op_id": op_id,
            "boot_id": health.BOOT_ID,
        },
        status_code=202,
    )


async def _broadcaster():
    """
    Polls acquirer.latest() at state.BROADCAST_FPS, serializes the frame once,
    and fans it out to all connected WebSocket clients. Also fans out server
    notices and structured operation events. Dropped connections are pruned.
    """
    interval   = 1.0 / max(state.BROADCAST_FPS, 1)
    last_t     = 0.0
    last_diag  = 0.0   # throttle the heartbeat log to ~once/sec

    while True:
        await asyncio.sleep(interval)

        if not _connections:
            # Drain event queues even with no viewers so they don't go stale.
            OPERATIONS.drain_events()
            _shared.drain_notices()
            continue

        texts = []
        # Queued server notices (compute-backstop reverts etc.) — P2a-3.
        for notice in _shared.drain_notices():
            texts.append(json.dumps({"message": f"[server] {notice}"}))
        # Structured operation stage events for the Operations tab.
        for ev in OPERATIONS.drain_events():
            texts.append(json.dumps({"op": _json_safe(ev)}))
        texts.append(json.dumps({"recording": _json_safe(_recording.status())}))
        for text in texts:
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
        # NOTE: mutate in place — rebinding the name would shadow the global.
        if dead:
            _connections.difference_update(dead)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """
    WebSocket endpoint. Receives control messages as text JSON:
        {"center": Hz, "sample_rate": Hz, "gain": dB, "nfft": int, "rows": int}
    Sends spectrogram frames as binary (see serialize_frame).
    """
    global _admin_ws
    # Role resolved by BasicAuthMiddleware and stashed on the ASGI scope. Falls
    # back to DEFAULT_ROLE (auth-disabled/demo) so a missing key never locks a
    # client out.
    role = ws.scope.get("role", DEFAULT_ROLE)

    # Viewers/interns are unlimited; only ONE admin may hold the slot at a time.
    # The check-and-set is under _slot_lock so two interleaving admin handshakes
    # can't both see the slot free. A busy refusal uses a distinct 4001 code (vs
    # 1008 for auth) so the client can tell "another admin connected" from
    # "unauthorized"; the browser's auto-retry then acts as a takeover queue.
    async with _slot_lock:
        if role == "admin" and _admin_ws is not None:
            await ws.accept()
            await ws.send_text(json.dumps(
                {"role": role, "auth_enabled": AUTH_ENABLED, "error": "admin-busy"}
            ))
            await ws.close(code=4001)
            print(f"[ws] refused extra admin (slot busy): {ws.client}")
            return
        await ws.accept()
        _connections.add(ws)
        if role == "admin":
            _admin_ws = ws
    # Tell the client its role immediately so app.js can enable/lock controls.
    # auth_enabled lets the UI hide the sign-out button in --demo / auth-off mode.
    await ws.send_text(json.dumps({"role": role, "auth_enabled": AUTH_ENABLED}))
    client = ws.client
    print(f"[ws] client connected: {client} (role={role})")
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
                # Role gate (defense in depth): read-only roles may never mutate
                # the shared config. The UI already blocks their controls, but a
                # crafted frame must be ignored here too. Stay connected so the
                # client keeps receiving live frames.
                if role not in WRITE_ROLES:
                    await ws.send_text(json.dumps(
                        {"message": "read-only role: control ignored", "denied": True}
                    ))
                    continue
                if _recording.active():
                    await ws.send_text(json.dumps(
                        {"message": "controls are locked while recording", "denied": True}
                    ))
                    continue
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
        # Free the admin slot only if this socket owned it (verify identity to
        # survive a takeover race). Under the lock so it can't clobber a fresh
        # admin that grabbed the slot between our break and here. The liveness
        # ping above doubles as dead-admin eviction, funnelling through here.
        if role == "admin":
            async with _slot_lock:
                if _admin_ws is ws:
                    _admin_ws = None
        print(f"[ws] client disconnected: {client} (role={role})")


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
    global _acquirer, _computer, _shared, _quantize, _recording

    parser = argparse.ArgumentParser(
        description="striqt WebSocket live viewer server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device",   default="air8201b",
                        help="SDR to drive: air8201b | pluto | soapy | demo | "
                             "auto (enumerate, need exactly one) | "
                             "driver=X[,serial=Y] (pick one of several)")
    parser.add_argument("--demo",     action="store_true",
                        help="Use synthetic IQ (no radio hardware); alias for "
                             "--device demo")
    # 1-4 channels: the frontend builds its panes/series from the header's
    # channel list (P3-4); 4 is just a sane demo ceiling, not a hard limit.
    parser.add_argument("--channels", type=int, default=None, choices=(1, 2, 3, 4),
                        help="use the first N RX channels (demo: creates N; "
                             "real devices: trims the discovered set)")
    parser.add_argument("--ports", default="auto",
                        help="explicit RX port list such as 0 or 0,1; "
                             "auto probes the device (profile fallback)")
    parser.add_argument("--quantize", action="store_true",
                        help="Encode waterfall as uint8 (~4x smaller frames)")
    parser.add_argument("--fps",      type=float, default=state.BROADCAST_FPS,
                        help="Max broadcast frame rate (fps)")
    parser.add_argument("--backend",  default=state.SPEC_BACKEND,
                        choices=sorted(BACKENDS),
                        help="Spectrogram backend")
    parser.add_argument("--host",     default="0.0.0.0",
                        help="Bind address")
    parser.add_argument("--port",     type=int, default=8000,
                        help="Listen port")
    args = parser.parse_args()

    # Resolve the device first (P3-1): --demo remains the historical alias and
    # may not contradict an explicit real --device choice.
    selector = args.device
    if args.demo:
        if selector not in ("air8201b", "demo"):
            parser.error(f"--demo conflicts with --device {selector}")
        selector = "demo"
    name, adapter = devices.resolve_device(selector)
    devices.set_adapter(adapter)

    # Channel plan: profile default → live discovery (every real device is
    # asked getNumChannels when reachable) → explicit --ports → --channels trim.
    is_demo = name == "demo"
    channels = None
    if args.ports != "auto":
        try:
            channels = tuple(dict.fromkeys(
                int(p.strip()) for p in args.ports.split(",") if p.strip()))
        except ValueError:
            parser.error("--ports must be 'auto' or a comma-separated integer list")
        if not channels or min(channels) < 0:
            parser.error("--ports must contain non-negative RX port numbers")
    elif not is_demo:
        channels = devices.probe_channels(name, adapter)   # None → profile
    state.configure_device(name, channels)
    state.set_device_label(adapter.label)
    if args.channels is not None:
        if is_demo:
            state.set_channels(tuple(range(args.channels)))
        else:
            have = state.CHANNELS
            if args.channels > len(have):
                parser.error(f"requested {args.channels} channels but the "
                             f"device has {have}")
            state.set_channels(have[:args.channels])

    if is_demo and not _ANALYSIS_OK and args.backend in CALIBRATED_GRID_BACKENDS:
        print("[demo] striqt.analysis unavailable; falling back to quicklook backend")
        state.set_backend("quicklook")
    else:
        state.set_backend(args.backend)

    if not is_demo and not _SENSOR_OK:
        print(
            "ERROR: striqt.sensor not importable (radio hardware deps missing).\n"
            "  Run with --demo for synthetic IQ, or install the striqt radio stack.",
            file=sys.stderr,
        )
        sys.exit(1)

    state.set_fps(args.fps)
    _quantize     = args.quantize
    _shared       = SharedConfig()
    if is_demo:
        # DemoAcquirer generates synthetic IQ and self-publishes — no DMA to
        # overflow, so it keeps the inline-compute path and needs no Computer.
        _acquirer = DemoAcquirer(_shared)
        _computer = None
    else:
        _acquirer = Acquirer(_shared)
        _computer = Computer(_acquirer, _shared)
    health.bind(_acquirer, _shared)
    _recording = RecordingManager(_acquirer, _shared, demo=is_demo)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn not installed. Run:\n  pip install 'uvicorn[standard]'",
            file=sys.stderr,
        )
        sys.exit(1)

    mode    = "DEMO (synthetic IQ)" if is_demo else f"{state.DEVICE_LABEL} radio"
    q_note  = " + uint8 quantization" if _quantize else ""
    print(f"\nstriqt web viewer — {mode}")
    print(f"  backend={state.SPEC_BACKEND}, fps={state.BROADCAST_FPS:.0f}{q_note}")
    print(f"  boot_id={health.BOOT_ID}")

    # Report auth status loudly so an unintentionally-open public server is obvious.
    if AUTH_DISABLED:
        print(
            "  auth:     *** WARNING: RADIO_AUTH_DISABLE=1 — auth DISABLED, "
            f"everyone gets role '{DEFAULT_ROLE}'. Do NOT use in production. ***"
        )
    else:
        print(f"  auth:     3-role Basic Auth ENABLED (roles: {', '.join(_ROLE_CREDS)})")
        _env_for = {"admin": "ADMIN", "viewer": "VIEWER", "interns": "INTERN"}
        using_defaults = any(
            os.environ.get(f"{p}_USER") is None or os.environ.get(f"{p}_PASS") is None
            for p in _env_for.values()
        )
        if using_defaults:
            print(
                "            *** WARNING: one or more roles use built-in DEFAULT "
                "passwords (visible in source). Override ADMIN/VIEWER/INTERN "
                "_USER/_PASS for production (setup.sh generates these). ***"
            )
        if not os.environ.get("RADIO_SESSION_SECRET"):
            print(
                "            *** WARNING: RADIO_SESSION_SECRET unset — cookie "
                "signing key is derived from (possibly default) credentials and "
                "may be forgeable. Set it for production. ***"
            )

    print(f"  listening on http://{args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  local:    http://localhost:{args.port}")
    print(
        f"  tunnel:   cloudflared tunnel --url http://localhost:{args.port}\n"
        f"            (or run:  bash live/run_web.sh --tunnel)\n"
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
