
import pytest
from fastapi.testclient import TestClient

from creality_agent.api import create_app
from creality_agent.config import initialize
from creality_agent.models import ServiceError
from creality_agent.service import Engine
from creality_agent.store import Store


def model_file(tmp_path):
    model = tmp_path / "triangle.stl"
    model.write_text("solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\nendloop\nendfacet\nendsolid test\n")
    return model


def configure(home, root):
    settings = initialize(home)
    settings.import_roots = [str(root)]
    (home / "config.json").write_text(settings.model_dump_json())
    return settings


def test_api_auth_idempotency_and_real_local_import(tmp_path):
    source = model_file(tmp_path)
    home = tmp_path / "runtime"
    configure(home, tmp_path)
    app = create_app(home)
    token = (home / "agent.token").read_text().strip()
    with TestClient(app) as client:
        assert client.get("/v1/jobs").status_code == 401
        assert client.get("/v1/jobs", headers={"Authorization": b"Bearer \xff"}).status_code == 401
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "same-request"}
        body = {"request": "Print this triangle", "local_path": str(source)}
        first = client.post("/v1/jobs", json=body, headers=headers)
        assert first.status_code == 201, first.text
        second = client.post("/v1/jobs", json=body, headers=headers)
        assert second.json()["id"] == first.json()["id"]
        altered = client.post("/v1/jobs", json={**body, "request": "Different request"}, headers=headers)
        assert altered.status_code == 409
        import time
        for _ in range(30):
            job = client.get("/v1/jobs/" + first.json()["id"], headers=headers).json()
            if job["state"] != "acquiring":
                break
            time.sleep(0.05)
        assert job["state"] == "acquired", job
        assert job["models"][0]["metadata"]["dimensions_mm"] == [10.0, 10.0, 0.0]
        assert "path" not in job["models"][0]
        assert "local_path" not in job["request"]
        assert len(client.get("/v1/jobs", headers=headers).json()) == 1
        assert client.get("/v1/jobs", headers={**headers, "Origin": "https://untrusted.invalid"}).status_code == 403
    store = Store(home)
    assert len(store.list()) == 1
    store.close()


def test_typed_validation_does_not_echo_secret_input(tmp_path):
    app = create_app(tmp_path / "runtime", run_worker=False)
    token = (tmp_path / "runtime/agent.token").read_text().strip()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "a"}
        response = client.post("/v1/jobs", json={"request": "x", "source_url": "https://SECRET.invalid/a.stl",
                                               "local_path": "/SECRET/a.stl"}, headers=headers)
        assert response.status_code == 422
        assert "SECRET" not in response.text
        assert client.post("/v1/jobs", headers=headers, content=b"x" * (1024 * 1024 + 1)).status_code == 413


async def test_unqualified_fleet_never_sends_start(tmp_path, monkeypatch):
    settings = initialize(tmp_path)
    engine = Engine(tmp_path, settings)
    job = engine.store.create({"request": {"printer_id": "k1-max-1"}, "printer_id": "k1-max-1",
                               "gcode_path": "not-accessed", "holds": []})
    engine.store.update(job["id"], "queued")
    result = await engine.start(job["id"])
    assert result["state"] == "held"
    assert not result["ambiguous_start"]
    assert "qualified" in " ".join(result["holds"])
    await engine.close()


async def test_ambiguous_start_recovery_is_not_retried(tmp_path):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1", "remote_filename": "job.gcode"})
    engine.store.update(job["id"], "starting")
    await engine.recover()
    recovered = engine.store.get(job["id"])
    assert recovered["state"] == "held" and recovered["ambiguous_start"]
    with pytest.raises(ServiceError):
        await engine.start(job["id"])
    await engine.close()


def test_nested_transaction_rolls_back_job_and_request(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.transaction():
        job = store.create({"request": {}})
        store.remember("key", "fingerprint", {"id": job["id"]})
        raise RuntimeError("Crash before reply")
    assert store.list() == []
    assert store.remembered("key", "fingerprint") is None
    store.close()


async def test_resume_requires_owner_decision(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1", "remote_filename": "job.gcode"})
    engine.store.update(job["id"], "paused")
    with pytest.raises(ServiceError, match="owner decision"):
        await engine.control(job["id"], "resume")
    await engine.close()
