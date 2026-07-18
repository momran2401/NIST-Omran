"""striqt hardware accessor shims.

getattr-based accessors that work against the installed striqt build, whose
method/attribute names may differ from the vendored source tree. Extracted
verbatim from striqt_web_server.py.
"""
from __future__ import annotations

import numpy as np

from . import state

# ---------------------------------------------------------------------------
# striqt hardware shims
# (match the getattr pattern used in striqt_server_TCP.py so this works
#  against the installed striqt build which may differ from the vendored source)
# ---------------------------------------------------------------------------

def get_device(source):
    return getattr(source, "_device", getattr(source, "device", None))

def get_rx_stream(source):
    return getattr(source, "_rx_stream", getattr(source, "rx_stream", None))

def get_stream_ports(source):
    return tuple(getattr(get_rx_stream(source), "ports", state.CHANNELS))

def get_stream_mtu(source):
    rx = get_rx_stream(source)
    if rx is None:
        return None
    for name in ("mtu", "_mtu", "stream_mtu"):
        val = getattr(rx, name, None)
        if val is not None:
            try:
                return int(val)
            except Exception:
                pass
    stream = getattr(rx, "stream", None)
    dev    = get_device(source)
    if dev is not None and stream is not None:
        for meth in ("getStreamMTU", "get_stream_mtu"):
            fn = getattr(dev, meth, None)
            if fn is not None:
                try:
                    return int(fn(stream))
                except Exception:
                    pass
    return None

def open_stream(source):
    rx  = get_rx_stream(source)
    dev = get_device(source)
    if rx is None or dev is None:
        raise RuntimeError("striqt source has no RX stream/device")
    if getattr(rx, "stream", None) is None:
        rx.open(dev)

def enable_stream(source, enabled):
    rx     = get_rx_stream(source)
    if rx is None:
        return
    dev    = get_device(source)
    stream = getattr(rx, "stream", None)
    if dev is None or stream is None:
        return
    methods = (("activateStream", "activate_stream") if enabled
               else ("deactivateStream", "deactivate_stream"))
    for meth in methods:
        fn = getattr(dev, meth, None)
        if fn is not None:
            try:
                fn(stream)
                return
            except TypeError:
                try:
                    fn(stream, 0, 0, 0)
                    return
                except Exception:
                    pass
            except Exception:
                pass

def close_source(source):
    for action in [lambda: enable_stream(source, False),
                   lambda: _close_rx_stream(source),
                   lambda: source.close()]:
        try:
            action()
        except Exception:
            pass

def _close_rx_stream(source):
    rx = get_rx_stream(source)
    if rx is not None:
        dev = get_device(source)
        if dev is not None and getattr(rx, "stream", None) is not None:
            rx.close(dev)

def stream_buffers_for(source, samples):
    rx    = get_rx_stream(source)
    ports = tuple(getattr(rx, "ports", state.CHANNELS))
    return [samples[state.CHANNELS.index(p)].view(np.float32) for p in ports], ports


def query_device_envelope(source):
    """
    Ask the open SoapySDR device for its real capability ranges (P3-3).
    Returns a partial envelope dict — only the keys the device answered — to
    be merged over the profile fallback by SharedConfig.set_envelope. Every
    step is defensive: a missing method, failed call, or odd range-object
    shape just drops that key (the fallback bound stays in force).
    """
    dev = get_device(source)
    if dev is None:
        return {}
    try:
        from SoapySDR import SOAPY_SDR_RX as _rx_dir
    except Exception:
        _rx_dir = 1   # SoapySDR's RX direction constant
    ch = state.CHANNELS[0] if state.CHANNELS else 0

    def _bounds(ranges):
        lows, highs = [], []
        for r in ranges:
            try:
                lows.append(float(r.minimum()))
                highs.append(float(r.maximum()))
            except Exception:
                try:
                    lows.append(float(r[0]))
                    highs.append(float(r[1]))
                except Exception:
                    pass
        if lows and highs:
            return min(lows), max(highs)
        return None

    env = {}
    for method, lo_key, hi_key in (
        ("getFrequencyRange",  "freq_min", "freq_max"),
        ("getGainRange",       "gain_min", "gain_max"),
        ("getSampleRateRange", "rate_min", "rate_max"),
    ):
        fn = getattr(dev, method, None)
        if fn is None:
            continue
        try:
            ranges = fn(_rx_dir, ch)
        except Exception:
            continue
        if not isinstance(ranges, (list, tuple)):
            ranges = [ranges]   # getGainRange returns a single Range object
        got = _bounds(ranges)
        if got:
            env[lo_key], env[hi_key] = got
    return env
