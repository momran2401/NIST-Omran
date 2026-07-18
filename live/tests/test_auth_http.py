"""Auth-enabled HTTP integration: roles actually gate the control surface.

Spawns a real demo server WITH authentication enabled and checks the wire
behavior: anonymous callers get only minimal health, viewers cannot mutate,
admins can, and the login redirect fires for pages.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[1]

ADMIN = ("qa-admin", "qa-admin-pass-1")
VIEWER = ("qa-viewer", "qa-viewer-pass-1")


def _basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _req(base, path, auth=None, payload=None):
    headers = {}
    if auth:
        headers["Authorization"] = _basic(*auth)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"null")
        except Exception:
            body = None
        return e.code, body, dict(e.headers)


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ,
               ADMIN_USER=ADMIN[0], ADMIN_PASS=ADMIN[1],
               VIEWER_USER=VIEWER[0], VIEWER_PASS=VIEWER[1],
               INTERN_USER="qa-intern", INTERN_PASS="qa-intern-pass-1",
               RADIO_SESSION_SECRET="qa-secret")
    env.pop("RADIO_AUTH_DISABLE", None)
    proc = subprocess.Popen(
        [sys.executable, str(LIVE / "striqt_web_server.py"),
         "--demo", "--backend", "quicklook", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                status, body, _ = _req(base, "/health")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("auth demo server never became healthy")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_anonymous_health_is_minimal(server):
    status, body, _ = _req(server, "/health")
    assert status == 200
    assert body["boot_id"]
    assert "device" not in body        # rich fields need a role


def test_anonymous_page_redirects_to_login(server):
    req = urllib.request.Request(server + "/", headers={"Accept": "text/html"})
    # Don't follow redirects: build an opener that surfaces the 303.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(req, timeout=5)
        raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 303
        assert e.headers.get("Location") == "/login"


def test_viewer_cannot_mutate(server):
    status, body, _ = _req(server, "/config", auth=VIEWER,
                           payload={"center": 2000e6})
    assert status == 403


def test_admin_can_mutate_and_gets_op_id(server):
    status, body, _ = _req(server, "/config", auth=ADMIN,
                           payload={"center": 2000e6})
    assert status == 200
    assert "center" in body["ack"]["applied"]
    assert body["ack"]["op_id"] is not None


def test_admin_health_is_rich(server):
    status, body, _ = _req(server, "/health", auth=ADMIN)
    assert status == 200
    assert body["device"]["name"] == "demo"
