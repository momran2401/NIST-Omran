"""Mutable runtime state, configured once at startup.

The frontends call configure_device()/set_fps()/set_backend() before starting
any thread; every core module reads these attributes at call time (never
`from state import CHANNELS`), so the one assignment is visible everywhere.
"""
from __future__ import annotations

import os

from .constants import BACKENDS, DEVICE_PROFILES

DEVICE       = "air8201b"
DEVICE_LABEL = DEVICE_PROFILES[DEVICE]["label"]
CHANNELS     = tuple(DEVICE_PROFILES[DEVICE]["channels"])

BROADCAST_FPS = 15        # default max frames/sec to browsers/clients

# Backend: "calibrated" (striqt PSD/ENBW dB spectrogram), "quicklook" (simple
# FFT dB), "psd" (striqt power_spectral_density statistic traces), or "ssb"
# (striqt 5G SSB spectrogram).
SPEC_BACKEND = os.environ.get("SPEC_BACKEND", "calibrated").strip().lower()
if SPEC_BACKEND not in BACKENDS:
    SPEC_BACKEND = "calibrated"


def configure_device(name: str, channels=None):
    """Select the active device profile. `channels` overrides the profile's
    RX port tuple (demo multi-channel testing, or a discovered channel count
    from a live driver)."""
    global DEVICE, DEVICE_LABEL, CHANNELS
    if name not in DEVICE_PROFILES:
        raise ValueError(f"unknown device {name!r} (known: {sorted(DEVICE_PROFILES)})")
    DEVICE = name
    DEVICE_LABEL = DEVICE_PROFILES[name]["label"]
    CHANNELS = tuple(channels) if channels is not None else tuple(
        DEVICE_PROFILES[name]["channels"]
    )


def set_device_label(label: str):
    """Refine the display label after discovery (e.g. append a serial)."""
    global DEVICE_LABEL
    DEVICE_LABEL = str(label)


def set_channels(channels):
    """Override the RX port tuple after live discovery."""
    global CHANNELS
    CHANNELS = tuple(channels)


def set_fps(fps: float):
    global BROADCAST_FPS
    BROADCAST_FPS = max(float(fps), 0.5)


def set_backend(backend: str):
    global SPEC_BACKEND
    backend = str(backend).strip().lower()
    if backend in BACKENDS:
        SPEC_BACKEND = backend
