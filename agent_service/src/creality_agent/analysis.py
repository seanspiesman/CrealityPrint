"""Bounded geometry metadata extraction; this is not a mesh quality check."""

from __future__ import annotations

import io
import math
import re
import struct
import zipfile
import zlib
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException


class ModelError(ValueError):
    """Sanitized error for unsupported or malformed model input."""


_MAX_VERTICES = 5_000_000
_MAX_XML_ENTRY_BYTES = 32 * 1024 * 1024
_MAX_3MF_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_FLOAT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_3MF_UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}


def _read_model(path: Path) -> tuple[Path, bytes]:
    source = Path(path).expanduser().absolute()
    try:
        if not source.is_file():
            raise ModelError("Model file is missing or inaccessible")
        data = source.read_bytes()
    except ModelError:
        raise
    except OSError:
        raise ModelError("Model file is missing or inaccessible") from None
    return source, data


def _bounds(vertices: list[tuple[float, float, float]]) -> tuple[list[float], list[float], list[float]]:
    mins = [min(point[axis] for point in vertices) for axis in range(3)]
    maxs = [max(point[axis] for point in vertices) for axis in range(3)]
    return mins, maxs, [maxs[i] - mins[i] for i in range(3)]


def _finite_vertex(values: list[float]) -> tuple[float, float, float]:
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ModelError("Model contains malformed or non-finite coordinates")
    return values[0], values[1], values[2]


def _parse_binary_stl(data: bytes) -> dict | None:
    if len(data) < 84:
        return None
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + triangle_count * 50
    if expected != len(data):
        return None
    if triangle_count > _MAX_VERTICES // 3:
        raise ModelError("STL exceeds the supported vertex limit")
    vertices = []
    offset = 84
    for _ in range(triangle_count):
        # Each facet has a normal, three vertices, and a two-byte attribute word.
        offset += 12
        for _ in range(3):
            vertex = struct.unpack_from("<3f", data, offset)
            vertices.append(_finite_vertex(list(vertex)))
            offset += 12
        offset += 2
    if not vertices:
        raise ModelError("STL contains no vertices")
    mins, maxs, dimensions = _bounds(vertices)
    return {
        "format": "stl",
        "dimensions_mm": dimensions,
        "coordinate_units": "assumed_mm (STL does not declare units)",
        "raw_bounds": {"min": mins, "max": maxs},
        "triangle_count": triangle_count,
        "warnings": ["STL units are not encoded; dimensions assume coordinates are millimeters."],
    }


def _parse_ascii_stl(data: bytes) -> dict:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ModelError("STL is malformed or truncated") from None
    vertices = []
    facets = 0
    for line in text.splitlines():
        fields = line.strip().split()
        if not fields:
            continue
        tag = fields[0].lower()
        if tag == "facet":
            facets += 1
        elif tag == "vertex":
            if len(fields) != 4 or len(vertices) >= _MAX_VERTICES:
                raise ModelError("STL contains malformed coordinates or exceeds the vertex limit")
            if not all(_FLOAT.fullmatch(value) for value in fields[1:]):
                raise ModelError("Model contains malformed or non-finite coordinates")
            vertices.append(_finite_vertex([float(value) for value in fields[1:]]))
    if not vertices or len(vertices) % 3 or facets * 3 != len(vertices):
        raise ModelError("STL is malformed or truncated")
    mins, maxs, dimensions = _bounds(vertices)
    return {
        "format": "stl",
        "dimensions_mm": dimensions,
        "coordinate_units": "assumed_mm (STL does not declare units)",
        "raw_bounds": {"min": mins, "max": maxs},
        "triangle_count": len(vertices) // 3,
        "warnings": ["STL units are not encoded; dimensions assume coordinates are millimeters."],
    }


def _analyze_stl(data: bytes) -> dict:
    # Binary STL permits arbitrary header text, including "solid". Exact layout
    # recognition takes precedence; ASCII parsing handles nonmatching lengths.
    binary = _parse_binary_stl(data)
    if binary is not None:
        return binary
    return _parse_ascii_stl(data)


def _analyze_obj(data: bytes) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ModelError("OBJ is malformed or inaccessible") from None
    vertices = []
    for line in text.splitlines():
        fields = line.strip().split()
        if not fields or fields[0] != "v":
            continue
        if len(fields) < 4 or len(vertices) >= _MAX_VERTICES:
            raise ModelError("OBJ contains malformed coordinates or exceeds the vertex limit")
        coordinate_tokens = fields[1:4]
        if not all(_FLOAT.fullmatch(value) for value in coordinate_tokens):
            raise ModelError("Model contains malformed or non-finite coordinates")
        vertices.append(_finite_vertex([float(value) for value in coordinate_tokens]))
    if not vertices:
        raise ModelError("OBJ contains no vertices")
    mins, maxs, dimensions = _bounds(vertices)
    return {
        "format": "obj",
        "dimensions_mm": dimensions,
        "coordinate_units": "assumed_mm (OBJ does not declare units)",
        "raw_bounds": {"min": mins, "max": maxs},
        "vertex_count": len(vertices),
        "warnings": [
            "OBJ units are not encoded; dimensions assume coordinates are millimeters.",
            "Only vertex positions were inspected; assembly, faces, and other features are not qualified.",
        ],
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _analyze_3mf(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            total_size = sum(info.file_size for info in infos)
            if total_size > _MAX_3MF_UNCOMPRESSED_BYTES:
                raise ModelError("3MF archive exceeds the supported inspection size")
            xml_infos = [
                info
                for info in infos
                if info.filename.lower().endswith((".xml", ".model", ".rels"))
            ]
            if any(info.file_size > _MAX_XML_ENTRY_BYTES for info in xml_infos):
                raise ModelError("3MF XML entry exceeds the supported inspection size")
            model_infos = [info for info in infos if info.filename.lower().endswith(".model")]
            if not model_infos:
                raise ModelError("3MF has no inspectable model entry or its XML is too large")
            root = ElementTree.fromstring(archive.read(model_infos[0]))
    except ModelError:
        raise
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError, zlib.error, ElementTree.ParseError, DefusedXmlException):
        raise ModelError("3MF archive or model XML is malformed or unsafe") from None

    declared_unit = root.attrib.get("unit", "millimeter").lower()
    if declared_unit not in _3MF_UNIT_TO_MM:
        raise ModelError("3MF declares an unsupported coordinate unit")
    objects = sum(1 for element in root.iter() if _local_name(element.tag) == "object")
    build_items = sum(1 for element in root.iter() if _local_name(element.tag) == "item")
    unit_to_mm = _3MF_UNIT_TO_MM[declared_unit]
    return {
        "format": "3mf",
        "dimensions_mm": None,
        "coordinate_units": declared_unit,
        "object_count": objects,
        "build_item_count": build_items,
        "warnings": [
            "3MF dimensions were not computed; project transforms and component assemblies require the native slicer adapter.",
            "Embedded project settings are untrusted metadata and were not evaluated.",
            "Project geometry and fit are not qualified by this metadata inspection.",
        ],
        "unit_scale_to_mm": unit_to_mm,
    }


def analyze_model(path: Path) -> dict:
    """Return bounded geometry metadata without certifying mesh quality or fit."""
    source, data = _read_model(path)
    suffix = source.suffix.lower()
    if suffix == ".stl":
        return _analyze_stl(data)
    if suffix == ".obj":
        return _analyze_obj(data)
    if suffix == ".3mf":
        return _analyze_3mf(data)
    raise ModelError("Unsupported model format; use STL, OBJ, or 3MF")
