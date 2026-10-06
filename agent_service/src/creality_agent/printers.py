from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Printer
from .models import ServiceError
from .network import pin_lan


class Moonraker:
    def __init__(self, printer: Printer, *, read_only_probe: bool = False):
        if not printer.endpoint or (not read_only_probe and (not printer.identity_confirmed or not printer.protocol_qualified)):
            raise ServiceError("Printer identity and LAN protocol require qualification")
        parts = urlsplit(printer.endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ServiceError("Printer endpoint configuration is invalid")
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ServiceError("Configure a printer origin without a path or query")
        self.read_only_probe = read_only_probe
        self.printer = printer
        self.endpoint = printer.endpoint.rstrip("/")
        key = os.environ.get(printer.api_key_env, "") if printer.api_key_env else ""
        self.headers = {"X-Api-Key": key} if key else {}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                pinned, host_header, host = await asyncio.to_thread(pin_lan, self.endpoint + path)
                async with client.stream(method, pinned, headers={**self.headers, "Host": host_header},
                                         extensions={"sni_hostname": host}, **kwargs) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 1024 * 1024:
                            raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, dict) or "result" not in result:
                    raise ValueError()
                value = result["result"]
                if path.startswith("/printer/print/"):
                    if value != "ok":
                        raise ValueError()
                elif not isinstance(value, dict):
                    raise ValueError()
                return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise ServiceError("Printer did not return a usable response; action may require reconciliation") from None

    async def status(self) -> dict:
        info = await self._request("GET", "/printer/info")
        result = await self._request("GET", "/printer/objects/query?print_stats&virtual_sdcard&pause_resume")
        stats = result.get("status", {}).get("print_stats", {})
        state = stats.get("state")
        if info.get("state") != "ready" or state not in {"standby", "printing", "paused", "complete", "error"}:
            raise ServiceError("Printer status is missing or not ready")
        return {"state": state, "filename": stats.get("filename", ""),
                "progress": result.get("status", {}).get("virtual_sdcard", {}).get("progress"),
                "print_duration": stats.get("print_duration"), "observed_at": time.time(),
                "firmware": info.get("software_version"), "eventtime": result.get("eventtime")}

    async def upload(self, path: Path, remote_name: str):
        if self.read_only_probe or not self.printer.control_qualified:
            raise ServiceError("Printer upload/control requires qualification")
        # print=false: upload alone must never trigger a physical start.
        with path.open("rb") as f:
            result = await self._request("POST", "/server/files/upload", data={"root": "gcodes", "print": "false"},
                                         files={"file": (remote_name, f, "application/octet-stream")})
        if result.get("print_started"):
            raise ServiceError("Unexpected printer start during upload; reconciliation required")
        name = result.get("item", {}).get("path")
        if name != remote_name:
            raise ServiceError("Printer did not confirm the expected uploaded file")
        return name

    async def control(self, action: str, filename: str | None = None):
        if self.read_only_probe or not self.printer.control_qualified or action not in {"start", "pause", "resume", "cancel"}:
            raise ServiceError("Requested printer control is not qualified")
        body = {"filename": filename} if filename else {}
        await self._request("POST", "/printer/print/" + action, json=body)
