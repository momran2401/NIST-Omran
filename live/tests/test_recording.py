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
