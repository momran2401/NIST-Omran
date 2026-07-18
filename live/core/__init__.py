"""live/core — shared backend for every NIST-Omran live viewer frontend.

Import order matters only in that striqt_compat must load first (it re-execs
once to fix LD_LIBRARY_PATH on the AIR-T pixi env before scipy/striqt import);
importing this package guarantees that.

Frontends use:
    from core import state, devices
    from core.config import SharedConfig
    from core.acquisition import Acquirer, Computer, DemoAcquirer
    from core.serialization import serialize_frame, parse_frame
    from core.operations import OPERATIONS
    from core.health import health_snapshot
"""
from . import striqt_compat  # noqa: F401  (must be first — see docstring)

__version__ = "1.0.0"
