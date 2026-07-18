"""striqt source classes for non-AIR-T SoapySDR radios.

PlutoSource is ported from live/pluto_standalone.py (P3-1); GenericSoapySource
extends the same trick to any SoapySDR driver string, best-effort. Both reuse
the Air8201BSourceSpec values — the Soapy drivers ignore the AirStack
master-clock/time-source fields they don't implement. striqt/ itself is never
modified.
"""
from __future__ import annotations

from ..constants import DEVICE_PROFILES, MASTER_CLOCK_RATE
from ..striqt_compat import (
    Air7101BSourceSpec, Air7201BSourceSpec, Air8201BSourceSpec,
    Airstack1Source, _SENSOR_OK, _SoapySource,
)

# Per-model striqt spec classes (striqt_compat falls back to Air8201BSourceSpec
# when the installed build doesn't ship a model's own class).
SPEC_CLASSES = {
    "air7101b": Air7101BSourceSpec,
    "air7201b": Air7201BSourceSpec,
    "air8201b": Air8201BSourceSpec,
}


def make_source_spec(device=None, overrides=None):
    """
    Build the striqt source spec for `device` (profile name), applying any
    applied-source-config `overrides` (the verified-reconnect path). Unknown
    override keys are dropped against the spec class's declared fields so a
    stale/foreign key can never crash source construction.
    """
    spec_cls = SPEC_CLASSES.get(device, Air8201BSourceSpec)
    profile_clock = DEVICE_PROFILES.get(device, {}).get("master_clock_rate")
    options = {
        "master_clock_rate": profile_clock or MASTER_CLOCK_RATE,
        "array_backend": "numpy",
        "time_source": "host",
        "time_sync_at": "open",
        "clock_source": "internal",
        "gapless": True,
        "receive_retries": 0,
    }
    options.update(dict(overrides or {}))
    fields = set(getattr(spec_cls, "__struct_fields__", ()) or ())
    if fields:
        options = {k: v for k, v in options.items() if k in fields}
    return spec_cls(**options)


if _SENSOR_OK and _SoapySource is not None:
    class PlutoSource(Airstack1Source):
        """
        PlutoSDR adapter (ported from live/pluto_standalone.py, P3-1).

        Subclasses Airstack1Source to reuse all of its striqt stream/arm/read
        machinery, but overrides __init__ to call SoapySource.__init__ directly.
        This skips two things in Airstack1Source.__init__ that crash on a Pluto:
          1. driver='SoapyAIRT'  -- replaced with driver='plutosdr'
          2. _set_jesd_sysref_delay()  -- AIR-T FPGA register write, absent on Pluto
        get_id/read_peripherals are overridden because the AirStack versions read
        the Jetson eth0 MAC and an AirStack-only temperature sensor.
        """

        def __init__(self, spec, **kwargs):
            _SoapySource.__init__(self, spec, driver="plutosdr", **kwargs)

        def get_id(self):
            try:
                return self.device.getHardwareKey()
            except Exception:
                return "pluto"

        def read_peripherals(self):
            return {}

    class GenericSoapySource(Airstack1Source):
        """
        Best-effort adapter for any other SoapySDR radio: same shape as
        PlutoSource but with the driver string chosen at construction time
        (from enumeration). Works wherever the driver tolerates the AirStack
        spec fields it doesn't implement.
        """

        def __init__(self, spec, driver, **kwargs):
            self._generic_driver = str(driver)
            _SoapySource.__init__(self, spec, driver=str(driver), **kwargs)

        def get_id(self):
            try:
                return self.device.getHardwareKey()
            except Exception:
                return self._generic_driver

        def read_peripherals(self):
            return {}
else:
    PlutoSource = None
    GenericSoapySource = None
