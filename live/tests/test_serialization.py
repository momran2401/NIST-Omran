"""Frame wire-format round-trip tests."""
import numpy as np

from core.serialization import parse_frame, serialize_frame


def _frame(rows=6, bins=128, channels=(0, 1)):
    rng = np.random.default_rng(7)
    blocks = [rng.normal(-60, 15, (rows, bins)).astype(np.float32)
              for _ in channels]
    header = {"center": 1955e6, "fs": 15.36e6, "gain": 0.0,
              "nfft": bins, "rows": rows, "shape": [rows, bins],
              "channels": list(channels), "time": 123.0}
    return header, blocks


def test_float32_roundtrip():
    header, blocks = _frame()
    h2, b2 = parse_frame(serialize_frame(header, blocks))
    assert h2["center"] == header["center"]
    assert len(b2) == 2
    assert np.allclose(b2[0], blocks[0])
    assert np.allclose(b2[1], blocks[1])


def test_quantized_roundtrip_within_tolerance():
    header, blocks = _frame()
    h2, b2 = parse_frame(serialize_frame(header, blocks, quantize=True))
    assert h2["dtype"] == "uint8"
    vmin, vmax = h2["scale"]
    # Max quantization error is one LSB of the encoded range.
    tol = (vmax - vmin) / 255 + 1e-6
    clipped = np.clip(blocks[0], vmin, vmax)
    assert np.max(np.abs(b2[0] - clipped)) <= tol


def test_single_channel_frame():
    header, blocks = _frame(channels=(0,))
    h2, b2 = parse_frame(serialize_frame(header, blocks))
    assert h2["channels"] == [0]
    assert len(b2) == 1


def test_malformed_payload_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_frame(b"\x01")
