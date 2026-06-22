#!/usr/bin/env python3
"""
AIR-T live spectrogram server -- scrolling waterfall, full UI control, GPU FFT.

Runs ON the Deepwave AIR8201.
  - Reader thread drains the SDR DMA continuously (decoupled -> no fatal overflow).
  - Sender thread turns the latest samples into `rows` fresh spectrogram rows
    (one batched FFT, GPU via cupy when available) and streams them. `rows` is
    set by the viewer: a small increment for the scrolling view, or the full
    history for "replace" mode (capture length == history -> the image refreshes
    wholesale instead of scrolling).
  - Control thread applies UI commands: center, sample rate, gain, FFT, rows.

server -> viewer (frames):   [4B len][JSON header][float32 payload ch0,ch1]
viewer -> server (control):  [4B len][JSON command]
"""

import socket
import struct
import json
import time
import sys
import select
import threading

import numpy as np
# Import SoapySDR FIRST. cupy pulls in the system libstdc++; if it loads before
# SoapySDR, SoapySDR can't find the newer GLIBCXX it was built against.
import SoapySDR
from SoapySDR import (
    SOAPY_SDR_RX, SOAPY_SDR_CF32, SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT,
)
try:
    import cupy as _cp
    _HAVE_GPU = True
except Exception:
    _cp, _HAVE_GPU = None, False
USE_GPU = _HAVE_GPU

# ------------------------------- settings ----------------------------------
HOST, PORT      = "0.0.0.0", 5005
CHANNELS        = [0, 1]
CENTER          = 1955e6
SAMPLE_RATE     = 15.36e6
GAIN_DB         = 0.0
NFFT            = 1024
ROWS_PER_UPDATE = 12          # rows pushed per frame in scrolling mode
TARGET_FPS      = 30
DC_NULL_BINS    = 2
READ_SIZE       = 1 << 18
MAX_NFFT        = 4096
# Continuous ring buffer depth per channel. Big enough to hold a full-window
# "replace" capture (nfft * history) in one shot. 1<<22 ~= 4.19 M samples
# ~= 273 ms at 15.36 MS/s, ~33 MB complex64 per channel.
MAX_TAIL        = 1 << 22

FREQ_MIN, FREQ_MAX = 300e6, 6e9
RATE_MIN, RATE_MAX = 3.90625e6, 125e6
GAIN_MIN, GAIN_MAX = -30.0, 0.0
# ---------------------------------------------------------------------------

_WIN_CACHE = {}


def get_window(n):
    w = _WIN_CACHE.get(n)
    if w is None:
        w = np.hanning(n).astype(np.float32)
        _WIN_CACHE[n] = w
    return w


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def recvall(sock, n):
    chunks, got = [], 0
    while got < n:
        c = sock.recv(n - got)
        if not c:
            raise ConnectionError("socket closed")
        chunks.append(c)
        got += len(c)
    return b"".join(chunks)


class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.center = CENTER
        self.sample_rate = SAMPLE_RATE
        self.gain = GAIN_DB
        self.nfft = NFFT
        self.rows = ROWS_PER_UPDATE
        self.dirty = set()

    def update(self, d):
        with self.lock:
            if "center" in d:
                self.center = clamp(float(d["center"]), FREQ_MIN, FREQ_MAX)
                self.dirty.add("center")
            if "sample_rate" in d:
                self.sample_rate = clamp(float(d["sample_rate"]), RATE_MIN, RATE_MAX)
                self.dirty.add("sample_rate")
            if "gain" in d:
                self.gain = clamp(float(d["gain"]), GAIN_MIN, GAIN_MAX)
                self.dirty.add("gain")
            if "nfft" in d:
                self.nfft = int(clamp(int(d["nfft"]), 64, MAX_NFFT))
            if "rows" in d:
                self.rows = int(clamp(int(d["rows"]), 1, 4096))

    def snapshot(self):
        with self.lock:
            return dict(center=self.center, sample_rate=self.sample_rate,
                        gain=self.gain, nfft=self.nfft, rows=self.rows)

    def take_dirty(self):
        with self.lock:
            d, self.dirty = self.dirty, set()
            return d


def open_radio(cfg):
    devs = SoapySDR.Device.enumerate()
    if not devs:
        print("No SoapySDR device found.")
        sys.exit(1)
    dev = SoapySDR.Device(devs[0])
    for ch in CHANNELS:
        dev.setSampleRate(SOAPY_SDR_RX, ch, cfg.sample_rate)
        try:
            dev.setGain(SOAPY_SDR_RX, ch, cfg.gain)
        except Exception as e:
            print(f"  setGain ch{ch} warning: {e}")
        dev.setFrequency(SOAPY_SDR_RX, ch, cfg.center)
    stream = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, CHANNELS)
    dev.activateStream(stream)
    print(f"Radio armed: center {cfg.center/1e6:.2f} MHz, "
          f"{cfg.sample_rate/1e6:.3f} MS/s, channels {CHANNELS}")
    return dev, stream


class Acquirer(threading.Thread):
    """Drains the SDR continuously into a ring buffer, applies config.

    The ring is a fixed-size rolling history per channel; writes are O(read)
    regardless of how big a window the viewer later asks for, so 'replace' mode
    can pull a full nfft*history capture without stalling acquisition (which is
    what triggers the fatal DMA overflow).
    """
    def __init__(self, dev, stream, cfg):
        super().__init__(daemon=True)
        self.dev, self.stream, self.cfg = dev, stream, cfg
        self.lock = threading.Lock()
        self.cap = MAX_TAIL
        self.ring = [np.zeros(self.cap, np.complex64) for _ in CHANNELS]
        self.widx = 0          # next write index (shared; channels advance together)
        self.filled = 0        # valid samples in the ring (caps at cap)
        self.running = True
        self.read_size = min(dev.getStreamMTU(stream), READ_SIZE)
        self.tmp = [np.zeros(self.read_size, np.complex64) for _ in CHANNELS]

    def apply_dirty(self):
        dirty = self.cfg.take_dirty()
        if not dirty:
            return
        snap = self.cfg.snapshot()
        try:
            if "center" in dirty:
                for ch in CHANNELS:
                    self.dev.setFrequency(SOAPY_SDR_RX, ch, snap["center"])
            if "gain" in dirty:
                for ch in CHANNELS:
                    self.dev.setGain(SOAPY_SDR_RX, ch, snap["gain"])
            if "sample_rate" in dirty:
                self.dev.deactivateStream(self.stream)
                for ch in CHANNELS:
                    self.dev.setSampleRate(SOAPY_SDR_RX, ch, snap["sample_rate"])
                time.sleep(0.02)
                self.dev.activateStream(self.stream)
                print(f"retuned: {snap['center']/1e6:.2f} MHz, "
                      f"{snap['sample_rate']/1e6:.3f} MS/s")
        except Exception as e:
            print("apply_dirty warning:", e)

    def _write(self, n):
        """Write tmp[:n] of every channel into the ring with wraparound."""
        w, cap = self.widx, self.cap
        end = w + n
        if end <= cap:
            for i in range(len(CHANNELS)):
                self.ring[i][w:end] = self.tmp[i][:n]
        else:
            first = cap - w
            for i in range(len(CHANNELS)):
                self.ring[i][w:] = self.tmp[i][:first]
                self.ring[i][:end - cap] = self.tmp[i][first:n]
        self.widx = end % cap
        self.filled = min(self.filled + n, cap)

    def run(self):
        while self.running:
            self.apply_dirty()
            sr = self.dev.readStream(self.stream, self.tmp, self.read_size,
                                     timeoutUs=int(1e6))
            if sr.ret > 0:
                with self.lock:
                    self._write(sr.ret)
            elif sr.ret in (SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT):
                continue
            else:
                print("acquirer: stream error", sr.ret, "-- restarting")
                try:
                    self.dev.deactivateStream(self.stream)
                except Exception:
                    pass
                time.sleep(0.02)
                self.dev.activateStream(self.stream)

    def get_latest(self, need):
        """Most recent `need` samples per channel, in chronological order."""
        with self.lock:
            need = min(need, self.filled)
            if need <= 0:
                return [np.zeros(0, np.complex64) for _ in CHANNELS]
            start = (self.widx - need) % self.cap
            out = []
            if start + need <= self.cap:
                for i in range(len(CHANNELS)):
                    out.append(self.ring[i][start:start + need].copy())
            else:
                first = self.cap - start
                for i in range(len(CHANNELS)):
                    out.append(np.concatenate(
                        [self.ring[i][start:], self.ring[i][:need - first]]))
            return out

    def stop(self):
        self.running = False


def spectro_block(iq, nfft, rows):
    """rows consecutive FFTs from the latest nfft*rows samples (one batch)."""
    frames = iq[:nfft * rows].reshape(rows, nfft)
    win = get_window(nfft)
    c = nfft // 2
    if USE_GPU:
        g = _cp.asarray(frames) * _cp.asarray(win)[None, :]
        X = _cp.fft.fftshift(_cp.fft.fft(g, axis=1), axes=1)
        P = X.real ** 2 + X.imag ** 2
        out = (10.0 * _cp.log10(P + 1e-12)).astype(_cp.float32)
        out[:, c - DC_NULL_BINS:c + DC_NULL_BINS + 1] = out.min(axis=1, keepdims=True)
        return _cp.asnumpy(out)
    frames = frames * win[None, :]
    X = np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)
    P = X.real ** 2 + X.imag ** 2
    out = (10.0 * np.log10(P + 1e-12)).astype(np.float32)
    out[:, c - DC_NULL_BINS:c + DC_NULL_BINS + 1] = out.min(axis=1, keepdims=True)
    return out


def send_frame(conn, blocks, header):
    payload = b"".join(b.tobytes() for b in blocks)
    hb = json.dumps(header).encode("utf-8")
    conn.sendall(struct.pack(">I", len(hb)) + hb + payload)


def control_reader(conn, cfg):
    try:
        while True:
            ln = struct.unpack(">I", recvall(conn, 4))[0]
            cfg.update(json.loads(recvall(conn, ln).decode("utf-8")))
    except Exception:
        return


def serve_client(conn, acq, cfg):
    target_dt = 1.0 / TARGET_FPS
    while True:
        t0 = time.time()
        snap = cfg.snapshot()
        nfft, rows = snap["nfft"], snap["rows"]
        if nfft * rows > MAX_TAIL:            # clamp a huge replace window
            rows = max(1, MAX_TAIL // nfft)
        need = nfft * rows
        win = acq.get_latest(need)
        if all(w.size >= need for w in win):
            _, wready, _ = select.select([], [conn], [], 0)
            if wready:
                blocks = [spectro_block(w, nfft, rows) for w in win]
                header = {
                    "center": snap["center"], "fs": snap["sample_rate"],
                    "gain": snap["gain"], "nfft": nfft, "rows": rows,
                    "channels": CHANNELS, "shape": [rows, nfft], "dtype": "float32",
                }
                send_frame(conn, blocks, header)
        dt = time.time() - t0
        if dt < target_dt:
            time.sleep(target_dt - dt)


def main():
    cfg = Config()
    dev, stream = open_radio(cfg)
    acq = Acquirer(dev, stream, cfg)
    acq.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(1)
    print(f"FFT backend: {'GPU (cupy)' if USE_GPU else 'CPU (numpy, batched)'}")
    print(f"Listening on {HOST}:{PORT} -- start live_viewer_full.py on the Mac.")
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("Viewer connected from", addr)
            threading.Thread(target=control_reader, args=(conn, cfg),
                             daemon=True).start()
            try:
                serve_client(conn, acq, cfg)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                print("Viewer disconnected:", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            print("Waiting for the viewer to reconnect ...")
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        acq.stop()
        acq.join(timeout=1.0)
        try:
            dev.deactivateStream(stream)
            dev.closeStream(stream)
        except Exception:
            pass
        srv.close()


if __name__ == "__main__":
    main()



