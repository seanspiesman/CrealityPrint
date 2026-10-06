from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from creality_agent import service
from creality_agent.config import initialize
from creality_agent.models import ServiceError
from creality_agent.service import Engine
from creality_agent.store import Store


class FakeAdapter:
    def __init__(self, store: Store, home: Path, job_id: str, *, upload_error=False,
                 start_error=False, statuses=None):
        self.store = store
        self.home = home
        self.job_id = job_id
        self.upload_error = upload_error
        self.start_error = start_error
        self.statuses = list(statuses or [])
        self.actions: list[str] = []

    async def upload(self, path: Path, remote_name: str):
        assert path.exists()
        job = self.store.get(self.job_id)
        assert job["state"] == "starting"
        assert job["remote_filename"] == remote_name
        if self.upload_error:
            raise RuntimeError("upload response was lost")

    async def control(self, action: str, filename: str | None = None):
        self.actions.append(action)
        if action == "start":
            assert filename == f"{self.job_id}.gcode"
            if self.start_error:
                raise RuntimeError("start response was lost")

    async def status(self):
        if self.statuses:
            return self.statuses.pop(0)
        return {"state": "standby", "filename": ""}


def queued_job(engine: Engine, tmp_path: Path) -> dict:
    gcode = tmp_path / "job.gcode"
    gcode.write_text("G1 X1 Y1\n")
    record = engine.store.create({
        "request": {"printer_id": "printer-1"},
        "printer_id": "printer-1",
        "gcode_path": str(gcode),
        "gcode_sha256": service.sha256(gcode),
        "holds": [],
    })
    return engine.store.update(record["id"], "queued")


def install_eligibility(monkeypatch, engine: Engine, adapter, calls: list[str] | None = None):
    async def eligibility(_job):
        if calls is not None:
            calls.append("eligibility")
        return None, adapter, []

    monkeypatch.setattr(engine, "eligibility", eligibility)


@pytest.mark.asyncio
async def test_start_persists_intent_checks_again_after_upload_and_observes_matching_job(
    tmp_path, monkeypatch,
):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    order: list[str] = []
    adapter = FakeAdapter(engine.store, tmp_path, job["id"], statuses=[
        {"state": "printing", "filename": "someone-elses.gcode"},
        {"state": "printing", "filename": f"{job['id']}.gcode"},
    ])
    install_eligibility(monkeypatch, engine, adapter, order)
    original_upload = adapter.upload

    async def upload(path, remote_name):
        order.append("upload")
        await original_upload(path, remote_name)

    original_control = adapter.control

    async def control(action, filename=None):
        if action == "start":
            order.append("start")
            assert order == ["eligibility", "upload", "eligibility", "start"]
        await original_control(action, filename)

    adapter.upload = upload
    adapter.control = control

    async def no_wait(_seconds):
        order.append("observe")

    monkeypatch.setattr(service.asyncio, "sleep", no_wait)
    result = await engine.start(job["id"])
    assert order[:4] == ["eligibility", "upload", "eligibility", "start"]
    assert order[4:] == ["observe"]
    assert result["state"] == "printing"
    assert result["observation"]["filename"] == f"{job['id']}.gcode"
    assert result["observation"]["state"] == "printing"
    await engine.close()


@pytest.mark.asyncio
async def test_second_eligibility_failure_holds_ambiguous_without_start(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    adapter = FakeAdapter(engine.store, tmp_path, job["id"])
    calls: list[str] = []

    async def eligibility(_job):
        calls.append("eligibility")
        if len(calls) == 2:
            assert engine.store.get(job["id"])["state"] == "starting"
            raise ServiceError("Camera clearance became stale")
        return None, adapter, []

    monkeypatch.setattr(engine, "eligibility", eligibility)
    result = await engine.start(job["id"])
    assert calls == ["eligibility", "eligibility"]
    assert result["state"] == "held" and result["ambiguous_start"]
    assert result["remote_filename"] == f"{job['id']}.gcode"
    assert adapter.actions == []
    await engine.close()


@pytest.mark.parametrize("failure", ["upload", "start"])
@pytest.mark.asyncio
async def test_upload_or_start_failure_is_ambiguous_and_future_start_does_not_retry(
    tmp_path, monkeypatch, failure,
):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    adapter = FakeAdapter(
        engine.store,
        tmp_path,
        job["id"],
        upload_error=failure == "upload",
        start_error=failure == "start",
    )
    install_eligibility(monkeypatch, engine, adapter)
    result = await engine.start(job["id"])
    assert result["state"] == "held" and result["ambiguous_start"]
    with pytest.raises(ServiceError, match="previous starts are not retried"):
        await engine.start(job["id"])
    assert adapter.actions == ([] if failure == "upload" else ["start"])
    await engine.close()


@pytest.mark.asyncio
async def test_cancel_of_ambiguous_start_preserves_intent_and_holds(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    held = engine.store.update(
        job["id"], "held", remote_filename=f"{job['id']}.gcode", ambiguous_start=True,
        holds=["Start interrupted; reconcile printer state"],
    )

    async def unexpected_eligibility(_job):
        pytest.fail("ambiguous cancellation must not proceed to adapter checks")

    monkeypatch.setattr(engine, "eligibility", unexpected_eligibility)
    with pytest.raises(ServiceError, match="unresolved"):
        await engine.control(job["id"], "cancel")
    after = engine.store.get(job["id"])
    assert after["state"] == "held"
    assert after["remote_filename"] == held["remote_filename"]
    assert after["ambiguous_start"] is True
    with pytest.raises(ServiceError, match="previous starts are not retried"):
        await engine.start(job["id"])
    await engine.close()


@pytest.mark.asyncio
async def test_monitor_failure_pauses_once_and_resume_requires_owner_decision(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    remote = f"{job['id']}.gcode"
    engine.store.update(job["id"], "printing", remote_filename=remote)
    adapter = FakeAdapter(engine.store, tmp_path, job["id"], statuses=[
        {"state": "printing", "filename": remote},  # pause ownership check
        {"state": "paused", "filename": remote},
        {"state": "paused", "filename": remote},  # resume ownership check
        {"state": "printing", "filename": remote},
    ])
    monkeypatch.setattr(service, "Moonraker", lambda _printer: adapter)
    monkeypatch.setattr(engine, "printer", lambda printer_id: SimpleNamespace(id=printer_id))

    async def status(_printer_id):
        return {"state": "printing", "filename": remote}

    async def capture(_printer):
        return b"frame", 1.0

    async def failed_assessment(_printer, _frame):
        return {"verdict": "failed"}

    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine.camera, "capture", capture)
    monkeypatch.setattr(service, "failure_assessment", failed_assessment)
    await engine.monitor(engine.store.get(job["id"]))
    assert adapter.actions == ["pause"]
    assert engine.store.get(job["id"])["state"] == "paused"
    with pytest.raises(ServiceError, match="owner decision"):
        await engine.control(job["id"], "resume")
    assert adapter.actions == ["pause"]

    engine.store.approve(job["id"], "resume")
    result = await engine.control(job["id"], "resume")
    assert adapter.actions == ["pause", "resume"]
    assert result["state"] == "printing"
    await engine.close()


@pytest.mark.asyncio
async def test_recovery_reacquires_monitor_without_starting_matching_active_job(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = queued_job(engine, tmp_path)
    remote = f"{job['id']}.gcode"
    engine.store.update(job["id"], "starting", remote_filename=remote)
    calls: list[str] = []

    async def status(_printer_id):
        calls.append("status")
        return {"state": "printing", "filename": remote}

    async def monitor(_job):
        calls.append("monitor")

    async def start(_job_id):
        calls.append("start")
        pytest.fail("recovery must never issue another start")

    async def stop_loop(_seconds):
        raise RuntimeError("stop after one scheduler pass")

    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine, "monitor", monitor)
    monkeypatch.setattr(engine, "start", start)
    monkeypatch.setattr(service.asyncio, "sleep", stop_loop)
    with pytest.raises(RuntimeError, match="one scheduler pass"):
        await engine.loop()
    assert calls == ["status", "monitor"]
    assert engine.store.get(job["id"])["state"] == "printing"
    await engine.close()
