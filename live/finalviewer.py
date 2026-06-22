#!/usr/bin/env python3
"""
Live two-port spectrogram + PSD viewer (runs on the Mac) -- full featured.

Two display modes, chosen from the "Mode" menu:
  * Boring Mode 🥱  -- the image is one contiguous capture and refreshes wholesale
                every update (capture length == the time-scale you pick). This
                is the main view; its time axis is exact.
  * Cool Mode 😎 -- the older scrolling view. The time-scale menu sets how far
                back the pane "remembers".

The "Window (ms)" menu sets the time scale for whichever mode is active, and
the spectrogram y-axis is calibrated to it: 0 ms (now) at the top, the window
length at the bottom, newest at the top.

Pairs with airt_live_server_full.py. numpy + pyqtgraph + PyQt6 + stdlib.
Start the server first, then run this:  python3 live_viewer_full.py <ip> [--port N]
"""

import sys
import argparse
import socket
import struct
import json
import csv
import time

import numpy as np
from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import pyqtgraph.exporters

# ------------------------------- settings ----------------------------------
AIRT_HOST, AIRT_PORT = "192.168.50.1", 5005   # default = direct-Ethernet link
SCROLL_ROWS = 12                 # rows/update the server pushes in waterfall mode
WINDOW_MS_DEFAULT = 20          # time-scale menu default (milliseconds)
WINDOW_PRESETS = [10, 20, 50, 100, 200, 500, 1000]
DEFAULT_FS, DEFAULT_NFFT = 15.36e6, 1024   # server defaults (for the first request)
MAX_ROWS = 20000                 # safety cap on buffer depth / snapshot rows
PSD_YRANGE = (-80.0, 20.0)       # fixed PSD power axis (dB): -80 at bottom, +20 at top
# Span presets are the standard LTE/5G-NR sample rates (multiples of 1.92 MHz:
# x4=7.68, x8=15.36, x16=30.72, x32=61.44), which line up with the cellular
# bands this rig looks at. The menu is editable, so any rate in the AIR-T's
# 3.91-125 MS/s range can be typed too.
RATES_MHZ = [3.84, 7.68, 15.36, 30.72, 61.44]
RATE_MIN_MHZ, RATE_MAX_MHZ = 3.90625, 125.0
NFFTS = [256, 512, 1024, 2048, 4096]
# PSD pens: RX1 (cyan/yellow) and RX2 (orange/magenta), mean vs max per channel.
RX1_MEAN_PEN = pg.mkPen(color=(80, 220, 220), width=2)    # cyan
RX1_MAX_PEN = pg.mkPen(color=(245, 215, 80), width=2)     # yellow
RX2_MEAN_PEN = pg.mkPen(color=(255, 150, 70), width=2)    # orange
RX2_MAX_PEN = pg.mkPen(color=(235, 120, 235), width=2)    # magenta
HOLD1_PEN = pg.mkPen(color=(80, 220, 220, 110), width=1,   # faint cyan, dashed
                     style=QtCore.Qt.PenStyle.DashLine)
HOLD2_PEN = pg.mkPen(color=(255, 150, 70, 110), width=1,   # faint orange, dashed
                     style=QtCore.Qt.PenStyle.DashLine)
DIFF_PEN = pg.mkPen(color=(235, 235, 235), width=2)        # white
MIN1_PEN = pg.mkPen(color=(80, 220, 220, 140), width=1,    # dotted cyan
                    style=QtCore.Qt.PenStyle.DotLine)
MIN2_PEN = pg.mkPen(color=(255, 150, 70, 140), width=1,    # dotted orange
                    style=QtCore.Qt.PenStyle.DotLine)
# Display-rate presets for the "JEEZ SLOW DOWN" control -> max visual fps.
SPEED_PRESETS = [("Off (full speed)", 0.0), ("15 fps", 15.0), ("8 fps", 8.0),
                 ("4 fps", 4.0), ("2 fps", 2.0), ("1 fps", 1.0)]
# ---------------------------------------------------------------------------


def recvall(sock, n):
    chunks, got = [], 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


class Receiver(QtCore.QThread):
    frameReady = QtCore.pyqtSignal(object)
    statusChanged = QtCore.pyqtSignal(str)

    def __init__(self, host, port):
        super().__init__()
        self.host, self.port = host, port
        self._running = True
        self._sock = None
        self._lock = QtCore.QMutex()
        self._pending = {}
        self.gui_busy = False

    def stop(self):
        self._running = False

    def send_control(self, d):
        self._lock.lock()
        try:
            self._pending.update(d)
            if self._sock is not None:
                try:
                    payload = json.dumps(self._pending).encode("utf-8")
                    self._sock.sendall(struct.pack(">I", len(payload)) + payload)
                    self._pending = {}
                except Exception:
                    pass
        finally:
            self._lock.unlock()

    def run(self):
        while self._running:
            try:
                self.statusChanged.emit(f"connecting to {self.host}:{self.port} ...")
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._lock.lock()
                self._sock = sock
                if self._pending:
                    try:
                        p = json.dumps(self._pending).encode("utf-8")
                        sock.sendall(struct.pack(">I", len(p)) + p)
                        self._pending = {}
                    except Exception:
                        pass
                self._lock.unlock()
                self.statusChanged.emit(f"connected to {self.host}:{self.port}")
                while self._running:
                    hlen = struct.unpack(">I", recvall(sock, 4))[0]
                    header = json.loads(recvall(sock, hlen).decode("utf-8"))
                    rows, nfft = header["shape"]
                    nbytes = rows * nfft * 4
                    blocks = []
                    for _ in header["channels"]:
                        raw = recvall(sock, nbytes)
                        blocks.append(np.frombuffer(raw, dtype=np.float32)
                                      .reshape(rows, nfft))
                    header["blocks"] = blocks
                    if not self.gui_busy:
                        self.gui_busy = True
                        self.frameReady.emit(header)
            except Exception as e:
                self._lock.lock()
                self._sock = None
                self._lock.unlock()
                self.statusChanged.emit(f"disconnected ({e}); retrying ...")
                self.msleep(1200)


class LiveViewer(QtWidgets.QMainWindow):
    def __init__(self, host=AIRT_HOST, port=AIRT_PORT):
        super().__init__()
        self.host, self.port = host, port
        self.setWindowTitle("AIR8201 Live Spectrogram + PSD")
        self.resize(2050, 1200)

        self.replace_mode = True        # flicker is the main view; waterfall optional
        self.nfft = None
        self.center = None
        self.fs = None
        self.window_ms = WINDOW_MS_DEFAULT
        self.buffers = {}
        self.images = {}
        self.specplots = {}
        self.paused = False
        self.absolute = True
        self.auto_scale = True
        self.levels = (-90.0, -10.0)
        self.psd_port = 0               # PSD shows RX1; the channel selector was removed
        self.min_interval = 0.0         # min seconds between visual updates (0 = full)
        self._last_render = 0.0
        self._refit_pending = False     # set by span/window changes -> autoRange next frame
        self.show_diff = False          # RX1-RX2 difference view
        self.peak_marker = True         # label the strongest bin
        self.peak_hold = False          # hold max-ever envelope
        self.show_min = False           # min-over-time (noise floor) traces
        self.psd_yspan = None           # fixed PSD y-axis span in dB (None = auto)
        self.crosshair = True           # mouse readout on the PSD
        self.hold1 = self.hold2 = None  # running peak-hold arrays
        self._frames, self._t_fps, self._fps = 0, time.time(), 0.0
        self._geom = self._freqs = None

        self._build_ui()
        self.receiver = Receiver(self.host, self.port)
        self.receiver.frameReady.connect(self.on_frame)
        self.receiver.statusChanged.connect(lambda s: self.status_label.setText(s))
        self.receiver.start()
        # Start in flicker mode: ask the server for a snapshot of this many rows
        # (using the server defaults until the first frame tells us the real
        # fs / nfft). Queued until the socket connects.
        self.receiver.send_control(
            {"rows": self._rows_for_window(DEFAULT_FS, DEFAULT_NFFT)})

    # --------------------------------------------------- rows <-> time helper
    def _rows_for_window(self, fs, nfft):
        """How many FFT rows make up `window_ms` at this fs / nfft."""
        return int(max(1, min(round(self.window_ms / 1000.0 * fs / nfft), MAX_ROWS)))

    # ----------------------------------------------------------------- UI
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.addWidget(self._controls())

        self.status_label = QtWidgets.QLabel("starting ...")
        self.status_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.status_label)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setWordWrap(True)
        outer.addWidget(self.meta_label)

        # Pinned band-power monitor -- fixed on screen so the level stays in one
        # place (Eric's flicker ask), readable even while the spectrogram updates.
        self.band_label = QtWidgets.QLabel("Band monitor: --")
        self.band_label.setStyleSheet(
            "font-family: Menlo, Consolas, monospace; font-size: 15px; "
            "font-weight: bold; color: #ffd24a; background: #1b2733; "
            "padding: 5px; border-radius: 4px;")
        outer.addWidget(self.band_label)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        outer.addWidget(split, stretch=1)

        self.graphics = pg.GraphicsLayoutWidget()
        titles = {0: "Spectrogram Port 0 -- RX1", 1: "Spectrogram Port 1 -- RX2"}
        col = 0
        for ch in (0, 1):
            plot = self.graphics.addPlot(row=0, col=col)
            plot.setTitle(titles[ch])
            plot.setLabel("bottom", "Frequency (MHz)")
            plot.setLabel("left", "Time (ms, 0 = now at top)")
            plot.showGrid(x=True, y=True, alpha=0.2)
            # invert Y so the time axis reads 0 at the top (newest) and the
            # window length at the bottom (oldest) -- newest data sits on top.
            plot.getViewBox().invertY(True)
            img = pg.ImageItem()
            plot.addItem(img)
            hist = pg.HistogramLUTItem()
            hist.setImageItem(img)
            hist.gradient.loadPreset("viridis")
            self.graphics.addItem(hist, row=0, col=col + 1)
            self.specplots[ch] = plot
            self.images[ch] = img
            col += 2
        split.addWidget(self.graphics)

        self.psd_plot = pg.PlotWidget()
        self.psd_plot.setTitle("Power Spectral Density (RX1 + RX2)")
        self.psd_plot.setLabel("bottom", "Frequency (MHz)")
        self.psd_plot.setLabel("left", "Power (dB)")
        self.psd_plot.setYRange(*PSD_YRANGE, padding=0)   # fixed -80..+20 dB
        self.psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self.psd_plot.addLegend(offset=(20, 20))
        self.psd_rx1_mean = self.psd_plot.plot(name="RX1 Mean", pen=RX1_MEAN_PEN)
        self.psd_rx1_max = self.psd_plot.plot(name="RX1 Max", pen=RX1_MAX_PEN)
        self.psd_rx2_mean = self.psd_plot.plot(name="RX2 Mean", pen=RX2_MEAN_PEN)
        self.psd_rx2_max = self.psd_plot.plot(name="RX2 Max", pen=RX2_MAX_PEN)
        # peak-hold envelopes (faint, dashed) -- max-ever per bin while held
        self.psd_hold1 = self.psd_plot.plot(name="RX1 hold", pen=HOLD1_PEN)
        self.psd_hold2 = self.psd_plot.plot(name="RX2 hold", pen=HOLD2_PEN)
        self.psd_hold1.setVisible(False)
        self.psd_hold2.setVisible(False)
        # RX1 - RX2 difference (directivity), hidden until toggled on
        self.psd_diff = self.psd_plot.plot(name="RX1-RX2 (dB)", pen=DIFF_PEN)
        self.psd_diff.setVisible(False)
        # min-over-time traces (noise floor), hidden until toggled on
        self.psd_min1 = self.psd_plot.plot(name="RX1 min", pen=MIN1_PEN)
        self.psd_min2 = self.psd_plot.plot(name="RX2 min", pen=MIN2_PEN)
        self.psd_min1.setVisible(False)
        self.psd_min2.setVisible(False)
        # Single measurement band (green): defines the band-power monitor AND is
        # what "Tune to selection" tunes to. No fill -- just two thin draggable
        # edge lines, so it doesn't sit as a box over the PSD.
        self.meas_region = pg.LinearRegionItem(
            brush=(120, 255, 160, 0),
            pen=pg.mkPen((120, 255, 160), width=2))
        self.meas_region.setZValue(-4)
        self.meas_region.sigRegionChanged.connect(lambda *_: self._update_band())
        self.psd_plot.addItem(self.meas_region)
        # peak marker: dot + label on the strongest bin of the displayed max curve
        self.peak_dot = pg.ScatterPlotItem(size=10, brush=pg.mkBrush(245, 215, 80),
                                           pen=pg.mkPen("k"))
        self.peak_text = pg.TextItem(color=(245, 215, 80), anchor=(0.5, 1.2))
        self.psd_plot.addItem(self.peak_dot)
        self.psd_plot.addItem(self.peak_text)
        # crosshair + readout
        cpen = pg.mkPen((150, 150, 150), width=1,
                        style=QtCore.Qt.PenStyle.DashLine)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=cpen)
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=cpen)
        self.cursor_text = pg.TextItem(color=(210, 210, 210), anchor=(0, 1))
        for it in (self.vline, self.hline, self.cursor_text):
            self.psd_plot.addItem(it, ignoreBounds=True)
        self._mouse_proxy = pg.SignalProxy(self.psd_plot.scene().sigMouseMoved,
                                           rateLimit=60, slot=self._on_mouse_moved)
        split.addWidget(self.psd_plot)
        split.setSizes([560, 500])      # taller PSD so the curve values are readable

    def _controls(self):
        box = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(4, 4, 4, 4)

        radio = QtWidgets.QGroupBox("Radio (controls the AIR-T)")
        rl = QtWidgets.QHBoxLayout(radio)
        rl.addWidget(QtWidgets.QLabel("Center (MHz):"))
        self.center_spin = QtWidgets.QDoubleSpinBox()
        self.center_spin.setRange(300.0, 6000.0)
        self.center_spin.setDecimals(3)
        self.center_spin.setValue(1955.0)
        self.center_spin.setSingleStep(5.0)
        self.center_spin.editingFinished.connect(
            lambda: self.receiver.send_control({"center": self.center_spin.value() * 1e6}))
        rl.addWidget(self.center_spin)
        rl.addWidget(QtWidgets.QLabel("Span (MS/s):"))
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.setEditable(True)
        self.rate_combo.setValidator(
            QtGui.QDoubleValidator(RATE_MIN_MHZ, RATE_MAX_MHZ, 5, self))
        self.rate_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for r in RATES_MHZ:
            self.rate_combo.addItem(f"{r:.2f}", r)
        self.rate_combo.setCurrentText("15.36")
        self.rate_combo.activated.connect(self._change_rate)
        self.rate_combo.lineEdit().editingFinished.connect(self._change_rate)
        rl.addWidget(self.rate_combo)
        rl.addWidget(QtWidgets.QLabel("Gain (dB):"))
        self.gain_spin = QtWidgets.QDoubleSpinBox()
        self.gain_spin.setRange(-30.0, 0.0)
        self.gain_spin.setSingleStep(0.5)
        self.gain_spin.setValue(0.0)
        self.gain_spin.editingFinished.connect(
            lambda: self.receiver.send_control({"gain": self.gain_spin.value()}))
        rl.addWidget(self.gain_spin)
        rl.addWidget(QtWidgets.QLabel("FFT:"))
        self.nfft_combo = QtWidgets.QComboBox()
        for n in NFFTS:
            self.nfft_combo.addItem(str(n), n)
        self.nfft_combo.setCurrentText("1024")
        self.nfft_combo.activated.connect(self._change_nfft)
        rl.addWidget(self.nfft_combo)
        self.tune_btn = QtWidgets.QPushButton("Tune to selection")
        self.tune_btn.clicked.connect(self._tune_to_selection)
        rl.addWidget(self.tune_btn)
        grid.addWidget(radio, 0, 0)

        disp = QtWidgets.QGroupBox("Display")
        dl = QtWidgets.QHBoxLayout(disp)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        dl.addWidget(self.pause_btn)
        dl.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Boring Mode 🥱", "Cool Mode 😎"])
        self.mode_combo.setCurrentText("Boring Mode 🥱")   # main view = flicker
        self.mode_combo.currentTextChanged.connect(self._toggle_mode)
        dl.addWidget(self.mode_combo)
        dl.addWidget(QtWidgets.QLabel("Window (ms):"))
        self.win_combo = QtWidgets.QComboBox()
        self.win_combo.setEditable(True)
        self.win_combo.setValidator(QtGui.QIntValidator(5, 60000, self))
        self.win_combo.addItems([str(v) for v in WINDOW_PRESETS])
        self.win_combo.setCurrentText(str(WINDOW_MS_DEFAULT))
        self.win_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.win_combo.activated.connect(self._change_window)             # picked
        self.win_combo.lineEdit().editingFinished.connect(self._change_window)  # typed
        dl.addWidget(self.win_combo)
        dl.addWidget(QtWidgets.QLabel("JEEZ SLOW DOWN:"))
        self.speed_combo = QtWidgets.QComboBox()
        for label, _ in SPEED_PRESETS:
            self.speed_combo.addItem(label)
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.activated.connect(self._change_speed)
        dl.addWidget(self.speed_combo)
        self.auto_chk = QtWidgets.QCheckBox("Auto color")
        self.auto_chk.setChecked(True)
        self.auto_chk.stateChanged.connect(lambda s: setattr(self, "auto_scale", bool(s)))
        dl.addWidget(self.auto_chk)
        self.abs_chk = QtWidgets.QCheckBox("Absolute RF")
        self.abs_chk.setChecked(True)
        self.abs_chk.stateChanged.connect(self._toggle_absolute)
        dl.addWidget(self.abs_chk)
        self.reset_btn = QtWidgets.QPushButton("Reset view")
        self.reset_btn.clicked.connect(self._reset_view)
        dl.addWidget(self.reset_btn)
        self.csv_btn = QtWidgets.QPushButton("Save PSD CSV")
        self.csv_btn.clicked.connect(self._save_csv)
        dl.addWidget(self.csv_btn)
        self.png_btn = QtWidgets.QPushButton("Export PNG")
        self.png_btn.clicked.connect(self._export_png)
        dl.addWidget(self.png_btn)
        grid.addWidget(disp, 1, 0)

        psd = QtWidgets.QGroupBox("PSD tools")
        pl = QtWidgets.QHBoxLayout(psd)
        self.diff_chk = QtWidgets.QCheckBox("RX1\u2212RX2 diff")
        self.diff_chk.stateChanged.connect(self._toggle_diff)
        pl.addWidget(self.diff_chk)
        self.peak_chk = QtWidgets.QCheckBox("Peak marker")
        self.peak_chk.setChecked(True)
        self.peak_chk.stateChanged.connect(
            lambda s: (setattr(self, "peak_marker", bool(s)), self._update_psd()))
        pl.addWidget(self.peak_chk)
        self.hold_chk = QtWidgets.QCheckBox("Peak hold")
        self.hold_chk.stateChanged.connect(self._toggle_hold)
        pl.addWidget(self.hold_chk)
        self.holdclear_btn = QtWidgets.QPushButton("Clear hold")
        self.holdclear_btn.clicked.connect(self._clear_hold)
        pl.addWidget(self.holdclear_btn)
        self.min_chk = QtWidgets.QCheckBox("Min trace")
        self.min_chk.stateChanged.connect(self._toggle_min)
        pl.addWidget(self.min_chk)
        self.cross_chk = QtWidgets.QCheckBox("Crosshair")
        self.cross_chk.setChecked(True)
        self.cross_chk.stateChanged.connect(self._toggle_crosshair)
        pl.addWidget(self.cross_chk)
        pl.addWidget(QtWidgets.QLabel("Y span (dB):"))
        self.yspan_combo = QtWidgets.QComboBox()
        self.yspan_combo.addItems(["Auto", "10", "20", "40", "60"])
        self.yspan_combo.setCurrentText("Auto")
        self.yspan_combo.activated.connect(self._change_yspan)
        pl.addWidget(self.yspan_combo)
        pl.addStretch(1)
        grid.addWidget(psd, 2, 0)
        return box

    # ------------------------------------------------- handlers
    def _change_rate(self, *args):
        try:
            mhz = float(self.rate_combo.currentText())
        except (ValueError, TypeError):
            return
        fs = max(RATE_MIN_MHZ, min(mhz, RATE_MAX_MHZ)) * 1e6
        d = {"sample_rate": fs}
        if self.replace_mode:           # keep the snapshot == window_ms at the new fs
            d["rows"] = self._rows_for_window(fs, self.nfft or DEFAULT_NFFT)
        self.receiver.send_control(d)
        self._refit_pending = True      # span changed -> refit the x-axis next frame

    def _change_speed(self, *args):
        label = self.speed_combo.currentText()
        fps = dict(SPEED_PRESETS).get(label, 0.0)
        self.min_interval = 0.0 if fps <= 0 else 1.0 / fps

    def _toggle_diff(self, state):
        # Diff view shows RX1-RX2 (directivity) on its own scale; hide the four
        # raw curves so autoRange fits the difference, not the -90..-10 spectra.
        self.show_diff = bool(state)
        for c in (self.psd_rx1_mean, self.psd_rx1_max,
                  self.psd_rx2_mean, self.psd_rx2_max):
            c.setVisible(not self.show_diff)
        self.psd_diff.setVisible(self.show_diff)
        self.psd_hold1.setVisible(self.peak_hold and not self.show_diff)
        self.psd_hold2.setVisible(self.peak_hold and not self.show_diff)
        self.psd_min1.setVisible(self.show_min and not self.show_diff)
        self.psd_min2.setVisible(self.show_min and not self.show_diff)
        if self.show_diff:                      # the marker tracks raw maxima
            self.peak_dot.clear()
            self.peak_text.setText("")
        self._update_psd()
        self._fit_psd()

    def _toggle_hold(self, state):
        self.peak_hold = bool(state)
        self.psd_hold1.setVisible(self.peak_hold and not self.show_diff)
        self.psd_hold2.setVisible(self.peak_hold and not self.show_diff)
        if not self.peak_hold:
            self.hold1 = self.hold2 = None
        self._update_psd()

    def _clear_hold(self):
        self.hold1 = self.hold2 = None
        self._update_psd()

    def _toggle_min(self, state):
        self.show_min = bool(state)
        self.psd_min1.setVisible(self.show_min and not self.show_diff)
        self.psd_min2.setVisible(self.show_min and not self.show_diff)
        self._update_psd()

    def _toggle_crosshair(self, state):
        self.crosshair = bool(state)
        for it in (self.vline, self.hline, self.cursor_text):
            it.setVisible(self.crosshair)

    def _change_yspan(self, *args):
        t = self.yspan_combo.currentText()
        if t == "Auto":
            self.psd_yspan = None
            self.psd_plot.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        else:
            try:
                self.psd_yspan = float(t)
            except ValueError:
                return
            self._apply_psd_yspan()

    def _apply_psd_yspan(self):
        # Lock the PSD y-axis to a fixed dB span, tracking the strongest signal
        # so it stays in view. Auto (None) leaves the normal auto-range alone.
        if self.psd_yspan is None or self.show_diff:
            return
        peak = None
        for ch in (0, 1):
            b = self.buffers.get(ch)
            if (b is not None and self._freqs is not None
                    and b.shape[1] == self._freqs.size):
                v = float(b.max())
                peak = v if peak is None else max(peak, v)
        if peak is not None:
            head = 0.1 * self.psd_yspan          # small headroom above the peak
            self.psd_plot.getViewBox().setYRange(
                peak - self.psd_yspan + head, peak + head, padding=0)

    def _on_mouse_moved(self, evt):
        if not self.crosshair:
            return
        pos = evt[0]
        vb = self.psd_plot.getPlotItem().vb
        if not self.psd_plot.sceneBoundingRect().contains(pos):
            return
        mp = vb.mapSceneToView(pos)
        self.vline.setPos(mp.x())
        self.hline.setPos(mp.y())
        self.cursor_text.setText(f"{mp.x():.3f} MHz, {mp.y():.1f} dB")
        self.cursor_text.setPos(mp.x(), mp.y())

    def _change_nfft(self):
        nfft = self.nfft_combo.currentData()
        d = {"nfft": nfft}
        if self.replace_mode:           # keep the snapshot == window_ms at the new nfft
            d["rows"] = self._rows_for_window(self.fs or DEFAULT_FS, nfft)
        self.receiver.send_control(d)

    def _tune_to_selection(self):
        lo, hi = self.meas_region.getRegion()
        sel = 0.5 * (lo + hi)
        if self.absolute:
            new_c = sel * 1e6
        else:
            base = self.center if self.center else self.center_spin.value() * 1e6
            new_c = base + sel * 1e6
        new_c = max(300e6, min(6e9, new_c))
        self.center_spin.setValue(new_c / 1e6)
        self.receiver.send_control({"center": new_c})

    def _toggle_pause(self, checked):
        self.paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")
        for plot in self.specplots.values():
            plot.setMouseEnabled(x=checked, y=checked)
        self.psd_plot.setMouseEnabled(x=True, y=True)

    def _toggle_mode(self, mode):
        # Boring Mode 🥱 == capture length == window: the server produces a full window
        # of rows per frame, so the image refreshes wholesale. Cool Mode 😎 == the
        # old scrolling view (SCROLL_ROWS rows/frame, buffer = window of memory).
        self.replace_mode = (mode == "Boring Mode 🥱")
        if self.replace_mode:
            rows = self._rows_for_window(self.fs or DEFAULT_FS, self.nfft or DEFAULT_NFFT)
        else:
            rows = SCROLL_ROWS
        self._clear_buffers()           # wipe residue; on_frame rebuilds to the new depth
        self.receiver.send_control({"rows": int(rows)})

    def _clear_buffers(self):
        # Reset both panes so a mode switch starts clean -- no half-scrolled rows
        # left behind.
        if self.nfft is None or not self.buffers:
            return
        for ch in self.buffers:
            self.buffers[ch][:] = -150.0
            self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                     levels=self.levels)

    def _toggle_absolute(self, state):
        self.absolute = bool(state)
        self._geom = None
        if self.fs is not None and self.buffers:
            self._apply_geometry()
            self._update_psd()      # push the new baseband/RF x-data to the curves
        # The x extent just jumped (RF center <-> baseband), so re-fit the view
        # AFTER the curves hold the new data; otherwise the PSD condenses into a
        # sliver where the data no longer is.
        for plot in self.specplots.values():
            plot.getViewBox().autoRange()
        self._fit_psd()

    def _change_window(self, *args):
        try:
            ms = int(round(float(self.win_combo.currentText())))
        except (ValueError, TypeError):
            return
        ms = max(5, min(ms, 60000))
        if ms == self.window_ms:
            return
        self.window_ms = ms
        self._refit_pending = True      # time span changed -> refit y next frame
        if self.replace_mode:
            # flicker: ask the server for a snapshot of the new length. The buffer
            # resizes to whatever the server actually returns on the next frame.
            self.receiver.send_control(
                {"rows": self._rows_for_window(self.fs or DEFAULT_FS,
                                               self.nfft or DEFAULT_NFFT)})
        elif self.fs and self.nfft and self.buffers:
            # waterfall: resize the memory buffer now, preserving the newest rows.
            depth = self._rows_for_window(self.fs, self.nfft)
            newb = {}
            for ch in self.buffers:
                old = self.buffers[ch]
                buf = np.full((depth, self.nfft), -150.0, np.float32)
                k = min(depth, old.shape[0])
                buf[:k] = old[:k]       # newest live at the top (row 0)
                newb[ch] = buf
            self.buffers = newb
            self._geom = None
            self._apply_geometry()

    def _reset_view(self):
        for plot in self.specplots.values():
            plot.getViewBox().enableAutoRange(True)
            plot.autoRange()
        self._fit_psd()

    def _fit_psd(self):
        # Fit the frequency (x) axis to the span; keep power (y) pinned at the
        # fixed -80..+20 dB range -- except in diff view, where the difference
        # has its own small scale and we let it auto-fit.
        vb = self.psd_plot.getViewBox()
        if self.show_diff:
            vb.enableAutoRange(x=True, y=True)
            return
        if self._freqs is not None and self._freqs.size:
            vb.setXRange(float(self._freqs.min()), float(self._freqs.max()),
                         padding=0.01)
        vb.setYRange(*PSD_YRANGE, padding=0)

    # ------------------------------------------------- geometry / frames
    def _freqs_mhz(self):
        base = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs))
        return ((self.center + base) if self.absolute else base) / 1e6

    def _edges_mhz(self):
        if self.absolute:
            return ((self.center - self.fs/2)/1e6, (self.center + self.fs/2)/1e6)
        return (-self.fs/2/1e6, self.fs/2/1e6)

    def _apply_geometry(self):
        if not self.buffers or self.fs is None or self.nfft is None:
            return
        depth = next(iter(self.buffers.values())).shape[0]
        geom = (self.center, self.fs, self.nfft, depth, self.absolute)
        if geom == self._geom:
            return
        self._geom = geom
        self._freqs = self._freqs_mhz()
        f0, f1 = self._edges_mhz()
        # Time axis in real milliseconds: each row is one FFT over nfft samples
        # (nfft/fs s); the pane holds `depth` rows -> spans depth*nfft/fs.
        t_ms = depth * self.nfft / self.fs * 1e3
        for ch in self.buffers:
            self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                     levels=self.levels)
            self.images[ch].setRect(QtCore.QRectF(f0, 0, f1 - f0, t_ms))
        # Keep the measurement band where the user put it, unless a retune /
        # mode flip pushed it off-screen -- then drop it on a narrow middle band.
        ml, mh = sorted(self.meas_region.getRegion())
        if mh <= f0 or ml >= f1 or (mh - ml) <= 0:
            self.meas_region.setRegion([f0 + 0.45 * (f1 - f0), f0 + 0.55 * (f1 - f0)])

    def on_frame(self, header):
        try:
            if self.paused:
                self._meta(header, live=False)
                return

            # "JEEZ SLOW DOWN": cap the visual update rate. We drop the frame's
            # render (and, in waterfall, its rows) when it arrives too soon. This
            # slows the display, not the radio -- flicker just refreshes less
            # often; waterfall scrolls slower but skips the dropped rows.
            now2 = time.time()
            if self.min_interval > 0.0 and (now2 - self._last_render) < self.min_interval:
                self._meta(header, live=True)
                return
            self._last_render = now2

            # fps counts rendered frames, so it tracks the actual display rate.
            self._frames += 1
            now = time.time()
            if now - self._t_fps >= 1.0:
                self._fps = self._frames / (now - self._t_fps)
                self._frames, self._t_fps = 0, now

            nfft = header["nfft"]
            center = header["center"]
            fs = header["fs"]
            n_rows = header["rows"]
            chans = header["channels"]

            # Target display depth. Boring Mode 🥱 shows exactly what arrived; waterfall
            # holds `window_ms` worth of memory regardless of the per-frame count.
            if self.replace_mode:
                depth = int(min(n_rows, MAX_ROWS))
            else:
                depth = self._rows_for_window(fs, nfft)

            cur_depth = (next(iter(self.buffers.values())).shape[0]
                         if self.buffers else -1)
            if (nfft != self.nfft or center != self.center or fs != self.fs
                    or not self.buffers or cur_depth != depth):
                self.nfft, self.center, self.fs = nfft, center, fs
                self.buffers = {ch: np.full((depth, nfft), -150.0, np.float32)
                                for ch in chans}
                self.hold1 = self.hold2 = None   # peak-hold is freq-specific
                self._geom = None
                self._apply_geometry()

            for ch, block in zip(chans, header["blocks"]):
                buf = self.buffers[ch]
                d = buf.shape[0]
                blk = block[::-1]                 # server sends oldest-first; flip so
                                                  # row 0 == newest (top, with invertY)
                if self.replace_mode:
                    buf[:] = -150.0
                    k = min(blk.shape[0], d)
                    buf[:k, :] = blk[:k, :]
                else:
                    m = min(blk.shape[0], d)
                    buf = np.roll(buf, m, axis=0)  # push older rows down
                    buf[:m, :] = blk[:m, :]        # newest at the top
                    self.buffers[ch] = buf

            if self.auto_scale:
                samp = np.concatenate([b[::3, ::4].ravel() for b in self.buffers.values()])
                vmin, vmax = float(np.percentile(samp, 5)), float(np.percentile(samp, 99))
                if vmax - vmin < 5:
                    vmax = vmin + 5
                self.levels = (vmin, vmax)
            else:
                lv = self.images[0].getLevels()
                if lv is not None:
                    try:
                        self.levels = (float(lv[0]), float(lv[1]))
                    except (TypeError, IndexError):
                        pass

            for ch in self.buffers:
                self.images[ch].setImage(self.buffers[ch], autoLevels=False,
                                         levels=self.levels)
            self._update_psd()
            self._update_band()
            if self._refit_pending:     # span/window changed -> fit to new extent
                for plot in self.specplots.values():
                    plot.getViewBox().autoRange()
                self._fit_psd()
                self._refit_pending = False
            self._apply_psd_yspan()     # keep the PSD y-axis locked if requested
            self._meta(header, live=True)
        finally:
            self.receiver.gui_busy = False

    def _update_band(self):
        if self._freqs is None or not self.buffers:
            self.band_label.setText("Band monitor: --")
            return
        lo, hi = sorted(self.meas_region.getRegion())
        mask = (self._freqs >= lo) & (self._freqs <= hi)
        nb = int(mask.sum())
        if nb == 0:
            self.band_label.setText(
                f"Band {lo:.3f}\u2013{hi:.3f} MHz: no bins in range")
            return
        band, qual = {}, {}
        for ch in (0, 1):
            b = self.buffers.get(ch)
            if b is None or b.shape[1] != self._freqs.size:
                continue
            lin = 10.0 ** (b.mean(axis=0) / 10.0)     # time-avg PSD, dB -> linear
            band[ch] = 10.0 * np.log10(lin[mask].mean())          # in-band level
            qual[ch] = band[ch] - 10.0 * np.log10(lin.mean())     # vs span avg (RSRQ-ish)
        seg = [f"Band {lo:.3f}\u2013{hi:.3f} MHz ({nb} bins)"]
        if 0 in band:
            seg.append(f"RX1 {band[0]:.1f} dB")
        if 1 in band:
            seg.append(f"RX2 {band[1]:.1f} dB")
        if 0 in band and 1 in band:
            seg.append(f"\u0394 {band[0] - band[1]:+.1f} dB")     # directivity
        qseg = [f"{'RX1' if ch == 0 else 'RX2'} {qual[ch]:+.1f}"
                for ch in (0, 1) if ch in qual]
        if qseg:
            seg.append("Q " + " ".join(qseg) + " dB")
        self.band_label.setText("   |   ".join(seg))

    def _update_psd(self):
        if self._freqs is None:
            return
        b0 = self.buffers.get(0)
        b1 = self.buffers.get(1)
        ok0 = b0 is not None and b0.shape[1] == self._freqs.size
        ok1 = b1 is not None and b1.shape[1] == self._freqs.size
        m0 = b0.mean(axis=0) if ok0 else None
        x0 = b0.max(axis=0) if ok0 else None
        m1 = b1.mean(axis=0) if ok1 else None
        x1 = b1.max(axis=0) if ok1 else None

        if self.show_diff:
            if m0 is not None and m1 is not None:
                self.psd_diff.setData(self._freqs, m0 - m1)
            return                      # diff view hides everything else

        if ok0:
            self.psd_rx1_mean.setData(self._freqs, m0)
            self.psd_rx1_max.setData(self._freqs, x0)
        if ok1:
            self.psd_rx2_mean.setData(self._freqs, m1)
            self.psd_rx2_max.setData(self._freqs, x1)

        if self.show_min:
            if ok0:
                self.psd_min1.setData(self._freqs, b0.min(axis=0))
            if ok1:
                self.psd_min2.setData(self._freqs, b1.min(axis=0))

        if self.peak_hold:
            if x0 is not None:
                self.hold1 = x0 if self.hold1 is None or self.hold1.size != x0.size \
                    else np.maximum(self.hold1, x0)
                self.psd_hold1.setData(self._freqs, self.hold1)
            if x1 is not None:
                self.hold2 = x1 if self.hold2 is None or self.hold2.size != x1.size \
                    else np.maximum(self.hold2, x1)
                self.psd_hold2.setData(self._freqs, self.hold2)

        if self.peak_marker and x0 is not None:
            i = int(np.argmax(x0))
            fpk, ppk = float(self._freqs[i]), float(x0[i])
            self.peak_dot.setData([fpk], [ppk])
            self.peak_text.setText(f"{fpk:.3f} MHz\n{ppk:.1f} dB")
            self.peak_text.setPos(fpk, ppk)
        else:
            self.peak_dot.clear()
            self.peak_text.setText("")

    def _meta(self, h, live):
        depth = next(iter(self.buffers.values())).shape[0] if self.buffers else 0
        win_ms = depth * h["nfft"] / h["fs"] * 1e3 if depth and h["fs"] else 0.0
        mode = "flicker" if self.replace_mode else "waterfall"
        self.meta_label.setText(
            f"{'LIVE' if live else 'PAUSED'} | center {h['center']/1e6:.3f} MHz | "
            f"span {h['fs']/1e6:.2f} MS/s | gain {h.get('gain',0):.1f} dB | "
            f"FFT {h['nfft']} | {mode} | window {win_ms:.0f} ms ({depth} rows) | "
            f"scale {'auto' if self.auto_scale else 'manual'} "
            f"[{self.levels[0]:.0f},{self.levels[1]:.0f}] | "
            f"{'absolute RF' if self.absolute else 'baseband'} | {self._fps:.0f} fps")

    # ------------------------------------------------- exports
    def _save_csv(self):
        if self.nfft is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save PSD CSV", "live_psd.csv", "CSV (*.csv)")
        if not path:
            return
        freqs = self._freqs if self._freqs is not None else self._freqs_mhz()
        b0 = self.buffers.get(0)
        b1 = self.buffers.get(1)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["freq_mhz", "rx1_mean_db", "rx1_max_db",
                        "rx2_mean_db", "rx2_max_db"])
            m0, x0 = b0.mean(axis=0), b0.max(axis=0)
            if b1 is not None:
                m1, x1 = b1.mean(axis=0), b1.max(axis=0)
            else:
                m1 = x1 = np.full(freqs.shape, np.nan)
            for i, fr in enumerate(freqs):
                w.writerow([f"{fr:.6f}", f"{m0[i]:.3f}", f"{x0[i]:.3f}",
                            f"{m1[i]:.3f}", f"{x1[i]:.3f}"])
        self.status_label.setText(f"saved {path}")

    def _settings_caption(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        mode = "flicker" if self.replace_mode else "waterfall"
        c = self.center / 1e6 if self.center else self.center_spin.value()
        fs = self.fs / 1e6 if self.fs else 0.0
        depth = next(iter(self.buffers.values())).shape[0] if self.buffers else 0
        win_ms = (depth * self.nfft / self.fs * 1e3
                  if depth and self.fs and self.nfft else self.window_ms)
        return (f"{ts}   center {c:.3f} MHz   span {fs:.2f} MS/s   "
                f"gain {self.gain_spin.value():.1f} dB   FFT {self.nfft}   "
                f"{mode}   window {win_ms:.0f} ms   "
                f"{'absolute RF' if self.absolute else 'baseband'}   "
                f"color {'auto' if self.auto_scale else 'manual'} "
                f"[{self.levels[0]:.0f},{self.levels[1]:.0f}] dB")

    def _stamp_png(self, path, caption):
        img = QtGui.QImage(path)
        if img.isNull():
            return
        strip = 30
        out = QtGui.QImage(img.width(), img.height() + strip,
                           QtGui.QImage.Format.Format_ARGB32)
        out.fill(QtGui.QColor("#101418"))
        p = QtGui.QPainter(out)
        p.drawImage(0, 0, img)
        p.setPen(QtGui.QColor("#d0d0d0"))
        font = QtGui.QFont("Menlo")
        font.setPointSize(10)
        p.setFont(font)
        p.drawText(QtCore.QRectF(10, img.height(), img.width() - 20, strip),
                   int(QtCore.Qt.AlignmentFlag.AlignVCenter
                       | QtCore.Qt.AlignmentFlag.AlignLeft),
                   caption)
        p.end()
        out.save(path)

    def _export_png(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export PNG", "live_view.png", "PNG (*.png)")
        if not path:
            return
        pg.exporters.ImageExporter(self.graphics.scene()).export(path)
        self._stamp_png(path, self._settings_caption())   # caption with settings
        self.status_label.setText(f"exported {path}")

    def closeEvent(self, event):
        self.receiver.stop()
        self.receiver.wait(2000)
        super().closeEvent(event)


def main():
    ap = argparse.ArgumentParser(description="AIR-T live spectrogram viewer")
    ap.add_argument("host", nargs="?", default=AIRT_HOST,
                    help=f"server address (default {AIRT_HOST} = the Ethernet "
                         f"link; pass the AIR-T's Wi-Fi IP to use the router)")
    ap.add_argument("--port", type=int, default=AIRT_PORT)
    args = ap.parse_args()
    pg.setConfigOptions(antialias=False, imageAxisOrder="row-major",
                        background="#101418", foreground="#d0d0d0")
    app = QtWidgets.QApplication(sys.argv[:1])
    LiveViewer(args.host, args.port).show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()