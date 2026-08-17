from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto

from remembering.coordinates import TilePosition
from remembering.tiles import TileMap


START_OF_DAY_MINUTES = 360
DAY_LENGTH_MINUTES = 16 * 60


def format_clock_time(minutes: int) -> str:
    total_minutes = minutes % (24 * 60)
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def day_progress_ratio(minutes: int) -> float:
    elapsed = minutes - START_OF_DAY_MINUTES
    normalized = elapsed / DAY_LENGTH_MINUTES
    return max(0.0, min(1.0, normalized))


class Mode(Enum):
    MORNING = auto()
    DIRECT = auto()
    REPLAY = auto()


class ObjectKind(Enum):
    OBJECT = auto()
    BED = auto()
    TABLE = auto()
    FOOD_PREP_STATION = auto()
    WORKBENCH = auto()
    TOOL_STORAGE = auto()
    BRANCH = auto()
    PEBBLE = auto()
    GRASS = auto()
    WILD_PLANT = auto()
    CROP = auto()
    TREE = auto()
    BOULDER = auto()
    BUSH = auto()
    BARREL = auto()
    BUCKET = auto()
    BASKET = auto()
    AXE = auto()
    CUPBOARD = auto()


class RoomQuality(Enum):
    RUINED = "ruined"
    DAMAGED = "damaged"
    NORMAL = "normal"
    FINE = "fine"
    GREAT = "great"


def tree_state_data(state: dict[str, object]) -> dict[str, object]:
    loaded = state
    return {
        "branch_taken": bool(loaded.get("branch_taken", False)),
        "stump_memory_count": max(0, int(loaded.get("stump_memory_count", 0))),
        "persistence_modifier": (
            float(loaded["persistence_modifier"])
            if loaded.get("persistence_modifier") is not None
            else None
        ),
    }


def encode_tree_state(data: dict[str, object]) -> dict[str, object]:
    return dict(data)


@dataclass(frozen=True, slots=True)
class RoutineStep:
    target_id: int | None
    action: str
    target_type: str | None = None
    area_bounds: tuple[int, int, int, int] | None = None
    quantity: int | None = None
    target_point: tuple[float, float] | None = None
    secondary_bounds: tuple[int, int, int, int] | None = None
    source_areas: tuple[tuple[int, int, int, int], ...] | None = None
    target_areas: tuple[tuple[int, int, int, int], ...] | None = None
    target_build_memory: str | None = None
    max_game_minutes: int | None = None
    till_until_done: bool = False
    nearest_to_player: bool = False
    condition_kind: str | None = None
    condition_subject: str | None = None
    condition_operator: str = ">="
    condition_value: float | None = None
    routine_name: str | None = None


@dataclass(slots=True)
class BuildMemory:
    memory_id: str
    object_type: str
    column: int
    row: int
    orientation: str = "E/W"
    quality: int = 20
    build_count: int = 0
    persistent: bool = False
    persistence_modifier: float | None = None
    state: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ObjectState:
    x: int
    y: int
    orientation: str = "E/W"
    quality: int = 100
    active: bool = True
    state: dict[str, object] = field(default_factory=dict)
    persistent: bool = False
    variant: str | None = None
    form: str | None = None
    flavor: str | None = None
    container: str | None = None


@dataclass(slots=True)
class WorldObject:
    object_id: int
    name: str
    kind: ObjectKind
    x: int
    y: int
    width: int
    height: int
    active: bool = True
    state: dict[str, object] = field(default_factory=dict)
    blocks_movement: bool = False
    blocks_vision: bool = False
    mobility: str = "fixed"
    traits: tuple[str, ...] = ()
    persistent: bool = False
    descriptions: dict[str, str] = field(default_factory=dict)
    interactions: dict[str, dict[str, object]] = field(default_factory=dict)
    capacity: dict[str, int] = field(default_factory=dict)
    nutrition: int = 0
    condition_recovery: dict[str, float] = field(default_factory=dict)
    type_id: str = ""
    orientation: str = "E/W"
    quality: int = 100
    persistent_state: ObjectState | None = None
    daily_spawned: bool = False
    variant: str | None = None
    form: str = ""
    flavor: str | None = None
    container: str | None = None

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def tile_position(self) -> TilePosition:
        return TilePosition.from_mapxy(self.center)

    def contains(self, point: tuple[int, int]) -> bool:
        px, py = point
        return (
            self.active
            and self.container is None
            and self.x <= px <= self.x + self.width
            and self.y <= py <= self.y + self.height
        )

    @property
    def quality_stage(self) -> str:
        if self.quality <= 20:
            return "ruined"
        if self.quality <= 40:
            return "damaged"
        if self.quality <= 60:
            return "worn"
        if self.quality <= 79:
            return "good"
        return "fine"

    @property
    def description(self) -> str:
        return self.descriptions.get(self.quality_stage, "")


@dataclass(frozen=True, slots=True)
class SpriteOverlay:
    overlay_id: str
    state_field: str
    value_min: float = 0.0
    value_max: float | None = None
    capacity_resource: str | None = None
    alpha_min: int = 0
    alpha_max: int = 255


@dataclass(frozen=True, slots=True)
class ObjectForm:
    form_id: str
    name: str | None
    descriptions: dict[str, str]
    footprint: tuple[int, int] = (1, 1)
    blocks_movement: bool = False
    blocks_vision: bool = False
    mobility: str = "fixed"
    traits: tuple[str, ...] = ()
    interactions: dict[str, dict[str, object]] = field(default_factory=dict)
    spawn_tiles: tuple[str, ...] = ()
    can_spawn_on_water: bool = False
    spawn_influence: tuple[tuple[str, float, int, float], ...] = ()
    build_cost: tuple[tuple[str, int], ...] = ()
    capacity: dict[str, int | dict[str, int]] = field(default_factory=dict)
    nutrition: int = 0
    condition_recovery: dict[str, float] = field(default_factory=dict)
    illness_exposures: dict[str, float] = field(default_factory=dict)
    build_duration_seconds: float = 0.0
    persistence: dict[str, PersistencePolicy] = field(default_factory=dict)
    states: tuple[str, ...] = ()
    sprite_overlays: tuple[SpriteOverlay, ...] = ()

    def capacity_for(self, quality: int) -> dict[str, int]:
        if quality <= 20:
            stage = "ruined"
        elif quality <= 40:
            stage = "damaged"
        elif quality <= 60:
            stage = "worn"
        elif quality <= 79:
            stage = "good"
        else:
            stage = "fine"
        return {
            resource: int(value.get(stage, 0) if isinstance(value, dict) else value)
            for resource, value in self.capacity.items()
        }


@dataclass(frozen=True, slots=True)
class ObjectMemoryDefinition:
    memory_id: str
    text: str
    chance: float = 1.0
    radius_tiles: float = 3.0


@dataclass(frozen=True, slots=True)
class ObjectType:
    type_id: str
    name: str
    variant_names: dict[str, str]
    kind: ObjectKind
    default_form: str
    forms: dict[str, ObjectForm]
    variants: tuple[str, ...] = ()
    default_variant: str | None = None
    state_fields: tuple[str, ...] = ()
    state_defaults: dict[str, object] = field(default_factory=dict)
    growth: dict[str, object] = field(default_factory=dict)
    memory_refs: tuple[str, ...] = ()

    def form_definition(
        self, form: str | None = None, variant: str | None = None
    ) -> ObjectForm:
        return self.forms[form or self.default_form]

    def name_for(self, variant: str | None = None) -> str:
        return self.variant_names.get(variant or "", self.name)


@dataclass(frozen=True, slots=True)
class MapDoor:
    side: str
    offset: int
    width: int
    connects_to: str | None = None


@dataclass(slots=True)
class BoundaryObject:
    """A full object on a grid line; edge is canonical ``north`` or ``west``."""

    boundary_id: str
    kind: str
    column: int
    row: int
    edge: str
    open: bool = False
    locked: bool = False
    swing: str = "counterclockwise"
    blocks_vision: bool = True


@dataclass(frozen=True, slots=True)
class MapBuilding:
    building_id: str
    name: str


@dataclass(frozen=True, slots=True)
class MapTerrain:
    terrain_id: str
    name: str
    kind: str
    points: tuple[tuple[int, int], ...]
    display_color: tuple[int, int, int]
    blocks_movement: bool = True
    width: int = 0


@dataclass(frozen=True, slots=True)
class MapStructure:
    structure_id: str
    building_id: str
    name: str
    kind: str
    x: int
    y: int
    width: int
    height: int
    blocks_movement: bool = False
    quality: RoomQuality | None = None
    display_color: tuple[int, int, int] | None = None
    doors: tuple[MapDoor, ...] = ()


@dataclass(slots=True)
class LevelTileState:
    column: int
    row: int
    till_percentage: float = 0.0
    tilled_today: bool = False
    soil_persistence_percentage: float = 0.0
    kind_override: str | None = None
    persistence_modifier: float | None = None


@dataclass(frozen=True, slots=True)
class PersistencePolicy:
    chance_per_count: float
    decay_chance: float
    modifier_range: tuple[float, float] = (1.0, 1.0)


@dataclass(slots=True)
class MapDefinition:
    name: str
    width: int
    height: int
    buildings: list[MapBuilding]
    terrain: list[MapTerrain]
    structures: list[MapStructure]
    objects: dict[int, WorldObject]
    tile_map: TileMap
    boundaries: list[BoundaryObject] = field(default_factory=list)
    object_types: dict[str, ObjectType] = field(default_factory=dict)
    object_memories: dict[str, ObjectMemoryDefinition] = field(default_factory=dict)
    tile_states: dict[tuple[int, int], LevelTileState] = field(default_factory=dict)
    tile_sprite_overlays: dict[str, tuple[SpriteOverlay, ...]] = field(
        default_factory=dict
    )
    tile_illness_exposures: dict[TileKind, dict[str, float]] = field(
        default_factory=dict
    )
    cheat_memory: tuple[RoutineStep, ...] = ()
    remembered_routine: tuple[RoutineStep, ...] = ()
    build_memories: dict[str, BuildMemory] = field(default_factory=dict)
    till_progress_per_action: float = 5.0
    soil_persistence_gain: float = 1.0
    reverted_till_progress_range: tuple[float, float] = (80.0, 100.0)
    tile_persistence_modifier_range: tuple[float, float] = (1.0, 1.0)
    characters: dict[int, CharacterState] = field(default_factory=dict)
    controlled_character_id: int | None = None
    character_types: dict[str, CharacterType] = field(default_factory=dict)


@dataclass(slots=True)
class SkillState:
    level: int = 0
    experience: float = 0.0


@dataclass(frozen=True, slots=True)
class CharacterType:
    type_id: str
    name: str
    description: str
    inherits: str | None
    conditions: dict[str, dict[str, object]]
    secondary_stats: dict[str, dict[str, object]]
    skills: dict[str, dict[str, object]]
    behavior: dict[str, object]


@dataclass(slots=True)
class CharacterState:
    character_id: int
    type_id: str
    name: str
    last_sleep_id: int
    conditions: dict[str, float] = field(default_factory=dict)
    condition_memory: dict[str, float] = field(default_factory=dict)
    skills: dict[str, SkillState] = field(default_factory=dict)
    used_nap_windows: set[str] = field(default_factory=set)


@dataclass(slots=True)
class PlayerState:
    x: float = 140
    y: float = 120
    speed: float = 190
    inventory: Counter[str] = field(default_factory=Counter)
    character_id: int = 1
    conditions: dict[str, float] = field(
        default_factory=lambda: {
            "trauma": 90.0,
            "hunger": 95.0,
            "thirst": 95.0,
            "fatigue": 82.0,
        }
    )
    condition_memory: dict[str, float] = field(
        default_factory=lambda: {"trauma": 90.0, "hunger": 95.0, "thirst": 95.0}
    )
    skills: dict[str, SkillState] = field(
        default_factory=lambda: {
            "farming": SkillState(),
            "crafting": SkillState(),
            "harvesting": SkillState(),
        }
    )
    last_sleep_id: int = 1
    used_nap_windows: set[str] = field(default_factory=set)
    has_hoe: bool = False
    carrying_hoe: bool = False
    hoe_quality: int = 20
    has_axe: bool = False
    carrying_axe: bool = False
    carried_objects: list[WorldObject] = field(default_factory=list)
    meal_ready: bool = False
    achievements: set[str] = field(default_factory=set)

    @property
    def hunger(self) -> int:
        """Compatibility view: legacy hunger represented fullness."""
        return round(100 - self.conditions.get("hunger", 0.0))

    @hunger.setter
    def hunger(self, value: int) -> None:
        self.conditions["hunger"] = max(0.0, min(99.0, 100.0 - float(value)))

    @property
    def energy(self) -> int:
        """Compatibility view retained for authored quests during migration."""
        return round(100 - self.conditions.get("fatigue", 0.0))

    @energy.setter
    def energy(self, value: int) -> None:
        self.conditions["fatigue"] = max(0.0, min(99.0, 100.0 - float(value)))

    @property
    def tile_position(self) -> TilePosition:
        return TilePosition.from_mapxy((self.x, self.y))

    @tile_position.setter
    def tile_position(self, value: TilePosition) -> None:
        self.x, self.y = value.mapxy

    @property
    def bucket(self) -> WorldObject | None:
        return next(
            (obj for obj in self.carried_objects if obj.type_id == "bucket" and obj.active),
            None,
        )

    @property
    def has_bucket(self) -> bool:
        return self.bucket is not None

    @property
    def basket(self) -> WorldObject | None:
        return next(
            (obj for obj in self.carried_objects if obj.type_id == "basket" and obj.active),
            None,
        )

    @property
    def has_basket(self) -> bool:
        return self.basket is not None

    @property
    def bucket_water_uses(self) -> int:
        return int(self.bucket.state.get("water_uses", 0)) if self.bucket else 0

    @bucket_water_uses.setter
    def bucket_water_uses(self, value: int) -> None:
        if self.bucket is not None:
            capacity = int(self.bucket.capacity.get("water", 0))
            self.bucket.state["water_uses"] = max(0, min(capacity, int(value)))

    @property
    def bucket_filled(self) -> bool:
        return self.bucket_water_uses > 0

    @bucket_filled.setter
    def bucket_filled(self, value: bool) -> None:
        if self.bucket is not None:
            self.bucket_water_uses = self.bucket.capacity.get("water", 0) if value else 0


@dataclass(slots=True)
class DayState:
    number: int = 1
    attempts: int = 1
    mode: Mode = Mode.MORNING
    remembered_routine: list[RoutineStep] = field(default_factory=list)
    today_routine: list[RoutineStep] = field(default_factory=list)
    command_history: list[RoutineStep] = field(default_factory=list)
    replay_index: int = 0
    current_time_minutes: int = START_OF_DAY_MINUTES
