from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from creality_agent import vision
from creality_agent.api import create_app
from creality_agent.config import Printer, initialize


def loopback_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(vision.httpx, "AsyncClient", factory)


@pytest.mark.parametrize("url", [
    "https://localhost/detect",
    "http://example.com/detect",
    "http://user:pass@localhost/detect",
    "http://localhost:bad/detect",
    "http://localhost:0/detect",
    "http://localhost:/detect",
    "http://localhost/detect#fragment",
    "ftp://127.0.0.1/detect",
])
@pytest.mark.asyncio
async def test_non_loopback_or_malformed_detector_url_never_creates_client(monkeypatch, url):
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("invalid detector endpoints must be rejected before any request")

    monkeypatch.setattr(vision.httpx, "AsyncClient", forbidden_client)
    printer = Printer(
        id="p1", name="Printer", model="test", failure_detector_qualified=True,
        failure_detector_url=url,
    )
    assert await vision.failure_assessment(printer, b"synthetic-frame") == {
        "verdict": "unknown",
        "reason": "Failure analysis must use a loopback local detector",
    }


@pytest.mark.asyncio
async def test_localhost_failure_detector_uses_literal_loopback_and_keeps_only_verdict(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={
            "verdict": "failed", "explanation": "private detector reasoning", "image": "pixel payload",
        })

    loopback_client(monkeypatch, handler)
    printer = Printer(
        id="p1", name="Printer", model="test", failure_detector_qualified=True,
        failure_detector_url="http://localhost:8123/detect",
    )
    result = await vision.failure_assessment(printer, b"synthetic-frame")
    assert result == {"verdict": "failed"}
    assert seen[0].url.host == "127.0.0.1"
    assert seen[0].url.port == 8123
    assert seen[0].headers["host"] == "localhost:8123"


@pytest.mark.asyncio
async def test_localhost_bed_detector_uses_literal_loopback_and_bounded_result(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.jpg"
    Image.new("RGB", (4, 4), color=(20, 30, 40)).save(baseline)
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={
            "verdict": "clear", "explanation": "private detector reasoning", "image": "pixel payload",
        })

    loopback_client(monkeypatch, handler)
    printer = Printer(
        id="p1", name="Printer", model="test", vision_qualified=True,
        bed_detector_url="http://localhost:8765/clearance", baseline_file="baseline.jpg",
        roi=(0, 0, 4, 4),
    )
    result = await vision.qualified_bed_assessment(printer, b"synthetic-frame", 100.0, tmp_path)
    assert result == {"verdict": "clear", "captured_at": 100.0}
    assert seen[0].url.host == "127.0.0.1"
    assert seen[0].url.port == 8765
    assert seen[0].headers["host"] == "localhost:8765"
    assert b"synthetic-frame" in seen[0].content
    assert b"private detector reasoning" not in json.dumps(result).encode()


@pytest.mark.asyncio
async def test_detector_failure_returns_unknown(monkeypatch):
    loopback_client(monkeypatch, lambda _request: httpx.Response(503, text="private backend response"))
    printer = Printer(
        id="p1", name="Printer", model="test", failure_detector_qualified=True,
        failure_detector_url="http://127.0.0.1:9001/detect",
    )
    assert await vision.failure_assessment(printer, b"synthetic-frame") == {
        "verdict": "unknown", "reason": "Local detector unavailable",
    }


def test_api_payload_excludes_image_and_freeform_detector_data(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    settings = initialize(home)
    printer = settings.printers[0]
    printer.failure_detector_qualified = True
    printer.failure_detector_url = "http://localhost:9001/detect"
    (home / "config.json").write_text(settings.model_dump_json())

    def handler(_request: httpx.Request):
        return httpx.Response(200, json={
            "verdict": "failed", "explanation": "PRIVATE_FREEFORM_MARKER", "image": "PRIVATE_PIXEL_MARKER",
        })

    loopback_client(monkeypatch, handler)
    app = create_app(home, run_worker=False)
    engine = app.state.engine
    active_printer = engine.settings.printers[0]
    job = engine.store.create({"request": {}, "printer_id": active_printer.id,
                               "remote_filename": "job.gcode", "holds": []})
    engine.store.update(job["id"], "printing")

    async def status(_printer_id):
        return {"state": "printing", "filename": "job.gcode"}

    async def capture(_printer):
        return b"synthetic-frame", 100.0

    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine.camera, "capture", capture)
    asyncio.run(engine.monitor(engine.store.get(job["id"])))

    token = (home / "agent.token").read_text().strip()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        event_payload = client.get(f"/v1/events?job_id={job['id']}", headers=headers).json()
        job_payload = client.get(f"/v1/jobs/{job['id']}", headers=headers).json()
    encoded = json.dumps({"events": event_payload, "job": job_payload})
    assert "PRIVATE_FREEFORM_MARKER" not in encoded
    assert "PRIVATE_PIXEL_MARKER" not in encoded
    assert all(event["data"] == {"action": "pause"} for event in event_payload if event["kind"] == "failure_detected")
    assert job_payload["state"] == "printing"
    asyncio.run(engine.close())
