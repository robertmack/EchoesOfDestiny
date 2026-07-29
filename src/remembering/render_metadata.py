from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image


METADATA_KEY = "remembering.render"
SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_PROJECTION = "orthographic_top_down"


class AssetMetadataError(ValueError):
    """A PNG contains invalid Remembering rendering metadata."""

    def __init__(self, path: Path, field: str, message: str) -> None:
        self.path = path
        self.field = field
        super().__init__(f"Invalid render metadata in {path} at {field}: {message}")


@dataclass(frozen=True, slots=True)
class RenderFrame:
    frame_id: str
    rect_px: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class RenderAnchor:
    mode: str
    point: tuple[float, float] | None = None
    margin: float | None = None


@dataclass(frozen=True, slots=True)
class PngRenderMetadata:
    schema_version: int
    projection: str
    canvas_size_px: tuple[int, int]
    frames: tuple[RenderFrame, ...]
    anchor: RenderAnchor
    rotation_angles: tuple[int, ...] | str = ()

    def frame(self, frame_id: str = "default") -> RenderFrame:
        match = next(
            (frame for frame in self.frames if frame.frame_id == frame_id), None
        )
        if match is None:
            raise AssetMetadataError(
                Path("<loaded PNG>"),
                f"render.frames[{frame_id!r}]",
                "frame ID does not exist",
            )
        return match


def _error(path: Path, field: str, message: str) -> AssetMetadataError:
    return AssetMetadataError(path, field, message)


def _object(path: Path, value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, field, "must be an object")
    return value


def _default_metadata(image_size: tuple[int, int]) -> PngRenderMetadata:
    width, height = image_size
    return PngRenderMetadata(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        projection=DEFAULT_PROJECTION,
        canvas_size_px=image_size,
        frames=(RenderFrame("default", (0, 0, width, height)),),
        anchor=RenderAnchor("normalized", (0.5, 0.5)),
    )


def load_png_render_metadata(path: str | Path) -> PngRenderMetadata:
    png_path = Path(path)
    try:
        with Image.open(png_path) as image:
            image_size = tuple(int(value) for value in image.size)
            raw_metadata = image.info.get(METADATA_KEY)
    except (OSError, ValueError) as exc:
        raise AssetMetadataError(png_path, "PNG", str(exc)) from exc

    if raw_metadata is None:
        return _default_metadata(image_size)
    if not isinstance(raw_metadata, str):
        raise _error(png_path, METADATA_KEY, "text chunk value must be a string")
    try:
        document = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise _error(
            png_path,
            METADATA_KEY,
            f"malformed JSON at line {exc.lineno}, column {exc.colno}",
        ) from exc

    root = _object(png_path, document, METADATA_KEY)
    version = root.get("_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _error(png_path, "_schema_version", "must be an integer")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise _error(
            png_path,
            "_schema_version",
            f"unsupported version {version}; expected {SUPPORTED_SCHEMA_VERSION}",
        )

    render = _object(png_path, root.get("render"), "render")
    projection = render.get("projection")
    if projection != DEFAULT_PROJECTION:
        raise _error(
            png_path,
            "render.projection",
            f"currently must be {DEFAULT_PROJECTION!r}",
        )
    raw_frames = render.get("frames")
    if raw_frames is None:
        raw_frames = [
            {
                "id": "default",
                "rect_px": [0, 0, image_size[0], image_size[1]],
            }
        ]
    elif not isinstance(raw_frames, list) or not raw_frames:
        raise _error(png_path, "render.frames", "must be a non-empty array")
    frames: list[RenderFrame] = []
    frame_ids: set[str] = set()
    canvas_width, canvas_height = image_size
    for index, raw_frame in enumerate(raw_frames):
        field = f"render.frames[{index}]"
        frame = _object(png_path, raw_frame, field)
        frame_id = frame.get("id")
        if not isinstance(frame_id, str) or not frame_id:
            raise _error(png_path, f"{field}.id", "must be a non-empty string")
        if frame_id in frame_ids:
            raise _error(png_path, f"{field}.id", f"duplicate frame ID {frame_id!r}")
        rect = frame.get("rect_px")
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(
                isinstance(item, bool) or not isinstance(item, int) for item in rect
            )
        ):
            raise _error(
                png_path, f"{field}.rect_px", "must be an array of four integers"
            )
        x, y, width, height = (int(item) for item in rect)
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise _error(
                png_path,
                f"{field}.rect_px",
                "x/y must be non-negative and width/height must be positive",
            )
        if x + width > canvas_width or y + height > canvas_height:
            raise _error(
                png_path, f"{field}.rect_px", "rectangle exceeds the PNG canvas"
            )
        frames.append(RenderFrame(frame_id, (x, y, width, height)))
        frame_ids.add(frame_id)

    anchor = _object(png_path, render.get("anchor"), "render.anchor")
    mode = anchor.get("mode")
    if mode not in {"normalized", "random_within_tile"}:
        raise _error(
            png_path,
            "render.anchor.mode",
            "must be 'normalized' or 'random_within_tile'",
        )
    point: tuple[float, float] | None = None
    margin: float | None = None
    if mode == "normalized":
        raw_point = anchor.get("point")
        if (
            not isinstance(raw_point, list)
            or len(raw_point) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in raw_point
            )
        ):
            raise _error(
                png_path, "render.anchor.point", "must be an array of two numbers"
            )
        point = float(raw_point[0]), float(raw_point[1])
        if any(value < 0.0 or value > 1.0 for value in point):
            raise _error(
                png_path,
                "render.anchor.point",
                "normalized values must be from 0.0 to 1.0",
            )
    else:
        raw_margin = anchor.get("margin", 0.2)
        if isinstance(raw_margin, bool) or not isinstance(raw_margin, (int, float)):
            raise _error(
                png_path, "render.anchor.margin", "must be a number"
            )
        margin = float(raw_margin)
        if not 0.0 <= margin < 0.5:
            raise _error(
                png_path,
                "render.anchor.margin",
                "must be at least 0.0 and less than 0.5",
            )

    rotation_angles: tuple[int, ...] | str = ()
    raw_rotation = render.get("rotation")
    if raw_rotation is not None:
        rotation = _object(png_path, raw_rotation, "render.rotation")
        if rotation.get("mode") != "random":
            raise _error(png_path, "render.rotation.mode", "must be 'random'")
        raw_angles = rotation.get("angles")
        if raw_angles == "all":
            rotation_angles = "all"
        elif (
            not isinstance(raw_angles, list)
            or not raw_angles
            or any(
                isinstance(angle, bool)
                or not isinstance(angle, int)
                or angle not in {0, 90, 180, 270}
                for angle in raw_angles
            )
        ):
            raise _error(
                png_path,
                "render.rotation.angles",
                "must be 'all' or a non-empty array containing 0, 90, 180, or 270",
            )
        else:
            rotation_angles = tuple(dict.fromkeys(raw_angles))

    return PngRenderMetadata(
        schema_version=version,
        projection=projection,
        canvas_size_px=image_size,
        frames=tuple(frames),
        anchor=RenderAnchor(mode, point, margin),
        rotation_angles=rotation_angles,
    )


def metadata_as_dict(metadata: PngRenderMetadata) -> dict[str, object]:
    data = asdict(metadata)
    data["_schema_version"] = data.pop("schema_version")
    anchor = data.pop("anchor")
    clean_anchor = {"mode": anchor["mode"]}
    if anchor["mode"] == "normalized":
        clean_anchor["point"] = anchor["point"]
    else:
        clean_anchor["margin"] = anchor["margin"]
    data["render"] = {
        "projection": data.pop("projection"),
        "frames": [
            {"id": frame["frame_id"], "rect_px": frame["rect_px"]}
            for frame in data.pop("frames")
        ],
        "anchor": clean_anchor,
    }
    rotation_angles = data.pop("rotation_angles")
    if rotation_angles:
        data["render"]["rotation"] = {
            "mode": "random",
            "angles": rotation_angles,
        }
    data.pop("canvas_size_px")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Print a PNG's parsed {METADATA_KEY} rendering metadata."
    )
    parser.add_argument("png", type=Path)
    args = parser.parse_args()
    print(json.dumps(metadata_as_dict(load_png_render_metadata(args.png)), indent=2))


if __name__ == "__main__":
    main()
