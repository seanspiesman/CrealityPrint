from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server import MCPServer
from pydantic import ValidationError

from .models import JobRequest

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API URL must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _sanitize(value: Any, token: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item, token)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in ("image", "frame", "pixel"))
        }
    if isinstance(value, list):
        return [_sanitize(item, token) for item in value]
    if isinstance(value, str) and token:
        return value.replace(token, "[redacted]")
    return value


def _validation_details(error: ValidationError) -> list[dict[str, str]]:
    details = []
    for item in error.errors(include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ()))
        details.append({"field": location, "message": item.get("msg", "Invalid value")[:200]})
    return details


def build_mcp(base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    """Create an MCP facade over the authenticated persistent API."""
    api_url = _base_url(base_url)
    if not token or not token.strip():
        raise ValueError("API token is not configured")

    server = MCPServer(name="creality-agent")

    async def call(method: str, path: str, *, params: dict[str, Any] | None = None,
                   payload: dict[str, Any] | None = None, idempotency_key: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key is not None:
            if not idempotency_key.strip():
                raise ValueError("idempotency_key must not be empty")
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                base_url=api_url,
                trust_env=False,
                follow_redirects=False,
                timeout=360,
                transport=transport,
            ) as client:
                response = await client.request(method, path, params=params, json=payload, headers=headers)
            response.raise_for_status()
            data = _sanitize(response.json(), token)
            return {"items": data} if isinstance(data, list) else data
        except httpx.HTTPStatusError as exc:
            result: dict[str, Any] = {"error": f"API request failed ({exc.response.status_code})"}
            try:
                error_data = _sanitize(exc.response.json(), token)
            except (ValueError, json.JSONDecodeError):
                error_data = None
            if isinstance(error_data, dict):
                if isinstance(error_data.get("detail"), (str, int, float)):
                    result["detail"] = str(error_data["detail"])[:500]
                if isinstance(error_data.get("fields"), list):
                    result["fields"] = error_data["fields"][:20]
            return result
        except (httpx.HTTPError, ValueError):
            return {"error": "API request failed"}

    @server.tool(description="Read the service capabilities.")
    async def capabilities() -> dict[str, Any]:
        return await call("GET", "/v1/capabilities")

    @server.tool(description="List configured printers.")
    async def list_printers() -> dict[str, Any]:
        return await call("GET", "/v1/printers")

    @server.tool(description="Read status for one configured printer.")
    async def get_printer_status(printer_id: str) -> dict[str, Any]:
        return await call("GET", f"/v1/printers/{_identifier(printer_id, 'printer_id')}/status")

    @server.tool(description="List configured print profiles.")
    async def list_profiles() -> dict[str, Any]:
        return await call("GET", "/v1/profiles")

    @server.tool(description="Ask the owner a consequential question and persist it in the app. Answering never grants resume or budget approval. Requires an idempotency key.")
    async def request_owner_input(question: str, idempotency_key: str, job_id: str | None = None) -> dict[str, Any]:
        return await call("POST", "/v1/questions", payload={"question": question, "job_id": job_id},
                          idempotency_key=idempotency_key)

    @server.tool(description="Read owner questions and answers. Do not invent answers or continue dependent work before an answer exists.")
    async def list_questions() -> dict[str, Any]:
        return await call("GET", "/v1/questions")

    @server.tool(description="List jobs.")
    async def list_jobs() -> dict[str, Any]:
        return await call("GET", "/v1/jobs")

    @server.tool(description="Read one job.")
    async def get_job(job_id: str) -> dict[str, Any]:
        return await call("GET", f"/v1/jobs/{_identifier(job_id, 'job_id')}")

    @server.tool(description="Read job and service events, optionally filtered by job.")
    async def events(after: int = 0, job_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"after": after}
        if job_id is not None:
            params["job_id"] = _identifier(job_id, "job_id")
        return await call("GET", "/v1/events", params=params)

    @server.tool(description="Create a durable job request. Requires an idempotency key.")
    async def create_job(
        request: str,
        idempotency_key: str,
        source_url: str | None = None,
        local_path: str | None = None,
        printer_id: str | None = None,
        material: str | None = None,
        color: str | None = None,
        settings: dict[str, Any] | None = None,
        copies: int = 1,
    ) -> dict[str, Any]:
        try:
            body = JobRequest(
                request=request,
                source_url=source_url,
                local_path=local_path,
                printer_id=printer_id,
                material=material,
                color=color,
                settings=settings or {},
                copies=copies,
            )
        except ValidationError as exc:
            return {"error": "Invalid job request", "details": _validation_details(exc)}
        return await call("POST", "/v1/jobs", payload=body.model_dump(exclude_none=True),
                          idempotency_key=idempotency_key)

    @server.tool(description="Prepare a job using a configured profile. Requires an idempotency key.")
    async def prepare_job(job_id: str, profile_id: str, idempotency_key: str) -> dict[str, Any]:
        return await call("POST", f"/v1/jobs/{_identifier(job_id, 'job_id')}/prepare",
                          payload={"profile_id": _identifier(profile_id, "profile_id")},
                          idempotency_key=idempotency_key)

    @server.tool(description="Select an imported archive artifact for a job. Requires an idempotency key.")
    async def select_model(job_id: str, artifact_id: str, idempotency_key: str) -> dict[str, Any]:
        return await call("POST", f"/v1/jobs/{_identifier(job_id, 'job_id')}/select",
                          payload={"artifact_id": _identifier(artifact_id, "artifact_id")},
                          idempotency_key=idempotency_key)

    async def mutate(job_id: str, action: str, idempotency_key: str) -> dict[str, Any]:
        valid_job = _identifier(job_id, "job_id")
        if action not in {"queue", "start", "pause", "cancel", "resume"}:
            raise ValueError("Invalid action")
        return await call("POST", f"/v1/jobs/{valid_job}/{action}", payload={},
                          idempotency_key=idempotency_key)

    @server.tool(description="Queue a prepared job. Requires an idempotency key.")
    async def queue_job(job_id: str, idempotency_key: str) -> dict[str, Any]:
        return await mutate(job_id, "queue", idempotency_key)

    @server.tool(description="Start a queued job. Requires an idempotency key.")
    async def start_job(job_id: str, idempotency_key: str) -> dict[str, Any]:
        return await mutate(job_id, "start", idempotency_key)

    @server.tool(description="Pause a running job. Requires an idempotency key.")
    async def pause_job(job_id: str, idempotency_key: str) -> dict[str, Any]:
        return await mutate(job_id, "pause", idempotency_key)

    @server.tool(description="Cancel a job. Requires an idempotency key.")
    async def cancel_job(job_id: str, idempotency_key: str) -> dict[str, Any]:
        return await mutate(job_id, "cancel", idempotency_key)

    @server.tool(description="Resume a paused job. Requires an idempotency key.")
    async def resume_job(job_id: str, idempotency_key: str) -> dict[str, Any]:
        return await mutate(job_id, "resume", idempotency_key)

    return server


def main() -> None:
    from .cli import default_home

    api_url = os.environ.get("CREALITY_AGENT_API_URL", "http://127.0.0.1:18088")
    home = default_home()
    try:
        token = (home / "agent.token").read_text(encoding="utf-8").strip()
        server = build_mcp(api_url, token)
    except (OSError, ValueError) as exc:
        print(f"creality-agent-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    server.run(transport="stdio")
