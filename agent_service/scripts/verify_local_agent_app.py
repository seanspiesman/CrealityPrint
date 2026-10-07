#!/usr/bin/env python3
"""Verify editable-project export and offline re-slice with a custom Creality app.

This harness only uses a synthetic/imported model, local profile JSON, an isolated
job database, and G-code files. It does not initialize the service, configure a
printer, run detectors, capture a camera, or send output anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from creality_agent.analysis import analyze_model
from creality_agent.config import LocalModel, Settings
from creality_agent.models import ServiceError
from creality_agent.project import export_project
from creality_agent.service import Engine, gcode_estimates
from creality_agent.slicer import slice_model

MAX_TIMEOUT_SECONDS = 300


def _file(value: str, label: str, suffixes: set[str] | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"{label} path must be absolute")
    try:
        if path.is_symlink() or not path.is_file():
            raise argparse.ArgumentTypeError(f"{label} must be an existing regular file")
        resolved = path.resolve(strict=True)
    except OSError:
        raise argparse.ArgumentTypeError(f"{label} is inaccessible") from None
    if suffixes and resolved.suffix.lower() not in suffixes:
        raise argparse.ArgumentTypeError(f"{label} format is unsupported")
    return resolved


def _profile(path: Path, expected_type: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError(f"{expected_type} profile is not readable JSON") from None
    if not isinstance(data, dict) or data.get("type") != expected_type or not isinstance(data.get("name"), str):
        raise ValueError(f"profile must be a flattened {expected_type} JSON preset")
    if data.get("inherits") not in (None, ""):
        raise ValueError(f"profile must be flattened before verification: {expected_type}")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gcode_evidence(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        content = stream.read()
    motion = sum(1 for line in content.splitlines() if re.match(r"^G[0-3]\b", line.strip(), re.IGNORECASE))
    extrusion = sum(1 for line in content.splitlines()
                    if re.match(r"^G[0-3]\b", line.strip(), re.IGNORECASE) and re.search(r"\bE-?\d", line, re.IGNORECASE))
    comment_lines = [line[:180] for line in content.splitlines()
                     if "filament_settings_id" in line or "filament_type" in line]
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "motion_command_count": motion,
        "extrusion_axis_motion_count": extrusion,
        "estimates": gcode_estimates(path),
        "filament_metadata_comments": comment_lines[:8],
    }


def _project_structure(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        model_path = next((item for item in names if item.lower() == "3d/3dmodel.model"), None)
        settings_path = next((item for item in names if item.lower() == "metadata/project_settings.config"), None)
        if not model_path or not settings_path:
            raise ServiceError("Export does not contain a native model and effective project settings")
        from defusedxml import ElementTree

        root = ElementTree.fromstring(archive.read(model_path))
        local = lambda element: element.tag.rsplit("}", 1)[-1]
        return {
            "editable_3mf_zip": True,
            "model_entry": model_path,
            "project_settings_entry": settings_path,
            "object_count": sum(local(element) == "object" for element in root.iter()),
            "build_item_count": sum(local(element) == "item" for element in root.iter()),
        }


async def _verify(args: argparse.Namespace) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise ServiceError("Offline native verification is currently supported on macOS only")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise ServiceError("macOS network-denying process sandbox is unavailable")
    binary = _file(str(args.binary), "Custom app binary")
    if not os.access(binary, os.X_OK):
        raise ServiceError("Custom app binary is not executable")
    model = _file(str(args.model), "Model", {".stl"})
    machine = _file(str(args.machine), "Machine profile", {".json"})
    process = _file(str(args.process), "Process profile", {".json"})
    filament = _file(str(args.filament), "Filament profile", {".json"})
    machine_data = _profile(machine, "machine")
    process_data = _profile(process, "process")
    filament_data = _profile(filament, "filament")
    geometry = analyze_model(model)
    if geometry.get("format") != "stl":
        raise ServiceError("Verifier only accepts STL source geometry")

    requested_output = args.output.expanduser().absolute()
    if requested_output.exists() or requested_output.is_symlink():
        raise ServiceError("Output path must be new; existing files are never removed")
    if not requested_output.parent.is_dir():
        raise ServiceError("Output parent directory must already exist")
    output = requested_output.parent.resolve() / requested_output.name
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    engine_home = output / "isolated-job-data"
    engine_home.mkdir(mode=0o700)
    settings = Settings(slicer_binary=str(binary), gui_helper_binary=str(binary),
                        local_model=LocalModel(base_url="", model="", api_key=None),
                        printers=[], notifications_enabled=False)
    engine = Engine(engine_home, settings)
    job = engine.store.create({"request": {"settings": {}, "copies": 1}})
    profile = SimpleNamespace(settings=[machine, process], filaments=[filament])
    report: dict[str, Any] = {
        "passed": False,
        "network_or_printer_activity": False,
        "physical_control": False,
        "hardware_qualification": False,
        "application_binary": binary.name,
        "source": {"path": str(model), "sha256": _sha256(model), "geometry": geometry},
        "profiles": {
            "machine": {"name": machine_data["name"], "sha256": _sha256(machine)},
            "process": {"name": process_data["name"], "sha256": _sha256(process)},
            "filament": {"name": filament_data["name"], "sha256": _sha256(filament)},
        },
        "limitations": [
            "Synthetic/local artifact verification only; no printer was queried or controlled.",
            "G-code estimates are slicer metadata, not physical-print acceptance.",
        ],
    }
    try:
        project, verification, warnings = await asyncio.wait_for(
            export_project(engine, job["id"], model, profile, output / "project-export"),
            timeout=args.timeout_seconds,
        )
        report["native_project"] = {
            "path": str(project), "size_bytes": project.stat().st_size, "sha256": _sha256(project),
            **_project_structure(project), **verification, "warnings": warnings,
        }
        wrapper = output / "sandboxed-creality-cli"
        wrapper.write_text(
            "#!/bin/sh\nexec /usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' "
            + shlex.quote(str(binary)) + ' "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o700)
        sliced = await slice_model(wrapper, project, output / "gcode", [], [],
                                   args.timeout_seconds, cli_mode=True)
        gcode = [Path(item) for item in sliced["gcode_paths"]]
        artifacts = [_gcode_evidence(item) for item in gcode]
        if not artifacts or any(item["size_bytes"] <= 0 or item["motion_command_count"] == 0
                                or item["extrusion_axis_motion_count"] == 0
                                or not item["estimates"].get("hours") or not item["estimates"].get("grams")
                                for item in artifacts):
            raise ServiceError("Re-sliced G-code is missing motion, extrusion, or usable estimate metadata")
        report["reslice"] = {"elapsed_seconds": sliced["elapsed_seconds"], "warnings": sliced["warnings"],
                             "gcode_artifacts": artifacts}
        if _sha256(model) != report["source"]["sha256"]:
            raise ServiceError("Source model changed during verification")
        if any(_sha256(path) != report["profiles"][key]["sha256"]
               for key, path in (("machine", machine), ("process", process), ("filament", filament))):
            raise ServiceError("Profile inputs changed during verification")
        report["passed"] = True
        return report
    finally:
        engine.store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path, help="Separate custom app executable")
    parser.add_argument("--model", required=True, type=Path, help="Local STL source model")
    parser.add_argument("--machine", required=True, type=Path, help="Flattened local machine profile JSON")
    parser.add_argument("--process", required=True, type=Path, help="Flattened local process profile JSON")
    parser.add_argument("--filament", required=True, type=Path, help="Flattened local filament profile JSON")
    parser.add_argument("--output", required=True, type=Path, help="New output directory; must not already exist")
    parser.add_argument("--timeout-seconds", type=float, default=120,
                        help=f"Per-stage bound in seconds (1..{MAX_TIMEOUT_SECONDS}; default: 120)")
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout-seconds must be in the range 1..{MAX_TIMEOUT_SECONDS}")
    return args


def main() -> int:
    args = _parse_args()
    requested_output = args.output.expanduser().absolute()
    if requested_output.exists() or requested_output.is_symlink():
        print("Output path must be new; existing files are never removed", file=sys.stderr)
        return 2
    report_path = requested_output / "verification.json"
    try:
        report = asyncio.run(_verify(args))
    except (ServiceError, ValueError, OSError, TimeoutError, RuntimeError,
            argparse.ArgumentTypeError, zipfile.BadZipFile) as exc:
        report = {"passed": False, "network_or_printer_activity": False,
                  "physical_control": False, "hardware_qualification": False,
                  "error_type": type(exc).__name__, "error": str(exc)[:240]}
        if report_path.parent.is_dir() and not report_path.parent.is_symlink():
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
