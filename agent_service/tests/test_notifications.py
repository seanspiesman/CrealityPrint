from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from creality_agent import notifications


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    osascript = tmp_path / "osascript"
    osascript.touch()
    monkeypatch.setattr(notifications, "_OSASCRIPT", osascript)
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, wait=lambda: asyncio.sleep(0))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return tmp_path, calls


@pytest.mark.asyncio
async def test_notify_uses_osascript_without_shell_or_event_id_in_content(macos):
    home, calls = macos

    delivered = await notifications.notify('Print "paused"', "Failure detected\\check bed", "evt-123", home)

    assert delivered is True
    args, kwargs = calls[0]
    assert args[0] == str(notifications._OSASCRIPT)
    assert args[1] == "-e"
    assert 'display notification "Failure detected\\\\check bed"' in args[2]
    assert 'with title "Print \\"paused\\""' in args[2]
    assert "evt-123" not in args[2]
    assert kwargs["cwd"] == str(home)
    assert "shell" not in kwargs


@pytest.mark.asyncio
async def test_notify_rejects_secrets_or_frame_content_without_delivery(macos):
    home, calls = macos

    assert await notifications.notify("Printer", "token=supersecret", "evt-1", home) is False
    assert await notifications.notify("Printer", "data:image/png;base64,AAAA", "evt-2", home) is False
    assert calls == []


@pytest.mark.asyncio
async def test_notify_returns_false_for_invalid_context_or_nonzero_status(macos, monkeypatch):
    home, calls = macos

    assert await notifications.notify("Printer", "Paused", "", home) is False
    assert await notifications.notify("Printer", "Paused", "evt-1", home / "missing") is False
    assert calls == []

    async def failed_exec(*args, **kwargs):
        async def failed_wait():
            return 1

        return SimpleNamespace(returncode=1, wait=failed_wait)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_exec)
    assert await notifications.notify("Printer", "Paused", "evt-1", home) is False


@pytest.mark.asyncio
async def test_notify_cancellation_terminates_and_reaps_osascript(macos, monkeypatch):
    home, _ = macos
    waiting = asyncio.Event()

    class BlockingProcess:
        returncode = None
        terminated = False
        killed = False

        async def wait(self):
            waiting.set()
            while not self.terminated and not self.killed:
                await asyncio.sleep(0.01)
            self.returncode = -15 if self.terminated else -9
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = BlockingProcess()

    async def blocking_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocking_exec)
    task = asyncio.create_task(notifications.notify("Printer", "Paused", "evt-2", home))
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is False
    assert process.returncode == -15
