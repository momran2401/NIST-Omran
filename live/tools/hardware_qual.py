#!/usr/bin/env python3
"""
Hardware qualification: prove every radio setting ACTUALLY applies.

Run ON the radio host, against real hardware:

    python3 live/tools/hardware_qual.py                     # AIR8201B
    python3 live/tools/hardware_qual.py --device pluto
    python3 live/tools/hardware_qual.py --device auto
    python3 live/tools/hardware_qual.py --quick             # fewer points

For each test point it applies the setting through the SAME validated path the
UI uses (SharedConfig.update), then requires:
  1. the operation to reach a terminal state (hardware apply + readback ran),
  2. driver readback to match within adapter tolerance (VERIFIED) — or the
     adapter to declare readback unsupported (UNVERIFIED, reported as such),
  3. a fresh frame whose header echoes the applied value (data-path proof).

Exit code 0 only when no test point FAILED or MISMATCHED. "unverified" points
are warnings (driver can't answer), not failures.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import devices, health, state                       # noqa: E402
from core.acquisition import Acquirer, Computer, DemoAcquirer  # noqa: E402
from core.config import SharedConfig                           # noqa: E402
from core.operations import OPERATIONS                         # noqa: E402
from core.striqt_compat import _SENSOR_OK                      # noqa: E402


def wait_for(predicate, timeout, poll=0.1):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = predicate()
        if v:
            return v
        time.sleep(poll)
    return None


def run_point(shared, acquirer, field, value, header_key, timeout):
    """Apply one setting; return (state, detail)."""
    ack = shared.update({field: value})
    if ack["rejected"]:
        return "failed", f"rejected: {ack['rejected']}"
    op_id = ack["op_id"]
    if op_id is None:
        return "success", "no net change (already at this value)"

    op = wait_for(lambda: (
        (o := OPERATIONS.get(op_id)) and o["state"] != "running" and o) or None,
        timeout)
    if not op:
        return "failed", "operation never reached a terminal state"

    applied = shared.snapshot()
    want = getattr(applied, field)
    hdr = wait_for(lambda: (
        (h := acquirer.latest()[0])
        and abs(float(h.get(header_key, float("nan"))) - float(want)) < 1e-3
        and h) or None, timeout)
    if not hdr:
        return "failed", (f"op finished '{op['state']}' but no frame echoed "
                          f"{header_key}={want}")
    return op["state"], f"applied {want}, frame echoed, op {op['state']}"


def main():
    parser = argparse.ArgumentParser(description="on-radio settings qualification")
    parser.add_argument("--device", default="air8201b")
    parser.add_argument("--demo", action="store_true",
                        help="dry-run the harness against the synthetic source")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="per-point timeout (s)")
    args = parser.parse_args()

    selector = "demo" if args.demo else args.device
    name, adapter = devices.resolve_device(selector)
    devices.set_adapter(adapter)
    state.configure_device(name)
    state.set_device_label(adapter.label)
    is_demo = name == "demo"
    if not is_demo and not _SENSOR_OK:
        print("ERROR: striqt.sensor not importable on this host", file=sys.stderr)
        sys.exit(2)
    if is_demo:
        state.set_backend("quicklook")

    shared = SharedConfig()
    if is_demo:
        shared.update({"backend": "quicklook"})
        acquirer, computer = DemoAcquirer(shared), None
    else:
        acquirer = Acquirer(shared)
        computer = Computer(acquirer, shared)
    health.bind(acquirer, shared)
    acquirer.start()
    if computer is not None:
        computer.start()

    print(f"\n=== hardware qualification: {state.DEVICE_LABEL} "
          f"(channels {state.CHANNELS}) ===\n")
    if not wait_for(lambda: acquirer.latest()[0], args.timeout * 2):
        print("FAILED: no first frame — radio never streamed", file=sys.stderr)
        sys.exit(1)

    env = shared.envelope()
    centers = [1955e6, 2155e6, 751e6, 3550e6]
    if args.quick:
        centers = centers[:2]
    centers = [c for c in centers if env["freq_min"] <= c <= env["freq_max"]]
    rates = [3.84e6, 15.36e6] if not args.quick else [15.36e6]
    rates = [r for r in rates if env["rate_min"] <= r <= env["rate_max"]]
    gains = [env["gain_min"], min(env["gain_max"], env["gain_min"] + 10)]

    points = ([("center", c, "center") for c in centers]
              + [("sample_rate", r, "fs") for r in rates]
              + [("gain", g, "gain") for g in gains]
              + [("center", 1955e6, "center"),          # return to defaults
                 ("sample_rate", 15.36e6 if 15.36e6 <= env["rate_max"] else rates[0], "fs")])

    results = []
    for field, value, hkey in points:
        label = f"{field} = {value/1e6:.4g} M" if value > 1e4 else f"{field} = {value:g}"
        print(f"→ {label}")
        verdict, detail = run_point(shared, acquirer, field, value, hkey,
                                    args.timeout)
        print(f"   {verdict.upper()}: {detail}\n")
        results.append((label, verdict, detail))

    # Sustained streaming check after all changes.
    hdr0 = acquirer.latest()[0]
    time.sleep(3.0)
    hdr1 = acquirer.latest()[0]
    streaming = hdr1 and hdr0 and hdr1["time"] > hdr0["time"]
    results.append(("sustained streaming after all changes",
                    "success" if streaming else "failed",
                    "frames still advancing" if streaming else "stream stalled"))

    print("=== summary ===")
    bad = unverified = 0
    for label, verdict, _ in results:
        mark = {"verified": "✓", "success": "✓", "unverified": "~",
                "mismatch": "✗", "failed": "✗"}.get(verdict, "?")
        if verdict in ("mismatch", "failed"):
            bad += 1
        elif verdict == "unverified":
            unverified += 1
        print(f"  {mark} {verdict.upper():10s} {label}")
    print(f"\n{len(results) - bad}/{len(results)} points OK"
          + (f" ({unverified} unverified — driver gave no readback)"
             if unverified else ""))

    shared.stop()
    acquirer.join(timeout=3.0)
    if computer is not None:
        computer.join(timeout=3.0)
    # Exit contract: 0 = all verified; 1 = mismatch/failure;
    # 2 = applied but required readback unsupported on REAL hardware
    # (demo has no readback by design and stays exit 0).
    if bad:
        sys.exit(1)
    if unverified and not is_demo:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
