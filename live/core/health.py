"""Process health model.

BOOT_ID is minted once per process — the browser's Reset Radio verification
polls /health until it sees a DIFFERENT boot_id, which is proof the service
actually restarted (a 202 from the old process proves nothing). The snapshot
also reports radio/stream/frame liveness so the Operations tab can show
service health without guessing from frame arrival.
"""
from __future__ import annotations

import time
import uuid

from . import state
from .operations import OPERATIONS

BOOT_ID = uuid.uuid4().hex
STARTED_AT = time.time()

_acquirer = None
_shared = None


def bind(acquirer, shared):
    """Called once by the frontend after building the acquisition stack."""
    global _acquirer, _shared
    _acquirer = acquirer
    _shared = shared


def health_snapshot():
    now = time.time()
    out = {
        "status": "starting",
        "boot_id": BOOT_ID,
        "started_at": STARTED_AT,
        "uptime_s": round(now - STARTED_AT, 3),
        "device": {
            "name": state.DEVICE,
            "label": state.DEVICE_LABEL,
            "channels": list(state.CHANNELS),
        },
        "radio": None,
        "last_frame_age_s": None,
        "last_operation": OPERATIONS.last_terminal(),
    }
    if _acquirer is None:
        return out

    ring = getattr(_acquirer, "ring_status", lambda: None)()
    if ring is not None:
        out["radio"] = ring

    header = None
    try:
        header, _ = _acquirer.latest()
    except Exception:
        pass
    if header is not None and header.get("time"):
        out["last_frame_age_s"] = round(now - float(header["time"]), 3)

    if out["last_frame_age_s"] is not None and out["last_frame_age_s"] < 5.0:
        out["status"] = "ok"
    elif now - STARTED_AT < 10.0:
        out["status"] = "starting"
    else:
        out["status"] = "degraded"
    return out
