from __future__ import annotations

import asyncio
import json

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from creality_agent.mcp_server import build_mcp


def run(coro):
    return asyncio.run(coro)


def test_read_tools_forward_auth_and_results():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"printers": [{"id": "p1"}]})

    server = build_mcp("http://agent.local", "secret-token", httpx.MockTransport(handler))
    tools = {tool.name: tool for tool in run(server.list_tools())}
    assert "capabilities" in tools
    assert "get_printer_status" in tools
    assert "select_model" in tools
    assert not ({"approve", "configure", "image_frame", "raw_gcode"} & tools.keys())

    result = run(server.call_tool("list_printers", {}))
    assert result.structured_content == {"printers": [{"id": "p1"}]}
    assert seen[0].url.path == "/v1/printers"
    assert seen[0].headers["authorization"] == "Bearer secret-token"


def test_mutations_forward_exact_paths_payloads_and_idempotency():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content),
                     request.headers.get("Idempotency-Key")))
        return httpx.Response(200, json={"ok": True})

    server = build_mcp("http://agent.local", "t", httpx.MockTransport(handler))

    async def exercise():
        await server.call_tool("create_job", {
            "request": "print a bracket", "source_url": "https://models.local/bracket.stl",
            "printer_id": "p_1", "settings": {"infill": 20}, "copies": 2,
            "idempotency_key": "create-1",
        })
        await server.call_tool("prepare_job", {
            "job_id": "j1", "profile_id": "pla", "idempotency_key": "prepare-1",
        })
        await server.call_tool("select_model", {
            "job_id": "j1", "artifact_id": "a1b2", "idempotency_key": "select-1",
        })
        await server.call_tool("queue_job", {"job_id": "j1", "idempotency_key": "queue-1"})

    run(exercise())
    assert seen == [
        ("POST", "/v1/jobs", {
            "request": "print a bracket", "source_url": "https://models.local/bracket.stl",
            "printer_id": "p_1", "settings": {"infill": 20}, "copies": 2,
        }, "create-1"),
        ("POST", "/v1/jobs/j1/prepare", {"profile_id": "pla"}, "prepare-1"),
        ("POST", "/v1/jobs/j1/select", {"artifact_id": "a1b2"}, "select-1"),
        ("POST", "/v1/jobs/j1/queue", {}, "queue-1"),
    ]


def test_events_query_and_path_validation():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"events": []})

    server = build_mcp("http://agent.local", "t", httpx.MockTransport(handler))
    run(server.call_tool("events", {"after": 7, "job_id": "job-2"}))
    assert seen[0].url.path == "/v1/events"
    assert dict(seen[0].url.params) == {"after": "7", "job_id": "job-2"}

    with pytest.raises(Exception, match="Error executing tool get_job"):
        run(server.call_tool("get_job", {"job_id": "../secret"}))
    assert len(seen) == 1


def test_api_errors_are_sanitized():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="secret-token camera-frame")

    server = build_mcp("http://agent.local", "secret-token", httpx.MockTransport(handler))
    result = run(server.call_tool("capabilities", {}))
    assert result.structured_content == {"error": "API request failed (403)"}
    assert "secret-token" not in str(result)
    assert "camera-frame" not in str(result)


def test_success_response_redacts_tokens_and_omits_image_fields():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "message": "Bearer secret-token rejected",
            "image_frame": "base64pixels",
            "job": {"id": "j1"},
        })

    server = build_mcp("http://agent.local", "secret-token", httpx.MockTransport(handler))
    result = run(server.call_tool("capabilities", {}))
    assert result.structured_content == {
        "message": "Bearer [redacted] rejected", "job": {"id": "j1"},
    }


def test_array_results_and_actual_mcp_client_session():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/printers"
        return httpx.Response(200, json=[{"id": "p1"}, {"id": "p2"}])

    server = build_mcp("http://agent.local", "t", httpx.MockTransport(handler))

    async def exercise():
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert "list_printers" in {tool.name for tool in tools.tools}
                result = await client.call_tool("list_printers", {})
                assert result.structured_content == {
                    "items": [{"id": "p1"}, {"id": "p2"}],
                }
            tasks.cancel_scope.cancel()

    run(exercise())


def test_domain_error_detail_is_bounded_and_sanitized():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "detail": "Job is held because owner approval is pending: secret-token",
            "image": "hidden",
        })

    server = build_mcp("http://agent.local", "secret-token", httpx.MockTransport(handler))
    result = run(server.call_tool("start_job", {
        "job_id": "j1", "idempotency_key": "start-1",
    }))
    assert result.structured_content == {
        "error": "API request failed (409)",
        "detail": "Job is held because owner approval is pending: [redacted]",
    }


def test_job_validation_details_omit_pydantic_context():
    server = build_mcp("http://agent.local", "t", httpx.MockTransport(
        lambda _: httpx.Response(200, json={"ok": True}),
    ))
    result = run(server.call_tool("create_job", {
        "request": "", "idempotency_key": "create-1", "source_url": "https://example.test/a.stl",
    }))
    details = result.structured_content["details"]
    assert details and all(set(item) == {"field", "message"} for item in details)
    assert all("ctx" not in item and "input" not in item for item in details)


def test_configuration_rejects_empty_token_and_unsafe_base_url():
    with pytest.raises(ValueError, match="token"):
        build_mcp("http://agent.local", " ")
    with pytest.raises(ValueError, match="credentials"):
        build_mcp("http://user:pass@agent.local", "t")
