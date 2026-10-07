"""Isolated native project export with independent, conservative artifact inspection."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import struct
import sys
import uuid
import zipfile
from pathlib import Path

from defusedxml import ElementTree

from .ingestion import sha256, validate_archive
from .models import ServiceError
from .native_config import MAX_BYTES, compare_settings, merge_expected, normalize_configs, strict_json
from .slicer import _require_file, _stop_process


def profile_digest(paths):
    return hashlib.sha256("".join(sha256(p) for p in paths).encode()).hexdigest()


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _triangles(path: Path):
    data = path.read_bytes()
    if path.suffix.lower() == ".stl":
        if len(data) >= 84 and len(data) == 84 + struct.unpack_from("<I", data, 80)[0] * 50:
            return [tuple(struct.unpack_from("<3f", data, i + j))
                    for i in range(84, len(data), 50) for j in (12, 24, 36)]
        vertices = [tuple(map(float, line.strip().split()[1:])) for line in data.decode("ascii").splitlines()
                    if line.strip().lower().startswith("vertex ")]
        return vertices
    if path.suffix.lower() == ".3mf":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".model")]
            main = next((n for n in names if n.lower() == "3d/3dmodel.model"), names[0] if len(names) == 1 else None)
            if not main:
                raise ServiceError("Project has no unambiguous root model")
            roots, objects = {}, {}
            for name in names:
                root = ElementTree.fromstring(archive.read(name))
                if root.get("unit", "millimeter") != "millimeter":
                    raise ServiceError("Native project units do not match millimeters")
                roots[name] = root
                objects[name] = {e.get("id"): e for e in root.iter() if _tag(e) == "object"}
            items = [e for e in roots[main].iter() if _tag(e) == "item"]
            if len(items) != 1:
                raise ServiceError("One build item is required for this preparation request")

            def translation(element):
                values = list(map(float, element.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0").split()))
                if (len(values) != 12 or values[:9] != [1, 0, 0, 0, 1, 0, 0, 0, 1]
                        or not all(math.isfinite(v) for v in values)):
                    raise ServiceError("Project transform requires additional geometry qualification")
                return values[9:]

            def expand(name, id, offset, ancestors):
                key = (name, id)
                if key in ancestors or len(ancestors) > 32 or name not in objects or id not in objects[name]:
                    raise ServiceError("Project contains an invalid/cyclic component reference")
                node, result = objects[name][id], []
                for mesh in (e for e in node if _tag(e) == "mesh"):
                    vertices = [tuple(float(e.get(axis)) + offset[i] for i, axis in enumerate(("x", "y", "z")))
                                for e in mesh.iter() if _tag(e) == "vertex"]
                    for triangle in (e for e in mesh.iter() if _tag(e) == "triangle"):
                        for axis in ("v1", "v2", "v3"):
                            index = int(triangle.get(axis))
                            if not 0 <= index < len(vertices):
                                raise ServiceError("Project triangle references an invalid vertex")
                            result.append(vertices[index])
                for component in (e for e in node.iter() if _tag(e) == "component"):
                    reference = next((v for k, v in component.attrib.items() if k.rsplit("}", 1)[-1] == "path"), name)
                    reference = reference.lstrip("/")
                    delta = translation(component)
                    result.extend(expand(reference, component.get("objectid"),
                        [offset[i] + delta[i] for i in range(3)], ancestors | {key}))
                    if len(result) > 5_000_000:
                        raise ServiceError("Project assembly exceeds the geometry verification limit")
                return result
            return expand(main, items[0].get("objectid"), translation(items[0]), set())
    raise ServiceError("OBJ geometry round-trip verification is not yet qualified")


def _mesh_signature(vertices):
    if not vertices or len(vertices) % 3 or any(len(v) != 3 or not all(math.isfinite(a) for a in v) for v in vertices):
        raise ServiceError("Project geometry is malformed")
    origin = [min(v[i] for v in vertices) for i in range(3)]
    triangles = [tuple(sorted(tuple(round(v[j] - origin[j], 5) for j in range(3)) for v in vertices[i:i+3]))
                 for i in range(0, len(vertices), 3)]
    return sorted(triangles)


def verify_project(project, source, profiles, policy, normalized=None):
    validate_archive(project, policy)
    if _mesh_signature(_triangles(source)) != _mesh_signature(_triangles(project)):
        raise ServiceError("Exported geometry differs from the requested source; preparation held")
    with zipfile.ZipFile(project) as archive:
        info = archive.getinfo("Metadata/project_settings.config")
        if info.file_size > 8 * 1024 * 1024:
            raise ServiceError("Project settings exceed the verification size limit")
        actual = strict_json(archive.read(info))
    if normalized is not None:
        count = compare_settings(*normalized)
        return {"geometry_verified": True, "settings_verified": True, "settings_count": count,
                "project_sha256": sha256(project), "profile_sha256": profile_digest(profiles)}
    expected = {}
    for path in profiles:
        raw = json.loads(path.read_text())
        # Profile bookkeeping is intentionally not a print setting.
        expected.update({k: v for k, v in raw.items() if k not in
                        {"name", "type", "inherits", "from", "setting_id", "instantiation", "version",
                         "filament_id", "description", "compatible_printers", "compatible_printers_condition"}})
    compared = []
    for key, value in expected.items():
        if key not in actual or actual[key] != value:
            raise ServiceError("Exported settings do not match the verified local profiles: " + key[:80])
        compared.append(key)
    if not compared:
        raise ServiceError("No effective print settings could be verified")
    return {"geometry_verified": True, "settings_verified": True, "settings_count": len(compared),
            "project_sha256": sha256(project), "profile_sha256": profile_digest(profiles)}


async def export_project(engine, id, source, profile, folder):
    binary = engine.settings.gui_helper_binary
    if not binary:
        raise ServiceError("Install/configure the custom app's isolated preparation helper first")
    executable = _require_file(Path(binary), "Native helper")
    if not os.access(executable, os.X_OK):
        raise ServiceError("Native helper is not executable")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    request_id = uuid.uuid4().hex
    project = folder / "project.3mf"
    paths = [Path(p).resolve() for p in [*profile.settings, *profile.filaments]]
    digest = profile_digest(paths)
    expected = merge_expected(await normalize_configs(executable, paths, folder))
    job = engine.store.get(id)
    manifest = {"version": 1, "request_id": request_id, "job_id": id, "model_path": str(source.resolve()),
                "input_sha256": sha256(source), "settings": [str(p) for p in paths[:len(profile.settings)]],
                "filaments": [str(p) for p in paths[len(profile.settings):]],
                "overrides": job["request"].get("settings", {}), "copies": job["request"].get("copies", 1),
                "output_project": str(project)}
    request_file = folder / "request.json"
    request_file.write_text(json.dumps(manifest))
    request_file.chmod(0o600)
    data_dir = folder / "app-data"
    argv = [str(executable), "--datadir", str(data_dir), "--local-agent-prepare", str(request_file)]
    if sys.platform == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            raise ServiceError("Offline native preparation sandbox is unavailable")
        argv = [str(sandbox), "-p", "(version 1)(allow default)(deny network*)", *argv]
    process = await asyncio.create_subprocess_exec(*argv,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        await asyncio.wait_for(process.wait(), 300)
        result_file = folder / "result.json"
        if process.returncode or result_file.is_symlink() or not result_file.is_file() or result_file.stat().st_size > 65536:
            raise ServiceError("Isolated native preparation failed; manual project was not used")
        result = json.loads(result_file.read_text())
        if (result.get("version") != 1 or result.get("request_id") != request_id or result.get("ok") is not True
                or result.get("input_sha256") != manifest["input_sha256"]
                or result.get("settings_sha256") != digest or result.get("project_path") != str(project)
                or project.is_symlink() or not project.is_file()):
            raise ServiceError("Native preparation manifest failed verification")
        if sha256(source) != manifest["input_sha256"] or profile_digest(paths) != digest:
            raise ServiceError("Source or profiles changed during native preparation")
        project_digest = sha256(project)
        validate_archive(project, engine.settings.policy)
        with zipfile.ZipFile(project) as archive:
            info = archive.getinfo("Metadata/project_settings.config")
            if info.file_size > MAX_BYTES:
                raise ServiceError("Project settings exceed the verification size limit")
            settings_bytes = archive.read(info)
        strict_json(settings_bytes)
        actual_file = folder / "exported-settings.json"
        actual_file.write_bytes(settings_bytes)
        actual_file.chmod(0o600)
        actual = (await normalize_configs(executable, [actual_file], folder))[0]
        if (sha256(source) != manifest["input_sha256"] or profile_digest(paths) != digest
                or sha256(project) != project_digest):
            raise ServiceError("Source or profiles changed during project verification")
        verification = await asyncio.to_thread(verify_project, project, source, paths,
                                             engine.settings.policy, (expected, actual))
        return project, verification, result.get("warnings", [])
    except (TimeoutError, asyncio.CancelledError):
        await _stop_process(process, process_group=True)
        raise
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError):
        raise ServiceError("Exported project could not be independently verified") from None
    finally:
        for temporary in folder.glob(".local-agent-project-*.3mf"):
            if temporary.is_file() or temporary.is_symlink():
                temporary.unlink(missing_ok=True)
