from __future__ import annotations

import json

import pytest

from creality_agent.ingestion import sha256
from creality_agent.models import ServiceError
from creality_agent.native_config import checked_values, compare_settings, merge_expected, strict_json


def typed(value, size=None):
    result = {"type": 1, "serialized": value}
    if size is not None:
        result["vector_size"] = size
    return result


def test_typed_comparison_preserves_real_values_and_vector_cardinality():
    expected = {"creality_flush_time": typed("119"), "nozzle_temperature": typed("220", 1),
                "sparse_infill_density": typed("30%"), "curr_bed_type": typed("High Temp Plate"),
                "machine_start_gcode": typed("G28\nM104 S220")}
    assert compare_settings(expected, expected.copy()) == 5
    for key, changed in {"creality_flush_time": typed("86"), "nozzle_temperature": typed("220,220", 2),
                         "sparse_infill_density": typed("30"), "curr_bed_type": typed("Cool Plate"),
                         "machine_start_gcode": typed("G28\nM104 S230")}.items():
        with pytest.raises(ServiceError, match=key):
            compare_settings(expected, {**expected, key: changed})
    with pytest.raises(ServiceError, match="creality_flush_time"):
        compare_settings(expected, {})


def test_only_reviewed_motion_singleton_pair_is_equivalent():
    expected = {"machine_max_speed_x": typed("200", 1)}
    assert compare_settings(expected, {"machine_max_speed_x": typed("200,200", 2)}) == 1
    for value in (typed("200,100", 2), typed("200,200,200", 3), {"type": 3, "serialized": "200,200", "vector_size": 2}):
        with pytest.raises(ServiceError):
            compare_settings(expected, {"machine_max_speed_x": value})


def test_profiles_cannot_silently_override_conflicting_intent():
    with pytest.raises(ServiceError, match="conflicting"):
        merge_expected([{"layer_height": typed("0.2")}, {"layer_height": typed("0.3")}])
    assert merge_expected([{"layer_height": typed("0.2")}] * 2) == {"layer_height": typed("0.2")}


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'])
def test_strict_json_rejects_ambiguous_and_nonfinite_values(data):
    with pytest.raises(ServiceError):
        strict_json(data)


def test_raw_key_accounting_detects_drops_and_missing_canonical_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"name": "fixture", "layer_height": "0.2", "bed_type": ["Cool Plate"]}))
    report = {"safe": True, "sha256": sha256(path), "values": {"layer_height": typed("0.2")}, "accounting": [
        {"raw_key": "name", "classification": "metadata", "canonical_keys": []},
        {"raw_key": "layer_height", "classification": "current", "canonical_keys": ["layer_height"]}]}
    with pytest.raises(ServiceError, match="incomplete"):
        checked_values(report, path)
    report["accounting"].append({"raw_key": "bed_type", "classification": "metadata", "canonical_keys": []})
    with pytest.raises(ServiceError, match="bed_type"):
        checked_values(report, path)


def test_reviewed_legacy_accounting_only_accepts_disabled_obsolete_setting(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"adaptive_layer_height":"0","wall_infill_order":"inner wall/outer wall/infill"}')
    report = {"safe": True, "sha256": sha256(path), "values": {"wall_sequence": typed("inner/outer"), "is_infill_first": typed("0")}, "accounting": [
        {"raw_key": "adaptive_layer_height", "classification": "obsolete_disabled", "canonical_keys": []},
        {"raw_key": "wall_infill_order", "classification": "mapped_current", "canonical_keys": ["wall_sequence", "is_infill_first"]}]}
    assert checked_values(report, path) == {"wall_sequence": typed("inner/outer"), "is_infill_first": typed("0")}
    path.write_text('{"adaptive_layer_height":"1","wall_infill_order":"inner wall/outer wall/infill"}')
    report["sha256"] = sha256(path)
    with pytest.raises(ServiceError, match="Obsolete active"):
        checked_values(report, path)


def test_legacy_mapping_cannot_partially_account_for_intent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"wall_infill_order":"inner wall/outer wall/infill"}')
    report = {"safe": True, "sha256": sha256(path), "values": {"wall_sequence": typed("inner/outer")},
              "accounting": [{"raw_key": "wall_infill_order", "classification": "mapped_current",
                              "canonical_keys": ["wall_sequence"]}]}
    with pytest.raises(ServiceError, match="wall_infill_order"):
        checked_values(report, path)


@pytest.mark.parametrize("raw", ["220C", "abc", "1;2", "nan", "1e999", True])
def test_native_numeric_parser_cannot_silently_accept_junk(raw):
    from creality_agent.native_config import validate_numeric_raw
    with pytest.raises(ServiceError):
        validate_numeric_raw([raw], 0x4001)
