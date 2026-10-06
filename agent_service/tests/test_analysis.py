import struct
import zipfile
from pathlib import Path

import pytest

from creality_agent.analysis import ModelError, analyze_model


def _binary_stl(
    path: Path,
    vertices: tuple[tuple[float, float, float], ...],
    normal: tuple[float, float, float] = (0.0, 0.0, 1.0),
    header: bytes = b"fixture",
) -> None:
    payload = bytearray(header.ljust(80, b"\0"))
    payload.extend(struct.pack("<I", len(vertices) // 3))
    for offset in range(0, len(vertices), 3):
        payload.extend(struct.pack("<3f", *normal))
        for vertex in vertices[offset : offset + 3]:
            payload.extend(struct.pack("<3f", *vertex))
        payload.extend(struct.pack("<H", 0))
    path.write_bytes(payload)


def _3mf(path: Path, model_xml: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", model_xml)


def test_binary_stl_reports_millimeter_assumption_triangle_count_and_bounds(tmp_path: Path) -> None:
    source = tmp_path / "triangle.stl"
    _binary_stl(source, ((-2.0, 0.0, 1.0), (3.0, 4.0, 1.0), (0.0, -1.0, 5.0)))

    result = analyze_model(source)

    assert result["format"] == "stl"
    assert result["triangle_count"] == 1
    assert result["dimensions_mm"] == [5.0, 5.0, 4.0]
    assert result["raw_bounds"] == {"min": [-2.0, -1.0, 1.0], "max": [3.0, 4.0, 5.0]}
    assert "assumed_mm" in result["coordinate_units"]
    assert "assume" in result["warnings"][0]


def test_binary_stl_steps_over_normal_and_attribute_for_each_triangle(tmp_path: Path) -> None:
    source = tmp_path / "two-triangles.stl"
    _binary_stl(
        source,
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (10.0, 10.0, 10.0),
            (11.0, 10.0, 10.0),
            (10.0, 11.0, 10.0),
        ),
        normal=(500.0, 600.0, 700.0),
    )

    result = analyze_model(source)

    assert result["triangle_count"] == 2
    assert result["raw_bounds"] == {"min": [0.0, 0.0, 0.0], "max": [11.0, 11.0, 10.0]}


def test_binary_stl_with_solid_header_and_ascii_decodable_floats_is_recognized(tmp_path: Path) -> None:
    source = tmp_path / "solid-header.stl"
    _binary_stl(
        source,
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        normal=(0.0, 0.0, 0.0),
        header=b"solid binary fixture",
    )
    source_bytes = source.read_bytes()
    source_bytes.decode("ascii")

    result = analyze_model(source)

    assert result["triangle_count"] == 1
    assert result["dimensions_mm"] == [0.0, 0.0, 0.0]


def test_long_ascii_stl_uses_text_parser_instead_of_binary_count(tmp_path: Path) -> None:
    source = tmp_path / "ascii.stl"
    source.write_text(
        "solid test\n"
        "facet normal 0 0 1\n"
        "outer loop\n"
        "vertex 0 0 0\n"
        "vertex 10 0 0\n"
        "vertex 0 10 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid test\n",
        encoding="ascii",
    )

    result = analyze_model(source)

    assert result["triangle_count"] == 1
    assert result["dimensions_mm"] == [10.0, 10.0, 0.0]


@pytest.mark.parametrize("bad_vertex", [(float("nan"), 0.0, 0.0), (0.0, float("inf"), 0.0)])
def test_binary_stl_rejects_nonfinite_vertices(tmp_path: Path, bad_vertex) -> None:
    source = tmp_path / "bad.stl"
    _binary_stl(source, (bad_vertex, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))

    with pytest.raises(ModelError, match="non-finite"):
        analyze_model(source)


def test_truncated_binary_stl_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "truncated.stl"
    source.write_bytes(b"binary".ljust(80, b"\0") + struct.pack("<I", 2) + b"short")

    with pytest.raises(ModelError, match="malformed or truncated"):
        analyze_model(source)


def test_obj_reports_negative_bounds_and_only_vertex_metadata(tmp_path: Path) -> None:
    source = tmp_path / "shape.obj"
    source.write_text(
        "# fixture\nv -5 2 0\nv 1 -3 4\nv -2 0 -1\nf 1 2 3\n",
        encoding="utf-8",
    )

    result = analyze_model(source)

    assert result["format"] == "obj"
    assert result["vertex_count"] == 3
    assert result["raw_bounds"] == {"min": [-5.0, -3.0, -1.0], "max": [1.0, 2.0, 4.0]}
    assert result["dimensions_mm"] == [6.0, 5.0, 5.0]
    assert any("only vertex positions" in warning.lower() for warning in result["warnings"])


def test_3mf_reports_declared_units_object_and_build_counts_without_fit_claims(tmp_path: Path) -> None:
    source = tmp_path / "project.3mf"
    _3mf(
        source,
        b"""<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="inch">
          <resources><object id="1" type="model"><mesh/><components/></object>
          <object id="2" type="support"><mesh/></object></resources>
          <build><item objectid="1"/><item objectid="2"/></build></model>""",
    )

    result = analyze_model(source)

    assert result["format"] == "3mf"
    assert result["coordinate_units"] == "inch"
    assert result["unit_scale_to_mm"] == 25.4
    assert result["object_count"] == 2
    assert result["build_item_count"] == 2
    assert result["dimensions_mm"] is None
    assert any("transforms" in warning for warning in result["warnings"])
    assert any("untrusted" in warning for warning in result["warnings"])


def test_3mf_rejects_dtd_xml(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.3mf"
    _3mf(
        source,
        b'<!DOCTYPE model [<!ENTITY x "unsafe">]><model unit="millimeter"><resources/></model>',
    )

    with pytest.raises(ModelError, match="malformed or unsafe"):
        analyze_model(source)
