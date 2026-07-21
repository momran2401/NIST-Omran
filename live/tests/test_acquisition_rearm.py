"""Same-device rearm lifecycle tests (no hardware required)."""

from core import acquisition, state
from core.acquisition import Acquirer
from core.config import SharedConfig


class FakeSource:
    def __init__(self, calls):
        self.calls = calls

    def arm_spec(self, _capture):
        self.calls.append("arm")


def test_rearm_releases_existing_rx_stream_before_open(monkeypatch):
    state.configure_device("air8201b")
    acquirer = Acquirer(SharedConfig())
    calls = []
    acquirer.source = FakeSource(calls)

    monkeypatch.setattr(acquisition, "enable_stream", lambda _s, on: calls.append("enable" if on else "disable"))
    monkeypatch.setattr(acquisition, "_close_rx_stream", lambda _s: calls.append("close-rx"))
    monkeypatch.setattr(acquisition, "open_stream", lambda _s: calls.append("open-rx"))
    monkeypatch.setattr(acquirer, "_readback_and_verify", lambda _cfg, _op: "success")
    monkeypatch.setattr(acquirer, "_arm_verification", lambda _op, _state: None)

    acquirer.rearm(acquirer.shared.snapshot(), op_id=123)

    assert calls == ["disable", "close-rx", "open-rx", "arm", "enable"]


def test_air_t_recovery_never_closes_process_lifetime_source(monkeypatch):
    state.configure_device("air8201b")
    acquirer = Acquirer(SharedConfig())
    source = FakeSource([])
    acquirer.source = source
    calls = []

    monkeypatch.setattr(acquisition, "close_source", lambda _s: calls.append("close-source"))
    monkeypatch.setattr(acquisition.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(acquirer, "rearm", lambda _cfg: calls.append("rearm"))
    monkeypatch.setattr(acquirer, "_make_read_buffers", lambda: (1, None, []))

    acquirer._recover(acquirer.shared.snapshot(), "transient DMA error")

    assert calls == ["rearm"]
    assert acquirer.source is source
