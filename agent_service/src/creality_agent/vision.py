"""Local-only reference comparator. Qualification is required before it can clear a bed."""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np
from PIL import Image, ImageFilter, UnidentifiedImageError

from .config import Printer
from .models import ServiceError
from .network import pin_lan


def _pin_loopback(url: str) -> tuple[str, str] | None:
    """Rewrite an explicitly loopback detector URL to a literal loopback address."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"} or
                parts.username is not None or parts.password is not None or parts.fragment or
                parts.netloc.endswith(":")):
            return None
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            return None
        port = port or 80
        literal = "::1" if host == "::1" else "127.0.0.1"
        bracketed = f"[{literal}]" if ":" in literal else literal
        target = urlunsplit(("http", f"{bracketed}:{port}", parts.path or "/", parts.query, ""))
        display_host = f"[{host}]" if ":" in host else host
        host_header = f"{display_host}:{port}" if port != 80 or parts.port is not None else display_host
        return target, host_header
    except (ValueError, UnicodeError):
        return None


class Camera:
    def __init__(self):
        self.sequences: dict[str, str] = {}

    async def capture(self, printer: Printer) -> tuple[bytes, float] | None:
        if not printer.camera_url or not printer.camera_association_confirmed:
            return None
        try:
            pinned, host_header, host = await asyncio.to_thread(pin_lan, printer.camera_url)
            async with (
                httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client,
                client.stream("GET", pinned, headers={"Cache-Control": "no-cache", "Host": host_header},
                              extensions={"sni_hostname": host}) as response,
            ):
                response.raise_for_status()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 8 * 1024 * 1024:
                        return None
                # HTTP Date says when the response was served, not when a frame was captured.
                stamp = response.headers.get(printer.frame_time_header) if printer.frame_time_header else None
                sequence = response.headers.get(printer.frame_sequence_header) if printer.frame_sequence_header else None
                if not stamp and not sequence:
                    return None
                captured = float(stamp) if stamp else time.time()
                if captured > time.time() + 2 or captured < time.time() - 10:
                    return None
                if sequence:
                    if self.sequences.get(printer.id) == sequence:
                        return None
                    self.sequences[printer.id] = sequence
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 12_000_000:
                        return None
                    image.verify()
                return bytes(raw), captured
        except (httpx.HTTPError, ValueError, OSError, UnidentifiedImageError, ServiceError):
            return None


def bed_assessment(printer: Printer, frame: bytes, captured_at: float, home: Path) -> dict:
    unknown = {"verdict": "unknown", "reason": "Camera/reference assessment is not qualified", "captured_at": captured_at}
    if not printer.vision_qualified or not printer.baseline_file or not printer.roi:
        return unknown
    baseline_path = (home / printer.baseline_file).resolve()
    if not baseline_path.is_relative_to(home.resolve()) or not baseline_path.is_file():
        return unknown
    try:
        with Image.open(baseline_path) as reference, Image.open(io.BytesIO(frame)) as current:
            if reference.size != current.size or reference.width * reference.height > 12_000_000:
                return unknown
            left, top, right, bottom = printer.roi
            if not (0 <= left < right <= reference.width and 0 <= top < bottom <= reference.height):
                return unknown
            baseline = reference.convert("L").crop(printer.roi)
            image = current.convert("L").crop(printer.roi)
            values = np.asarray(image, dtype=np.float32)
            if values.mean() < 20 or values.mean() > 235 or values.std() < 5:
                return {**unknown, "reason": "Image is dark, overexposed or lacks texture"}
            # Registration/exposure shifts remain unknown; raw similarity cannot establish readiness.
            a = np.asarray(baseline.filter(ImageFilter.GaussianBlur(1)), dtype=np.float32)
            b = np.asarray(image.filter(ImageFilter.GaussianBlur(1)), dtype=np.float32)
            difference = np.abs(a - b)
            changed = float((difference > 25).mean())
            verdict = "occupied" if changed > 0.03 else "unknown"
            reason = "Change detected on bed" if verdict == "occupied" else "Similarity needs a validated clearance classifier"
            return {"verdict": verdict, "reason": reason, "changed_fraction": changed,
                    "captured_at": captured_at, "frame_hash": hashlib.sha256(frame).hexdigest()}
    except (OSError, ValueError, UnidentifiedImageError):
        return unknown


async def failure_assessment(printer: Printer, frame: bytes) -> dict:
    if not printer.failure_detector_qualified or not printer.failure_detector_url:
        return {"verdict": "unknown", "reason": "Failure detector is not qualified"}
    pinned = _pin_loopback(printer.failure_detector_url)
    if not pinned:
        return {"verdict": "unknown", "reason": "Failure analysis must use a loopback local detector"}
    target, host_header = pinned
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            r = await client.post(target, content=frame,
                                  headers={"Content-Type": "image/jpeg", "Host": host_header})
            r.raise_for_status()
            data = r.json()
        if data.get("verdict") not in {"ok", "failed", "unknown"}:
            raise ValueError()
        # Keep only the bounded verdict; no pixel or free-form detector payload leaves this module.
        return {"verdict": data["verdict"]}
    except (httpx.HTTPError, ValueError, AttributeError):
        return {"verdict": "unknown", "reason": "Local detector unavailable"}


async def qualified_bed_assessment(printer: Printer, frame: bytes, captured_at: float, home: Path) -> dict:
    if not printer.vision_qualified or not printer.bed_detector_url:
        return bed_assessment(printer, frame, captured_at, home)
    pinned = _pin_loopback(printer.bed_detector_url)
    baseline = (home / (printer.baseline_file or "")).resolve()
    if (not pinned or not baseline.is_relative_to(home.resolve()) or not baseline.is_file()):
        return {"verdict": "unknown", "reason": "Local clearance detector/reference is unavailable"}
    target, host_header = pinned
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            with baseline.open("rb") as ref:
                r = await client.post(target,
                    files={"frame": ("frame.jpg", frame, "image/jpeg"),
                           "reference": ("reference.jpg", ref, "image/jpeg")},
                    data={"roi": str(printer.roi), "captured_at": str(captured_at)},
                    headers={"Host": host_header})
            r.raise_for_status()
            data = r.json()
        if data.get("verdict") not in {"clear", "occupied", "unknown"}:
            raise ValueError()
        return {"verdict": data["verdict"], "captured_at": captured_at}
    except (httpx.HTTPError, ValueError, AttributeError, OSError):
        return {"verdict": "unknown", "reason": "Local clearance detector unavailable"}
