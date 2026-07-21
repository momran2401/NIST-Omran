"""Recording orchestration: release live radio, supervise sweep, resume live."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from . import state
from .dsp import aligned_nfft
from .operations import OPERATIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECORDINGS_DIR = Path(
    os.environ.get("RADIO_RECORDINGS_DIR", REPO_ROOT / "recordings")
).expanduser()


class RecordingManager:
    def __init__(self, acquirer, shared, *, demo=False):
        self.acquirer = acquirer
        self.shared = shared
        self.demo = demo
        self._lock = asyncio.Lock()
        self._task = None
        self._process = None
        self._stop = asyncio.Event()
        self._thread_stop = threading.Event()
        self._status = {"state": "idle"}

    def status(self):
        return dict(self._status)

    def active(self):
        return self._status.get("state") in {"starting", "recording", "stopping"}

    def defaults(self):
        cfg = self.shared.snapshot()
        return {
            "center_frequency": float(cfg.center),
            "sample_rate": float(cfg.sample_rate),
            "gain": float(cfg.gain),
            "capture_duration": max(float(cfg.duration), 0.1),
            "directory": str(DEFAULT_RECORDINGS_DIR),
            "include_raw_iq": False,
        }

    async def start(self, request):
        async with self._lock:
            if self.active():
                raise RuntimeError("a recording is already running")
            duration = request.get("duration")
            duration = float(duration) if duration not in (None, "") else None
            if duration is not None and duration <= 0:
                raise ValueError("duration must be positive or blank")
            directory = Path(request.get("directory") or DEFAULT_RECORDINGS_DIR).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            radio_id = str(request.get("radio_id") or state.DEVICE).replace("/", "_")
            output = directory / radio_id / f"{stamp}.zarr.zip"
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise FileExistsError(f"recording output already exists: {output}")
            op_id = OPERATIONS.begin("record", f"record sweep to {output}")
            self._stop = asyncio.Event()
            self._thread_stop = threading.Event()
            self._status = {"state": "starting", "op_id": op_id,
                            "output": str(output), "started_at": time.time(),
                            "duration": duration, "captures": 0, "elapsed_s": 0.0}
            self._task = asyncio.create_task(
                self._run(request, output, duration, op_id), name="radio-recording")
            return self.status()

    async def stop(self):
        if not self.active():
            return self.status()
        self._status["state"] = "stopping"
        self._stop.set()
        self._thread_stop.set()
        proc = self._process
        if proc is not None and proc.returncode is None:
            proc.send_signal(signal.SIGINT)
        return self.status()

    async def shutdown(self):
        await self.stop()
        if self._task:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self._task, 15)

    def _default_spec(self, request, output):
        cfg = self.shared.snapshot()
        center = float(request.get("center_frequency", cfg.center))
        sample_rate = float(request.get("sample_rate", cfg.sample_rate))
        gain = float(request.get("gain", cfg.gain))
        raw = "\n  iq_waveform: {}" if request.get("include_raw_iq") else ""
        freq_res = sample_rate / max(aligned_nfft(int(cfg.nfft)), 1)
        capture_duration = max(float(request.get("capture_duration") or cfg.duration or 0.1), 0.001)
        ports = ", ".join(str(p) for p in state.CHANNELS)
        backend = "numpy" if self.demo else "cupy"
        binding = "noise" if self.demo else (state.DEVICE if state.DEVICE.startswith("air") else "air8201b")
        source_extra = "\n  num_rx_ports: %d" % len(state.CHANNELS) if self.demo else ""
        capture_extra = "\n    noise_psd: 1e-17" if self.demo else f"\n    center_frequency: {center!r}\n    gain: {gain!r}"
        return f'''sensor_binding: {binding}
source:
  master_clock_rate: 125e6
  array_backend: {backend}{source_extra}
captures:
  - port: [{ports}]
    duration: {capture_duration!r}
    sample_rate: {sample_rate!r}
    backend_sample_rate: {float(cfg.backend_sample_rate or sample_rate)!r}
    analysis_bandwidth: {float(cfg.analysis_bandwidth)!r}
    lo_shift: {str(cfg.lo_shift)!r}
    host_resample: {str(bool(cfg.host_resample)).lower()}{capture_extra}
analysis:
  spectrogram:
    frequency_resolution: {freq_res!r}
    fractional_overlap: {str(cfg.fractional_overlap)!r}
    window_fill: {str(cfg.window_fill)!r}
    window: {json.dumps(cfg.window)}
    trim_stopband: {str(bool(cfg.trim_stopband)).lower()}
  power_spectral_density:
    frequency_resolution: {freq_res!r}
    fractional_overlap: {str(cfg.psd_fractional_overlap)!r}
    window_fill: {str(cfg.psd_window_fill)!r}
    window: {json.dumps(cfg.psd_window)}
    trim_stopband: {str(bool(cfg.psd_trim_stopband)).lower()}
    time_statistic: {json.dumps(list(cfg.psd_time_statistic))}
  channel_power_time_series:
    detector_period: 0.01
    power_detectors: [rms, peak]{raw}
sink:
  path: {json.dumps(str(output))}
  store: zip
'''

    async def _run(self, request, output, duration, op_id):
        spec_path = None
        terminal = "success"
        detail = "recording completed"
        try:
            OPERATIONS.stage(op_id, "applying", "stopping live acquisition and releasing radio")
            released = await asyncio.to_thread(self.acquirer.pause_and_release, 15.0)
            if not released:
                raise RuntimeError("live acquirer did not release the radio within 15 seconds")
            OPERATIONS.stage(op_id, "released", "live radio released; allowing hardware handles to settle")
            if not self.demo:
                await asyncio.sleep(float(os.environ.get("RADIO_RECORDING_SETTLE_SEC", "2.0")))
            OPERATIONS.stage(op_id, "applied", "sweep runner starting")
            self._status["state"] = "recording"
            advanced = str(request.get("yaml") or "").strip()
            spec_text = advanced or self._default_spec(request, output)
            fd, spec_name = tempfile.mkstemp(prefix="radio-record-", suffix=".yaml")
            os.close(fd)
            spec_path = Path(spec_name)
            spec_path.write_text(spec_text, encoding="utf-8")
            if self.demo:
                await self._run_demo(output, duration, op_id, spec_text)
            else:
                await self._run_hardware(spec_path, output, duration, op_id)
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("recording finished without a non-empty archive")
            if self._stop.is_set():
                detail = "recording stopped by operator"
            elif duration:
                detail = "recording duration reached"
        except Exception as exc:
            terminal, detail = "failed", str(exc)
            self._status["error"] = detail
        finally:
            self._process = None
            if spec_path:
                with contextlib.suppress(OSError):
                    spec_path.unlink()
            self.acquirer.resume()
            OPERATIONS.stage(op_id, "resume-live", "live acquisition resume requested")
            if terminal == "success":
                self._status["state"] = "idle"
            else:
                self._status["state"] = "failed"
            self._status["finished_at"] = time.time()
            OPERATIONS.finish(op_id, terminal, detail)

    async def _run_hardware(self, spec_path, output, duration, op_id):
        # AIR-T retains FPGA descriptors for the importing process lifetime;
        # a subprocess cannot acquire it even after Device.close(). Supervise
        # the blocking wrapper in a worker thread inside this process instead.
        from sweep_runner import run_sweep

        def progress(kind, **event):
            if kind == "progress":
                self._status.update(captures=event["captures"],
                                    elapsed_s=event["elapsed_s"])
                OPERATIONS.stage(
                    op_id, "progress",
                    f'{event["captures"]} captures · {event["elapsed_s"]:.1f} s')

        result = await asyncio.to_thread(
            run_sweep, str(spec_path), str(output), duration,
            self._thread_stop.is_set, progress, self.acquirer.source)
        self._status.update(result)

    async def _run_demo(self, output, duration, op_id, spec_text):
        started = time.monotonic()
        while not self._stop.is_set() and (duration is None or time.monotonic() - started < duration):
            await asyncio.sleep(min(0.25, duration or 0.25))
            self._status["captures"] += 1
            self._status["elapsed_s"] = round(time.monotonic() - started, 3)
            OPERATIONS.stage(op_id, "progress", f'{self._status["captures"]} captures · {self._status["elapsed_s"]:.1f} s')
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("demo-recording.json", json.dumps({"demo": True, "spec": spec_text}))
