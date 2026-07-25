from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto
import json

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
    BED = auto()
    TABLE = auto()
    FOOD_PREP_STATION = auto()
    WORKBENCH = auto()
    TOOL_STORAGE = auto()
    STICK = auto()
    STONE = auto()
    GRASS = auto()
    WILD_GRAIN = auto()
    FIELD = auto()
    TREE = auto()
    BOULDER = auto()
    BERRY_BUSH = auto()
    BARREL = auto()


class RoomQuality(Enum):
    RUINED = "ruined"
    DAMAGED = "damaged"
    NORMAL = "normal"
    FINE = "fine"
    GREAT = "great"


def tree_state_data(state: str) -> dict[str, object]:
    if state == "branch_taken":
        return {"form": "tree", "branch_taken": True, "stump_memory_count": 0}
    if state == "stump":
        return {"form": "stump", "branch_taken": True, "stump_memory_count": 0}
    try:
        loaded = json.loads(state) if state else {}
    except (json.JSONDecodeError, TypeError):
        loaded = {}
    return {
        "form": "stump" if loaded.get("form") == "stump" else "tree",
        "branch_taken": bool(loaded.get("branch_taken", False)),
        "stump_memory_count": max(0, int(loaded.get("stump_memory_count", 0))),
    }


def encode_tree_state(data: dict[str, object]) -> str:
    return json.dumps(data, separators=(",", ":"))


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


@dataclass(slots=True)
class ObjectState:
    x: int
    y: int
    orientation: str = "E/W"
    quality: int = 100
    active: bool = True
    state: str = ""
    persistent: bool = False


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
    state: str = ""
    blocks_movement: bool = False
    persistent: bool = False
    descriptions: dict[str, str] = field(default_factory=dict)
    type_id: str = ""
    orientation: str = "E/W"
    quality: int = 100
    persistent_state: ObjectState | None = None
    daily_spawned: bool = False

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def contains(self, point: tuple[int, int]) -> bool:
        px, py = point
        return self.active and self.x <= px <= self.x + self.width and self.y <= py <= self.y + self.height

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
class ObjectType:
    type_id: str
    name: str
    kind: ObjectKind
    descriptions: dict[str, str]
    width: int
    height: int
    blocks_movement: bool = False
    spawn_tiles: tuple[str, ...] = ()
    spawn_influence: tuple[tuple[str, float, int, float], ...] = ()
    build_cost: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class MapDoor:
    side: str
    offset: int
    width: int
    connects_to: str | None = None


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
    till_count: int = 0
    tilled_today: bool = False
    permanent_kind: str | None = None
    crop: str | None = None
    crop_growth: float = 0.0
    watered: bool = False
    tended: bool = False


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
    object_types: dict[str, ObjectType] = field(default_factory=dict)
    tile_states: dict[tuple[int, int], LevelTileState] = field(default_factory=dict)
    permanent_soil_chance_per_till: float = 0.00001
    till_count_loss_chance: float = 0.10


@dataclass(slots=True)
class PlayerState:
    x: float = 140
    y: float = 120
    speed: float = 190
    inventory: Counter[str] = field(default_factory=Counter)
    hunger: int = 35
    energy: int = 100
    has_hoe: bool = False
    carrying_hoe: bool = False
    hoe_quality: int = 20
    has_axe: bool = False
    carrying_axe: bool = False
    has_bucket: bool = False
    bucket_water_uses: int = 0
    has_basket: bool = False
    meal_ready: bool = False
    achievements: set[str] = field(default_factory=set)

    @property
    def bucket_filled(self) -> bool:
        return self.bucket_water_uses > 0

    @bucket_filled.setter
    def bucket_filled(self, value: bool) -> None:
        self.bucket_water_uses = 5 if value else 0


@dataclass(slots=True)
class DayState:
    number: int = 1
    mode: Mode = Mode.MORNING
    remembered_routine: list[RoutineStep] = field(default_factory=list)
    today_routine: list[RoutineStep] = field(default_factory=list)
    replay_index: int = 0
    current_time_minutes: int = START_OF_DAY_MINUTES
