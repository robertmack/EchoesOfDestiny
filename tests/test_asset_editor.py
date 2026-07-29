from __future__ import annotations

import json
from pathlib import Path
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from tools.object_asset_editor import (
    add_render_metadata_fields,
    clipboard_dib_bytes,
    clipboard_png_bytes,
    fit_clipboard_image,
    increase_png_to_tile_size,
    load_asset_slots,
    make_background_transparent,
    replace_png_from_image,
    resize_png_to_dimensions,
    rotate_png_clockwise,
    save_render_metadata_text,
    scale_png_to_correct_size,
)


def test_add_all_known_render_metadata_fields() -> None:
    updated = json.loads(
        add_render_metadata_fields("{}", ("rotation", "random_anchor", "frames"), (8, 6))
    )

    assert updated["render"]["rotation"] == {
        "mode": "random",
        "angles": "all",
    }
    assert updated["render"]["anchor"] == {
        "mode": "random_within_tile",
        "margin": 0.2,
    }
    assert updated["render"]["frames"] == [
        {"id": "default", "rect_px": [0, 0, 8, 6]}
    ]


def test_clipboard_encodings_include_png_alpha_and_dib_fallback() -> None:
    source = Image.new("RGBA", (3, 2), (10, 20, 30, 64))

    png = clipboard_png_bytes(source)
    dib = clipboard_dib_bytes(source)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert not dib.startswith(b"BM")
    with Image.open(BytesIO(png)) as decoded:
        assert decoded.size == (3, 2)
        assert decoded.getpixel((0, 0)) == (10, 20, 30, 64)


def test_asset_inventory_contains_objects_and_forms() -> None:
    slots = load_asset_slots()
    bed = next(slot for slot in slots if slot.object_id == "bed")

    assert any(slot.object_id == "bed" and slot.form_id is None for slot in slots)
    assert bed.footprint == (2, 1)
    assert bed.maximum_size_px == (128, 64)
    assert any(
        slot.object_id == "tree"
        and slot.form_id == "standing"
        and slot.variant_id is None
        and slot.asset == "assets/sprites/objects/tree_standing.png"
        and slot.path is not None
        and slot.path.name == "tree.png"
        for slot in slots
    )
    assert any(
        slot.object_id == "tree"
        and slot.form_id == "stump"
        and slot.variant_id is None
        and slot.asset == "assets/sprites/objects/tree_stump.png"
        for slot in slots
    )
    assert any(
        slot.object_id == "bed"
        and slot.asset == "assets/sprites/objects/bed.png"
        for slot in slots
    )
    empty_barrel = next(
        slot
        for slot in slots
        if slot.object_id == "barrel" and slot.state_id == "empty"
    )
    assert empty_barrel.asset == "assets/sprites/objects/barrel_empty.png"
    assert empty_barrel.path is not None
    assert empty_barrel.path.name in {"barrel_empty.png", "barrel.png"}
    assert any(
        slot.object_id == "bush"
        and slot.variant_id == "berry"
        and slot.asset == "assets/sprites/objects/bush_berry.png"
        for slot in slots
    )
    wheat_seed = next(
        slot
        for slot in slots
        if slot.object_id == "crop"
        and slot.form_id == "seed"
        and slot.variant_id == "wheat"
    )
    assert wheat_seed.asset == "assets/sprites/objects/crop_seed_wheat.png"
    assert wheat_seed.path is not None
    assert wheat_seed.path.name == "crop_seed.png"
    wild_wheat = next(
        slot
        for slot in slots
        if slot.object_id == "wild_plant" and slot.variant_id == "wheat"
    )
    assert wild_wheat.asset == "assets/sprites/objects/wild_plant_wheat.png"
    assert wild_wheat.path is not None
    assert wild_wheat.path.name == "wild_plant_wheat.png"
    grassland = next(
        slot
        for slot in slots
        if slot.category == "tile" and slot.object_id == "grassland"
    )
    assert grassland.asset == "assets/sprites/tiles/grassland.png"
    assert grassland.maximum_size_px == (64, 64)


def test_clipboard_image_shrinks_without_upscaling_and_preserves_alpha() -> None:
    large = Image.new("RGBA", (40, 20), (20, 40, 60, 128))
    fitted_large = fit_clipboard_image(large, (16, 16))
    small = Image.new("RGBA", (4, 4), (100, 120, 140, 64))
    fitted_small = fit_clipboard_image(small, (16, 16))

    assert fitted_large.size == (16, 16)
    assert fitted_large.getbbox() == (0, 4, 16, 12)
    assert fitted_large.getpixel((8, 8))[3] == 128
    assert fitted_small.getbbox() == (6, 6, 10, 10)
    assert fitted_small.getpixel((8, 8))[3] == 64
    assert fitted_small.getpixel((0, 0))[3] == 0


def test_replacement_fits_footprint_and_keeps_png_text_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "asset.png"
    document = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "anchor": {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("remembering.render", json.dumps(document))
    png_info.add_text("artist.note", "preserve me")
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(path, pnginfo=png_info)

    replace_png_from_image(
        path, Image.new("RGBA", (16, 8), (0, 255, 0, 96)), (8, 8)
    )

    with Image.open(path) as replaced:
        assert replaced.size == (8, 4)
        assert replaced.mode == "RGBA"
        assert replaced.info["remembering.render"] == json.dumps(document)
        assert replaced.info["artist.note"] == "preserve me"
        assert replaced.getpixel((4, 2))[3] == 96


def test_replacement_creates_a_missing_conventional_asset(tmp_path: Path) -> None:
    path = tmp_path / "new_object.png"
    source = Image.new("RGBA", (80, 40), (20, 40, 60, 128))

    replace_png_from_image(path, source, (32, 32))

    with Image.open(path) as created:
        assert created.size == (32, 16)
        assert created.mode == "RGBA"
        assert created.getpixel((16, 8))[3] == 128


def test_correct_scale_updates_canvas_and_explicit_frame_rectangles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oversized.png"
    document = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "frames": [
                {"id": "default", "rect_px": [0, 0, 80, 40]},
                {"id": "right", "rect_px": [40, 0, 40, 40]},
            ],
            "anchor": {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("remembering.render", json.dumps(document))
    Image.new("RGBA", (80, 40), (255, 255, 255, 255)).save(
        path, pnginfo=info
    )

    assert scale_png_to_correct_size(path, (32, 32)) is True

    with Image.open(path) as scaled:
        parsed = json.loads(scaled.info["remembering.render"])
        assert scaled.size == (32, 16)
        assert parsed["render"]["frames"] == [
            {"id": "default", "rect_px": [0, 0, 32, 16]},
            {"id": "right", "rect_px": [16, 0, 16, 16]},
        ]
    assert scale_png_to_correct_size(path, (32, 32)) is False


def test_explicit_pixel_dimensions_resize_image_and_frames(tmp_path: Path) -> None:
    path = tmp_path / "dimensions.png"
    document = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "frames": [{"id": "default", "rect_px": [2, 1, 6, 3]}],
            "anchor": {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("remembering.render", json.dumps(document))
    Image.new("RGBA", (10, 5), (10, 20, 30, 128)).save(path, pnginfo=info)

    assert resize_png_to_dimensions(path, (20, 20)) is True

    with Image.open(path) as resized:
        parsed = json.loads(resized.info["remembering.render"])
        assert resized.size == (20, 20)
        assert parsed["render"]["frames"] == [
            {"id": "default", "rect_px": [4, 4, 12, 12]}
        ]
    assert resize_png_to_dimensions(path, (20, 20)) is False

    with pytest.raises(ValueError, match="positive integers"):
        resize_png_to_dimensions(path, (0, 20))


def test_make_background_transparent_only_clears_connected_background(
    tmp_path: Path,
) -> None:
    path = tmp_path / "white-background.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("artist.note", "preserve")
    image = Image.new("RGBA", (7, 7), (255, 255, 255, 255))
    for x in range(1, 6):
        for y in range(1, 6):
            image.putpixel((x, y), (20, 40, 60, 255))
    image.putpixel((3, 3), (255, 255, 255, 255))
    image.save(path, pnginfo=info)

    assert make_background_transparent(path) is True

    with Image.open(path) as changed:
        assert changed.getpixel((0, 0))[3] == 0
        assert changed.getpixel((3, 3)) == (255, 255, 255, 255)
        assert changed.info["artist.note"] == "preserve"
    assert make_background_transparent(path) is False


def test_increase_to_tile_size_centers_art_and_updates_frames(
    tmp_path: Path,
) -> None:
    path = tmp_path / "small.png"
    document = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "frames": [{"id": "default", "rect_px": [0, 0, 8, 4]}],
            "anchor": {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("remembering.render", json.dumps(document))
    Image.new("RGBA", (8, 4), (20, 40, 60, 96)).save(path, pnginfo=info)

    assert increase_png_to_tile_size(path, (32, 32)) is True

    with Image.open(path) as increased:
        parsed = json.loads(increased.info["remembering.render"])
        assert increased.size == (32, 32)
        assert increased.getpixel((0, 0))[3] == 0
        assert increased.getpixel((16, 16))[3] == 96
        assert parsed["render"]["frames"] == [
            {"id": "default", "rect_px": [0, 8, 32, 16]}
        ]
    assert increase_png_to_tile_size(path, (32, 32)) is False


def test_save_render_metadata_validates_before_replacing_chunk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "editable.png"
    original = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "anchor": {"mode": "normalized", "point": [0.5, 0.5]},
        },
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("remembering.render", json.dumps(original))
    info.add_text("artist.note", "keep")
    Image.new("RGBA", (16, 12), (1, 2, 3, 128)).save(path, pnginfo=info)
    changed = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "anchor": {"mode": "random_within_tile", "margin": 0.1},
        },
    }

    save_render_metadata_text(path, json.dumps(changed))

    with Image.open(path) as saved:
        assert json.loads(saved.info["remembering.render"]) == changed
        assert saved.info["artist.note"] == "keep"
        assert saved.getpixel((0, 0)) == (1, 2, 3, 128)

    invalid = json.dumps(
        {
            "_schema_version": 1,
            "render": {
                "projection": "orthographic_top_down",
                "anchor": {"mode": "normalized", "point": [2, 0.5]},
            },
        }
    )
    with pytest.raises(ValueError, match="render.anchor.point"):
        save_render_metadata_text(path, invalid)
    with Image.open(path) as unchanged:
        assert json.loads(unchanged.info["remembering.render"]) == changed


def test_rotate_sprite_updates_canvas_frames_anchor_and_alpha(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oriented.png"
    document = {
        "_schema_version": 1,
        "render": {
            "projection": "orthographic_top_down",
            "frames": [
                {"id": "default", "rect_px": [1, 2, 3, 4]},
            ],
            "anchor": {"mode": "normalized", "point": [0.25, 0.75]},
        },
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("remembering.render", json.dumps(document))
    source = Image.new("RGBA", (8, 10), (0, 0, 0, 0))
    source.putpixel((1, 2), (20, 40, 60, 96))
    source.save(path, pnginfo=info)

    rotate_png_clockwise(path)

    with Image.open(path) as rotated:
        parsed = json.loads(rotated.info["remembering.render"])
        assert rotated.size == (10, 8)
        assert rotated.getpixel((7, 1)) == (20, 40, 60, 96)
        assert parsed["render"]["frames"] == [
            {"id": "default", "rect_px": [4, 1, 4, 3]}
        ]
        assert parsed["render"]["anchor"]["point"] == [0.25, 0.25]
