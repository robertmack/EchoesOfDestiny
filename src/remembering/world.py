from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from remembering.model import (
    MapBuilding,
    MapDefinition,
    MapDoor,
    LevelTileState,
    MapStructure,
    MapTerrain,
    ObjectKind,
    ObjectState,
    ObjectType,
    RoomQuality,
    WorldObject,
)
from remembering.tiles import TRAVERSABLE_TILE_KINDS, Tile, TileEdge, TileKind, TileMap


DEFAULT_MAP_PATH = Path(__file__).resolve().parents[2] / "data" / "homestead.json"
DEFAULT_CURRENT_LEVEL_PATH = Path(__file__).resolve().parents[2] / "data" / "current_level.json"
DEFAULT_OBJECT_TYPES_PATH = Path(__file__).resolve().parents[2] / "data" / "object_types.json"
DEFAULT_TILE_TYPES_PATH = Path(__file__).resolve().parents[2] / "data" / "tile_types.json"


class MapLoadError(ValueError):
    """Raised when an authored map file cannot be loaded safely."""


class ObjectPersistenceError(ValueError):
    """Raised when persistent object state cannot be read or written safely."""


def _required(entry: dict[str, Any], field: str, entry_name: str) -> Any:
    if field not in entry:
        raise MapLoadError(f"{entry_name} is missing required field {field!r}")
    return entry[field]


def load_map(
    path: Path = DEFAULT_MAP_PATH,
    persistence_path: Path | None = DEFAULT_CURRENT_LEVEL_PATH,
    object_types_path: Path = DEFAULT_OBJECT_TYPES_PATH,
    tile_types_path: Path = DEFAULT_TILE_TYPES_PATH,
    day_number: int = 1,
    reset_for_morning: bool = False,
) -> MapDefinition:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        object_types = load_object_types(object_types_path)
        (
            tile_spawn_seed,
            tile_spawn_chances,
            tile_spawn_influences,
            permanent_soil_chance_per_till,
            till_count_loss_chance,
        ) = load_tile_spawn_rules(
            tile_types_path, object_types
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MapLoadError(f"Could not load map {path}: {exc}") from exc

    try:
        tile_size = int(data.get("tile_size", 32))
        map_width = int(_required(data, "width", "map")) * tile_size
        map_height = int(_required(data, "height", "map")) * tile_size
        buildings = [
            MapBuilding(
                building_id=str(_required(entry, "id", "building")),
                name=str(_required(entry, "name", "building")),
            )
            for entry in _required(data, "buildings", "map")
        ]
        terrain = [
            MapTerrain(
                terrain_id=str(_required(entry, "id", "terrain")),
                name=str(_required(entry, "name", "terrain")),
                kind=str(_required(entry, "kind", "terrain")),
                points=tuple(
                    (int(point[0]) * tile_size, int(point[1]) * tile_size)
                    for point in _required(entry, "points", "terrain")
                ),
                display_color=tuple(int(channel) for channel in _required(entry, "display_color", "terrain")),
                blocks_movement=bool(entry.get("blocks_movement", True)),
                width=int(entry.get("width", 0)) * tile_size,
            )
            for entry in _required(data, "terrain", "map")
        ]
        structures = [
            MapStructure(
                structure_id=str(_required(entry, "id", "structure")),
                building_id=str(_required(entry, "building_id", "room")),
                name=str(_required(entry, "name", "structure")),
                kind=str(_required(entry, "kind", "structure")),
                x=int(_required(entry, "x", "structure")) * tile_size,
                y=int(_required(entry, "y", "structure")) * tile_size,
                width=int(_required(entry, "width", "structure")) * tile_size,
                height=int(_required(entry, "height", "structure")) * tile_size,
                blocks_movement=bool(entry.get("blocks_movement", False)),
                quality=RoomQuality(str(entry["quality"])) if "quality" in entry else None,
                display_color=(
                    tuple(int(channel) for channel in entry["display_color"])
                    if "display_color" in entry
                    else None
                ),
                doors=tuple(
                    MapDoor(
                        side=str(_required(door, "side", "door")),
                        offset=int(_required(door, "offset", "door")) * tile_size,
                        width=int(_required(door, "width", "door")) * tile_size,
                        connects_to=str(door["connects_to"]) if door.get("connects_to") is not None else None,
                    )
                    for door in entry.get("doors", [])
                ),
            )
            for entry in _required(data, "structures", "map")
        ]
        objects = [_object_from_instance(entry, object_types, tile_size) for entry in _required(data, "objects", "map")]
        objects.extend(_generate_trees(data, structures, terrain, objects, map_width, map_height, object_types))
        if persistence_path is not None:
            objects = _apply_persistent_objects(
                objects, persistence_path, object_types, tile_size, reset_for_morning=reset_for_morning
            )
        tile_map = _build_tile_map(
            map_width,
            map_height,
            tile_size,
            structures,
            terrain,
            objects,
        )
        tile_states = (
            _load_level_tile_states(persistence_path, tile_map)
            if persistence_path is not None
            else {}
        )
        daily_objects = _generate_daily_objects(
            tile_map,
            objects,
            object_types,
            tile_spawn_chances,
            tile_spawn_influences,
            tile_spawn_seed + day_number,
            day_number,
        )
        objects.extend(daily_objects)
        _apply_object_occupancy(tile_map, daily_objects)
        object_ids = [obj.object_id for obj in objects]
        building_ids = [building.building_id for building in buildings]
        terrain_ids = [feature.terrain_id for feature in terrain]
        structure_ids = [structure.structure_id for structure in structures]
        if len(building_ids) != len(set(building_ids)):
            raise MapLoadError("Map contains duplicate building IDs")
        if len(terrain_ids) != len(set(terrain_ids)):
            raise MapLoadError("Map contains duplicate terrain IDs")
        for feature in terrain:
            if len(feature.display_color) != 3 or any(channel < 0 or channel > 255 for channel in feature.display_color):
                raise MapLoadError(f"Terrain {feature.terrain_id!r} has an invalid display_color")
            minimum_points = 2 if feature.kind == "river" else 3
            if len(feature.points) < minimum_points:
                raise MapLoadError(f"Terrain {feature.terrain_id!r} does not have enough points")
            if feature.kind == "river" and feature.width <= 0:
                raise MapLoadError(f"River {feature.terrain_id!r} must have a positive width")
        buildings_by_id = {building.building_id: building for building in buildings}
        for structure in structures:
            if structure.building_id not in buildings_by_id:
                raise MapLoadError(
                    f"Room {structure.structure_id!r} belongs to unknown building {structure.building_id!r}"
                )
            if structure.kind == "room" and structure.quality is None:
                raise MapLoadError(f"Room {structure.structure_id!r} is missing a quality")
            if structure.kind == "room" and structure.display_color is None:
                raise MapLoadError(f"Room {structure.structure_id!r} is missing a display_color")
            if structure.display_color is not None and (
                len(structure.display_color) != 3
                or any(channel < 0 or channel > 255 for channel in structure.display_color)
            ):
                raise MapLoadError(
                    f"Structure {structure.structure_id!r} display_color must contain three values from 0 to 255"
                )
            for door in structure.doors:
                if door.side not in {"top", "bottom", "left", "right"}:
                    raise MapLoadError(
                        f"Door on structure {structure.structure_id!r} has invalid side {door.side!r}"
                    )
                wall_length = structure.width if door.side in {"top", "bottom"} else structure.height
                if door.width <= 0 or door.offset < 0 or door.offset + door.width > wall_length:
                    raise MapLoadError(f"Door on structure {structure.structure_id!r} falls outside its wall")
        if len(object_ids) != len(set(object_ids)):
            raise MapLoadError("Map contains duplicate object IDs")
        for obj in objects:
            if obj.object_id <= 0:
                raise MapLoadError("Object IDs must be positive integers")
            if obj.orientation not in {"N/S", "E/W"}:
                raise MapLoadError(f"Object {obj.object_id} has invalid orientation {obj.orientation!r}")
            if not 1 <= obj.quality <= 100:
                raise MapLoadError(f"Object {obj.object_id} quality must be from 1 to 100")
        if len(structure_ids) != len(set(structure_ids)):
            raise MapLoadError("Map contains duplicate structure IDs")
        structures_by_id = {structure.structure_id: structure for structure in structures}
        for structure in structures:
            for door in structure.doors:
                if door.connects_to is None:
                    continue
                target = structures_by_id.get(door.connects_to)
                if target is None:
                    raise MapLoadError(
                        f"Door on {structure.structure_id!r} connects to unknown room {door.connects_to!r}"
                    )
                opening = door_opening(structure, door)
                if not any(
                    other.connects_to == structure.structure_id and door_opening(target, other) == opening
                    for other in target.doors
                ):
                    raise MapLoadError(
                        f"Door between {structure.structure_id!r} and {target.structure_id!r} is not reciprocal"
                    )
        return MapDefinition(
            name=str(_required(data, "name", "map")),
            width=map_width,
            height=map_height,
            buildings=buildings,
            terrain=terrain,
            structures=structures,
            objects={obj.object_id: obj for obj in objects},
            tile_map=tile_map,
            object_types=object_types,
            tile_states=tile_states,
            permanent_soil_chance_per_till=permanent_soil_chance_per_till,
            till_count_loss_chance=till_count_loss_chance,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise MapLoadError(f"Invalid map data in {path}: {exc}") from exc


def create_world() -> dict[int, WorldObject]:
    return load_map().objects


def initialize_current_level_from_map(
    map_path: Path = DEFAULT_MAP_PATH,
    current_level_path: Path = DEFAULT_CURRENT_LEVEL_PATH,
    object_types_path: Path = DEFAULT_OBJECT_TYPES_PATH,
) -> None:
    """Create fresh mutable session state from a fixed authored level."""
    try:
        map_data = json.loads(map_path.read_text(encoding="utf-8"))
        object_types = load_object_types(object_types_path)
        tile_size = int(map_data.get("tile_size", 32))
        entries = []
        for authored in _required(map_data, "objects", "map"):
            instance = _object_from_instance(authored, object_types, tile_size)
            state = {
                "x": instance.x // tile_size,
                "y": instance.y // tile_size,
                "orientation": instance.orientation,
                "quality": instance.quality,
                "active": instance.active,
                "state": instance.state,
            }
            entries.append(
                {
                    "id": instance.object_id,
                    "type": instance.type_id,
                    "persistent_state": {"persistent": True, **state},
                    "current_state": dict(state),
                }
            )
        payload = {"level_map": map_path.name, "objects": entries, "tiles": []}
        current_level_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = current_level_path.with_suffix(current_level_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(current_level_path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise ObjectPersistenceError(f"Could not initialize current level from {map_path}: {exc}") from exc


def sync_current_level_from_map(
    map_path: Path = DEFAULT_MAP_PATH,
    current_level_path: Path = DEFAULT_CURRENT_LEVEL_PATH,
    object_types_path: Path = DEFAULT_OBJECT_TYPES_PATH,
) -> None:
    """Upsert authored map instances into the mutable current-level state."""
    try:
        map_data = json.loads(map_path.read_text(encoding="utf-8"))
        current_data = (
            json.loads(current_level_path.read_text(encoding="utf-8"))
            if current_level_path.exists()
            else {"level_map": map_path.name, "objects": []}
        )
        object_types = load_object_types(object_types_path)
        tile_size = int(map_data.get("tile_size", 32))
        current_by_id = {int(entry["id"]): entry for entry in current_data.get("objects", [])}
        for authored in _required(map_data, "objects", "map"):
            instance = _object_from_instance(authored, object_types, tile_size)
            existing = current_by_id.get(instance.object_id, {})
            existing_persistent = existing.get("persistent_state", {})
            persistent = bool(existing_persistent.get("persistent", instance.persistent))
            state = {
                "x": instance.x // tile_size,
                "y": instance.y // tile_size,
                "orientation": instance.orientation,
                "quality": instance.quality,
                "active": instance.active,
                "state": instance.state,
            }
            current_by_id[instance.object_id] = {
                "id": instance.object_id,
                "type": instance.type_id,
                "persistent_state": {"persistent": persistent, **state},
                "current_state": dict(state),
            }
        payload = {
            "level_map": map_path.name,
            "objects": list(current_by_id.values()),
            "tiles": current_data.get("tiles", []),
        }
        temporary_path = current_level_path.with_suffix(current_level_path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(current_level_path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise ObjectPersistenceError(f"Could not update current level from {map_path}: {exc}") from exc


def load_object_types(path: Path = DEFAULT_OBJECT_TYPES_PATH) -> dict[str, ObjectType]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        definitions = [
            ObjectType(
                type_id=str(_required(entry, "id", "object type")),
                name=str(_required(entry, "name", "object type")),
                kind=ObjectKind[str(_required(entry, "kind", "object type"))],
                descriptions=_quality_descriptions(entry),
                width=int(_required(entry, "width", "object type")),
                height=int(_required(entry, "height", "object type")),
                blocks_movement=bool(entry.get("blocks_movement", False)),
                spawn_tiles=tuple(str(kind) for kind in entry.get("spawn_tiles", [])),
                spawn_influence=tuple(
                    (
                        str(_required(influence, "type", "spawn influence")),
                        float(_required(influence, "chance", "spawn influence")),
                        int(_required(influence, "distance", "spawn influence")),
                        float(_required(influence, "decay", "spawn influence")),
                    )
                    for influence in entry.get("spawn_influence", [])
                ),
                build_cost=tuple(
                    (str(item), int(amount))
                    for item, amount in entry.get("build_cost", {}).items()
                ),
            )
            for entry in _required(data, "object_types", "object catalog")
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MapLoadError(f"Could not load object catalog {path}: {exc}") from exc
    if len({definition.type_id for definition in definitions}) != len(definitions):
        raise MapLoadError("Object catalog contains duplicate type IDs")
    valid_tile_kinds = {kind.value for kind in TileKind}
    for definition in definitions:
        if not set(definition.spawn_tiles) <= valid_tile_kinds:
            raise MapLoadError(f"Object type {definition.type_id!r} contains an invalid spawn tile")
    return {definition.type_id: definition for definition in definitions}


def _quality_descriptions(entry: dict[str, Any]) -> dict[str, str]:
    stages = ("ruined", "damaged", "worn", "good", "fine")
    if "descriptions" in entry:
        descriptions = {str(stage): str(text) for stage, text in entry["descriptions"].items()}
        if set(descriptions) != set(stages):
            raise MapLoadError(f"Object type {entry.get('id')!r} must describe every quality stage")
        return descriptions
    legacy = str(entry.get("description", ""))
    return {stage: legacy for stage in stages}


def load_tile_spawn_rules(
    path: Path, object_types: dict[str, ObjectType]
) -> tuple[
    int,
    dict[TileKind, dict[str, float]],
    dict[TileKind, tuple[tuple[str, float, int, float], ...]],
    float,
    float,
]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rules: dict[TileKind, dict[str, float]] = {}
        influences: dict[TileKind, tuple[tuple[str, float, int, float], ...]] = {}
        for kind in TileKind:
            entry = _required(_required(data, "tile_types", "tile catalog"), kind.value, "tile type")
            chances = {str(type_id): float(chance) for type_id, chance in entry.get("spawn_chances", {}).items()}
            for type_id, chance in chances.items():
                if type_id not in object_types or not 0 <= chance <= 1:
                    raise MapLoadError(f"Invalid spawn chance for {type_id!r} on {kind.value}")
            rules[kind] = chances
            influences[kind] = tuple(
                (
                    str(_required(influence, "type", "tile spawn influence")),
                    float(_required(influence, "chance", "tile spawn influence")),
                    int(_required(influence, "distance", "tile spawn influence")),
                    float(_required(influence, "decay", "tile spawn influence")),
                )
                for influence in entry.get("spawn_influence", [])
            )
            for type_id, chance, distance, decay in influences[kind]:
                if type_id not in object_types or not 0 <= chance <= 1 or distance < 1 or not 0 <= decay <= 1:
                    raise MapLoadError(f"Invalid spawn influence for {type_id!r} on {kind.value}")
        grassland = _required(_required(data, "tile_types", "tile catalog"), "grassland", "tile type")
        tilling = _required(grassland, "tilling", "grassland tile type")
        if str(_required(tilling, "result", "grassland tilling")) != TileKind.SOIL.value:
            raise MapLoadError("Grassland tilling must currently result in soil")
        required_fields = {"till_count", "tilled_today", "permanent_kind", "crop", "crop_growth", "watered", "tended"}
        if set(_required(tilling, "tracked_fields", "grassland tilling")) != required_fields:
            raise MapLoadError("Grassland tilling must declare all tracked tile-instance fields")
        permanent_chance = float(tilling.get("permanent_chance_per_till", 0.00001))
        loss_chance = float(tilling.get("untended_count_loss_chance", 0.10))
        if not 0 <= permanent_chance <= 1 or not 0 <= loss_chance <= 1:
            raise MapLoadError("Tilling chances must be between zero and one")
        return int(data.get("seed", 0)), rules, influences, permanent_chance, loss_chance
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise MapLoadError(f"Could not load tile catalog {path}: {exc}") from exc


def _object_from_instance(
    entry: dict[str, Any], object_types: dict[str, ObjectType], tile_size: int = 32
) -> WorldObject:
    type_id = str(_required(entry, "type", "object instance"))
    if type_id not in object_types:
        raise MapLoadError(f"Object instance references unknown type {type_id!r}")
    definition = object_types[type_id]
    live = entry
    orientation = _normalize_orientation(str(live.get("orientation", entry.get("orientation", "E/W"))))
    obj = WorldObject(
        object_id=int(_required(entry, "id", "object instance")),
        name=definition.name,
        kind=definition.kind,
        x=int(_required(live, "x", "object instance")) * tile_size,
        y=int(_required(live, "y", "object instance")) * tile_size,
        width=(int(entry.get("height", definition.height)) if orientation == "N/S" else int(entry.get("width", definition.width))),
        height=(int(entry.get("width", definition.width)) if orientation == "N/S" else int(entry.get("height", definition.height))),
        active=bool(live.get("active", True)),
        state=str(live.get("state", "")),
        blocks_movement=definition.blocks_movement,
        persistent=False,
        descriptions=dict(definition.descriptions),
        type_id=type_id,
        orientation=orientation,
        quality=int(live.get("quality", live.get("condition", entry.get("quality", entry.get("condition", 100))))),
    )
    return obj


def _normalize_orientation(value: str) -> str:
    legacy = {"north": "N/S", "south": "N/S", "east": "E/W", "west": "E/W"}
    return legacy.get(value.lower(), value.upper())


def _object_from_persistent_entry(
    entry: dict[str, Any], object_types: dict[str, ObjectType], tile_size: int
) -> WorldObject:
    return _object_from_instance(entry, object_types, tile_size)


def _apply_persistent_objects(
    objects: list[WorldObject],
    path: Path,
    object_types: dict[str, ObjectType],
    tile_size: int,
    *,
    reset_for_morning: bool = False,
) -> list[WorldObject]:
    if not path.exists():
        return objects
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved = []
        for entry in _required(data, "objects", "persistence"):
            normalized = dict(entry)
            if "current_state" in entry:
                persistent_state = dict(entry.get("persistent_state", {}))
                is_persistent = bool(persistent_state.get("persistent", False))
                chosen_state = persistent_state if reset_for_morning and is_persistent else entry["current_state"]
                normalized = {"id": entry["id"], "type": entry["type"], **chosen_state}
                normalized.pop("persistent", None)
                loaded = _object_from_persistent_entry(normalized, object_types, tile_size)
                loaded.persistent = is_persistent
                loaded.persistent_state = ObjectState(
                    x=int(persistent_state.get("x", loaded.x // tile_size)) * tile_size,
                    y=int(persistent_state.get("y", loaded.y // tile_size)) * tile_size,
                    orientation=_normalize_orientation(str(persistent_state.get("orientation", loaded.orientation))),
                    quality=int(persistent_state.get("quality", persistent_state.get("condition", loaded.quality))),
                    active=bool(persistent_state.get("active", loaded.active)),
                    state=str(persistent_state.get("state", loaded.state)),
                    persistent=is_persistent,
                )
                saved.append(loaded)
                continue
            # Persistence is a current-level concern. Legacy flat entries do not
            # contain enough information to establish a dawn-reset baseline.
            continue
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ObjectPersistenceError(f"Could not load persistent objects {path}: {exc}") from exc
    by_id = {obj.object_id: obj for obj in objects}
    by_id.update((obj.object_id, obj) for obj in saved)
    return list(by_id.values())


def _load_level_tile_states(path: Path, tile_map: TileMap) -> dict[tuple[int, int], LevelTileState]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        states: dict[tuple[int, int], LevelTileState] = {}
        for entry in data.get("tiles", []):
            column = int(_required(entry, "x", "tile state"))
            row = int(_required(entry, "y", "tile state"))
            tile = tile_map.tile_at(column, row)
            if tile is None:
                raise MapLoadError(f"Tile state ({column}, {row}) is outside the level")
            raw_growth = entry.get("crop_growth", 0.0)
            growth = float(raw_growth) / 3.0 if isinstance(raw_growth, int) else float(raw_growth)
            state = LevelTileState(
                column=column,
                row=row,
                till_count=max(0, int(entry.get("till_count", 0))),
                tilled_today=bool(entry.get("tilled_today", False)),
                permanent_kind=str(entry["permanent_kind"]) if entry.get("permanent_kind") else None,
                crop=str(entry["crop"]) if entry.get("crop") else None,
                crop_growth=max(0.0, min(1.0, growth)),
                watered=bool(entry.get("watered", False)),
                tended=bool(entry.get("tended", entry.get("sung_to", False))),
            )
            if state.permanent_kind not in {None, TileKind.SOIL.value}:
                raise MapLoadError(f"Tile state ({column}, {row}) has an invalid permanent kind")
            if state.permanent_kind == TileKind.SOIL.value or state.tilled_today or state.crop:
                tile.kind = TileKind.SOIL
            if state.crop:
                tile.properties.append(f"crop:{state.crop}")
                tile.properties.append(f"crop_growth:{state.crop_growth}")
            states[(column, row)] = state
        return states
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise ObjectPersistenceError(f"Could not load tile state from {path}: {exc}") from exc


def advance_level_tile_states(
    states: dict[tuple[int, int], LevelTileState],
    *,
    day_number: int,
    permanent_chance_per_till: float,
    till_count_loss_chance: float,
) -> None:
    randomizer = random.Random(91_003 + day_number)
    for key in sorted(list(states)):
        state = states[key]
        if state.tilled_today and state.permanent_kind is None:
            chance = min(1.0, permanent_chance_per_till * state.till_count)
            if randomizer.random() < chance:
                state.permanent_kind = TileKind.SOIL.value
        elif not state.tilled_today and state.till_count > 0:
            if randomizer.random() < till_count_loss_chance:
                state.till_count -= 1
        if state.crop is not None:
            state.crop = None
            state.crop_growth = 0.0
        state.watered = False
        state.tended = False
        state.tilled_today = False
        if state.till_count == 0 and state.permanent_kind is None and state.crop is None:
            del states[key]


def save_persistent_objects(
    objects: dict[int, WorldObject],
    path: Path = DEFAULT_CURRENT_LEVEL_PATH,
    tile_size: int = 32,
    tile_states: dict[tuple[int, int], LevelTileState] | None = None,
) -> None:
    entries = []
    for obj in objects.values():
        if obj.daily_spawned:
            continue
        state = obj.persistent_state if obj.persistent else ObjectState(
            obj.x, obj.y, obj.orientation, obj.quality, obj.active, obj.state
        )
        if state is None:
            continue
        entry: dict[str, Any] = {"id": obj.object_id, "type": obj.type_id}
        baseline = obj.persistent_state or state
        entry["persistent_state"] = {
            "persistent": obj.persistent,
            "x": baseline.x // tile_size,
            "y": baseline.y // tile_size,
            "orientation": baseline.orientation,
            "quality": baseline.quality,
            "active": baseline.active,
            "state": baseline.state,
        }
        entry["current_state"] = {
            "x": round(obj.x / tile_size),
            "y": round(obj.y / tile_size),
            "orientation": obj.orientation,
            "quality": obj.quality,
            "active": obj.active,
            "state": obj.state,
        }
        entries.append(entry)
    if tile_states is None and path.exists():
        try:
            existing_tiles = json.loads(path.read_text(encoding="utf-8")).get("tiles", [])
        except (OSError, json.JSONDecodeError):
            existing_tiles = []
    else:
        existing_tiles = [
            {
                "x": state.column,
                "y": state.row,
                "till_count": state.till_count,
                "tilled_today": state.tilled_today,
                "permanent_kind": state.permanent_kind,
                "crop": state.crop,
                "crop_growth": state.crop_growth,
                "watered": state.watered,
                "tended": state.tended,
            }
            for state in sorted((tile_states or {}).values(), key=lambda value: (value.row, value.column))
        ]
    payload = {
        "level_map": DEFAULT_MAP_PATH.name,
        "objects": entries,
        "tiles": existing_tiles,
    }
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(path)
    except OSError as exc:
        raise ObjectPersistenceError(f"Could not save persistent objects {path}: {exc}") from exc


def _spread_spawn_influence(
    influences: dict[tuple[int, int], dict[str, float]],
    source_column: int,
    source_row: int,
    type_id: str,
    chance: float,
    distance: int,
    decay: float,
) -> None:
    for row in range(source_row - distance, source_row + distance + 1):
        for column in range(source_column - distance, source_column + distance + 1):
            tile_distance = max(abs(column - source_column), abs(row - source_row))
            effective_chance = chance * decay ** max(0, tile_distance - 1)
            tile_influences = influences.setdefault((column, row), {})
            tile_influences[type_id] = 1 - (1 - tile_influences.get(type_id, 0.0)) * (
                1 - effective_chance
            )


def _generate_daily_objects(
    tile_map: TileMap,
    objects: list[WorldObject],
    object_types: dict[str, ObjectType],
    spawn_chances: dict[TileKind, dict[str, float]],
    tile_spawn_influences: dict[TileKind, tuple[tuple[str, float, int, float], ...]],
    seed: int,
    day_number: int,
) -> list[WorldObject]:
    randomizer = random.Random(seed)
    influences: dict[tuple[int, int], dict[str, float]] = {}
    for source_row in range(tile_map.rows):
        for source_column in range(tile_map.columns):
            source_tile = tile_map.tile_at(source_column, source_row)
            if source_tile is None:
                continue
            for type_id, chance, distance, decay in tile_spawn_influences.get(source_tile.kind, ()):
                _spread_spawn_influence(
                    influences, source_column, source_row, type_id, chance, distance, decay
                )
    for source in objects:
        definition = object_types.get(source.type_id)
        if not source.active or definition is None:
            continue
        located = tile_map.tile_at_world(*source.center)
        if located is None:
            continue
        source_column, source_row, _ = located
        for type_id, chance, distance, decay in definition.spawn_influence:
            if type_id not in object_types or not 0 <= chance <= 1 or distance < 1 or not 0 <= decay <= 1:
                raise MapLoadError(f"Invalid spawn influence on object type {definition.type_id!r}")
            _spread_spawn_influence(
                influences, source_column, source_row, type_id, chance, distance, decay
            )

    spawned: list[WorldObject] = []
    next_id = 2_000_000 + day_number * 10_000
    for row in range(tile_map.rows):
        for column in range(tile_map.columns):
            tile = tile_map.tile_at(column, row)
            if tile is None or tile.kind not in TRAVERSABLE_TILE_KINDS or "blocked" in tile.properties:
                continue
            if any(prop.startswith("object:") for prop in tile.properties):
                continue
            chances = dict(spawn_chances.get(tile.kind, {}))
            for type_id, chance in influences.get((column, row), {}).items():
                chances[type_id] = 1 - (1 - chances.get(type_id, 0.0)) * (1 - chance)
            for type_id, chance in chances.items():
                definition = object_types[type_id]
                if definition.spawn_tiles and tile.kind.value not in definition.spawn_tiles:
                    continue
                if randomizer.random() >= chance:
                    continue
                center_x, center_y = tile_map.tile_center(column, row)
                spawned.append(
                    WorldObject(
                        object_id=next_id + len(spawned),
                        name=definition.name,
                        kind=definition.kind,
                        x=round(center_x - definition.width / 2),
                        y=round(center_y - definition.height / 2),
                        width=definition.width,
                        height=definition.height,
                        blocks_movement=definition.blocks_movement,
                        persistent=False,
                        descriptions=dict(definition.descriptions),
                        type_id=type_id,
                        quality=100,
                        daily_spawned=True,
                    )
                )
                break
    return spawned


def _generate_trees(
    data: dict[str, Any],
    structures: list[MapStructure],
    terrain: list[MapTerrain],
    authored_objects: list[WorldObject],
    map_width: int,
    map_height: int,
    object_types: dict[str, ObjectType],
) -> list[WorldObject]:
    config = data.get("tree_generation")
    if config is None:
        return []
    bed = next((obj for obj in authored_objects if obj.type_id == "bed"), None)
    if bed is None:
        raise MapLoadError("Tree generation requires an object with ID 'bed'")

    count = int(_required(config, "count", "tree_generation"))
    tree_width = int(config.get("tree_width", 30))
    tree_height = int(config.get("tree_height", 38))
    clear_radius = float(config.get("homestead_clear_radius", 420))
    density_power = float(config.get("density_power", 1.8))
    min_spacing = float(config.get("min_spacing", 38))
    if count < 0 or tree_width <= 0 or tree_height <= 0 or density_power <= 0 or min_spacing < 0:
        raise MapLoadError("Tree generation values must be positive")

    randomizer = random.Random(int(config.get("seed", 0)))
    origin_x, origin_y = bed.center
    max_distance = max(
        math.hypot(origin_x, origin_y),
        math.hypot(map_width - origin_x, origin_y),
        math.hypot(origin_x, map_height - origin_y),
        math.hypot(map_width - origin_x, map_height - origin_y),
    )
    trees: list[WorldObject] = []
    attempts = 0
    max_attempts = max(1_000, count * 500)
    while len(trees) < count and attempts < max_attempts:
        attempts += 1
        x = randomizer.randint(20, map_width - tree_width - 20)
        y = randomizer.randint(20, map_height - tree_height - 20)
        center_x = x + tree_width / 2
        center_y = y + tree_height / 2
        distance = math.hypot(center_x - origin_x, center_y - origin_y)
        if distance < clear_radius:
            continue
        density = min(1.0, distance / max_distance) ** density_power
        if randomizer.random() > density:
            continue
        if any(_rectangles_overlap(x, y, tree_width, tree_height, room.x, room.y, room.width, room.height, 24) for room in structures):
            continue
        if any(
            feature.kind != "dense_forest"
            and (
                (feature.kind == "pond" and _point_in_polygon(center_x, center_y, feature.points))
                or terrain_blocks_point(feature, center_x, center_y, max(tree_width, tree_height) / 2 + 10)
            )
            for feature in terrain
        ):
            continue
        if any(_rectangles_overlap(x, y, tree_width, tree_height, obj.x, obj.y, obj.width, obj.height, 10) for obj in authored_objects):
            continue
        if any(math.hypot(center_x - tree.center[0], center_y - tree.center[1]) < min_spacing for tree in trees):
            continue
        trees.append(
            WorldObject(
                object_id=1_000_000 + len(trees) + 1,
                name="Tree",
                kind=ObjectKind.TREE,
                x=x,
                y=y,
                width=tree_width,
                height=tree_height,
                blocks_movement=True,
                descriptions=dict(object_types["tree"].descriptions),
                type_id="tree",
                quality=100,
            )
        )
    if len(trees) != count:
        raise MapLoadError(f"Could only place {len(trees)} of {count} requested trees")
    return trees


def _rectangles_overlap(
    ax: int,
    ay: int,
    aw: int,
    ah: int,
    bx: int,
    by: int,
    bw: int,
    bh: int,
    padding: int,
) -> bool:
    return not (
        ax + aw + padding <= bx
        or bx + bw + padding <= ax
        or ay + ah + padding <= by
        or by + bh + padding <= ay
    )


def terrain_blocks_point(feature: MapTerrain, x: float, y: float, clearance: float = 0.0) -> bool:
    if not feature.blocks_movement:
        return False
    if feature.kind == "river":
        radius = feature.width / 2 + clearance
        return any(
            _point_segment_distance(x, y, start, end) <= radius
            for start, end in zip(feature.points, feature.points[1:])
        )
    if _point_in_polygon(x, y, feature.points):
        return True
    return any(
        _point_segment_distance(x, y, start, end) <= clearance
        for start, end in zip(feature.points, (*feature.points[1:], feature.points[0]))
    )


def _point_segment_distance(
    x: float,
    y: float,
    start: tuple[int, int],
    end: tuple[int, int],
) -> float:
    start_x, start_y = start
    dx, dy = end[0] - start_x, end[1] - start_y
    if dx == 0 and dy == 0:
        return math.hypot(x - start_x, y - start_y)
    ratio = max(0.0, min(1.0, ((x - start_x) * dx + (y - start_y) * dy) / (dx * dx + dy * dy)))
    closest_x = start_x + ratio * dx
    closest_y = start_y + ratio * dy
    return math.hypot(x - closest_x, y - closest_y)


def _point_in_polygon(x: float, y: float, points: tuple[tuple[int, int], ...]) -> bool:
    inside = False
    previous_x, previous_y = points[-1]
    for current_x, current_y in points:
        if (current_y > y) != (previous_y > y):
            crossing_x = (previous_x - current_x) * (y - current_y) / (previous_y - current_y) + current_x
            if x < crossing_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def _build_tile_map(
    width: int,
    height: int,
    tile_size: int,
    structures: list[MapStructure],
    terrain: list[MapTerrain],
    objects: list[WorldObject],
) -> TileMap:
    if tile_size <= 0 or width % tile_size or height % tile_size:
        raise MapLoadError("Map width and height must be divisible by tile_size")
    tile_map = TileMap(
        columns=width // tile_size,
        rows=height // tile_size,
        tile_size=tile_size,
        tiles=[Tile(TileKind.GRASSLAND) for _ in range((width // tile_size) * (height // tile_size))],
    )
    for row in range(tile_map.rows):
        for column in range(tile_map.columns):
            tile = tile_map.tile_at(column, row)
            center_x = column * tile_size + tile_size / 2
            center_y = row * tile_size + tile_size / 2
            for feature in terrain:
                if feature.kind == "river":
                    distance = min(
                        _point_segment_distance(center_x, center_y, start, end)
                        for start, end in zip(feature.points, feature.points[1:])
                    )
                    if distance <= feature.width / 2:
                        tile.kind = TileKind.DEEP_WATER
                        tile.properties = [f"terrain:{feature.terrain_id}"]
                    elif distance <= feature.width / 2 + tile_size:
                        tile.kind = TileKind.SHALLOW_WATER
                        tile.properties = [f"terrain:{feature.terrain_id}"]
                elif _point_in_polygon(center_x, center_y, feature.points):
                    if feature.kind == "mountains":
                        tile.kind = TileKind.MOUNTAIN
                    elif feature.kind == "ocean":
                        tile.kind = TileKind.DEEP_WATER
                    elif feature.kind == "dense_forest":
                        tile.kind = TileKind.GRASSLAND
                        tile.properties.append("blocked")
                    elif feature.kind == "pond":
                        tile.kind = TileKind.POND
                    tile.properties.append(f"terrain:{feature.terrain_id}")
                elif feature.kind == "mountains":
                    boundary_distance = min(
                        _point_segment_distance(center_x, center_y, start, end)
                        for start, end in zip(feature.points, (*feature.points[1:], feature.points[0]))
                    )
                    if boundary_distance <= tile_size:
                        tile.kind = TileKind.HILLS
                        tile.properties.append(f"terrain:{feature.terrain_id}:foothills")

    for room in structures:
        first_column = room.x // tile_size
        last_column = (room.x + room.width) // tile_size - 1
        first_row = room.y // tile_size
        last_row = (room.y + room.height) // tile_size - 1
        for row in range(first_row, last_row + 1):
            for column in range(first_column, last_column + 1):
                tile = tile_map.tile_at(column, row)
                tile.kind = TileKind.WOODEN_FLOOR
                tile.properties = [
                    f"room:{room.structure_id}",
                    f"building:{room.building_id}",
                    f"quality:{room.quality.value}",
                ]
                tile.passable = {edge: True for edge in TileEdge}

    _close_nontraversable_edges(tile_map)
    for room in structures:
        _apply_room_edges(tile_map, room)
    _apply_object_occupancy(tile_map, objects)
    return tile_map


def _apply_object_occupancy(tile_map: TileMap, objects: list[WorldObject]) -> None:
    tile_size = tile_map.tile_size
    for obj in objects:
        if not obj.active:
            continue
        first_column = obj.x // tile_size
        last_column = (obj.x + obj.width - 1) // tile_size
        first_row = obj.y // tile_size
        last_row = (obj.y + obj.height - 1) // tile_size
        for row in range(first_row, last_row + 1):
            for column in range(first_column, last_column + 1):
                tile = tile_map.tile_at(column, row)
                if tile is None:
                    continue
                object_property = f"object:{obj.object_id}"
                if object_property not in tile.properties:
                    tile.properties.append(object_property)
                if obj.blocks_movement:
                    if "blocked" not in tile.properties:
                        tile.properties.append("blocked")
                    for edge in TileEdge:
                        tile_map.set_edge_passable(column, row, edge, False)


def rebuild_tile_map(map_definition: MapDefinition) -> None:
    """Rebuild object occupancy while retaining mutable terrain changes."""
    previous_kinds = [tile.kind for tile in map_definition.tile_map.tiles]
    rebuilt = _build_tile_map(
        map_definition.width,
        map_definition.height,
        map_definition.tile_map.tile_size,
        map_definition.structures,
        map_definition.terrain,
        list(map_definition.objects.values()),
    )
    for previous_kind, tile in zip(previous_kinds, rebuilt.tiles):
        if previous_kind is TileKind.SOIL and tile.kind is TileKind.GRASSLAND:
            tile.kind = TileKind.SOIL
    for (column, row), state in map_definition.tile_states.items():
        tile = rebuilt.tile_at(column, row)
        if tile is not None and state.crop is not None:
            tile.properties.append(f"crop:{state.crop}")
            tile.properties.append(f"crop_growth:{state.crop_growth}")
    map_definition.tile_map = rebuilt


def _close_nontraversable_edges(tile_map: TileMap) -> None:
    for row in range(tile_map.rows):
        for column in range(tile_map.columns):
            tile = tile_map.tile_at(column, row)
            if tile.kind in TRAVERSABLE_TILE_KINDS and "blocked" not in tile.properties:
                continue
            for edge in TileEdge:
                tile_map.set_edge_passable(column, row, edge, False)


def _apply_room_edges(tile_map: TileMap, room: MapStructure) -> None:
    size = tile_map.tile_size
    left = room.x // size
    right = (room.x + room.width) // size - 1
    top = room.y // size
    bottom = (room.y + room.height) // size - 1
    for column in range(left, right + 1):
        tile_map.set_edge_passable(column, top, TileEdge.NORTH, False)
        tile_map.set_edge_passable(column, bottom, TileEdge.SOUTH, False)
    for row in range(top, bottom + 1):
        tile_map.set_edge_passable(left, row, TileEdge.WEST, False)
        tile_map.set_edge_passable(right, row, TileEdge.EAST, False)
    for door in room.doors:
        if door.side in {"top", "bottom"}:
            first = (room.x + door.offset) // size
            last = (room.x + door.offset + door.width) // size - 1
            row = top if door.side == "top" else bottom
            edge = TileEdge.NORTH if door.side == "top" else TileEdge.SOUTH
            for column in range(first, last + 1):
                tile_map.set_edge_passable(column, row, edge, True)
        else:
            first = (room.y + door.offset) // size
            last = (room.y + door.offset + door.width) // size - 1
            column = left if door.side == "left" else right
            edge = TileEdge.WEST if door.side == "left" else TileEdge.EAST
            for row in range(first, last + 1):
                tile_map.set_edge_passable(column, row, edge, True)


def door_opening(structure: MapStructure, door: MapDoor) -> tuple[str, int, int, int]:
    if door.side == "top":
        return "horizontal", structure.y, structure.x + door.offset, structure.x + door.offset + door.width
    if door.side == "bottom":
        return (
            "horizontal",
            structure.y + structure.height,
            structure.x + door.offset,
            structure.x + door.offset + door.width,
        )
    if door.side == "left":
        return "vertical", structure.x, structure.y + door.offset, structure.y + door.offset + door.width
    return (
        "vertical",
        structure.x + structure.width,
        structure.y + door.offset,
        structure.y + door.offset + door.width,
    )


def map_door_openings(map_definition: MapDefinition) -> list[tuple[str, int, int, int]]:
    """Return unique doorway spans; reciprocal room doors describe one opening."""
    return sorted(
        {
            door_opening(structure, door)
            for structure in map_definition.structures
            for door in structure.doors
        }
    )


def structure_wall_rects(structure: MapStructure, thickness: int = 8) -> list[tuple[int, int, int, int]]:
    """Build wall rectangles, splitting each wall around its authored doors."""
    walls: list[tuple[int, int, int, int]] = []
    sides = {
        "top": (structure.x, structure.y, structure.width, True),
        "bottom": (structure.x, structure.y + structure.height - thickness, structure.width, True),
        "left": (structure.x, structure.y, structure.height, False),
        "right": (structure.x + structure.width - thickness, structure.y, structure.height, False),
    }
    for side, (x, y, length, horizontal) in sides.items():
        doors = sorted((door for door in structure.doors if door.side == side), key=lambda door: door.offset)
        cursor = 0
        for door in doors:
            opening_start = max(cursor, min(length, door.offset))
            opening_end = max(opening_start, min(length, door.offset + door.width))
            if opening_start > cursor:
                if horizontal:
                    walls.append((x + cursor, y, opening_start - cursor, thickness))
                else:
                    walls.append((x, y + cursor, thickness, opening_start - cursor))
            cursor = opening_end
        if cursor < length:
            if horizontal:
                walls.append((x + cursor, y, length - cursor, thickness))
            else:
                walls.append((x, y + cursor, thickness, length - cursor))
    return walls
