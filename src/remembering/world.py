from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from remembering.model import (
    BoundaryObject,
    BuildMemory,
    CharacterState,
    CharacterType,
    MapBuilding,
    MapDefinition,
    MapDoor,
    LevelTileState,
    MapStructure,
    MapTerrain,
    ObjectKind,
    ObjectForm,
    ObjectState,
    ObjectType,
    PersistencePolicy,
    RoomQuality,
    RoutineStep,
    SkillState,
    SpriteOverlay,
    WorldObject,
)
from remembering.coordinates import (
    bounds_as_position_data,
    bounds_from_position_data,
    mapxy_as_position_data,
    position_from_data,
)
from remembering.jsonc import loads_jsonc
from remembering.tiles import TRAVERSABLE_TILE_KINDS, Tile, TileEdge, TileKind, TileMap


DEFAULT_MAP_PATH = Path(__file__).resolve().parents[2] / "data" / "homestead.jsonc"
DEFAULT_CURRENT_LEVEL_PATH = Path(__file__).resolve().parents[2] / "data" / "current_level.jsonc"
DEFAULT_OBJECT_TYPES_PATH = Path(__file__).resolve().parents[2] / "data" / "object_types.jsonc"
DEFAULT_TILE_TYPES_PATH = Path(__file__).resolve().parents[2] / "data" / "tile_types.jsonc"
DEFAULT_CHARACTER_TYPES_PATH = Path(__file__).resolve().parents[2] / "data" / "character_types.jsonc"
DEFAULT_MEMORY_DIR = Path(__file__).resolve().parents[2] / "data" / "memories"


class MapLoadError(ValueError):
    """Raised when an authored map file cannot be loaded safely."""


class ObjectPersistenceError(ValueError):
    """Raised when persistent object state cannot be read or written safely."""


def _load_behavior(type_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MapLoadError(f"Character type {type_id!r} behavior must be an object")
    behavior = dict(value)
    for section in (
        "priorities",
        "diet",
        "senses",
        "sleep",
        "reproduction",
        "attacks",
        "abilities",
    ):
        if not isinstance(behavior.get(section), dict):
            raise MapLoadError(
                f"Character type {type_id!r} behavior {section!r} must be an object"
            )
    if not isinstance(behavior.get("predator"), bool):
        raise MapLoadError(
            f"Character type {type_id!r} behavior predator must be boolean"
        )
    senses = behavior["senses"]
    for field in ("hearing_distance", "smell_distance", "vision_distance"):
        if not isinstance(senses.get(field), (int, float)) or senses[field] < 0:
            raise MapLoadError(
                f"Character type {type_id!r} has invalid {field}"
            )
    reproduction = behavior["reproduction"]
    if not isinstance(reproduction.get("enabled"), bool):
        raise MapLoadError(
            f"Character type {type_id!r} reproduction enabled must be boolean"
        )
    return behavior


def load_character_types(
    path: Path = DEFAULT_CHARACTER_TYPES_PATH,
) -> dict[str, CharacterType]:
    try:
        catalog = loads_jsonc(path.read_text(encoding="utf-8"))
        defaults = dict(catalog.get("_defaults", {}))
        entries = {
            str(_required(entry, "id", "character type")): dict(entry)
            for entry in catalog.get("character_types", [])
        }
        if len(entries) != len(catalog.get("character_types", [])):
            raise MapLoadError("Character catalog contains duplicate type IDs")

        resolved_data: dict[str, dict[str, Any]] = {}

        def resolve(type_id: str, resolving: tuple[str, ...] = ()) -> dict[str, Any]:
            if type_id in resolved_data:
                return resolved_data[type_id]
            if type_id in resolving:
                cycle = " -> ".join((*resolving, type_id))
                raise MapLoadError(f"Character type inheritance cycle: {cycle}")
            entry = entries.get(type_id)
            if entry is None:
                raise MapLoadError(f"Unknown inherited character type {type_id!r}")
            parent_id = (
                str(entry["inherits"]) if entry.get("inherits") is not None else None
            )
            base = (
                resolve(parent_id, (*resolving, type_id))
                if parent_id is not None
                else defaults
            )
            resolved_data[type_id] = _merge_definition(base, entry)
            return resolved_data[type_id]

        definitions: dict[str, CharacterType] = {}
        for type_id in entries:
            entry = resolve(type_id)
            description = str(
                _required(entry, "description", f"character type {type_id!r}")
            )
            if not description.strip():
                raise MapLoadError(
                    f"Character type {type_id!r} description cannot be empty"
                )
            definitions[type_id] = CharacterType(
                type_id=type_id,
                name=str(entry.get("name", type_id.title())),
                description=description,
                inherits=(
                    str(entry["inherits"])
                    if entry.get("inherits") is not None
                    else None
                ),
                conditions={
                    str(stat_id): dict(config)
                    for stat_id, config in entry.get("conditions", {}).items()
                },
                secondary_stats={
                    str(stat_id): dict(config)
                    for stat_id, config in entry.get("secondary_stats", {}).items()
                },
                skills={
                    str(skill_id): dict(config)
                    for skill_id, config in entry.get("skills", {}).items()
                },
                behavior=_load_behavior(type_id, entry.get("behavior", {})),
            )
        return definitions
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise MapLoadError(f"Could not load character catalog {path}: {exc}") from exc


def _load_characters(
    map_data: dict[str, Any],
    objects: list[WorldObject],
    persistence_path: Path | None,
    character_types_path: Path,
) -> tuple[dict[int, CharacterState], int | None, dict[str, CharacterType]]:
    definitions = load_character_types(character_types_path)
    entries = map_data.get("characters", [])
    controlled_id = (
        int(map_data["controlled_character_id"])
        if map_data.get("controlled_character_id") is not None
        else None
    )
    if persistence_path is not None and persistence_path.exists():
        persisted = loads_jsonc(persistence_path.read_text(encoding="utf-8"))
        entries = persisted.get("characters") or entries
        if persisted.get("characters") and persisted.get("controlled_character_id") is not None:
            controlled_id = int(persisted["controlled_character_id"])
    object_by_id = {obj.object_id: obj for obj in objects}
    characters: dict[int, CharacterState] = {}
    for entry in entries:
        type_id = str(_required(entry, "type", "character instance"))
        definition = definitions.get(type_id)
        if definition is None:
            raise MapLoadError(f"Unknown character type {type_id!r}")
        character_id = int(_required(entry, "id", "character instance"))
        sleep_id = int(_required(entry, "last_sleep_id", "character instance"))
        sleep_object = object_by_id.get(sleep_id)
        if sleep_object is None or "Sleep" not in sleep_object.interactions:
            raise MapLoadError(
                f"Character {character_id} references invalid sleep object {sleep_id}"
            )
        conditions = {
            str(key): float(value)
            for key, value in entry.get("conditions", {}).items()
        }
        missing_conditions = (
            set(definition.conditions) - {"fatigue"} - set(conditions)
        )
        if missing_conditions:
            raise MapLoadError(
                f"Character {character_id} is missing starting conditions "
                f"{sorted(missing_conditions)!r}"
            )
        memory = {
            str(key): float(value)
            for key, value in entry.get("condition_memory", {}).items()
        }
        remembered_conditions = set(definition.conditions) - {"fatigue"}
        missing_memory = remembered_conditions - set(memory)
        if missing_memory:
            raise MapLoadError(
                f"Character {character_id} is missing condition memory "
                f"{sorted(missing_memory)!r}"
            )
        raw_skills = dict(entry.get("skills", {}))
        missing_skills = set(definition.skills) - set(raw_skills)
        if missing_skills:
            raise MapLoadError(
                f"Character {character_id} is missing skills "
                f"{sorted(missing_skills)!r}"
            )
        skills = {
            str(key): SkillState(
                level=int(value.get("level", 0)) if isinstance(value, dict) else int(value),
                experience=(
                    float(value.get("experience", 0.0))
                    if isinstance(value, dict)
                    else 0.0
                ),
            )
            for key, value in raw_skills.items()
        }
        characters[character_id] = CharacterState(
            character_id=character_id,
            type_id=type_id,
            name=str(entry.get("name", definition.name)),
            last_sleep_id=sleep_id,
            conditions=conditions,
            condition_memory=memory,
            skills=skills,
            used_nap_windows=set(entry.get("used_nap_windows", [])),
        )
    if controlled_id is not None and controlled_id not in characters:
        raise MapLoadError(f"Unknown controlled character {controlled_id}")
    return characters, controlled_id, definitions


def _character_payload(characters: dict[int, CharacterState]) -> list[dict[str, Any]]:
    return [
        {
            "id": character.character_id,
            "type": character.type_id,
            "name": character.name,
            "last_sleep_id": character.last_sleep_id,
            "conditions": character.conditions,
            "condition_memory": character.condition_memory,
            "skills": {
                skill_id: {
                    "level": skill.level,
                    "experience": skill.experience,
                }
                for skill_id, skill in character.skills.items()
            },
            "used_nap_windows": sorted(character.used_nap_windows),
        }
        for character in sorted(characters.values(), key=lambda value: value.character_id)
    ]


def _required(entry: dict[str, Any], field: str, entry_name: str) -> Any:
    if field not in entry:
        raise MapLoadError(f"{entry_name} is missing required field {field!r}")
    return entry[field]


def _load_authored_routine(
    entries: object, tile_size: int
) -> tuple[RoutineStep, ...]:
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise MapLoadError("cheat_memory must be an array")

    def point(value: object, field: str) -> tuple[float, float] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            try:
                return position_from_data(value).mapxy
            except ValueError as exc:
                raise MapLoadError(f"cheat_memory {field}: {exc}") from exc
        if not isinstance(value, list) or len(value) != 2:
            raise MapLoadError(f"cheat_memory {field} must contain two tile coordinates")
        return float(value[0]) * tile_size, float(value[1]) * tile_size

    def bounds(value: object, field: str) -> tuple[int, int, int, int] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            try:
                return bounds_from_position_data(value)
            except ValueError as exc:
                raise MapLoadError(f"cheat_memory {field}: {exc}") from exc
        if not isinstance(value, list) or len(value) != 4:
            raise MapLoadError(f"cheat_memory {field} must contain four tile coordinates")
        return tuple(round(float(component) * tile_size) for component in value)  # type: ignore[return-value]

    def areas(
        value: object, field: str
    ) -> tuple[tuple[int, int, int, int], ...] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise MapLoadError(f"cheat_memory {field} must be an array")
        return tuple(
            bound
            for entry in value
            if (bound := bounds(entry, field)) is not None
        )

    routine: list[RoutineStep] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MapLoadError(f"cheat_memory entry {index + 1} must be an object")
        action = entry.get("action")
        if not isinstance(action, str) or not action:
            raise MapLoadError(f"cheat_memory entry {index + 1} has an invalid action")
        routine.append(
            RoutineStep(
                target_id=int(entry["target_id"]) if entry.get("target_id") is not None else None,
                action=action,
                target_type=(
                    str(entry["target_type"])
                    if entry.get("target_type") is not None
                    else None
                ),
                area_bounds=bounds(entry.get("area_bounds"), "area_bounds"),
                quantity=(
                    int(entry["quantity"]) if entry.get("quantity") is not None else None
                ),
                target_point=point(entry.get("target_point"), "target_point"),
                secondary_bounds=bounds(
                    entry.get("secondary_bounds"), "secondary_bounds"
                ),
                source_areas=areas(entry.get("source_areas"), "source_areas"),
                target_areas=areas(entry.get("target_areas"), "target_areas"),
                target_build_memory=(
                    str(entry["target_build_memory"])
                    if entry.get("target_build_memory") is not None
                    else None
                ),
                max_game_minutes=(
                    int(entry["max_game_minutes"])
                    if entry.get("max_game_minutes") is not None
                    else None
                ),
                till_until_done=bool(entry.get("till_until_done", False)),
                nearest_to_player=bool(entry.get("nearest_to_player", False)),
            )
        )
    return tuple(routine)


def _routine_payload(
    routine: list[RoutineStep] | tuple[RoutineStep, ...], tile_size: int
) -> list[dict[str, Any]]:
    def point(
        value: tuple[float, float] | None,
    ) -> dict[str, list[float | int]] | None:
        return mapxy_as_position_data(value) if value is not None else None

    def bounds(
        value: tuple[int, int, int, int] | None,
    ) -> dict[str, dict[str, list[float | int]]] | None:
        return bounds_as_position_data(value) if value is not None else None

    entries: list[dict[str, Any]] = []
    for step in routine:
        entry: dict[str, Any] = {"action": step.action}
        optional = {
            "target_id": step.target_id,
            "target_type": step.target_type,
            "area_bounds": bounds(step.area_bounds),
            "quantity": step.quantity,
            "target_point": point(step.target_point),
            "secondary_bounds": bounds(step.secondary_bounds),
            "source_areas": (
                [bounds(area) for area in step.source_areas]
                if step.source_areas is not None
                else None
            ),
            "target_areas": (
                [bounds(area) for area in step.target_areas]
                if step.target_areas is not None
                else None
            ),
            "target_build_memory": step.target_build_memory,
            "max_game_minutes": step.max_game_minutes,
            "till_until_done": step.till_until_done or None,
            "nearest_to_player": step.nearest_to_player or None,
        }
        entry.update((key, value) for key, value in optional.items() if value is not None)
        entries.append(entry)
    return entries


def memory_file_path(
    name: str, directory: Path = DEFAULT_MEMORY_DIR
) -> Path:
    cleaned = name.strip()
    if cleaned.lower().endswith(".jsonc"):
        cleaned = cleaned[:-6]
    elif cleaned.lower().endswith(".memory"):
        cleaned = cleaned[:-7]
    if (
        not cleaned
        or cleaned in {".", ".."}
        or any(character in cleaned for character in '<>:"/\\|?*')
    ):
        raise MapLoadError("Memory name must be a safe filename")
    return directory / f"{cleaned}.jsonc"


def save_memory_file(
    name: str,
    routine: list[RoutineStep] | tuple[RoutineStep, ...],
    *,
    tile_size: int,
    directory: Path = DEFAULT_MEMORY_DIR,
) -> Path:
    path = memory_file_path(name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_schema_version": 1,
        "name": path.stem,
        "commands": _routine_payload(routine, tile_size),
    }
    path.write_text(
        json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def load_memory_file(
    name: str,
    *,
    tile_size: int,
    directory: Path = DEFAULT_MEMORY_DIR,
) -> tuple[RoutineStep, ...]:
    path = memory_file_path(name, directory)
    if not path.is_file():
        legacy_path = path.with_suffix(".memory")
        if legacy_path.is_file():
            path = legacy_path
        else:
            raise MapLoadError(f"Command-set file does not exist: {path}")
    try:
        payload = loads_jsonc(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MapLoadError(f"Memory file must contain an object: {path}")
        if payload.get("_schema_version") != 1:
            raise MapLoadError(
                f"Memory file has an unsupported schema version: {path}"
            )
        return _load_authored_routine(payload.get("commands"), tile_size)
    except (OSError, json.JSONDecodeError) as exc:
        raise MapLoadError(f"Could not load memory file {path}: {exc}") from exc


def load_remembered_routine(
    path: Path = DEFAULT_CURRENT_LEVEL_PATH, tile_size: int = 32
) -> tuple[RoutineStep, ...]:
    if not path.exists():
        return ()
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        return _load_authored_routine(data.get("remembered_routine"), tile_size)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise ObjectPersistenceError(
            f"Could not load remembered routine from {path}: {exc}"
        ) from exc


def _load_build_memories(data: object) -> dict[str, BuildMemory]:
    if data is None:
        return {}
    if not isinstance(data, list):
        raise MapLoadError("build_memories must be an array")
    memories: dict[str, BuildMemory] = {}
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise MapLoadError(f"build_memories entry {index + 1} must be an object")
        memory_id = str(_required(entry, "id", "build memory"))
        memories[memory_id] = BuildMemory(
            memory_id=memory_id,
            object_type=str(_required(entry, "object_type", "build memory")),
            column=int(_required(entry, "x", "build memory")),
            row=int(_required(entry, "y", "build memory")),
            orientation=str(entry.get("orientation", "E/W")),
            quality=max(1, min(100, int(entry.get("quality", 20)))),
            build_count=max(0, int(entry.get("build_count", 0))),
            persistent=bool(entry.get("persistent", False)),
            persistence_modifier=(
                float(entry["persistence_modifier"])
                if entry.get("persistence_modifier") is not None
                else None
            ),
            state=dict(entry.get("state", {})),
        )
    return memories


def _build_memory_payload(
    memories: dict[str, BuildMemory] | None,
) -> list[dict[str, Any]]:
    return [
        {
            "id": memory.memory_id,
            "object_type": memory.object_type,
            "x": memory.column,
            "y": memory.row,
            "orientation": memory.orientation,
            "quality": memory.quality,
            "build_count": memory.build_count,
            "persistent": memory.persistent,
            "persistence_modifier": memory.persistence_modifier,
            "state": memory.state,
        }
        for memory in sorted((memories or {}).values(), key=lambda item: item.memory_id)
    ]


def load_map(
    path: Path = DEFAULT_MAP_PATH,
    persistence_path: Path | None = DEFAULT_CURRENT_LEVEL_PATH,
    object_types_path: Path = DEFAULT_OBJECT_TYPES_PATH,
    tile_types_path: Path = DEFAULT_TILE_TYPES_PATH,
    character_types_path: Path = DEFAULT_CHARACTER_TYPES_PATH,
    day_number: int = 1,
    reset_for_morning: bool = False,
) -> MapDefinition:
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        object_types = load_object_types(object_types_path)
        (
            tile_spawn_seed,
            tile_spawn_chances,
            tile_spawn_influences,
            tile_sprite_overlays,
            till_progress_per_action,
            soil_persistence_gain,
            reverted_till_progress_range,
            tile_persistence_modifier_range,
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
        boundaries = [_boundary_from_entry(entry) for entry in data.get("boundaries", [])]
        objects = [_object_from_instance(entry, object_types, tile_size) for entry in _required(data, "objects", "map")]
        objects.extend(
            _generate_trees(
                data,
                structures,
                terrain,
                objects,
                map_width,
                map_height,
                object_types,
                tile_size,
            )
        )
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
        _apply_boundaries(tile_map, boundaries)
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
        boundary_ids: set[str] = set()
        occupied_edges: dict[tuple[int, int, str], str] = {}
        for boundary in boundaries:
            if boundary.boundary_id in boundary_ids:
                raise MapLoadError(f"Map contains duplicate boundary ID {boundary.boundary_id!r}")
            boundary_ids.add(boundary.boundary_id)
            if boundary.kind not in {"wall", "fence", "door"}:
                raise MapLoadError(f"Boundary {boundary.boundary_id!r} has invalid type {boundary.kind!r}")
            if boundary.edge not in {"north", "west"}:
                raise MapLoadError(f"Boundary {boundary.boundary_id!r} has invalid edge {boundary.edge!r}")
            if boundary.kind == "door" and boundary.swing not in {
                "clockwise",
                "counterclockwise",
            }:
                raise MapLoadError(
                    f"Door {boundary.boundary_id!r} has invalid swing {boundary.swing!r}"
                )
            valid = (
                boundary.edge == "north"
                and 0 <= boundary.column < tile_map.columns
                and 0 <= boundary.row <= tile_map.rows
            ) or (
                boundary.edge == "west"
                and 0 <= boundary.column <= tile_map.columns
                and 0 <= boundary.row < tile_map.rows
            )
            if not valid:
                raise MapLoadError(f"Boundary {boundary.boundary_id!r} is outside the map")
            address = (boundary.column, boundary.row, boundary.edge)
            if address in occupied_edges:
                raise MapLoadError(
                    f"Boundaries {occupied_edges[address]!r} and {boundary.boundary_id!r} occupy the same edge"
                )
            occupied_edges[address] = boundary.boundary_id
        if len(object_ids) != len(set(object_ids)):
            raise MapLoadError("Map contains duplicate object IDs")
        characters, controlled_character_id, character_types = _load_characters(
            data, objects, persistence_path, character_types_path
        )
        for obj in objects:
            if obj.object_id <= 0:
                raise MapLoadError("Object IDs must be positive integers")
            if obj.orientation not in {"N", "E", "S", "W", "N/S", "E/W"}:
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
            boundaries=boundaries,
            object_types=object_types,
            tile_states=tile_states,
            tile_sprite_overlays=tile_sprite_overlays,
            cheat_memory=_load_authored_routine(data.get("cheat_memory"), tile_size),
            remembered_routine=(
                load_remembered_routine(persistence_path, tile_size)
                if persistence_path is not None
                else ()
            ),
            build_memories=(
                _load_build_memories(
                    loads_jsonc(persistence_path.read_text(encoding="utf-8")).get(
                        "build_memories"
                    )
                )
                if persistence_path is not None and persistence_path.exists()
                else {}
            ),
            till_progress_per_action=till_progress_per_action,
            soil_persistence_gain=soil_persistence_gain,
            reverted_till_progress_range=reverted_till_progress_range,
            tile_persistence_modifier_range=tile_persistence_modifier_range,
            characters=characters,
            controlled_character_id=controlled_character_id,
            character_types=character_types,
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
        map_data = loads_jsonc(map_path.read_text(encoding="utf-8"))
        existing_memory = (
            loads_jsonc(current_level_path.read_text(encoding="utf-8")).get(
                "remembered_routine", []
            )
            if current_level_path.exists()
            else []
        )
        existing_build_memories = (
            loads_jsonc(current_level_path.read_text(encoding="utf-8")).get(
                "build_memories", []
            )
            if current_level_path.exists()
            else []
        )
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
                "variant": instance.variant,
                "form": instance.form,
                "flavor": instance.flavor,
                "container": instance.container,
            }
            entries.append(
                {
                    "id": instance.object_id,
                    "type": instance.type_id,
                    "persistent_state": {"persistent": True, **state},
                    "current_state": dict(state),
                }
            )
        payload = {
            "level_map": map_path.name,
            "remembered_routine": existing_memory,
            "build_memories": existing_build_memories,
            "controlled_character_id": map_data.get("controlled_character_id"),
            "characters": map_data.get("characters", []),
            "objects": entries,
            "tiles": [],
        }
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
        map_data = loads_jsonc(map_path.read_text(encoding="utf-8"))
        current_data = (
            loads_jsonc(current_level_path.read_text(encoding="utf-8"))
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
                "variant": instance.variant,
                "form": instance.form,
                "flavor": instance.flavor,
                "container": instance.container,
            }
            current_by_id[instance.object_id] = {
                "id": instance.object_id,
                "type": instance.type_id,
                "persistent_state": {"persistent": persistent, **state},
                "current_state": dict(state),
            }
        payload = {
            "level_map": map_path.name,
            "remembered_routine": current_data.get("remembered_routine", []),
            "build_memories": current_data.get("build_memories", []),
            "quest_state": current_data.get("quest_state", {}),
            "controlled_character_id": current_data.get(
                "controlled_character_id", map_data.get("controlled_character_id")
            ),
            "characters": current_data.get(
                "characters", map_data.get("characters", [])
            ),
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


class ObjectTypeCatalog(dict[str, ObjectType]):
    """Object definitions with the JSONC-specified fallback for unknown IDs."""

    def __init__(self, definitions: dict[str, ObjectType], defaults: dict[str, Any]):
        super().__init__(definitions)
        self.defaults = defaults

    def __missing__(self, type_id: str) -> ObjectType:
        definition = _load_object_type({"id": type_id}, self.defaults)
        self[type_id] = definition
        return definition

    def get(self, key: str, default: ObjectType | None = None) -> ObjectType | None:
        if default is not None:
            return super().get(key, default)
        return self[key]


def _merge_definition(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge definition objects recursively; arrays and scalar values replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_definition(merged[key], value)
        else:
            merged[key] = value
    return merged


def _title_from_id(type_id: str) -> str:
    return type_id.replace("_", " ").title()


def _load_object_type(entry: dict[str, Any], defaults: dict[str, Any]) -> ObjectType:
    type_id = str(_required(entry, "id", "object type"))
    inherited = _merge_definition(defaults, {k: v for k, v in entry.items() if k != "forms"})
    raw_forms = entry.get("forms")
    if raw_forms:
        forms = {
            str(form_id): _load_object_form(
                str(form_id), _merge_definition(inherited, form_entry), type_id
            )
            for form_id, form_entry in raw_forms.items()
        }
        # Form choice belongs to the entity. The first form is only the catalog's
        # lookup fallback for existing callers that are not resolving an instance.
        default_form = next(iter(forms))
    else:
        default_form = ""
        forms = {"": _load_object_form("", inherited, type_id)}

    variants = tuple(str(variant_id) for variant_id in inherited.get("variants", []))
    default_variant = (
        str(inherited["default_variant"]) if inherited.get("default_variant") else None
    )
    if default_variant is not None and default_variant not in variants:
        raise MapLoadError(
            f"Object type {type_id!r} has unknown default variant {default_variant!r}"
        )
    raw_name = inherited.get("name") or _title_from_id(type_id)
    if isinstance(raw_name, dict):
        name = str(raw_name.get("default") or _title_from_id(type_id))
        variant_names = {
            str(variant_id): str(value)
            for variant_id, value in raw_name.items()
            if variant_id != "default"
        }
    else:
        name = str(raw_name)
        variant_names = {}
    kind_name = str(inherited.get("kind", "OBJECT"))
    kind = ObjectKind.__members__.get(kind_name, ObjectKind.OBJECT)
    return ObjectType(
        type_id=type_id,
        name=name,
        variant_names=variant_names,
        kind=kind,
        default_form=default_form,
        forms=forms,
        variants=variants,
        default_variant=default_variant,
        state_fields=tuple(str(field) for field in inherited.get("state_fields", [])),
        state_defaults={
            str(field): value
            for field, value in inherited.get("state_defaults", {}).items()
        },
        growth=dict(inherited.get("growth", {})),
    )


def load_object_types(path: Path = DEFAULT_OBJECT_TYPES_PATH) -> dict[str, ObjectType]:
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        schema_version = int(_required(data, "_schema_version", "object catalog"))
        if schema_version != 1:
            raise MapLoadError(
                f"Unsupported object catalog schema version {schema_version}"
            )
        defaults = dict(_required(data, "_defaults", "object catalog"))
        definitions = []
        for entry in _required(data, "object_types", "object catalog"):
            definitions.append(_load_object_type(entry, defaults))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MapLoadError(f"Could not load object catalog {path}: {exc}") from exc
    if len({definition.type_id for definition in definitions}) != len(definitions):
        raise MapLoadError("Object catalog contains duplicate type IDs")
    valid_tile_kinds = {kind.value for kind in TileKind}
    for definition in definitions:
        for form in definition.forms.values():
            if not set(form.spawn_tiles) <= valid_tile_kinds:
                raise MapLoadError(
                    f"Object type {definition.type_id!r} contains an invalid spawn tile"
                )
    return ObjectTypeCatalog(
        {definition.type_id: definition for definition in definitions}, defaults
    )


def _load_object_form(form_id: str, entry: dict[str, Any], type_id: str) -> ObjectForm:
    footprint = entry.get("footprint", [1, 1])
    if (
        not isinstance(footprint, list)
        or len(footprint) != 2
        or any(int(value) < 1 for value in footprint)
    ):
        raise MapLoadError(
            f"Form {form_id!r} on object type {type_id!r} has an invalid footprint"
        )
    condition_recovery = {
        str(condition_id): float(value)
        for condition_id, value in entry.get("condition_recovery", {}).items()
    }
    unknown_conditions = set(condition_recovery) - {
        "trauma", "hunger", "thirst", "fatigue"
    }
    if unknown_conditions:
        raise MapLoadError(
            f"Object type {type_id!r} has unknown condition recovery "
            f"{sorted(unknown_conditions)!r}"
        )
    return ObjectForm(
        form_id=form_id,
        # A name dictionary belongs to the object/variant layer. Forms only
        # override the display name when they provide a literal string.
        name=str(entry["name"]) if isinstance(entry.get("name"), str) else None,
        descriptions=_quality_descriptions(entry),
        footprint=(int(footprint[0]), int(footprint[1])),
        blocks_movement=bool(entry.get("blocks_movement", False)),
        blocks_vision=bool(entry.get("blocks_vision", False)),
        mobility=str(entry.get("mobility", "fixed")),
        traits=tuple(str(trait) for trait in entry.get("traits", [])),
        interactions=_load_interactions(entry.get("interactions", {}), type_id, form_id),
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
        capacity={
            str(resource): (
                {str(stage): int(amount) for stage, amount in value.items()}
                if isinstance(value, dict)
                else int(value)
            )
            for resource, value in entry.get("capacity", {}).items()
        },
        nutrition=max(0, int(entry.get("nutrition", 0))),
        condition_recovery=condition_recovery,
        build_duration_seconds=float(entry.get("build_duration_seconds", 0.0)),
        persistence={
            str(policy_id): _load_persistence_policy(policy, type_id, str(policy_id))
            for policy_id, policy in entry.get("persistence", {}).items()
        },
        states=tuple(str(state_id) for state_id in entry.get("states", [])),
        sprite_overlays=_load_sprite_overlays(
            entry.get("sprite_overlays", []), f"object type {type_id!r}/{form_id!r}"
        ),
    )


def _load_sprite_overlays(
    entries: object, owner: str
) -> tuple[SpriteOverlay, ...]:
    if not isinstance(entries, list):
        raise MapLoadError(f"sprite_overlays on {owner} must be an array")
    overlays: list[SpriteOverlay] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MapLoadError(f"Each sprite overlay on {owner} must be an object")
        overlay_id = str(_required(entry, "id", f"sprite overlay on {owner}"))
        state_field = str(
            _required(entry, "state_field", f"sprite overlay {overlay_id!r}")
        )
        if not overlay_id.replace("_", "").replace("-", "").isalnum():
            raise MapLoadError(f"Invalid sprite overlay id {overlay_id!r} on {owner}")
        if overlay_id in seen:
            raise MapLoadError(f"Duplicate sprite overlay {overlay_id!r} on {owner}")
        seen.add(overlay_id)
        value_range = entry.get("value_range", [0.0, None])
        alpha_range = entry.get("alpha_range", [0, 255])
        if (
            not isinstance(value_range, list)
            or len(value_range) != 2
            or not isinstance(alpha_range, list)
            or len(alpha_range) != 2
        ):
            raise MapLoadError(f"Invalid ranges for sprite overlay {overlay_id!r}")
        alpha_min, alpha_max = int(alpha_range[0]), int(alpha_range[1])
        if not 0 <= alpha_min <= 255 or not 0 <= alpha_max <= 255:
            raise MapLoadError(f"Invalid alpha range for sprite overlay {overlay_id!r}")
        overlays.append(
            SpriteOverlay(
                overlay_id=overlay_id,
                state_field=state_field,
                value_min=float(value_range[0]),
                value_max=(
                    float(value_range[1]) if value_range[1] is not None else None
                ),
                capacity_resource=(
                    str(entry["capacity_resource"])
                    if entry.get("capacity_resource") is not None
                    else None
                ),
                alpha_min=alpha_min,
                alpha_max=alpha_max,
            )
        )
    return tuple(overlays)


def _load_persistence_policy(
    entry: object, type_id: str, policy_id: str
) -> PersistencePolicy:
    if not isinstance(entry, dict):
        raise MapLoadError(
            f"Persistence policy {policy_id!r} on {type_id!r} must be an object"
        )
    modifier = entry.get("modifier_range", [1.0, 1.0])
    if (
        not isinstance(modifier, list)
        or len(modifier) != 2
        or float(modifier[0]) <= 0
        or float(modifier[1]) < float(modifier[0])
    ):
        raise MapLoadError(
            f"Persistence policy {policy_id!r} on {type_id!r} has an invalid modifier_range"
        )
    chance = float(_required(entry, "chance_per_count", "persistence policy"))
    decay = float(_required(entry, "decay_chance", "persistence policy"))
    if not 0 <= chance <= 1 or not 0 <= decay <= 1:
        raise MapLoadError(
            f"Persistence policy {policy_id!r} on {type_id!r} has invalid probabilities"
        )
    return PersistencePolicy(
        chance, decay, (float(modifier[0]), float(modifier[1]))
    )


def _quality_descriptions(entry: dict[str, Any]) -> dict[str, str]:
    stages = ("ruined", "damaged", "worn", "good", "fine")
    descriptions = {
        str(stage): str(text)
        for stage, text in entry.get("descriptions", {}).items()
    }
    if descriptions and set(descriptions) != set(stages):
        raise MapLoadError("Every object form must describe every quality stage")
    return descriptions


def _load_interactions(
    groups: dict[str, Any], type_id: str, form_id: str
) -> dict[str, dict[str, object]]:
    actions: dict[str, dict[str, object]] = {}
    for group, entries in groups.items():
        if not isinstance(entries, list):
            raise MapLoadError(
                f"Interaction group {group!r} on {type_id!r} must be an array"
            )
        for entry in entries:
            action_id = str(_required(entry, "id", f"interaction group {group!r}"))
            label = str(entry.get("label") or _title_from_id(action_id))
            if label in actions:
                raise MapLoadError(
                    f"Duplicate interaction label {label!r} on {type_id!r}/{form_id!r}"
                )
            actions[label] = {
                **dict(entry),
                "id": action_id,
                "group": str(group),
            }
    return actions


def load_tile_spawn_rules(
    path: Path, object_types: dict[str, ObjectType]
) -> tuple[
    int,
    dict[TileKind, dict[str, float]],
    dict[TileKind, tuple[tuple[str, float, int, float], ...]],
    dict[str, tuple[SpriteOverlay, ...]],
    float,
    float,
    tuple[float, float],
    tuple[float, float],
]:
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        rules: dict[TileKind, dict[str, float]] = {}
        influences: dict[TileKind, tuple[tuple[str, float, int, float], ...]] = {}
        sprite_overlays: dict[str, tuple[SpriteOverlay, ...]] = {}
        for kind in TileKind:
            entry = _required(_required(data, "tile_types", "tile catalog"), kind.value, "tile type")
            chances = {str(type_id): float(chance) for type_id, chance in entry.get("spawn_chances", {}).items()}
            for type_id, chance in chances.items():
                object_types[type_id]
                if not 0 <= chance <= 1:
                    raise MapLoadError(f"Invalid spawn chance for {type_id!r} on {kind.value}")
            rules[kind] = chances
            sprite_overlays[kind.value] = _load_sprite_overlays(
                entry.get("sprite_overlays", []), f"tile type {kind.value!r}"
            )
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
                object_types[type_id]
                if not 0 <= chance <= 1 or distance < 1 or not 0 <= decay <= 1:
                    raise MapLoadError(f"Invalid spawn influence for {type_id!r} on {kind.value}")
        grassland = _required(_required(data, "tile_types", "tile catalog"), "grassland", "tile type")
        tilling = _required(grassland, "tilling", "grassland tile type")
        if str(_required(tilling, "result", "grassland tilling")) != TileKind.SOIL.value:
            raise MapLoadError("Grassland tilling must currently result in soil")
        required_fields = {
            "till_percentage",
            "tilled_today",
            "soil_persistence_percentage",
            "kind_override",
            "persistence_modifier",
        }
        if set(_required(tilling, "tracked_fields", "grassland tilling")) != required_fields:
            raise MapLoadError("Grassland tilling must declare all tracked tile-instance fields")
        permanent_chance = float(tilling.get("progress_per_till", 5.0))
        persistence_gain = float(
            tilling.get("persistence_gain_per_conversion", 1.0)
        )
        reverted_range = tilling.get(
            "reverted_till_progress_range", [80.0, 100.0]
        )
        modifier = tilling.get("persistence_modifier_range", [1.0, 1.0])
        if (
            not isinstance(modifier, list)
            or len(modifier) != 2
            or float(modifier[0]) <= 0
            or float(modifier[1]) < float(modifier[0])
        ):
            raise MapLoadError("Grassland tilling has an invalid persistence_modifier_range")
        if not 0 < permanent_chance <= 100:
            raise MapLoadError("Tilling progress is invalid")
        if (
            not 0 < persistence_gain <= 100
            or not isinstance(reverted_range, list)
            or len(reverted_range) != 2
            or not 0 <= float(reverted_range[0]) <= float(reverted_range[1]) <= 100
        ):
            raise MapLoadError("Soil persistence settings are invalid")
        return (
            int(data.get("seed", 0)),
            rules,
            influences,
            sprite_overlays,
            permanent_chance,
            persistence_gain,
            (float(reverted_range[0]), float(reverted_range[1])),
            (float(modifier[0]), float(modifier[1])),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MapLoadError):
            raise
        raise MapLoadError(f"Could not load tile catalog {path}: {exc}") from exc


def _object_from_instance(
    entry: dict[str, Any], object_types: dict[str, ObjectType], tile_size: int = 32
) -> WorldObject:
    type_id = str(_required(entry, "type", "object instance"))
    definition = object_types[type_id]
    live = entry
    variant = str(live["variant"]) if live.get("variant") else definition.default_variant
    if variant is not None and variant not in definition.variants:
        raise MapLoadError(f"Object instance references unknown variant {variant!r}")
    raw_form = live.get("form")
    form_id = (
        definition.default_form
        if raw_form is None or str(raw_form) in {"", "None"}
        else str(raw_form)
    )
    if form_id not in definition.forms:
        raise MapLoadError(f"Object instance references unknown form {form_id!r}")
    form = definition.form_definition(form_id, variant)
    quality = int(
        live.get(
            "quality",
            live.get("condition", entry.get("quality", entry.get("condition", 100))),
        )
    )
    orientation = _normalize_orientation(str(live.get("orientation", entry.get("orientation", "E/W"))))
    footprint_width, footprint_height = form.footprint
    if orientation in {"N", "S"}:
        footprint_width, footprint_height = footprint_height, footprint_width
    obj = WorldObject(
        object_id=int(_required(entry, "id", "object instance")),
        name=form.name or definition.name_for(variant),
        kind=definition.kind,
        x=int(_required(live, "x", "object instance")) * tile_size,
        y=int(_required(live, "y", "object instance")) * tile_size,
        width=footprint_width * tile_size,
        height=footprint_height * tile_size,
        active=bool(live.get("active", True)),
        state={**definition.state_defaults, **dict(live.get("state", {}))},
        blocks_movement=form.blocks_movement,
        blocks_vision=form.blocks_vision,
        mobility=form.mobility,
        traits=form.traits,
        persistent=False,
        descriptions=dict(form.descriptions),
        interactions={action: dict(details) for action, details in form.interactions.items()},
        capacity=form.capacity_for(quality),
        nutrition=form.nutrition,
        condition_recovery=dict(form.condition_recovery),
        type_id=type_id,
        orientation=orientation,
        quality=quality,
        variant=variant,
        form=form_id,
        flavor=str(live["flavor"]) if live.get("flavor") else None,
        container=str(live["container"]) if live.get("container") else None,
    )
    return obj


def _normalize_orientation(value: str) -> str:
    legacy = {
        "n/s": "N", "e/w": "E", "north": "N", "east": "E",
        "south": "S", "west": "W",
    }
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
    authored_ids = {obj.object_id for obj in objects}
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        saved = []
        for entry in _required(data, "objects", "persistence"):
            normalized = dict(entry)
            if "current_state" in entry:
                persistent_state = dict(entry.get("persistent_state", {}))
                is_persistent = bool(persistent_state.get("persistent", False))
                if (
                    reset_for_morning
                    and not is_persistent
                    and int(entry["id"]) not in authored_ids
                ):
                    continue
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
                    state=dict(persistent_state.get("state", loaded.state)),
                    persistent=is_persistent,
                    variant=(
                        str(persistent_state["variant"])
                        if persistent_state.get("variant")
                        else loaded.variant
                    ),
                        form=(
                            str(persistent_state["form"])
                            if persistent_state.get("form") not in {None, "", "None"}
                            else loaded.form
                        ),
                    flavor=(
                        str(persistent_state["flavor"])
                        if persistent_state.get("flavor")
                        else loaded.flavor
                    ),
                    container=(
                        str(persistent_state["container"])
                        if persistent_state.get("container")
                        else loaded.container
                    ),
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
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        states: dict[tuple[int, int], LevelTileState] = {}
        for entry in data.get("tiles", []):
            column = int(_required(entry, "x", "tile state"))
            row = int(_required(entry, "y", "tile state"))
            tile = tile_map.tile_at(column, row)
            if tile is None:
                raise MapLoadError(f"Tile state ({column}, {row}) is outside the level")
            state = LevelTileState(
                column=column,
                row=row,
                till_percentage=max(
                    0.0, min(100.0, float(entry.get("till_percentage", 0.0)))
                ),
                tilled_today=bool(entry.get("tilled_today", False)),
                soil_persistence_percentage=max(
                    0.0,
                    min(
                        100.0,
                        float(entry.get("soil_persistence_percentage", 0.0)),
                    ),
                ),
                kind_override=(
                    str(entry["kind_override"])
                    if entry.get("kind_override")
                    else None
                ),
                persistence_modifier=(
                    float(entry["persistence_modifier"])
                    if entry.get("persistence_modifier") is not None
                    else None
                ),
            )
            if state.kind_override not in {None, TileKind.SOIL.value}:
                raise MapLoadError(f"Tile state ({column}, {row}) has an invalid kind override")
            if state.kind_override == TileKind.SOIL.value:
                tile.kind = TileKind.SOIL
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
    reverted_till_progress_range: tuple[float, float],
    persistence_modifier_range: tuple[float, float] = (1.0, 1.0),
) -> None:
    randomizer = random.Random(91_003 + day_number)
    for key in sorted(list(states)):
        state = states[key]
        if state.persistence_modifier is None:
            state.persistence_modifier = random.Random(
                f"remembering:persistence-modifier:tile:{state.column}:{state.row}"
            ).uniform(*persistence_modifier_range)
        if state.kind_override == TileKind.SOIL.value:
            remains_soil = (
                randomizer.random() * 100.0
                < state.soil_persistence_percentage
            )
            if not remains_soil:
                state.kind_override = None
                state.till_percentage = randomizer.uniform(
                    *reverted_till_progress_range
                )
        state.tilled_today = False
        if state.till_percentage <= 0 and state.kind_override is None:
            del states[key]


def save_persistent_objects(
    objects: dict[int, WorldObject],
    path: Path = DEFAULT_CURRENT_LEVEL_PATH,
    tile_size: int = 32,
    tile_states: dict[tuple[int, int], LevelTileState] | None = None,
    remembered_routine: list[RoutineStep] | tuple[RoutineStep, ...] | None = None,
    build_memories: dict[str, BuildMemory] | None = None,
    quest_state: dict[str, object] | None = None,
    characters: dict[int, CharacterState] | None = None,
    controlled_character_id: int | None = None,
) -> None:
    entries = []
    for obj in objects.values():
        if obj.daily_spawned:
            continue
        if (
            isinstance(obj.state, dict)
            and obj.state.get("build_memory_id")
            and not obj.persistent
        ):
            continue
        state = obj.persistent_state if obj.persistent else ObjectState(
            obj.x, obj.y, obj.orientation, obj.quality, obj.active, obj.state,
            variant=obj.variant, form=obj.form, flavor=obj.flavor,
            container=obj.container,
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
            "variant": baseline.variant,
            "form": baseline.form,
            "flavor": baseline.flavor,
            "container": baseline.container,
        }
        entry["current_state"] = {
            "x": round(obj.x / tile_size),
            "y": round(obj.y / tile_size),
            "orientation": obj.orientation,
            "quality": obj.quality,
            "active": obj.active,
            "state": obj.state,
            "variant": obj.variant,
            "form": obj.form,
            "flavor": obj.flavor,
            "container": obj.container,
        }
        entries.append(entry)
    existing_data: dict[str, Any] = {}
    if path.exists():
        try:
            existing_data = loads_jsonc(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_data = {}
    if tile_states is None:
        try:
            existing_tiles = existing_data.get("tiles", [])
        except AttributeError:
            existing_tiles = []
    else:
        existing_tiles = [
            {
                "x": state.column,
                "y": state.row,
                "till_percentage": state.till_percentage,
                "tilled_today": state.tilled_today,
                "soil_persistence_percentage": state.soil_persistence_percentage,
                "kind_override": state.kind_override,
                "persistence_modifier": state.persistence_modifier,
            }
            for state in sorted((tile_states or {}).values(), key=lambda value: (value.row, value.column))
        ]
    payload = {
        "level_map": DEFAULT_MAP_PATH.name,
        "remembered_routine": (
            _routine_payload(remembered_routine, tile_size)
            if remembered_routine is not None
            else existing_data.get("remembered_routine", [])
        ),
        "build_memories": (
            _build_memory_payload(build_memories)
            if build_memories is not None
            else existing_data.get("build_memories", [])
        ),
        "quest_state": (
            quest_state
            if quest_state is not None
            else existing_data.get("quest_state", {})
        ),
        "controlled_character_id": (
            controlled_character_id
            if controlled_character_id is not None
            else existing_data.get("controlled_character_id")
        ),
        "characters": (
            _character_payload(characters)
            if characters is not None
            else existing_data.get("characters", [])
        ),
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
        if not source.active or source.container is not None or definition is None:
            continue
        source_form = definition.form_definition(source.form, source.variant)
        located = tile_map.tile_at_world(*source.center)
        if located is None:
            continue
        source_column, source_row, _ = located
        for type_id, chance, distance, decay in source_form.spawn_influence:
            object_types[type_id]
            if not 0 <= chance <= 1 or distance < 1 or not 0 <= decay <= 1:
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
                form = definition.form_definition(variant=definition.default_variant)
                if form.spawn_tiles and tile.kind.value not in form.spawn_tiles:
                    continue
                if randomizer.random() >= chance:
                    continue
                center_x, center_y = tile_map.tile_center(column, row)
                spawned.append(
                    WorldObject(
                        object_id=next_id + len(spawned),
                        name=form.name or definition.name_for(definition.default_variant),
                        kind=definition.kind,
                        x=round(center_x - form.footprint[0] * tile_map.tile_size / 2),
                        y=round(center_y - form.footprint[1] * tile_map.tile_size / 2),
                        width=form.footprint[0] * tile_map.tile_size,
                        height=form.footprint[1] * tile_map.tile_size,
                        blocks_movement=form.blocks_movement,
                        blocks_vision=form.blocks_vision,
                        mobility=form.mobility,
                        traits=form.traits,
                        state=dict(definition.state_defaults),
                        persistent=False,
                        descriptions=dict(form.descriptions),
                        interactions={
                            action: dict(details)
                            for action, details in form.interactions.items()
                        },
                        capacity=form.capacity_for(100),
                        nutrition=form.nutrition,
                        condition_recovery=dict(form.condition_recovery),
                        type_id=type_id,
                        quality=100,
                        daily_spawned=True,
                        variant=definition.default_variant,
                        form=definition.default_form,
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
    tile_size: int,
) -> list[WorldObject]:
    config = data.get("tree_generation")
    if config is None:
        return []
    bed = next((obj for obj in authored_objects if obj.type_id == "bed"), None)
    if bed is None:
        raise MapLoadError("Tree generation requires an object with ID 'bed'")

    count = int(_required(config, "count", "tree_generation"))
    tree_form = object_types["tree"].form_definition()
    legacy_scale = tile_size / 32.0
    tree_width = tree_form.footprint[0] * tile_size
    tree_height = tree_form.footprint[1] * tile_size
    clear_radius = float(config.get("homestead_clear_radius", 420)) * legacy_scale
    density_power = float(config.get("density_power", 1.8))
    min_spacing = float(config.get("min_spacing", 38)) * legacy_scale
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
        placement_margin = round(20 * legacy_scale)
        x = randomizer.randint(
            placement_margin, map_width - tree_width - placement_margin
        )
        y = randomizer.randint(
            placement_margin, map_height - tree_height - placement_margin
        )
        center_x = x + tree_width / 2
        center_y = y + tree_height / 2
        distance = math.hypot(center_x - origin_x, center_y - origin_y)
        if distance < clear_radius:
            continue
        density = min(1.0, distance / max_distance) ** density_power
        if randomizer.random() > density:
            continue
        if any(
            _rectangles_overlap(
                x,
                y,
                tree_width,
                tree_height,
                room.x,
                room.y,
                room.width,
                room.height,
                24 * legacy_scale,
            )
            for room in structures
        ):
            continue
        if any(
            feature.kind != "dense_forest"
            and (
                (feature.kind == "pond" and _point_in_polygon(center_x, center_y, feature.points))
                or terrain_blocks_point(
                    feature,
                    center_x,
                    center_y,
                    max(tree_width, tree_height) / 2 + 10 * legacy_scale,
                )
            )
            for feature in terrain
        ):
            continue
        if any(
            _rectangles_overlap(
                x,
                y,
                tree_width,
                tree_height,
                obj.x,
                obj.y,
                obj.width,
                obj.height,
                10 * legacy_scale,
            )
            for obj in authored_objects
        ):
            continue
        if any(math.hypot(center_x - tree.center[0], center_y - tree.center[1]) < min_spacing for tree in trees):
            continue
        trees.append(
            WorldObject(
                object_id=1_000_000 + len(trees) + 1,
                name=tree_form.name or object_types["tree"].name,
                kind=ObjectKind.TREE,
                x=x,
                y=y,
                width=tree_width,
                height=tree_height,
                blocks_movement=tree_form.blocks_movement,
                blocks_vision=tree_form.blocks_vision,
                mobility=tree_form.mobility,
                traits=tree_form.traits,
                state=dict(object_types["tree"].state_defaults),
                descriptions=dict(tree_form.descriptions),
                interactions={
                    action: dict(details)
                    for action, details in tree_form.interactions.items()
                },
                capacity=tree_form.capacity_for(100),
                type_id="tree",
                quality=100,
                form=object_types["tree"].default_form,
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
        if not obj.active or obj.container is not None:
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


def _apply_boundaries(tile_map: TileMap, boundaries: list[BoundaryObject]) -> None:
    for boundary in boundaries:
        try:
            edge = TileEdge(boundary.edge)
        except ValueError as exc:
            raise MapLoadError(
                f"Boundary {boundary.boundary_id!r} has invalid edge {boundary.edge!r}"
            ) from exc
        column, row = boundary.column, boundary.row
        # South/east map-border canonical edges have no tile on their lower/right side.
        if edge is TileEdge.NORTH and row == tile_map.rows:
            row -= 1
            edge = TileEdge.SOUTH
        elif edge is TileEdge.WEST and column == tile_map.columns:
            column -= 1
            edge = TileEdge.EAST
        # Unlocked doors are route-plannable even while visually closed; the
        # character opens them automatically when reaching the crossing.
        passable = boundary.kind == "door" and not boundary.locked
        tile_map.set_edge_passable(column, row, edge, passable)


def _boundary_from_entry(entry: dict[str, Any]) -> BoundaryObject:
    column = int(_required(entry, "x", "boundary"))
    row = int(_required(entry, "y", "boundary"))
    edge = str(_required(entry, "edge", "boundary")).lower()
    if edge == "east":
        column += 1
        edge = "west"
    elif edge == "south":
        row += 1
        edge = "north"
    return BoundaryObject(
        boundary_id=str(_required(entry, "id", "boundary")),
        kind=str(_required(entry, "type", "boundary")),
        column=column,
        row=row,
        edge=edge,
        open=bool(entry.get("open", False)),
        locked=bool(entry.get("locked", False)),
        swing=str(entry.get("swing", "counterclockwise")).lower(),
        blocks_vision=bool(entry.get("blocks_vision", entry.get("type") == "wall")),
    )


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
    _apply_boundaries(rebuilt, map_definition.boundaries)
    for previous_kind, tile in zip(previous_kinds, rebuilt.tiles):
        if previous_kind is TileKind.SOIL and tile.kind is TileKind.GRASSLAND:
            tile.kind = TileKind.SOIL
    for (column, row), state in map_definition.tile_states.items():
        tile = rebuilt.tile_at(column, row)
        if tile is not None and state.kind_override == TileKind.SOIL.value:
            tile.kind = TileKind.SOIL
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
