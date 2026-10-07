"""Typed native syntax normalization; Python owns effective-setting comparison."""
from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import uuid
from pathlib import Path

from .ingestion import sha256
from .models import ServiceError
from .slicer import _stop_process

MAX_BYTES = 8 * 1024 * 1024
METADATA = frozenset({"name", "type", "inherits", "from", "setting_id", "instantiation", "version",
    "filament_id", "description", "compatible_printers", "compatible_printers_condition",
    "printer_settings_id", "print_settings_id", "filament_settings_id"})
# Native legacy conversion explicitly expands these singleton motion limits to
# Normal/Silent pairs. No other vector may change cardinality.
MOTION_LIMITS = frozenset({"machine_max_acceleration_x", "machine_max_acceleration_y",
    "machine_max_acceleration_z", "machine_max_acceleration_e", "machine_max_acceleration_extruding",
    "machine_max_acceleration_retracting", "machine_max_acceleration_travel",
    "machine_max_speed_x", "machine_max_speed_y", "machine_max_speed_z", "machine_max_speed_e",
    "machine_max_jerk_x", "machine_max_jerk_y", "machine_max_jerk_z", "machine_max_jerk_e"})
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def validate_numeric_raw(raw, native_type):
    # ConfigOptionType is part of this versioned native contract. Some native
    # vector deserializers accept trailing junk or failed numeric extraction.
    # Reject those representations before any GUI preparation can use them.
    scalar_type = native_type & ~0x4000
    if scalar_type not in {1, 2, 4, 5, 6, 7}:
        return
    vector = bool(native_type & 0x4000)
    items = raw if isinstance(raw, list) else [raw]
    if vector:
        items = [part for item in items for part in (item.split(",") if isinstance(item, str) else [item])]
    if isinstance(raw, list) and not vector:
        raise ServiceError("Unexpected numeric setting cardinality")
    pattern = r"[+-]?\d+" if scalar_type == 2 else NUMBER
    if scalar_type in {4, 5}:
        pattern += r"%?"
    elif scalar_type in {6, 7}:
        pattern += (r"(?:\s*[x,]\s*" + NUMBER + r")") * (1 if scalar_type == 6 else 2)
    for item in items:
        text = str(item).strip()
        if vector and text == "nil":
            # Nullable-vector legality is established by the native parser;
            # its documented nil serialization carries no numeric value.
            continue
        if (not isinstance(item, (str, int, float)) or isinstance(item, bool)
                or re.fullmatch(pattern, text) is None):
            raise ServiceError("Malformed numeric print setting; preparation held")
        for number in re.findall(NUMBER, text):
            if not math.isfinite(float(number)):
                raise ServiceError("Non-finite numeric print setting; preparation held")
        if scalar_type == 2 and not -(2**31) <= int(text) < 2**31:
            raise ServiceError("Numeric print setting exceeds the native integer range")


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ServiceError("Duplicate JSON settings key; preparation held")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ServiceError("Non-finite JSON settings value; preparation held")
    try:
        parsed = json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError):
        raise ServiceError("Settings are not valid JSON; preparation held") from None
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            nonfinite(value)
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)
    finite(parsed)
    return parsed


def checked_values(report, path):
    raw = strict_json(path.read_bytes())
    if not isinstance(raw, dict) or not isinstance(report, dict) or report.get("safe") is not True:
        raise ServiceError("Unsupported native settings; preparation held")
    if report.get("sha256") != sha256(path):
        raise ServiceError("Settings changed during normalization")
    values, accounting = report.get("values"), report.get("accounting")
    if not isinstance(values, dict) or not isinstance(accounting, list):
        raise ServiceError("Native settings accounting is malformed")
    seen, effective = set(), set()
    for item in accounting:
        if not isinstance(item, dict):
            raise ServiceError("Native settings accounting is malformed")
        key, classification, canonical = item.get("raw_key"), item.get("classification"), item.get("canonical_keys")
        if not isinstance(key, str) or key not in raw or key in seen or not isinstance(canonical, list):
            raise ServiceError("Native settings accounting is incomplete")
        seen.add(key)
        if classification == "metadata" and key in METADATA and canonical == []:
            continue
        if classification == "obsolete_disabled" and key == "adaptive_layer_height" and canonical == []:
            if raw[key] not in ("0", 0, False):
                raise ServiceError("Obsolete active setting requires review")
            continue
        if classification == "current" and canonical == [key] and key not in METADATA:
            effective.add(key)
        elif (classification == "mapped_current" and key == "wall_infill_order"
                and canonical in (["wall_sequence", "is_infill_first"], ["is_infill_first", "wall_sequence"])):
            effective.update(canonical)
        else:
            raise ServiceError("Unaccounted native settings key: " + key[:80])
    if seen != set(raw) or set(values) - METADATA != effective:
        raise ServiceError("Native settings accounting is incomplete")
    result = {}
    for key in effective:
        value = values.get(key)
        if (not isinstance(value, dict) or type(value.get("type")) is not int
                or not isinstance(value.get("serialized"), str)
                or set(value) - {"type", "serialized", "vector_size"}
                or ("vector_size" in value and (type(value["vector_size"]) is not int or value["vector_size"] < 0))):
            raise ServiceError("Malformed typed native setting: " + key[:80])
        result[key] = value
        if key in raw:
            validate_numeric_raw(raw[key], value["type"])
    return result


async def normalize_configs(executable, paths, folder):
    if not 1 <= len(paths) <= 4:
        raise ServiceError("Native normalization requires one to four settings files")
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ServiceError("Settings file exceeds native normalization limits")
        strict_json(path.read_bytes())
    nonce = uuid.uuid4().hex
    output = folder / ("normalized-" + nonce + ".json")
    request = folder / ("normalize-request-" + nonce + ".json")
    request.write_text(json.dumps({"version": 1, "files": [str(p.resolve()) for p in paths], "output": str(output.resolve())}))
    request.chmod(0o600)
    argv = [str(executable), "--local-agent-normalize", str(request)]
    if sys.platform == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            raise ServiceError("Offline native normalization sandbox is unavailable")
        argv = [str(sandbox), "-p", "(version 1)(allow default)(deny network*)", *argv]
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        await asyncio.wait_for(process.wait(), 30)
        if process.returncode or output.is_symlink() or not output.is_file() or output.stat().st_size > MAX_BYTES:
            raise ServiceError("Native settings normalization failed; preparation held")
        output.chmod(0o600)
        result = strict_json(output.read_bytes())
        if (not isinstance(result, dict) or result.get("version") != 1 or result.get("ok") is not True
                or not isinstance(result.get("files"), list) or len(result["files"]) != len(paths)):
            raise ServiceError("Native settings normalization report is invalid")
        return [checked_values(item, path) for item, path in zip(result["files"], paths, strict=True)]
    except (TimeoutError, asyncio.CancelledError):
        await _stop_process(process, process_group=True)
        raise


def merge_expected(configs):
    expected = {}
    for config in configs:
        for key, value in config.items():
            if key in expected and expected[key] != value:
                raise ServiceError("Local profiles contain conflicting settings: " + key[:80])
            expected[key] = value
    return expected


def compare_settings(expected, actual):
    for key, value in expected.items():
        observed = actual.get(key)
        if observed == value:
            continue
        if (key in MOTION_LIMITS and isinstance(observed, dict) and value.get("vector_size") == 1
                and observed.get("vector_size") == 2 and observed.get("type") == value.get("type")
                and observed.get("serialized") == value.get("serialized", "") + "," + value.get("serialized", "")):
            continue
        raise ServiceError("Exported settings do not match the verified local profiles: " + key[:80])
    if not expected:
        raise ServiceError("No effective print settings could be verified")
    return len(expected)
