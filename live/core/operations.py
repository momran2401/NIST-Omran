"""Structured operation log: every radio-affecting action is an Operation.

An Operation is a sequence of timestamped stages ending in a verdict:

    requested → validated → applying → applied → readback → data-path →
    SUCCESS | VERIFIED | MISMATCH | UNVERIFIED | FAILED

Stages print to the terminal as they happen ("[op #7] readback: center
1955.000 MHz") AND are queued as structured events for the frontends (the web
server drains drain_events() into WebSocket {"op": ...} messages; the
Operations tab renders them). The ring keeps the most recent operations for
the /operations endpoint, so a client that connects late still sees history.

Terminal verdict states:
    success      completed; no hardware verification applicable (e.g. rows)
    verified     hardware readback matched the request within tolerance
    unverified   applied, but the driver could not answer a readback query
    mismatch     hardware readback disagreed with the request
    failed       the operation itself errored (arm failed, restart failed...)
    superseded   a newer operation replaced this one before it completed
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

TERMINAL_STATES = {"success", "verified", "unverified", "mismatch", "failed",
                   "superseded"}


class OperationLog:
    def __init__(self, keep: int = 200):
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._ops = deque(maxlen=keep)          # completed + running op dicts
        self._by_id = {}
        self._events = []                       # queued events for broadcast

    # -- lifecycle ---------------------------------------------------------

    def begin(self, kind: str, summary: str) -> int:
        op_id = next(self._seq)
        op = {
            "id": op_id,
            "kind": str(kind),
            "summary": str(summary),
            "state": "running",
            "t_start": time.time(),
            "t_end": None,
            "stages": [],
        }
        with self._lock:
            self._ops.append(op)
            self._by_id[op_id] = op
            # Trim the id index alongside the deque.
            while len(self._by_id) > self._ops.maxlen:
                oldest = min(self._by_id)
                self._by_id.pop(oldest, None)
        self.stage(op_id, "requested", summary)
        return op_id

    def stage(self, op_id, stage: str, detail: str = "", level: str = "info"):
        """Record (and print) one stage of an operation. Unknown/finished op
        ids are tolerated — logging must never break the radio path."""
        if op_id is None:
            return
        entry = {"t": time.time(), "stage": str(stage),
                 "detail": str(detail), "level": str(level)}
        with self._lock:
            op = self._by_id.get(op_id)
            if op is None:
                return
            op["stages"].append(entry)
            self._events.append({"op_id": op_id, "kind": op["kind"],
                                 "state": op["state"], **entry})
            del self._events[:-100]
        tag = "" if level == "info" else f" [{level.upper()}]"
        print(f"[op #{op_id}] {stage}: {detail}{tag}" if detail
              else f"[op #{op_id}] {stage}{tag}")

    def finish(self, op_id, state: str, detail: str = ""):
        if op_id is None:
            return
        state = state if state in TERMINAL_STATES else "success"
        level = ("error" if state == "failed"
                 else "warn" if state in ("mismatch", "unverified")
                 else "info")
        with self._lock:
            op = self._by_id.get(op_id)
            if op is not None:
                op["state"] = state
                op["t_end"] = time.time()
        self.stage(op_id, state.upper(), detail, level=level)

    # -- readout -----------------------------------------------------------

    def recent(self, n: int = 50):
        with self._lock:
            return [dict(op, stages=list(op["stages"]))
                    for op in list(self._ops)[-n:]]

    def get(self, op_id):
        with self._lock:
            op = self._by_id.get(op_id)
            return dict(op, stages=list(op["stages"])) if op else None

    def drain_events(self):
        """Take the queued stage events (for WS fan-out). Same contract as
        SharedConfig.drain_notices."""
        with self._lock:
            events, self._events = self._events, []
            return events

    def set_fields(self, op_id, fields):
        """Record which config fields this operation changed — the Acquirer
        scopes hardware readback to them (a rows-only change must not be
        judged by an unrelated missing gain getter)."""
        with self._lock:
            op = self._by_id.get(op_id)
            if op is not None:
                op["fields"] = [str(f) for f in fields]

    def fields(self, op_id):
        """The changed-field list for an op, or None (unknown → full check)."""
        with self._lock:
            op = self._by_id.get(op_id)
            return list(op["fields"]) if op and "fields" in op else None

    def last_terminal(self):
        """Most recent finished operation (for /health)."""
        with self._lock:
            for op in reversed(self._ops):
                if op["state"] in TERMINAL_STATES:
                    return {"id": op["id"], "kind": op["kind"],
                            "summary": op["summary"], "state": op["state"],
                            "t_end": op["t_end"]}
        return None


OPERATIONS = OperationLog()


# -- shared helpers used by config/acquisition ------------------------------

def fmt_value(key, value):
    """Human units for the common radio fields."""
    try:
        if key in ("center", "center_frequency"):
            return f"{float(value)/1e6:.6g} MHz"
        if key in ("sample_rate", "backend_sample_rate"):
            return f"{float(value)/1e6:.6g} MS/s"
        if key == "gain":
            return f"{float(value):.1f} dB"
    except (TypeError, ValueError):
        pass
    return repr(value)


def verdict_state(verdicts):
    """Collapse per-field readback verdicts into one operation state."""
    states = {v["state"] for v in verdicts}
    if "mismatch" in states:
        return "mismatch"
    if states == {"readback_unsupported"}:
        return "unverified"
    return "verified"
