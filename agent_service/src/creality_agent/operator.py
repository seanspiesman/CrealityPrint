"""Owner-only desktop contract. This module is never exposed as an MCP tool."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import uuid
from pathlib import Path

from pydantic import Field, ValidationError

from .config import CFSSlot, LocalModel, Policy, Printer, Profile, StrictModel, save_settings
from .ingestion import sha256
from .models import JobRequest, ServiceError
from .network import pin_lan
from .printers import Moonraker
from .vision import _pin_loopback


class OperatorAction(StrictModel):
    action: str = Field(min_length=1, max_length=60)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)


def budget_fingerprint(job: dict) -> str:
    return hashlib.sha256(json.dumps({"gcode": job.get("gcode_sha256"),
        "project": job.get("project_sha256"), "estimates": job.get("estimates")}, sort_keys=True).encode()).hexdigest()


def local_origin(url: str) -> tuple[str, str, str]:
    """Pin local model hosts without allowing a cloud redirect or DNS rebinding."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ServiceError("Model endpoint must be a local HTTP(S) base URL without credentials")
    try:
        if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
            pinned = _pin_loopback(url)
            if not pinned:
                raise ServiceError("Loopback model endpoints must use HTTP")
            return pinned[0], pinned[1], parts.hostname
        return pin_lan(url, allow_tailnet=True)
    except ValueError:
        raise ServiceError("Invalid local model endpoint") from None


def state(engine):
    engine.store.collect_alerts()
    return {"jobs": [engine.public_job(j) for j in engine.store.list()],
            "printers": [p.model_dump(exclude={"api_key_env", "baseline_file"}) for p in engine.settings.printers],
            "profiles": [p.model_dump() for p in engine.settings.profiles],
            "policy": engine.settings.policy.model_dump(),
            "model": {**engine.settings.local_model.model_dump(exclude={"api_key"}),
                      "api_key_set": bool(engine.settings.local_model.api_key)},
            "questions": engine.store.questions(), "alerts": engine.store.alerts(), "conversations": engine.store.conversations()}


async def dispatch(engine, action: str, payload: dict):
    def exact(required=(), optional=()):
        if set(payload) - set(required) - set(optional) or any(k not in payload for k in required):
            raise ServiceError("Invalid action fields", 422)

    if action == "create_job":
        return engine.public_job(await engine.create(JobRequest.model_validate(payload)))
    if action in {"select_model", "prepare_job", "queue_job", "start_job", "pause_job", "cancel_job", "resume_job"}:
        extra = {"select_model": ("artifact_id",), "prepare_job": ("profile_id",)}.get(action, ())
        exact(("job_id", *extra))
        id = payload["job_id"]
        if action == "select_model":
            job = await engine.select(id, payload["artifact_id"])
        elif action == "prepare_job":
            job = await engine.prepare(id, payload["profile_id"])
        elif action == "queue_job":
            job = await engine.queue(id)
        elif action == "start_job":
            job = await engine.start(id)
        else:
            job = await engine.control(id, action.removesuffix("_job"))
        return engine.public_job(job)
    if action in {"approve_resume", "approve_budget"}:
        exact(("job_id",))
        job = engine.store.get(payload["job_id"])
        async with engine.job_locks[job["id"]]:
            job = engine.store.get(job["id"])
            if action == "approve_resume":
                if job["state"] != "paused":
                    raise ServiceError("Only a paused job can receive a resume decision")
                engine.store.approve(job["id"], "resume")
            else:
                if job.get("ambiguous_start") or job.get("control_reconciliation_required"):
                    raise ServiceError("Reconcile the physical operation before a budget decision")
                estimates = job.get("estimates", {})
                if any(not isinstance(estimates.get(k), (int, float)) or not math.isfinite(estimates[k])
                       or estimates[k] <= 0 for k in ("hours", "grams")):
                    raise ServiceError("Known finite time/filament estimates are required for a budget decision")
                if job["state"] not in {"prepared", "queued", "held"} or not job.get("gcode_sha256"):
                    raise ServiceError("Prepare the exact artifact before approving its budget")
                engine.store.budget_approve(job["id"], budget_fingerprint(job))
                holds = [h for h in job.get("holds", []) if not h.startswith("Estimated time/filament exceeds")]
                engine.store.update(job["id"], "prepared" if job["state"] == "held" and not holds else None, holds=holds)
        return engine.public_job(engine.store.get(job["id"]))
    if action == "open_project":
        exact(("job_id",))
        job = engine.store.get(payload["job_id"])
        project = Path(job.get("editable_project") or "").resolve()
        if (not project.is_relative_to(engine.home / "jobs" / job["id"]) or not project.is_file()
                or sha256(project) != job.get("project_sha256")):
            raise ServiceError("Verified native project is unavailable; prepare it first")
        return {"job_id": job["id"], "project_path": str(project), "sha256": job["project_sha256"]}
    if action == "probe_printer":
        exact(("printer_id",))
        return await Moonraker(engine.printer(payload["printer_id"]), read_only_probe=True).status()
    if action == "acknowledge_alert":
        exact(("alert_id",))
        engine.store.acknowledge(int(payload["alert_id"]))
        return {"ok": True}
    if action == "answer_question":
        exact(("question_id", "answer"))
        answer = payload["answer"]
        if not isinstance(answer, str) or not 0 < len(answer.strip()) <= 16000:
            raise ServiceError("Enter an answer of up to 16000 characters", 422)
        return engine.store.answer_question(payload["question_id"], answer)
    if action == "new_conversation":
        exact()
        return engine.store.conversation()
    if action == "get_conversation":
        exact(("conversation_id",))
        return engine.store.conversation(payload["conversation_id"])
    if action == "chat":
        exact(("message",), ("conversation_id",))
        message = payload["message"]
        if not isinstance(message, str) or not 0 < len(message.strip()) <= 16000:
            raise ServiceError("Enter a message of up to 16000 characters", 422)
        conversation = engine.store.conversation(payload.get("conversation_id"))
        id = conversation["id"]
        if id in engine.chat_tasks and not engine.chat_tasks[id].done():
            raise ServiceError("Wait for the current local model response")
        if not engine.settings.local_model.model:
            raise ServiceError("Configure your local model endpoint and model first")
        engine.store.message(id, {"role": "user", "content": message})
        from .local_chat import respond
        task = asyncio.create_task(respond(engine, id))
        engine.chat_tasks[id] = task
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return {"conversation_id": id, "status": "running"}
    # Enrollment changes serialize and publish only validated configuration.
    async with engine.config_lock:
        settings = engine.settings.model_copy(deep=True)
        if action == "save_model":
            exact(("base_url", "model"), ("api_key",))
            if not isinstance(payload["model"], str) or not 0 < len(payload["model"]) <= 200:
                raise ServiceError("A model identifier is required", 422)
            await asyncio.to_thread(local_origin, payload["base_url"])
            settings.local_model = LocalModel(**{**settings.local_model.model_dump(), **payload})
        elif action == "save_policy":
            exact(("max_hours", "max_grams", "monitoring_loss_seconds", "pause_on_monitoring_loss"))
            settings.policy = Policy(**{**settings.policy.model_dump(), **payload,
                                       "limits_confirmed": True, "monitoring_policy_confirmed": True})
        elif action == "save_profile":
            profile = Profile.model_validate(payload)
            engine.printer(profile.printer_id)
            for value in [*profile.settings, *profile.filaments]:
                file = Path(value).expanduser().resolve()
                if not file.is_file() or file.suffix != ".json":
                    raise ServiceError("Profile files must be accessible local JSON files")
            settings.profiles = [p for p in settings.profiles if p.id != profile.id] + [profile]
        elif action == "enroll_printer":
            editable = {"name", "model", "api_key_env", "endpoint", "nozzle_mm", "cfs", "camera_url", "camera_association_confirmed",
                        "frame_sequence_header", "frame_time_header", "roi", "bed_detector_url", "failure_detector_url",
                        "material", "color", "filament_verified", "remaining_grams", "identity_confirmed"}
            exact(("printer_id",), editable)
            original = engine.printer(payload["printer_id"])
            changes = {k: v for k, v in payload.items() if k != "printer_id"}
            if "model" in changes and changes["model"] != original.model:
                raise ServiceError("Keep the registered fleet model; change the selected printer instead")
            updated = Printer.model_validate({**original.model_dump(), **changes})
            for url in [updated.endpoint, updated.camera_url]:
                if url:
                    await asyncio.to_thread(pin_lan, url)
            for url in [updated.bed_detector_url, updated.failure_detector_url]:
                if url and not _pin_loopback(url):
                    raise ServiceError("Camera analysis must use a loopback detector on this Mac")
            changed = {k for k in changes if getattr(original, k) != getattr(updated, k)}
            if changed - {"name", "model"}:
                # Changing enrollment invalidates automatic readiness; it can never grant qualification.
                updated.auto_start = False
            if changed & {"endpoint", "api_key_env", "nozzle_mm", "cfs", "identity_confirmed"}:
                updated.control_qualified = updated.protocol_qualified = False
            if changed & {"camera_url", "camera_association_confirmed", "roi", "frame_sequence_header", "frame_time_header", "bed_detector_url"}:
                updated.vision_qualified = False
            if changed & {"camera_url", "camera_association_confirmed", "frame_sequence_header", "frame_time_header", "failure_detector_url"}:
                updated.failure_detector_qualified = False
            settings.printers = [updated if p.id == updated.id else p for p in settings.printers]
        elif action == "save_cfs_inventory":
            exact(("printer_id", "slots"))
            engine.printer(payload["printer_id"])
            slots = [CFSSlot.model_validate(s) for s in payload["slots"]]
            if len(slots) > 16 or len({s.slot_id for s in slots}) != len(slots):
                raise ServiceError("CFS inventory needs unique slot identifiers, at most 16")
            for printer in settings.printers:
                if printer.id == payload["printer_id"]:
                    printer.cfs_slots = slots
                    printer.auto_start = False
        elif action == "enroll_reference":
            exact(("printer_id", "bed_clear_confirmed", "roi"))
            if payload["bed_clear_confirmed"] is not True:
                raise ServiceError("Confirm the physical bed is empty before recording a reference")
            printer = engine.printer(payload["printer_id"])
            capture = await engine.camera.capture(printer)
            if not capture:
                raise ServiceError("A fresh camera frame with qualified source timestamps is required")
            # Image bytes never enter an HTTP response, event, or chat context.
            import io

            from PIL import Image
            roi = tuple(payload["roi"])
            with Image.open(io.BytesIO(capture[0])) as image:
                if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= image.width and 0 <= roi[1] < roi[3] <= image.height):
                    raise ServiceError("ROI must cover a valid portion of the camera frame")
            folder = engine.home / "references"
            folder.mkdir(mode=0o700, exist_ok=True)
            name = printer.id + "-" + uuid.uuid4().hex + ".jpg"
            path = folder / name
            path.write_bytes(capture[0])
            path.chmod(0o600)
            for p in settings.printers:
                if p.id == printer.id:
                    p.baseline_file, p.roi = "references/" + name, roi
                    p.vision_qualified = p.auto_start = False
        else:
            raise ServiceError("Unknown operator action", 422)
        save_settings(engine.home, settings)
        # Preserve the shared Settings identity used by API readers.
        for field in type(settings).model_fields:
            setattr(engine.settings, field, getattr(settings, field))
        engine.store.event(None, "configuration_updated", {"action": action})
    return state(engine)


def is_loopback_client(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def typed_error(error: Exception) -> ServiceError:
    if isinstance(error, ValidationError):
        return ServiceError("Invalid typed operator action", 422)
    return ServiceError("Invalid operator action fields", 422)
