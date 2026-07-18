"""Device adapter contract.

One adapter per supported radio family. The contract gives every frontend the
same handles for:

  - create_source()          open a striqt source for this device
  - describe_capabilities()  identity, channels, envelope, readback support
  - read_back(source, cfg)   query the LIVE driver for the actually-applied
                             center/sample_rate/gain (None per field when the
                             driver can't answer)
  - verify(cfg, actuals)     compare requested vs read-back values with
                             adapter-specific tolerances

Readback is the heart of the verified-settings pipeline (operations.py): a
config change is only reported as VERIFIED when the driver's own answer
matches the request within tolerance.
"""
from __future__ import annotations

from .. import state
from ..constants import DEVICE_PROFILES
from ..shims import get_device

try:
    from SoapySDR import SOAPY_SDR_RX as _RX_DIR
except Exception:
    _RX_DIR = 1   # SoapySDR's RX direction constant


class DeviceAdapter:
    """Base adapter. Subclasses set `name` and override create_source()."""

    name = None                 # profile key in constants.DEVICE_PROFILES
    # Verification tolerances. Frequency tolerance is the max of the absolute
    # floor and the relative fraction — synthesizer step sizes differ per
    # radio, so exact equality is the wrong test.
    freq_tol_hz   = 10.0        # absolute floor
    freq_tol_rel  = 1e-6        # relative to the requested value
    rate_tol_rel  = 1e-4
    gain_tol_db   = 0.5
    # Whether the driver supports config readback at all (demo says no and
    # reports "readback_unsupported" honestly instead of faking agreement).
    supports_readback = True

    def __init__(self, info=None):
        # `info` is the SoapySDR enumeration dict when discovery picked this
        # device (may carry serial/label); None for explicit selection.
        self.info = dict(info) if info else {}

    # -- identity ----------------------------------------------------------

    @property
    def profile(self):
        return DEVICE_PROFILES[self.name]

    @property
    def label(self):
        base = self.profile["label"]
        serial = self.info.get("serial")
        return f"{base} ({serial})" if serial else base

    def describe_capabilities(self):
        prof = self.profile
        return {
            "name":              self.name,
            "label":             self.label,
            "serial":            self.info.get("serial"),
            "driver":            self.info.get("driver"),
            "channels":          list(state.CHANNELS),
            "envelope":          dict(prof["envelope"]),
            "query_envelope":    bool(prof.get("query_envelope")),
            "supports_readback": bool(self.supports_readback),
            "tolerances": {
                "freq_hz":  self.freq_tol_hz,
                "freq_rel": self.freq_tol_rel,
                "rate_rel": self.rate_tol_rel,
                "gain_db":  self.gain_tol_db,
            },
        }

    # -- lifecycle ---------------------------------------------------------

    def create_source(self):
        raise NotImplementedError

    # -- readback ----------------------------------------------------------

    def read_back(self, source, cfg):
        """
        Query the live driver for the actually-applied tuning. Returns
        {"center": Hz|None, "sample_rate": Hz|None, "gain": [dB|None, ...]}
        — None per field when the driver has no answer. Never raises: a
        readback failure is data ("readback_unsupported"), not an error.
        """
        if not self.supports_readback:
            return {"center": None, "sample_rate": None,
                    "gain": [None] * len(state.CHANNELS)}
        dev = get_device(source)
        out = {"center": None, "sample_rate": None,
               "gain": [None] * len(state.CHANNELS)}
        if dev is None:
            return out
        ch0 = state.CHANNELS[0] if state.CHANNELS else 0
        try:
            out["center"] = float(dev.getFrequency(_RX_DIR, ch0))
        except Exception:
            pass
        try:
            out["sample_rate"] = float(dev.getSampleRate(_RX_DIR, ch0))
        except Exception:
            pass
        gains = []
        for ch in state.CHANNELS:
            try:
                gains.append(float(dev.getGain(_RX_DIR, ch)))
            except Exception:
                gains.append(None)
        out["gain"] = gains
        return out

    # -- verification ------------------------------------------------------

    def hardware_expectations(self, source, capture, cfg):
        """
        The values striqt is expected to have PROGRAMMED into the driver —
        which legitimately differ from the user-facing capture values: a
        non-"none" lo_shift intentionally offsets the hardware LO, and
        backend_sample_rate/host_resample run the SDR at a different rate
        than the delivered capture rate. Comparing raw driver getters against
        cfg would falsely fail both valid cases. Asks striqt's own resampler
        design when discoverable; falls back to the declared backend rate.
        Returns {"center": Hz, "sample_rate": Hz}.
        """
        center = float(cfg.center)
        rate = float(cfg.sample_rate)
        for obj in (source, getattr(source, "backend", None)):
            fn = getattr(obj, "get_resampler", None)
            if fn is None:
                continue
            try:
                design = fn(capture)
                center = float(cfg.center) - float(design.get("lo_offset", 0.0))
                rate = float(design.get("fs_sdr", rate))
                return {"center": center, "sample_rate": rate}
            except Exception:
                pass
        if float(getattr(cfg, "backend_sample_rate", 0.0) or 0.0) > 0:
            rate = float(cfg.backend_sample_rate)
        return {"center": center, "sample_rate": rate}

    def verify(self, cfg, actuals, expected=None):
        """
        Compare the requested cfg against driver readback. Returns a list of
        per-field verdict dicts:
          {"field", "requested", "actual", "state"}
        where state ∈ {"verified", "mismatch", "readback_unsupported"}.
        Gain is judged per channel against the striqt calibrated-gain
        convention caveat: some drivers report a composite gain that differs
        from the requested calibrated value by a fixed offset, so a gain
        mismatch is a warning-grade signal, not proof of failure.
        """
        verdicts = []
        expected = dict(expected or {})
        exp_center = float(expected.get("center", cfg.center))
        exp_rate = float(expected.get("sample_rate", cfg.sample_rate))

        def judge(field, requested, actual, tol):
            if actual is None:
                return {"field": field, "requested": requested,
                        "actual": None, "state": "readback_unsupported"}
            ok = abs(float(actual) - float(requested)) <= tol
            return {"field": field, "requested": float(requested),
                    "actual": float(actual),
                    "state": "verified" if ok else "mismatch"}

        freq_tol = max(self.freq_tol_hz, self.freq_tol_rel * abs(exp_center))
        verdicts.append(judge("center", exp_center, actuals.get("center"), freq_tol))
        rate_tol = max(1.0, self.rate_tol_rel * abs(exp_rate))
        verdicts.append(judge("sample_rate", exp_rate,
                              actuals.get("sample_rate"), rate_tol))
        gains = actuals.get("gain") or []
        for i, ch in enumerate(state.CHANNELS):
            actual = gains[i] if i < len(gains) else None
            v = judge(f"gain[ch{ch}]", cfg.gain, actual, self.gain_tol_db)
            verdicts.append(v)
        return verdicts
