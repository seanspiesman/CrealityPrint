from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import Response

from .config import StrictModel, initialize
from .mcp_server import build_mcp
from .models import JobRequest, PrepareRequest, ServiceError
from .operator import OperatorAction, dispatch, is_loopback_client, state, typed_error
from .service import Engine


class OwnerQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class Selection(StrictModel):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{24}$")


class Guard:
    def __init__(self, app, token: str, owner_token: str, origins: list[str]):
        self.app, self.token, self.owner_token, self.origins = app, token, owner_token, origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        if scope["path"] != "/health":
            auth = headers.get(b"authorization", b"")
            supplied = auth[7:] if auth.startswith(b"Bearer ") else b""
            if not (supplied and (hmac.compare_digest(supplied, self.token.encode()) or
                                 hmac.compare_digest(supplied, self.owner_token.encode()))):
                return await JSONResponse({"detail": "Valid bearer authentication required"}, 401)(scope, receive, send)
            scope["creality_role"] = "owner" if hmac.compare_digest(supplied, self.owner_token.encode()) else "agent"
            if scope["path"].startswith("/v1/operator/"):
                client = scope.get("client")
                if scope["creality_role"] != "owner" or not client or not is_loopback_client(client[0]):
                    return await JSONResponse({"detail": "Local owner access required"}, 403)(scope, receive, send)
            origin = headers.get(b"origin")
            if origin and origin.decode("latin-1") not in self.origins:
                return await JSONResponse({"detail": "Origin is not authorized"}, 403)(scope, receive, send)
        try:
            if int(headers.get(b"content-length", b"0")) > 1024 * 1024:
                return await Response(status_code=413)(scope, receive, send)
        except ValueError:
            return await Response(status_code=400)(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > 1024 * 1024:
                    raise ServiceError("Request exceeds size limit", 413)
            return message
        await self.app(scope, limited_receive, send)


def create_app(home: Path, run_worker: bool = True) -> FastAPI:
    settings = initialize(home)
    token, owner = (home / "agent.token").read_text().strip(), (home / "owner.token").read_text().strip()
    mcp = build_mcp(settings.public_base_url, token)
    configured_host = urlsplit(settings.public_base_url).netloc
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[configured_host, "127.0.0.1:*", "localhost:*"],
                                                      allowed_origins=settings.allowed_origins))
    engine = Engine(home, settings)
    request_locks = {}

    @asynccontextmanager
    async def lifespan(app):
        lock_file = (home / "service.lock").open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("Another service owns this runtime directory") from None
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                if run_worker:
                    engine.loop_task = asyncio.create_task(engine.loop())
                yield
        finally:
            await engine.close()
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()

    app = FastAPI(title="Creality Agent API", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    import httpx
    engine.agent_tools = build_mcp(settings.public_base_url, token, transport=httpx.ASGITransport(app=app))
    app.add_middleware(Guard, token=token, owner_token=owner, origins=settings.allowed_origins)

    @app.exception_handler(ServiceError)
    async def domain_error(request, error):
        return JSONResponse({"detail": str(error)}, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": "Invalid typed request", "fields": [list(e["loc"]) for e in error.errors()]}, 422)

    @app.get("/v1/operator/state")
    async def operator_state():
        return state(engine)

    @app.post("/v1/operator/actions")
    async def operator_action(body: OperatorAction):
        key = "operator:" + body.idempotency_key
        fingerprint = hashlib.sha256(body.model_dump_json(exclude={"idempotency_key"}).encode()).hexdigest()
        lock = request_locks.setdefault(key, asyncio.Lock())
        async with lock:
            prior = engine.store.remembered(key, fingerprint)
            if prior is not None:
                return prior
            try:
                result = await dispatch(engine, body.action, body.payload)
            except (ValueError, TypeError, KeyError) as error:
                raise typed_error(error) from None
            engine.store.remember(key, fingerprint, result)
            return result

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/v1/capabilities")
    async def capabilities():
        return engine.capabilities()

    @app.get("/v1/printers")
    async def printers():
        return engine.printers()

    @app.get("/v1/printers/{id}/status")
    async def printer_status(id: str):
        return await engine.status(id)

    @app.get("/v1/profiles")
    async def profiles():
        return [{"id": p.id, "printer_id": p.printer_id, "material": p.material, "color": p.color,
                 "nozzle_mm": p.nozzle_mm, "verified": p.verified} for p in settings.profiles]

    @app.get("/v1/jobs")
    async def jobs():
        return [engine.public_job(j) for j in engine.store.list()]

    @app.get("/v1/jobs/{id}")
    async def job(id: str):
        return engine.public_job(engine.store.get(id))

    @app.get("/v1/events")
    async def events(after: int = 0, job_id: str | None = None):
        # Durable events contain only explicit service metadata, never raw camera/detector payloads.
        return engine.store.events(max(0, after), job_id)

    async def mutate(request: Request, body: dict, operation):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128:
            raise ServiceError("Provide an Idempotency-Key of 1–128 characters", 422)
        fingerprint = hashlib.sha256(json.dumps({"path": request.url.path, "body": body},
                                                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        lock = request_locks.setdefault(key, asyncio.Lock())
        async with lock:
            prior = engine.store.remembered(key, fingerprint)
            if prior is not None:
                return prior
            # Record create + replay response in one transaction; spawning is deferred until coroutine yields.
            if request.url.path == "/v1/jobs":
                with engine.store.transaction():
                    result = engine.public_job(await operation())
                    engine.store.remember(key, fingerprint, result)
            else:
                result = engine.public_job(await operation())
                engine.store.remember(key, fingerprint, result)
            return result

    @app.get("/v1/questions")
    async def questions():
        return engine.store.questions()

    @app.post("/v1/questions", status_code=201)
    async def request_question(request: Request, body: OwnerQuestion):
        if not body.question.strip():
            raise ServiceError("A question is required", 422)
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128:
            raise ServiceError("Provide an Idempotency-Key of 1–128 characters", 422)
        fingerprint = hashlib.sha256(json.dumps({"path": request.url.path, "body": body.model_dump()},
                                                sort_keys=True).encode()).hexdigest()
        async with request_locks.setdefault(key, asyncio.Lock()):
            prior = engine.store.remembered(key, fingerprint)
            if prior is not None:
                return prior
            with engine.store.transaction():
                result = engine.store.request_question(body.question, body.job_id)
                engine.store.remember(key, fingerprint, result)
            return result

    @app.post("/v1/jobs", status_code=201)
    async def create_job(request: Request, body: JobRequest):
        return await mutate(request, body.model_dump(), lambda: engine.create(body))

    @app.post("/v1/jobs/{id}/prepare")
    async def prepare_job(id: str, request: Request, body: PrepareRequest):
        return await mutate(request, body.model_dump(), lambda: engine.prepare(id, body.profile_id))

    @app.post("/v1/jobs/{id}/select")
    async def select_model(id: str, request: Request, body: Selection):
        return await mutate(request, body.model_dump(), lambda: engine.select(id, body.artifact_id))

    @app.post("/v1/jobs/{id}/queue")
    async def queue_job(id: str, request: Request):
        return await mutate(request, {}, lambda: engine.queue(id))

    @app.post("/v1/jobs/{id}/start")
    async def start_job(id: str, request: Request):
        return await mutate(request, {}, lambda: engine.start(id))

    @app.post("/v1/jobs/{id}/pause")
    async def pause_job(id: str, request: Request):
        return await mutate(request, {}, lambda: engine.control(id, "pause"))

    @app.post("/v1/jobs/{id}/cancel")
    async def cancel_job(id: str, request: Request):
        return await mutate(request, {}, lambda: engine.control(id, "cancel"))

    @app.post("/v1/jobs/{id}/resume")
    async def resume_job(id: str, request: Request):
        return await mutate(request, {}, lambda: engine.control(id, "resume"))

    app.mount("/mcp", mcp_app)
    return app
