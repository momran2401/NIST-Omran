"""Frame wire format: [4-byte LE header length][JSON header][blocks...].

serialize_frame packs a frame for the browser (float32 or quantized uint8);
parse_frame is the inverse, for Python clients and tests.
"""
from __future__ import annotations

import json
import struct

import numpy as np

# ---------------------------------------------------------------------------
# Frame serialization (browser-friendly binary WebSocket message)
# ---------------------------------------------------------------------------

def serialize_frame(header: dict, blocks: list, quantize: bool = False) -> bytes:
    """
    Pack a complete spectrogram frame into a single binary WebSocket message:

        [4-byte LE uint32 : header JSON byte length]
        [UTF-8 JSON header bytes]
        [block-0 raw bytes]   (float32 LE, or uint8 if quantize=True)
        [block-1 raw bytes]
        ...

    With quantize=True the header gains:
        "dtype": "uint8"
        "scale": [vmin_dB, vmax_dB]
    and each block is a uint8 array (0=vmin, 255=vmax). ~4× smaller payload.
    PSD accuracy is unaffected because the browser recomputes PSD from the
    dequantized blocks, which differ from float32 by at most 1/255 of the dB range.
    """
    if quantize and blocks:
        # Use per-frame global range so quantization is consistent across channels.
        # NaN-safe: a single NaN would make np.percentile return NaN and turn the
        # whole uint8 frame to garbage (LV-R4).
        all_vals = np.concatenate([b.ravel() for b in blocks])
        vmin = float(np.nanpercentile(all_vals, 1))
        vmax = float(np.nanpercentile(all_vals, 99))
        if not (np.isfinite(vmin) and np.isfinite(vmax)):
            vmin, vmax = -100.0, 0.0   # all-NaN block fallback
        if vmax - vmin < 1.0:
            vmax = vmin + 1.0
        hdr       = dict(header, dtype="uint8", scale=[vmin, vmax])
        hdr_bytes = json.dumps(hdr).encode("utf-8")
        parts     = [struct.pack("<I", len(hdr_bytes)), hdr_bytes]
        rng       = vmax - vmin
        for block in blocks:
            u8 = ((np.nan_to_num(np.asarray(block, dtype=np.float32), nan=vmin) - vmin) / rng * 255
                  ).clip(0, 255).astype(np.uint8)
            parts.append(u8.tobytes(order="C"))
    else:
        hdr_bytes = json.dumps(header).encode("utf-8")
        parts     = [struct.pack("<I", len(hdr_bytes)), hdr_bytes]
        for block in blocks:
            parts.append(np.asarray(block, dtype=np.float32, order="C").tobytes())
    return b"".join(parts)


def parse_frame(payload: bytes):
    """
    Inverse of serialize_frame: unpack one binary frame message into
    (header_dict, [np.ndarray blocks]). Dequantizes uint8 frames back to
    float32 dB using the header's scale, so callers always see dB blocks.
    Raises ValueError on a malformed payload.
    """
    if len(payload) < 4:
        raise ValueError("frame shorter than the 4-byte header-length prefix")
    (hdr_len,) = struct.unpack("<I", payload[:4])
    if len(payload) < 4 + hdr_len:
        raise ValueError("frame shorter than its declared header length")
    header = json.loads(payload[4:4 + hdr_len].decode("utf-8"))
    rows, bins = (int(v) for v in header["shape"])
    channels = header.get("channels") or [0]
    body = payload[4 + hdr_len:]

    blocks = []
    if header.get("dtype") == "uint8":
        vmin, vmax = (float(v) for v in header["scale"])
        block_bytes = rows * bins
        for i in range(len(channels)):
            raw = np.frombuffer(
                body, dtype=np.uint8, count=block_bytes, offset=i * block_bytes
            ).reshape(rows, bins)
            blocks.append(
                (raw.astype(np.float32) / 255.0 * (vmax - vmin) + vmin)
            )
    else:
        block_bytes = rows * bins * 4
        for i in range(len(channels)):
            blocks.append(np.frombuffer(
                body, dtype="<f4", count=rows * bins, offset=i * block_bytes
            ).reshape(rows, bins).copy())
    return header, blocks
