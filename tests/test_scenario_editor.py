import pytest

from remembering.jsonc import loads_jsonc
from tools.scenario_editor import (
    flip_door_swing,
    normalize_orientation,
    next_object_id,
    replace_boundaries_array,
    replace_objects_array,
    top_level_array_bounds,
    validate_scenario_objects,
)


SCENARIO = """// scenario docs stay here
{
    "name": "Test",
    "width": 10,
    "height": 8,
    "objects": [
        {"id": 1, "type": "bed", "x": 2, "y": 3, "orientation": "E/W", "quality": 50}
    ],
    // later comments survive too
    "characters": []
}
"""
DEFINITIONS = {"bed": {"footprint": [2, 1]}, "table": {"footprint": [2, 2]}}


def test_replace_objects_preserves_unrelated_jsonc() -> None:
    objects = [{"id": 2, "type": "table", "x": 4, "y": 5, "orientation": "N/S", "quality": 80}]
    updated = replace_objects_array(SCENARIO, objects)

    assert "// scenario docs stay here" in updated
    assert "// later comments survive too" in updated
    assert loads_jsonc(updated)["objects"] == objects
    start, end = top_level_array_bounds(updated, "objects")
    assert updated[start] == "[" and updated[end] == "]"


def test_flip_door_swing_and_boundary_save_round_trip() -> None:
    source = SCENARIO.replace(
        '    "characters": []',
        '    "boundaries": [{"id": "door", "type": "door", "x": 3, "y": 4, "edge": "north"}],\n'
        '    "characters": []',
    )
    door = loads_jsonc(source)["boundaries"][0]

    flip_door_swing(door)
    assert door["swing"] == "clockwise"
    updated = replace_boundaries_array(source, [door])
    assert loads_jsonc(updated)["boundaries"] == [door]

    flip_door_swing(door)
    assert door["swing"] == "counterclockwise"


def test_validate_objects_checks_ids_types_orientation_and_bounds() -> None:
    document = loads_jsonc(SCENARIO)
    validate_scenario_objects(document, DEFINITIONS)

    document["objects"][0]["x"] = 9
    with pytest.raises(ValueError, match="outside"):
        validate_scenario_objects(document, DEFINITIONS)


def test_next_object_id_uses_highest_existing_id() -> None:
    assert next_object_id([{"id": 7}, {"id": 2}]) == 8
    assert next_object_id([]) == 1


def test_orientation_aliases_preserve_legacy_scenarios() -> None:
    assert [normalize_orientation(value) for value in ("E/W", "S", "W", "N/S")] == ["E", "S", "W", "N"]

    document = loads_jsonc(SCENARIO)
    document["objects"][0]["orientation"] = "S"
    validate_scenario_objects(document, DEFINITIONS)
