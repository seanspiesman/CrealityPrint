import asyncio
from pathlib import Path

import pytest

from creality_agent.slicer import SliceError, slice_model


def _fake_cli(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake-slicer"
    executable.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _model(tmp_path: Path, suffix: str = ".stl") -> Path:
    model = tmp_path / f"model{suffix}"
    model.write_text("fixture", encoding="utf-8")
    return model


@pytest.mark.asyncio
async def test_success_returns_absolute_gcode_and_sanitized_warnings(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--outputdir') + 1])\n"
        "(out / 'plate_1.gcode').write_text('; generated')\n"
        "print('WARNING: example warning with /private/path')",
    )
    settings = tmp_path / "settings.json"
    filaments = tmp_path / "filament.json"
    settings.write_text("{}")
    filaments.write_text("{}")
    output_dir = tmp_path / "job"

    result = await slice_model(binary, _model(tmp_path), output_dir, [settings], [filaments])

    assert result["gcode_paths"] == [str((output_dir / "plate_1.gcode").resolve())]
    assert result["elapsed_seconds"] >= 0
    assert result["warnings"] == ["Slicer reported 1 warning(s)."]
    assert "/private/path" not in str(result)


@pytest.mark.asyncio
async def test_nonzero_exit_is_sanitized_and_partial_outputs_are_removed(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--outputdir') + 1])\n"
        "(out / 'partial.gcode').write_text('partial')\n"
        "print('secret details /Users/private')\n"
        "sys.exit(9)",
    )
    output_dir = tmp_path / "job"

    with pytest.raises(SliceError, match="exit code 9") as error:
        await slice_model(binary, _model(tmp_path), output_dir, [], [])

    assert "/Users/private" not in str(error.value)
    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_timeout_stops_process_and_cleans_partial_output(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        "import pathlib, sys, time\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--outputdir') + 1])\n"
        "(out / 'partial.gcode').write_text('partial')\n"
        "time.sleep(10)",
    )
    output_dir = tmp_path / "job"

    with pytest.raises(SliceError, match="timed out"):
        await slice_model(binary, _model(tmp_path), output_dir, [], [], timeout_seconds=0.05)

    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_stops_process_and_cleans_partial_output(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        "import pathlib, sys, time\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--outputdir') + 1])\n"
        "(out / 'partial.gcode').write_text('partial')\n"
        "time.sleep(10)",
    )
    output_dir = tmp_path / "job"
    task = asyncio.create_task(slice_model(binary, _model(tmp_path), output_dir, [], []))
    for _ in range(100):
        if output_dir.exists() and list(output_dir.iterdir()):
            break
        await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_external_profiles_are_rejected_for_3mf(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path, "raise RuntimeError('must not run')")
    profile = tmp_path / "settings.json"
    profile.write_text("{}")

    with pytest.raises(SliceError, match="cannot be used with 3MF"):
        await slice_model(binary, _model(tmp_path, ".3mf"), tmp_path / "job", [profile], [])


@pytest.mark.asyncio
async def test_missing_output_is_error_and_existing_user_files_are_preserved(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path, "print('done')")
    with pytest.raises(SliceError, match="no valid G-code"):
        await slice_model(binary, _model(tmp_path), tmp_path / "job", [], [])

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    user_file = output_dir / "keep.txt"
    user_file.write_text("preserve")
    with pytest.raises(SliceError, match="must be empty"):
        await slice_model(binary, _model(tmp_path), output_dir, [], [])
    assert user_file.read_text() == "preserve"


async def test_wrapped_helper_cancellation_stops_descendants(tmp_path: Path) -> None:
    import sys

    from creality_agent.slicer import _stop_process

    marker = tmp_path / "child-heartbeat"
    child = "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); p=pathlib.Path(" + repr(str(marker)) + ");\nwhile True:\n p.write_text(str(time.time())); time.sleep(.02)"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", parent, start_new_session=True)
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        assert marker.exists()
        await _stop_process(process, process_group=True)
        await asyncio.sleep(.05)
        last = marker.read_text()
        await asyncio.sleep(.1)
        assert marker.read_text() == last
        assert process.returncode is not None
    finally:
        await _stop_process(process, process_group=True)


async def test_custom_app_uses_explicit_headless_mode_for_saved_project(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path,
        "import pathlib,sys\n"
        "assert sys.argv[1] == '--cli'\n"
        "assert '--load-settings' not in sys.argv and '--load-filaments' not in sys.argv\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--outputdir')+1])\n"
        "(out/'plate_1.gcode').write_text('; saved project slice')")
    result = await slice_model(binary, _model(tmp_path, '.3mf'), tmp_path / 'slice', [], [], cli_mode=True)
    assert Path(result['gcode_paths'][0]).is_file()
