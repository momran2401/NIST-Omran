"""End-to-end demo pipeline: config change → frame echo → operation verdict.

Runs the real DemoAcquirer thread with the quicklook backend (no striqt
needed), which exercises the same SharedConfig/compute/publish/operations path
the hardware Acquirer uses.
"""
import time

import pytest

from core import health, state
from core.acquisition import DemoAcquirer
from core.config import SharedConfig
from core.operations import OPERATIONS


@pytest.fixture()
def demo():
    state.configure_device("demo")
    state.set_backend("quicklook")
    shared = SharedConfig()
    shared.update({"backend": "quicklook"})
    acq = DemoAcquirer(shared)
    health.bind(acq, shared)
    acq.start()
    yield shared, acq
    shared.stop()
    acq.join(timeout=3.0)


def wait_for(predicate, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = predicate()
        if v:
            return v
        time.sleep(0.05)
    return None


def test_frames_flow_and_echo_config(demo):
    shared, acq = demo
    hdr = wait_for(lambda: acq.latest()[0])
    assert hdr is not None, "no demo frame produced"
    assert hdr["channels"] == [0, 1]
    assert hdr["center"] == shared.snapshot().center


def test_center_change_verified_by_frame_and_op(demo):
    shared, acq = demo
    assert wait_for(lambda: acq.latest()[0])
    ack = shared.update({"center": 1962.5e6})
    op_id = ack["op_id"]
    assert op_id is not None

    op = wait_for(lambda: (
        (o := OPERATIONS.get(op_id)) and o["state"] != "running" and o) or None)
    assert op, "operation never completed"
    assert op["state"] == "success"
    stages = [s["stage"] for s in op["stages"]]
    assert "data-path" in stages, stages

    hdr = wait_for(lambda: (
        (h := acq.latest()[0]) and h["center"] == 1962.5e6 and h) or None)
    assert hdr, "frame header never echoed the new center"


def test_health_reports_ok_once_streaming(demo):
    shared, acq = demo
    assert wait_for(lambda: acq.latest()[0])
    snap = health.health_snapshot()
    assert snap["status"] in ("ok", "starting")
    assert snap["boot_id"]
    assert snap["device"]["name"] == "demo"
