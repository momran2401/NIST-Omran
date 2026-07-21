import asyncio
import zipfile

from core.config import SharedConfig
from core.recording import RecordingManager


class FakeAcquirer:
    def __init__(self):
        self.paused = False

    def pause_and_release(self, _timeout):
        self.paused = True
        return True

    def resume(self):
        self.paused = False


def test_demo_record_duration_closes_output_and_resumes(tmp_path):
    async def scenario():
        acquirer = FakeAcquirer()
        manager = RecordingManager(acquirer, SharedConfig(), demo=True)
        await manager.start({"duration": 0.3, "directory": str(tmp_path),
                             "include_raw_iq": True})
        await manager._task
        status = manager.status()
        assert status["state"] == "idle"
        assert status["captures"] >= 1
        assert not acquirer.paused
        with zipfile.ZipFile(status["output"]) as archive:
            assert "demo-recording.json" in archive.namelist()

    asyncio.run(scenario())


def test_demo_run_until_stop(tmp_path):
    async def scenario():
        acquirer = FakeAcquirer()
        manager = RecordingManager(acquirer, SharedConfig(), demo=True)
        await manager.start({"duration": None, "directory": str(tmp_path)})
        await asyncio.sleep(0.3)
        await manager.stop()
        await manager._task
        assert manager.status()["state"] == "idle"
        assert not acquirer.paused

    asyncio.run(scenario())


def test_rolling_view_uses_dma_safe_recording_capture_duration(tmp_path):
    async def scenario():
        shared = SharedConfig()  # rolling mode: duration == 0
        manager = RecordingManager(FakeAcquirer(), shared, demo=False)

        assert manager.defaults()["capture_duration"] == 0.02
        spec = manager._default_spec(
            {"directory": str(tmp_path)}, tmp_path / "capture.zarr.zip")
        assert "    duration: 0.02\n" in spec

    asyncio.run(scenario())


def test_record_spec_uses_form_radio_fields_and_raw_iq(tmp_path):
    async def scenario():
        manager = RecordingManager(FakeAcquirer(), SharedConfig(), demo=False)
        spec = manager._default_spec({
            "center_frequency": 2.1e9,
            "sample_rate": 7.68e6,
            "gain": -3.5,
            "capture_duration": 0.01,
            "include_raw_iq": True,
        }, tmp_path / "capture.zarr.zip")

        assert "center_frequency: 2100000000.0" in spec
        assert "sample_rate: 7680000.0" in spec
        assert "gain: -3.5" in spec
        assert "duration: 0.01" in spec
        assert "iq_waveform: {}" in spec

    asyncio.run(scenario())
