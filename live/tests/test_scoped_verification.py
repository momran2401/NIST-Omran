"""Field-scoped readback + superseded-state tests."""
import numpy as np

from core import state
from core.acquisition import Acquirer
from core.config import SharedConfig
from core.operations import OPERATIONS


def make_acquirer():
    state.configure_device("demo")   # 2-channel profile; no hardware touched
    shared = SharedConfig()
    return shared, Acquirer(shared)   # never started — unit-level only


def test_display_only_op_skips_readback_entirely():
    _, acq = make_acquirer()
    op = OPERATIONS.begin("config", "rows → 40")
    OPERATIONS.set_fields(op, ["rows"])
    # No device/source access must happen: _readback_and_verify returns
    # "success" before ever calling the adapter.
    verdict = acq._readback_and_verify(object(), op)
    assert verdict == "success"
    stages = [s["stage"] for s in OPERATIONS.get(op)["stages"]]
    assert "readback" in stages
    detail = next(s["detail"] for s in OPERATIONS.get(op)["stages"]
                  if s["stage"] == "readback")
    assert "not applicable" in detail


def test_fields_recorded_on_config_ops():
    state.configure_device("air8201b")
    shared = SharedConfig()
    ack = shared.update({"center": 2000e6, "rows": 24})
    fields = OPERATIONS.fields(ack["op_id"])
    assert set(fields) == {"center", "rows"}
    state.configure_device("demo")


def test_unknown_fields_mean_full_check():
    _, acq = make_acquirer()
    op = OPERATIONS.begin("radio", "open")   # no set_fields → full recipe
    assert OPERATIONS.fields(op) is None


def test_superseded_is_a_distinct_terminal_state():
    state.configure_device("air8201b")
    shared = SharedConfig()
    a1 = shared.update({"center": 1900e6})
    a2 = shared.update({"center": 1910e6})   # supersedes op 1 before hardware
    op1 = OPERATIONS.get(a1["op_id"])
    assert op1["state"] == "superseded"
    op2 = OPERATIONS.get(a2["op_id"])
    assert op2["state"] == "running"
    state.configure_device("demo")
