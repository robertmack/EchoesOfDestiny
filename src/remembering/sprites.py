from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pygame

from remembering.model import ObjectType, SpriteOverlay, WorldObject
from remembering.render_metadata import (
    AssetMetadataError,
    PngRenderMetadata,
    RenderAnchor,
    load_png_render_metadata,
)


DEFAULT_OBJECT_SPRITE_ROOT = (
    Path(__file__).resolve().parents[1] / "assets" / "sprites" / "objects"
)
DEFAULT_TILE_SPRITE_ROOT = (
    Path(__file__).resolve().parents[1] / "assets" / "sprites" / "tiles"
)
DEFAULT_BOUNDARY_SPRITE_ROOT = (
    Path(__file__).resolve().parents[1] / "assets" / "sprites" / "boundaries"
)


def conventional_sprite_stem(
    type_id: str,
    *,
    form: str | None = None,
    default_form: str | None = None,
    variant: str | None = None,
    state: str | None = None,
    flavor: str | None = None,
) -> str:
    parts = [type_id]
    if form and form != default_form:
        parts.append(form)
    if variant:
        parts.append(variant)
    if state:
        parts.append(state)
    stem = "_".join(parts)
    if flavor:
        stem += f"_f-{flavor}"
    return stem


def sprite_stem(
    obj: WorldObject,
    definition: ObjectType,
    *,
    include_form: bool = True,
    include_variant: bool = True,
    include_state: bool = True,
    include_flavor: bool = True,
) -> str:
    raw_state = obj.state.get("sprite_state") if include_state else None
    state = (
        str(raw_state)
        if raw_state and str(raw_state).replace("_", "").isalnum()
        else ""
    )
    return conventional_sprite_stem(
        definition.type_id,
        form=obj.form if include_form else None,
        # A form remains part of its specific asset name. The unmodified object
        # filename is the final generic fallback, not an implicit first form.
        default_form=None,
        variant=obj.variant if include_variant else None,
        state=state or None,
        flavor=obj.flavor if include_flavor else None,
    )


def sprite_candidate_stems(
    obj: WorldObject, definition: ObjectType
) -> tuple[str, ...]:
    candidates = (
        {},
        {"include_flavor": False},
        {"include_flavor": False, "include_state": False},
        {
            "include_flavor": False,
            "include_state": False,
            "include_variant": False,
        },
        {
            "include_flavor": False,
            "include_state": False,
            "include_variant": False,
            "include_form": False,
        },
    )
    stems: list[str] = []
    for options in candidates:
        stem = sprite_stem(obj, definition, **options)
        if stem not in stems:
            stems.append(stem)
    return tuple(stems)


@dataclass(frozen=True, slots=True)
class LoadedSprite:
    image: pygame.Surface
    anchor: RenderAnchor
    projection: str
    asset_path: Path
    frame_id: str
    rotation_angles: tuple[int, ...] | str = ()


class ObjectSpriteCatalog:
    """Resolves conventional filenames, then loads their embedded render data."""

    def __init__(self, root: Path = DEFAULT_OBJECT_SPRITE_ROOT) -> None:
        self.root = root.resolve()
        self._metadata: dict[Path, PngRenderMetadata] = {}
        self._images: dict[Path, pygame.Surface] = {}
        self._frames: dict[tuple[Path, str], LoadedSprite] = {}

    def reload(self) -> None:
        self._metadata.clear()
        self._images.clear()
        self._frames.clear()

    def path_for(
        self, obj: WorldObject, definition: ObjectType
    ) -> Path | None:
        for stem in sprite_candidate_stems(obj, definition):
            candidate = self.root / f"{stem}.png"
            if candidate.is_file():
                return candidate
        return None

    def overlay_path_for(
        self, obj: WorldObject, definition: ObjectType, overlay_id: str
    ) -> Path | None:
        for stem in sprite_candidate_stems(obj, definition):
            candidate = self.root / f"{stem}_overlay-{overlay_id}.png"
            if candidate.is_file():
                return candidate
        return None

    def sprite_for(
        self, obj: WorldObject, definition: ObjectType
    ) -> LoadedSprite | None:
        path = self.path_for(obj, definition)
        if path is None:
            return None
        frame_id = "default"
        cache_key = path, frame_id
        loaded = self._frames.get(cache_key)
        if loaded is not None:
            return loaded

        metadata = self._metadata.get(path)
        if metadata is None:
            metadata = load_png_render_metadata(path)
            self._metadata[path] = metadata
        frame = next(
            (item for item in metadata.frames if item.frame_id == frame_id), None
        )
        if frame is None:
            raise AssetMetadataError(
                path,
                "render.frames['default']",
                "convention-resolved sprites require a default frame",
            )

        image = self._images.get(path)
        if image is None:
            try:
                image = pygame.image.load(path).convert_alpha()
            except pygame.error as exc:
                raise AssetMetadataError(path, "PNG", str(exc)) from exc
            self._images[path] = image
        frame_image = image.subsurface(pygame.Rect(frame.rect_px)).copy()
        loaded = LoadedSprite(
            image=frame_image,
            anchor=metadata.anchor,
            projection=metadata.projection,
            asset_path=path,
            frame_id=frame_id,
            rotation_angles=metadata.rotation_angles,
        )
        self._frames[cache_key] = loaded
        return loaded

    def overlay_for(
        self, obj: WorldObject, definition: ObjectType, overlay_id: str
    ) -> LoadedSprite | None:
        path = self.overlay_path_for(obj, definition, overlay_id)
        return self._load_path(path) if path is not None else None

    def _load_path(self, path: Path) -> LoadedSprite:
        frame_id = "default"
        cache_key = path, frame_id
        loaded = self._frames.get(cache_key)
        if loaded is not None:
            return loaded
        metadata = self._metadata.get(path)
        if metadata is None:
            metadata = load_png_render_metadata(path)
            self._metadata[path] = metadata
        frame = next((item for item in metadata.frames if item.frame_id == frame_id), None)
        if frame is None:
            raise AssetMetadataError(
                path,
                "render.frames['default']",
                "convention-resolved sprites require a default frame",
            )
        image = self._images.get(path)
        if image is None:
            try:
                image = pygame.image.load(path).convert_alpha()
            except pygame.error as exc:
                raise AssetMetadataError(path, "PNG", str(exc)) from exc
            self._images[path] = image
        loaded = LoadedSprite(
            image=image.subsurface(pygame.Rect(frame.rect_px)).copy(),
            anchor=metadata.anchor,
            projection=metadata.projection,
            asset_path=path,
            frame_id=frame_id,
            rotation_angles=metadata.rotation_angles,
        )
        self._frames[cache_key] = loaded
        return loaded

    def image_for(
        self, obj: WorldObject, definition: ObjectType
    ) -> pygame.Surface | None:
        sprite = self.sprite_for(obj, definition)
        return sprite.image if sprite is not None else None


def overlay_alpha(
    overlay: SpriteOverlay,
    state: object,
    capacities: dict[str, int] | None = None,
) -> int:
    """Map an instance-state value to the overlay's configured alpha range."""
    raw = getattr(state, overlay.state_field, None)
    if raw is None and isinstance(state, dict):
        raw = state.get(overlay.state_field)
    if isinstance(raw, bool):
        ratio = float(raw)
    elif isinstance(raw, (int, float)):
        upper = overlay.value_max
        if upper is None and overlay.capacity_resource is not None:
            upper = float((capacities or {}).get(overlay.capacity_resource, 0))
        upper = 1.0 if upper is None else upper
        span = upper - overlay.value_min
        ratio = 0.0 if span <= 0 else (float(raw) - overlay.value_min) / span
    else:
        ratio = float(bool(raw))
    ratio = max(0.0, min(1.0, ratio))
    return round(overlay.alpha_min + (overlay.alpha_max - overlay.alpha_min) * ratio)


class TileSpriteCatalog:
    def __init__(self, root: Path = DEFAULT_TILE_SPRITE_ROOT) -> None:
        self.root = root.resolve()
        self._images: dict[Path, pygame.Surface] = {}

    def reload(self) -> None:
        self._images.clear()

    def image_for(self, tile_id: str, overlay_id: str | None = None) -> pygame.Surface | None:
        suffix = f"_overlay-{overlay_id}" if overlay_id else ""
        path = self.root / f"{tile_id}{suffix}.png"
        if not path.is_file():
            return None
        image = self._images.get(path)
        if image is None:
            image = pygame.image.load(path).convert_alpha()
            self._images[path] = image
        return image


class BoundarySpriteCatalog:
    """Loads horizontal 64x8 boundary sprites by boundary type."""

    EXPECTED_SIZE = (64, 8)

    def __init__(self, root: Path = DEFAULT_BOUNDARY_SPRITE_ROOT) -> None:
        self.root = root.resolve()
        self._images: dict[Path, pygame.Surface] = {}

    def reload(self) -> None:
        self._images.clear()

    def image_for(self, boundary_type: str) -> pygame.Surface | None:
        path = self.root / f"{boundary_type}.png"
        if not path.is_file():
            return None
        image = self._images.get(path)
        if image is None:
            image = pygame.image.load(path).convert_alpha()
            if image.get_size() != self.EXPECTED_SIZE:
                raise AssetMetadataError(
                    path,
                    "PNG dimensions",
                    "boundary sprites must be 64x8 pixels in horizontal orientation",
                )
            self._images[path] = image
        return image
