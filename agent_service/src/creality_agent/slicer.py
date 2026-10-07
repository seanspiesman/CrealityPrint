"""Safe subprocess adapter for Creality Print's headless slicing CLI."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import time
from pathlib import Path


class SliceError(RuntimeError):
    """A sanitized failure from input validation or the slicer process."""


def _require_file(path: Path, label: str) -> Path:
    path = Path(path).expanduser().absolute()
    try:
        mode = path.stat().st_mode
    except OSError:
        raise SliceError(f"{label} is missing or inaccessible") from None
    if not stat.S_ISREG(mode):
        raise SliceError(f"{label} must be a regular file")
    return path


def _clean_output_dir(directory: Path) -> None:
    """Remove only entries in the initially empty per-job directory."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink(missing_ok=True)
            else:
                shutil.rmtree(entry)
        except OSError:
            # Cleanup is best-effort; never follow a symlink or broaden deletion.
            continue


def _warning_summary(output: str) -> list[str]:
    count = sum(1 for line in output.splitlines() if "warning" in line.lower())
    return [f"Slicer reported {count} warning(s)."] if count else []


async def _stop_process(process: asyncio.subprocess.Process, *, process_group: bool = False) -> None:
    if process_group:
        # The caller starts a private session; terminate wrappers and their helper descendants.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        return
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def slice_model(
    binary: Path,
    model: Path,
    output_dir: Path,
    settings: list[Path],
    filaments: list[Path],
    timeout_seconds: float = 300,
    *, cli_mode: bool = False,
) -> dict:
    """Slice one STL, OBJ, or 3MF with verified inputs and isolated outputs."""
    started = time.monotonic()
    binary = _require_file(Path(binary), "Slicer executable")
    if not os.access(binary, os.X_OK):
        raise SliceError("Slicer executable is not executable")
    model = _require_file(Path(model), "Model")
    suffix = model.suffix.lower()
    if suffix not in {".stl", ".obj", ".3mf"}:
        raise SliceError("Unsupported model format; use STL, OBJ, or 3MF")
    if suffix == ".3mf" and (settings or filaments):
        raise SliceError("External profiles cannot be used with 3MF projects")
    if timeout_seconds <= 0:
        raise SliceError("Timeout must be positive")

    setting_paths = [_require_file(path, "Settings profile") for path in settings]
    filament_paths = [_require_file(path, "Filament profile") for path in filaments]
    output_dir = Path(output_dir).expanduser().absolute()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise SliceError("Output directory must be a real directory")
        if any(output_dir.iterdir()):
            raise SliceError("Output directory must be empty for this job")
    except SliceError:
        raise
    except OSError:
        raise SliceError("Output directory is unavailable") from None

    argv: list[str] = [str(binary), *(["--cli"] if cli_mode else []), "--slice", "0", "--outputdir", str(output_dir)]
    if setting_paths:
        argv.extend(("--load-settings", ";".join(map(str, setting_paths))))
    if filament_paths:
        argv.extend(("--load-filaments", ";".join(map(str, filament_paths))))
    argv.append(str(model))

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            await _stop_process(process)
            await process.communicate()
            _clean_output_dir(output_dir)
            raise SliceError("Slicer timed out") from None
        if process.returncode != 0:
            _clean_output_dir(output_dir)
            raise SliceError(f"Slicer failed with exit code {process.returncode}")
        outputs = sorted(output_dir.glob("*.gcode"))
        valid_outputs = [
            item
            for item in outputs
            if not item.is_symlink() and item.is_file() and item.stat().st_size > 0
        ]
        if not valid_outputs or len(valid_outputs) != len(outputs):
            _clean_output_dir(output_dir)
            raise SliceError("Slicer produced no valid G-code output")
        output = output_bytes.decode("utf-8", errors="replace")
        return {
            "gcode_paths": [str(item.resolve()) for item in valid_outputs],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "warnings": _warning_summary(output),
        }
    except asyncio.CancelledError:
        if process is not None:
            await _stop_process(process)
            await process.communicate()
        _clean_output_dir(output_dir)
        raise
    except SliceError:
        raise
    except (OSError, ValueError):
        _clean_output_dir(output_dir)
        raise SliceError("Unable to run slicer") from None
