from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

_OSASCRIPT = Path("/usr/bin/osascript")
_MAX_FIELD_LENGTH = 300
_SENSITIVE_CONTENT = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[_ -]?key|access[_ -]?code|password|token|authorization)\s*[:=]\s*\S+|sk-[a-z0-9_-]{16,}|https?://[^\s/@:]+:[^\s/@]+@|data:image/|base64,|<\s*(?:image|frame)\b)"
)


def _apple_script_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(char for char in escaped if char in "\t" or ord(char) >= 32) + '"'


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop and reap osascript after a timeout or caller cancellation."""
    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2)
    except TimeoutError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        await process.wait()


async def notify(title: str, message: str, event_id: str, home: Path) -> bool:
    """Ask macOS Notification Center to display a local, text-only event.

    The caller owns durable event persistence and deduplication keyed by event_id.
    This helper reports whether osascript accepted the display request; it cannot
    prove the user saw it. Keep secrets, printer credentials, and image/frame data
    out of title and message. macOS may require the logged-in user to allow
    notifications from the invoking osascript/host process.
    """
    if sys.platform != "darwin" or not _OSASCRIPT.is_file():
        return False
    if not isinstance(home, Path) or not home.is_dir():
        return False
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
        return False
    if not isinstance(title, str) or not isinstance(message, str):
        return False
    if not title.strip() or not message.strip():
        return False
    if len(title) > _MAX_FIELD_LENGTH or len(message) > _MAX_FIELD_LENGTH:
        return False
    if _SENSITIVE_CONTENT.search(title) or _SENSITIVE_CONTENT.search(message):
        return False

    script = f"display notification {_apple_script_string(message)} with title {_apple_script_string(title)}"
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(_OSASCRIPT),
            "-e",
            script,
            cwd=str(home),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.CancelledError:
        if process is not None:
            await _stop_process(process)
        raise
    except TimeoutError:
        if process is not None:
            await _stop_process(process)
        return False
    except OSError:
        return False
    return process.returncode == 0
