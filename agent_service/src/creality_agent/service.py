from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .analysis import ModelError, analyze_model
from .config import Printer, Settings
from .ingestion import download, extract_models, import_file, sha256, validate_archive
from .models import JobRequest, ServiceError
from .notifications import notify
from .operator import budget_fingerprint
from .printers import Moonraker
from .slicer import SliceError, slice_model
from .store import Store
from .vision import Camera, failure_assessment, qualified_bed_assessment


class Engine:
    def __init__(self, home: Path, settings: Settings):
        self.home, self.settings = home.resolve(), settings
        self.store = Store(home)
        self.job_locks = defaultdict(asyncio.Lock)
        self.printer_locks = defaultdict(asyncio.Lock)
        self.tasks: dict[str, asyncio.Task] = {}
        self.camera = Camera()
        self.last_monitor: dict[str, float] = {}
        self.loop_task: asyncio.Task | None = None
        self.tick_tasks: dict[str, asyncio.Task] = {}
        self.awake_process = None
        self.acquire_slots = asyncio.Semaphore(2)
        self.config_lock = asyncio.Lock()
        self.chat_tasks: dict[str, asyncio.Task] = {}
        self.agent_tools = None
        self.notification_attempts: dict[int, float] = {}
        self.alert_task: asyncio.Task | None = None

    def printer(self, id: str) -> Printer:
        for p in self.settings.printers:
            if p.id == id:
                return p
        raise ServiceError("Unknown printer", 404)

    def public_job(self, job: dict) -> dict:
        result = dict(job)
        request = dict(result.get("request", {}))
        if request.get("source_url"):
            parts = urlsplit(request["source_url"])
            request["source_url"] = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
        request.pop("local_path", None)
        result["request"] = request
        result.pop("model_path", None)
        result.pop("gcode_path", None)
        result.pop("source_path", None)
        result["project_available"] = bool(result.pop("editable_project", None))
        result["models"] = [{k: v for k, v in m.items() if k != "path"} for m in result.get("models", [])]
        return result

    def capabilities(self) -> dict:
        return {"version": "0.1.0", "durable_jobs": True, "api": True, "mcp": True,
                "formats": ["STL", "OBJ", "3MF", "ZIP"], "camera_pixels_exported": False,
                "adapters": {"slicer_cli": "installed-help-verified; execution requires profile",
                             "editable_project_export": "isolated native helper implemented; round-trip qualification required",
                             "creality_gui_ipc": "owner-only local app bridge implemented",
                             "moonraker": "implemented; requires per-printer qualification",
                             "cfs_control": "not-yet-qualified", "bed_recognition": "requires local qualified detector",
                             "failure_recognition": "requires local qualified detector"},
                "policy": self.settings.policy.model_dump()}

    def printers(self) -> list[dict]:
        return [{"id": p.id, "name": p.name, "model": p.model, "nozzle_mm": p.nozzle_mm, "cfs": p.cfs,
                 "identity_confirmed": p.identity_confirmed, "protocol_qualified": p.protocol_qualified,
                 "control_qualified": p.control_qualified, "camera_association_confirmed": p.camera_association_confirmed,
                 "vision_qualified": p.vision_qualified, "filament_verified": p.filament_verified,
                 "failure_detector_qualified": p.failure_detector_qualified, "material": p.material,
                 "color": p.color, "remaining_grams": p.remaining_grams,
                 "cfs_slots": [slot.model_dump() for slot in p.cfs_slots], "auto_start": p.auto_start} for p in self.settings.printers]

    def spawn(self, id: str, coro):
        if id in self.tasks and not self.tasks[id].done():
            coro.close()
            raise ServiceError("Job has an active operation")
        task = asyncio.create_task(coro)
        self.tasks[id] = task
        # Always retrieve exceptions; errors are persisted by the worker.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    async def create(self, request: JobRequest) -> dict:
        if request.printer_id:
            self.printer(request.printer_id)
        job = self.store.create({"request": request.model_dump(), "models": [], "holds": []})
        self.store.update(job["id"], "acquiring")
        self.spawn(job["id"], self.acquire(job["id"]))
        return self.store.get(job["id"])

    async def acquire(self, id: str):
        async with self.acquire_slots:
            await self._acquire(id)

    async def _acquire(self, id: str):
        folder = self.home / "jobs" / id
        folder.mkdir(parents=True, exist_ok=True)
        original = folder / "original.download"
        try:
            request = self.store.get(id)["request"]
            if request.get("source_url"):
                suffix = await asyncio.to_thread(download, request["source_url"], original, self.settings.policy)
            else:
                suffix = await asyncio.to_thread(import_file, request["local_path"], original,
                                                 self.settings.import_roots, self.settings.policy)
            source = folder / ("original" + suffix)
            original.rename(source)
            if suffix == ".zip":
                models = await asyncio.to_thread(extract_models, source, folder / "models", self.settings.policy)
            else:
                if suffix == ".3mf":
                    await asyncio.to_thread(validate_archive, source, self.settings.policy)
                models = [source]
            records = []
            for model in models:
                metadata = await asyncio.to_thread(analyze_model, model)
                records.append({"artifact_id": hashlib.sha256(str(model.relative_to(folder)).encode()).hexdigest()[:24],
                                "name": str(model.relative_to(folder)), "path": str(model),
                                "sha256": await asyncio.to_thread(sha256, model), "metadata": metadata})
            holds = [] if len(records) == 1 else ["Select the required model/variant; multiple files were acquired"]
            self.store.update(id, "acquired" if not holds else "held", models=records,
                              selected_artifact=records[0]["artifact_id"] if len(records) == 1 else None,
                              source_path=str(source), source_sha256=await asyncio.to_thread(sha256, source), holds=holds)
        except asyncio.CancelledError:
            original.unlink(missing_ok=True)
            self.store.update(id, "held", holds=["Acquisition interrupted; retry requires a new job"])
            raise
        except (ServiceError, ModelError) as error:
            self.store.update(id, "held", holds=[str(error)])
        except Exception:  # noqa: BLE001 -- worker boundary persists a sanitized failure
            self.store.update(id, "held", holds=["Model acquisition failed; inspect local inputs"])

    async def select(self, id: str, artifact_id: str) -> dict:
        async with self.job_locks[id]:
            job = self.store.get(id)
            if job.get("ambiguous_start") or job.get("control_reconciliation_required") or job.get("remote_filename"):
                raise ServiceError("Model selection cannot discard physical job ownership")
            if job["state"] not in {"acquired", "held"} or not any(
                    m["artifact_id"] == artifact_id for m in job.get("models", [])):
                raise ServiceError("Select a model from this job's imported artifact list")
            return self.store.update(id, "acquired", selected_artifact=artifact_id, holds=[])

    async def prepare(self, id: str, profile_id: str) -> dict:
        async with self.job_locks[id]:
            job = self.store.get(id)
            if job["state"] not in {"acquired", "held", "prepared"}:
                raise ServiceError("Wait for acquisition before preparing the job")
            if job.get("ambiguous_start") or job.get("control_reconciliation_required"):
                raise ServiceError("Reconcile the previous physical operation before re-preparing")
            profile = next((p for p in self.settings.profiles if p.id == profile_id), None)
            if profile is None or not profile.verified:
                raise ServiceError("A verified local printer/process/filament profile is required")
            model = next((m for m in job.get("models", []) if m["artifact_id"] == job.get("selected_artifact")), None)
            if not model:
                raise ServiceError("Select a model before preparation")
            if sha256(Path(model["path"])) != model["sha256"]:
                raise ServiceError("Acquired model changed; import it again before preparation")
            printer = self.printer(profile.printer_id)
            request = job["request"]
            if request.get("printer_id") and request["printer_id"] != printer.id:
                raise ServiceError("Profile does not match the explicitly requested printer")
            if not printer.identity_confirmed or printer.nozzle_mm != profile.nozzle_mm:
                raise ServiceError("Printer/nozzle configuration does not match the profile")
            if (request.get("settings") or request.get("copies", 1) != 1) and not self.settings.gui_helper_binary:
                raise ServiceError("Explicit setting overrides/copies require the native project helper; none are ignored")
            if request.get("material") and request["material"].lower() != profile.material.lower():
                raise ServiceError("Profile would substitute the requested material; owner decision required")
            if request.get("color") and request["color"].lower() != (profile.color or "").lower():
                raise ServiceError("Profile would substitute the requested color; owner decision required")
            if Path(model["path"]).suffix.lower() == ".3mf" and not self.settings.gui_helper_binary:
                raise ServiceError("Imported 3MF settings require native project normalization before slicing")
            profile_fingerprint = hashlib.sha256(profile.model_dump_json().encode()).hexdigest()
            self.store.update(id, "preparing", printer_id=printer.id, profile_id=profile.id,
                              profile_fingerprint=profile_fingerprint, holds=[])
            self.spawn(id, self._slice(id, Path(model["path"]), profile))
            return self.store.get(id)

    async def _slice(self, id: str, model: Path, profile):
        output = self.home / "jobs" / id / ("slice-" + str(time.time_ns()))
        try:
            project, verification, helper_warnings = None, {}, []
            if self.settings.gui_helper_binary:
                from .project import export_project
                project, verification, helper_warnings = await export_project(self, id, model, profile,
                    output.parent / (output.name + "-project"))
                # Re-open and re-slice the saved project without external profile flags.
                result = await slice_model(Path(self.settings.slicer_binary), project, output, [], [], cli_mode=True)
            else:
                result = await slice_model(Path(self.settings.slicer_binary), model, output,
                                           [Path(p) for p in profile.settings], [Path(p) for p in profile.filaments])
            files = result["gcode_paths"]
            if len(files) != 1:
                raise ServiceError("Multiple plates need an explicit assembly/plate plan")
            gcode = Path(files[0])
            estimates = gcode_estimates(gcode)
            self.store.update(id, "prepared", gcode_path=str(gcode), gcode_sha256=sha256(gcode),
                              estimates=estimates, warnings=result["warnings"] + helper_warnings,
                              editable_project=str(project) if project else None, **verification,
                              holds=["Physical bed-fit/process preflight qualification is required"], preflight_qualified=False)
        except asyncio.CancelledError:
            self.store.update(id, "held", holds=["Preparation interrupted"])
            raise
        except (SliceError, ServiceError) as error:
            self.store.update(id, "held", holds=[str(error)])
        except Exception:  # noqa: BLE001 -- worker boundary persists a sanitized failure
            self.store.update(id, "held", holds=["Preparation failed; inspect local slicer configuration"])

    async def status(self, id: str) -> dict:
        return await Moonraker(self.printer(id)).status()

    async def queue(self, id: str) -> dict:
        async with self.job_locks[id]:
            job = self.store.get(id)
            if job.get("ambiguous_start") or job.get("control_reconciliation_required"):
                raise ServiceError("Reconcile the previous physical operation before requeueing")
            if job["state"] not in {"prepared", "held"} or not job.get("gcode_path"):
                raise ServiceError("Prepare a job before queueing")
            return self.store.update(id, "queued")

    async def eligibility(self, job: dict) -> tuple[Printer, Moonraker, list[str]]:
        p = self.printer(job.get("printer_id") or job["request"].get("printer_id") or "")
        holds = list(job.get("holds", []))
        if self.store.has_open_question(job["id"]):
            holds.append("An unanswered owner question holds this job")
        if not all([p.identity_confirmed, p.protocol_qualified, p.control_qualified, p.auto_start,
                    p.vision_qualified, p.camera_association_confirmed, p.filament_verified]):
            holds.append("Printer, control, camera, clearance and filament must be qualified for automatic start")
        if not p.failure_detector_qualified or not p.failure_detector_url:
            holds.append("Failure monitoring must be qualified before automatic start")
        if any(j.get("monitoring_lost") and j["state"] in {"printing", "paused", "held"} for j in self.store.list()):
            holds.append("Monitoring is unavailable; new starts are held until recovery")
        if p.cfs:
            holds.append("CFS slot/color mapping and load management require a qualified adapter")
        profile = next((r for r in self.settings.profiles if r.id == job.get("profile_id")), None)
        if not profile or not profile.verified or p.nozzle_mm != profile.nozzle_mm:
            holds.append("Verified nozzle/profile match is required")
        if profile and job.get("profile_fingerprint") != hashlib.sha256(profile.model_dump_json().encode()).hexdigest():
            holds.append("Profile enrollment changed; prepare the job again")
        if profile and job.get("profile_sha256"):
            from .project import profile_digest
            if profile_digest([Path(f) for f in [*profile.settings, *profile.filaments]]) != job["profile_sha256"]:
                holds.append("Profile file contents changed; prepare the job again")
        if profile and (p.material != profile.material or (profile.color and p.color != profile.color)):
            holds.append("Loaded material/color does not match the sliced profile")
        policy = self.settings.policy
        estimate = job.get("estimates", {})
        if not policy.limits_confirmed:
            holds.append("Owner has not selected automatic-start resource limits")
        if not policy.monitoring_policy_confirmed:
            holds.append("Owner has not selected monitoring-loss behavior")
        if not job.get("editable_project") or not job.get("preflight_qualified"):
            holds.append("Saved editable project and qualified preflight are required")
        if job.get("warnings"):
            holds.append("Slicer warnings require resolution")
        if any(not isinstance(estimate.get(k), (int, float)) or not math.isfinite(estimate[k])
               or estimate[k] <= 0 for k in ("hours", "grams")):
            holds.append("Usable time/filament estimates are required")
        elif ((estimate["hours"] > policy.max_hours or estimate["grams"] > policy.max_grams)
              and not self.store.budget_allowed(job["id"], budget_fingerprint(job))):
            holds.append("Estimated time/filament exceeds configured limits; owner decision required")
        if estimate.get("grams") is not None and (p.remaining_grams is None or p.remaining_grams < estimate["grams"]):
            holds.append("Verified remaining filament is insufficient or unknown")
        if holds:
            raise ServiceError("; ".join(dict.fromkeys(holds)))
        adapter = Moonraker(p)
        status = await adapter.status()
        if status["state"] not in {"standby", "complete"}:
            raise ServiceError("Printer is not idle")
        capture = await self.camera.capture(p)
        if not capture:
            raise ServiceError("Camera is unavailable or lacks qualified source freshness")
        frame, captured = capture
        result = await qualified_bed_assessment(p, frame, captured, self.home)
        if result.get("verdict") != "clear" or time.time() - captured > policy.frame_max_age_seconds:
            raise ServiceError("Fresh bed clearance is not established")
        monitored = await failure_assessment(p, frame)
        if monitored.get("verdict") != "ok":
            raise ServiceError("Fresh failure-monitoring assessment is unavailable or indicates a problem")
        return p, adapter, []

    async def start(self, id: str) -> dict:
        async with self.job_locks[id]:
            job = self.store.get(id)
            if job["state"] not in {"queued", "prepared"}:
                raise ServiceError("Only a queued/prepared job can start; previous starts are not retried")
            pid = job.get("printer_id", "")
            async with self.printer_locks[pid]:
                try:
                    _p, adapter, _ = await self.eligibility(job)
                    path = Path(job["gcode_path"])
                    if sha256(path) != job["gcode_sha256"]:
                        raise ServiceError("Prepared artifact changed; re-prepare before starting")
                    # Persist intent before the first remote state-changing request.
                    remote = id + ".gcode"
                    self.store.update(id, "starting", remote_filename=remote, holds=[])
                    await adapter.upload(path, remote)
                    # Upload time can stale camera/state: require another full eligibility check.
                    await self.eligibility(job)
                    self.store.consume_budget(id)
                    await adapter.control("start", remote)
                    observed = await self.observe(adapter, "printing", remote)
                    return self.store.update(id, "printing", observation=observed)
                except ServiceError as error:
                    state = self.store.get(id)["state"]
                    return self.store.update(id, "held", holds=[str(error)],
                                             ambiguous_start=state == "starting")
                except Exception:  # noqa: BLE001 -- worker boundary persists a sanitized failure
                    return self.store.update(id, "held", holds=["Start interrupted; reconcile printer state"],
                                             ambiguous_start=self.store.get(id)["state"] == "starting")

    async def observe(self, adapter: Moonraker, wanted: str, filename: str) -> dict:
        for _ in range(10):
            status = await adapter.status()
            if status["filename"] == filename and status["state"] == wanted:
                return status
            await asyncio.sleep(0.5)
        raise ServiceError("Requested printer state was not observed; owner reconciliation required")

    async def control(self, id: str, action: str) -> dict:
        async with self.job_locks[id]:
            job = self.store.get(id)
            if job.get("ambiguous_start") or job.get("control_reconciliation_required"):
                raise ServiceError("Printer operation is unresolved; reconcile before changing this job")
            if action == "cancel" and job["state"] not in {"starting", "printing", "paused", "pausing", "resuming", "canceling"}:
                task = self.tasks.get(id)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                return self.store.update(id, "canceled", holds=[])
            expected = {"pause": {"printing"}, "resume": {"paused"}, "cancel": {"printing", "paused"}}
            if action not in expected or job["state"] not in expected[action]:
                raise ServiceError("Job is not in a state eligible for that control")

            if action == "resume" and not self.store.consume(id, "resume"):
                raise ServiceError("Resume requires a recorded owner decision")
            adapter = Moonraker(self.printer(job["printer_id"]))
            current = await adapter.status()
            if current["filename"] != job.get("remote_filename"):
                raise ServiceError("Printer is running a different job; no control sent")
            pending = {"pause": "pausing", "resume": "resuming", "cancel": "canceling"}[action]
            self.store.update(id, pending)
            try:
                await adapter.control(action)
                wanted = {"pause": "paused", "resume": "printing", "cancel": "standby"}[action]
                observed = await self.observe(adapter, wanted, job["remote_filename"])
                return self.store.update(id, "canceled" if action == "cancel" else wanted, observation=observed)
            except ServiceError as error:
                return self.store.update(id, "held", holds=[str(error)], control_reconciliation_required=True)

    async def recover(self):
        for job in self.store.worklist():
            if job["state"] in {"acquiring", "preparing"}:
                self.store.update(job["id"], "held", holds=["Interrupted preparation/acquisition requires re-preparation"])
            elif job["state"] in {"starting", "printing", "paused", "pausing", "resuming", "canceling"}:
                try:
                    status = await self.status(job["printer_id"])
                    if status["filename"] != job.get("remote_filename"):
                        raise ServiceError("Printer job does not match saved start intent")
                    if status["state"] in {"printing", "paused"}:
                        self.store.update(job["id"], status["state"], observation=status)
                    elif status["state"] == "complete":
                        self.store.update(job["id"], "completed", observation=status)
                    else:
                        raise ServiceError("Saved job needs owner reconciliation")
                except ServiceError as error:
                    self.store.update(job["id"], "held", holds=[str(error)], ambiguous_start=True)

        self.store.event(None, "recovery", {})

    def monitoring_lost(self, id: str):
        since = time.monotonic() - self.last_monitor.setdefault(id, time.monotonic())
        if since > self.settings.policy.monitoring_loss_seconds and not self.store.get(id).get("monitoring_lost"):
            self.store.update(id, monitoring_lost=True)
            self.store.event(id, "monitoring_unavailable", {"seconds": round(since)})
        return since

    async def alerts(self):
        self.store.collect_alerts()
        if self.settings.notifications_enabled:
            now = time.monotonic()
            for alert in self.store.pending_notifications():
                if now - self.notification_attempts.get(alert["id"], -60) < 60:
                    continue
                self.notification_attempts[alert["id"]] = now
                if await notify("Creality Print Local Agent", alert["message"], str(alert["id"]), self.home):
                    self.store.mark_delivered(alert["id"])

    async def monitor(self, job: dict):
        id, p = job["id"], self.printer(job["printer_id"])
        try:
            status = await self.status(p.id)
            if status["filename"] != job.get("remote_filename"):
                raise ServiceError("Observed printer job changed; monitoring ownership lost")
            if status["state"] == "complete":
                self.store.update(id, "completed", observation=status)
                return
            if status["state"] == "error":
                self.store.update(id, "failed", observation=status)
                return
            if status["state"] == "paused":
                self.store.update(id, "paused", observation=status)
                return
            if status["state"] != "printing":
                raise ServiceError("Print is no longer in the expected active state")
            capture = await self.camera.capture(p)
            fresh = capture and time.time() - capture[1] <= self.settings.policy.frame_max_age_seconds
            assessment = await failure_assessment(p, capture[0]) if fresh else {"verdict": "unknown"}
            if assessment["verdict"] == "failed":
                self.store.event(id, "failure_detected", {"action": "pause"})
                await self.control(id, "pause")
                return
            if assessment["verdict"] == "ok":
                self.last_monitor[id] = time.monotonic()
                if self.store.get(id).get("monitoring_lost"):
                    self.store.update(id, monitoring_lost=False)
                    self.store.event(id, "monitoring_recovered", {})
            else:
                since = self.monitoring_lost(id)
                policy = self.settings.policy
                if (since > policy.monitoring_loss_seconds and policy.monitoring_policy_confirmed
                        and policy.pause_on_monitoring_loss):
                    await self.control(id, "pause")
                    return
            self.store.update(id, observation=status, monitoring=assessment)
        except ServiceError as error:
            self.monitoring_lost(id)
            self.store.event(id, "monitoring_error", {"reason": str(error)})

    async def monitor_paused(self, job: dict):
        # Observe owner controls at the physical printer while retaining tool resume authority.
        try:
            p = self.printer(job["printer_id"])
            status = await self.status(p.id)
            if status["filename"] != job.get("remote_filename"):
                raise ServiceError("Paused printer job changed; monitoring ownership lost")
            if status["state"] in {"printing", "complete", "error"}:
                observed = {"printing": "printing", "complete": "completed", "error": "failed"}[status["state"]]
                self.store.update(job["id"], observed, observation=status)
                return
            if status["state"] != "paused":
                raise ServiceError("Paused print state is unavailable")
            self.store.update(job["id"], observation=status)
            if not job.get("monitoring_lost"):
                return
            capture = await self.camera.capture(p)
            fresh = capture and time.time() - capture[1] <= self.settings.policy.frame_max_age_seconds
            assessment = await failure_assessment(p, capture[0]) if fresh else {"verdict": "unknown"}
            if assessment["verdict"] == "ok":
                self.last_monitor[job["id"]] = time.monotonic()
                self.store.update(job["id"], monitoring_lost=False, monitoring=assessment, observation=status)
                self.store.event(job["id"], "monitoring_recovered", {})
        except ServiceError as error:
            self.monitoring_lost(job["id"])
            self.store.event(job["id"], "monitoring_error", {"reason": str(error)})

    async def tick(self, job: dict):
        try:
            if job["state"] == "queued":
                await self.start(job["id"])
            elif job["state"] == "printing":
                await self.monitor(job)
            elif job["state"] == "paused":
                await self.monitor_paused(job)
            elif job["state"] == "held" and (job.get("ambiguous_start") or job.get("control_reconciliation_required")):
                status = await self.status(job["printer_id"])
                if status["filename"] == job.get("remote_filename") and status["state"] in {"printing", "paused"}:
                    self.store.update(job["id"], status["state"], observation=status, holds=[],
                                      ambiguous_start=False, control_reconciliation_required=False)
        except ServiceError as error:
            self.store.event(job["id"], "worker_hold", {"reason": str(error)})
        except Exception:  # noqa: BLE001 -- one job must not stop other printers' monitoring
            self.store.event(job["id"], "worker_error", {"reason": "Operation failed; local reconciliation required"})

    async def loop(self):
        await self.recover()
        while True:
            jobs = self.store.worklist()
            active_ids = {j["id"] for j in jobs}
            for id, task in list(self.tick_tasks.items()):
                if id not in active_ids and task.done():
                    del self.tick_tasks[id]
            for job in jobs:
                existing = self.tick_tasks.get(job["id"])
                if existing is None or existing.done():
                    self.tick_tasks[job["id"]] = asyncio.create_task(self.tick(job))
            await self.update_awake(jobs)
            self.store.collect_alerts()
            if self.alert_task is None or self.alert_task.done():
                self.alert_task = asyncio.create_task(self.alerts())
                self.alert_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            await asyncio.sleep(3)

    async def update_awake(self, jobs: list[dict]):
        active = any(j["state"] in {"acquiring", "preparing", "starting", "printing", "paused", "pausing", "resuming", "canceling"}
                     for j in jobs)
        executable = Path("/usr/bin/caffeinate")
        if active and executable.is_file() and self.awake_process is None:
            self.awake_process = await asyncio.create_subprocess_exec(str(executable), "-i",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        elif not active and self.awake_process is not None:
            self.awake_process.terminate()
            await self.awake_process.wait()
            self.awake_process = None

    async def close(self):
        if self.loop_task:
            self.loop_task.cancel()
        for task in [*self.tasks.values(), *self.tick_tasks.values(), *self.chat_tasks.values(),
                     *([self.alert_task] if self.alert_task else [])]:
            task.cancel()
        await asyncio.gather(*self.tasks.values(), *self.tick_tasks.values(), *self.chat_tasks.values(),
                             *([self.loop_task] if self.loop_task else []),
                             *([self.alert_task] if self.alert_task else []), return_exceptions=True)
        if self.awake_process is not None:
            self.awake_process.terminate()
            await self.awake_process.wait()
            self.awake_process = None
        self.store.close()


def gcode_estimates(path: Path) -> dict:
    # Only slicer-generated comments supply estimates; absent/unrecognized fields hold starts.
    with path.open(errors="replace") as f:
        text = f.read(256 * 1024)
    grams = re.search(r";\s*filament used \[g\]\s*=\s*([\d.]+)", text, re.IGNORECASE)
    duration = re.search(r";\s*estimated printing time[^=]*=\s*([^\r\n]+)", text, re.IGNORECASE)
    hours = None
    if duration:
        parts = re.findall(r"([\d.]+)\s*([dhms])", duration[1])
        if parts:
            seconds = sum(float(v) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for v, u in parts)
            hours = seconds / 3600
    return {"hours": hours, "grams": float(grams[1]) if grams else None}
