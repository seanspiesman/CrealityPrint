from __future__ import annotations

import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from creality_agent import local_chat, service
from creality_agent.api import create_app
from creality_agent.config import LocalModel, initialize, load_settings
from creality_agent.models import ServiceError
from creality_agent.operator import budget_fingerprint, dispatch, local_origin
from creality_agent.project import verify_project
from creality_agent.service import Engine
from creality_agent.store import Store


def headers(home, role="owner"):
    return {"Authorization": "Bearer " + (home / (role + ".token")).read_text().strip()}


def post(client, home, action, payload, key="one"):
    return client.post("/v1/operator/actions", headers=headers(home),
                       json={"action": action, "payload": payload, "idempotency_key": key})


def test_operator_boundary_and_no_secret_state(tmp_path):
    app = create_app(tmp_path, run_worker=False)
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        assert client.get("/v1/operator/state", headers=headers(tmp_path, "agent")).status_code == 403
        assert post(client, tmp_path, "enroll_printer", {"printer_id": "k1-max-1", "auto_start": True}).status_code == 422
        result = post(client, tmp_path, "save_model", {"base_url": "http://127.0.0.1:8000/v1", "model": "local-qwen", "api_key": "PRIVATE-MODEL-KEY"})
        assert result.status_code == 200, result.text
        assert "PRIVATE-MODEL-KEY" not in result.text
        assert result.json()["model"]["api_key_set"]
        for role in ("agent", "owner"):
            assert (tmp_path / (role + ".token")).read_text().strip() not in result.text
        assert result.json()["policy"]["max_hours"] == 8
        assert result.json()["policy"]["max_grams"] == 250
        assert not result.json()["policy"]["pause_on_monitoring_loss"]
    assert load_settings(tmp_path).local_model.api_key == "PRIVATE-MODEL-KEY"
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600
    remote = create_app(tmp_path, run_worker=False)
    with TestClient(remote, client=("192.168.4.42", 1234)) as client:
        assert client.get("/v1/operator/state", headers=headers(tmp_path)).status_code == 403


async def test_budget_decision_bound_to_artifact_and_estimates(tmp_path):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "gcode_sha256": "old", "estimates": {"hours": 10, "grams": 300}})
    engine.store.update(job["id"], "prepared")
    await dispatch(engine, "approve_budget", {"job_id": job["id"]})
    assert engine.store.budget_allowed(job["id"], budget_fingerprint(engine.store.get(job["id"])))
    changed = engine.store.update(job["id"], gcode_sha256="new")
    assert not engine.store.budget_allowed(job["id"], budget_fingerprint(changed))
    changed = engine.store.update(job["id"], gcode_sha256="old", estimates={"hours": 11, "grams": 300})
    assert not engine.store.budget_allowed(job["id"], budget_fingerprint(changed))
    await engine.close()


async def test_monitor_loss_continues_print_deduplicates_and_recovers(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1", "remote_filename": "owned.gcode"})
    engine.store.update(job["id"], "printing")
    engine.last_monitor[job["id"]] = time.monotonic() - 61
    async def status(_id):
        return {"state": "printing", "filename": "owned.gcode"}
    async def capture(_p):
        return b"local-only-pixels", time.time()
    verdict = "unknown"
    async def assessment(_p, _frame):
        return {"verdict": verdict}
    async def never_control(*args):
        pytest.fail("Monitoring loss must continue the current print")
    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine.camera, "capture", capture)
    monkeypatch.setattr(service, "failure_assessment", assessment)
    monkeypatch.setattr(engine, "control", never_control)
    await engine.monitor(engine.store.get(job["id"]))
    await engine.monitor(engine.store.get(job["id"]))
    current = engine.store.get(job["id"])
    assert current["state"] == "printing" and current["monitoring_lost"]
    assert sum(e["kind"] == "monitoring_unavailable" for e in engine.store.events()) == 1
    with pytest.raises(ServiceError, match="Monitoring is unavailable"):
        await engine.eligibility({"id": "new", "request": {}, "printer_id": "k1-max-1"})
    verdict = "ok"
    await engine.monitor(current)
    assert not engine.store.get(job["id"])["monitoring_lost"]
    assert any(e["kind"] == "monitoring_recovered" for e in engine.store.events())
    engine.store.collect_alerts()
    assert {a["kind"] for a in engine.store.alerts()} == {"monitoring_recovered", "monitoring_unavailable"}
    assert "local-only-pixels" not in json.dumps(engine.store.events())
    await engine.close()
    reopened = Store(tmp_path)
    reopened.collect_alerts()
    assert len(reopened.alerts()) == 2
    reopened.acknowledge(reopened.alerts()[0]["id"])
    assert reopened.alerts()[0]["acknowledged"]
    reopened.close()


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://user:pass@127.0.0.1/v1", "http://127.0.0.1/v1?key=secret"])
def test_cloud_model_endpoints_rejected(url):
    with pytest.raises(ServiceError):
        local_origin(url)


async def test_local_model_uses_guarded_tools_not_operator_decisions(tmp_path, monkeypatch):
    app = create_app(tmp_path, run_worker=False)
    engine = app.state.engine
    engine.settings.local_model = LocalModel(model="qwen-local")
    calls = 0
    async def fake_completion(model, messages, tools):
        nonlocal calls
        calls += 1
        names = {t["function"]["name"] for t in tools}
        assert "approve_resume" not in names and "approve_budget" not in names
        assert "owner.token" not in json.dumps(messages)
        if calls == 1:
            return {"role": "assistant", "content": "Checking printers", "tool_calls": [
                {"id": "call", "type": "function", "function": {"name": "list_printers", "arguments": "{}"}}]}
        assert messages[-1]["role"] == "tool"
        assert len(json.loads(messages[-1]["content"])["items"]) == 6
        return {"role": "assistant", "content": "All six printers require qualification."}
    monkeypatch.setattr(local_chat, "completion", fake_completion)
    answer = await dispatch(engine, "chat", {"message": "What printers are available?"})
    await engine.chat_tasks[answer["conversation_id"]]
    messages = engine.store.conversation(answer["conversation_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[-1]["content"].startswith("All six")
    await engine.close()
    reopened = Store(tmp_path)
    assert reopened.conversation(answer["conversation_id"])["messages"] == messages
    reopened.close()


async def test_provider_response_bounded_and_sanitized(tmp_path):
    def fixture(request):
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == "Bearer local-key"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Ready"}}]})
    result = await local_chat.completion(LocalModel(model="qwen", api_key="local-key"), [], [], httpx.MockTransport(fixture))
    assert result == {"role": "assistant", "content": "Ready"}


def project_fixture(tmp_path, changed_geometry=False, changed_settings=False):
    source = tmp_path / "source.stl"
    source.write_text("solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid t")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"type": "process", "name": "fixture", "layer_height": "0.2"}))
    project = tmp_path / "project.3mf"
    with zipfile.ZipFile(project, "w") as archive:
        archive.writestr("3D/3dmodel.model", '<model unit="millimeter"><resources><object id="1"><mesh><vertices>'
                        '<vertex x="0" y="0" z="0"/><vertex x="' + ("2" if changed_geometry else "1") +
                        '" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/>'
                        '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
        archive.writestr("Metadata/project_settings.config", json.dumps({"layer_height": "0.3" if changed_settings else "0.2"}))
    return project, source, [profile]


def test_native_project_geometry_settings_roundtrip_fixture(tmp_path):
    result = verify_project(*project_fixture(tmp_path), initialize(tmp_path / "runtime").policy)
    assert result["geometry_verified"] and result["settings_verified"]


@pytest.mark.parametrize("geometry,settings", [(True, False), (False, True)])
def test_native_project_changed_geometry_or_settings_held(tmp_path, geometry, settings):
    with pytest.raises(ServiceError):
        verify_project(*project_fixture(tmp_path, geometry, settings), initialize(tmp_path / "runtime").policy)


def test_reconcile_interrupted_tool_group_without_replay():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "pending", "type": "function", "function": {"name": "start_job", "arguments": "{}"}}]},
        {"role": "user", "content": "Continue"}]
    history = local_chat.reconciled_history(messages)
    assert [m["role"] for m in history] == ["assistant", "tool", "user"]
    assert history[1]["tool_call_id"] == "pending"
    assert "inspect job state" in history[1]["content"]


def test_supplied_tailnet_model_allowed_but_printers_remain_lan_only():
    from creality_agent.network import pin_lan
    assert local_origin("http://100.107.19.8:8888/v1")[0] == "http://100.107.19.8:8888/v1"
    with pytest.raises(ServiceError):
        pin_lan("http://100.107.19.8:8888")


def test_durable_questions_agent_request_owner_answer(tmp_path):
    app = create_app(tmp_path, run_worker=False)
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        agent = {**headers(tmp_path, "agent"), "Idempotency-Key": "question-one"}
        body = {"question": "What hole diameter is required?"}
        first = client.post("/v1/questions", headers=agent, json=body)
        assert first.status_code == 201
        question = first.json()
        assert client.post("/v1/questions", headers=agent, json=body).json() == question
        assert len(client.get("/v1/questions", headers=agent).json()) == 1
        denied = client.post("/v1/operator/actions", headers=agent, json={"action": "answer_question",
            "payload": {"question_id": question["id"], "answer": "10mm"}, "idempotency_key": "answer"})
        assert denied.status_code == 403
        answer = post(client, tmp_path, "answer_question", {"question_id": question["id"], "answer": "10mm"})
        assert answer.status_code == 200 and answer.json()["status"] == "answered"
        assert post(client, tmp_path, "answer_question", {"question_id": question["id"], "answer": "10mm"}).json() == answer.json()
        state = client.get("/v1/operator/state", headers=headers(tmp_path)).json()
        assert state["questions"][0]["answer"] == "10mm"
        assert any(a["kind"] == "question_requested" for a in state["alerts"])
        assert not app.state.engine.store.db.execute("SELECT 1 FROM decisions").fetchone()
    reopened = Store(tmp_path)
    assert reopened.question(question["id"])["answer"] == "10mm"
    reopened.close()


async def test_question_tools_have_no_owner_answer_tool(tmp_path):
    app = create_app(tmp_path, run_worker=False)
    names = {t.name for t in await app.state.engine.agent_tools.list_tools()}
    assert {"request_owner_input", "list_questions"} <= names
    assert "answer_question" not in names
    await app.state.engine.close()


async def test_variant_selection_preserves_ambiguous_physical_ownership(tmp_path):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "models": [{"artifact_id": "variant"}],
        "remote_filename": "owned.gcode", "ambiguous_start": True})
    engine.store.update(job["id"], "held")
    with pytest.raises(ServiceError, match="physical job ownership"):
        await engine.select(job["id"], "variant")
    assert engine.store.get(job["id"])["state"] == "held"
    assert engine.store.worklist()[0]["id"] == job["id"]
    await engine.close()


async def test_unanswered_job_question_holds_new_start(tmp_path):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1"})
    question = engine.store.request_question("Which slot?", job["id"])
    with pytest.raises(ServiceError, match="unanswered owner question"):
        await engine.eligibility(job)
    engine.store.answer_question(question["id"], "Slot 1")
    assert not engine.store.has_open_question(job["id"])
    assert not engine.printer("k1-max-1").auto_start
    await engine.close()


async def test_notification_delay_does_not_stop_monitoring_ticks(tmp_path, monkeypatch):
    import asyncio

    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1"})
    engine.store.update(job["id"], "printing")
    engine.store.event(job["id"], "monitoring_unavailable", {})
    entered, twice = asyncio.Event(), asyncio.Event()
    count = 0
    sleep = asyncio.sleep

    async def blocked_notification(*args):
        entered.set()
        await asyncio.Event().wait()

    async def tick(_job):
        nonlocal count
        count += 1
        if count >= 2:
            twice.set()

    async def no_operation(*args):
        pass

    async def quick_tick(seconds):
        await sleep(.01 if seconds == 3 else seconds)

    monkeypatch.setattr(service, "notify", blocked_notification)
    monkeypatch.setattr(engine, "tick", tick)
    monkeypatch.setattr(engine, "recover", no_operation)
    monkeypatch.setattr(engine, "update_awake", no_operation)
    monkeypatch.setattr(service.asyncio, "sleep", quick_tick)
    engine.loop_task = asyncio.create_task(engine.loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(twice.wait(), 1)
        assert not engine.alert_task.done()
    finally:
        await engine.close()


async def test_paused_job_observes_manual_printer_resume_without_sending_control(tmp_path, monkeypatch):
    engine = Engine(tmp_path, initialize(tmp_path))
    job = engine.store.create({"request": {}, "printer_id": "k1-max-1", "remote_filename": "owned.gcode"})
    job = engine.store.update(job["id"], "paused")

    async def status(_id):
        return {"state": "printing", "filename": "owned.gcode"}

    async def no_control(*args):
        pytest.fail("A status observation must never send a resume command")

    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine, "control", no_control)
    await engine.monitor_paused(job)
    assert engine.store.get(job["id"])["state"] == "printing"
    await engine.close()
