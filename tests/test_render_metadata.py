from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from remembering.render_metadata import (
    AssetMetadataError,
    load_png_render_metadata,
)


def render_document(
    *,
    size: tuple[int, int] = (16, 12),
    frames: list[dict[str, object]] | None = None,
    anchor: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "frames": frames
            if frames is not None
            else [{"id": "default", "rect_px": [0, 0, *size]}],
            "anchor": anchor
            if anchor is not None
            else {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }


def write_png(
    path: Path,
    *,
    size: tuple[int, int] = (16, 12),
    metadata: dict[str, object] | str | None = None,
) -> Path:
    png_info = None
    if metadata is not None:
        png_info = PngImagePlugin.PngInfo()
        value = metadata if isinstance(metadata, str) else json.dumps(metadata)
        png_info.add_text("remembering.render", value)
    Image.new("RGBA", size, (255, 0, 0, 255)).save(path, pnginfo=png_info)
    return path


def test_loads_valid_embedded_render_metadata(tmp_path: Path) -> None:
    path = write_png(tmp_path / "valid.png", metadata=render_document())

    metadata = load_png_render_metadata(path)

    assert metadata.schema_version == 1
    assert metadata.projection == "orthographic_top_down"
    assert metadata.canvas_size_px == (16, 12)
    assert metadata.frame().rect_px == (0, 0, 16, 12)
    assert metadata.anchor.mode == "normalized"
    assert metadata.anchor.point == (0.5, 0.5)


def test_png_without_metadata_uses_safe_defaults(tmp_path: Path) -> None:
    path = write_png(tmp_path / "plain.png", size=(23, 17))

    metadata = load_png_render_metadata(path)

    assert metadata.projection == "orthographic_top_down"
    assert metadata.canvas_size_px == (23, 17)
    assert metadata.frames[0].frame_id == "default"
    assert metadata.frames[0].rect_px == (0, 0, 23, 17)
    assert metadata.anchor.point == (0.5, 0.5)


def test_malformed_json_is_not_treated_as_absent(tmp_path: Path) -> None:
    path = write_png(tmp_path / "bad-json.png", metadata="{not json")

    with pytest.raises(AssetMetadataError) as error:
        load_png_render_metadata(path)

    assert str(path) in str(error.value)
    assert "remembering.render" in str(error.value)
    assert "malformed JSON" in str(error.value)


def test_missing_required_render_field_names_the_field(tmp_path: Path) -> None:
    document = render_document()
    del document["render"]["anchor"]  # type: ignore[index]
    path = write_png(tmp_path / "missing-anchor.png", metadata=document)

    with pytest.raises(AssetMetadataError, match=r"render\.anchor"):
        load_png_render_metadata(path)


def test_embedded_metadata_without_frames_uses_full_png_default(
    tmp_path: Path,
) -> None:
    document = render_document(size=(19, 13))
    del document["render"]["frames"]  # type: ignore[index]
    path = write_png(
        tmp_path / "implicit-frame.png", size=(19, 13), metadata=document
    )

    metadata = load_png_render_metadata(path)

    assert metadata.frames == (
        metadata.frame(),
    )
    assert metadata.frame().frame_id == "default"
    assert metadata.frame().rect_px == (0, 0, 19, 13)


@pytest.mark.parametrize(
    "rect",
    (
        [-1, 0, 4, 4],
        [0, 0, 0, 4],
        [14, 0, 4, 4],
        [0, 10, 4, 4],
        [0, 0, 4],
        [0, 0, 4, "4"],
    ),
)
def test_invalid_frame_rectangle_is_rejected(
    tmp_path: Path, rect: list[object]
) -> None:
    document = render_document(
        frames=[{"id": "default", "rect_px": rect}]
    )
    path = write_png(tmp_path / "bad-rect.png", metadata=document)

    with pytest.raises(AssetMetadataError, match=r"render\.frames\[0\]\.rect_px"):
        load_png_render_metadata(path)


@pytest.mark.parametrize(
    "point",
    (
        [-0.01, 0.5],
        [0.5, 1.01],
        [0.5],
        ["0.5", 0.5],
    ),
)
def test_invalid_normalized_anchor_is_rejected(
    tmp_path: Path, point: list[object]
) -> None:
    document = render_document(
        anchor={"mode": "normalized", "point": point}
    )
    path = write_png(tmp_path / "bad-anchor.png", metadata=document)

    with pytest.raises(AssetMetadataError, match=r"render\.anchor\.point"):
        load_png_render_metadata(path)


def test_random_within_tile_anchor_defaults_to_twenty_percent_margin(
    tmp_path: Path,
) -> None:
    document = render_document(
        anchor={"mode": "random_within_tile"}
    )
    path = write_png(tmp_path / "random-anchor.png", metadata=document)

    anchor = load_png_render_metadata(path).anchor

    assert anchor.mode == "random_within_tile"
    assert anchor.point is None
    assert anchor.margin == pytest.approx(0.2)


def test_random_within_tile_anchor_accepts_explicit_margin(tmp_path: Path) -> None:
    document = render_document(
        anchor={"mode": "random_within_tile", "margin": 0.1}
    )
    path = write_png(tmp_path / "random-anchor-margin.png", metadata=document)

    assert load_png_render_metadata(path).anchor.margin == pytest.approx(0.1)


@pytest.mark.parametrize("margin", (-0.01, 0.5, 1.0, "0.2", True))
def test_random_within_tile_anchor_rejects_invalid_margin(
    tmp_path: Path, margin: object
) -> None:
    document = render_document(
        anchor={"mode": "random_within_tile", "margin": margin}
    )
    path = write_png(tmp_path / "bad-random-margin.png", metadata=document)

    with pytest.raises(AssetMetadataError, match=r"render\.anchor\.margin"):
        load_png_render_metadata(path)


def test_multiple_frame_definitions_are_preserved(tmp_path: Path) -> None:
    document = render_document(
        frames=[
            {"id": "default", "rect_px": [0, 0, 8, 12]},
            {"id": "alternate", "rect_px": [8, 0, 8, 12]},
        ]
    )
    path = write_png(tmp_path / "frames.png", metadata=document)

    metadata = load_png_render_metadata(path)

    assert tuple(frame.frame_id for frame in metadata.frames) == (
        "default",
        "alternate",
    )
    assert metadata.frame("alternate").rect_px == (8, 0, 8, 12)


def test_frame_selection_defaults_to_default_id(tmp_path: Path) -> None:
    document = render_document(
        frames=[
            {"id": "alternate", "rect_px": [0, 0, 8, 12]},
            {"id": "default", "rect_px": [8, 0, 8, 12]},
        ]
    )
    path = write_png(tmp_path / "default-frame.png", metadata=document)

    assert load_png_render_metadata(path).frame().rect_px == (8, 0, 8, 12)


def test_random_rotation_angles_are_loaded(tmp_path: Path) -> None:
    document = render_document()
    document["render"]["rotation"] = {  # type: ignore[index]
        "mode": "random",
        "angles": [0, 90, 180, 270],
    }
    path = write_png(tmp_path / "rotation.png", metadata=document)

    assert load_png_render_metadata(path).rotation_angles == (0, 90, 180, 270)


def test_all_rotation_angles_enable_free_rotation(tmp_path: Path) -> None:
    document = render_document()
    document["render"]["rotation"] = {  # type: ignore[index]
        "mode": "random",
        "angles": "all",
    }
    path = write_png(tmp_path / "free-rotation.png", metadata=document)

    assert load_png_render_metadata(path).rotation_angles == "all"
