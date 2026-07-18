#!/usr/bin/env python3
"""
Terminal (curses) live monitor — a thin frontend over live/core.

Runs the SAME validated backend as the web viewer (SharedConfig freedom-model
tiers, calibrated/quicklook/psd/ssb compute, device adapters, verified
operations with hardware readback) and renders an ASCII waterfall + status in
the terminal. Intended for quick checks over SSH when no display is available.

Usage:
    python3 live/striqt_standalone_terminal.py                     # AIR8201B
    python3 live/striqt_standalone_terminal.py --demo              # no hardware
    python3 live/striqt_standalone_terminal.py --device pluto
    python3 live/striqt_standalone_terminal.py --center-mhz 1955 --rate-msps 15.36 \
            --nfft 1024 --fps 3 --backend quicklook

Keys:
    q         quit
    ← / →     tune center −/+ 1 MHz      ( < / > = ±10 MHz )
    ↑ / ↓     gain +1 / −1 dB
    r         cycle sample rate           n   cycle FFT size
    b         cycle backend               o   toggle operations/log pane
"""

import argparse
import contextlib
import io
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import devices, health, state                        # noqa: E402
from core.acquisition import Acquirer, Computer, DemoAcquirer  # noqa: E402
from core.config import SharedConfig                           # noqa: E402
from core.constants import BACKENDS, NFFT_CHOICES, RATES_HZ    # noqa: E402
from core.striqt_compat import _ANALYSIS_OK, _SENSOR_OK        # noqa: E402

GRADIENT = " .:-=+*#%@"


class LogBuffer(io.TextIOBase):
    """Captures everything the core prints (operation stages, radio logs)
    while curses owns the screen, and serves the tail to the log pane."""

    def __init__(self, keep=300):
        self.lines = deque(maxlen=keep)
        self._partial = ""
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                if line.strip():
                    self.lines.append(line)
        return len(text)

    def flush(self):
        pass

    def tail(self, n):
        with self._lock:
            return list(self.lines)[-n:]


def downsample_row(row, width):
    """Max-pool one spectrogram row (dB) to `width` columns — max keeps
    narrowband signals visible at any terminal width."""
    n = row.shape[0]
    if width >= n:
        idx = np.linspace(0, n - 1, width).astype(int)
        return row[idx]
    edges = np.unique(np.linspace(0, n, width + 1).astype(int))
    pooled = np.maximum.reduceat(row, edges[:-1])
    if pooled.shape[0] < width:
        pooled = np.pad(pooled, (0, width - pooled.shape[0]), mode="edge")
    return pooled[:width]


def render_block(block, rows_avail, width):
    """Map a (rows, bins) dB block to a list of ASCII strings."""
    finite = block[np.isfinite(block)]
    if finite.size == 0:
        return ["(no data)"]
    vmin = float(np.percentile(finite, 5))
    vmax = float(np.percentile(finite, 99))
    if vmax - vmin < 1.0:
        vmax = vmin + 1.0
    shown = block[-rows_avail:] if block.shape[0] > rows_avail else block
    out = []
    scale = (len(GRADIENT) - 1) / (vmax - vmin)
    for r in range(shown.shape[0]):
        vals = downsample_row(shown[r], width)
        idx = np.clip((vals - vmin) * scale, 0, len(GRADIENT) - 1).astype(int)
        out.append("".join(GRADIENT[i] for i in idx))
    return out


def cycle(choices, current, key=float):
    vals = list(choices)
    try:
        i = min(range(len(vals)), key=lambda j: abs(key(vals[j]) - key(current)))
        return vals[(i + 1) % len(vals)]
    except Exception:
        return vals[0]


def ui_loop(stdscr, shared, acquirer, fps, logbuf):
    import curses

    curses.curs_set(0)
    stdscr.nodelay(True)
    show_log = True
    interval = 1.0 / max(fps, 0.5)

    while True:
        t0 = time.time()

        # ── keys ─────────────────────────────────────────────────────────
        while True:
            ch = stdscr.getch()
            if ch == -1:
                break
            cfg = shared.snapshot()
            if ch in (ord("q"), ord("Q")):
                return
            elif ch == curses.KEY_LEFT:
                shared.update({"center": cfg.center - 1e6})
            elif ch == curses.KEY_RIGHT:
                shared.update({"center": cfg.center + 1e6})
            elif ch == ord("<"):
                shared.update({"center": cfg.center - 10e6})
            elif ch == ord(">"):
                shared.update({"center": cfg.center + 10e6})
            elif ch == curses.KEY_UP:
                shared.update({"gain": cfg.gain + 1.0})
            elif ch == curses.KEY_DOWN:
                shared.update({"gain": cfg.gain - 1.0})
            elif ch in (ord("r"), ord("R")):
                shared.update({"sample_rate": cycle(RATES_HZ, cfg.sample_rate)})
            elif ch in (ord("n"), ord("N")):
                shared.update({"nfft": cycle(NFFT_CHOICES, cfg.nfft, key=int)})
            elif ch in (ord("b"), ord("B")):
                order = sorted(BACKENDS)
                shared.update({"backend": order[(order.index(cfg.backend) + 1)
                                                % len(order)]})
            elif ch in (ord("o"), ord("O")):
                show_log = not show_log

        # ── layout ───────────────────────────────────────────────────────
        maxy, maxx = stdscr.getmaxyx()
        width = max(20, maxx - 1)
        stdscr.erase()

        cfg = shared.snapshot()
        snap = health.health_snapshot()
        head1 = (f"{state.DEVICE_LABEL}  {cfg.center/1e6:.3f} MHz  "
                 f"{cfg.sample_rate/1e6:.2f} MS/s  gain {cfg.gain:.0f} dB  "
                 f"nfft {cfg.nfft}  backend {cfg.backend}")
        age = snap.get("last_frame_age_s")
        head2 = (f"health {snap['status']}  boot {snap['boot_id'][:8]}  "
                 + (f"frame {age:.1f}s ago" if age is not None else "no frames yet"))
        keys = "q quit  ←/→ ±1MHz  </> ±10MHz  ↑/↓ gain  r rate  n nfft  b backend  o log"
        try:
            stdscr.addnstr(0, 0, head1, width)
            stdscr.addnstr(1, 0, head2 + "   " + keys, width)

            log_rows = min(9, maxy // 3) if show_log else 0
            wf_top = 2
            wf_avail = maxy - wf_top - log_rows - 1

            header, blocks = acquirer.latest()
            if header is not None and blocks and wf_avail > 2:
                nch = len(blocks)
                per_ch = max(2, (wf_avail - nch) // max(1, nch))
                y = wf_top
                for i, block in enumerate(blocks):
                    if y >= maxy - log_rows - 1:
                        break
                    stdscr.addnstr(
                        y, 0,
                        f"── RX{i+1} (port {header['channels'][i]}) " + "─" * width,
                        width)
                    y += 1
                    for line in render_block(np.asarray(block), per_ch, width):
                        if y >= maxy - log_rows - 1:
                            break
                        stdscr.addnstr(y, 0, line, width)
                        y += 1
            elif wf_avail > 2:
                stdscr.addnstr(wf_top + 1, 0, "waiting for first frame…", width)

            if show_log and log_rows > 0:
                base = maxy - log_rows - 1
                stdscr.addnstr(base, 0, "── operations / log " + "─" * width, width)
                for j, line in enumerate(logbuf.tail(log_rows - 1)):
                    stdscr.addnstr(base + 1 + j, 0, line, width)
        except Exception:
            pass   # a resize mid-draw can push addnstr out of bounds — redraw next tick

        stdscr.refresh()

        dt = interval - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)


def main():
    parser = argparse.ArgumentParser(
        description="striqt terminal live monitor (thin frontend over live/core)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device", default="air8201b",
                        help="air8201b | pluto | soapy | demo | auto | driver=X[,serial=Y]")
    parser.add_argument("--demo", action="store_true",
                        help="synthetic IQ (alias for --device demo)")
    parser.add_argument("--center-mhz", type=float, default=None)
    parser.add_argument("--rate-msps", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--nfft", type=int, default=None)
    parser.add_argument("--rows", type=int, default=None)
    parser.add_argument("--fps", type=float, default=3.0,
                        help="terminal refresh rate")
    parser.add_argument("--backend", default=None, choices=sorted(BACKENDS))
    args = parser.parse_args()

    selector = "demo" if args.demo else args.device
    name, adapter = devices.resolve_device(selector)
    devices.set_adapter(adapter)
    state.configure_device(name)
    state.set_device_label(adapter.label)
    state.set_fps(args.fps)
    is_demo = name == "demo"

    if is_demo and not _ANALYSIS_OK:
        state.set_backend("quicklook")
    if args.backend:
        state.set_backend(args.backend)
    if not is_demo and not _SENSOR_OK:
        print("ERROR: striqt.sensor not importable — run with --demo or install "
              "the radio stack.", file=sys.stderr)
        sys.exit(1)

    shared = SharedConfig()
    initial = {}
    if args.center_mhz is not None:
        initial["center"] = args.center_mhz * 1e6
    if args.rate_msps is not None:
        initial["sample_rate"] = args.rate_msps * 1e6
    if args.gain is not None:
        initial["gain"] = args.gain
    if args.nfft is not None:
        initial["nfft"] = args.nfft
    if args.rows is not None:
        initial["rows"] = args.rows
    if initial:
        # Same validated path as every other frontend — clamps/snaps + op log.
        shared.update(initial)

    if is_demo:
        acquirer, computer = DemoAcquirer(shared), None
    else:
        acquirer = Acquirer(shared)
        computer = Computer(acquirer, shared)
    health.bind(acquirer, shared)
    acquirer.start()
    if computer is not None:
        computer.start()

    # Core prints (operation stages, radio logs) go to the in-UI log pane
    # while curses owns the terminal; the tail is replayed on exit.
    logbuf = LogBuffer()
    import curses
    try:
        with contextlib.redirect_stdout(logbuf), contextlib.redirect_stderr(logbuf):
            curses.wrapper(ui_loop, shared, acquirer, args.fps, logbuf)
    finally:
        shared.stop()
        acquirer.join(timeout=3.0)
        if computer is not None:
            computer.join(timeout=3.0)
        print("\n".join(logbuf.tail(30)))
        print("terminal monitor stopped.")


if __name__ == "__main__":
    main()
