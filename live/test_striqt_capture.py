#!/usr/bin/env python3
"""
Small striqt AIR8201 capture smoke test.

Runs ON the Deepwave AIR8201 against the *installed* striqt package (do not force
the local striqt/src onto sys.path -- it is a different version). It opens the
striqt AIR-T source with the same known-good pattern as
airt_live_server_striqt.py:

    Airstack1Source.from_spec(spec)
      -> open_stream(source)        # open RX stream BEFORE arming
      -> source.arm_spec(capture)
      -> enable_stream(source, True)
      -> source._read_stream(...)   # chunked reads
      -> close_source(source)

It captures RX ports 0 and 1 for a few short durations and prints
shape / dtype / elapsed / stream_ports / stream_mtu, plus any exceptions.
No Mac viewer required.
"""

from __future__ import annotations

import time

import numpy as np

from striqt.sensor import specs
from striqt.sensor.lib.sources.deepwave import Air8201BSourceSpec, Airstack1Source


CHANNELS = (0, 1)
CENTER = 1955e6
SAMPLE_RATE = 15.36e6
GAIN_DB = 0.0
MASTER_CLOCK_RATE = 125e6
READ_SIZE = 1 << 18  # 262144 samples per chunked read


# --- striqt source helpers (kept identical to airt_live_server_striqt.py) ---


def get_device(source):
    return getattr(source, "_device", getattr(source, "device", None))


def get_rx_stream(source):
    return getattr(source, "_rx_stream", getattr(source, "rx_stream", None))


def get_stream_ports(source):
    rx_stream = get_rx_stream(source)
    return tuple(getattr(rx_stream, "ports", CHANNELS))


def get_stream_mtu(source):
    rx_stream = get_rx_stream(source)

    if rx_stream is None:
        return None

    for name in ("mtu", "_mtu", "stream_mtu"):
        val = getattr(rx_stream, name, None)
        if val is not None:
            try:
                return int(val)
            except Exception:
                pass

    stream = getattr(rx_stream, "stream", None)
    dev = get_device(source)

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
    rx_stream = get_rx_stream(source)
    dev = get_device(source)

    if rx_stream is None or dev is None:
        raise RuntimeError("striqt source has no RX stream/device")

    if getattr(rx_stream, "stream", None) is None:
        rx_stream.open(dev)


def enable_stream(source, enabled):
    rx_stream = get_rx_stream(source)

    if rx_stream is None:
        return

    dev = get_device(source)
    stream = getattr(rx_stream, "stream", None)

    if dev is None or stream is None:
        return

    if enabled:
        for meth in ("activateStream", "activate_stream"):
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
    else:
        for meth in ("deactivateStream", "deactivate_stream"):
            fn = getattr(dev, meth, None)
            if fn is not None:
                try:
                    fn(stream)
                    return
                except Exception:
                    pass


def close_source(source):
    try:
        enable_stream(source, False)
    except Exception:
        pass

    try:
        rx_stream = get_rx_stream(source)
        if rx_stream is not None:
            dev = get_device(source)
            if dev is not None and getattr(rx_stream, "stream", None) is not None:
                try:
                    rx_stream.close(dev)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        source.close()
    except Exception:
        pass


def make_source():
    source_spec = Air8201BSourceSpec(
        master_clock_rate=MASTER_CLOCK_RATE,
        array_backend="numpy",
        time_source="host",
        time_sync_at="open",
        clock_source="internal",
        gapless=True,
        receive_retries=0,
    )
    return Airstack1Source.from_spec(source_spec)


def make_capture(duration_sec):
    return specs.SoapyCapture(
        port=CHANNELS,
        center_frequency=CENTER,
        gain=tuple([GAIN_DB] * len(CHANNELS)),
        duration=duration_sec,
        sample_rate=SAMPLE_RATE,
        backend_sample_rate=SAMPLE_RATE,
        host_resample=False,
        analysis_bandwidth=float("inf"),
        lo_shift="none",
    )


def stream_buffers_for(source, samples):
    rx_stream = get_rx_stream(source)
    ports = tuple(getattr(rx_stream, "ports", CHANNELS))

    buffers = []
    for port in ports:
        ch_index = CHANNELS.index(port)
        # Soapy complex float buffers are interleaved float32 I/Q.
        buffers.append(samples[ch_index].view(np.float32))

    return buffers


def capture_once(source, duration_sec):
    requested = round(duration_sec * SAMPLE_RATE)
    samples = np.empty((len(CHANNELS), requested), dtype=np.complex64)

    open_stream(source)
    source.arm_spec(make_capture(duration_sec))
    enable_stream(source, True)

    buffers = stream_buffers_for(source, samples)

    received = 0
    while received < requested:
        count = min(READ_SIZE, requested - received)
        got, _ = source._read_stream(
            buffers,
            offset=received,
            count=count,
            timeout_sec=count / SAMPLE_RATE + 0.1,
            on_overflow="log",
        )
        if got <= 0:
            time.sleep(0.001)
            continue
        received += got

    return samples


def main():
    source = make_source()
    try:
        open_stream(source)
        print("opened AIR8201 via installed striqt")
        print(f"channels={CHANNELS}")

        stream_ports = get_stream_ports(source)
        stream_mtu = get_stream_mtu(source)

        for ms in (1, 5, 10, 20):
            duration = ms / 1000.0
            try:
                t0 = time.perf_counter()
                samples = capture_once(source, duration)
                elapsed = time.perf_counter() - t0
                # refresh in case the stream reports them only after arming
                stream_ports = get_stream_ports(source)
                stream_mtu = get_stream_mtu(source)
                print(
                    f"{ms:>2} ms: shape={samples.shape}, dtype={samples.dtype}, "
                    f"elapsed={elapsed:.4f}s, stream_ports={stream_ports}, "
                    f"stream_mtu={stream_mtu}"
                )
            except Exception as exc:
                print(f"{ms:>2} ms: exception: {type(exc).__name__}: {exc}")
    finally:
        close_source(source)
        print("source control closed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
