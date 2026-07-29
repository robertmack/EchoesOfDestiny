from remembering.jsonc import loads_jsonc
from tools.object_definition_editor import (
    append_definition,
    delete_definition,
    object_spans,
    replace_definition,
)


CATALOG = """{
    // This schema comment must survive edits.
    "_defaults": {"kind": "OBJECT"},
    "object_types": [
        {
            "id": "bed",
            "name": "Bed"
        },
        {
            "id": "table",
            "name": "Table"
        }
    ]
}
"""


def test_replace_definition_preserves_surrounding_jsonc_comments() -> None:
    updated = replace_definition(
        CATALOG, "bed", {"id": "bed", "name": "Old Bed", "footprint": [2, 1]}
    )

    assert "// This schema comment must survive edits." in updated
    assert loads_jsonc(updated)["object_types"][0]["name"] == "Old Bed"
    assert [entry[2]["id"] for entry in object_spans(updated)] == ["bed", "table"]


def test_append_and_delete_definition_keep_valid_catalog() -> None:
    updated = append_definition(CATALOG, {"id": "barrel", "name": "Barrel"})
    assert [entry["id"] for entry in loads_jsonc(updated)["object_types"]] == [
        "bed",
        "table",
        "barrel",
    ]

    updated = delete_definition(updated, "table")
    assert [entry["id"] for entry in loads_jsonc(updated)["object_types"]] == [
        "bed",
        "barrel",
    ]
