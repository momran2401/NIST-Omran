"""Operation log + adapter verification contract tests."""
from core import state
from core.devices.base import DeviceAdapter
from core.operations import OperationLog, fmt_value, verdict_state


def test_operation_lifecycle():
    log = OperationLog()
    op = log.begin("config", "center → 2000 MHz")
    log.stage(op, "applying", "rearm")
    log.finish(op, "verified", "readback matched")
    rec = log.get(op)
    assert rec["state"] == "verified"
    stages = [s["stage"] for s in rec["stages"]]
    assert stages == ["requested", "applying", "VERIFIED"]
    assert log.last_terminal()["id"] == op


def test_events_drain_once():
    log = OperationLog()
    op = log.begin("reset", "restart")
    log.finish(op, "failed", "boom")
    events = log.drain_events()
    assert [e["stage"] for e in events] == ["requested", "FAILED"]
    assert log.drain_events() == []


def test_unknown_op_id_tolerated():
    log = OperationLog()
    log.stage(9999, "applying", "x")     # must not raise
    log.finish(9999, "verified")


def test_verdict_state_collapse():
    v = lambda s: {"state": s}
    assert verdict_state([v("verified"), v("verified")]) == "verified"
    assert verdict_state([v("verified"), v("mismatch")]) == "mismatch"
    assert verdict_state([v("readback_unsupported")]) == "unverified"
    assert verdict_state([v("verified"), v("readback_unsupported")]) == "verified"


def test_fmt_value_units():
    assert fmt_value("center", 1955e6) == "1955 MHz"
    assert fmt_value("sample_rate", 15.36e6) == "15.36 MS/s"
    assert fmt_value("gain", -3) == "-3.0 dB"
    assert fmt_value("rows", 12) == "12"


class FakeAdapter(DeviceAdapter):
    """Contract-test adapter with scripted readback answers."""
    name = "demo"   # reuse the demo profile for envelope data

    def __init__(self, actuals):
        super().__init__()
        self._actuals = actuals

    def create_source(self):
        return object()

    def read_back(self, source, cfg):
        return self._actuals


class Cfg:
    center = 1955e6
    sample_rate = 15.36e6
    gain = 0.0


def test_adapter_verify_verified(monkeypatch):
    state.configure_device("demo")
    a = FakeAdapter({"center": 1955e6 + 1.0, "sample_rate": 15.36e6,
                     "gain": [0.2, -0.2]})
    verdicts = a.verify(Cfg(), a.read_back(None, Cfg()))
    assert verdict_state(verdicts) == "verified"


def test_adapter_verify_mismatch():
    state.configure_device("demo")
    a = FakeAdapter({"center": 1955e6 + 5e5, "sample_rate": 15.36e6,
                     "gain": [0.0, 0.0]})
    verdicts = a.verify(Cfg(), a.read_back(None, Cfg()))
    by_field = {v["field"]: v["state"] for v in verdicts}
    assert by_field["center"] == "mismatch"
    assert verdict_state(verdicts) == "mismatch"


def test_identify_deepwave_models():
    from core.devices import identify_deepwave
    assert identify_deepwave({"label": "AIR-7101B serial 1"}) == "air7101b"
    assert identify_deepwave({"hardware": "AIR 7201B"}) == "air7201b"
    assert identify_deepwave({"label": "Deepwave AIR8201B"}) == "air8201b"
    assert identify_deepwave({"driver": "SoapyAIRT"}) == "air8201b"


def test_verify_uses_striqt_lo_and_backend_rate_expectations():
    # An intentional lo_shift offsets the hardware LO, and backend_sample_rate
    # runs the SDR at a different rate — neither may read as a mismatch.
    state.configure_device("demo")

    class LoCfg(Cfg):
        lo_shift = "left"
        backend_sample_rate = 30.72e6

    class FakeSource:
        def get_resampler(self, capture):
            return {"lo_offset": 3.84e6, "fs_sdr": 30.72e6}

    a = FakeAdapter({"center": Cfg.center - 3.84e6, "sample_rate": 30.72e6,
                     "gain": [0.0, 0.0]})
    expected = a.hardware_expectations(FakeSource(), object(), LoCfg())
    assert expected["center"] == Cfg.center - 3.84e6
    assert expected["sample_rate"] == 30.72e6
    verdicts = a.verify(LoCfg(), a.read_back(None, LoCfg()), expected)
    assert verdict_state(verdicts) == "verified"
    # Without the expectation correction the same readback would mismatch.
    naive = a.verify(LoCfg(), a.read_back(None, LoCfg()))
    assert verdict_state(naive) == "mismatch"


def test_adapter_verify_unsupported():
    state.configure_device("demo")
    a = FakeAdapter({"center": None, "sample_rate": None, "gain": [None, None]})
    verdicts = a.verify(Cfg(), a.read_back(None, Cfg()))
    assert verdict_state(verdicts) == "unverified"
