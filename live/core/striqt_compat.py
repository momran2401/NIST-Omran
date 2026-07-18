"""striqt import compatibility layer.

Runs the pixi libstdc++ re-exec guard BEFORE any striqt/scipy import, then
imports the striqt sensor + analysis stacks defensively. Every other core
module gets its striqt symbols (and the _SENSOR_OK/_ANALYSIS_OK flags) from
here, so a missing hardware stack degrades exactly one way, everywhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

def _ensure_pixi_runtime_libs():
    """
    The AIR-T pixi env ships a newer libstdc++ needed by scipy/striqt waveform
    extensions. Re-exec once with that lib dir in LD_LIBRARY_PATH when needed.
    """
    if os.name != "posix":
        return
    try:
        lib_dir = Path(sys.executable).resolve().parents[1] / "lib"
    except Exception:
        return
    if not (lib_dir / "libstdc++.so.6").exists():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in current.split(":") if p]
    lib_s = str(lib_dir)
    if lib_s in parts:
        return
    os.environ["LD_LIBRARY_PATH"] = ":".join([lib_s] + parts)
    if os.environ.get("RADIO_WEB_LD_REEXEC") == "1":
        return
    os.environ["RADIO_WEB_LD_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_pixi_runtime_libs()

# striqt hardware imports (only needed for real radio mode)
try:
    from striqt.sensor import specs
    from striqt.sensor.lib.sources import deepwave as _deepwave_sources
    Air8201BSourceSpec = _deepwave_sources.Air8201BSourceSpec
    Airstack1Source = _deepwave_sources.Airstack1Source
    # Older/other Deepwave models: not every installed striqt build ships their
    # spec classes — fall back to the AIR8201B spec, which the SoapyAIRT driver
    # accepts for the shared AirStack fields.
    Air7101BSourceSpec = getattr(_deepwave_sources, "Air7101BSourceSpec",
                                 Air8201BSourceSpec)
    Air7201BSourceSpec = getattr(_deepwave_sources, "Air7201BSourceSpec",
                                 Air8201BSourceSpec)
    try:
        from striqt.sensor.lib.sources.soapy import SoapySource as _SoapySource
    except Exception:
        _SoapySource = None
    try:
        from striqt.sensor.lib.sources.soapy import ReceiveStreamError
    except Exception:
        try:
            from striqt.sensor.lib.sources.base import ReceiveStreamError
        except Exception:
            ReceiveStreamError = OSError
    _SENSOR_OK = True
except Exception as _sensor_err:
    _SENSOR_OK = False
    specs = None
    Air8201BSourceSpec = None
    Air7101BSourceSpec = None
    Air7201BSourceSpec = None
    Airstack1Source = None
    _SoapySource = None
    ReceiveStreamError = OSError

# striqt analysis (calibrated spectrogram — optional, falls back to quicklook)
try:
    from striqt.analysis import specs as analysis_specs
    from striqt.analysis import measurements as striqt_measurements
    from striqt.analysis.measurements import shared as striqt_shared
    _ANALYSIS_OK = True
    _ANALYSIS_ERR = None
except Exception as e:
    analysis_specs = None
    striqt_measurements = None
    striqt_shared = None
    _ANALYSIS_OK = False
    _ANALYSIS_ERR = e
