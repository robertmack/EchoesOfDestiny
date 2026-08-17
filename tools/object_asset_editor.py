from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import deque
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab, ImageTk, PngImagePlugin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from remembering.jsonc import loads_jsonc  # noqa: E402
from remembering.render_metadata import (  # noqa: E402
    AssetMetadataError,
    load_png_render_metadata,
    metadata_as_dict,
)
from remembering.sprites import conventional_sprite_stem  # noqa: E402


OBJECT_CATALOG_PATH = PROJECT_ROOT / "data" / "object_types.jsonc"
TILE_CATALOG_PATH = PROJECT_ROOT / "data" / "tile_types.jsonc"
LIBRESPRITE_EXE = Path(
    os.environ.get(
        "REMEMBERING_LIBRESPRITE_EXE",
        r"C:\Users\robma\dev\libreArt\libresprite.exe",
    )
)
PREVIEW_ZOOM_MIN_PERCENT = 25
PREVIEW_ZOOM_MAX_PERCENT = 3200


@dataclass(frozen=True, slots=True)
class AssetSlot:
    category: str
    object_id: str
    object_name: str
    form_id: str | None
    variant_id: str | None
    state_id: str | None
    asset: str | None
    candidate_assets: tuple[str, ...]
    frame: str
    footprint: tuple[int, int]
    expected_size_px: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        if self.category == "tile":
            asset_status = (
                "" if self.path is not None and self.path.is_file() else "  [missing sprite]"
            )
            return f"Tile / {self.object_name} ({self.object_id}){asset_status}"
        if self.category == "boundary":
            state = f" / {self.state_id}" if self.state_id else ""
            asset_status = (
                "" if self.path is not None and self.path.is_file() else "  [missing sprite]"
            )
            return f"Boundary / {self.object_name}{state}{asset_status}"
        form = f" / {self.form_id}" if self.form_id else ""
        variant = f" / {self.variant_id}" if self.variant_id else ""
        state = f" / {self.state_id}" if self.state_id else ""
        path = self.path
        requested = self.requested_path
        asset_status = "  [missing sprite]"
        if path is not None and path.is_file():
            asset_status = (
                f"  [uses {path.name}]"
                if requested is not None and path != requested
                else ""
            )
        return (
            f"{self.object_name} ({self.object_id}){form}{variant}{state}{asset_status}"
        )

    @property
    def path(self) -> Path | None:
        for asset in self.candidate_assets:
            candidate = (SOURCE_ROOT / asset).resolve()
            if candidate.is_file():
                return candidate
        return self.requested_path

    @property
    def requested_path(self) -> Path | None:
        return (SOURCE_ROOT / self.asset).resolve() if self.asset else None

    @property
    def maximum_size_px(self) -> tuple[int, int]:
        return self.expected_size_px or (
            self.footprint[0] * 64,
            self.footprint[1] * 64,
        )


def merge_definition(
    base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_definition(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_asset_slots(
    path: Path = OBJECT_CATALOG_PATH,
    tile_path: Path = TILE_CATALOG_PATH,
) -> list[AssetSlot]:
    data = loads_jsonc(path.read_text(encoding="utf-8"))
    defaults = dict(data.get("_defaults", {}))
    slots: list[AssetSlot] = []
    for raw_object in data["object_types"]:
        object_id = str(raw_object["id"])
        inherited = merge_definition(
            defaults,
            {key: value for key, value in raw_object.items() if key != "forms"},
        )
        raw_name = inherited.get("name")
        object_name = (
            str(raw_name.get("default", object_id))
            if isinstance(raw_name, dict)
            else str(raw_name or object_id.replace("_", " ").title())
        )
        forms = raw_object.get("forms")
        default_form = next(iter(forms)) if forms else None
        resolved_forms: list[tuple[str | None, dict[str, Any]]]
        if forms:
            resolved_forms = [
                (str(form_id), merge_definition(inherited, form))
                for form_id, form in forms.items()
            ]
        else:
            resolved_forms = [(None, inherited)]
        variants = tuple(str(value) for value in inherited.get("variants", []))
        for form_id, _definition in resolved_forms:
            raw_footprint = _definition.get("footprint", [1, 1])
            footprint = int(raw_footprint[0]), int(raw_footprint[1])
            for variant_id in variants or (None,):
                states = tuple(str(value) for value in _definition.get("states", []))
                for state_id in (None, *states):
                    stems = []
                    for candidate_form, candidate_variant, candidate_state in (
                        (form_id, variant_id, state_id),
                        (form_id, variant_id, None),
                        (form_id, None, None),
                        (None, None, None),
                    ):
                        stem = conventional_sprite_stem(
                            object_id,
                            form=candidate_form,
                            default_form=None,
                            variant=candidate_variant,
                            state=candidate_state,
                        )
                        if stem not in stems:
                            stems.append(stem)
                    slots.append(
                        AssetSlot(
                            category="object",
                            object_id=object_id,
                            object_name=object_name,
                            form_id=form_id,
                            variant_id=variant_id,
                            state_id=state_id,
                            asset=f"assets/sprites/objects/{stems[0]}.png",
                            candidate_assets=tuple(
                                f"assets/sprites/objects/{stem}.png" for stem in stems
                            ),
                            frame="default",
                            footprint=footprint,
                        )
                    )
    tile_data = loads_jsonc(tile_path.read_text(encoding="utf-8"))
    for tile_id in tile_data.get("tile_types", {}):
        asset = f"assets/sprites/tiles/{tile_id}.png"
        slots.append(
            AssetSlot(
                category="tile",
                object_id=str(tile_id),
                object_name=str(tile_id).replace("_", " ").title(),
                form_id=None,
                variant_id=None,
                state_id=None,
                asset=asset,
                candidate_assets=(asset,),
                frame="default",
                footprint=(1, 1),
            )
        )
    for boundary_id, boundary_name, states in (
        ("wall", "Wall", (None,)),
        ("fence", "Fence", (None,)),
        ("door", "Door", ("closed", "open")),
    ):
        for state_id in states:
            suffix = "_open" if state_id == "open" else ""
            asset = f"assets/sprites/boundaries/{boundary_id}{suffix}.png"
            slots.append(
                AssetSlot(
                    category="boundary",
                    object_id=boundary_id,
                    object_name=boundary_name,
                    form_id=None,
                    variant_id=None,
                    state_id=state_id,
                    asset=asset,
                    candidate_assets=(asset,),
                    frame="default",
                    footprint=(1, 1),
                    expected_size_px=(64, 8),
                )
            )
    return slots


def fit_clipboard_image(
    source: Image.Image, canvas_size: tuple[int, int]
) -> Image.Image:
    """Shrink to fit without upscaling, then center on a transparent RGBA canvas."""
    target_width, target_height = canvas_size
    image = source.convert("RGBA")
    if image.width > target_width or image.height > target_height:
        image.thumbnail(canvas_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    canvas.alpha_composite(
        image,
        (
            (target_width - image.width) // 2,
            (target_height - image.height) // 2,
        ),
    )
    return canvas


def scale_image_down(
    source: Image.Image, maximum_size: tuple[int, int]
) -> Image.Image:
    """Return an RGBA image no larger than the footprint, without upscaling."""
    image = source.convert("RGBA")
    if image.width <= maximum_size[0] and image.height <= maximum_size[1]:
        return image.copy()
    image.thumbnail(maximum_size, Image.Resampling.LANCZOS)
    return image


def prefer_horizontal(source: Image.Image) -> Image.Image:
    """Rotate portrait artwork clockwise; landscape and square are horizontal."""
    image = source.convert("RGBA")
    if image.height > image.width:
        return image.transpose(Image.Transpose.ROTATE_270)
    return image.copy()


def clipboard_image() -> Image.Image | None:
    grabbed = ImageGrab.grabclipboard()
    if isinstance(grabbed, Image.Image):
        return grabbed.copy()
    if isinstance(grabbed, list):
        candidate = next(
            (
                Path(item)
                for item in grabbed
                if Path(item).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                and Path(item).is_file()
            ),
            None,
        )
        if candidate is not None:
            with Image.open(candidate) as image:
                return image.copy()
    return None


def clipboard_png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGBA").save(output, format="PNG")
    return output.getvalue()


def clipboard_dib_bytes(image: Image.Image) -> bytes:
    """Return a CF_DIB payload (a BMP without its 14-byte file header)."""
    output = BytesIO()
    image.convert("RGB").save(output, format="BMP")
    return output.getvalue()[14:]


def copy_image_to_clipboard(image: Image.Image) -> None:
    if sys.platform != "win32":
        raise OSError("Image clipboard export is currently supported on Windows.")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    def publish(format_id: int, payload: bytes) -> None:
        handle = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not allocate clipboard data.")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise OSError(ctypes.get_last_error(), "Could not lock clipboard data.")
        ctypes.memmove(pointer, payload, len(payload))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            kernel32.GlobalFree(handle)
            raise OSError(ctypes.get_last_error(), "Could not publish clipboard data.")

    if not user32.OpenClipboard(None):
        raise OSError(ctypes.get_last_error(), "Could not open the Windows clipboard.")
    try:
        if not user32.EmptyClipboard():
            raise OSError(ctypes.get_last_error(), "Could not clear the clipboard.")
        png_format = user32.RegisterClipboardFormatW("PNG")
        publish(png_format, clipboard_png_bytes(image))
        publish(8, clipboard_dib_bytes(image))  # CF_DIB fallback
    finally:
        user32.CloseClipboard()


def open_in_libresprite(path: Path, executable: Path = LIBRESPRITE_EXE) -> None:
    if not executable.is_file():
        raise FileNotFoundError(f"LibreSprite executable was not found: {executable}")
    if not path.is_file():
        raise FileNotFoundError(f"Sprite asset was not found: {path}")
    subprocess.Popen(
        [str(executable), str(path.resolve())],
        cwd=str(executable.parent),
    )


def png_text_chunks(path: Path) -> dict[str, str]:
    with Image.open(path) as image:
        return {
            str(key): value
            for key, value in image.info.items()
            if isinstance(value, str)
        }


def scaled_text_chunks(
    chunks: dict[str, str],
    old_size: tuple[int, int],
    new_size: tuple[int, int],
) -> dict[str, str]:
    """Scale explicit frame rectangles when the PNG canvas changes."""
    raw = chunks.get("remembering.render")
    if raw is None or old_size == new_size:
        return chunks
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return chunks
    render = document.get("render")
    frames = render.get("frames") if isinstance(render, dict) else None
    if isinstance(frames, list):
        scale_x = new_size[0] / old_size[0]
        scale_y = new_size[1] / old_size[1]
        for frame in frames:
            rect = frame.get("rect_px") if isinstance(frame, dict) else None
            if isinstance(rect, list) and len(rect) == 4:
                x, y, width, height = rect
                frame["rect_px"] = [
                    round(int(x) * scale_x),
                    round(int(y) * scale_y),
                    max(1, round(int(width) * scale_x)),
                    max(1, round(int(height) * scale_y)),
                ]
        chunks = dict(chunks)
        chunks["remembering.render"] = json.dumps(
            document, separators=(",", ":")
        )
    return chunks


def rotated_text_chunks(
    chunks: dict[str, str], old_size: tuple[int, int]
) -> dict[str, str]:
    """Rotate explicit frame rectangles and normalized anchors clockwise."""
    raw = chunks.get("remembering.render")
    if raw is None:
        return chunks
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return chunks
    render = document.get("render")
    if not isinstance(render, dict):
        return chunks
    old_width, old_height = old_size
    frames = render.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            rect = frame.get("rect_px") if isinstance(frame, dict) else None
            if isinstance(rect, list) and len(rect) == 4:
                x, y, width, height = (int(value) for value in rect)
                frame["rect_px"] = [
                    old_height - (y + height),
                    x,
                    height,
                    width,
                ]
    anchor = render.get("anchor")
    if (
        isinstance(anchor, dict)
        and anchor.get("mode") == "normalized"
        and isinstance(anchor.get("point"), list)
        and len(anchor["point"]) == 2
    ):
        x, y = (float(value) for value in anchor["point"])
        anchor["point"] = [1.0 - y, x]
    chunks = dict(chunks)
    chunks["remembering.render"] = json.dumps(document, separators=(",", ":"))
    return chunks


def fitted_canvas_text_chunks(
    chunks: dict[str, str],
    scale: float,
    offset: tuple[int, int],
) -> dict[str, str]:
    """Transform explicit frames into a proportionally fitted padded canvas."""
    raw = chunks.get("remembering.render")
    if raw is None:
        return chunks
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return chunks
    render = document.get("render")
    frames = render.get("frames") if isinstance(render, dict) else None
    if isinstance(frames, list):
        offset_x, offset_y = offset
        for frame in frames:
            rect = frame.get("rect_px") if isinstance(frame, dict) else None
            if isinstance(rect, list) and len(rect) == 4:
                x, y, width, height = (int(value) for value in rect)
                frame["rect_px"] = [
                    offset_x + round(x * scale),
                    offset_y + round(y * scale),
                    max(1, round(width * scale)),
                    max(1, round(height * scale)),
                ]
        chunks = dict(chunks)
        chunks["remembering.render"] = json.dumps(
            document, separators=(",", ":")
        )
    return chunks


def save_png_with_text(
    path: Path, image: Image.Image, chunks: dict[str, str]
) -> None:
    png_info = PngImagePlugin.PngInfo()
    for key, value in chunks.items():
        png_info.add_text(key, value)

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".png", dir=path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        image.save(temporary_path, format="PNG", pnginfo=png_info)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def save_render_metadata_text(path: Path, metadata_text: str) -> None:
    """Validate and replace remembering.render without changing image pixels."""
    try:
        document = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError("Metadata must be a JSON object.")

    normalized_text = json.dumps(document, indent=2)
    chunks = png_text_chunks(path)
    chunks["remembering.render"] = normalized_text
    with Image.open(path) as existing:
        image = existing.convert("RGBA")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-metadata-", suffix=".png", dir=path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        save_png_with_text(temporary_path, image, chunks)
        try:
            load_png_render_metadata(temporary_path)
        except AssetMetadataError as exc:
            raise AssetMetadataError(path, exc.field, str(exc).split(": ", 1)[-1]) from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    save_png_with_text(path, image, chunks)


def add_render_metadata_fields(
    metadata_text: str,
    fields: tuple[str, ...],
    image_size: tuple[int, int],
) -> str:
    document = json.loads(metadata_text)
    if not isinstance(document, dict):
        raise ValueError("Metadata must be a JSON object.")
    document.setdefault("_schema_version", 1)
    render = document.setdefault("render", {})
    if not isinstance(render, dict):
        raise ValueError("render must be a JSON object.")
    render.setdefault("projection", "orthographic_top_down")
    if "rotation" in fields:
        render.setdefault(
            "rotation",
            {"mode": "random", "angles": "all"},
        )
    if "random_anchor" in fields:
        render["anchor"] = {"mode": "random_within_tile", "margin": 0.2}
    elif "normalized_anchor" in fields:
        render["anchor"] = {"mode": "normalized", "point": [0.5, 0.5]}
    else:
        render.setdefault("anchor", {"mode": "normalized", "point": [0.5, 0.5]})
    if "frames" in fields:
        render.setdefault(
            "frames",
            [{"id": "default", "rect_px": [0, 0, *image_size]}],
        )
    return json.dumps(document, indent=2)


def replace_png_from_image(
    path: Path,
    source: Image.Image,
    maximum_size: tuple[int, int],
    *,
    exact_canvas: bool = False,
    prefer_horizontal_orientation: bool = False,
) -> None:
    old_size = source.size
    chunks = {
        str(key): value
        for key, value in source.info.items()
        if isinstance(value, str)
    }
    if path.is_file():
        with Image.open(path) as existing:
            old_size = existing.size
        chunks = png_text_chunks(path)
    oriented_source = (
        prefer_horizontal(source) if prefer_horizontal_orientation else source
    )
    replacement = (
        fit_clipboard_image(oriented_source, maximum_size)
        if exact_canvas
        else scale_image_down(oriented_source, maximum_size)
    )
    chunks = scaled_text_chunks(chunks, old_size, replacement.size)
    save_png_with_text(path, replacement, chunks)


def scale_png_to_correct_size(
    path: Path,
    maximum_size: tuple[int, int],
    *,
    exact_canvas: bool = False,
    prefer_horizontal_orientation: bool = False,
) -> bool:
    with Image.open(path) as existing:
        source = existing.copy()
        old_size = existing.size
    oriented_source = (
        prefer_horizontal(source) if prefer_horizontal_orientation else source
    )
    replacement = (
        fit_clipboard_image(oriented_source, maximum_size)
        if exact_canvas
        else scale_image_down(oriented_source, maximum_size)
    )
    if replacement.size == old_size:
        return False
    chunks = scaled_text_chunks(
        png_text_chunks(path), old_size, replacement.size
    )
    save_png_with_text(path, replacement, chunks)
    return True


def resize_png_to_dimensions(path: Path, dimensions: tuple[int, int]) -> bool:
    width, height = dimensions
    if width < 1 or height < 1:
        raise ValueError("Pixel width and height must both be positive integers.")
    with Image.open(path) as existing:
        source = existing.convert("RGBA")
        old_size = existing.size
    if old_size == dimensions:
        return False
    replacement = source.resize(dimensions, Image.Resampling.LANCZOS)
    chunks = scaled_text_chunks(png_text_chunks(path), old_size, dimensions)
    save_png_with_text(path, replacement, chunks)
    return True


def make_background_transparent(path: Path, tolerance: int = 12) -> bool:
    """Clear edge-connected pixels matching the top-left background color."""
    with Image.open(path) as existing:
        image = existing.convert("RGBA")
    pixels = image.load()
    width, height = image.size
    background = pixels[0, 0][:3]

    def matches(x: int, y: int) -> bool:
        color = pixels[x, y]
        return color[3] > 0 and all(
            abs(color[channel] - background[channel]) <= tolerance
            for channel in range(3)
        )

    queue: deque[tuple[int, int]] = deque()
    visited: set[tuple[int, int]] = set()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    changed = False
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited:
            continue
        visited.add((x, y))
        if not matches(x, y):
            continue
        red, green, blue, _alpha = pixels[x, y]
        pixels[x, y] = red, green, blue, 0
        changed = True
        if x > 0:
            queue.append((x - 1, y))
        if x + 1 < width:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y + 1 < height:
            queue.append((x, y + 1))

    if changed:
        save_png_with_text(path, image, png_text_chunks(path))
    return changed


def rotate_png_clockwise(path: Path) -> None:
    with Image.open(path) as existing:
        old_size = existing.size
        rotated = existing.convert("RGBA").transpose(Image.Transpose.ROTATE_270)
    chunks = rotated_text_chunks(png_text_chunks(path), old_size)
    save_png_with_text(path, rotated, chunks)


def increase_png_to_tile_size(
    path: Path, tile_size_px: tuple[int, int]
) -> bool:
    with Image.open(path) as existing:
        source = existing.convert("RGBA")
        old_size = existing.size
    if old_size == tile_size_px:
        return False
    scale = min(
        tile_size_px[0] / old_size[0],
        tile_size_px[1] / old_size[1],
    )
    content_size = (
        max(1, round(old_size[0] * scale)),
        max(1, round(old_size[1] * scale)),
    )
    resized = source.resize(content_size, Image.Resampling.LANCZOS)
    offset = (
        (tile_size_px[0] - content_size[0]) // 2,
        (tile_size_px[1] - content_size[1]) // 2,
    )
    canvas = Image.new("RGBA", tile_size_px, (0, 0, 0, 0))
    canvas.alpha_composite(resized, offset)
    chunks = fitted_canvas_text_chunks(
        png_text_chunks(path), scale, offset
    )
    save_png_with_text(path, canvas, chunks)
    return True


class ObjectAssetEditor:
    PREVIEW_SIZE = (420, 420)

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("Remembering Sprite Asset Editor")
        self.root.geometry("1400x760")
        self.slots = load_asset_slots()
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_source: Image.Image | None = None
        self.undo_stack: list[tuple[Image.Image, str]] = []
        self.asset_dirty = False

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(left, text="Object / Form / Tile / Boundary").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=42, exportselection=False)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for slot in self.slots:
            self.listbox.insert("end", slot.label)
        self.listbox.bind("<<ListboxSelect>>", self.selection_changed)

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.heading = ttk.Label(right, text="Select an object", font=("", 13, "bold"))
        self.heading.grid(row=0, column=0, sticky="w")
        self.expected_size = ttk.Label(right, text="")
        self.expected_size.grid(row=0, column=0, sticky="e")

        dimensions = ttk.Frame(right)
        dimensions.grid(row=2, column=0, sticky="w", pady=(0, 8))
        ttk.Label(dimensions, text="Sprite pixels:").pack(side="left")
        self.pixel_width = tk.StringVar()
        self.pixel_height = tk.StringVar()
        self.pixel_width_entry = ttk.Entry(
            dimensions, textvariable=self.pixel_width, width=6, state="disabled"
        )
        self.pixel_width_entry.pack(side="left", padx=(6, 2))
        ttk.Label(dimensions, text="x").pack(side="left")
        self.pixel_height_entry = ttk.Entry(
            dimensions, textvariable=self.pixel_height, width=6, state="disabled"
        )
        self.pixel_height_entry.pack(side="left", padx=2)
        self.apply_dimensions_button = ttk.Button(
            dimensions,
            text="Apply Pixel Dimensions",
            command=self.apply_pixel_dimensions,
            state="disabled",
        )
        self.apply_dimensions_button.pack(side="left", padx=(6, 0))

        content = ttk.Panedwindow(right, orient="horizontal")
        content.grid(row=1, column=0, sticky="nsew", pady=8)
        preview_panel = ttk.Frame(content)
        metadata_panel = ttk.Frame(content)
        content.add(preview_panel, weight=1)
        content.add(metadata_panel, weight=1)

        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(0, weight=1)
        self.preview = tk.Canvas(
            preview_panel,
            background="#202428",
            highlightthickness=1,
            highlightbackground="#666666",
        )
        preview_x_scroll = ttk.Scrollbar(
            preview_panel, orient="horizontal", command=self.preview.xview
        )
        preview_y_scroll = ttk.Scrollbar(
            preview_panel, orient="vertical", command=self.preview.yview
        )
        self.preview.configure(
            xscrollcommand=preview_x_scroll.set,
            yscrollcommand=preview_y_scroll.set,
        )
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview.bind("<Button-1>", self.begin_pixel_edit)
        self.preview.bind("<B1-Motion>", self.continue_pixel_edit)
        preview_y_scroll.grid(row=0, column=1, sticky="ns")
        preview_x_scroll.grid(row=1, column=0, sticky="ew")
        zoom_controls = ttk.Frame(preview_panel)
        zoom_controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(zoom_controls, text="Zoom").pack(side="left")
        self.preview_zoom = tk.DoubleVar(value=100.0)
        self.zoom_label = tk.StringVar(value="100%")
        ttk.Scale(
            zoom_controls,
            from_=PREVIEW_ZOOM_MIN_PERCENT,
            to=PREVIEW_ZOOM_MAX_PERCENT,
            variable=self.preview_zoom,
            command=self.preview_zoom_changed,
        ).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(zoom_controls, textvariable=self.zoom_label, width=6).pack(
            side="left"
        )

        ttk.Label(metadata_panel, text="remembering.render (editable JSON)").pack(
            anchor="w"
        )
        metadata_buttons = ttk.Frame(metadata_panel)
        metadata_buttons.pack(fill="x", pady=(0, 4))
        for label, fields in (
            ("Rotation", ("rotation",)),
            ("Random Placement", ("random_anchor",)),
            ("Centered Anchor", ("normalized_anchor",)),
            ("Frames", ("frames",)),
            (
                "Add All",
                ("rotation", "random_anchor", "frames"),
            ),
        ):
            ttk.Button(
                metadata_buttons,
                text=label,
                command=lambda selected=fields: self.add_metadata_fields(selected),
            ).pack(side="left", padx=(0, 4))
        self.metadata = tk.Text(metadata_panel, wrap="none", width=48)
        self.metadata.pack(fill="both", expand=True)

        controls = ttk.Frame(right)
        controls.grid(row=3, column=0, sticky="ew")
        self.replace_button = ttk.Button(
            controls,
            text="Replace Image from Clipboard",
            command=self.replace_from_clipboard,
            state="disabled",
        )
        self.replace_button.pack(side="left")
        self.copy_button = ttk.Button(
            controls,
            text="Copy Image to Clipboard",
            command=self.copy_to_clipboard,
            state="disabled",
        )
        self.copy_button.pack(side="left", padx=(8, 0))
        self.libresprite_button = ttk.Button(
            controls,
            text="Open in LibreSprite",
            command=self.open_selected_in_libresprite,
            state="disabled",
        )
        self.libresprite_button.pack(side="left", padx=(8, 0))
        self.reload_image_button = ttk.Button(
            controls,
            text="Reload Image from File",
            command=self.reload_image_from_file,
            state="disabled",
        )
        self.reload_image_button.pack(side="left", padx=(8, 0))
        self.scale_button = ttk.Button(
            controls,
            text="Scale Image to Correct Size",
            command=self.scale_to_correct_size,
            state="disabled",
        )
        self.scale_button.pack(side="left", padx=(8, 0))
        self.rotate_button = ttk.Button(
            controls,
            text="Rotate Sprite 90° Clockwise",
            command=self.rotate_sprite,
            state="disabled",
        )
        self.rotate_button.pack(side="left", padx=(8, 0))
        self.increase_button = ttk.Button(
            controls,
            text="Increase Image to Expected Size",
            command=self.increase_to_tile_size,
            state="disabled",
        )
        self.increase_button.pack(side="left", padx=(8, 0))
        self.transparent_button = ttk.Button(
            controls,
            text="Make Background Transparent",
            command=self.remove_sprite_background,
            state="disabled",
        )
        self.transparent_button.pack(side="left", padx=(8, 0))
        self.save_metadata_button = ttk.Button(
            controls,
            text="Save Asset",
            command=self.save_asset,
            state="disabled",
        )
        self.save_metadata_button.pack(side="left", padx=(8, 0))
        self.create_variant_button = ttk.Button(
            controls,
            text="Create Specific Asset",
            command=self.create_variant_asset,
            state="disabled",
        )
        self.create_variant_button.pack(side="left", padx=(8, 0))
        self.revert_button = ttk.Button(
            controls,
            text="Revert",
            command=self.revert_asset,
            state="disabled",
        )
        self.revert_button.pack(side="left", padx=(8, 0))
        self.eraser_enabled = tk.BooleanVar(value=False)
        self.eraser_button = ttk.Checkbutton(
            controls,
            text="Erase Pixels",
            variable=self.eraser_enabled,
            command=self.enable_eraser,
        )
        self.eraser_button.pack(side="left", padx=(8, 0))
        self.set_pixel_enabled = tk.BooleanVar(value=False)
        self.set_pixel_button = ttk.Checkbutton(
            controls,
            text="Set Pixel",
            variable=self.set_pixel_enabled,
            command=self.enable_set_pixel,
        )
        self.set_pixel_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(
            side="left", padx=8
        )
        self.status = ttk.Label(controls, text="Choose a sprite asset.")
        self.status.pack(side="left", padx=8)

        palette = ttk.Frame(right)
        palette.grid(row=4, column=0, sticky="w", pady=(7, 0))
        ttk.Label(palette, text="Pixel color:").pack(side="left")
        self.selected_pixel_color = (0, 0, 0, 255)
        self.color_swatch = tk.Label(
            palette, background="#000000", width=3, relief="sunken"
        )
        self.color_swatch.pack(side="left", padx=(6, 6))
        for color in (
            "#000000",
            "#ffffff",
            "#7b4f2c",
            "#5c7f45",
            "#3d6f8e",
            "#c6a64b",
            "#9a5549",
            "#808080",
        ):
            tk.Button(
                palette,
                background=color,
                width=2,
                command=lambda value=color: self.select_palette_color(value),
            ).pack(side="left", padx=1)
        ttk.Button(
            palette, text="Select Color...", command=self.select_custom_color
        ).pack(side="left", padx=(7, 0))

        self.root.bind("<Control-z>", self.undo_edit)
        self.root.bind("<Control-Z>", self.undo_edit)
        self.root.bind("<Control-c>", self.copy_shortcut)
        self.root.bind("<Control-C>", self.copy_shortcut)
        self.root.bind("<Control-v>", self.replace_shortcut)
        self.root.bind("<Control-V>", self.replace_shortcut)

        if self.slots:
            self.listbox.selection_set(0)
            self.show_slot(self.slots[0])

    def selected_slot(self) -> AssetSlot | None:
        selection = self.listbox.curselection()
        return self.slots[selection[0]] if selection else None

    def selection_changed(self, _event: object = None) -> None:
        slot = self.selected_slot()
        if slot is not None:
            self.show_slot(slot)

    def set_metadata_text(self, text: str, *, editable: bool = True) -> None:
        self.metadata.configure(state="normal")
        self.metadata.delete("1.0", "end")
        self.metadata.insert("1.0", text)
        self.metadata.configure(state="normal" if editable else "disabled")

    def show_slot(self, slot: AssetSlot) -> None:
        self.undo_stack.clear()
        self.asset_dirty = False
        self.heading.configure(text=slot.label)
        size_prefix = (
            "Canonical boundary canvas"
            if slot.category == "boundary"
            else f"Footprint: {slot.footprint[0]}×{slot.footprint[1]} tiles  ·  Expected canvas"
        )
        self.expected_size.configure(
            text=(
                f"{size_prefix}: {slot.maximum_size_px[0]}×"
                f"{slot.maximum_size_px[1]} px"
            )
        )
        path = slot.path
        if path is None:
            self.set_pixel_dimensions(None)
            self.clear_preview("No sprite reference")
            self.replace_button.configure(state="disabled")
            self.copy_button.configure(state="disabled")
            self.libresprite_button.configure(state="disabled")
            self.reload_image_button.configure(state="disabled")
            self.scale_button.configure(state="disabled")
            self.rotate_button.configure(state="disabled")
            self.increase_button.configure(state="disabled")
            self.transparent_button.configure(state="disabled")
            self.save_metadata_button.configure(state="disabled")
            self.create_variant_button.configure(state="disabled")
            self.revert_button.configure(state="disabled")
            self.set_metadata_text(
                "No convention-based sprite path could be derived.",
                editable=False,
            )
            self.status.configure(text="No sprite reference.")
            return
        if not path.is_file():
            self.set_pixel_dimensions(None)
            self.clear_preview(f"Missing convention asset:\n{path}")
            self.replace_button.configure(state="normal")
            self.copy_button.configure(state="disabled")
            self.libresprite_button.configure(state="disabled")
            self.reload_image_button.configure(state="disabled")
            self.scale_button.configure(state="disabled")
            self.rotate_button.configure(state="disabled")
            self.increase_button.configure(state="disabled")
            self.transparent_button.configure(state="disabled")
            self.save_metadata_button.configure(state="disabled")
            self.create_variant_button.configure(state="disabled")
            self.revert_button.configure(state="disabled")
            self.set_metadata_text(
                "Copy an image and choose Replace Image from Clipboard to create "
                "this convention-based asset.",
                editable=False,
            )
            self.status.configure(
                text=f"Missing asset — new image maximum: "
                f"{slot.maximum_size_px[0]}×{slot.maximum_size_px[1]}"
            )
            return

        with Image.open(path) as image:
            canvas_size = image.size
            self.preview_source = image.convert("RGBA")
        self.set_pixel_dimensions(canvas_size)
        self.render_preview()
        self.replace_button.configure(state="normal")
        self.copy_button.configure(state="normal")
        self.libresprite_button.configure(state="normal")
        self.reload_image_button.configure(state="normal")
        self.scale_button.configure(state="normal")
        self.rotate_button.configure(state="normal")
        self.increase_button.configure(state="normal")
        self.transparent_button.configure(state="normal")
        self.save_metadata_button.configure(state="normal")
        self.create_variant_button.configure(
            state=(
                "normal"
                if any((slot.form_id, slot.variant_id, slot.state_id))
                else "disabled"
            )
        )
        self.revert_button.configure(state="normal")
        try:
            parsed = metadata_as_dict(load_png_render_metadata(path))
            metadata_text = json.dumps(parsed, indent=2)
            self.status.configure(
                text=f"{path.name} — canvas {canvas_size[0]}×{canvas_size[1]}"
            )
        except AssetMetadataError as exc:
            metadata_text = str(exc)
            self.status.configure(text="Metadata error.")
        self.set_metadata_text(metadata_text)

    def clear_preview(self, message: str) -> None:
        self.preview_source = None
        self.preview_photo = None
        self.preview.delete("all")
        self.preview.create_text(
            210, 210, text=message, fill="#dddddd", width=380, justify="center"
        )
        self.preview.configure(scrollregion=(0, 0, 420, 420))

    def preview_zoom_changed(self, _value: str = "") -> None:
        zoom = round(self.preview_zoom.get())
        self.zoom_label.set(f"{zoom}%")
        self.render_preview()

    def render_preview(self) -> None:
        if self.preview_source is None:
            return
        scale = max(0.25, self.preview_zoom.get() / 100.0)
        size = (
            max(1, round(self.preview_source.width * scale)),
            max(1, round(self.preview_source.height * scale)),
        )
        preview = self.preview_source.resize(size, Image.Resampling.NEAREST)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview.delete("all")
        self.preview.create_image(0, 0, image=self.preview_photo, anchor="nw")
        self.preview.configure(scrollregion=(0, 0, size[0], size[1]))

    def edit_snapshot(self) -> tuple[Image.Image, str]:
        assert self.preview_source is not None
        return (
            self.preview_source.copy(),
            self.metadata.get("1.0", "end-1c"),
        )

    def enable_eraser(self) -> None:
        if self.eraser_enabled.get():
            self.set_pixel_enabled.set(False)

    def enable_set_pixel(self) -> None:
        if self.set_pixel_enabled.get():
            self.eraser_enabled.set(False)

    def select_palette_color(self, color: str) -> None:
        red, green, blue = (
            int(color[1:3], 16),
            int(color[3:5], 16),
            int(color[5:7], 16),
        )
        self.selected_pixel_color = red, green, blue, 255
        self.color_swatch.configure(background=color)
        self.set_pixel_enabled.set(True)
        self.eraser_enabled.set(False)

    def select_custom_color(self) -> None:
        from tkinter import colorchooser

        chosen, hexadecimal = colorchooser.askcolor(
            color=self.color_swatch.cget("background"),
            title="Select Pixel Color",
        )
        if chosen is None or hexadecimal is None:
            return
        self.selected_pixel_color = (
            round(chosen[0]),
            round(chosen[1]),
            round(chosen[2]),
            255,
        )
        self.color_swatch.configure(background=hexadecimal)
        self.set_pixel_enabled.set(True)
        self.eraser_enabled.set(False)

    def begin_pixel_edit(self, event: object) -> None:
        if (
            not self.eraser_enabled.get()
            and not self.set_pixel_enabled.get()
        ) or self.preview_source is None:
            return
        self.undo_stack.append(self.edit_snapshot())
        self.edit_pixel_at_event(event)

    def continue_pixel_edit(self, event: object) -> None:
        if (
            self.eraser_enabled.get() or self.set_pixel_enabled.get()
        ) and self.preview_source is not None:
            self.edit_pixel_at_event(event)

    def edit_pixel_at_event(self, event: object) -> None:
        scale = max(0.25, self.preview_zoom.get() / 100.0)
        x = int(self.preview.canvasx(event.x) / scale)
        y = int(self.preview.canvasy(event.y) / scale)
        if (
            self.preview_source is None
            or x < 0
            or y < 0
            or x >= self.preview_source.width
            or y >= self.preview_source.height
        ):
            return
        if self.eraser_enabled.get():
            red, green, blue, _alpha = self.preview_source.getpixel((x, y))
            color = red, green, blue, 0
        else:
            color = self.selected_pixel_color
        self.preview_source.putpixel((x, y), color)
        self.asset_dirty = True
        self.render_preview()
        self.status.configure(text="Unsaved pixel edits.")

    def undo_edit(self, _event: object = None) -> str:
        if not self.undo_stack:
            return "break"
        self.preview_source, metadata_text = self.undo_stack.pop()
        self.set_metadata_text(metadata_text)
        self.asset_dirty = True
        self.render_preview()
        self.status.configure(text="Undid the previous staged edit.")
        return "break"

    def set_pixel_dimensions(
        self, dimensions: tuple[int, int] | None
    ) -> None:
        editable = dimensions is not None
        self.pixel_width.set("" if dimensions is None else str(dimensions[0]))
        self.pixel_height.set("" if dimensions is None else str(dimensions[1]))
        entry_state = "normal" if editable else "disabled"
        self.pixel_width_entry.configure(state=entry_state)
        self.pixel_height_entry.configure(state=entry_state)
        self.apply_dimensions_button.configure(
            state="normal" if editable else "disabled"
        )

    def apply_pixel_dimensions(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        try:
            dimensions = int(self.pixel_width.get()), int(self.pixel_height.get())
            changed = resize_png_to_dimensions(path, dimensions)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Invalid pixel dimensions", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=(
                f"Resized {path.name} to {dimensions[0]}x{dimensions[1]} px. "
                "Press F6 in the game to reload."
                if changed
                else f"{path.name} already has those pixel dimensions."
            )
        )

    def remove_sprite_background(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        try:
            changed = make_background_transparent(path)
        except OSError as exc:
            messagebox.showerror("Background removal failed", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=(
                f"Made the edge-connected background of {path.name} transparent. "
                "Press F6 in the game to reload."
                if changed
                else f"No opaque edge-connected background was found in {path.name}."
            )
        )

    def save_asset(self) -> None:
        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file() or self.preview_source is None:
            return
        if self.write_staged_asset(path):
            self.show_slot(slot)
            self.status.configure(
                text=f"Saved {path.name}. Press F6 in the game to reload."
            )

    def create_variant_asset(self) -> None:
        slot = self.selected_slot()
        target = slot.requested_path if slot is not None else None
        if (
            slot is None
            or not any((slot.form_id, slot.variant_id, slot.state_id))
            or target is None
            or self.preview_source is None
        ):
            return
        if self.write_staged_asset(target):
            self.refresh()
            self.status.configure(
                text=(
                    f"Created specific asset {target.name}. "
                    "Press F6 in the game to reload."
                )
            )

    def write_staged_asset(self, target: Path) -> bool:
        from tkinter import messagebox

        slot = self.selected_slot()
        source_path = slot.path if slot is not None else None
        if self.preview_source is None:
            return False
        metadata_text = self.metadata.get("1.0", "end-1c")
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-save-", suffix=".png", dir=target.parent
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        try:
            document = json.loads(metadata_text)
            chunks = (
                png_text_chunks(source_path)
                if source_path is not None and source_path.is_file()
                else {}
            )
            chunks["remembering.render"] = json.dumps(document, indent=2)
            save_png_with_text(temporary_path, self.preview_source, chunks)
            load_png_render_metadata(temporary_path)
            os.replace(temporary_path, target)
        except (AssetMetadataError, ValueError, OSError) as exc:
            messagebox.showerror("Asset was not saved", str(exc))
            self.status.configure(text="Asset was not saved.")
            return False
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        self.undo_stack.clear()
        self.asset_dirty = False
        return True

    def revert_asset(self) -> None:
        slot = self.selected_slot()
        if slot is None:
            return
        self.show_slot(slot)
        self.status.configure(text="Reverted staged edits to the saved PNG.")

    def add_metadata_fields(self, fields: tuple[str, ...]) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        with Image.open(path) as image:
            image_size = image.size
        try:
            updated = add_render_metadata_fields(
                self.metadata.get("1.0", "end-1c"), fields, image_size
            )
        except (json.JSONDecodeError, ValueError) as exc:
            messagebox.showerror("Invalid metadata", str(exc))
            return
        self.set_metadata_text(updated)
        self.status.configure(text="Metadata fields added; choose Save Metadata to write them.")

    def replace_from_clipboard(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None:
            return
        try:
            source = clipboard_image()
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc))
            return
        if source is None:
            messagebox.showwarning(
                "No image",
                "The clipboard does not contain an image or supported image file.",
            )
            return
        try:
            replace_png_from_image(
                path,
                source,
                slot.maximum_size_px,
                exact_canvas=slot.category == "boundary",
                prefer_horizontal_orientation=slot.category == "boundary",
            )
        except Exception as exc:
            messagebox.showerror("Replacement failed", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=f"Replaced {path.name}. Press F6 in the game to reload."
        )

    def copy_to_clipboard(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        try:
            with Image.open(path) as image:
                source = image.convert("RGBA")
            copy_image_to_clipboard(source)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Clipboard export failed", str(exc))
            return
        self.status.configure(
            text=f"Copied {path.name} to the clipboard with transparency."
        )

    def open_selected_in_libresprite(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        try:
            open_in_libresprite(path)
        except OSError as exc:
            messagebox.showerror("Could not open LibreSprite", str(exc))
            return
        self.status.configure(text=f"Opened {path.name} in LibreSprite.")

    def reload_image_from_file(self) -> None:
        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if slot is None or path is None or not path.is_file():
            return
        self.show_slot(slot)
        self.status.configure(
            text=f"Reloaded {path.name} from disk; staged edits were discarded."
        )

    def copy_shortcut(self, _event: object = None) -> str:
        self.copy_to_clipboard()
        return "break"

    def replace_shortcut(self, _event: object = None) -> str:
        self.replace_from_clipboard()
        return "break"

    def scale_to_correct_size(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if slot is None or path is None or not path.is_file():
            return
        try:
            changed = scale_png_to_correct_size(
                path,
                slot.maximum_size_px,
                exact_canvas=slot.category == "boundary",
                prefer_horizontal_orientation=slot.category == "boundary",
            )
        except Exception as exc:
            messagebox.showerror("Scaling failed", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=(
                f"Scaled {path.name} to fit "
                f"{slot.maximum_size_px[0]}×{slot.maximum_size_px[1]}. "
                "Press F6 in the game to reload."
                if changed
                else f"{path.name} already fits the correct maximum size."
            )
        )

    def rotate_sprite(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if path is None or not path.is_file():
            return
        try:
            rotate_png_clockwise(path)
        except Exception as exc:
            messagebox.showerror("Rotation failed", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=f"Rotated {path.name} clockwise. Press F6 in the game to reload."
        )

    def increase_to_tile_size(self) -> None:
        from tkinter import messagebox

        slot = self.selected_slot()
        path = slot.path if slot is not None else None
        if slot is None or path is None or not path.is_file():
            return
        try:
            changed = increase_png_to_tile_size(path, slot.maximum_size_px)
        except Exception as exc:
            messagebox.showerror("Resize failed", str(exc))
            return
        self.show_slot(slot)
        self.status.configure(
            text=(
                f"Increased {path.name} to "
                f"{slot.maximum_size_px[0]}×{slot.maximum_size_px[1]}. "
                "Press F6 in the game to reload."
                if changed
                else f"{path.name} already matches the expected tile canvas."
            )
        )

    def refresh(self) -> None:
        selected = self.selected_slot()
        self.slots = load_asset_slots()
        self.listbox.delete(0, "end")
        for slot in self.slots:
            self.listbox.insert("end", slot.label)
        if not self.slots:
            return
        selected_key = (
            (
                selected.category,
                selected.object_id,
                selected.form_id,
                selected.variant_id,
                selected.state_id,
            )
            if selected is not None
            else None
        )
        index = next(
            (
                index
                for index, slot in enumerate(self.slots)
                if (
                    slot.category,
                    slot.object_id,
                    slot.form_id,
                    slot.variant_id,
                    slot.state_id,
                )
                == selected_key
            ),
            0,
        )
        self.listbox.selection_set(index)
        self.show_slot(self.slots[index])

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    ObjectAssetEditor().run()


if __name__ == "__main__":
    main()
