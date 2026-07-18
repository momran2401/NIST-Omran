"""SharedConfig validation/clamping unit tests (fake radio — no hardware)."""
import pytest

from core import state
from core.config import RadioConfig, SharedConfig


@pytest.fixture()
def shared():
    state.configure_device("air8201b")
    return SharedConfig()


def test_center_maps_from_capture_block(shared):
    ack = shared.update({"capture": {"center_frequency": 1960e6}})
    assert "center" in ack["applied"]
    assert shared.snapshot().center == 1960e6


def test_center_clamped_to_envelope(shared):
    # The classic MHz-vs-Hz mistype: 1955 (Hz) clamps to the 300 MHz floor —
    # the server can only clamp; the UI now prevents this via MHz fields.
    ack = shared.update({"center": 1955.0})
    assert shared.snapshot().center == 300e6
    assert ack["applied"] == ["center"]

    shared.update({"center": 99e9})
    assert shared.snapshot().center == 6e9


def test_sample_rate_snaps_to_lte_grid(shared):
    shared.update({"sample_rate": 16e6})
    assert shared.snapshot().sample_rate == 15.36e6


def test_nfft_snaps_to_choices(shared):
    shared.update({"nfft": 1000})
    assert shared.snapshot().nfft == 1024


def test_gain_clamped(shared):
    shared.update({"gain": 99.0})
    assert shared.snapshot().gain == 10.0     # air8201b gain_max
    shared.update({"gain": -999.0})
    assert shared.snapshot().gain == -60.0


def test_ack_carries_op_id_and_no_op_for_noop(shared):
    ack = shared.update({"center": 2000e6})
    assert ack["op_id"] is not None
    # Same value again: no net change → no operation, nothing applied.
    ack2 = shared.update({"center": 2000e6})
    assert ack2["applied"] == []
    assert ack2["op_id"] is None


def test_take_dirty_returns_pending_op(shared):
    ack = shared.update({"center": 2100e6})
    dirty, cfg, op_id, reconnect = shared.take_dirty()
    assert dirty and cfg.center == 2100e6 and op_id == ack["op_id"]
    assert not reconnect
    # Consumed: second take is clean.
    dirty2, _, op_id2, _ = shared.take_dirty()
    assert not dirty2 and op_id2 is None


def test_source_overrides_apply_via_reconnect(shared):
    ack = shared.update({"source": {"calibration": "cal.json"}})
    assert "source.calibration" in ack["applied"]
    assert "calibration" in ack["reconnect"]
    dirty, cfg, _, reconnect = shared.take_dirty()
    assert dirty and reconnect
    assert cfg.source_config == {"calibration": "cal.json"}
    # Explicit null CLEARS the override back to the adapter default.
    ack2 = shared.update({"source": {"calibration": None}})
    assert "source.calibration" in ack2["applied"]
    assert shared.snapshot().source_config == {}
    # Re-clearing an absent key is a no-op, not a change.
    ack3 = shared.update({"source": {"calibration": None}})
    assert ack3["applied"] == []


def test_optional_capture_null_clears_to_default(shared):
    shared.update({"capture": {"analysis_bandwidth": 5e6, "lo_shift": "left"}})
    snap = shared.snapshot()
    assert snap.analysis_bandwidth == 5e6 and snap.lo_shift == "left"
    ack = shared.update({"capture": {"analysis_bandwidth": None,
                                     "lo_shift": None}})
    snap = shared.snapshot()
    assert snap.analysis_bandwidth == float("inf")
    assert snap.lo_shift == "none"
    assert set(ack["applied"]) >= {"analysis_bandwidth", "lo_shift"}


def test_lo_shift_validation(shared):
    shared.update({"capture": {"lo_shift": "left"}})
    assert shared.snapshot().lo_shift == "left"
    shared.update({"capture": {"lo_shift": "sideways"}})   # invalid → kept
    assert shared.snapshot().lo_shift == "left"


def test_unknown_capture_fields_reported_ignored(shared):
    ack = shared.update({"capture": {"center_frequency": 1900e6, "flux": 1}})
    assert "flux" in ack["ignored"]
    assert shared.snapshot().center == 1900e6


def test_analysis_keys_cannot_bypass_freedom_model(shared):
    before = shared.snapshot().window
    shared.update({"window": "boxcar"})     # top-level → stripped
    assert shared.snapshot().window == before


def test_pluto_envelope_differs(shared):
    state.configure_device("pluto")
    sc = SharedConfig()
    sc.update({"center": 100e6})
    assert sc.snapshot().center == 325e6    # pluto freq_min
    state.configure_device("air8201b")


def test_snapshot_is_isolated(shared):
    snap = shared.snapshot()
    snap.center = 1.0
    assert shared.snapshot().center != 1.0
