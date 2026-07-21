#!/usr/bin/env python3
"""
radioctl — SSH-friendly client for the running radio-web backend.

Talks HTTP to the same server the browser uses, so every change goes through
the identical validated + hardware-verified pipeline (operation IDs, driver
readback, fresh-frame confirmation).

Examples:
    python3 live/radioctl.py status
    python3 live/radioctl.py watch
    python3 live/radioctl.py logs
    python3 live/radioctl.py set --center-mhz 2593 --gain 5
    python3 live/radioctl.py set --json '{"analysis":{"target":"psd","time_statistic":"mean,max"}}'
    python3 live/radioctl.py self-test          # reversible on-radio settings qual

Auth: --user/--password, or RADIOCTL_USER / RADIOCTL_PASSWORD env vars
(the password is prompted when a user is given without one). Local
RADIO_AUTH_DISABLE=1 servers need no credentials.
"""

import argparse
import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

PASS_STATES = {"success", "verified"}
WARN_STATES = {"unverified"}


class Client:
    def __init__(self, base, user=None, password=None):
        self.base = base.rstrip("/")
        self.auth = None
        if user:
            raw = "{}:{}".format(user, password or "").encode("utf-8")
            self.auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def _headers(self, extra=None):
        headers = dict(extra or {})
        if self.auth:
            headers["Authorization"] = self.auth
        return headers

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=6) as response:
            return json.load(response)

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)


def print_status(client):
    health = client.get("/health")
    config = client.get("/config")
    cap = config["capture"]
    dev = config["device"]
    print("device       : {}  channels={}".format(dev["label"], dev["channels"]))
    print("service      : {}  boot={}  up {:.0f}s".format(
        health["status"], str(health["boot_id"])[:10], health.get("uptime_s") or 0))
    radio = health.get("radio")
    if radio:
        print("radio        : open={}  healthy={}  ring={:.0%}".format(
            radio["open"], radio["healthy"], radio.get("ring_fill") or 0))
    age = health.get("last_frame_age_s")
    print("last frame   : {}".format(f"{age:.2f} s ago" if age is not None else "none yet"))
    print("capture      : {:.6f} MHz  {:.4f} MS/s  gain={:.2f} dB  nfft={}".format(
        cap["center_frequency"] / 1e6, cap["sample_rate"] / 1e6,
        cap["gain"], cap["nfft"]))
    print("analysis     : {}  rows={}".format(config["backend"], config["rows"]))
    if config.get("source"):
        print("source ovr   : {}".format(config["source"]))
    last = health.get("last_operation")
    if last:
        print("last op      : #{} {} → {} ({})".format(
            last["id"], last["kind"], last["state"], last["summary"]))


def wait_operation(client, op_id, timeout=30.0):
    """Poll /operations until op_id reaches a terminal state; return the op."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for op in client.get("/operations")["operations"]:
            if op["id"] == op_id and op["state"] != "running":
                return op
        time.sleep(0.4)
    raise RuntimeError("operation #{} did not finish within {:.0f} s".format(
        op_id, timeout))


def stream_logs(client, interval=1.0):
    """Incrementally print operation stages as they happen."""
    seen = {}   # op_id -> stages printed
    while True:
        for op in client.get("/operations")["operations"]:
            start = seen.get(op["id"], 0)
            for stage in op["stages"][start:]:
                stamp = time.strftime("%H:%M:%S", time.localtime(stage["t"]))
                print("[{}] op#{:<4} {:10} {}".format(
                    stamp, op["id"], stage["stage"], stage["detail"]), flush=True)
            seen[op["id"]] = len(op["stages"])
        time.sleep(interval)


def apply_and_wait(client, name, payload, timeout=30.0):
    print("TEST {:22} {}".format(name, json.dumps(payload, sort_keys=True)))
    result = client.post("/config", payload)
    ack = result.get("ack", {})
    if ack.get("rejected"):
        raise RuntimeError("rejected: {}".format(ack["rejected"]))
    op_id = ack.get("op_id")
    if op_id is None:
        print("SKIP {:22} value produced no change".format(name))
        return "success"
    op = wait_operation(client, op_id, timeout)
    verdict = op["state"]
    tag = ("PASS" if verdict in PASS_STATES
           else "WARN" if verdict in WARN_STATES else "FAIL")
    print("{} {:22} op #{} → {}".format(tag, name, op_id, verdict.upper()))
    if tag == "FAIL":
        raise RuntimeError("op #{} finished {}".format(op_id, verdict))
    return verdict


def self_test(client, timeout=30.0):
    """
    Exercise every portable live control through the verified pipeline, then
    restore the starting recipe. Source/clock settings are deliberately
    excluded — there is no universally safe alternate value without knowing
    what is physically cabled; they still verify when changed explicitly.
    """
    config = client.get("/config")
    cap, env = config["capture"], config["envelope"]
    center = float(cap["center_frequency"])
    step = 1e6 if center + 1e6 <= env["freq_max"] else -1e6
    # Prefer a higher LTE-grid rate: AIR-T's current CV firmware accepts
    # 15.36/30.72 MS/s but rejects the nominal lower grid points.
    rates = [r for r in (30.72e6, 15.36e6, 7.68e6, 3.84e6)
             if env["rate_min"] <= r <= env["rate_max"]
             and r != cap["sample_rate"]]
    if env["gain_min"] < 0 and cap["gain"] <= 0:
        # AIR-T calibrated gain is attenuation-like; positive values in the
        # broad profile envelope are rejected by this firmware.
        alt_gain = max(env["gain_min"], cap["gain"] - 1.0)
    else:
        alt_gain = (min(env["gain_max"], cap["gain"] + 1.0)
                    if cap["gain"] + 1.0 <= env["gain_max"]
                    else max(env["gain_min"], cap["gain"] - 1.0))
    alt_nfft = next(n for n in (256, 512, 1024, 2048, 4096)
                    if n != int(cap["nfft"]))

    cases = [("center frequency", {"center": center + step}),
             ("gain", {"gain": alt_gain}),
             ("FFT size", {"nfft": alt_nfft}),
             ("frame rows", {"rows": int(config["rows"]) + 1}),
             ("LO-null toggle", {"lo_null": not bool(config["lo_null"])})]
    if rates:
        cases.insert(1, ("sample rate", {"sample_rate": rates[0]}))
    if config["backend"] != "quicklook":
        # quicklook always exists, even without the striqt analysis stack.
        cases.append(("analysis backend", {"backend": "quicklook"}))

    restore = {
        "capture": {"center_frequency": cap["center_frequency"],
                    "sample_rate": cap["sample_rate"],
                    "gain": cap["gain"], "nfft": cap["nfft"]},
        "rows": config["rows"],
        "backend": config["backend"],
        "lo_null": config["lo_null"],
    }
    failures = []
    try:
        for name, payload in cases:
            try:
                apply_and_wait(client, name, payload, timeout)
            except Exception as exc:
                failures.append((name, str(exc)))
                print("FAIL {:22} {}".format(name, exc), file=sys.stderr)
    finally:
        print("RESTORE                restoring the starting configuration")
        try:
            apply_and_wait(client, "starting config", restore, timeout)
        except Exception as exc:
            failures.append(("restore", str(exc)))
            print("FAIL restore            {}".format(exc), file=sys.stderr)

    if failures:
        print("\n{} self-test failure(s):".format(len(failures)), file=sys.stderr)
        for name, reason in failures:
            print("  {}: {}".format(name, reason), file=sys.stderr)
        return 1
    print("\nAll portable settings verified through the live pipeline.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="control/inspect the running radio-web backend")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default=os.environ.get("RADIOCTL_USER"))
    parser.add_argument("--password", default=os.environ.get("RADIOCTL_PASSWORD"),
                        help="prefer the prompt or RADIOCTL_PASSWORD over this flag")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=1.0)
    logs = sub.add_parser("logs")
    logs.add_argument("--interval", type=float, default=1.0)
    check = sub.add_parser("self-test")
    check.add_argument("--timeout", type=float, default=30.0)
    setting = sub.add_parser("set")
    setting.add_argument("--center-mhz", type=float)
    setting.add_argument("--rate-msps", type=float)
    setting.add_argument("--gain", type=float)
    setting.add_argument("--nfft", type=int)
    setting.add_argument("--backend",
                         choices=("calibrated", "quicklook", "psd", "ssb"))
    setting.add_argument("--json", help="raw control payload (merged last)")
    args = parser.parse_args()

    if args.user and args.password is None:
        args.password = getpass.getpass(
            "Radio password for {}: ".format(args.user))
    client = Client(args.url, args.user, args.password)

    if args.command == "status":
        print_status(client)
    elif args.command == "watch":
        while True:
            print("\033[2J\033[H", end="")
            print_status(client)
            time.sleep(args.interval)
    elif args.command == "logs":
        stream_logs(client, args.interval)
    elif args.command == "self-test":
        return self_test(client, args.timeout)
    else:  # set
        payload = json.loads(args.json) if args.json else {}
        capture = {}
        if args.center_mhz is not None:
            capture["center_frequency"] = args.center_mhz * 1e6
        if args.rate_msps is not None:
            capture["sample_rate"] = args.rate_msps * 1e6
        if args.gain is not None:
            capture["gain"] = args.gain
        if args.nfft is not None:
            capture["nfft"] = args.nfft
        if capture:
            payload.setdefault("capture", {}).update(capture)
        if args.backend:
            payload["backend"] = args.backend
        if not payload:
            print("nothing to set (see --help)", file=sys.stderr)
            return 2
        return 0 if apply_and_wait(client, "set", payload) else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print("radioctl: HTTP {} {}".format(exc.code, body), file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, RuntimeError) as exc:
        print("radioctl: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
