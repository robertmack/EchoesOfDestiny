from __future__ import annotations

import copy
import math
import json
import random
import sys
from datetime import datetime
from dataclasses import dataclass, field, replace
from pathlib import Path

import pygame

from remembering.camera import Camera
from remembering.coordinates import (
    bounds_as_position_data,
    bounds_from_position_data,
    mapxy_as_position_data,
    position_from_data,
)
from remembering.navigation import find_tile_path, find_tile_path_to_any
from remembering.illness import ActiveIllness, IllnessType, load_illness_types
from remembering.model import (
    BuildMemory,
    BoundaryObject,
    START_OF_DAY_MINUTES,
    DayState,
    LevelTileState,
    Mode,
    ObjectKind,
    ObjectState,
    PlayerState,
    RoutineStep,
    WorldObject,
    day_progress_ratio,
    encode_tree_state,
    format_clock_time,
    tree_state_data,
)
from remembering.rules import (
    available_actions,
)
from remembering.quests import QuestLoadError, QuestManager, load_quest_state
from remembering.sprites import (
    BoundarySpriteCatalog,
    ObjectSpriteCatalog,
    TileSpriteCatalog,
    overlay_alpha,
)
from remembering.stats import (
    CONDITION_IDS,
    CONDITION_LABELS,
    apply_condition_effects,
    condition_color,
    condition_descriptor,
    gain_skill_experience,
    harvesting_speed_multiplier,
    healing_rate,
    critical_trauma_visible,
    learn_dawn_conditions,
    movement_speed_multiplier,
    task_speed_multiplier,
)
from remembering.tiles import Tile, TileEdge, TileKind
from remembering.ui_layout import (
    COMMAND_EDITOR_MAP_RECT,
    COMMAND_EDITOR_MESSAGES_RECT,
    COMMAND_EDITOR_RECT,
    LEFT_DOCK_RECT,
    LEFT_COMMAND_RECT,
    LEFT_MESSAGE_HISTORY_RECT,
    LEFT_ROUTINE_RECT,
    LEFT_SELECTION_RECT,
    MAP_RECT,
    MESSAGE_BAR_RECT,
    PlayerDockLayout,
    InventoryPage,
    RELOAD_BUTTON_RECT,
    RIGHT_DOCK_RECT,
    TIMELINE_HEIGHT_PX,
    WINDOW_SIZE_PX,
    WINDOW_TITLE,
    player_dock_layout,
)
from remembering.world import (
    DEFAULT_MEMORY_DIR,
    DEFAULT_CURRENT_LEVEL_PATH,
    advance_level_tile_states,
    initialize_current_level_from_map,
    MapLoadError,
    ObjectPersistenceError,
    load_memory_file,
    load_map,
    memory_file_path,
    save_memory_file,
    save_persistent_objects,
    sync_current_level_from_map,
    rebuild_tile_map,
)


def build_context_menu_options(
    obj: WorldObject,
    player: PlayerState,
    world_pos: tuple[int, int],
    objects: dict[str, WorldObject],
) -> list[str]:
    actions = object_action_menu_options(obj, player)
    options = ["Move To"]
    options.extend(actions)
    return options


DISABLED_INTERACTION_SUFFIX = " (requirements not met)"


def object_action_menu_options(obj: WorldObject, player: PlayerState) -> list[str]:
    if obj.kind is not ObjectKind.WORKBENCH:
        return available_actions(obj, player)
    available = set(available_actions(obj, player))
    return [
        action if action in available else f"{action}{DISABLED_INTERACTION_SUFFIX}"
        for action in obj.interactions
    ]


def action_option_enabled(option: str) -> bool:
    return option not in {EMPTY_BUCKET_REQUIRED, RUINED_BUCKET_REQUIRED} and not option.endswith(
        DISABLED_INTERACTION_SUFFIX
    )


def missing_recipe_ingredients(
    obj: WorldObject, player: PlayerState, action: str
) -> list[tuple[str, int]]:
    definition = obj.interactions.get(action.removesuffix(DISABLED_INTERACTION_SUFFIX), {})
    cost = definition.get("cost", {})
    return [
        (str(item), int(amount) - player.inventory[str(item)])
        for item, amount in cost.items()
        if player.inventory[str(item)] < int(amount)
    ]


def ingredient_requirement_label(item: str, amount: int) -> str:
    name = item.replace("_", " ")
    if amount != 1:
        name += "es" if name.endswith(("s", "x")) else "s"
    return f"{amount} {name}"


def build_ground_context_menu_options(
    tile: Tile,
    player: PlayerState,
    state: LevelTileState | None = None,
    crop: WorldObject | None = None,
) -> list[str]:
    options = ["Move To"]
    if player.has_bucket:
        options.append("Drop Bucket")
    if tile.kind in {TileKind.SHALLOW_WATER, TileKind.POND}:
        options.append("Drink Water")
        options.append("Drink Until Full")
        if player.bucket is not None and int(player.bucket.capacity.get("water", 0)) <= 0:
            options.append(RUINED_BUCKET_REQUIRED)
        else:
            options.append(
                "Gather Water"
                if player.has_bucket and not player.bucket_filled
                else EMPTY_BUCKET_REQUIRED
            )
    crop_state = crop.state if crop is not None else {}
    if tile.kind is TileKind.SOIL and crop is not None:
        if (
            player.has_bucket
            and player.bucket_filled
            and float(crop_state.get("water", 0.0)) < 100.0
        ):
            options.append("Water Crop")
        if float(crop_state.get("tended", 0.0)) < 100.0 and float(
            crop_state.get("growth_progress", 0.0)
        ) < 1.0:
            options.append("Tend Plant")
    return options


def compact_label(text: str, max_width: int, rendered_width: int) -> str:
    if len(text) <= 2:
        return text
    if rendered_width <= max_width:
        return text
    return text[0]


def wrap_text(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.size(candidate)[0] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def object_map_label(obj: WorldObject) -> str:
    return obj.name


def crop_inspection_lines(obj: WorldObject) -> list[str]:
    if obj.type_id != "crop":
        return []
    return [
        f"Growth: {float(obj.state.get('growth_progress', 0.0)) * 100:.1f}%",
        f"Water: {float(obj.state.get('water', 0.0)):.1f}%",
        f"Tended: {float(obj.state.get('tended', 0.0)):.1f}%",
    ]


def routine_step_editable_fields(step: RoutineStep) -> tuple[str, ...]:
    """Return fields meaningful to this command, plus any populated extras."""
    if step.action == "__end__":
        return ()
    if step.action in {"__if__", "__repeat_until__"}:
        return (
            "condition_kind",
            "condition_subject",
            "condition_operator",
            "condition_value",
        )
    fields = ["action"]
    if step.action == "Harvest and Eat Berries":
        fields.append("target_mode")
        if step.nearest_to_player:
            return tuple(fields)
        if step.area_bounds is not None or step.target_areas is not None:
            fields.append("area_bounds")
        else:
            fields.append("target_point")
        return tuple(fields)
    area_commands = {
        *AREA_COMMAND_TYPES,
        "Chop Trees",
        "Till Grassland",
        "Plant Wheat",
        "Water Crops",
        "Tend Crops",
        "Harvest Wheat",
        "Build Barrel",
        "Build Cupboard",
    }
    if step.action in area_commands:
        fields.extend(("area_bounds", "quantity", "target_areas"))
    elif step.action == "Fill Barrel":
        fields.extend(
            (
                "target_type",
                "target_point",
                "area_bounds",
                "source_areas",
                "target_build_memory",
            )
        )
    elif step.action == "Move To":
        fields.append("target_point")
    else:
        fields.extend(("target_type", "target_point"))

    if step.action in AREA_COMMAND_TYPES:
        fields.append("nearest_to_player")
    if step.action == "Water Crops":
        fields.extend(("secondary_bounds", "source_areas"))
    if step.action == "Till Grassland":
        fields.extend(("max_game_minutes", "till_until_done"))

    if step.condition_kind is not None:
        fields.extend(
            ("condition_kind", "condition_subject", "condition_operator", "condition_value")
        )
    return tuple(fields)


def routine_field_editor_value(step: RoutineStep, field_name: str) -> object:
    if field_name == "target_mode":
        if step.nearest_to_player:
            return "nearest"
        if step.area_bounds is not None or step.target_areas is not None:
            return "area"
        return "specific"
    value = getattr(step, field_name)
    if value is None:
        return None
    if field_name == "target_point":
        return mapxy_as_position_data(value)
    if field_name in {"area_bounds", "secondary_bounds"}:
        return bounds_as_position_data(value)
    if field_name in {"source_areas", "target_areas"}:
        return [bounds_as_position_data(area) for area in value]
    return value


def routine_field_runtime_value(field_name: str, value: object) -> object:
    if value is None:
        return None
    if field_name == "target_point":
        return position_from_data(value).mapxy
    if field_name in {"area_bounds", "secondary_bounds"}:
        return bounds_from_position_data(value)
    if field_name in {"source_areas", "target_areas"}:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} requires an array of bounds")
        return tuple(bounds_from_position_data(area) for area in value)
    return value


def sprite_size_within_footprint(
    sprite_size: tuple[int, int], footprint_size: tuple[int, int]
) -> tuple[int, int]:
    """Keep small sprites native-sized and proportionally shrink oversized ones."""
    sprite_width, sprite_height = sprite_size
    footprint_width, footprint_height = footprint_size
    scale = min(
        1.0,
        footprint_width / sprite_width,
        footprint_height / sprite_height,
    )
    return (
        max(1, round(sprite_width * scale)),
        max(1, round(sprite_height * scale)),
    )


def random_within_tile_anchor(
    object_id: int,
    type_id: str,
    sprite_size: tuple[int, int],
    footprint_size: tuple[int, int],
    margin: float = 0.2,
) -> tuple[float, float]:
    """Place the whole sprite inside a stable per-instance exclusion margin."""
    randomizer = random.Random(f"remembering:{type_id}:{object_id}")
    sprite_width, sprite_height = sprite_size
    footprint_width, footprint_height = footprint_size
    left = margin + sprite_width / footprint_width / 2.0
    right = 1.0 - margin - sprite_width / footprint_width / 2.0
    top = margin + sprite_height / footprint_height / 2.0
    bottom = 1.0 - margin - sprite_height / footprint_height / 2.0
    return (
        left + randomizer.random() * max(0.0, right - left),
        top + randomizer.random() * max(0.0, bottom - top),
    )


def distance_to_object(point: tuple[float, float], obj: WorldObject) -> float:
    """Return the shortest distance from a point to an object's footprint."""
    x, y = point
    dx = max(obj.x - x, 0.0, x - (obj.x + obj.width))
    dy = max(obj.y - y, 0.0, y - (obj.y + obj.height))
    return math.hypot(dx, dy)


def tile_aligned_area_bounds(
    start: tuple[float, float], end: tuple[float, float], tile_size: int
) -> tuple[int, int, int, int]:
    """Expand a drag to the outer edges of every tile it touches."""
    left = math.floor(min(start[0], end[0]) / tile_size) * tile_size
    top = math.floor(min(start[1], end[1]) / tile_size) * tile_size
    right = (math.floor(max(start[0], end[0]) / tile_size) + 1) * tile_size
    bottom = (math.floor(max(start[1], end[1]) / tile_size) + 1) * tile_size
    return left, top, right, bottom


def build_path_points(start: tuple[float, float], end: tuple[float, float], steps: int = 8) -> list[tuple[int, int]]:
    if steps <= 1:
        return [(int(start[0]), int(start[1])), (int(end[0]), int(end[1]))]
    points: list[tuple[int, int]] = []
    for index in range(steps + 1):
        ratio = index / steps
        x = start[0] + (end[0] - start[0]) * ratio
        y = start[1] + (end[1] - start[1]) * ratio
        points.append((int(x), int(y)))
    return points

WIDTH, HEIGHT = WINDOW_SIZE_PX
TOP_BAR_HEIGHT = TIMELINE_HEIGHT_PX
MESSAGE_BAR_HEIGHT = MESSAGE_BAR_RECT.height
MAP_SIZE = MAP_RECT.width
SIDEBAR_WIDTH = LEFT_DOCK_RECT.width
RIGHT_SIDEBAR_WIDTH = RIGHT_DOCK_RECT.width
PLAYER_RADIUS = 14
INTERACTION_DISTANCE = 45
GAME_MINUTES_PER_REAL_SECOND = 1.0
FIXED_SIMULATION_TICK_SECONDS = 0.05
DAY_FADE_DURATION_SECONDS = 0.75
NIGHT_BUMPER_DURATION_SECONDS = 2.75
NIGHT_BUMPER_TEXT = (
    "As the light fades behind closed eyes, you relive the sequence of the day."
)
DEFAULT_NIGHT_HINTS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "night_hints.json"
)
MORNING_OPENING_FADE_SECONDS = 5.0
INTRO_PAGE_SECONDS = 6.0
INTRO_FOOTER_HEIGHT = 48
INTRO_IMAGE_PATHS = tuple(
    Path(__file__).resolve().parents[1] / "assets" / "images" / f"opening_pg{page}.png"
    for page in range(1, 5)
)
EMPTY_BUCKET_REQUIRED = "Gather Water (empty bucket required)"
RUINED_BUCKET_REQUIRED = "Gather Water (bucket is ruined)"
TIME_SPEED_OPTIONS = (
    (".5x", 0.5),
    ("1x", 1.0),
    ("2x", 2.0),
    ("4x", 4.0),
    ("10x", 10.0),
    ("20x", 20.0),
    ("40x", 40.0),
)
AREA_COMMAND_TYPES = {
    "Gather Pebbles": {"pebble"},
    "Gather Branches": {"branch"},
    "Gather Seeds": {"wild_plant"},
    "Gather Tall Grass": {"grass"},
    "Harvest Berries": {"bush"},
}
AREA_COMMANDS = list(AREA_COMMAND_TYPES)
BUILD_COMMAND_TYPES = {
    "Build Barrel": "barrel",
    "Build Cupboard": "cupboard",
}
NEAREST_AREA_COMMANDS = {
    *AREA_COMMAND_TYPES,
    *BUILD_COMMAND_TYPES,
    "Chop Trees",
    "Till Grassland",
    "Plant Wheat",
    "Water Crops",
    "Tend Crops",
    "Harvest Wheat",
}
AREA_COMMAND_CATEGORIES = ("Gather", "Farm", "Build")
RESOURCE_CHEAT_KEYS = {
    pygame.K_w: "wood",
    pygame.K_f: "fiber",
    pygame.K_b: "branch",
    pygame.K_p: "pebble",
    pygame.K_s: "seed",
    pygame.K_g: "grains",
}
MAX_MESSAGE_HISTORY = 500
CONDITION_THOUGHT_DISPLAY_SECONDS = 4.0
VOMITING_DURATION_SECONDS = CONDITION_THOUGHT_DISPLAY_SECONDS
CONDITION_THOUGHT_GAP_SECONDS = 8.0
UNAVAILABLE_ROUTINE_COLOR = (235, 82, 82)
BLOCKED_ROUTINE_COLOR = (224, 190, 72)
ROUTINE_IF_ACTION = "__if__"
ROUTINE_LOOP_ACTION = "__repeat_until__"
ROUTINE_END_ACTION = "__end__"
ROUTINE_REFERENCE_ACTION = "__routine__"
ROUTINE_CONTROL_ACTIONS = {
    ROUTINE_IF_ACTION,
    ROUTINE_LOOP_ACTION,
    ROUTINE_END_ACTION,
}
CONDITION_COMPLAINTS = {
    "trauma": (
        (90, "I can barely move. Everything hurts."),
        (70, "Every step hurts."),
        (50, "I'm hurt."),
    ),
    "hunger": (
        (90, "I'm starving."),
        (70, "I need to find something to eat."),
        (50, "I'm getting hungry."),
    ),
    "thirst": (
        (90, "I desperately need water."),
        (70, "I'm so thirsty."),
        (50, "I could use a drink."),
    ),
    "fatigue": (
        (90, "I can barely keep my eyes open."),
        (70, "I need to rest soon."),
        (50, "I'm getting tired."),
    ),
}
MAP_LEFT = MAP_RECT.left
MAP_RIGHT = MAP_RECT.right
MAP_TOP = MAP_RECT.top
MAP_BOTTOM = MAP_RECT.bottom
MESSAGE_BAR = MESSAGE_BAR_RECT
RELOAD_BUTTON = RELOAD_BUTTON_RECT
MAP_VIEWPORT = MAP_RECT


def tilling_duration_seconds(_hoe_quality: int) -> float:
    """Fixed until skills and equipment provide authored work-rate modifiers."""
    return 15.0


def planting_duration_seconds(has_basket: bool) -> float:
    return 1.5 if has_basket else 4.0


def sun_track_position(progress: float, track_x: int, track_width: int, radius: int = 8) -> int:
    clamped = max(0.0, min(1.0, progress))
    return track_x + radius + round((track_width - radius * 2) * clamped)


def object_job_duration_seconds(action: str, obj: WorldObject, has_basket: bool) -> float:
    duration = obj.interactions.get(action, {}).get("duration_seconds", {})
    if not isinstance(duration, dict):
        return float(duration)
    key = "with_basket" if has_basket and "with_basket" in duration else "base"
    return float(duration.get(key, 0.55))


@dataclass(slots=True)
class PendingJob:
    target_id: int
    action: str
    interaction_point: tuple[float, float] | None = None
    advances_replay: bool = True


@dataclass(slots=True)
class DayPlanActivity:
    kind: str
    label: str
    macro_name: str | None = None
    scheduled_minutes: int | None = None
    children: list[DayPlanActivity] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NightHint:
    minute: int
    text: str


def load_night_hints(
    path: Path = DEFAULT_NIGHT_HINTS_PATH,
) -> tuple[int, tuple[NightHint, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cutoff = int(payload["cutoff_minutes"])
    hints = tuple(
        sorted(
            (
                NightHint(int(entry["minute"]), str(entry["text"]))
                for entry in payload.get("hints", [])
            ),
            key=lambda hint: hint.minute,
        )
    )
    return cutoff, hints


@dataclass(slots=True)
class AreaTarget:
    action: str
    point: tuple[float, float]
    target_id: int | None = None
    prerequisite_target_ids: tuple[int, ...] = ()
    placement_point: tuple[float, float] | None = None
    work_fraction: float = 1.0


@dataclass(slots=True)
class BarrelFillJob:
    barrel_id: int
    source_areas: tuple[tuple[int, int, int, int], ...]
    phase: str = "choose"


@dataclass(slots=True)
class FieldWaterJob:
    crop_bounds: tuple[int, int, int, int]
    source_areas: tuple[tuple[int, int, int, int], ...]
    quantity: int
    crop_points: list[tuple[float, float]]
    phase: str = "choose"
    current_crop: tuple[float, float] | None = None


def available_area_commands(player: PlayerState) -> list[str]:
    commands = list(AREA_COMMANDS)
    if player.carrying_axe:
        commands.append("Chop Trees")
    if player.carrying_hoe:
        commands.append("Till Grassland")
    if player.inventory["seed"] > 0:
        commands.append("Plant Wheat")
    if player.has_bucket:
        commands.append("Water Crops")
    commands.append("Tend Crops")
    commands.append("Build Barrel")
    commands.append("Build Cupboard")
    commands.append("Harvest Wheat")
    return commands


def area_commands_for_category(player: PlayerState, category: str) -> list[str]:
    commands = available_area_commands(player)
    if category == "Gather":
        return [
            command
            for command in commands
            if command.startswith("Gather ")
            or command in {"Harvest Berries", "Chop Trees"}
        ]
    if category == "Farm":
        return [
            command
            for command in commands
            if command in {"Till Grassland", "Plant Wheat", "Water Crops", "Tend Crops", "Harvest Wheat"}
        ]
    if category == "Build":
        return [command for command in commands if command in BUILD_COMMAND_TYPES]
    return []


class Game:
    def __init__(
        self,
        *,
        fullscreen: bool = True,
        skip_intro: bool = False,
        persistence_path: Path = DEFAULT_CURRENT_LEVEL_PATH,
        memory_directory: Path = DEFAULT_MEMORY_DIR,
    ) -> None:
        pygame.init()
        display_flags = pygame.SCALED if fullscreen else pygame.HIDDEN
        pygame.display.set_mode((WIDTH, HEIGHT), display_flags)
        pygame.display.set_caption(WINDOW_TITLE)
        self.screen = pygame.display.get_surface()
        if fullscreen:
            pygame.display.toggle_fullscreen()
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 25)
        self.small_font = pygame.font.Font(None, 20)
        self.title_font = pygame.font.Font(None, 42)
        self.wake_fonts = {
            "plain": pygame.font.Font(None, 32),
            "bold": pygame.font.Font(None, 34),
            "italic": pygame.font.Font(None, 34),
        }
        self.wake_fonts["bold"].set_bold(True)
        self.wake_fonts["italic"].set_italic(True)
        self.object_sprites = ObjectSpriteCatalog()
        self.tile_sprites = TileSpriteCatalog()
        self.boundary_sprites = BoundarySpriteCatalog()
        self.day = DayState(mode=Mode.DIRECT)
        self.night_cutoff_minutes, self.night_hints = load_night_hints()
        self.shown_night_hint_minutes: set[int] = set()
        self.persistence_path = persistence_path
        self.memory_directory = memory_directory
        initialize_current_level_from_map(current_level_path=self.persistence_path)
        self.map = load_map(
            persistence_path=self.persistence_path,
            day_number=self.day.number,
            reset_for_morning=True,
        )
        self.day.remembered_routine = list(self.map.remembered_routine)
        self.objects = self.map.objects
        self.build_memories = self.map.build_memories
        self.sync_all_barrel_sprite_states()
        self.world_scale = self.map.tile_map.tile_size / 32.0
        self.player_radius = PLAYER_RADIUS * self.world_scale
        self.interaction_distance = INTERACTION_DISTANCE * self.world_scale
        spawn_x, spawn_y = self.player_spawn()
        self.player = PlayerState(x=spawn_x, y=spawn_y)
        character = self.map.characters.get(self.map.controlled_character_id or -1)
        if character is not None:
            self.player.character_id = character.character_id
            self.player.conditions = dict(character.conditions)
            self.player.condition_memory = dict(character.condition_memory)
            self.player.skills = character.skills
            self.player.last_sleep_id = character.last_sleep_id
            self.player.used_nap_windows = set(character.used_nap_windows)
            sleep_object = self.objects[character.last_sleep_id]
            self.player.conditions["fatigue"] = max(
                0.0, min(99.0, 100.0 - sleep_object.quality)
            )
        self.player.speed *= self.world_scale
        self.player.carried_objects = [
            obj for obj in self.objects.values() if obj.container == "player"
        ]
        self.quests = QuestManager.from_file()
        try:
            self.quests.restore_state(load_quest_state(self.persistence_path))
        except QuestLoadError as exc:
            self.messages = [f"Could not restore quests: {exc}"]
        self.quests.record_start_day(self.player.conditions)
        self.storage_memories = self.load_storage_memories()
        self.camera = Camera(
            MAP_LEFT,
            MAP_TOP,
            MAP_RIGHT - MAP_LEFT,
            MAP_BOTTOM - MAP_TOP,
            zoom=64 / self.map.tile_map.tile_size,
            min_zoom=16 / self.map.tile_map.tile_size,
            max_zoom=1.0,
        )
        self.camera.center_on(self.object_of_type("bed").center, (self.map.width, self.map.height))
        self.last_camera_follow_position = (self.player.x, self.player.y)
        self.selected_id: int | None = None
        self.selected_tile: tuple[int, int] | None = None
        self.pending_job: PendingJob | None = None
        self.action_buttons: list[tuple[pygame.Rect, str]] = []
        self.command_buttons: list[tuple[pygame.Rect, str]] = []
        self.command_category_buttons: list[tuple[pygame.Rect, str]] = []
        self.active_command_category: str | None = None
        self.area_quantity_buttons: list[tuple[pygame.Rect, int]] = []
        self.target_selection_buttons: list[tuple[pygame.Rect, str]] = []
        self.target_selection_mode = "area"
        self.area_command_quantity = 10
        self.till_time_buttons: list[tuple[pygame.Rect, str]] = []
        self.till_max_game_minutes = 60
        self.till_until_done = False
        self.active_command: str | None = None
        self.command_drag_start: tuple[int, int] | None = None
        self.command_drag_current: tuple[int, int] | None = None
        self.area_targets: list[AreaTarget] = []
        self.pending_area_target: AreaTarget | None = None
        self.area_job_timer = 0.0
        self.barrel_source_selection_id: int | None = None
        self.barrel_fill_job: BarrelFillJob | None = None
        self.pending_water_crop_selection: tuple[tuple[int, int, int, int], int] | None = None
        self.field_water_job: FieldWaterJob | None = None
        self.pending_source_areas: list[tuple[int, int, int, int]] = []
        self.pending_target_areas: list[tuple[int, int, int, int]] = []
        self.pending_empty_area_memory = False
        self.pending_failed_memory_thought = "Why did I come here?"
        self.thought_bubble_text: str | None = None
        self.thought_bubble_timer = 0.0
        self.thought_bubble_source: str | None = None
        self.illness_types = load_illness_types()
        self.active_illnesses: dict[str, ActiveIllness] = {}
        self.vomiting_timer_seconds = 0.0
        self.condition_thought_cooldown = 2.0
        self.condition_thought_index = 0
        self.object_memory_check_accumulator = 0.0
        self.walk_target: tuple[float, float] | None = None
        self.path_target: tuple[float, float] | None = None
        self.navigation_path: list[tuple[float, float]] = []
        self.preview_path: list[tuple[float, float]] = []
        self.stagger_phase = 0.0
        self.context_menu: list[tuple[pygame.Rect, str]] = []
        self.context_menu_options: list[str] = []
        self.context_option_target_ids: list[int | None] = []
        self.context_menu_pos: tuple[int, int] | None = None
        self.context_ground_target: tuple[float, float] | None = None
        self.context_inventory_item_id: int | None = None
        self.context_boundary_id: str | None = None
        self.camera_dragging = False
        self.camera_drag_position: tuple[int, int] | None = None
        self.messages: list[str] = ["Day 1 begins in Direct Control. Click an object to begin."]
        self.message_scroll_offset = 0
        self.running = True
        self.menu_index = 0
        self.job_timer = 0.0
        self.job_duration = 0.55
        self.time_accumulator = 0.0
        self.simulation_step_accumulator = 0.0
        self.time_speed = 1.0
        self.simulation_paused = False
        self.pause_button = pygame.Rect(0, 0, 0, 0)
        self.speed_down_button = pygame.Rect(0, 0, 0, 0)
        self.speed_display = pygame.Rect(0, 0, 0, 0)
        self.speed_up_button = pygame.Rect(0, 0, 0, 0)
        self.step_button = pygame.Rect(0, 0, 0, 0)
        self.inventory_page = InventoryPage.INVENTORY
        self.equipment_collapsed = False
        self.player_info_tab_buttons: list[
            tuple[pygame.Rect, InventoryPage]
        ] = []
        self.inventory_food_buttons: list[tuple[pygame.Rect, WorldObject]] = []
        self.macro_buttons: list[tuple[pygame.Rect, str]] = []
        self.macro_dropdown_open = False
        self.macro_dropdown_buttons: list[tuple[pygame.Rect, str]] = []
        self.macro_command_buttons: list[tuple[pygame.Rect, str]] = []
        self.main_macro_dropdown_buttons: list[tuple[pygame.Rect, str]] = []
        self.macro_recording = False
        self.macro_record_start_index = 0
        self.macro_previous_today_routine: list[RoutineStep] | None = None
        self.routine_moves_expanded = False
        self.equipment_toggle_button = pygame.Rect(0, 0, 0, 0)
        self.single_step_active = False
        self.single_step_command_started = False
        self.day_transition_phase: str | None = None
        self.day_transition_progress = 0.0
        self.day_transition_prepared = False
        self.day_position_history: list[tuple[float, float]] = [
            (self.player.x, self.player.y)
        ]
        self.rewind_cursor = 0.0
        self.rewind_speed = self.time_speed
        self.rewind_start_time_minutes = self.day.current_time_minutes
        self.rewind_checkpoints: list[tuple[int, str, dict[str, object]]] = []
        self.rewind_checkpoint_index = -1
        self.rewind_flashing_objects: set[int] = set()
        self.rewind_flashing_tiles: set[tuple[int, int]] = set()
        self.rewind_persistent_objects: dict[int, WorldObject] = {}
        self.rewind_persistent_tile_states: dict[
            tuple[int, int], LevelTileState
        ] = {}
        self.rewind_persistent_tile_kinds: dict[tuple[int, int], TileKind] = {}
        self.dawn_transition_state: tuple[object, ...] | None = None
        self.dawn_object_signatures = self.object_persistence_signatures()
        self.dawn_tile_signatures = self.tile_persistence_signatures()
        self.replay_outcome = "expand"
        self.record_routine_commands = True
        self.auto_cheat_memory = False
        self.adjusting_memory = False
        self.memory_edit_index = 0
        self.memory_editor_rows: list[tuple[pygame.Rect, int]] = []
        self.memory_editor_buttons: list[tuple[pygame.Rect, str]] = []
        self.memory_editor_fields: list[tuple[pygame.Rect, str]] = []
        self.memory_field_dropdown_open: str | None = None
        self.memory_field_dropdown_buttons: list[
            tuple[pygame.Rect, str, object]
        ] = []
        self.memory_file_name_rect = pygame.Rect(0, 0, 0, 0)
        self.memory_file_name = "homestead"
        self.memory_edit_field: str | None = None
        self.memory_edit_buffer = ""
        self.routine_rename_source: str | None = None
        self.memory_editor_previous_pause = False
        self.memory_browser_open = False
        self.memory_browser_rows: list[tuple[pygame.Rect, str]] = []
        self.memory_favorite_buttons: list[tuple[pygame.Rect, str]] = []
        self.memory_favorites = self.load_memory_favorites()
        self.running_command_set_name: str | None = None
        self.skip_intro = skip_intro
        self._editor_camera_restore: tuple[float, float, float] | None = None
        self.memory_editor_map_camera: tuple[float, float, float] | None = None
        self.memory_editor_camera_dragging = False
        self.memory_map_field_selection: str | None = None
        self.memory_map_drag_start: tuple[float, float] | None = None
        self.memory_map_drag_current: tuple[float, float] | None = None
        self.day_plan: list[DayPlanActivity] = [
            DayPlanActivity("explore", "Explore"),
            DayPlanActivity("sleep", "Sleep", scheduled_minutes=23 * 60),
        ]
        self.day_plan_rows: list[tuple[pygame.Rect, int]] = []
        self.day_plan_buttons: list[tuple[pygame.Rect, str, int | None]] = []
        self.selected_conditional_index: int | None = None

    def run(self) -> None:
        if not self.skip_intro:
            self.play_intro()
        while self.running:
            dt = self.clock.tick(60) / 1000
            self.handle_events()
            self.update(dt)
            player_position = (self.player.x, self.player.y)
            if player_position != self.last_camera_follow_position:
                self.camera.follow(player_position, (self.map.width, self.map.height))
                self.last_camera_follow_position = player_position
            self.draw()
        pygame.quit()

    def play_intro(self) -> None:
        """Show the opening pages before entering the normal game loop."""
        pages = [pygame.image.load(path).convert() for path in INTRO_IMAGE_PATHS]
        page_index = 0
        elapsed = 0.0

        while self.running and page_index < len(pages):
            elapsed += self.clock.tick(60) / 1000
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                    return
                if event.type != pygame.KEYDOWN:
                    continue
                if event.key == pygame.K_ESCAPE:
                    return
                if event.key == pygame.K_SPACE:
                    page_index += 1
                    elapsed = 0.0

            if elapsed >= INTRO_PAGE_SECONDS:
                page_index += 1
                elapsed = 0.0
            if page_index >= len(pages):
                return

            page = pages[page_index]
            image_area = pygame.Rect(0, 0, WIDTH, HEIGHT - INTRO_FOOTER_HEIGHT)
            scale = min(
                image_area.width / page.get_width(),
                image_area.height / page.get_height(),
            )
            size = (
                max(1, round(page.get_width() * scale)),
                max(1, round(page.get_height() * scale)),
            )
            shown_page = pygame.transform.smoothscale(page, size)
            self.screen.fill((0, 0, 0))
            self.screen.blit(shown_page, shown_page.get_rect(center=image_area.center))
            instructions = self.small_font.render(
                "Space to advance     Escape to skip", True, (220, 220, 220)
            )
            footer = pygame.Rect(0, image_area.bottom, WIDTH, INTRO_FOOTER_HEIGHT)
            self.screen.blit(instructions, instructions.get_rect(center=footer.center))
            pygame.display.flip()

    def handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif (
                event.type == pygame.KEYDOWN
                and event.key == pygame.K_q
                and getattr(event, "mod", pygame.key.get_mods()) & pygame.KMOD_CTRL
            ):
                self.running = False
            elif (
                self.day_transition_phase == "fade_in"
                and event.type == pygame.KEYDOWN
            ):
                self.day_transition_phase = None
                self.day_transition_progress = 0.0
            elif (
                self.day_transition_phase == "rewind"
                and event.type == pygame.KEYDOWN
                and event.key in {
                    pygame.K_PLUS,
                    pygame.K_EQUALS,
                    pygame.K_KP_PLUS,
                    pygame.K_MINUS,
                    pygame.K_KP_MINUS,
                }
            ):
                self.adjust_time_speed(
                    -1
                    if event.key in {pygame.K_MINUS, pygame.K_KP_MINUS}
                    else 1
                )
            elif (
                self.day_transition_phase == "rewind"
                and event.type == pygame.MOUSEBUTTONDOWN
                and event.button == 1
                and (
                    self.speed_down_button.collidepoint(event.pos)
                    or self.speed_up_button.collidepoint(event.pos)
                )
            ):
                self.adjust_time_speed(
                    -1 if self.speed_down_button.collidepoint(event.pos) else 1
                )
            elif self.day_transition_phase not in {None, "planner"}:
                continue
            elif event.type == pygame.MOUSEWHEEL:
                mouse_pos = getattr(event, "pos", pygame.mouse.get_pos())
                if self.adjusting_memory and COMMAND_EDITOR_MAP_RECT.collidepoint(mouse_pos):
                    self.zoom_memory_editor_map(event.y, mouse_pos)
                elif LEFT_MESSAGE_HISTORY_RECT.collidepoint(mouse_pos):
                    self.message_scroll_offset = max(
                        0, self.message_scroll_offset + event.y * 3
                    )
                elif MAP_VIEWPORT.collidepoint(mouse_pos):
                    self.camera.set_zoom(
                        self.camera.zoom * (1.12**event.y),
                        mouse_pos,
                        (self.map.width, self.map.height),
                    )
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 2:
                self.camera_dragging = False
                self.camera_drag_position = None
                self.memory_editor_camera_dragging = False
            elif event.type == pygame.KEYUP and event.key in (pygame.K_LCTRL, pygame.K_RCTRL):
                self.finish_additive_selection()
            elif (
                event.type == pygame.MOUSEBUTTONUP
                and event.button == 1
                and self.adjusting_memory
                and self.memory_map_drag_start is not None
            ):
                self.finish_memory_map_selection(event.pos)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1 and self.command_drag_start is not None:
                self.finish_command_drag(event.pos)
            elif event.type == pygame.MOUSEMOTION and self.camera_dragging:
                if self.camera_drag_position is not None:
                    delta = (
                        event.pos[0] - self.camera_drag_position[0],
                        event.pos[1] - self.camera_drag_position[1],
                    )
                    self.camera.pan_by_screen(delta, (self.map.width, self.map.height))
                self.camera_drag_position = event.pos
            elif event.type == pygame.MOUSEMOTION and self.memory_editor_camera_dragging:
                if self.camera_drag_position is not None:
                    self.pan_memory_editor_map(
                        (
                            event.pos[0] - self.camera_drag_position[0],
                            event.pos[1] - self.camera_drag_position[1],
                        )
                    )
                self.camera_drag_position = event.pos
            elif (
                event.type == pygame.MOUSEMOTION
                and self.adjusting_memory
                and self.memory_map_drag_start is not None
                and COMMAND_EDITOR_MAP_RECT.collidepoint(event.pos)
            ):
                self.memory_map_drag_current = self.memory_editor_world_at(event.pos)
            elif event.type == pygame.MOUSEMOTION and self.command_drag_start is not None:
                if MAP_VIEWPORT.collidepoint(event.pos):
                    self.command_drag_current = self.camera.screen_to_world(event.pos)
            elif event.type == pygame.KEYDOWN:
                modifiers = getattr(event, "mod", pygame.key.get_mods())
                number_keys = {
                    pygame.K_1: 0,
                    pygame.K_KP1: 0,
                    pygame.K_2: 1,
                    pygame.K_KP2: 1,
                    pygame.K_3: 2,
                    pygame.K_KP3: 2,
                    pygame.K_4: 3,
                    pygame.K_KP4: 3,
                    pygame.K_5: 4,
                    pygame.K_KP5: 4,
                    pygame.K_6: 5,
                    pygame.K_KP6: 5,
                }
                action_index = number_keys.get(event.key)

                if event.key == pygame.K_h and modifiers & pygame.KMOD_CTRL:
                    self.clear_negative_conditions()
                elif self.adjusting_memory:
                    self.handle_memory_editor_key(
                        event.key, modifiers, event.unicode
                    )
                elif (
                    modifiers & pygame.KMOD_CTRL
                    and event.key in RESOURCE_CHEAT_KEYS
                ):
                    self.player.inventory[RESOURCE_CHEAT_KEYS[event.key]] += 1
                elif event.key == pygame.K_c:
                    self.open_memory_editor()
                elif event.key == pygame.K_F5:
                    self.reload_map()
                elif event.key == pygame.K_F6:
                    self.reload_sprites()
                elif event.key == pygame.K_a:
                    self.start_auto_cheat_memory()
                elif event.key == pygame.K_ESCAPE and self.auto_cheat_memory:
                    self.stop_auto_cheat_memory()
                elif (
                    event.key == pygame.K_p
                    and self.day.mode is not Mode.MORNING
                ):
                    self.toggle_simulation_pause()
                elif event.key in {
                    pygame.K_PLUS,
                    pygame.K_EQUALS,
                    pygame.K_KP_PLUS,
                }:
                    self.adjust_time_speed(1)
                elif event.key in {pygame.K_MINUS, pygame.K_KP_MINUS}:
                    self.adjust_time_speed(-1)
                elif event.key == pygame.K_b:
                    if not self.player.has_bucket:
                        self.create_carried_object("bucket")
                elif event.key == pygame.K_e:
                    self.toggle_equipment_panel()
                elif event.key == pygame.K_r:
                    self.routine_moves_expanded = not self.routine_moves_expanded
                elif self.context_menu_options:
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                        self.context_menu_options = []
                        self.context_menu_pos = None
                        self.context_ground_target = None
                        self.context_inventory_item_id = None
                    elif action_index is not None:
                        self.activate_context_option(action_index)
                elif event.key == pygame.K_ESCAPE:
                    if self.day.mode is Mode.MORNING:
                        self.running = False
                    else:
                        self.cancel_current_command()
                elif self.day.mode is Mode.MORNING:
                    self.handle_morning_key(event.key)
                elif self.day.mode is Mode.DIRECT and self.selected_id and action_index is not None:
                    self.activate_sidebar_action(action_index)
                elif self.day.mode is Mode.DIRECT and action_index is not None:
                    self.activate_area_menu_index(action_index)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if self.adjusting_memory:
                    if event.button == 1:
                        self.handle_memory_editor_click(event.pos)
                    elif event.button == 2 and COMMAND_EDITOR_MAP_RECT.collidepoint(event.pos):
                        self.memory_editor_camera_dragging = True
                        self.camera_drag_position = event.pos
                elif self.day.mode is Mode.MORNING:
                    if event.button == 1:
                        # The planner covers the ordinary controls, so hidden
                        # dock buttons must not consume its clicks.
                        self.handle_morning_click(event.pos)
                elif event.button == 3 and self.handle_inventory_food_click(
                    event.pos
                ):
                    pass
                elif event.button == 2 and MAP_VIEWPORT.collidepoint(event.pos):
                    self.camera_dragging = True
                    self.camera_drag_position = event.pos
                elif event.button == 1 and self.pause_button.collidepoint(event.pos):
                    self.toggle_simulation_pause()
                elif event.button == 1 and self.speed_down_button.collidepoint(event.pos):
                    self.adjust_time_speed(-1)
                elif event.button == 1 and self.speed_up_button.collidepoint(event.pos):
                    self.adjust_time_speed(1)
                elif event.button == 1 and self.step_button.collidepoint(event.pos):
                    self.start_single_command_step()
                elif event.button == 1 and self.equipment_toggle_button.collidepoint(
                    event.pos
                ):
                    self.toggle_equipment_panel()
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos)
                    for rect, _page in self.player_info_tab_buttons
                ):
                    self.inventory_page = next(
                        page
                        for rect, page in self.player_info_tab_buttons
                        if rect.collidepoint(event.pos)
                    )
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos)
                    for rect, _name in self.macro_dropdown_buttons
                ):
                    name = next(
                        name
                        for rect, name in self.macro_dropdown_buttons
                        if rect.collidepoint(event.pos)
                    )
                    self.select_routine_dropdown_entry(name)
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos) for rect, _action in self.macro_buttons
                ):
                    action = next(
                        action
                        for rect, action in self.macro_buttons
                        if rect.collidepoint(event.pos)
                    )
                    if action == "Macro Dropdown":
                        self.macro_dropdown_open = not self.macro_dropdown_open
                    elif action == "Play":
                        if self.macro_recording:
                            self.stop_macro_recording()
                        self.play_selected_macro()
                    elif action == "Record":
                        self.start_macro_recording()
                    elif action == "Stop":
                        self.stop_macro_recording()
                    else:
                        if self.macro_recording:
                            self.stop_macro_recording()
                        self.open_memory_editor()
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos)
                    for rect, _name in self.main_macro_dropdown_buttons
                ):
                    name = next(
                        name
                        for rect, name in self.main_macro_dropdown_buttons
                        if rect.collidepoint(event.pos)
                    )
                    self.select_routine_dropdown_entry(name)
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos)
                    for rect, _action in self.macro_command_buttons
                ):
                    action = next(
                        action
                        for rect, action in self.macro_command_buttons
                        if rect.collidepoint(event.pos)
                    )
                    if action == "Macro Dropdown":
                        self.macro_dropdown_open = not self.macro_dropdown_open
                    elif action == "Play":
                        if self.macro_recording:
                            self.stop_macro_recording()
                        self.play_selected_macro()
                    elif action == "Record":
                        self.start_macro_recording()
                    elif action == "Stop":
                        self.stop_macro_recording()
                    elif action == "Toggle Moves":
                        self.routine_moves_expanded = not self.routine_moves_expanded
                    else:
                        self.open_memory_editor()
                elif event.button == 1 and self.context_menu_options:
                    option_index = next(
                        (index for index, (rect, _) in enumerate(self.context_menu) if rect.collidepoint(event.pos)),
                        None,
                    )
                    if option_index is not None:
                        self.activate_context_option(option_index)
                    else:
                        self.context_menu_options = []
                        self.context_menu_pos = None
                        self.context_ground_target = None
                        self.context_inventory_item_id = None
                elif event.button == 1 and RELOAD_BUTTON.collidepoint(event.pos):
                    self.reload_map()
                elif self.day.mode is Mode.DIRECT:
                    if event.button == 1:
                        quantity_change = next(
                            (change for rect, change in self.area_quantity_buttons if rect.collidepoint(event.pos)),
                            None,
                        )
                        target_selection = next(
                            (
                                mode
                                for rect, mode in self.target_selection_buttons
                                if rect.collidepoint(event.pos)
                            ),
                            None,
                        )
                        till_time_action = next(
                            (
                                action
                                for rect, action in self.till_time_buttons
                                if rect.collidepoint(event.pos)
                            ),
                            None,
                        )
                        command = next(
                            (label for rect, label in self.command_buttons if rect.collidepoint(event.pos)),
                            None,
                        )
                        category = next(
                            (
                                label
                                for rect, label in self.command_category_buttons
                                if rect.collidepoint(event.pos)
                            ),
                            None,
                        )
                        sidebar_action_index = next(
                            (
                                index
                                for index, (rect, _) in enumerate(self.action_buttons)
                                if rect.collidepoint(event.pos)
                            ),
                            None,
                        )
                        if sidebar_action_index is not None:
                            self.activate_sidebar_action(sidebar_action_index)
                        elif till_time_action is not None:
                            if till_time_action == "decrease":
                                self.till_max_game_minutes = max(
                                    60, self.till_max_game_minutes - 60
                                )
                                self.till_until_done = False
                            elif till_time_action == "increase":
                                self.till_max_game_minutes += 60
                                self.till_until_done = False
                            else:
                                self.till_until_done = not self.till_until_done
                        elif target_selection is not None:
                            self.set_target_selection_mode(target_selection)
                        elif quantity_change is not None:
                            self.area_command_quantity = max(
                                1, min(99, self.area_command_quantity + quantity_change)
                            )
                        elif category is not None:
                            self.active_command_category = category
                            self.active_command = None
                            self.selected_id = None
                            self.selected_tile = None
                        elif command is not None:
                            if command == "Back":
                                self.active_command_category = None
                                self.active_command = None
                            else:
                                self.select_area_command(command)
                        elif self.active_command and MAP_VIEWPORT.collidepoint(event.pos):
                            self.command_drag_start = self.camera.screen_to_world(event.pos)
                            self.command_drag_current = self.command_drag_start
                        else:
                            self.handle_world_click(event.pos)
                    elif event.button == 3:
                        self.handle_context_click(event.pos)
                elif (
                    self.day.mode is Mode.REPLAY
                    and event.button == 1
                    and MAP_VIEWPORT.collidepoint(event.pos)
                ):
                    self.handle_world_click(event.pos)

    def reload_map(self) -> None:
        try:
            sync_current_level_from_map(current_level_path=self.persistence_path)
            reloaded_map = load_map(
                persistence_path=self.persistence_path,
                day_number=self.day.attempts,
            )
        except (MapLoadError, ObjectPersistenceError) as exc:
            self.log(f"Map reload failed: {exc}")
            return

        self.map = reloaded_map
        self.objects = reloaded_map.objects
        self.player.carried_objects = [
            obj for obj in self.objects.values() if obj.container == "player"
        ]
        self.camera.clamp((self.map.width, self.map.height))
        self.selected_id = None
        self.selected_tile = None
        self.pending_job = None
        self.walk_target = None
        self.path_target = None
        self.navigation_path = []
        self.preview_path = []
        self.context_menu_options = []
        self.context_menu_pos = None
        self.context_ground_target = None
        self.job_timer = 0.0
        self.area_targets.clear()
        self.pending_area_target = None
        self.area_job_timer = 0.0
        self.barrel_source_selection_id = None
        self.barrel_fill_job = None
        self.pending_water_crop_selection = None
        self.field_water_job = None
        self.pending_source_areas.clear()
        self.pending_target_areas.clear()
        self.pending_empty_area_memory = False
        self.pending_failed_memory_thought = "Why did I come here?"
        self.thought_bubble_text = None
        self.thought_bubble_timer = 0.0
        self.thought_bubble_source = None
        self.condition_thought_cooldown = 2.0
        self.command_drag_start = None
        self.command_drag_current = None
        self.log(f"Reloaded map: {self.map.name}")

    def reload_sprites(self) -> None:
        self.object_sprites.reload()
        self.tile_sprites.reload()
        self.boundary_sprites.reload()
        self.log("Sprites reloaded.")

    def handle_morning_key(self, key: int) -> None:
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.start_planned_day()
        elif key == pygame.K_e:
            self.open_memory_editor()

    def handle_morning_click(self, pos: tuple[int, int]) -> None:
        for rect, action, index in self.day_plan_buttons:
            if rect.collidepoint(pos):
                if action == "start":
                    self.start_planned_day()
                elif action == "editor":
                    self.open_memory_editor()
                elif action == "add_macro" and index is not None:
                    routines = sorted(self.available_command_sets(), key=lambda item: item[0].lower())
                    self.add_macro_to_day_plan(routines[index][0])
                elif action == "add_conditional":
                    insertion = next((i for i, item in enumerate(self.day_plan) if item.kind == "sleep"), len(self.day_plan))
                    self.day_plan.insert(insertion, DayPlanActivity("conditional", "If time ≥ 12:00 PM"))
                    self.selected_conditional_index = insertion
                elif action.startswith("add_activity:"):
                    kind = action.split(":", 1)[1]
                    labels = {
                        "explore": "Explore",
                        "sleep": "Sleep",
                        "power_nap": "Power Nap",
                        "break": "Break",
                    }
                    scheduled = 23 * 60 if kind == "sleep" else None
                    self.day_plan.append(DayPlanActivity(kind, labels[kind], scheduled_minutes=scheduled))
                elif action == "select_conditional" and index is not None:
                    self.selected_conditional_index = index
                elif action == "add_slot" and index is not None:
                    self.day_plan[index].children.append(DayPlanActivity("slot", "Choose routine…"))
                    self.selected_conditional_index = index
                elif action.startswith("remove_slot:") and index is not None:
                    child_index = int(action.split(":", 1)[1])
                    if child_index < len(self.day_plan[index].children):
                        self.day_plan[index].children.pop(child_index)
                elif action == "up" and index is not None and index > 0:
                    self.day_plan[index - 1], self.day_plan[index] = (
                        self.day_plan[index], self.day_plan[index - 1]
                    )
                elif action == "down" and index is not None and index + 1 < len(self.day_plan):
                    self.day_plan[index + 1], self.day_plan[index] = (
                        self.day_plan[index], self.day_plan[index + 1]
                    )
                elif action == "delete" and index is not None:
                    self.day_plan.pop(index)
                return

    def add_macro_to_day_plan(self, name: str) -> None:
        if (
            self.selected_conditional_index is not None
            and self.selected_conditional_index < len(self.day_plan)
            and self.day_plan[self.selected_conditional_index].kind == "conditional"
        ):
            conditional = self.day_plan[self.selected_conditional_index]
            replacement = DayPlanActivity("macro", f"Routine: {name}", macro_name=name)
            empty = next((i for i, child in enumerate(conditional.children) if child.kind == "slot"), None)
            if empty is None:
                conditional.children.append(replacement)
            else:
                conditional.children[empty] = replacement
            return
        insertion = next(
            (i for i, item in enumerate(self.day_plan) if item.kind == "sleep"),
            len(self.day_plan),
        )
        self.day_plan.insert(
            insertion, DayPlanActivity("macro", f"Routine: {name}", macro_name=name)
        )

    def start_planned_day(self) -> None:
        planned_routine: list[RoutineStep] = []
        unavailable: list[str] = []

        def append_activity(activity: DayPlanActivity) -> None:
            if activity.kind == "macro" and activity.macro_name:
                try:
                    planned_routine.extend(
                        load_memory_file(
                            activity.macro_name,
                            tile_size=self.map.tile_map.tile_size,
                            directory=self.memory_directory,
                        )
                    )
                except MapLoadError:
                    unavailable.append(activity.macro_name)
            elif activity.kind in {"power_nap", "sleep"}:
                bed = self.object_of_type("bed")
                action = "Power Nap" if activity.kind == "power_nap" else "Sleep"
                planned_routine.append(
                    RoutineStep(bed.object_id, action, bed.type_id)
                )
            elif activity.kind == "conditional":
                for child in activity.children:
                    append_activity(child)

        for activity in self.day_plan:
            append_activity(activity)

        if planned_routine:
            self.day.remembered_routine = planned_routine
            self.day.mode = Mode.REPLAY
            self.day.replay_index = 0
            self.day.today_routine.clear()
            self.replay_outcome = "planned_day"
            self.record_routine_commands = False
            self.simulation_step_accumulator = 0.0
            self.simulation_paused = False
            self.log(f"Playing {len(planned_routine)} planned commands.")
        else:
            self.choose_morning_option("Direct Control")
        for name in unavailable:
            self.log(f"Skipped unavailable routine: {name}.")
        self.log(f"Day started with {len(self.day_plan)} planned activities.")
        if self.day_transition_phase == "planner":
            self.day_transition_phase = "fade_in"
            self.day_transition_progress = 0.0

    def morning_options(self) -> list[str]:
        options = ["Direct Control"]
        if self.day.remembered_routine:
            options.extend(
                [
                    "Replay Memory and Sleep",
                    "Replay Memory and Expand Routine",
                    "Replay Memory and Explore",
                    "Adjust Memory",
                ]
            )
        return options

    def choose_morning_option(self, label: str) -> None:
        if label == "Direct Control":
            self.day.mode = Mode.DIRECT
            self.day.today_routine.clear()
            self.record_routine_commands = True
            self.log("Direct Control selected. Click an object, then choose an action.")
        elif label == "Adjust Memory":
            self.open_memory_editor()
        else:
            self.day.mode = Mode.REPLAY
            self.day.replay_index = 0
            self.day.today_routine.clear()
            if label == "Replay Memory and Sleep":
                self.replay_outcome = "sleep"
                self.record_routine_commands = False
            elif label == "Replay Memory and Explore":
                self.replay_outcome = "explore"
                self.record_routine_commands = False
            elif label == "Replay Remembered Routine":
                self.replay_outcome = "legacy"
                self.record_routine_commands = True
            else:
                self.replay_outcome = "expand"
                self.day.today_routine = list(self.day.remembered_routine)
                self.record_routine_commands = True
            self.simulation_paused = False
            self.log("Replaying the remembered routine.")

    def start_auto_cheat_memory(self) -> None:
        try:
            loaded = load_memory_file(
                "homestead",
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except MapLoadError as exc:
            self.log(f"Could not load homestead.memory: {exc}")
            return
        self.day.remembered_routine = list(loaded)
        self.auto_cheat_memory = True
        self.memory_edit_index = 0
        self.menu_index = 0
        self.log(
            f"Loaded {len(self.day.remembered_routine)} orders from homestead.memory. Esc stops automatic replay."
        )
        if self.day.mode is Mode.MORNING:
            self.choose_morning_option("Replay Memory and Sleep")

    def stop_auto_cheat_memory(self) -> None:
        self.auto_cheat_memory = False
        self.cancel_current_command()
        self.day.mode = Mode.DIRECT
        self.day.replay_index = 0
        self.replay_outcome = "expand"
        self.record_routine_commands = True
        self.simulation_paused = True
        self.log("Automatic cheat-memory replay stopped.")

    def open_memory_editor(self) -> None:
        if self.day.mode is Mode.MORNING and self.day_transition_phase == "planner":
            # The transition overlay is drawn after the editor and would cover
            # it. Closing the editor naturally returns to the morning planner.
            self.day_transition_phase = None
            self.day_transition_progress = 0.0
        self.memory_editor_previous_pause = self.simulation_paused
        self.simulation_paused = True
        self.adjusting_memory = True
        self.context_menu_options.clear()
        self.context_menu_pos = None
        self.memory_edit_field = None
        self.memory_edit_buffer = ""
        self.memory_browser_open = False
        self.memory_editor_map_camera = None
        self.memory_editor_camera_dragging = False
        self.memory_map_field_selection = None
        if (
            self.day.mode is Mode.REPLAY
            and self.day.replay_index < len(self.day.remembered_routine)
        ):
            self.memory_edit_index = self.day.replay_index
        else:
            self.memory_edit_index = min(
                self.memory_edit_index,
                max(0, len(self.day.remembered_routine) - 1),
            )

    def close_memory_editor(self) -> None:
        self.adjusting_memory = False
        self.memory_edit_field = None
        self.memory_edit_buffer = ""
        self.memory_editor_camera_dragging = False
        self.memory_map_field_selection = None
        self.menu_index = 0
        self.simulation_paused = self.memory_editor_previous_pause

    def handle_memory_editor_key(
        self, key: int, modifiers: int, text_input: str = ""
    ) -> None:
        if self.memory_browser_open:
            if key == pygame.K_ESCAPE:
                self.memory_browser_open = False
            return
        routine = self.day.remembered_routine
        if self.memory_edit_field is not None:
            if key == pygame.K_ESCAPE:
                self.memory_edit_field = None
                self.memory_edit_buffer = ""
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.commit_memory_field()
            elif key == pygame.K_BACKSPACE:
                self.memory_edit_buffer = self.memory_edit_buffer[:-1]
            elif text_input and text_input.isprintable():
                self.memory_edit_buffer += text_input
            return
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE, pygame.K_c):
            self.close_memory_editor()
            return
        if self.editing_history and key not in {pygame.K_UP, pygame.K_DOWN}:
            self.log("History is read-only; duplicate it before editing.")
            return
        if not routine:
            if key == pygame.K_n:
                self.add_memory_step()
            return
        if key == pygame.K_TAB:
            self.indent_memory_step(-1 if modifiers & pygame.KMOD_SHIFT else 1)
        elif key == pygame.K_UP:
            if modifiers & pygame.KMOD_SHIFT:
                self.move_memory_step(-1)
            else:
                self.memory_edit_index = (self.memory_edit_index - 1) % len(routine)
        elif key == pygame.K_DOWN:
            if modifiers & pygame.KMOD_SHIFT:
                self.move_memory_step(1)
            else:
                self.memory_edit_index = (self.memory_edit_index + 1) % len(routine)
        elif key in (pygame.K_DELETE, pygame.K_BACKSPACE):
            self.remove_memory_step()
        elif key == pygame.K_d and modifiers & pygame.KMOD_CTRL:
            self.duplicate_memory_step()
        elif key == pygame.K_n:
            self.add_memory_step()
        elif key == pygame.K_r:
            self.replace_memory_step()

    def handle_memory_editor_click(self, pos: tuple[int, int]) -> None:
        if COMMAND_EDITOR_MAP_RECT.collidepoint(pos) and self.day.remembered_routine:
            if self.editing_history:
                self.log("History is read-only; duplicate it before editing.")
                return
            step = self.day.remembered_routine[self.memory_edit_index]
            if step.action == "Move To":
                point = self.memory_editor_world_at(pos)
                self.day.remembered_routine[self.memory_edit_index] = replace(
                    step,
                    target_id=None,
                    target_type=None,
                    target_point=point,
                    area_bounds=None,
                    target_areas=None,
                    nearest_to_player=False,
                )
                self.memory_map_field_selection = None
            elif step.action == "Harvest and Eat Berries" and routine_field_editor_value(
                step, "target_mode"
            ) in {"area", "specific"}:
                point = self.memory_editor_world_at(pos)
                self.memory_map_drag_start = point
                self.memory_map_drag_current = point
            return
        if self.memory_browser_open:
            for rect, name in self.memory_favorite_buttons:
                if rect.collidepoint(pos):
                    self.toggle_memory_favorite(name)
                    return
            for rect, name in self.memory_browser_rows:
                if rect.collidepoint(pos):
                    self.load_named_memory(name)
                    self.memory_browser_open = False
                    return
            return

        for rect, field_name, value in self.memory_field_dropdown_buttons:
            if rect.collidepoint(pos):
                self.select_memory_field_choice(field_name, value)
                return
        if self.memory_file_name_rect.collidepoint(pos):
            if self.editing_history:
                self.log("History cannot be renamed.")
                return
            self.memory_edit_field = "__memory_file_name__"
            self.memory_edit_buffer = self.memory_file_name
            return
        for rect, index in self.memory_editor_rows:
            if rect.collidepoint(pos):
                self.memory_edit_index = index
                self.memory_edit_field = None
                self.memory_editor_map_camera = None
                return
        for rect, field_name in self.memory_editor_fields:
            if rect.collidepoint(pos) and self.day.remembered_routine:
                if self.editing_history:
                    self.log("History is read-only; duplicate it before editing.")
                    return
                step = self.day.remembered_routine[self.memory_edit_index]
                if field_name == "target_point" and step.action == "Move To":
                    self.memory_map_field_selection = "target_point"
                    self.memory_edit_field = None
                    self.memory_edit_buffer = ""
                    return
                if self.memory_field_choices(field_name) is not None:
                    self.memory_field_dropdown_open = (
                        None
                        if self.memory_field_dropdown_open == field_name
                        else field_name
                    )
                    self.memory_edit_field = None
                    self.memory_edit_buffer = ""
                    return
                self.memory_edit_field = field_name
                value = getattr(
                    self.day.remembered_routine[self.memory_edit_index], field_name
                )
                self.memory_edit_buffer = json.dumps(
                    routine_field_editor_value(
                        self.day.remembered_routine[self.memory_edit_index],
                        field_name,
                    )
                )
                return
        for rect, action in self.memory_editor_buttons:
            if not rect.collidepoint(pos):
                continue
            if self.editing_history and action in {
                "Save",
                "Run Now",
                "Record Routine",
                "Rename Routine",
                "Move Up",
                "Move Down",
                "Remove",
                "Add Command",
                "Add Conditional",
                "Add Loop",
            }:
                self.log("History is read-only; duplicate it before editing.")
                return
            if (
                action in {"Save", "Run Now", "Record Routine"}
                and self.memory_edit_field == "__memory_file_name__"
            ):
                self.commit_memory_field()
                if self.memory_edit_field == "__memory_file_name__":
                    return
            if action == "Done":
                self.close_memory_editor()
            elif action == "Move Up":
                self.move_memory_step(-1)
            elif action == "Move Down":
                self.move_memory_step(1)
            elif action == "Remove":
                self.remove_memory_step()
            elif action == "Duplicate":
                self.duplicate_memory_step()
            elif action == "New":
                self.add_memory_step()
            elif action == "Add Conditional":
                self.toggle_memory_step_condition()
            elif action == "Add Loop":
                self.add_memory_loop()
            elif action == "Add Command":
                self.add_memory_step_inside()
            elif action in {"New Set", "New Routine"}:
                self.new_command_set()
            elif action == "Save":
                self.save_named_memory(self.memory_file_name)
            elif action == "Load":
                self.memory_browser_open = True
            elif action == "Run Now":
                self.run_command_set(self.memory_file_name)
            elif action == "Record Routine":
                self.start_macro_recording()
            elif action == "Duplicate Routine":
                self.duplicate_current_routine()
            elif action == "Rename Routine":
                if self.editing_history:
                    self.log("History cannot be renamed.")
                else:
                    self.routine_rename_source = self.memory_file_name
                    self.memory_edit_field = "__memory_file_name__"
                    self.memory_edit_buffer = self.memory_file_name
            elif action == "Expand Routine":
                self.expand_selected_routine_reference()
            return

    def memory_editor_world_at(self, pos: tuple[int, int]) -> tuple[float, float]:
        camera = self.memory_editor_map_camera
        if camera is None:
            return float(self.player.x), float(self.player.y)
        camera_x, camera_y, zoom = camera
        local_x = (pos[0] - COMMAND_EDITOR_MAP_RECT.x) / COMMAND_EDITOR_MAP_RECT.width
        local_y = (pos[1] - COMMAND_EDITOR_MAP_RECT.y) / COMMAND_EDITOR_MAP_RECT.height
        return (
            camera_x + local_x * MAP_VIEWPORT.width / zoom,
            camera_y + local_y * MAP_VIEWPORT.height / zoom,
        )

    def pan_memory_editor_map(self, delta: tuple[int, int]) -> None:
        camera = self.memory_editor_map_camera
        if camera is None:
            return
        camera_x, camera_y, zoom = camera
        camera_x -= (
            delta[0] * MAP_VIEWPORT.width / COMMAND_EDITOR_MAP_RECT.width / zoom
        )
        camera_y -= (
            delta[1] * MAP_VIEWPORT.height / COMMAND_EDITOR_MAP_RECT.height / zoom
        )
        visible_width = MAP_VIEWPORT.width / zoom
        visible_height = MAP_VIEWPORT.height / zoom
        self.memory_editor_map_camera = (
            max(0.0, min(camera_x, max(0.0, self.map.width - visible_width))),
            max(0.0, min(camera_y, max(0.0, self.map.height - visible_height))),
            zoom,
        )

    def zoom_memory_editor_map(self, direction: int, pos: tuple[int, int]) -> None:
        camera = self.memory_editor_map_camera
        if camera is None:
            return
        world_x, world_y = self.memory_editor_world_at(pos)
        _camera_x, _camera_y, old_zoom = camera
        camera_zoom = max(
            self.camera.min_zoom,
            min(
                old_zoom * self.camera.world_scale * (1.12**direction),
                self.camera.max_zoom,
            ),
        )
        zoom = camera_zoom / self.camera.world_scale
        local_x = (pos[0] - COMMAND_EDITOR_MAP_RECT.x) / COMMAND_EDITOR_MAP_RECT.width
        local_y = (pos[1] - COMMAND_EDITOR_MAP_RECT.y) / COMMAND_EDITOR_MAP_RECT.height
        camera_x = world_x - local_x * MAP_VIEWPORT.width / zoom
        camera_y = world_y - local_y * MAP_VIEWPORT.height / zoom
        visible_width = MAP_VIEWPORT.width / zoom
        visible_height = MAP_VIEWPORT.height / zoom
        self.memory_editor_map_camera = (
            max(0.0, min(camera_x, max(0.0, self.map.width - visible_width))),
            max(0.0, min(camera_y, max(0.0, self.map.height - visible_height))),
            zoom,
        )

    def finish_memory_map_selection(self, pos: tuple[int, int]) -> None:
        if not self.day.remembered_routine:
            self.memory_map_drag_start = None
            self.memory_map_drag_current = None
            return
        step = self.day.remembered_routine[self.memory_edit_index]
        start = self.memory_map_drag_start
        end = (
            self.memory_editor_world_at(pos)
            if COMMAND_EDITOR_MAP_RECT.collidepoint(pos)
            else self.memory_map_drag_current
        )
        self.memory_map_drag_start = None
        self.memory_map_drag_current = None
        if start is None or end is None or step.action != "Harvest and Eat Berries":
            return
        mode = routine_field_editor_value(step, "target_mode")
        if mode == "area":
            bounds = tile_aligned_area_bounds(start, end, self.map.tile_map.tile_size)
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step,
                target_id=None,
                target_type="bush",
                target_point=None,
                area_bounds=bounds,
                target_areas=(bounds,),
                nearest_to_player=False,
                quantity=1,
            )
            return
        if mode == "specific":
            bush = next(
                (
                    obj
                    for obj in reversed(list(self.objects.values()))
                    if obj.active
                    and obj.type_id == "bush"
                    and obj.contains((int(end[0]), int(end[1])))
                ),
                None,
            )
            if bush is None:
                self.log("Specific target: click a berry bush.")
                return
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step,
                target_id=bush.object_id,
                target_type="bush",
                target_point=bush.center,
                area_bounds=None,
                target_areas=None,
                nearest_to_player=False,
                quantity=1,
            )

    def memory_field_choices(
        self, field_name: str
    ) -> list[tuple[str, object]] | None:
        if field_name == "action":
            actions = {"Move To", "Fill Barrel", *NEAREST_AREA_COMMANDS, *BUILD_COMMAND_TYPES}
            for obj in self.objects.values():
                actions.update(obj.interactions)
            return [(action, action) for action in sorted(actions)]
        if field_name == "target_type":
            return [("None", None), *((name, name) for name in sorted(self.map.object_types))]
        if field_name == "target_mode":
            return [
                ("Nearest", "nearest"),
                ("Target", "specific"),
                ("Area", "area"),
            ]
        if field_name in {"till_until_done", "nearest_to_player"}:
            return [("True", True), ("False", False)]
        if field_name == "condition_kind":
            return [("Time", "time"), ("Player stat", "stat"), ("Inventory", "inventory")]
        if field_name == "condition_operator":
            return [(operator, operator) for operator in ("=", "!=", "<", "<=", ">", ">=")]
        if field_name == "condition_subject" and self.day.remembered_routine:
            step = self.day.remembered_routine[self.memory_edit_index]
            if step.condition_kind == "time":
                return [("Game time", "time")]
            if step.condition_kind == "stat":
                return [(name.title(), name) for name in sorted(self.player.conditions)]
            if step.condition_kind == "inventory":
                return [(name.replace("_", " ").title(), name) for name in sorted(self.player.inventory)]
        return None

    def select_memory_field_choice(self, field_name: str, value: object) -> None:
        if not self.day.remembered_routine:
            return
        step = self.day.remembered_routine[self.memory_edit_index]
        if field_name == "action":
            self.day.remembered_routine[self.memory_edit_index] = replace(
                RoutineStep(None, str(value)),
                condition_kind=step.condition_kind,
                condition_subject=step.condition_subject,
                condition_operator=step.condition_operator,
                condition_value=step.condition_value,
            )
        elif field_name == "target_mode":
            mode = str(value)
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step,
                target_id=(step.target_id if mode == "specific" else None),
                target_type="bush",
                target_point=(step.target_point if mode == "specific" else None),
                area_bounds=(step.area_bounds if mode == "area" else None),
                target_areas=(
                    (step.area_bounds,)
                    if mode == "area" and step.area_bounds is not None
                    else ()
                    if mode == "area"
                    else None
                ),
                nearest_to_player=mode == "nearest",
                quantity=1,
            )
        elif field_name == "condition_kind":
            defaults = {"time": ("time", 12 * 60), "stat": ("fatigue", 50), "inventory": ("pebble", 0)}
            subject, threshold = defaults[str(value)]
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step,
                condition_kind=str(value),
                condition_subject=subject,
                condition_value=threshold,
            )
        else:
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step, **{field_name: value}
            )
        self.memory_field_dropdown_open = None
        self.memory_field_dropdown_buttons.clear()

    def new_command_set(self) -> None:
        """Start a blank command set and focus its name for immediate editing."""
        self.day.remembered_routine.clear()
        self.day.replay_index = 0
        self.memory_edit_index = 0
        self.memory_file_name = ""
        self.memory_edit_field = "__memory_file_name__"
        self.memory_edit_buffer = ""

    @property
    def memory_favorites_path(self) -> Path:
        return self.memory_directory / ".favorites.json"

    def load_memory_favorites(self) -> set[str]:
        try:
            payload = json.loads(self.memory_favorites_path.read_text(encoding="utf-8"))
            return {str(name) for name in payload if isinstance(name, str)}
        except (OSError, json.JSONDecodeError, TypeError):
            return set()

    def save_memory_favorites(self) -> None:
        self.memory_directory.mkdir(parents=True, exist_ok=True)
        self.memory_favorites_path.write_text(
            json.dumps(sorted(self.memory_favorites), indent=2) + "\n",
            encoding="utf-8",
        )

    def toggle_memory_favorite(self, name: str) -> None:
        if name in self.memory_favorites:
            self.memory_favorites.remove(name)
        else:
            self.memory_favorites.add(name)
        self.save_memory_favorites()

    def available_command_sets(self) -> list[tuple[str, float]]:
        if not self.memory_directory.is_dir():
            return []
        return [
            (path.stem, path.stat().st_mtime)
            for path in self.memory_directory.glob("*.jsonc")
            if path.is_file()
        ]

    def routine_step_status(self, step: RoutineStep) -> str:
        """Classify a command as available, temporarily blocked, or invalid."""
        if step.action == ROUTINE_REFERENCE_ACTION:
            return "available"
        if step.action == ROUTINE_END_ACTION:
            return "available"
        if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
            valid_subject = (
                step.condition_subject == "time"
                if step.condition_kind == "time"
                else step.condition_subject in self.player.conditions
                if step.condition_kind == "stat"
                else step.condition_subject in self.player.inventory
                if step.condition_kind == "inventory"
                else False
            )
            return (
                "available"
                if valid_subject
                and step.condition_value is not None
                and step.condition_operator in {"=", "!=", "<", "<=", ">", ">="}
                else "invalid"
            )
        if step.action == "Move To":
            if step.target_point is None:
                return "invalid"
            return "available" if self.build_navigation_path(step.target_point) else "blocked"
        if step.action == "Fill Barrel":
            source_areas = step.source_areas or (
                (step.area_bounds,) if step.area_bounds is not None else ()
            )
            target = self.routine_step_target(step)
            if target is None or not target.active or not source_areas:
                return "invalid"
            return "available" if step.action in available_actions(target, self.player) else "blocked"
        if step.action in {"Drink Water", "Drink Until Full"} and step.target_id is None:
            if step.target_point is None:
                return "invalid"
            return "available" if self.build_navigation_path(step.target_point) else "blocked"
        if step.action.startswith("Eat ") and step.target_id is None:
            available = any(
                carried.active
                and "edible" in carried.traits
                and (step.target_type is None or carried.type_id == step.target_type)
                for carried in self.player.carried_objects
            )
            return "available" if available else "blocked"
        if step.action == "Harvest and Eat Berries":
            target = self.berry_routine_target(step)
            if target is None:
                return "blocked"
            return "available" if self.build_navigation_path_to_object(target) else "blocked"
        if step.action in {*NEAREST_AREA_COMMANDS, *BUILD_COMMAND_TYPES}:
            areas = (
                ((0, 0, self.map.width, self.map.height),)
                if step.nearest_to_player
                else step.target_areas
                or ((step.area_bounds,) if step.area_bounds is not None else ())
            )
            if not areas:
                return "invalid"
            if step.action == "Water Crops" and not (
                step.source_areas or step.secondary_bounds is not None
            ):
                return "invalid"
            available = any(self.build_area_targets(step.action, area) for area in areas)
            return "available" if available else "blocked"
        target = self.routine_step_target(step)
        if target is None or not target.active or step.action not in target.interactions:
            return "invalid"
        if step.action not in available_actions(target, self.player):
            return "blocked"
        return "available" if self.build_navigation_path_to_object(target) else "blocked"

    def routine_step_target(self, step: RoutineStep) -> WorldObject | None:
        build_memory_id = self.build_memory_id_for_step(step)
        if build_memory_id is not None:
            return self.object_for_build_memory(build_memory_id)
        return self.objects.get(step.target_id)

    def berry_routine_target(self, step: RoutineStep) -> WorldObject | None:
        """Choose an available bush using the routine's target mode and path cost."""
        areas = step.target_areas or (
            (step.area_bounds,) if step.area_bounds is not None else ()
        )
        candidates = [
            obj
            for obj in self.objects.values()
            if obj.active
            and obj.type_id == "bush"
            and step.action in available_actions(obj, self.player)
            and (
                step.nearest_to_player
                or any(
                    left <= obj.center[0] < right and top <= obj.center[1] < bottom
                    for left, top, right, bottom in areas
                )
                or (not areas and obj.object_id == step.target_id)
            )
        ]
        if not candidates:
            return None
        if not step.nearest_to_player and not areas:
            return candidates[0]
        objects_by_goal: dict[tuple[float, float], list[WorldObject]] = {}
        for obj in candidates:
            for point in self.navigation_interaction_points(obj):
                objects_by_goal.setdefault(point, []).append(obj)
        path = find_tile_path_to_any(
            (self.player.x, self.player.y),
            list(objects_by_goal),
            self.map.tile_map,
        )
        if not path:
            return None
        return min(
            objects_by_goal[path[-1]],
            key=lambda obj: math.dist(obj.center, path[-1]),
        )

    def routine_step_can_complete(self, step: RoutineStep) -> bool:
        return self.routine_step_status(step) == "available"

    def routine_status(
        self, routine: list[RoutineStep] | tuple[RoutineStep, ...]
    ) -> str:
        if not routine:
            return "invalid"
        depth = 0
        for step in routine:
            if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                depth += 1
            elif step.action == ROUTINE_END_ACTION:
                depth -= 1
                if depth < 0:
                    return "invalid"
        if depth:
            return "invalid"
        statuses = {self.routine_step_status(step) for step in routine}
        if "invalid" in statuses:
            return "invalid"
        return "blocked" if "blocked" in statuses else "available"

    def routine_can_complete(
        self, routine: list[RoutineStep] | tuple[RoutineStep, ...]
    ) -> bool:
        return self.routine_status(routine) == "available"

    def named_routine_status(self, name: str) -> str:
        try:
            routine = load_memory_file(
                name,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except MapLoadError:
            return "invalid"
        return self.routine_status(routine)

    def named_routine_can_complete(self, name: str) -> bool:
        return self.named_routine_status(name) == "available"

    @staticmethod
    def routine_status_color(status: str, available_color: tuple[int, int, int]) -> tuple[int, int, int]:
        if status == "invalid":
            return UNAVAILABLE_ROUTINE_COLOR
        if status == "blocked":
            return BLOCKED_ROUTINE_COLOR
        return available_color

    def start_macro_recording(self) -> bool:
        """Record subsequent direct-play commands into the working command set."""
        if self.macro_recording:
            return True
        if self.editing_history:
            self.log("History is read-only. Duplicate it before recording changes.")
            return False
        if self.day.mode is not Mode.DIRECT:
            self.log("Routines can only be recorded during Direct Control.")
            return False
        self.macro_previous_today_routine = self.day.today_routine
        self.day.today_routine = self.day.remembered_routine
        self.macro_record_start_index = len(self.day.remembered_routine)
        self.macro_recording = True
        self.record_routine_commands = True
        if self.adjusting_memory:
            self.close_memory_editor()
        self.log(f"Recording routine {self.memory_file_name or '(unnamed)'!r}.")
        return True

    def record_player_command(self, step: RoutineStep) -> None:
        """Record one directly issued command in both active destinations."""
        self.day.today_routine.append(step)
        self.day.command_history.append(step)

    def stop_macro_recording(self) -> bool:
        if not self.macro_recording:
            return False
        recorded = self.day.remembered_routine[self.macro_record_start_index :]
        previous = self.macro_previous_today_routine
        if previous is not None:
            previous.extend(recorded)
            self.day.today_routine = previous
        self.macro_previous_today_routine = None
        self.macro_recording = False
        self.log(f"Stopped macro recording; added {len(recorded)} command(s).")
        return True

    @property
    def editing_history(self) -> bool:
        return self.memory_file_name.casefold() == "history"

    def duplicate_current_routine(self, new_name: str | None = None) -> bool:
        source_name = self.memory_file_name or "Routine"
        base = new_name or f"{source_name} Copy"
        candidate = base
        suffix = 2
        while memory_file_path(candidate, self.memory_directory).exists():
            candidate = f"{base} {suffix}"
            suffix += 1
        try:
            save_memory_file(
                candidate,
                self.day.remembered_routine,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except (MapLoadError, OSError) as exc:
            self.log(f"Could not duplicate routine: {exc}")
            return False
        self.memory_file_name = candidate
        self.routine_rename_source = candidate
        self.memory_edit_field = "__memory_file_name__"
        self.memory_edit_buffer = candidate
        self.log(f"Duplicated routine as {candidate!r}.")
        return True

    def rename_current_routine(self, new_name: str) -> bool:
        old_name = self.memory_file_name.strip()
        if not old_name or old_name.casefold() == "history":
            self.log("History cannot be renamed.")
            return False
        try:
            old_path = memory_file_path(old_name, self.memory_directory)
            new_path = memory_file_path(new_name, self.memory_directory)
        except MapLoadError as exc:
            self.log(f"Could not rename routine: {exc}")
            return False
        if new_path.exists() and new_path != old_path:
            self.log(f"A routine named {new_path.stem!r} already exists.")
            return False
        if not old_path.exists():
            self.log(f"Routine {old_name!r} no longer exists.")
            return False
        if new_path == old_path:
            self.routine_rename_source = None
            self.memory_file_name = new_path.stem
            return True
        try:
            old_path.rename(new_path)
        except OSError as exc:
            self.log(f"Could not rename routine: {exc}")
            return False
        if old_name in self.memory_favorites:
            self.memory_favorites.remove(old_name)
            self.memory_favorites.add(new_path.stem)
            self.save_memory_favorites()
        self.day.command_history = [
            replace(step, routine_name=new_path.stem)
            if step.action == ROUTINE_REFERENCE_ACTION
            and step.routine_name == old_name
            else step
            for step in self.day.command_history
        ]
        self.memory_file_name = new_path.stem
        self.log(f"Renamed routine to {new_path.stem!r}.")
        return True

    def expand_selected_routine_reference(self) -> bool:
        if not self.day.remembered_routine:
            return False
        step = self.day.remembered_routine[self.memory_edit_index]
        if step.action != ROUTINE_REFERENCE_ACTION or not step.routine_name:
            return False
        try:
            expanded = load_memory_file(
                step.routine_name,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except MapLoadError as exc:
            self.log(f"Could not expand routine: {exc}")
            return False
        if self.routine_reference_has_cycle(step.routine_name, set()):
            self.log("Routine expansion stopped because it contains a reference cycle.")
            return False
        self.day.remembered_routine[self.memory_edit_index:self.memory_edit_index + 1] = expanded
        self.memory_edit_index = min(
            self.memory_edit_index,
            max(0, len(self.day.remembered_routine) - 1),
        )
        self.log(f"Expanded routine {step.routine_name!r}.")
        return True

    def routine_reference_has_cycle(self, name: str, visiting: set[str]) -> bool:
        key = name.casefold()
        if key in visiting:
            return True
        try:
            routine = load_memory_file(
                name,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except MapLoadError:
            return False
        nested = visiting | {key}
        return any(
            step.action == ROUTINE_REFERENCE_ACTION
            and step.routine_name is not None
            and self.routine_reference_has_cycle(step.routine_name, nested)
            for step in routine
        )

    def remove_memory_step(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        removed = self.memory_edit_index
        action = routine[removed].action
        paired_index = None
        if action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
            paired_index = self.matching_routine_end(removed)
        elif action == ROUTINE_END_ACTION:
            paired_index = self.matching_routine_opener(removed)
        if paired_index is not None:
            routine.pop(max(removed, paired_index))
            routine.pop(min(removed, paired_index))
            self.memory_edit_index = min(
                min(removed, paired_index), max(0, len(routine) - 1)
            )
            self.day.replay_index = min(self.day.replay_index, len(routine))
            return
        routine.pop(removed)
        if self.day.mode is Mode.REPLAY and removed < self.day.replay_index:
            self.day.replay_index -= 1
        self.day.replay_index = min(self.day.replay_index, len(routine))
        self.memory_edit_index = min(removed, max(0, len(routine) - 1))

    def duplicate_memory_step(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        if routine[self.memory_edit_index].action in ROUTINE_CONTROL_ACTIONS:
            self.log("Select a command inside the block to duplicate it.")
            return
        insertion = self.memory_edit_index + 1
        routine.insert(insertion, routine[self.memory_edit_index])
        if self.day.mode is Mode.REPLAY and insertion <= self.day.replay_index:
            self.day.replay_index += 1
        self.memory_edit_index = insertion

    def add_memory_step(self) -> None:
        self.day.remembered_routine.append(RoutineStep(None, "Move To"))
        self.memory_edit_index = len(self.day.remembered_routine) - 1

    def toggle_memory_step_condition(self) -> None:
        self.wrap_memory_step(ROUTINE_IF_ACTION)

    def add_memory_loop(self) -> None:
        self.wrap_memory_step(ROUTINE_LOOP_ACTION)

    def wrap_memory_step(self, control_action: str) -> None:
        if not self.day.remembered_routine:
            self.add_memory_step()
        index, end = self.routine_selection_span(self.memory_edit_index)
        control = RoutineStep(
            None,
            control_action,
            condition_kind="time",
            condition_subject="time",
            condition_operator=">=",
            condition_value=12 * 60,
        )
        self.day.remembered_routine.insert(index, control)
        self.day.remembered_routine.insert(end + 2, RoutineStep(None, ROUTINE_END_ACTION))
        self.memory_edit_index = index

    def add_memory_step_inside(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            self.add_memory_step()
            return
        insertion = self.memory_edit_index + 1
        if routine[self.memory_edit_index].action == ROUTINE_END_ACTION:
            insertion = self.memory_edit_index
        routine.insert(insertion, RoutineStep(None, "Move To"))
        self.memory_edit_index = insertion

    def matching_routine_end(self, opener_index: int) -> int | None:
        depth = 0
        for index in range(opener_index + 1, len(self.day.remembered_routine)):
            action = self.day.remembered_routine[index].action
            if action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                depth += 1
            elif action == ROUTINE_END_ACTION:
                if depth == 0:
                    return index
                depth -= 1
        return None

    def matching_routine_opener(self, end_index: int) -> int | None:
        depth = 0
        for index in range(end_index - 1, -1, -1):
            action = self.day.remembered_routine[index].action
            if action == ROUTINE_END_ACTION:
                depth += 1
            elif action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                if depth == 0:
                    return index
                depth -= 1
        return None

    def routine_selection_span(self, index: int) -> tuple[int, int]:
        action = self.day.remembered_routine[index].action
        if action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
            end = self.matching_routine_end(index)
            return index, end if end is not None else index
        if action == ROUTINE_END_ACTION:
            opener = self.matching_routine_opener(index)
            return (opener, index) if opener is not None else (index, index)
        return index, index

    def indent_memory_step(self, direction: int) -> bool:
        """Move the selected command/block in or out by one structural level."""
        routine = self.day.remembered_routine
        if not routine:
            return False
        start, end = self.routine_selection_span(self.memory_edit_index)
        replay_step = (
            routine[self.day.replay_index]
            if self.day.mode is Mode.REPLAY
            and self.day.replay_index < len(routine)
            else None
        )
        if direction > 0:
            if start == 0 or routine[start - 1].action != ROUTINE_END_ACTION:
                self.log("Tab needs a preceding condition or repeat block.")
                return False
            destination = start - 1
        else:
            parent_opener = None
            for candidate in range(start - 1, -1, -1):
                if routine[candidate].action not in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                    continue
                candidate_end = self.matching_routine_end(candidate)
                if candidate_end is not None and candidate_end >= end:
                    parent_opener = candidate
                    break
            if parent_opener is None:
                self.log("The selection is already at the outermost level.")
                return False
            parent_end = self.matching_routine_end(parent_opener)
            if parent_end is None:
                return False
            destination = parent_end + 1

        segment = routine[start : end + 1]
        del routine[start : end + 1]
        if destination > end:
            destination -= len(segment)
        routine[destination:destination] = segment
        self.memory_edit_index = destination
        if replay_step is not None:
            self.day.replay_index = next(
                (
                    index
                    for index, step in enumerate(routine)
                    if step is replay_step
                ),
                min(self.day.replay_index, len(routine)),
            )
        return True

    def routine_step_depth(self, index: int) -> int:
        depth = 0
        for step in self.day.remembered_routine[:index]:
            if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                depth += 1
            elif step.action == ROUTINE_END_ACTION:
                depth = max(0, depth - 1)
        if self.day.remembered_routine[index].action == ROUTINE_END_ACTION:
            depth = max(0, depth - 1)
        return depth

    def routine_step_display_label(self, index: int) -> str:
        step = self.day.remembered_routine[index]
        number = self.routine_step_number(index)
        if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
            return f"{number}. {self.routine_condition_label(step)}"
        if step.action == ROUTINE_END_ACTION:
            opener = self.matching_routine_opener(index)
            return (
                "End Repeat"
                if opener is not None
                and self.day.remembered_routine[opener].action == ROUTINE_LOOP_ACTION
                else "End If"
            )
        if step.action == ROUTINE_REFERENCE_ACTION:
            return f"{number}. Played Routine: {step.routine_name or '(missing)'}"
        quantity = f" ×{step.quantity}" if step.quantity is not None else ""
        return f"{number}. {step.action}{quantity}"

    def routine_step_number(self, index: int) -> int:
        """Number commands and controls among siblings in one structural block."""
        depth = self.routine_step_depth(index)
        scope_start = 0
        openers: list[int] = []
        for candidate, step in enumerate(self.day.remembered_routine[:index]):
            if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                openers.append(candidate)
            elif step.action == ROUTINE_END_ACTION and openers:
                openers.pop()
        if openers:
            scope_start = openers[-1] + 1
        return 1 + sum(
            step.action != ROUTINE_END_ACTION
            and self.routine_step_depth(candidate) == depth
            for candidate, step in enumerate(
                self.day.remembered_routine[scope_start:index], start=scope_start
            )
        )

    def routine_condition_label(self, step: RoutineStep) -> str:
        subject = (step.condition_subject or step.condition_kind or "value").replace("_", " ")
        value: object = step.condition_value
        if step.condition_kind == "time" and step.condition_value is not None:
            minutes = round(step.condition_value) % (24 * 60)
            hour, minute = divmod(minutes, 60)
            suffix = "AM" if hour < 12 else "PM"
            value = f"{12 if hour % 12 == 0 else hour % 12}:{minute:02d} {suffix}"
        prefix = "Repeat until" if step.action == ROUTINE_LOOP_ACTION else "If"
        return f"{prefix} {subject} {step.condition_operator} {value}"

    def routine_step_condition_met(self, step: RoutineStep) -> bool:
        if step.condition_kind is None:
            return True
        if step.condition_value is None:
            return False
        if step.condition_kind == "time":
            actual = float(self.day.current_time_minutes)
        elif step.condition_kind == "stat":
            if step.condition_subject not in self.player.conditions:
                return False
            actual = float(self.player.conditions[step.condition_subject])
        elif step.condition_kind == "inventory":
            if step.condition_subject not in self.player.inventory:
                return False
            actual = float(self.player.inventory[step.condition_subject])
        else:
            return False
        expected = float(step.condition_value)
        comparisons = {
            "=": actual == expected,
            "!=": actual != expected,
            "<": actual < expected,
            "<=": actual <= expected,
            ">": actual > expected,
            ">=": actual >= expected,
        }
        return comparisons.get(step.condition_operator, False)

    def routine_block_has_executable_command(
        self, start_index: int, end_index: int
    ) -> bool:
        """Return whether a reachable command in a control block can run now."""
        index = start_index
        while index < end_index:
            step = self.day.remembered_routine[index]
            if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                nested_end = self.matching_routine_end(index)
                if nested_end is None or nested_end > end_index:
                    return False
                condition_met = self.routine_step_condition_met(step)
                enters_block = (
                    not condition_met
                    if step.action == ROUTINE_LOOP_ACTION
                    else condition_met
                )
                if enters_block and self.routine_block_has_executable_command(
                    index + 1, nested_end
                ):
                    return True
                index = nested_end + 1
                continue
            if (
                step.action != ROUTINE_END_ACTION
                and self.routine_step_condition_met(step)
                and self.routine_step_status(step) == "available"
            ):
                return True
            index += 1
        return False

    def commit_memory_field(self) -> None:
        if self.memory_edit_field == "__memory_file_name__":
            if not self.memory_edit_buffer.strip():
                self.log("Enter a command set name before saving.")
                return
            try:
                # Resolving validates the name without touching the filesystem.
                memory_file_path(self.memory_edit_buffer, self.memory_directory)
            except MapLoadError as exc:
                self.log(str(exc))
                return
            new_name = self.memory_edit_buffer.removesuffix(".jsonc").removesuffix(".memory")
            if self.routine_rename_source is not None:
                source = self.routine_rename_source
                self.routine_rename_source = None
                self.memory_file_name = source
                if not self.rename_current_routine(new_name):
                    return
            else:
                self.memory_file_name = new_name
            self.memory_edit_field = None
            self.memory_edit_buffer = ""
            return
        if self.memory_edit_field is None or not self.day.remembered_routine:
            return
        try:
            value = json.loads(self.memory_edit_buffer)
            field_name = self.memory_edit_field
            if field_name in {
                "target_point",
                "area_bounds",
                "secondary_bounds",
                "source_areas",
                "target_areas",
            }:
                value = routine_field_runtime_value(field_name, value)
            elif field_name == "action" and not isinstance(value, str):
                raise ValueError("action requires a JSON string")
            elif field_name in {
                "target_type",
                "target_build_memory",
                "condition_kind",
                "condition_subject",
                "condition_operator",
            } and not (
                value is None or isinstance(value, str)
            ):
                raise ValueError(f"{field_name} requires a JSON string or null")
            elif field_name in {
                "target_id",
                "quantity",
                "max_game_minutes",
            } and not (value is None or isinstance(value, int)):
                raise ValueError(f"{field_name} requires an integer or null")
            elif field_name in {"till_until_done", "nearest_to_player"} and not isinstance(
                value, bool
            ):
                raise ValueError(f"{field_name} requires true or false")
            elif field_name == "condition_value" and not isinstance(value, (int, float)):
                raise ValueError("condition_value requires a number")
            step = self.day.remembered_routine[self.memory_edit_index]
            self.day.remembered_routine[self.memory_edit_index] = replace(
                step, **{field_name: value}
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.log(f"Memory field is invalid: {exc}")
            return
        self.memory_edit_field = None
        self.memory_edit_buffer = ""

    def save_named_memory(self, name: str) -> None:
        try:
            path = save_memory_file(
                name,
                self.day.remembered_routine,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except (OSError, MapLoadError) as exc:
            self.log(f"Could not save command set: {exc}")
            return
        self.log(
            f"Saved {len(self.day.remembered_routine)} orders to {path.name}."
        )

    def load_named_memory(self, name: str) -> bool:
        if name.casefold() == "history":
            self.day.remembered_routine = list(self.day.command_history)
            self.memory_file_name = "History"
            self.day.replay_index = 0
            self.memory_edit_index = min(
                self.memory_edit_index,
                max(0, len(self.day.remembered_routine) - 1),
            )
            self.log(f"Loaded History ({len(self.day.remembered_routine)} commands).")
            return True
        try:
            routine = load_memory_file(
                name,
                tile_size=self.map.tile_map.tile_size,
                directory=self.memory_directory,
            )
        except MapLoadError as exc:
            self.log(f"Could not load command set: {exc}")
            return False
        self.day.remembered_routine = list(routine)
        self.memory_file_name = name.removesuffix(".jsonc").removesuffix(".memory")
        self.day.replay_index = 0
        self.memory_edit_index = 0
        self.memory_edit_field = None
        self.memory_edit_buffer = ""
        self.log(
            f"Loaded {len(routine)} orders from "
            f"{memory_file_path(name, self.memory_directory).name}."
        )
        return True

    def select_routine_dropdown_entry(self, name: str) -> None:
        self.macro_dropdown_open = False
        if name == "__new__":
            self.open_memory_editor()
            self.new_command_set()
            return
        self.memory_file_name = name
        self.load_named_memory(name)

    def play_selected_macro(self) -> bool:
        name = self.memory_file_name.strip()
        if not name:
            self.log("Select a macro before pressing Play.")
            return False
        if name.casefold() == "history":
            self.log("History is read-only; duplicate it to create a playable routine.")
            return False
        if not self.load_named_memory(name):
            return False
        return self.run_command_set(name)

    def run_command_set(self, name: str) -> bool:
        if self.day.mode is not Mode.DIRECT:
            self.log("A command set can only be launched during Direct Control.")
            return False
        if self.has_queued_command():
            self.log("Finish or cancel the current command before launching a command set.")
            return False
        if not self.day.remembered_routine:
            self.log("The command set is empty.")
            return False
        self.day.command_history.append(
            RoutineStep(None, ROUTINE_REFERENCE_ACTION, routine_name=name)
        )
        self.running_command_set_name = name or "unnamed"
        self.day.mode = Mode.REPLAY
        self.day.replay_index = 0
        self.replay_outcome = "command_set"
        self.record_routine_commands = False
        self.close_memory_editor()
        self.simulation_paused = False
        self.log(
            f"Running command set {self.running_command_set_name!r} "
            f"({len(self.day.remembered_routine)} orders)."
        )
        return True

    def move_memory_step(self, direction: int) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        if routine[self.memory_edit_index].action in ROUTINE_CONTROL_ACTIONS:
            self.log("Condition and repeat blocks keep their structural position.")
            return
        destination = self.memory_edit_index + direction
        if not 0 <= destination < len(routine):
            return
        routine[self.memory_edit_index], routine[destination] = (
            routine[destination],
            routine[self.memory_edit_index],
        )
        if self.day.mode is Mode.REPLAY:
            if self.day.replay_index == self.memory_edit_index:
                self.day.replay_index = destination
            elif self.day.replay_index == destination:
                self.day.replay_index = self.memory_edit_index
        self.memory_edit_index = destination

    def replace_memory_step(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        step = routine[self.memory_edit_index]
        if step.action == "Fill Barrel" or step.source_areas is not None:
            self.log("That compound memory keeps its specialized command.")
            return
        if step.area_bounds is None:
            self.log("Object-specific memories cannot change action type yet.")
            return
        replacements = [
            *AREA_COMMAND_TYPES,
            "Chop Trees",
            "Till Grassland",
            "Plant Wheat",
            "Tend Crops",
            "Harvest Wheat",
        ]
        try:
            index = replacements.index(step.action)
        except ValueError:
            index = -1
        action = replacements[(index + 1) % len(replacements)]
        routine[self.memory_edit_index] = replace(
            step,
            action=action,
            target_areas=step.target_areas or (step.area_bounds,),
        )

    def handle_world_click(self, pos: tuple[int, int]) -> None:
        world_pos = self.screen_to_world(pos)
        if world_pos is None:
            return
        for obj in reversed(list(self.objects.values())):
            if obj.contains(world_pos):
                self.selected_id = obj.object_id
                self.selected_tile = None
                if self.day.mode is Mode.REPLAY:
                    return
                self.path_target = (float(obj.x + obj.width // 2), float(obj.y + obj.height // 2))
                self.walk_target = None
                self.preview_path = self.build_navigation_path_to_object(obj)[1:]
                return
        self.selected_id = None
        located = self.map.tile_map.tile_at_world(*world_pos)
        self.selected_tile = (located[0], located[1]) if located is not None else None
        if self.day.mode is Mode.REPLAY:
            return
        self.preview_path = []
        self.navigation_path = []
        self.walk_target = None
        self.path_target = None

    def handle_context_click(self, pos: tuple[int, int]) -> None:
        self.context_inventory_item_id = None
        self.context_boundary_id = None
        self.context_option_target_ids = []
        world_pos = self.screen_to_world(pos)
        if world_pos is None:
            return
        door = self.door_at_world(*world_pos)
        if door is not None:
            self.selected_id = None
            self.selected_tile = None
            self.context_boundary_id = door.boundary_id
            self.context_menu_options = ["Open Door" if not door.open else "Close Door"]
            self.context_option_target_ids = [None]
            self.context_menu_pos = pos
            self.context_ground_target = None
            self.preview_path = []
            return
        targets = [
            obj
            for obj in reversed(list(self.objects.values()))
            if obj.active and obj.contains(world_pos)
        ]
        located = self.map.tile_map.tile_at_world(*world_pos)
        self.selected_tile = (located[0], located[1]) if located is not None else None
        target = self.map.tile_map.center_at_world(*world_pos)
        ground_options = (
            build_ground_context_menu_options(
                located[2],
                self.player,
                self.map.tile_states.get((located[0], located[1])),
                self.crop_at_tile(located[0], located[1]),
            )
            if target is not None and located is not None
            else []
        )
        if targets:
            primary = targets[0]
            self.selected_id = primary.object_id
            self.selected_tile = None
            self.path_target = primary.center
            self.preview_path = self.build_navigation_path_to_object(primary)[1:]
            if self.day.mode is Mode.REPLAY:
                return
            self.context_menu_options = ["Move To"]
            self.context_option_target_ids = [primary.object_id]
            for obj in targets:
                actions = object_action_menu_options(obj, self.player)
                self.context_menu_options.extend(actions)
                self.context_option_target_ids.extend([obj.object_id] * len(actions))
            tile_actions = ground_options[1:] if ground_options[:1] == ["Move To"] else ground_options
            self.context_menu_options.extend(tile_actions)
            self.context_option_target_ids.extend([None] * len(tile_actions))
        else:
            self.selected_id = None
            self.path_target = target
            self.context_menu_options = ground_options
            self.context_option_target_ids = [None] * len(ground_options)
        self.context_menu_pos = pos if target is not None else None
        self.context_ground_target = target
        self.preview_path = []

    def show_context_menu(self, obj: WorldObject, screen_pos: tuple[int, int], world_pos: tuple[int, int]) -> None:
        self.context_inventory_item_id = None
        options = build_context_menu_options(obj, self.player, world_pos, self.objects)
        if not options:
            self.log("No available action.")
            self.context_menu_options = []
            self.context_menu_pos = None
            return
        self.context_menu_options = options
        self.context_option_target_ids = [obj.object_id] * len(options)
        self.context_menu_pos = screen_pos
        self.context_ground_target = None
        self.selected_id = obj.object_id

    def activate_context_option(self, index: int) -> None:
        if not 0 <= index < len(self.context_menu_options):
            return
        option = self.context_menu_options[index]
        option_target_id = (
            self.context_option_target_ids[index]
            if len(self.context_option_target_ids) == len(self.context_menu_options)
            else self.selected_id
        )
        if not action_option_enabled(option):
            if option == RUINED_BUCKET_REQUIRED:
                self.log("The ruined bucket cannot hold water.")
            return
        inventory_item_id = self.context_inventory_item_id
        boundary_id = self.context_boundary_id
        self.context_menu_options = []
        self.context_option_target_ids = []
        self.context_menu_pos = None
        self.context_inventory_item_id = None
        self.context_boundary_id = None
        if option in {"Open Door", "Close Door"} and boundary_id is not None:
            door = next(
                (item for item in self.map.boundaries if item.boundary_id == boundary_id),
                None,
            )
            if door is None:
                return
            if door.locked:
                self.log("The door is locked.")
                return
            if self.distance_to_boundary(door, (self.player.x, self.player.y)) > self.interaction_distance:
                self.log("Move closer to the door.")
                return
            door.open = option == "Open Door"
            self.log("Opened the door." if door.open else "Closed the door.")
            return
        if inventory_item_id is not None and option.startswith("Eat "):
            food = self.objects.get(inventory_item_id)
            if (
                food is not None
                and food.active
                and food in self.player.carried_objects
                and "edible" in food.traits
            ):
                if self.record_routine_commands:
                    self.record_rewind_checkpoint(option)
                    self.record_player_command(
                        RoutineStep(None, option, food.type_id)
                    )
                self.consume_carried_food(food, "from inventory")
            return
        if option == "Fill Barrel" and option_target_id is not None:
            self.begin_barrel_source_selection(option_target_id)
            return
        if option == "Move To":
            if option_target_id is None:
                if self.context_ground_target is not None and self.plan_path(self.context_ground_target):
                    self.resume_for_command()
                    self.path_target = self.context_ground_target
                    self.log("Moving to selected location.")
                elif self.context_ground_target is not None:
                    self.log("No route to selected location.")
                self.context_ground_target = None
                return
            target = self.objects[option_target_id]
            if self.plan_path_to_object(target):
                self.resume_for_command()
                self.path_target = target.center
                self.log(f"Moving to {target.name}.")
            else:
                self.log(f"No route to {target.name}.")
            self.context_ground_target = None
            return
        if option == "Drop Bucket" and option_target_id is None:
            target = self.context_ground_target
            if target is not None and self.plan_path(target):
                self.pending_area_target = AreaTarget("Drop Bucket", target)
                self.area_job_timer = 0.0
                self.resume_for_command()
                self.path_target = target
            self.context_ground_target = None
            return
        if option in {"Drink Water", "Drink Until Full"} and option_target_id is None:
            target = self.context_ground_target
            if target is not None and self.queue_terrain_drink(
                target, record=True, until_full=option == "Drink Until Full"
            ):
                self.log(
                    "Going to drink until full."
                    if option == "Drink Until Full"
                    else "Going to drink water."
                )
            else:
                self.log("That terrain action is unavailable.")
            self.context_ground_target = None
            return
        if option in {"Gather Water", "Water Crop", "Tend Plant"} and option_target_id is None:
            target = self.context_ground_target
            located = self.map.tile_map.tile_at_world(*target) if target is not None else None
            state = self.map.tile_states.get((located[0], located[1])) if located is not None else None
            crop = self.crop_at_tile(located[0], located[1]) if located is not None else None
            crop_state = crop.state if crop is not None else {}
            valid = located is not None and (
                (
                    option == "Gather Water"
                    and located[2].kind in {TileKind.SHALLOW_WATER, TileKind.POND}
                    and self.player.has_bucket
                    and self.bucket_capacity > 0
                    and not self.player.bucket_filled
                )
                or (
                    option == "Water Crop"
                    and crop is not None
                    and self.player.has_bucket
                    and self.player.bucket_filled
                    and float(crop_state.get("water", 0.0)) < 100.0
                )
                or (
                    option == "Tend Plant"
                    and crop is not None
                    and float(crop_state.get("tended", 0.0)) < 100.0
                    and float(crop_state.get("growth_progress", 0.0)) < 1.0
                )
            )
            if target is not None and valid and self.plan_path(target):
                action = "Water Crops" if option == "Water Crop" else option
                self.pending_area_target = AreaTarget(action, target)
                self.area_job_timer = 0.0
                self.resume_for_command()
                self.path_target = target
                self.log(f"Going to {option.lower()}.")
            else:
                self.log("That terrain action is unavailable.")
            self.context_ground_target = None
            return
        if option_target_id is None:
            return
        self.queue_job(option_target_id, option, record=True)

    def queue_terrain_drink(
        self,
        target: tuple[float, float],
        *,
        record: bool,
        until_full: bool = False,
    ) -> bool:
        located = self.map.tile_map.tile_at_world(*target)
        if (
            located is None
            or located[2].kind not in {TileKind.SHALLOW_WATER, TileKind.POND}
            or not self.plan_path(target)
        ):
            return False
        action = "Drink Until Full" if until_full else "Drink Water"
        self.pending_area_target = AreaTarget(action, target)
        self.area_job_timer = 0.0
        self.resume_for_command()
        self.path_target = target
        if record and self.record_routine_commands:
            self.record_rewind_checkpoint(action)
            self.record_player_command(
                RoutineStep(None, action, target_point=target)
            )
        return True

    def activate_sidebar_action(self, index: int) -> None:
        if self.selected_id is None:
            return
        obj = self.objects[self.selected_id]
        actions = object_action_menu_options(obj, self.player)
        if index >= len(actions):
            return
        action = actions[index]
        if not action_option_enabled(action):
            return
        if action == "Fill Barrel":
            self.begin_barrel_source_selection(self.selected_id)
            return
        self.queue_job(self.selected_id, action, record=True)

    def begin_barrel_source_selection(self, barrel_id: int) -> None:
        self.barrel_source_selection_id = barrel_id
        self.pending_source_areas.clear()
        self.pending_target_areas.clear()
        self.active_command = "Fill Barrel"
        self.selected_id = None
        self.log("Fill Barrel: left-drag a water area, or left-click one water tile.")

    def activate_area_menu_index(self, index: int) -> None:
        if self.active_command_category is None:
            if index >= len(AREA_COMMAND_CATEGORIES):
                return
            self.active_command_category = AREA_COMMAND_CATEGORIES[index]
            self.active_command = None
            self.selected_id = None
            self.selected_tile = None
            return
        entries = [
            *area_commands_for_category(self.player, self.active_command_category),
            "Back",
        ]
        if index >= len(entries):
            return
        selected = entries[index]
        if selected == "Back":
            self.active_command_category = None
            self.active_command = None
            return
        self.select_area_command(selected)

    def select_area_command(self, command: str) -> None:
        self.pending_target_areas.clear()
        if (
            self.target_selection_mode == "nearest"
            and command in NEAREST_AREA_COMMANDS
        ):
            self.active_command = None
            self.queue_nearest_area_command(
                command,
                self.area_command_quantity,
                record=True,
                max_game_minutes=(
                    None
                    if command == "Till Grassland" and self.till_until_done
                    else self.till_max_game_minutes
                    if command == "Till Grassland"
                    else None
                ),
                till_until_done=(
                    self.till_until_done if command == "Till Grassland" else False
                ),
            )
            return
        self.active_command = command
        self.log(self.target_selection_prompt(command))

    def set_target_selection_mode(self, mode: str) -> None:
        if mode not in {"nearest", "target", "area"}:
            return
        self.target_selection_mode = mode
        self.command_drag_start = None
        self.command_drag_current = None
        self.pending_target_areas.clear()
        if self.active_command is None:
            return
        command = self.active_command
        if mode == "nearest" and command in NEAREST_AREA_COMMANDS:
            self.active_command = None
            self.queue_nearest_area_command(
                command,
                self.area_command_quantity,
                record=True,
                max_game_minutes=(
                    None
                    if command == "Till Grassland" and self.till_until_done
                    else self.till_max_game_minutes
                    if command == "Till Grassland"
                    else None
                ),
                till_until_done=(
                    self.till_until_done if command == "Till Grassland" else False
                ),
            )
            return
        self.log(self.target_selection_prompt(command))

    def target_selection_prompt(self, command: str) -> str:
        if self.target_selection_mode == "target":
            return f"{command}: click an x, y target on the map."
        if self.target_selection_mode == "area":
            return f"{command}: drag an area target on the map."
        return f"{command}: use targets nearest to the character."

    def target_selection_summary(self) -> str:
        point = self.command_drag_current or self.command_drag_start
        if point is None:
            return "Target: select x, y" if self.target_selection_mode == "target" else "Area: select on map"
        size = self.map.tile_map.tile_size
        if self.target_selection_mode == "target":
            return f"Target: ({int(point[0] // size)}, {int(point[1] // size)})"
        start = self.command_drag_start or point
        left, top, right, bottom = tile_aligned_area_bounds(start, point, size)
        return (
            f"Area: ({left // size}, {top // size})–"
            f"({right // size - 1}, {bottom // size - 1})"
        )

    def screen_to_world(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        return self.camera.screen_to_world(pos)

    def finish_command_drag(self, screen_pos: tuple[int, int]) -> None:
        end = self.camera.screen_to_world(screen_pos)
        start = self.command_drag_start
        self.command_drag_start = None
        self.command_drag_current = None
        if start is None or end is None or self.active_command is None:
            return
        if (
            self.target_selection_mode == "target"
            and self.active_command not in {"Fill Barrel", "Water Crops Source"}
        ):
            start = end
        left, top, right, bottom = tile_aligned_area_bounds(
            start, end, self.map.tile_map.tile_size
        )
        quantity = self.area_command_quantity
        if self.active_command == "Water Crops":
            self.pending_water_crop_selection = ((left, top, right, bottom), quantity)
            self.pending_source_areas.clear()
            self.active_command = "Water Crops Source"
            self.log("Water Crops: left-drag a water-source area, or left-click one water tile.")
            return
        if self.active_command == "Water Crops Source":
            self.pending_source_areas.append((left, top, right, bottom))
            if pygame.key.get_mods() & pygame.KMOD_CTRL:
                self.log("Water source area added. Keep selecting, then release Ctrl to finish.")
                return
            selection = self.pending_water_crop_selection
            self.pending_water_crop_selection = None
            source_areas = tuple(self.pending_source_areas)
            self.pending_source_areas.clear()
            self.active_command = None
            if selection is not None:
                self.queue_field_water_command(
                    selection[0],
                    source_areas,
                    selection[1],
                    record=True,
                )
            return
        if self.active_command == "Fill Barrel":
            barrel_id = self.barrel_source_selection_id
            self.pending_source_areas.append((left, top, right, bottom))
            if pygame.key.get_mods() & pygame.KMOD_CTRL:
                self.log("Water source area added. Keep selecting, then release Ctrl to finish.")
                return
            self.active_command = None
            self.barrel_source_selection_id = None
            source_areas = tuple(self.pending_source_areas)
            self.pending_source_areas.clear()
            if barrel_id is not None:
                self.queue_barrel_fill_command(
                    barrel_id,
                    source_areas,
                    record=True,
                )
            return
        if self.active_command in BUILD_COMMAND_TYPES:
            quantity = 1
        gather_commands = {*AREA_COMMAND_TYPES, "Chop Trees"}
        if self.active_command in gather_commands:
            self.pending_target_areas.append((left, top, right, bottom))
            if pygame.key.get_mods() & pygame.KMOD_CTRL:
                self.log("Gathering area added. Keep selecting, then release Ctrl to finish.")
                return
            target_areas = tuple(self.pending_target_areas)
            self.pending_target_areas.clear()
            self.queue_area_command(
                self.active_command,
                target_areas[0],
                quantity,
                record=True,
                target_areas=target_areas,
                max_game_minutes=(
                    (
                        None
                        if self.till_until_done
                        else self.till_max_game_minutes
                    )
                    if self.active_command == "Till Grassland"
                    else None
                ),
                till_until_done=(
                    self.till_until_done
                    if self.active_command == "Till Grassland"
                    else False
                ),
            )
            self.active_command = None
            return
        self.queue_area_command(
            self.active_command,
            (left, top, right, bottom),
            quantity,
            record=True,
        )
        self.active_command = None

    def finish_additive_selection(self) -> None:
        if self.command_drag_start is not None:
            return
        if self.active_command == "Fill Barrel" and self.pending_source_areas:
            barrel_id = self.barrel_source_selection_id
            source_areas = tuple(self.pending_source_areas)
            self.pending_source_areas.clear()
            self.barrel_source_selection_id = None
            self.active_command = None
            if barrel_id is not None:
                self.queue_barrel_fill_command(barrel_id, source_areas, record=True)
            return
        if self.active_command == "Water Crops Source" and self.pending_source_areas:
            selection = self.pending_water_crop_selection
            source_areas = tuple(self.pending_source_areas)
            self.pending_source_areas.clear()
            self.pending_water_crop_selection = None
            self.active_command = None
            if selection is not None:
                self.queue_field_water_command(
                    selection[0], source_areas, selection[1], record=True
                )
            return
        if self.active_command in {*AREA_COMMAND_TYPES, "Chop Trees"} and self.pending_target_areas:
            target_areas = tuple(self.pending_target_areas)
            self.pending_target_areas.clear()
            command = self.active_command
            self.active_command = None
            self.queue_area_command(
                command,
                target_areas[0],
                self.area_command_quantity,
                record=True,
                target_areas=target_areas,
                max_game_minutes=(
                    None
                    if command == "Till Grassland" and self.till_until_done
                    else self.till_max_game_minutes
                    if command == "Till Grassland"
                    else None
                ),
                till_until_done=(
                    self.till_until_done if command == "Till Grassland" else False
                ),
            )

    def queue_barrel_fill_command(
        self,
        barrel_id: int,
        source_areas: tuple[tuple[int, int, int, int], ...] | tuple[int, int, int, int],
        *,
        record: bool,
    ) -> None:
        source_areas = self.normalize_source_areas(source_areas)
        barrel = self.objects.get(barrel_id)
        if barrel is None or barrel.kind is not ObjectKind.BARREL or not barrel.active:
            if self.day.mode is Mode.REPLAY:
                self.visit_failed_memory(
                    None, "Fill Barrel", "the barrel is no longer here"
                )
            return
        self.barrel_fill_job = BarrelFillJob(barrel_id, source_areas)
        if record and self.record_routine_commands:
            self.record_rewind_checkpoint("Fill Barrel")
            self.record_player_command(
                RoutineStep(
                    barrel_id,
                    "Fill Barrel",
                    barrel.type_id,
                    area_bounds=source_areas[0],
                    target_point=barrel.center,
                    source_areas=source_areas,
                    target_build_memory=str(
                        barrel.state.get("build_memory_id")
                    )
                    if barrel.state.get("build_memory_id")
                    else None,
                )
            )
        self.resume_for_command()
        self.log("Filling the barrel from the selected water-source area.")

    def queue_field_water_command(
        self,
        crop_bounds: tuple[int, int, int, int],
        source_areas: tuple[tuple[int, int, int, int], ...] | tuple[int, int, int, int],
        quantity: int,
        *,
        record: bool,
    ) -> None:
        source_areas = self.normalize_source_areas(source_areas)
        crop_points = [
            target.point
            for target in self.build_area_targets("Water Crops", crop_bounds)
        ]
        crop_points.sort(
            key=lambda point: math.dist((self.player.x, self.player.y), point)
        )
        crop_points = crop_points[:quantity]
        if record and self.record_routine_commands:
            self.record_rewind_checkpoint("Water Crops")
            self.record_player_command(
                RoutineStep(
                    None,
                    "Water Crops",
                    area_bounds=crop_bounds,
                    quantity=quantity,
                    secondary_bounds=source_areas[0],
                    source_areas=source_areas,
                )
            )
        if not crop_points:
            if self.day.mode is Mode.REPLAY:
                center = (
                    (crop_bounds[0] + crop_bounds[2]) / 2,
                    (crop_bounds[1] + crop_bounds[3]) / 2,
                )
                self.visit_failed_memory(
                    center, "Water Crops", "none of these crops need water"
                )
            else:
                self.log("No crops in that area currently need water.")
            return
        self.field_water_job = FieldWaterJob(
            crop_bounds, source_areas, quantity, crop_points
        )
        self.resume_for_command()
        self.log(f"Watering {len(crop_points)} selected crop{'s' if len(crop_points) != 1 else ''}.")

    @staticmethod
    def normalize_source_areas(
        source_areas: tuple[tuple[int, int, int, int], ...] | tuple[int, int, int, int],
    ) -> tuple[tuple[int, int, int, int], ...]:
        if len(source_areas) == 4 and isinstance(source_areas[0], int):
            return (source_areas,)  # type: ignore[return-value]
        return source_areas  # type: ignore[return-value]

    def queue_area_command(
        self,
        command: str,
        bounds: tuple[int, int, int, int],
        quantity: int,
        *,
        record: bool,
        target_areas: tuple[tuple[int, int, int, int], ...] | None = None,
        max_game_minutes: int | None = None,
        till_until_done: bool = False,
    ) -> None:
        target_areas = target_areas or (bounds,)
        build_type = BUILD_COMMAND_TYPES.get(command)
        if build_type is not None and not self.can_afford_build(build_type):
            cost = self.build_cost(build_type)
            requirements = " and ".join(
                f"{amount} {item}" for item, amount in cost.items()
            )
            self.log(f"Need {requirements} to build a {build_type.replace('_', ' ')}.")
            if self.day.mode is Mode.REPLAY:
                center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
                self.visit_failed_memory(
                    center, command, "I do not have the required materials"
                )
            return
        targets = [
            target
            for area in target_areas
            for target in self.build_area_targets(command, area)
        ]
        targets = list(
            {
                (target.action, target.point, target.target_id): target
                for target in targets
            }.values()
        )
        targets.sort(key=lambda target: math.dist((self.player.x, self.player.y), target.point))
        if command == "Till Grassland":
            # Quantity limits how many distinct tiles are included. The time
            # budget controls how many till actions are performed among them.
            targets = targets[:quantity]
            repetitions: list[AreaTarget] = []
            progress_per_action = self.map.till_progress_per_action
            for target in targets:
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is None:
                    continue
                column, row, _tile = located
                state = self.map.tile_states.setdefault(
                    (column, row), LevelTileState(column, row)
                )
                if state.persistence_modifier is None:
                    state.persistence_modifier = self.persistence_modifier(
                        f"tile:{column}:{row}",
                        self.map.tile_persistence_modifier_range,
                    )
                increment = progress_per_action * state.persistence_modifier
                remaining_actions = max(
                    0,
                    math.ceil(
                        (100.0 - state.till_percentage) / increment - 1e-9
                    ),
                )
                repetitions.extend(
                    AreaTarget(
                        target.action,
                        target.point,
                        target.target_id,
                        target.prerequisite_target_ids,
                        target.placement_point,
                    )
                    for _ in range(remaining_actions)
                )
            if till_until_done:
                targets = repetitions
            else:
                budget = max_game_minutes if max_game_minutes is not None else 60
                action_minutes = tilling_duration_seconds(self.player.hoe_quality)
                full_actions = max(0, int(budget // action_minutes))
                targets = repetitions[:full_actions]
                remainder = budget - full_actions * action_minutes
                if remainder > 1e-9 and full_actions < len(repetitions):
                    partial = repetitions[full_actions]
                    partial.work_fraction = min(1.0, remainder / action_minutes)
                    targets.append(partial)
        elif command == "Plant Wheat":
            targets = targets[: self.player.inventory["seed"]]
        elif command == "Gather Water":
            targets = targets[:1]
        elif command == "Water Crops":
            targets = targets[: min(quantity, self.player.bucket_water_uses)]
        elif build_type is not None:
            cost = self.build_cost(build_type)
            affordable = min(
                (self.player.inventory[item] // amount for item, amount in cost.items()),
                default=0,
            )
            targets = targets[:min(1, affordable)]
        if command != "Till Grassland":
            targets = targets[:quantity]
        self.area_targets = targets
        self.pending_area_target = None
        self.area_job_timer = 0.0
        if record and self.record_routine_commands:
            self.record_rewind_checkpoint(command)
            self.record_player_command(
                RoutineStep(
                    None,
                    command,
                    area_bounds=bounds,
                    quantity=quantity,
                    target_areas=target_areas,
                    max_game_minutes=max_game_minutes,
                    till_until_done=till_until_done,
                )
            )
        if targets:
            self.resume_for_command()
        elif self.day.mode is Mode.REPLAY:
            already_built = (
                command in BUILD_COMMAND_TYPES
                and any(
                    obj.active
                    and obj.type_id == build_type
                    and any(
                        area[0] <= obj.center[0] <= area[2]
                        and area[1] <= obj.center[1] <= area[3]
                        for area in target_areas
                    )
                    for obj in self.objects.values()
                )
            )
            if already_built:
                self.pending_failed_memory_thought = self.failed_memory_thought(
                    command, "already built"
                )
                self.show_empty_area_memory_thought()
                self.resume_for_command()
                self.log(
                    f"Area command skipped because the {build_type} is already built."
                )
                return
            center = (
                (bounds[0] + bounds[2]) / 2,
                (bounds[1] + bounds[3]) / 2,
            )
            self.pending_empty_area_memory = True
            self.pending_failed_memory_thought = self.failed_memory_thought(command)
            if not self.plan_path(center):
                self.show_empty_area_memory_thought()
            self.resume_for_command()
        elif build_type is not None:
            self.log(f"A {build_type.replace('_', ' ')} cannot be built on that tile.")
        self.log(f"Area command queued {len(targets)} target{'s' if len(targets) != 1 else ''}.")

    def queue_nearest_gather_command(
        self, command: str, quantity: int, *, record: bool
    ) -> None:
        self.queue_nearest_area_command(command, quantity, record=record)

    def queue_nearest_area_command(
        self,
        command: str,
        quantity: int,
        *,
        record: bool,
        max_game_minutes: int | None = None,
        till_until_done: bool = False,
    ) -> None:
        if command not in NEAREST_AREA_COMMANDS:
            return
        routine_count = len(self.day.today_routine)
        self.queue_area_command(
            command,
            (0, 0, self.map.width, self.map.height),
            quantity,
            record=record,
            max_game_minutes=max_game_minutes,
            till_until_done=till_until_done,
        )
        if (
            record
            and self.record_routine_commands
            and len(self.day.today_routine) > routine_count
        ):
            self.day.today_routine[-1] = replace(
                self.day.today_routine[-1],
                area_bounds=None,
                target_areas=None,
                nearest_to_player=True,
            )
        self.log(
            f"Nearest area command chose {len(self.area_targets)} "
            f"target{'s' if len(self.area_targets) != 1 else ''}."
        )

    def show_failed_nearest_gather(self, command: str) -> None:
        self.pending_failed_memory_thought = self.failed_memory_thought(command)
        self.show_empty_area_memory_thought()

    def show_empty_area_memory_thought(self) -> None:
        self.pending_empty_area_memory = False
        self.thought_bubble_text = self.pending_failed_memory_thought
        self.pending_failed_memory_thought = "Why did I come here?"
        self.thought_bubble_timer = 2.5
        self.thought_bubble_source = "memory"
        self.log(self.thought_bubble_text)

    @staticmethod
    def failed_memory_thought(action: str, reason: str | None = None) -> str:
        if action in BUILD_COMMAND_TYPES and reason == "already built":
            return "I remember building it fondly."
        reasons = {
            "Gather Pebbles": "there are no pebbles here",
            "Gather Branches": "there are no branches here",
            "Gather Seeds": "there are no seeds here",
            "Gather Tall Grass": "there is no tall grass here",
            "Harvest Berries": "there are no ripe berries here",
            "Chop Trees": "there are no trees here to chop",
            "Till Grassland": "there is no grassland here to till",
            "Plant Wheat": "there is no open soil here to plant",
            "Water Crops": "none of these crops need water",
            "Tend Crops": "none of these crops need tending",
            "Harvest Wheat": "there is no mature wheat here",
            "Build Barrel": "a barrel cannot be built here",
            "Build Cupboard": "a cupboard cannot be built here",
            "Fill Barrel": "I cannot fill the barrel from here",
        }
        explanation = reason or reasons.get(action, "I cannot do that now")
        return f"I came here to {action.lower()}, but {explanation}."

    def visit_failed_memory(
        self,
        point: tuple[float, float] | None,
        action: str = "do something",
        reason: str | None = None,
    ) -> None:
        destination = point or (self.player.x, self.player.y)
        self.pending_empty_area_memory = True
        self.pending_failed_memory_thought = self.failed_memory_thought(action, reason)
        if not self.plan_path(destination):
            self.show_empty_area_memory_thought()
        self.resume_for_command()

    def build_area_targets(
        self, command: str, bounds: tuple[int, int, int, int]
    ) -> list[AreaTarget]:
        left, top, right, bottom = bounds
        targets: list[AreaTarget] = []
        type_ids = AREA_COMMAND_TYPES.get(command)
        if type_ids is not None:
            for obj in self.objects.values():
                center_x, center_y = obj.center
                action = (
                    "Harvest Berries"
                    if command == "Harvest Berries"
                    else "Gather"
                )
                if (
                    obj.active
                    and obj.type_id in type_ids
                    and action in available_actions(obj, self.player)
                    and left <= center_x <= right
                    and top <= center_y <= bottom
                ):
                    targets.append(AreaTarget(action, obj.center, obj.object_id))
        elif command == "Chop Trees":
            if not self.player.carrying_axe:
                return []
            for obj in self.objects.values():
                center_x, center_y = obj.center
                if (
                    obj.active
                    and obj.kind is ObjectKind.TREE
                    and "Chop Down Tree" in available_actions(obj, self.player)
                    and left <= center_x <= right
                    and top <= center_y <= bottom
                ):
                    targets.append(
                        AreaTarget("Chop Down Tree", obj.center, obj.object_id)
                    )
        elif command in {"Gather Water", "Till Grassland", "Plant Wheat", "Water Crops", "Tend Crops", "Harvest Wheat", *BUILD_COMMAND_TYPES}:
            if command == "Gather Water" and (not self.player.has_bucket or self.player.bucket_filled):
                return []
            if command == "Gather Water" and self.bucket_capacity <= 0:
                return []
            if command == "Till Grassland" and not self.player.carrying_hoe:
                return []
            if command == "Plant Wheat" and self.player.inventory["seed"] <= 0:
                return []
            if command == "Water Crops" and not self.player.has_bucket:
                return []
            if command in BUILD_COMMAND_TYPES and (
                not self.can_afford_build(BUILD_COMMAND_TYPES[command])
            ):
                return []
            tile_map = self.map.tile_map
            first_row = max(0, int(top // tile_map.tile_size))
            last_row = min(tile_map.rows - 1, int(bottom // tile_map.tile_size))
            first_column = max(0, int(left // tile_map.tile_size))
            last_column = min(tile_map.columns - 1, int(right // tile_map.tile_size))
            for row in range(first_row, last_row + 1):
                for column in range(first_column, last_column + 1):
                    tile = tile_map.tile_at(column, row)
                    point = tile_map.tile_center(column, row)
                    if not (left <= point[0] <= right and top <= point[1] <= bottom) or tile is None:
                        continue
                    if command == "Gather Water" and tile.kind is TileKind.SHALLOW_WATER:
                        targets.append(AreaTarget("Gather Water", point))
                    elif command in BUILD_COMMAND_TYPES:
                        active_objects = [
                            self.objects.get(int(prop.removeprefix("object:")))
                            for prop in tile.properties
                            if prop.startswith("object:") and prop.removeprefix("object:").isdigit()
                        ]
                        if (
                            self.can_stand_at(*point)
                            and "blocked" not in tile.properties
                            and not any(obj is not None and obj.active for obj in active_objects)
                        ):
                            adjacent = [
                                (point[0] + dx, point[1] + dy)
                                for dx, dy in (
                                    (-tile_map.tile_size, 0),
                                    (tile_map.tile_size, 0),
                                    (0, -tile_map.tile_size),
                                    (0, tile_map.tile_size),
                                )
                                if self.can_stand_at(point[0] + dx, point[1] + dy)
                            ]
                            if adjacent:
                                interaction = min(
                                    adjacent,
                                    key=lambda candidate: math.dist(
                                        (self.player.x, self.player.y), candidate
                                    ),
                                )
                                targets.append(
                                    AreaTarget(
                                        command,
                                        interaction,
                                        placement_point=point,
                                    )
                                )
                    elif (
                        command == "Till Grassland"
                        and tile.kind is TileKind.GRASSLAND
                        and "blocked" not in tile.properties
                    ):
                        tile_objects = []
                        for prop in tile.properties:
                            if not prop.startswith("object:"):
                                continue
                            try:
                                obj = self.objects.get(int(prop.removeprefix("object:")))
                            except ValueError:
                                obj = None
                            if obj is not None and obj.active:
                                tile_objects.append(obj)
                        gatherable = [
                            obj for obj in tile_objects
                            if "Gather" in available_actions(obj, self.player)
                            or "Pull Berry Bush" in available_actions(obj, self.player)
                        ]
                        if len(gatherable) == len(tile_objects):
                            targets.append(
                                AreaTarget(
                                    "Till Grassland",
                                    point,
                                    prerequisite_target_ids=tuple(
                                        obj.object_id for obj in gatherable
                                    ),
                                )
                            )
                    elif command == "Plant Wheat" and tile.kind is TileKind.SOIL:
                        if self.crop_at_tile(column, row) is None:
                            targets.append(AreaTarget("Plant Wheat", point))
                    elif command == "Water Crops" and tile.kind is TileKind.SOIL:
                        crop = self.crop_at_tile(column, row)
                        if (
                            crop is not None
                            and float(crop.state.get("water", 0.0)) < 100.0
                        ):
                            targets.append(AreaTarget("Water Crops", point))
                    elif command == "Tend Crops" and tile.kind is TileKind.SOIL:
                        crop = self.crop_at_tile(column, row)
                        if (
                            crop is not None
                            and float(crop.state.get("tended", 0.0)) < 100.0
                            and float(crop.state.get("growth_progress", 0.0)) < 1.0
                        ):
                            targets.append(AreaTarget("Tend Plant", point))
                    elif command == "Harvest Wheat" and tile.kind is TileKind.SOIL:
                        crop = self.crop_at_tile(column, row)
                        if (
                            crop is not None
                            and crop.variant == "wheat"
                            and crop.form == "mature"
                        ):
                            targets.append(
                                AreaTarget("Harvest Wheat", point, crop.object_id)
                            )
        return targets

    def queue_job(
        self,
        target_id: int,
        action: str,
        *,
        record: bool,
        advances_replay: bool = True,
    ) -> bool:
        if self.pending_job is not None:
            return False
        obj = self.objects.get(target_id)
        if action == "Power Nap":
            if self.player.conditions["fatigue"] <= 0:
                self.log("You are already fully rested.")
                return False
        if obj is None or action not in available_actions(obj, self.player):
            self.log(f"Skipped unavailable job: {action}.")
            if self.day.mode is Mode.REPLAY and advances_replay:
                self.day.replay_index += 1
                self.visit_failed_memory(
                    obj.center if obj is not None else None,
                    action,
                    "that task is no longer available",
                )
            return False
        if not self.plan_path_to_object(obj):
            self.log(f"No route to {obj.name}.")
            if self.day.mode is Mode.REPLAY and advances_replay:
                self.day.replay_index += 1
                self.visit_failed_memory(
                    obj.center, action, f"I cannot reach the {obj.name.lower()}"
                )
            return False
        self.pending_job = PendingJob(target_id, action, self.walk_target, advances_replay)
        self.resume_for_command()
        self.selected_id = None
        self.preview_path = []
        self.job_timer = 0.0
        if record and self.record_routine_commands and action != "Sleep":
            self.record_rewind_checkpoint(action)
            self.record_player_command(
                RoutineStep(
                    target_id,
                    action,
                    obj.type_id,
                    target_point=obj.center,
                    target_build_memory=str(obj.state.get("build_memory_id"))
                    if obj.state.get("build_memory_id")
                    else None,
                )
            )
        return True

    def resolve_routine_target(self, step: RoutineStep) -> int | None:
        """Resolve an object-specific order without changing what it referred to."""
        build_memory_id = self.build_memory_id_for_step(step)
        if build_memory_id is not None:
            built = self.object_for_build_memory(build_memory_id)
            if built is not None and step.action in available_actions(built, self.player):
                return built.object_id
            return None
        original = self.objects.get(step.target_id)
        if original is not None and step.action in available_actions(original, self.player):
            return original.object_id
        return None

    def build_memory_id_for_step(self, step: RoutineStep) -> str | None:
        if step.target_build_memory is not None:
            return step.target_build_memory
        if step.target_type is None or step.target_point is None:
            return None
        column = int(step.target_point[0] // self.map.tile_map.tile_size)
        row = int(step.target_point[1] // self.map.tile_map.tile_size)
        candidate = f"{step.target_type}:{column}:{row}"
        return candidate if candidate in self.build_memories else None

    def plan_path(self, target: tuple[float, float]) -> bool:
        path = self.build_navigation_path(target)
        if not path:
            self.navigation_path = []
            self.walk_target = None
            return False
        self.navigation_path = path[1:]
        self.walk_target = target
        return True

    def plan_path_to_object(self, obj: WorldObject) -> bool:
        path = self.build_navigation_path_to_object(obj)
        if not path:
            self.navigation_path = []
            self.walk_target = None
            return False
        self.navigation_path = path[1:]
        self.walk_target = path[-1]
        return True

    def build_navigation_path(self, target: tuple[float, float]) -> list[tuple[float, float]]:
        start = (self.player.x, self.player.y)
        tile_path = find_tile_path(start, target, self.map.tile_map)
        if tile_path is None:
            return []
        if tile_path and tile_path[0] == start:
            return [start, *tile_path[1:]]
        return [start, *tile_path]

    def build_navigation_path_to_object(self, obj: WorldObject) -> list[tuple[float, float]]:
        candidates = self.navigation_interaction_points(obj)
        path = find_tile_path_to_any(
            (self.player.x, self.player.y), candidates, self.map.tile_map
        )
        return path or []

    def navigation_interaction_points(
        self, obj: WorldObject
    ) -> list[tuple[float, float]]:
        tile_map = self.map.tile_map
        size = tile_map.tile_size
        support_id = obj.state.get("support_id")
        support = self.objects.get(support_id) if isinstance(support_id, int) else None
        interaction_body = support if support is not None and support.active else obj
        first_column = interaction_body.x // size
        last_column = (interaction_body.x + interaction_body.width - 1) // size
        first_row = interaction_body.y // size
        last_row = (interaction_body.y + interaction_body.height - 1) // size
        candidates: list[tuple[float, float]] = []
        for row in range(first_row - 1, last_row + 2):
            for column in range(first_column - 1, last_column + 2):
                if first_column <= column <= last_column and first_row <= row <= last_row:
                    continue
                center = tile_map.tile_center(column, row)
                if (
                    tile_map.tile_at(column, row) is not None
                    and self.can_stand_at(*center)
                    and distance_to_object(center, obj) <= self.interaction_distance
                    and self.tile_can_access_object(column, row, interaction_body)
                ):
                    candidates.append(center)

        return candidates

    def tile_can_access_object(self, column: int, row: int, obj: WorldObject) -> bool:
        """Return whether a tile shares an open cardinal edge with an object."""
        tile_map = self.map.tile_map
        size = tile_map.tile_size
        first_column = obj.x // size
        last_column = (obj.x + obj.width - 1) // size
        first_row = obj.y // size
        last_row = (obj.y + obj.height - 1) // size
        candidate = tile_map.tile_at(column, row)
        if candidate is None:
            return False

        neighbor: tuple[int, int] | None = None
        candidate_edge: TileEdge | None = None
        object_edge: TileEdge | None = None
        if row == first_row - 1 and first_column <= column <= last_column:
            neighbor = (column, first_row)
            candidate_edge, object_edge = TileEdge.SOUTH, TileEdge.NORTH
        elif row == last_row + 1 and first_column <= column <= last_column:
            neighbor = (column, last_row)
            candidate_edge, object_edge = TileEdge.NORTH, TileEdge.SOUTH
        elif column == first_column - 1 and first_row <= row <= last_row:
            neighbor = (first_column, row)
            candidate_edge, object_edge = TileEdge.EAST, TileEdge.WEST
        elif column == last_column + 1 and first_row <= row <= last_row:
            neighbor = (last_column, row)
            candidate_edge, object_edge = TileEdge.WEST, TileEdge.EAST
        if neighbor is None or candidate_edge is None or object_edge is None:
            return False
        object_tile = tile_map.tile_at(*neighbor)
        if object_tile is None:
            return False
        if obj.blocks_movement:
            candidate_room = next(
                (prop for prop in candidate.properties if prop.startswith("room:")), None
            )
            object_room = next(
                (prop for prop in object_tile.properties if prop.startswith("room:")), None
            )
            return candidate_room == object_room
        return candidate.passable[candidate_edge] and object_tile.passable[object_edge]

    def move_along_path(self, dt: float) -> bool:
        trauma = float(self.player.conditions.get("trauma", 0.0))
        injury = max(0.0, min(1.0, (trauma - 25.0) / 75.0))
        pace = 1.0
        if injury > 0.0:
            self.stagger_phase = (self.stagger_phase + dt * (3.5 + injury * 2.5)) % math.tau
            pace = 1.0 - injury * (0.18 + 0.32 * (0.5 + 0.5 * math.sin(self.stagger_phase * 1.7)))
        remaining_distance = max(
            0.0,
            self.player.speed
            * movement_speed_multiplier(self.player)
            * pace
            * dt,
        )
        while self.navigation_path and remaining_distance > 0.0:
            target_x, target_y = self.navigation_path[0]
            door = self.door_crossed_by_step(
                (self.player.x, self.player.y), (target_x, target_y)
            )
            if door is not None and not door.open:
                if door.locked:
                    self.navigation_path = []
                    self.walk_target = None
                    return False
                door.open = True
                self.log("Stopped to open the door.")
                return True
            dx = target_x - self.player.x
            dy = target_y - self.player.y
            distance = math.hypot(dx, dy)
            if distance <= 0.001:
                self.player.x, self.player.y = target_x, target_y
                self.navigation_path.pop(0)
                continue
            amount = min(distance, remaining_distance)
            heading_x, heading_y = dx / distance, dy / distance
            if injury > 0.0 and amount < distance:
                # Rotate the intended heading from side to side. Re-aiming at
                # the waypoint each tick keeps the stagger inside pathfinding.
                angle = math.sin(self.stagger_phase) * math.radians(24.0) * injury
                cosine, sine = math.cos(angle), math.sin(angle)
                stagger_x = heading_x * cosine - heading_y * sine
                stagger_y = heading_x * sine + heading_y * cosine
                next_x = self.player.x + stagger_x * amount
                next_y = self.player.y + stagger_y * amount
            else:
                next_x = self.player.x + heading_x * amount
                next_y = self.player.y + heading_y * amount
            if not self.can_stand_at(next_x, next_y):
                # Near obstacles, prefer the safe center line to abandoning
                # an otherwise valid route because of a cosmetic stumble.
                next_x = self.player.x + heading_x * amount
                next_y = self.player.y + heading_y * amount
                if not self.can_stand_at(next_x, next_y):
                    self.navigation_path = []
                    self.walk_target = None
                    return False
            self.player.x = next_x
            self.player.y = next_y
            remaining_distance -= amount
            if amount >= distance:
                self.player.x, self.player.y = target_x, target_y
                self.navigation_path.pop(0)
        self.walk_target = None
        if self.navigation_path:
            self.walk_target = self.navigation_path[-1]
        return True

    def door_at_world(self, x: float, y: float) -> BoundaryObject | None:
        """Return a door whose closed doorway edge is near a world position."""
        tolerance = max(8.0, 10.0 / self.camera.effective_zoom)
        return next(
            (
                boundary
                for boundary in reversed(self.map.boundaries)
                if boundary.kind == "door"
                and self.distance_to_boundary(boundary, (x, y)) <= tolerance
            ),
            None,
        )

    def distance_to_boundary(
        self, boundary: BoundaryObject, point: tuple[float, float]
    ) -> float:
        size = self.map.tile_map.tile_size
        x, y = point
        line_x = boundary.column * size
        line_y = boundary.row * size
        if boundary.edge == "west":
            nearest_y = max(line_y, min(line_y + size, y))
            return math.dist((x, y), (line_x, nearest_y))
        nearest_x = max(line_x, min(line_x + size, x))
        return math.dist((x, y), (nearest_x, line_y))

    def door_crossed_by_step(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> BoundaryObject | None:
        """Find the canonical boundary crossed by a cardinal tile-center step."""
        size = self.map.tile_map.tile_size
        start_column, start_row = int(start[0] // size), int(start[1] // size)
        end_column, end_row = int(end[0] // size), int(end[1] // size)
        if (start_column, start_row) == (end_column, end_row):
            return None
        if end_column > start_column:
            address = (end_column, start_row, "west")
        elif end_column < start_column:
            address = (start_column, start_row, "west")
        elif end_row > start_row:
            address = (start_column, end_row, "north")
        else:
            address = (start_column, start_row, "north")
        return next(
            (
                boundary
                for boundary in self.map.boundaries
                if boundary.kind == "door"
                and (boundary.column, boundary.row, boundary.edge) == address
            ),
            None,
        )

    def can_stand_at(self, x: float, y: float) -> bool:
        if (
            x < self.player_radius
            or y < self.player_radius
            or x > self.map.width - self.player_radius
            or y > self.map.height - self.player_radius
        ):
            return False

        if not self.map.tile_map.can_stand_at(x, y, self.player_radius):
            return False
        return True

    def player_spawn(self) -> tuple[float, float]:
        character = self.map.characters.get(self.map.controlled_character_id or -1)
        bed = (
            self.objects[character.last_sleep_id]
            if character is not None
            else self.object_of_type("bed")
        )
        tile_map = self.map.tile_map
        first_column = bed.x // tile_map.tile_size
        last_column = (bed.x + bed.width - 1) // tile_map.tile_size
        first_row = bed.y // tile_map.tile_size
        last_row = (bed.y + bed.height - 1) // tile_map.tile_size
        candidates = [
            tile_map.tile_center(column, row)
            for row in range(first_row - 2, last_row + 3)
            for column in range(first_column - 2, last_column + 3)
            if tile_map.tile_at(column, row) is not None
        ]
        standable = [
            point
            for point in candidates
            if tile_map.can_stand_at(*point, self.player_radius)
        ]
        if not standable:
            raise MapLoadError("The sleep object has no valid adjacent player spawn tile")
        north_of_bed = [point for point in standable if point[1] < bed.y]
        if north_of_bed:
            return min(north_of_bed, key=lambda point: (math.dist(point, bed.center), abs(point[0] - bed.center[0])))
        return min(standable, key=lambda point: (math.dist(point, bed.center), point[1], point[0]))

    def object_of_type(self, type_id: str) -> WorldObject:
        return next(obj for obj in self.objects.values() if obj.type_id == type_id)

    def has_queued_command(self) -> bool:
        return bool(
            self.pending_job is not None
            or self.pending_area_target is not None
            or self.area_targets
            or self.navigation_path
            or self.barrel_fill_job is not None
            or self.field_water_job is not None
        )

    def resume_for_command(self) -> None:
        self.simulation_paused = False
        if self.single_step_active:
            self.single_step_command_started = True

    def toggle_simulation_pause(self) -> None:
        self.simulation_paused = not self.simulation_paused
        if not self.simulation_paused:
            self.single_step_active = False
            self.single_step_command_started = False
        self.log("Paused." if self.simulation_paused else "Playing.")

    def toggle_equipment_panel(self) -> None:
        self.equipment_collapsed = not self.equipment_collapsed

    def adjust_time_speed(self, direction: int) -> None:
        speeds = [speed for _label, speed in TIME_SPEED_OPTIONS]
        try:
            index = speeds.index(self.time_speed)
        except ValueError:
            index = min(
                range(len(speeds)),
                key=lambda candidate: abs(speeds[candidate] - self.time_speed),
            )
        index = max(0, min(len(speeds) - 1, index + direction))
        self.time_speed = speeds[index]
        label = TIME_SPEED_OPTIONS[index][0]
        self.log(f"Time speed: {label}.")

    def start_single_command_step(self) -> None:
        if self.day.mode is Mode.MORNING:
            return
        self.single_step_active = True
        self.single_step_command_started = self.has_queued_command()
        self.simulation_paused = False
        self.log("Single-command step started.")

    def cancel_current_command(self) -> None:
        cancelled_command_set = (
            self.day.mode is Mode.REPLAY
            and self.replay_outcome == "command_set"
        )
        recorded_action: str | None = None
        if self.field_water_job is not None:
            recorded_action = "Water Crops"
        elif self.barrel_fill_job is not None:
            recorded_action = "Fill Barrel"
        elif self.area_targets or self.pending_area_target is not None:
            if self.day.today_routine and self.day.today_routine[-1].area_bounds is not None:
                recorded_action = self.day.today_routine[-1].action
        elif self.pending_job is not None:
            recorded_action = self.pending_job.action
        if (
            recorded_action is not None
            and self.day.today_routine
            and self.day.today_routine[-1].action == recorded_action
        ):
            self.day.today_routine.pop()
        had_command = bool(
            self.active_command
            or self.command_drag_start
            or self.pending_job
            or self.pending_area_target
            or self.area_targets
            or self.barrel_fill_job
            or self.field_water_job
            or self.navigation_path
        )
        self.active_command = None
        self.command_drag_start = None
        self.command_drag_current = None
        self.barrel_source_selection_id = None
        self.pending_water_crop_selection = None
        self.pending_source_areas.clear()
        self.pending_target_areas.clear()
        self.pending_job = None
        self.pending_area_target = None
        self.area_targets.clear()
        self.barrel_fill_job = None
        self.field_water_job = None
        self.navigation_path = []
        self.preview_path = []
        self.walk_target = None
        self.path_target = None
        self.selected_id = None
        self.selected_tile = None
        if self.day.mode is Mode.REPLAY:
            self.day.mode = Mode.DIRECT
        if cancelled_command_set:
            cancelled_name = self.running_command_set_name or "unnamed"
            self.running_command_set_name = None
            self.replay_outcome = "expand"
            self.record_routine_commands = True
            self.log(f"Command set {cancelled_name!r} stopped.")
        if had_command:
            self.log("Command cancelled.")

    def advance_crop_growth(self, game_minutes: float) -> None:
        growth = self.map.object_types["crop"].growth
        for crop in (
            obj
            for obj in self.objects.values()
            if obj.active and obj.type_id == "crop"
        ):
            progress = float(crop.state.get("growth_progress", 0.0))
            if progress >= 1.0 or crop.form == "dead":
                continue
            crop.state["age"] = float(crop.state.get("age", 0.0)) + game_minutes
            remaining_minutes = game_minutes
            base_minutes = float(growth["base_minutes"])
            while remaining_minutes > 1e-9 and progress < 1.0:
                water = max(0.0, min(100.0, float(crop.state.get("water", 0.0))))
                tended = max(
                    0.0, min(100.0, float(crop.state.get("tended", 0.0)))
                )
                water_multiplier = 1.0 + (
                    float(growth["water_multiplier"]) - 1.0
                ) * water / 100.0
                tended_multiplier = 1.0 + (
                    float(growth["tended_multiplier"]) - 1.0
                ) * tended / 100.0
                rate = water_multiplier * tended_multiplier / base_minutes
                next_step = min(100, math.floor(progress * 100 + 1e-9) + 1)
                next_boundary = next_step / 100.0
                minutes_to_boundary = (next_boundary - progress) / rate
                if remaining_minutes + 1e-9 < minutes_to_boundary:
                    progress += remaining_minutes * rate
                    remaining_minutes = 0.0
                    break
                progress = next_boundary
                remaining_minutes -= minutes_to_boundary
                self.decay_crop_care(crop, next_step)
            crop.state["growth_progress"] = progress
            next_form = (
                "mature"
                if progress >= 1.0
                else "growing"
                if progress >= 0.5
                else "sprout"
                if progress >= 0.15
                else "seed"
            )
            if crop.form != next_form:
                self.apply_object_form(crop, next_form)

    def decay_crop_care(self, crop: WorldObject, growth_percent: int) -> None:
        decay_rules = self.map.object_types["crop"].growth.get(
            "decay_per_growth_percent", {}
        )
        if not isinstance(decay_rules, dict):
            return
        for field in ("water", "tended"):
            bounds = decay_rules.get(field, [0.0, 0.0])
            if not isinstance(bounds, list) or len(bounds) != 2:
                continue
            decay_percent = random.Random(
                f"remembering:crop-care-decay:{crop.object_id}:{growth_percent}:{field}"
            ).uniform(float(bounds[0]), float(bounds[1]))
            crop.state[field] = max(
                0.0,
                float(crop.state.get(field, 0.0))
                * (1.0 - decay_percent / 100.0),
            )

    def crop_at_tile(self, column: int, row: int) -> WorldObject | None:
        tile_size = self.map.tile_map.tile_size
        return next(
            (
                obj
                for obj in self.objects.values()
                if obj.active
                and obj.type_id == "crop"
                and int(obj.center[0] // tile_size) == column
                and int(obj.center[1] // tile_size) == row
            ),
            None,
        )

    def plant_crop(
        self, column: int, row: int, variant: str = "wheat"
    ) -> WorldObject:
        definition = self.map.object_types["crop"]
        form = definition.form_definition("seed", variant)
        growth = definition.growth
        tile_size = self.map.tile_map.tile_size
        center_x, center_y = self.map.tile_map.tile_center(column, row)
        initial_randomizer = random.Random(
            f"remembering:crop-initial-care:{max(self.objects, default=0) + 1}"
        )

        def initial_percentage(field: str) -> float:
            bounds = growth.get(field, [0.0, 5.0])
            if not isinstance(bounds, list) or len(bounds) != 2:
                return 0.0
            return initial_randomizer.uniform(float(bounds[0]), float(bounds[1]))

        crop = WorldObject(
            object_id=max(self.objects, default=0) + 1,
            name=form.name or definition.name_for(variant),
            kind=definition.kind,
            x=round(center_x - form.footprint[0] * tile_size / 2),
            y=round(center_y - form.footprint[1] * tile_size / 2),
            width=form.footprint[0] * tile_size,
            height=form.footprint[1] * tile_size,
            state={
                "age": 0.0,
                "growth_progress": 0.0,
                "water": initial_percentage("initial_water_percentage"),
                "health": 100,
                "fertilized": False,
                "disease": None,
                "tended": initial_percentage("initial_tended_percentage"),
            },
            blocks_movement=form.blocks_movement,
            blocks_vision=form.blocks_vision,
            mobility=form.mobility,
            traits=form.traits,
            descriptions=dict(form.descriptions),
            interactions={
                action: dict(details) for action, details in form.interactions.items()
            },
            capacity=form.capacity_for(100),
            nutrition=form.nutrition,
            condition_recovery=dict(form.condition_recovery),
            type_id="crop",
            quality=100,
            persistent=False,
            variant=variant,
            form="seed",
            container=None,
        )
        self.objects[crop.object_id] = crop
        state = self.map.tile_states.setdefault(
            (column, row), LevelTileState(column, row)
        )
        return crop

    def water_sources_in_bounds(
        self, bounds: tuple[int, int, int, int]
    ) -> list[tuple[float, float]]:
        left, top, right, bottom = bounds
        tile_map = self.map.tile_map
        sources = []
        for row in range(
            max(0, int(top // tile_map.tile_size)),
            min(tile_map.rows, int(bottom // tile_map.tile_size) + 1),
        ):
            for column in range(max(0, int(left // tile_map.tile_size)), min(tile_map.columns, int(right // tile_map.tile_size) + 1)):
                tile = tile_map.tile_at(column, row)
                point = tile_map.tile_center(column, row)
                if (
                    tile is not None
                    and tile.kind in {TileKind.SHALLOW_WATER, TileKind.POND}
                    and left <= point[0] <= right
                    and top <= point[1] <= bottom
                ):
                    sources.append(point)
        return sources

    def water_sources_in_areas(
        self, areas: tuple[tuple[int, int, int, int], ...]
    ) -> list[tuple[float, float]]:
        return list(
            dict.fromkeys(
                source
                for bounds in areas
                for source in self.water_sources_in_bounds(bounds)
            )
        )

    def update_barrel_fill_job(self, dt: float) -> bool:
        job = self.barrel_fill_job
        if job is None:
            return False
        if not self.player.has_bucket or self.bucket_capacity <= 0:
            self.barrel_fill_job = None
            self.navigation_path = []
            self.walk_target = None
            self.log("The ruined bucket cannot hold water.")
            return True
        barrel = self.objects.get(job.barrel_id)
        if barrel is None or not barrel.active or barrel.kind is not ObjectKind.BARREL:
            self.barrel_fill_job = None
            self.visit_failed_memory(
                barrel.center if barrel is not None else None,
                "Fill Barrel",
                "the barrel is no longer available",
            )
            return True
        data = self.barrel_state(barrel)
        if int(data["water_uses"]) >= self.barrel_capacity:
            self.barrel_fill_job = None
            self.navigation_path = []
            self.walk_target = None
            self.log("The barrel is full.")
            return True
        if job.phase == "choose":
            if self.player.bucket_water_uses > 0:
                if not self.plan_path_to_object(barrel):
                    self.barrel_fill_job = None
                    self.visit_failed_memory(
                        barrel.center, "Fill Barrel", "I cannot reach the barrel"
                    )
                else:
                    job.phase = "barrel"
                return True
            candidates = sorted(
                self.water_sources_in_areas(job.source_areas),
                key=lambda point: math.dist((self.player.x, self.player.y), point),
            )
            for source in candidates:
                path = self.build_navigation_path(source)
                if path:
                    self.navigation_path = path[1:]
                    self.walk_target = path[-1]
                    job.phase = "source"
                    return True
            self.barrel_fill_job = None
            first_area = job.source_areas[0]
            center = ((first_area[0] + first_area[2]) / 2, (first_area[1] + first_area[3]) / 2)
            self.visit_failed_memory(
                center, "Fill Barrel", "there is no reachable water source here"
            )
            return True
        if self.navigation_path:
            if not self.move_along_path(dt):
                job.phase = "choose"
            return True
        if job.phase == "source":
            self.player.bucket_water_uses = self.bucket_capacity
            self.log("Filled the bucket from the selected water area.")
            job.phase = "choose"
            return True
        if job.phase == "barrel":
            moved = min(
                self.player.bucket_water_uses,
                self.barrel_capacity - int(data["water_uses"]),
            )
            self.player.bucket_water_uses -= moved
            data["water_uses"] = int(data["water_uses"]) + moved
            self.sync_barrel_sprite_state(data)
            barrel.state = data
            self.log(f"Added {moved} water uses to the barrel.")
            if int(data["water_uses"]) >= self.barrel_capacity:
                self.barrel_fill_job = None
                self.log("The barrel is full.")
            else:
                job.phase = "choose"
            return True
        return True

    def update_field_water_job(self, dt: float) -> bool:
        job = self.field_water_job
        if job is None:
            return False
        if not self.player.has_bucket or self.bucket_capacity <= 0:
            self.field_water_job = None
            self.navigation_path = []
            self.walk_target = None
            self.log("The ruined bucket cannot hold water.")
            return True
        while job.crop_points:
            point = job.crop_points[0]
            located = self.map.tile_map.tile_at_world(*point)
            crop = (
                self.crop_at_tile(located[0], located[1])
                if located is not None
                else None
            )
            if crop is not None and float(crop.state.get("water", 0.0)) < 100.0:
                break
            job.crop_points.pop(0)
        if not job.crop_points:
            self.field_water_job = None
            self.navigation_path = []
            self.walk_target = None
            self.log("Finished watering the selected crops.")
            return True
        if job.phase == "choose":
            if self.player.bucket_water_uses > 0:
                job.current_crop = job.crop_points[0]
                if self.plan_path(job.current_crop):
                    job.phase = "crop"
                else:
                    job.crop_points.pop(0)
                return True
            candidates = sorted(
                self.water_sources_in_areas(job.source_areas),
                key=lambda point: math.dist((self.player.x, self.player.y), point),
            )
            for source in candidates:
                path = self.build_navigation_path(source)
                if path:
                    self.navigation_path = path[1:]
                    self.walk_target = path[-1]
                    job.phase = "source"
                    return True
            self.field_water_job = None
            first_area = job.source_areas[0]
            center = ((first_area[0] + first_area[2]) / 2, (first_area[1] + first_area[3]) / 2)
            self.visit_failed_memory(
                center, "Water Crops", "there is no reachable water source here"
            )
            return True
        if self.navigation_path:
            if not self.move_along_path(dt):
                job.phase = "choose"
            return True
        if job.phase == "source":
            self.player.bucket_water_uses = self.bucket_capacity
            self.log("Refilled the bucket for the field.")
            job.phase = "choose"
            return True
        if job.phase == "crop" and job.current_crop is not None:
            located = self.map.tile_map.tile_at_world(*job.current_crop)
            crop = (
                self.crop_at_tile(located[0], located[1])
                if located is not None
                else None
            )
            if crop is not None and float(crop.state.get("water", 0.0)) < 100.0:
                crop.state["water"] = 100.0
                self.player.bucket_water_uses -= 1
                self.log("Watered a selected crop.")
            job.crop_points.pop(0)
            job.current_crop = None
            job.phase = "choose"
            return True
        return True

    def update(self, dt: float) -> None:
        for message in self.quests.update(self.player, self.day):
            self.log(message)
        if self.adjusting_memory:
            return
        if self.day_transition_phase is not None:
            self.update_day_transition(dt)
            return
        if self.day.mode is Mode.MORNING and self.auto_cheat_memory:
            self.choose_morning_option("Replay Memory and Sleep")
        if self.day.mode is Mode.DIRECT and not self.has_queued_command():
            self.simulation_paused = True
        self.update_condition_thoughts(dt)
        self.update_object_memories(dt)
        if self.day.mode is Mode.MORNING:
            self.simulation_step_accumulator = 0.0
            self._update_simulation_tick(0.0)
            return
        if self.simulation_paused:
            self.simulation_step_accumulator = 0.0
            return
        self.simulation_step_accumulator += dt * self.time_speed
        processed_tick = False
        while (
            self.simulation_step_accumulator + 1e-12
            >= FIXED_SIMULATION_TICK_SECONDS
        ):
            self.simulation_step_accumulator -= FIXED_SIMULATION_TICK_SECONDS
            self._update_simulation_tick(FIXED_SIMULATION_TICK_SECONDS)
            self.record_day_position()
            processed_tick = True
            if self.day.mode is Mode.DIRECT and not self.has_queued_command():
                self.simulation_paused = True
            if (
                self.day_transition_phase is not None
                or self.day.mode is Mode.MORNING
                or self.simulation_paused
            ):
                self.simulation_step_accumulator = 0.0
                break
        if not processed_tick:
            self._update_simulation_tick(0.0)

    def record_day_position(self) -> None:
        """Keep the visible route through the day for the nightly rewind."""
        position = (self.player.x, self.player.y)
        if position != self.day_position_history[-1]:
            self.day_position_history.append(position)

    def record_rewind_checkpoint(self, action: str) -> None:
        """Capture the mutable world immediately before a command starts."""
        self.rewind_checkpoints.append(
            (
                len(self.day_position_history) - 1,
                action,
                {
                    "objects": copy.deepcopy(self.objects),
                    "tile_states": copy.deepcopy(self.map.tile_states),
                    "tile_kinds": tuple(
                        tile.kind for tile in self.map.tile_map.tiles
                    ),
                    "inventory": copy.deepcopy(self.player.inventory),
                    "equipment": (
                        self.player.has_hoe,
                        self.player.carrying_hoe,
                        self.player.hoe_quality,
                        self.player.has_axe,
                        self.player.carrying_axe,
                    ),
                },
            )
        )

    def restore_rewind_checkpoint(self, snapshot: dict[str, object]) -> None:
        objects = copy.deepcopy(snapshot["objects"])
        for object_id, persistent in self.rewind_persistent_objects.items():
            objects[object_id] = copy.deepcopy(persistent)
        self.objects = objects
        self.map.objects = objects
        self.map.tile_states = copy.deepcopy(snapshot["tile_states"])
        for position, state in self.rewind_persistent_tile_states.items():
            self.map.tile_states[position] = copy.deepcopy(state)
        rebuild_tile_map(self.map)
        for tile, kind in zip(self.map.tile_map.tiles, snapshot["tile_kinds"]):
            tile.kind = kind
        for position, kind in self.rewind_persistent_tile_kinds.items():
            tile = self.map.tile_map.tile_at(*position)
            if tile is not None:
                tile.kind = kind
        self.player.inventory = copy.deepcopy(snapshot["inventory"])
        (
            self.player.has_hoe,
            self.player.carrying_hoe,
            self.player.hoe_quality,
            self.player.has_axe,
            self.player.carrying_axe,
        ) = snapshot["equipment"]
        self.player.carried_objects = [
            obj for obj in objects.values() if obj.active and obj.container == "player"
        ]
        self.storage_memories = self.load_storage_memories()

    def capture_transition_state(self) -> tuple[object, ...]:
        map_state, player_state, day_state, storage_state = copy.deepcopy(
            (self.map, self.player, self.day, self.storage_memories)
        )
        return (
            map_state,
            player_state,
            day_state,
            storage_state,
            copy.deepcopy(self.quests.state_data()),
        )

    def restore_transition_state(self, state: tuple[object, ...]) -> None:
        (
            self.map,
            self.player,
            self.day,
            self.storage_memories,
            quest_state,
        ) = copy.deepcopy(state)
        self.objects = self.map.objects
        self.build_memories = self.map.build_memories
        self.quests.restore_state(quest_state)
        self.sync_all_barrel_sprite_states()

    def active_condition_complaints(self) -> list[tuple[str, str]]:
        complaints: list[tuple[str, str]] = []
        for condition_id in CONDITION_IDS:
            value = self.player.conditions.get(condition_id, 0.0)
            text = next(
                (
                    complaint
                    for threshold, complaint in CONDITION_COMPLAINTS[condition_id]
                    if value >= threshold
                ),
                None,
            )
            if text is not None:
                complaints.append((condition_id, text))
        return complaints

    def clear_negative_conditions(self) -> None:
        for condition_id in CONDITION_IDS:
            self.player.conditions[condition_id] = 0.0
        self.thought_bubble_text = None
        self.thought_bubble_timer = 0.0
        self.thought_bubble_source = None
        self.condition_thought_cooldown = 2.0
        self.log("Cheat: all negative conditions removed.")

    def handle_inventory_food_click(self, position: tuple[int, int]) -> bool:
        if self.inventory_page is not InventoryPage.INVENTORY:
            return False
        food = next(
            (
                item
                for rect, item in self.inventory_food_buttons
                if rect.collidepoint(position)
            ),
            None,
        )
        if food is None:
            return False
        self.context_menu_options = [f"Eat {food.name}"]
        self.context_menu_pos = position
        self.context_ground_target = None
        self.context_inventory_item_id = food.object_id
        self.selected_id = None
        return True

    def area_target_duration(self, target: AreaTarget) -> float:
        if target.action == "Till Grassland":
            return (
                tilling_duration_seconds(self.player.hoe_quality)
                * target.work_fraction
            )
        if target.action == "Plant Wheat":
            return planting_duration_seconds(self.player.has_basket)
        if target.action == "Harvest Wheat":
            interaction = self.map.object_types["crop"].form_definition(
                "mature", "wheat"
            ).interactions["Harvest Wheat"]
            duration = interaction["duration_seconds"]
            duration_key = (
                "with_basket"
                if self.player.has_basket and "with_basket" in duration
                else "base"
            )
            return float(duration[duration_key]) / self.skill_speed_multiplier(
                "harvesting"
            )
        if target.action == "Harvest Berries" and target.target_id is not None:
            bush = self.objects[target.target_id]
            return object_job_duration_seconds(
                "Harvest Berries", bush, self.player.has_basket
            ) / self.skill_speed_multiplier("harvesting")
        if target.action in BUILD_COMMAND_TYPES:
            build_type = BUILD_COMMAND_TYPES[target.action]
            return self.map.object_types[
                build_type
            ].form_definition().build_duration_seconds
        return 0.0

    def active_work_progress(self) -> tuple[str, float] | None:
        if self.pending_area_target is not None:
            duration = self.area_target_duration(self.pending_area_target)
            if duration > 0.0 and math.dist(
                (self.player.x, self.player.y), self.pending_area_target.point
            ) <= 4:
                return (
                    self.pending_area_target.action,
                    max(0.0, min(1.0, self.area_job_timer / duration)),
                )
        if self.pending_job is not None:
            point = self.pending_job.interaction_point
            if point is None or math.dist((self.player.x, self.player.y), point) > 4:
                return None
            target = self.objects.get(self.pending_job.target_id)
            if target is None:
                return None
            duration = object_job_duration_seconds(
                self.pending_job.action, target, self.player.has_basket
            )
            if self.pending_job.action in {
                "Harvest Berries",
                "Harvest and Eat Berries",
                "Harvest Wheat",
            }:
                duration /= self.skill_speed_multiplier("harvesting")
            if duration > 0.0:
                return (
                    self.pending_job.action,
                    max(0.0, min(1.0, self.job_timer / duration)),
                )
        return None

    def work_thought_text(self) -> str | None:
        status = self.active_work_progress()
        if status is None:
            return None
        action, progress = status
        if progress >= 0.75:
            milestone = "almost done"
        elif progress >= 0.5:
            milestone = "1/2 done"
        elif progress >= 0.25:
            milestone = "1/4 done"
        else:
            return action
        return f"{action} — {milestone}"

    def object_memory_refs(self, obj: WorldObject) -> tuple[str, ...]:
        """Combine authored type memories with references learned at runtime."""
        authored = self.map.object_types[obj.type_id].memory_refs
        runtime = obj.state.get("memory_refs", [])
        if not isinstance(runtime, list):
            runtime = []
        return tuple(dict.fromkeys((*authored, *(str(value) for value in runtime))))

    def add_object_memory(self, obj: WorldObject, memory_id: str) -> bool:
        """Attach a catalog memory to one object, preserving it with object state."""
        if memory_id not in self.map.object_memories:
            raise ValueError(f"Unknown object memory {memory_id!r}")
        refs = obj.state.setdefault("memory_refs", [])
        if not isinstance(refs, list):
            refs = []
            obj.state["memory_refs"] = refs
        if memory_id in refs or memory_id in self.map.object_types[obj.type_id].memory_refs:
            return False
        refs.append(memory_id)
        return True

    def update_object_memories(self, dt: float) -> None:
        """Roll each nearby memory at most once per day."""
        if self.day.mode is not Mode.DIRECT or self.thought_bubble_text is not None:
            return
        self.object_memory_check_accumulator += dt
        if self.object_memory_check_accumulator < 0.5:
            return
        self.object_memory_check_accumulator = 0.0
        size = self.map.tile_map.tile_size
        for obj in sorted(self.objects.values(), key=lambda value: value.object_id):
            if not obj.active or obj.container is not None:
                continue
            if int(obj.state.get("object_memory_last_spoken_day", -1)) == self.day.attempts:
                continue
            for memory_id in self.object_memory_refs(obj):
                memory = self.map.object_memories.get(memory_id)
                if (
                    memory is None
                    or math.dist((self.player.x, self.player.y), obj.center)
                    > memory.radius_tiles * size
                ):
                    continue
                states = obj.state.setdefault("object_memory_state", {})
                if not isinstance(states, dict):
                    states = {}
                    obj.state["object_memory_state"] = states
                state = states.setdefault(memory_id, {})
                if not isinstance(state, dict):
                    state = {}
                    states[memory_id] = state
                if int(state.get("last_roll_day", -1)) == self.day.attempts:
                    continue
                state["last_roll_day"] = self.day.attempts
                times_said = max(0, int(state.get("times_said", 0)))
                chance = memory.chance * (0.5**times_said)
                if random.random() >= chance:
                    continue
                state["times_said"] = times_said + 1
                obj.state["object_memory_last_spoken_day"] = self.day.attempts
                self.thought_bubble_text = memory.text
                self.thought_bubble_source = "object_memory"
                self.thought_bubble_timer = CONDITION_THOUGHT_DISPLAY_SECONDS
                self.log(memory.text)
                return

    def update_condition_thoughts(self, dt: float) -> None:
        if (
            self.thought_bubble_source == "condition"
            and self.day.mode is not Mode.DIRECT
        ):
            self.thought_bubble_text = None
            self.thought_bubble_timer = 0.0
            self.thought_bubble_source = None
        if self.thought_bubble_timer > 0.0:
            self.thought_bubble_timer = max(0.0, self.thought_bubble_timer - dt)
            if self.thought_bubble_timer > 0.0:
                return
            ended_source = self.thought_bubble_source
            self.thought_bubble_text = None
            self.thought_bubble_source = None
            self.condition_thought_cooldown = (
                CONDITION_THOUGHT_GAP_SECONDS
                if ended_source == "condition"
                else 2.0
            )
        if self.thought_bubble_text is not None:
            return
        if self.day.mode is not Mode.DIRECT:
            return
        self.condition_thought_cooldown = max(
            0.0, self.condition_thought_cooldown - dt
        )
        if self.condition_thought_cooldown > 0.0:
            return
        complaints = self.active_condition_complaints()
        if not complaints:
            self.condition_thought_index = 0
            return
        _condition_id, complaint = complaints[
            self.condition_thought_index % len(complaints)
        ]
        self.condition_thought_index = (
            self.condition_thought_index + 1
        ) % len(complaints)
        self.thought_bubble_text = complaint
        self.thought_bubble_source = "condition"
        self.thought_bubble_timer = CONDITION_THOUGHT_DISPLAY_SECONDS

    def _update_simulation_tick(self, dt: float) -> None:
        if self.pending_empty_area_memory and not self.navigation_path:
            self.show_empty_area_memory_thought()
        self.advance_crop_growth(dt * GAME_MINUTES_PER_REAL_SECOND)
        if self.day.mode is not Mode.MORNING:
            self.time_accumulator += dt * GAME_MINUTES_PER_REAL_SECOND
            elapsed_minutes = int(self.time_accumulator)
            if elapsed_minutes:
                self.time_accumulator -= elapsed_minutes
                previous_minutes = self.day.current_time_minutes
                self.day.current_time_minutes = min(
                    self.night_cutoff_minutes,
                    previous_minutes + elapsed_minutes,
                )
                advanced_minutes = self.day.current_time_minutes - previous_minutes
                self.show_late_night_hints(
                    previous_minutes, self.day.current_time_minutes
                )
                self.update_illnesses(advanced_minutes)
                if self.advance_player_conditions(advanced_minutes):
                    return
                if self.day.current_time_minutes >= self.night_cutoff_minutes:
                    self.time_accumulator = 0.0
                    self.begin_day_transition()
                    return
        if self.vomiting_timer_seconds > 0.0:
            self.vomiting_timer_seconds = max(
                0.0, self.vomiting_timer_seconds - dt
            )
            return
        if self.update_barrel_fill_job(dt):
            return

        if self.update_field_water_job(dt):
            return
        if (
            self.day.mode in {Mode.DIRECT, Mode.REPLAY}
            and self.pending_job is None
            and self.pending_area_target is None
            and not self.navigation_path
            and self.area_targets
        ):
            target = self.area_targets.pop(0)
            if target.target_id is not None:
                self.queue_job(
                    target.target_id,
                    target.action,
                    record=False,
                    advances_replay=False,
                )
            elif target.prerequisite_target_ids:
                remaining = [
                    target_id
                    for target_id in target.prerequisite_target_ids
                    if (obj := self.objects.get(target_id)) is not None
                    and obj.active
                    and (
                        "Gather" in available_actions(obj, self.player)
                        or "Pull Berry Bush" in available_actions(obj, self.player)
                    )
                ]
                if remaining:
                    self.area_targets.insert(0, target)
                    prerequisite = self.objects[remaining[0]]
                    prerequisite_action = (
                        "Pull Berry Bush"
                        if "Pull Berry Bush" in available_actions(prerequisite, self.player)
                        else "Gather"
                    )
                    self.queue_job(
                        remaining[0],
                        prerequisite_action,
                        record=False,
                        advances_replay=False,
                    )
                elif self.plan_path(target.point):
                    self.pending_area_target = target
                    self.area_job_timer = 0.0
            elif self.plan_path(target.point):
                self.pending_area_target = target
                self.area_job_timer = 0.0

        if self.pending_area_target is not None:
            target = self.pending_area_target
            if math.dist((self.player.x, self.player.y), target.point) > 4:
                if not self.navigation_path or not self.move_along_path(dt):
                    self.log(f"The route to {target.action.lower()} is blocked.")
                    self.pending_area_target = None
                    self.area_job_timer = 0.0
                return
            timed_duration = self.area_target_duration(target)
            if timed_duration:
                if self.area_job_timer == 0.0:
                    self.log(f"{target.action} started.")
                self.area_job_timer += dt * task_speed_multiplier(self.player)
                if self.area_job_timer < timed_duration:
                    return
            if target.action == "Gather Water":
                if self.player.has_bucket and not self.player.bucket_filled:
                    self.player.bucket_water_uses = self.bucket_capacity
                    self.log("Filled the wooden bucket with 5 uses of water.")
            elif target.action in {"Drink Water", "Drink Until Full"}:
                recovery = 25.0 if self.player.has_bucket else 5.0
                apply_condition_effects(self.player, {"thirst": recovery})
                self.log(
                    f"Drank from the water ({recovery:g} Thirst recovered)."
                )
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    self.attempt_illness_exposures(
                        self.map.tile_illness_exposures.get(located[2].kind, {})
                    )
                if (
                    target.action == "Drink Until Full"
                    and self.player.conditions["thirst"] > 0.0
                ):
                    self.area_job_timer = 0.0
                    return
            elif target.action == "Till Grassland" and self.player.carrying_hoe:
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None and located[2].kind is TileKind.GRASSLAND:
                    column, row, tile = located
                    state = self.map.tile_states.setdefault(
                        (column, row), LevelTileState(column=column, row=row)
                    )
                    if state.persistence_modifier is None:
                        state.persistence_modifier = self.persistence_modifier(
                            f"tile:{column}:{row}",
                            self.map.tile_persistence_modifier_range,
                        )
                    state.till_percentage = min(
                        100.0,
                        state.till_percentage
                        + self.map.till_progress_per_action
                        * state.persistence_modifier
                        * target.work_fraction,
                        # A time-limited command may end partway through one
                        # ordinary till action; preserve that proportional work.
                    )
                    state.tilled_today = True
                    if state.till_percentage >= 100.0:
                        state.kind_override = TileKind.SOIL.value
                        state.soil_persistence_percentage = min(
                            100.0,
                            state.soil_persistence_percentage
                            + self.map.soil_persistence_gain
                            * state.persistence_modifier,
                        )
                        tile.kind = TileKind.SOIL
                        self.quests.record_event("soil_tiles_tilled")
                        self.log(
                            "The fully tilled grassland became soil "
                            f"({state.soil_persistence_percentage:.1f}% remembered)."
                        )
                    else:
                        self.log(
                            f"Tilled the grassland to {state.till_percentage:.1f}%."
                        )
            elif target.action == "Plant Wheat":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None and located[2].kind is TileKind.SOIL and self.player.inventory["seed"] > 0:
                    column, row, _tile = located
                    if self.crop_at_tile(column, row) is None:
                        self.plant_crop(column, row, "wheat")
                        self.player.inventory["seed"] -= 1
                        self.quests.record_event("wheat_planted")
                        self.log("Planted wheat seed.")
            elif target.action == "Water Crops" and self.player.bucket_filled:
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    column, row, _tile = located
                    crop = self.crop_at_tile(column, row)
                    if (
                        crop is not None
                        and float(crop.state.get("water", 0.0)) < 100.0
                    ):
                        crop.state["water"] = 100.0
                        self.player.bucket_water_uses -= 1
                        self.log("Watered the wheat; it will now grow much faster.")
            elif target.action in BUILD_COMMAND_TYPES:
                build_type = BUILD_COMMAND_TYPES[target.action]
                if self.can_afford_build(build_type):
                    if build_type == "barrel":
                        self.build_barrel(target.placement_point or target.point)
                    else:
                        self.build_fixed_object(
                            build_type, target.placement_point or target.point
                        )
            elif target.action == "Drop Bucket":
                self.drop_bucket(target.point)
            elif target.action == "Tend Plant":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    crop = self.crop_at_tile(located[0], located[1])
                    if (
                        crop is not None
                        and float(crop.state.get("tended", 0.0)) < 100.0
                    ):
                        crop.state["tended"] = 100.0
                        self.log("Tended the plant; it will grow a little faster.")
            elif target.action == "Harvest Wheat":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    column, row, _tile = located
                    crop = self.crop_at_tile(column, row)
                    if crop is not None and crop.variant == "wheat" and crop.form == "mature":
                        grains_before = self.player.inventory["grains"]
                        self.grant_interaction_loot(crop, "Harvest Wheat")
                        crop.active = False
                        grains = self.player.inventory["grains"] - grains_before
                        self.award_skill_experience("harvesting")
                        self.quests.record_event("wheat_harvested", grains)
                        self.log(f"Harvested {grains} grains.")
            elif target.action == "Harvest Berries" and target.target_id is not None:
                bush = self.objects[target.target_id]
                if "Harvest Berries" in available_actions(bush, self.player):
                    self.complete_job(
                        PendingJob(
                            bush.object_id,
                            "Harvest Berries",
                            advances_replay=False,
                        )
                    )
            self.pending_area_target = None
            self.area_job_timer = 0.0
            self.navigation_path = []
            self.walk_target = None
            return

        if (
            self.single_step_active
            and self.single_step_command_started
            and not self.has_queued_command()
        ):
            self.single_step_active = False
            self.single_step_command_started = False
            self.simulation_paused = True
            self.simulation_step_accumulator = 0.0
            self.log("Command complete. Paused.")
            return

        if (
            self.day.mode is Mode.REPLAY
            and self.pending_job is None
            and self.pending_area_target is None
            and not self.area_targets
            and not self.navigation_path
            and not self.pending_empty_area_memory
        ):
            if self.day.replay_index >= len(self.day.remembered_routine):
                if self.replay_outcome == "sleep":
                    bed = self.object_of_type("bed")
                    sleep_queued = self.queue_job(
                        bed.object_id,
                        "Sleep",
                        record=False,
                        advances_replay=False,
                    )
                    if not sleep_queued and self.auto_cheat_memory:
                        self.log(
                            "Automatic memory could not reach the bed; forcing the cheat-mode dawn reset."
                        )
                        self.begin_day_transition()
                else:
                    self.day.mode = Mode.DIRECT
                    if self.replay_outcome == "command_set":
                        completed_name = self.running_command_set_name or "unnamed"
                        self.running_command_set_name = None
                        self.day.replay_index = 0
                        self.replay_outcome = "expand"
                        self.record_routine_commands = True
                        self.log(f"Command set {completed_name!r} complete.")
                    elif self.replay_outcome == "explore":
                        self.log("Routine complete. Explore freely; new orders will not be remembered.")
                    elif self.replay_outcome == "legacy":
                        self.log("Routine complete. Returning to Direct Control.")
                    elif self.replay_outcome == "planned_day":
                        self.log("Planned routines complete. Returning to Direct Control.")
                    else:
                        self.log("Routine complete. Expand the remembered routine with new orders.")
            else:
                step = self.day.remembered_routine[self.day.replay_index]
                self.record_rewind_checkpoint(step.action)
                if step.action == ROUTINE_REFERENCE_ACTION:
                    self.day.replay_index += 1
                    return
                if step.action in {ROUTINE_IF_ACTION, ROUTINE_LOOP_ACTION}:
                    condition_met = self.routine_step_condition_met(step)
                    should_skip = (
                        condition_met
                        if step.action == ROUTINE_LOOP_ACTION
                        else not condition_met
                    )
                    if should_skip:
                        end = self.matching_routine_end(self.day.replay_index)
                        self.day.replay_index = (
                            end + 1 if end is not None else len(self.day.remembered_routine)
                        )
                    elif step.action == ROUTINE_LOOP_ACTION:
                        end = self.matching_routine_end(self.day.replay_index)
                        if end is None or not self.routine_block_has_executable_command(
                            self.day.replay_index + 1, end
                        ):
                            self.day.replay_index = (
                                end + 1
                                if end is not None
                                else len(self.day.remembered_routine)
                            )
                            self.log(
                                "Exited repeat: none of its commands are currently executable."
                            )
                        else:
                            self.day.replay_index += 1
                    else:
                        self.day.replay_index += 1
                    return
                if step.action == ROUTINE_END_ACTION:
                    opener = self.matching_routine_opener(self.day.replay_index)
                    if (
                        opener is not None
                        and self.day.remembered_routine[opener].action == ROUTINE_LOOP_ACTION
                    ):
                        self.day.replay_index = opener
                    else:
                        self.day.replay_index += 1
                    return
                if not self.routine_step_condition_met(step):
                    self.day.replay_index += 1
                    self.log(f"Skipped {step.action}: {self.routine_condition_label(step)} is false.")
                    return
                if (
                    step.action == "Water Crops"
                    and step.area_bounds is not None
                    and step.secondary_bounds is not None
                ):
                    self.day.replay_index += 1
                    self.queue_field_water_command(
                        step.area_bounds,
                        step.source_areas or (step.secondary_bounds,),
                        step.quantity or 1,
                        record=False,
                    )
                elif step.action == "Fill Barrel" and step.area_bounds is not None:
                    self.day.replay_index += 1
                    build_memory_id = self.build_memory_id_for_step(step)
                    remembered_object = (
                        self.object_for_build_memory(build_memory_id)
                        if build_memory_id is not None
                        else None
                    )
                    target_id = (
                        remembered_object.object_id
                        if remembered_object is not None
                        else step.target_id if build_memory_id is None else None
                    )
                    if target_id is None or target_id not in self.objects:
                        self.visit_failed_memory(
                            step.target_point,
                            step.action,
                            "the remembered barrel has not been built",
                        )
                    else:
                        self.queue_barrel_fill_command(
                            target_id,
                            step.source_areas or (step.area_bounds,),
                            record=False,
                        )
                elif (
                    step.action in {"Drink Water", "Drink Until Full"}
                    and step.target_id is None
                    and step.target_point is not None
                ):
                    self.day.replay_index += 1
                    if not self.queue_terrain_drink(
                        step.target_point,
                        record=False,
                        until_full=step.action == "Drink Until Full",
                    ):
                        self.visit_failed_memory(
                            step.target_point,
                            step.action,
                            "there is no reachable water at the remembered place",
                        )
                elif step.action.startswith("Eat ") and step.target_id is None:
                    self.day.replay_index += 1
                    food = next(
                        (
                            carried
                            for carried in self.player.carried_objects
                            if carried.active
                            and "edible" in carried.traits
                            and (
                                step.target_type is None
                                or carried.type_id == step.target_type
                            )
                        ),
                        None,
                    )
                    if food is None:
                        self.log(f"Skipped unavailable remembered job: {step.action}.")
                    else:
                        self.consume_carried_food(food, "from inventory")
                elif step.action == "Harvest and Eat Berries":
                    target = self.berry_routine_target(step)
                    if target is None:
                        self.day.replay_index += 1
                        self.log("Skipped unavailable remembered job: Harvest and Eat Berries.")
                    else:
                        self.queue_job(target.object_id, step.action, record=False)
                elif step.nearest_to_player and step.action in NEAREST_AREA_COMMANDS:
                    self.day.replay_index += 1
                    self.queue_nearest_area_command(
                        step.action,
                        step.quantity or 1,
                        record=False,
                        max_game_minutes=step.max_game_minutes,
                        till_until_done=step.till_until_done,
                    )
                elif step.area_bounds is not None:
                    self.day.replay_index += 1
                    self.queue_area_command(
                        step.action,
                        step.area_bounds,
                        step.quantity or 1,
                        record=False,
                        target_areas=step.target_areas,
                        max_game_minutes=step.max_game_minutes,
                        till_until_done=step.till_until_done,
                    )
                else:
                    target_id = self.resolve_routine_target(step)
                    if target_id is None:
                        self.log(f"Skipped unavailable remembered job: {step.action}.")
                        self.day.replay_index += 1
                        original = self.objects.get(step.target_id)
                        self.visit_failed_memory(
                            step.target_point
                            or (original.center if original is not None else None),
                            step.action,
                            "the remembered target is no longer available",
                        )
                    else:
                        self.queue_job(target_id, step.action, record=False)

        if self.pending_job is None and self.navigation_path:
            if not self.move_along_path(dt):
                self.log("The route became blocked.")
            return

        if self.pending_job is None:
            return
        interaction_point = self.pending_job.interaction_point
        at_interaction_point = (
            interaction_point is not None
            and math.dist((self.player.x, self.player.y), interaction_point) <= 4
        )
        if not at_interaction_point:
            if not self.navigation_path or not self.move_along_path(dt):
                self.log("The route to this job is blocked.")
                job = self.pending_job
                self.pending_job = None
                self.path_target = None
                if self.day.mode is Mode.REPLAY and job.advances_replay:
                    self.day.replay_index += 1
        else:
            speed = (
                1.0
                if self.pending_job.action == "Power Nap"
                else task_speed_multiplier(self.player)
            )
            self.job_timer += dt * speed
            target = self.objects[self.pending_job.target_id]
            required_duration = object_job_duration_seconds(
                self.pending_job.action, target, self.player.has_basket
            )
            if self.pending_job.action in {
                "Harvest Berries",
                "Harvest and Eat Berries",
                "Harvest Wheat",
            }:
                required_duration /= self.skill_speed_multiplier("harvesting")
            if self.job_timer >= required_duration:
                job = self.pending_job
                self.pending_job = None
                self.complete_job(job)
                if self.day.mode is Mode.REPLAY and job.advances_replay:
                    self.day.replay_index += 1

    def show_late_night_hints(
        self, previous_minutes: int, current_minutes: int
    ) -> None:
        for hint in self.night_hints:
            if (
                previous_minutes < hint.minute <= current_minutes
                and hint.minute not in self.shown_night_hint_minutes
            ):
                self.shown_night_hint_minutes.add(hint.minute)
                self.log(hint.text)
                self.thought_bubble_text = hint.text
                self.thought_bubble_source = "night_hint"
                self.thought_bubble_timer = CONDITION_THOUGHT_DISPLAY_SECONDS

    def advance_player_conditions(self, elapsed_minutes: int) -> bool:
        hourly_change = float(elapsed_minutes) / 60.0
        lethal = apply_condition_effects(
            self.player,
            {
                "hunger": -hourly_change,
                "thirst": -hourly_change,
                "fatigue": -hourly_change,
            },
        )
        trauma_recovery = healing_rate(self.player) * hourly_change
        if trauma_recovery:
            lethal = (
                apply_condition_effects(self.player, {"trauma": trauma_recovery})
                or lethal
            )
        if lethal:
            self.log("The Forgotten succumbed. Night closes in.")
            self.cancel_current_command()
            self.begin_day_transition()
            return True
        return False

    def attempt_illness_exposures(self, exposures: dict[str, float]) -> None:
        """Roll independent, hidden exposures for every illness on a source."""
        for illness_id, chance in exposures.items():
            if chance <= 0.0 or random.random() >= chance:
                continue
            definition = self.illness_types[illness_id]
            onset = self.day.current_time_minutes + random.randint(
                *definition.incubation_minutes
            )
            current = self.active_illnesses.get(illness_id)
            if current is None:
                self.active_illnesses[illness_id] = ActiveIllness(
                    illness_id, definition.initial_value, onset
                )
            else:
                current.value = max(current.value, definition.initial_value)
                current.onset_minute = min(current.onset_minute, onset)

    def show_illness_thought(self, text: str) -> None:
        self.thought_bubble_text = text
        self.thought_bubble_source = "illness"
        self.thought_bubble_timer = CONDITION_THOUGHT_DISPLAY_SECONDS
        self.log(text)

    def update_illnesses(self, elapsed_minutes: int) -> None:
        first_minute = self.day.current_time_minutes - elapsed_minutes + 1
        for minute in range(first_minute, self.day.current_time_minutes + 1):
            for illness_id, illness in list(self.active_illnesses.items()):
                definition = self.illness_types[illness_id]
                if not illness.revealed:
                    if minute < illness.onset_minute:
                        continue
                    illness.revealed = True
                    illness.next_vomit_check_minute = minute + random.randint(
                        *definition.effect_grace_minutes
                    )
                    self.show_illness_thought(definition.onset_thought)
                    continue
                if minute < (illness.next_vomit_check_minute or minute):
                    continue
                if random.random() * 100.0 >= illness.value:
                    continue
                apply_condition_effects(
                    self.player, definition.effect_condition_effects
                )
                self.vomiting_timer_seconds = max(
                    self.vomiting_timer_seconds, VOMITING_DURATION_SECONDS
                )
                illness.value *= 1.0 - definition.effect_clear_rate
                self.show_illness_thought(definition.effect_thought)
                self.log(
                    f"Vomiting reduced {definition.name} to {illness.value:g}%."
                )
                if illness.value <= definition.recovery_threshold:
                    del self.active_illnesses[illness_id]

    def player_looks_ill(self) -> bool:
        return any(illness.revealed for illness in self.active_illnesses.values())

    def visible_illness_factors(self) -> list[tuple[IllnessType, float]]:
        """Return each revealed illness separately; incubation remains unknowable."""
        return [
            (self.illness_types[illness_id], illness.value)
            for illness_id, illness in self.active_illnesses.items()
            if illness.revealed
        ]

    def skill_config(self, skill_id: str) -> dict[str, object]:
        character = self.map.characters.get(self.player.character_id)
        if character is None:
            return {}
        definition = self.map.character_types.get(character.type_id)
        if definition is None:
            return {}
        return definition.skills.get(skill_id, {})

    def skill_speed_multiplier(self, skill_id: str) -> float:
        config = self.skill_config(skill_id)
        if skill_id == "harvesting":
            return harvesting_speed_multiplier(
                self.player,
                speed_per_level=float(config.get("speed_per_level", 0.05)),
            )
        return 1.0

    def award_skill_experience(self, skill_id: str) -> None:
        config = self.skill_config(skill_id)
        leveled = gain_skill_experience(
            self.player,
            skill_id,
            float(config.get("experience_per_action", 1.0)),
            experience_per_level=float(config.get("experience_per_level", 10.0)),
        )
        if leveled:
            self.log(
                f"{skill_id.title()} reached level "
                f"{self.player.skills[skill_id].level}."
            )

    def complete_job(self, job: PendingJob) -> None:
        obj = self.objects[job.target_id]
        action = job.action
        if action == "Gather":
            loot = self.grant_interaction_loot(obj, action)
            obj.active = False
            self.log(f"Gathered {', '.join(loot)}.")
        elif action in {"Harvest Berries", "Harvest and Eat Berries"}:
            if not bool(obj.state.get("has_berries", True)):
                return
            self.grant_interaction_loot(obj, action)
            berries = self.create_interaction_objects(obj, action)
            obj.state["has_berries"] = False
            self.award_skill_experience("harvesting")
            if action == "Harvest and Eat Berries" and berries:
                self.consume_carried_food(berries[0], "straight from the bush")
            else:
                self.log("Harvested berries.")
        elif action == "Pull Berry Bush":
            had_berries = bool(obj.state.get("has_berries", True))
            self.grant_interaction_loot(obj, action)
            if had_berries:
                self.create_interaction_objects(obj, action)
            obj.active = False
            rebuild_tile_map(self.map)
            self.log(
                "Pulled the berry bush and gathered "
                + ("berries, fiber, and a branch." if had_berries else "fiber and a branch.")
            )
        elif action == "Break Off Branch":
            state = tree_state_data(obj.state)
            if obj.form == "stump" or state["branch_taken"]:
                return
            self.grant_interaction_loot(obj, action)
            state["branch_taken"] = True
            obj.state = encode_tree_state(state)
            self.log("Broke a branch from the tree.")
        elif action == "Chop Down Tree":
            if not self.player.carrying_axe or not obj.active:
                return
            state = tree_state_data(obj.state)
            state["branch_taken"] = True
            obj.state = encode_tree_state(state)
            self.grant_interaction_loot(obj, action)
            result_form = str(
                self.interaction_definition(obj, action).get(
                    "result_form", obj.form
                )
            )
            self.apply_object_form(obj, result_form)
            rebuild_tile_map(self.map)
            self.log("Chopped down the tree and gathered 3 wood.")
        elif action == "Craft Crude Hoe":
            self.consume_interaction_cost(obj, action)
            self.player.has_hoe = True
            self.player.carrying_hoe = True
            self.player.hoe_quality = 20
            self.unlock("First Tool")
            self.log("Crafted and equipped a crude hoe.")
        elif action == "Craft Crude Axe":
            self.consume_interaction_cost(obj, action)
            created = self.interaction_definition(obj, action).get("creates", {})
            if created:
                self.create_carried_object(str(created["type"]))
            self.player.has_axe = True
            self.player.carrying_axe = True
            self.log("Crafted and equipped a crude axe.")
        elif action == "Craft Wooden Bucket":
            self.consume_interaction_cost(obj, action)
            created = self.interaction_definition(obj, action).get("creates", {})
            self.create_carried_object(str(created["type"]), quality=50)
            self.log("Crafted a wooden bucket.")
        elif action in {"Pour Water Into Barrel", "Fill Bucket From Barrel"}:
            data = self.barrel_state(obj)
            barrel_water = int(data["water_uses"])
            if action == "Pour Water Into Barrel":
                moved = min(self.player.bucket_water_uses, self.barrel_capacity - barrel_water)
                self.player.bucket_water_uses -= moved
                data["water_uses"] = barrel_water + moved
                self.log(f"Poured {moved} use{'s' if moved != 1 else ''} into the barrel.")
            else:
                moved = min(
                    self.bucket_capacity - self.player.bucket_water_uses,
                    barrel_water,
                )
                self.player.bucket_water_uses += moved
                data["water_uses"] = barrel_water - moved
                self.log(f"Took {moved} use{'s' if moved != 1 else ''} from the barrel.")
            self.sync_barrel_sprite_state(data)
            obj.state = data
        elif action == "Drink from Barrel":
            data = self.barrel_state(obj)
            if int(data["water_uses"]) <= 0:
                return
            data["water_uses"] = int(data["water_uses"]) - 1
            apply_condition_effects(self.player, {"thirst": 25})
            self.attempt_illness_exposures(
                self.map.object_types[obj.type_id]
                .form_definition(obj.form or None, obj.variant)
                .illness_exposures
            )
            self.sync_barrel_sprite_state(data)
            obj.state = data
            self.log("Drank from the barrel (25 Thirst recovered).")
        elif action == "Weave Fiber Basket":
            self.consume_interaction_cost(obj, action)
            created = self.interaction_definition(obj, action).get("creates", {})
            self.create_carried_object(str(created["type"]))
            self.log("Wove a fiber basket.")
        elif action == "Store Hoe":
            self.player.carrying_hoe = False
            self.record_tool_stored(obj, "hoe", self.player.hoe_quality)
            self.log("Stored the crude hoe.")
        elif action == "Take Hoe":
            self.player.carrying_hoe = True
            self.record_tool_taken(obj, "hoe")
            self.log("Took the crude hoe.")
        elif action == "Store Axe":
            self.player.carrying_axe = False
            self.record_tool_stored(obj, "axe", 20)
            self.log("Stored the crude axe.")
        elif action == "Take Axe":
            self.player.carrying_axe = True
            self.record_tool_taken(obj, "axe")
            self.log("Took the crude axe.")
        elif action == "Pick Up":
            obj.container = "player"
            self.player.carried_objects.append(obj)
            rebuild_tile_map(self.map)
            self.log(f"Picked up the {obj.name.lower()}.")
        elif action == "Store Bucket":
            bucket = self.player.bucket
            if bucket is not None:
                bucket.container = f"object:{obj.object_id}"
                self.player.carried_objects.remove(bucket)
                obj.state.setdefault("bucket_ids", []).append(bucket.object_id)
                rebuild_tile_map(self.map)
                self.log("Stored the wooden bucket.")
        elif action == "Take Bucket":
            bucket_ids = obj.state.get("bucket_ids", [])
            if bucket_ids:
                bucket = self.objects[int(bucket_ids.pop(0))]
                bucket.container = "player"
                self.player.carried_objects.append(bucket)
                self.log("Took the wooden bucket.")
        elif action == "Store Food":
            food = next(
                (
                    carried
                    for carried in self.player.carried_objects
                    if carried.active and "edible" in carried.traits
                ),
                None,
            )
            if food is None:
                return
            food.container = f"object:{obj.object_id}"
            self.player.carried_objects.remove(food)
            obj.state.setdefault("food_ids", []).append(food.object_id)
            self.log(f"Stored the {food.name.lower()} in the cupboard.")
        elif action == "Take Food":
            food_ids = obj.state.setdefault("food_ids", [])
            if not food_ids:
                return
            food = self.objects.get(int(food_ids.pop(0)))
            if food is None or not food.active:
                return
            food.container = "player"
            self.player.carried_objects.append(food)
            self.log(f"Took the {food.name.lower()} from the cupboard.")
        elif action == "Harvest Wheat":
            grains_before = self.player.inventory["grains"]
            self.grant_interaction_loot(obj, action)
            grains_gathered = self.player.inventory["grains"] - grains_before
            self.award_skill_experience("harvesting")
            self.quests.record_event("wheat_harvested", grains_gathered)
            obj.active = False
            located = self.map.tile_map.tile_at_world(*obj.center)
            self.unlock("First Harvest")
            self.log(f"Harvested {grains_gathered} grains.")
        elif action == "Prepare Porridge":
            self.consume_interaction_cost(obj, action)
            definition = self.interaction_definition(obj, action)
            self.player.bucket_water_uses -= int(
                definition.get("water_cost", 0)
            )
            created = definition.get("creates", {})
            self.create_carried_object(
                str(created["type"]),
                quality=int(created.get("quality", 10)),
            )
            self.quests.record_event("homemade_food_prepared")
            self.log("Prepared a terrible bowl of porridge.")
        elif action in {"Eat Porridge", "Eat Berries"}:
            if "edible" in obj.traits:
                self.consume_carried_food(obj, "from the table")
                rebuild_tile_map(self.map)
                return
            definition = self.interaction_definition(obj, action)
            consumed_type = str(
                definition.get("consumes", {}).get("type", "")
            )
            food = next(
                (
                    carried
                    for carried in self.player.carried_objects
                    if carried.active
                    and "edible" in carried.traits
                    and carried.type_id == consumed_type
                ),
                None,
            )
            if food is None:
                return
            self.consume_carried_food(food, "at the table")
        elif action == "Power Nap":
            apply_condition_effects(
                self.player, {"fatigue": float(obj.quality)}
            )
            self.log(
                f"Power nap complete ({obj.quality} Fatigue recovered)."
            )
        elif action == "Sleep":
            self.unlock("Homecoming")
            self.player.last_sleep_id = obj.object_id
            self.begin_day_transition()

    def consume_carried_food(self, food: WorldObject, location: str) -> None:
        recovery = (
            food.condition_recovery
            if food.condition_recovery
            else {"hunger": float(food.nutrition)}
        )
        lethal = apply_condition_effects(self.player, recovery)
        self.attempt_illness_exposures(
            self.map.object_types[food.type_id]
            .form_definition(food.form or None, food.variant)
            .illness_exposures
        )
        self.quests.record_event("food_eaten")
        self.quests.record_event(
            "health_recovered",
            round(sum(max(0.0, float(value)) for value in recovery.values())),
        )
        food.active = False
        food.container = None
        if food in self.player.carried_objects:
            self.player.carried_objects.remove(food)
        if food.type_id == "porridge":
            self.quests.record_event("homemade_food_eaten")
        effects = ", ".join(
            f"{key} {value:+g}" for key, value in recovery.items() if value
        )
        self.log(f"Ate the {food.name.lower()} {location} ({effects}).")
        if lethal:
            self.log("The food was lethal. Night closes in.")
            self.begin_day_transition()

    def sync_controlled_character(self) -> None:
        character = self.map.characters.get(self.player.character_id)
        if character is None:
            return
        character.conditions = dict(self.player.conditions)
        character.condition_memory = dict(self.player.condition_memory)
        character.skills = self.player.skills
        character.last_sleep_id = self.player.last_sleep_id
        character.used_nap_windows = set(self.player.used_nap_windows)

    def interaction_definition(
        self, obj: WorldObject, action: str
    ) -> dict[str, object]:
        return obj.interactions.get(action, {})

    def create_carried_object(
        self, type_id: str, *, quality: int = 20
    ) -> WorldObject:
        definition = self.map.object_types[type_id]
        form = definition.form_definition()
        tile_size = self.map.tile_map.tile_size
        created = WorldObject(
            object_id=max(self.objects, default=0) + 1,
            name=form.name or definition.name_for(definition.default_variant),
            kind=definition.kind,
            x=round(self.player.x - form.footprint[0] * tile_size / 2),
            y=round(self.player.y - form.footprint[1] * tile_size / 2),
            width=form.footprint[0] * tile_size,
            height=form.footprint[1] * tile_size,
            state={"water_uses": 0} if "water" in form.capacity else {},
            blocks_movement=form.blocks_movement,
            blocks_vision=form.blocks_vision,
            mobility=form.mobility,
            traits=form.traits,
            descriptions=dict(form.descriptions),
            interactions={
                action: dict(details)
                for action, details in form.interactions.items()
            },
            capacity=form.capacity_for(quality),
            nutrition=form.nutrition,
            condition_recovery=dict(form.condition_recovery),
            type_id=type_id,
            quality=quality,
            variant=definition.default_variant,
            form=definition.default_form,
            container="player",
        )
        self.objects[created.object_id] = created
        self.player.carried_objects.append(created)
        return created

    def create_interaction_objects(
        self, obj: WorldObject, action: str
    ) -> list[WorldObject]:
        creates = self.interaction_definition(obj, action).get("creates", {})
        if not isinstance(creates, dict) or not creates.get("type"):
            return []
        quantity = max(0, int(creates.get("quantity", 1)))
        quality = max(1, min(100, int(creates.get("quality", obj.quality))))
        return [
            self.create_carried_object(str(creates["type"]), quality=quality)
            for _ in range(quantity)
        ]

    def drop_bucket(self, point: tuple[float, float]) -> None:
        bucket = self.player.bucket
        if bucket is None:
            return
        bucket.x = round(point[0] - bucket.width / 2)
        bucket.y = round(point[1] - bucket.height / 2)
        bucket.container = None
        self.player.carried_objects.remove(bucket)
        rebuild_tile_map(self.map)
        self.log("Dropped the wooden bucket.")

    def consume_interaction_cost(self, obj: WorldObject, action: str) -> None:
        cost = self.interaction_definition(obj, action).get("cost", {})
        for item, amount in cost.items():
            self.player.inventory[item] -= int(amount)

    def grant_interaction_loot(self, obj: WorldObject, action: str) -> list[str]:
        loot = self.interaction_definition(obj, action).get("loot", {})
        if "default" in loot or (
            obj.variant is not None and obj.variant in loot
        ):
            loot = loot.get(obj.variant, loot.get("default", {}))
        granted: list[str] = []
        for item, amount in loot.items():
            if isinstance(amount, list) and len(amount) == 2:
                granted_amount = random.randint(int(amount[0]), int(amount[1]))
            else:
                granted_amount = int(amount)
            self.player.inventory[item] += granted_amount
            granted.append(item)
        return granted

    def barrel_state(self, barrel: WorldObject) -> dict[str, object]:
        loaded = barrel.state
        memory = loaded.get("water_memory", {}) if isinstance(loaded, dict) else {}
        normalized = {
            "water_uses": max(0, min(self.barrel_capacity, int(loaded.get("water_uses", 0)))),
            "build_count": max(0, int(loaded.get("build_count", 0))),
            "built_today": bool(loaded.get("built_today", False)),
            "build_memory_id": loaded.get("build_memory_id"),
            "persistence_modifier": (
                float(loaded["persistence_modifier"])
                if loaded.get("persistence_modifier") is not None
                else None
            ),
            "water_memory": {
                "observed": max(0, min(self.barrel_capacity, int(memory.get("observed", 0)))),
                "count": max(0, int(memory.get("count", 0))),
                "remembered": max(0, min(self.barrel_capacity, int(memory.get("remembered", 0)))),
                "persistence_modifier": (
                    float(memory["persistence_modifier"])
                    if memory.get("persistence_modifier") is not None
                    else None
                ),
            },
        }
        self.sync_barrel_sprite_state(normalized)
        return normalized

    @staticmethod
    def sync_barrel_sprite_state(data: dict[str, object]) -> None:
        if int(data.get("water_uses", 0)) == 0:
            data["sprite_state"] = "empty"
        else:
            data.pop("sprite_state", None)

    def sync_all_barrel_sprite_states(self) -> None:
        for barrel in (
            obj for obj in self.objects.values() if obj.kind is ObjectKind.BARREL
        ):
            barrel.state = self.barrel_state(barrel)

    @staticmethod
    def persistence_modifier(
        identity: str, modifier_range: tuple[float, float]
    ) -> float:
        return random.Random(
            f"remembering:persistence-modifier:{identity}"
        ).uniform(*modifier_range)

    @staticmethod
    def policy_chance(count: int, modifier: float, policy) -> float:
        return min(1.0, policy.chance_per_count * count * modifier)

    def object_persistence_policy(
        self, obj: WorldObject, policy_id: str, form_id: str | None = None
    ):
        definition = self.map.object_types[obj.type_id]
        return definition.form_definition(form_id or obj.form).persistence.get(policy_id)

    def advance_simple_object_persistence(self) -> None:
        """Compatibility hook: persistence no longer decays between attempts."""

    def object_persistence_details(self, obj: WorldObject) -> list[str]:
        if obj.kind is ObjectKind.TREE:
            state = tree_state_data(obj.state)
            count = int(state["stump_memory_count"])
            if obj.form == "stump":
                count += 1
            policy = self.object_persistence_policy(obj, "object", "stump")
            if policy is None:
                return ["Persistence: cannot develop"]
            modifier = state["persistence_modifier"] or self.persistence_modifier(
                f"tree:{obj.object_id}:stump", policy.modifier_range
            )
            return [
                f"Stump memory count: {count}",
                f"Persistence affinity: {modifier:.2f}x",
                f"Persistence chance: {self.policy_chance(count, modifier, policy) * 100:.3f}%",
            ]
        if obj.kind is ObjectKind.BARREL:
            state = self.barrel_state(obj)
            policy_id = "water_level" if obj.persistent else "object"
            policy = self.object_persistence_policy(obj, policy_id)
            if policy is None:
                return ["Persistence: cannot develop"]
            if not obj.persistent:
                count = int(state["build_count"]) + (1 if state["built_today"] else 0)
                modifier = float(
                    state.get("persistence_modifier")
                    or self.persistence_modifier(
                        str(state.get("build_memory_id") or f"barrel:{obj.object_id}"),
                        policy.modifier_range,
                    )
                )
                return [
                    f"Barrel memory count: {count}",
                    f"Persistence affinity: {modifier:.2f}x",
                    f"Persistence chance: {self.policy_chance(count, modifier, policy) * 100:.3f}%",
                ]
            memory = state["water_memory"]
            count = int(memory["count"]) + 1
            modifier = float(
                memory.get("persistence_modifier")
                or self.persistence_modifier(
                    f"{state.get('build_memory_id')}:water", policy.modifier_range
                )
            )
            return [
                "Barrel persistence: remembered",
                f"Water-level count: {count}",
                f"Water affinity: {modifier:.2f}x",
                f"Level-memory chance: {self.policy_chance(count, modifier, policy) * 100:.3f}%",
                f"Remembered water: {memory['remembered']}/{self.barrel_capacity}",
            ]
        if obj.kind is ObjectKind.TOOL_STORAGE:
            policy = self.object_persistence_policy(obj, "stored_tools")
            if policy is None:
                return ["Persistence: cannot develop"]
            lines = []
            for tool, progress in self.storage_memories.get(obj.object_id, {}).items():
                count = int(progress["store_count"]) + (1 if progress["present"] else 0)
                modifier = float(
                    progress.get("persistence_modifier")
                    or self.persistence_modifier(
                        f"storage:{obj.object_id}:{tool}", policy.modifier_range
                    )
                )
                status = (
                    "remembered"
                    if progress["persistent"]
                    else f"{self.policy_chance(count, modifier, policy) * 100:.3f}%"
                )
                lines.append(
                    f"{tool.title()}: {count}, {modifier:.2f}x affinity ({status})"
                )
            return lines
        return ["Persistence: remembered"] if obj.persistent else ["Persistence: not remembered"]

    def tile_persistence_details(self, column: int, row: int) -> list[str]:
        state = self.map.tile_states.get((column, row))
        if state is None:
            return ["Persistence chance: 0.000%"]
        if state.kind_override == TileKind.SOIL.value:
            return [
                "Terrain: soil",
                "Till progress: 100.0%",
                (
                    "Chance to remain soil tonight: "
                    f"{state.soil_persistence_percentage:.1f}%"
                ),
            ]
        return [
            f"Till progress: {state.till_percentage:.1f}%",
            (
                "Remembered soil chance: "
                f"{state.soil_persistence_percentage:.1f}%"
            ),
            f"Persistence affinity: {(state.persistence_modifier or 1.0):.2f}x",
            (
                "Progress per till: "
                f"{self.map.till_progress_per_action * (state.persistence_modifier or 1.0):.3f}%"
            ),
        ]

    def build_cost(self, type_id: str) -> dict[str, int]:
        definition = self.map.object_types[type_id]
        return dict(definition.form_definition().build_cost)

    @property
    def barrel_capacity(self) -> int:
        return self.map.object_types["barrel"].form_definition().capacity_for(100)["water"]

    @property
    def bucket_capacity(self) -> int:
        quality = self.player.bucket.quality if self.player.bucket is not None else 100
        return self.map.object_types["bucket"].form_definition().capacity_for(quality)["water"]

    def apply_object_form(self, obj: WorldObject, form_id: str) -> None:
        definition = self.map.object_types[obj.type_id]
        form = definition.form_definition(form_id, obj.variant)
        obj.form = form_id
        obj.name = form.name or definition.name_for(obj.variant)
        obj.descriptions = dict(form.descriptions)
        obj.interactions = {
            action: dict(details) for action, details in form.interactions.items()
        }
        obj.capacity = form.capacity_for(obj.quality)
        obj.nutrition = form.nutrition
        obj.blocks_movement = form.blocks_movement
        obj.blocks_vision = form.blocks_vision
        obj.mobility = form.mobility
        obj.traits = form.traits
        width, height = (
            form.footprint[0] * self.map.tile_map.tile_size,
            form.footprint[1] * self.map.tile_map.tile_size,
        )
        if obj.orientation in {"N", "S", "N/S"}:
            width, height = height, width
        obj.width = width
        obj.height = height

    def can_afford_build(self, type_id: str) -> bool:
        cost = self.build_cost(type_id)
        return bool(cost) and all(
            self.player.inventory[item] >= amount for item, amount in cost.items()
        )

    def build_barrel(self, point: tuple[float, float]) -> None:
        definition = self.map.object_types["barrel"]
        form = definition.form_definition()
        width = form.footprint[0] * self.map.tile_map.tile_size
        height = form.footprint[1] * self.map.tile_map.tile_size
        column, row, _ = self.map.tile_map.tile_at_world(*point)
        memory_id = f"barrel:{column}:{row}"
        memory = self.build_memories.get(memory_id)
        if memory is None:
            policy = form.persistence.get("object")
            memory = BuildMemory(
                memory_id,
                "barrel",
                column,
                row,
                persistence_modifier=(
                    self.persistence_modifier(memory_id, policy.modifier_range)
                    if policy is not None
                    else None
                ),
            )
            self.build_memories[memory_id] = memory
        existing = self.object_for_build_memory(memory_id)
        if existing is None:
            object_id = max(self.objects, default=0) + 1
            x = round(point[0] - width / 2)
            y = round(point[1] - height / 2)
            existing = WorldObject(
                object_id,
                definition.name,
                definition.kind,
                x,
                y,
                width,
                height,
                state={"build_memory_id": memory_id},
                blocks_movement=form.blocks_movement,
                blocks_vision=form.blocks_vision,
                mobility=form.mobility,
                traits=form.traits,
                descriptions=dict(form.descriptions),
                interactions={
                    action: dict(details) for action, details in form.interactions.items()
                },
                capacity=form.capacity_for(20),
                nutrition=form.nutrition,
                condition_recovery=dict(form.condition_recovery),
                type_id=definition.type_id,
                quality=20,
                form=definition.default_form,
            )
            existing.persistent_state = ObjectState(
                x, y, quality=20, active=True, state={}, persistent=False
            )
            self.objects[object_id] = existing
        data = self.barrel_state(existing)
        data["build_memory_id"] = memory_id
        data["build_count"] = memory.build_count
        data["persistence_modifier"] = memory.persistence_modifier
        data["built_today"] = True
        data["water_uses"] = 0
        self.sync_barrel_sprite_state(data)
        existing.state = data
        existing.active = True
        for item, amount in self.build_cost("barrel").items():
            self.player.inventory[item] -= amount
        rebuild_tile_map(self.map)
        self.log("Built a wooden barrel.")

    def build_fixed_object(
        self, type_id: str, point: tuple[float, float]
    ) -> WorldObject:
        definition = self.map.object_types[type_id]
        form = definition.form_definition()
        tile_size = self.map.tile_map.tile_size
        width = form.footprint[0] * tile_size
        height = form.footprint[1] * tile_size
        column, row, _tile = self.map.tile_map.tile_at_world(*point)
        memory_id = f"{type_id}:{column}:{row}"
        memory = self.build_memories.setdefault(
            memory_id,
            BuildMemory(memory_id, type_id, column, row),
        )
        existing = self.object_for_build_memory(memory_id)
        if existing is None:
            object_id = max(self.objects, default=0) + 1
            x = round(point[0] - width / 2)
            y = round(point[1] - height / 2)
            existing = WorldObject(
                object_id,
                definition.name,
                definition.kind,
                x,
                y,
                width,
                height,
                state={"build_memory_id": memory_id, "food_ids": []},
                blocks_movement=form.blocks_movement,
                blocks_vision=form.blocks_vision,
                mobility=form.mobility,
                traits=form.traits,
                descriptions=dict(form.descriptions),
                interactions={
                    action: dict(details)
                    for action, details in form.interactions.items()
                },
                capacity=form.capacity_for(20),
                nutrition=form.nutrition,
                condition_recovery=dict(form.condition_recovery),
                type_id=type_id,
                quality=20,
                form=definition.default_form,
            )
            existing.persistent_state = ObjectState(
                x, y, quality=20, active=True, state={}, persistent=False
            )
            self.objects[object_id] = existing
        existing.active = True
        for item, amount in self.build_cost(type_id).items():
            self.player.inventory[item] -= amount
        rebuild_tile_map(self.map)
        self.log(f"Built a {definition.name.lower()}.")
        return existing

    def object_for_build_memory(self, memory_id: str) -> WorldObject | None:
        linked = next(
            (
                obj
                for obj in self.objects.values()
                if obj.active and obj.state.get("build_memory_id") == memory_id
            ),
            None,
        )
        if linked is not None:
            return linked
        memory = self.build_memories.get(memory_id)
        if memory is None:
            return None
        tile_size = self.map.tile_map.tile_size
        at_remembered_location = next(
            (
                obj
                for obj in self.objects.values()
                if obj.active
                and obj.type_id == memory.object_type
                and int(obj.center[0] // tile_size) == memory.column
                and int(obj.center[1] // tile_size) == memory.row
            ),
            None,
        )
        if at_remembered_location is not None:
            state = (
                self.barrel_state(at_remembered_location)
                if at_remembered_location.type_id == "barrel"
                else dict(at_remembered_location.state)
            )
            state["build_memory_id"] = memory_id
            at_remembered_location.state = state
        return at_remembered_location

    def begin_day_transition(self) -> None:
        if self.day_transition_phase is not None:
            return
        if len(self.day_position_history) > 1:
            # Keep the bedtime state so the rewind can begin immediately. The
            # comparatively expensive save/reload happens behind the fade-out.
            self.dawn_transition_state = self.capture_transition_state()
            self.day_transition_prepared = True
            self.rewind_cursor = float(len(self.day_position_history) - 1)
            self.rewind_speed = self.time_speed
            self.rewind_start_time_minutes = self.day.current_time_minutes
            self.rewind_checkpoint_index = len(self.rewind_checkpoints) - 1
        self.day_transition_phase = "night_bumper"
        self.day_transition_progress = 0.0
        self.simulation_paused = True

    def load_storage_memories(self) -> dict[int, dict[str, dict[str, object]]]:
        memories: dict[int, dict[str, dict[str, object]]] = {}
        for storage in (obj for obj in self.objects.values() if obj.type_id == "tool_storage"):
            storage.state["bucket_ids"] = [
                int(object_id)
                for object_id in storage.state.get("bucket_ids", [])
                if int(object_id) in self.objects
            ]
            data = storage.state
            tools = data.get("tools", {}) if isinstance(data, dict) else {}
            memories[storage.object_id] = {
                tool: {
                    "store_count": max(0, int(tools.get(tool, {}).get("store_count", 0))),
                    "present": bool(tools.get(tool, {}).get("present", False)),
                    "persistent": bool(tools.get(tool, {}).get("persistent", False)),
                    "quality": max(1, min(100, int(tools.get(tool, {}).get("quality", 20)))),
                    "persistence_modifier": (
                        float(tools.get(tool, {})["persistence_modifier"])
                        if tools.get(tool, {}).get("persistence_modifier") is not None
                        else None
                    ),
                }
                for tool in ("hoe", "axe")
            }
        return memories

    def sync_storage_memory(self, storage: WorldObject) -> None:
        storage.state = {"tools": self.storage_memories[storage.object_id]}

    def record_tool_stored(self, storage: WorldObject, tool: str, quality: int) -> None:
        progress = self.storage_memories[storage.object_id][tool]
        progress["present"] = True
        progress["quality"] = quality
        self.sync_storage_memory(storage)

    def record_tool_taken(self, storage: WorldObject, tool: str) -> None:
        self.storage_memories[storage.object_id][tool]["present"] = False
        self.sync_storage_memory(storage)

    def advance_storage_memories(self) -> None:
        randomizer = random.Random(44_701 + self.day.attempts)
        for storage_id, tools in self.storage_memories.items():
            storage = self.objects[storage_id]
            policy = self.object_persistence_policy(storage, "stored_tools")
            for tool in ("hoe", "axe"):
                progress = tools[tool]
                if policy is None:
                    continue
                if progress["persistence_modifier"] is None:
                    progress["persistence_modifier"] = self.persistence_modifier(
                        f"storage:{storage_id}:{tool}", policy.modifier_range
                    )
                modifier = float(progress["persistence_modifier"])
                if progress["present"]:
                    progress["store_count"] = int(progress["store_count"]) + 1
                    if not progress["persistent"]:
                        chance = self.policy_chance(
                            int(progress["store_count"]), modifier, policy
                        )
                        if randomizer.random() < chance:
                            progress["persistent"] = True
                        else:
                            progress["present"] = False
            self.sync_storage_memory(storage)
            if storage.persistent_state is not None:
                storage.persistent_state.state = storage.state

    def restore_persistent_tools(self) -> None:
        hoe = next((tools["hoe"] for tools in self.storage_memories.values() if tools["hoe"]["persistent"] and tools["hoe"]["present"]), None)
        axe = next((tools["axe"] for tools in self.storage_memories.values() if tools["axe"]["persistent"] and tools["axe"]["present"]), None)
        self.player.has_hoe = hoe is not None
        self.player.carrying_hoe = False
        if hoe is not None:
            self.player.hoe_quality = int(hoe["quality"])
        self.player.has_axe = axe is not None
        self.player.carrying_axe = False

    def advance_barrel_memories(self) -> None:
        randomizer = random.Random(83_119 + self.day.attempts)
        for barrel in (obj for obj in self.objects.values() if obj.type_id == "barrel"):
            data = self.barrel_state(barrel)
            memory_id = data.get("build_memory_id")
            memory = (
                self.build_memories.get(str(memory_id))
                if memory_id is not None
                else None
            )
            if not barrel.persistent:
                policy = self.object_persistence_policy(barrel, "object")
                if policy is None:
                    continue
                if data["persistence_modifier"] is None:
                    data["persistence_modifier"] = self.persistence_modifier(
                        str(memory_id or f"barrel:{barrel.object_id}"),
                        policy.modifier_range,
                    )
                modifier = float(data["persistence_modifier"])
                if memory is not None:
                    memory.persistence_modifier = modifier
                if not barrel.active or not data["built_today"]:
                    continue
                data["build_count"] = int(data["build_count"]) + 1
                if memory is not None:
                    memory.build_count = int(data["build_count"])
                chance = self.policy_chance(
                    int(data["build_count"]), modifier, policy
                )
                data["built_today"] = False
                data["water_uses"] = 0
                self.sync_barrel_sprite_state(data)
                if randomizer.random() < chance:
                    barrel.persistent = True
                    barrel.active = True
                    if memory is not None:
                        memory.persistent = True
                        memory.persistence_modifier = modifier
                    barrel.persistent_state = ObjectState(
                        barrel.x,
                        barrel.y,
                        barrel.orientation,
                        barrel.quality,
                        True,
                        data,
                        True,
                    )
                else:
                    barrel.active = False
                barrel.state = data
                if memory is not None:
                    memory.state = dict(data)
                continue

            actual_water = int(data["water_uses"])
            policy = self.object_persistence_policy(barrel, "water_level")
            if policy is None:
                continue
            water_memory = data["water_memory"]
            if water_memory["persistence_modifier"] is None:
                water_memory["persistence_modifier"] = self.persistence_modifier(
                    f"{memory_id or barrel.object_id}:water", policy.modifier_range
                )
            modifier = float(water_memory["persistence_modifier"])
            if actual_water == int(water_memory["observed"]):
                water_memory["count"] = int(water_memory["count"]) + 1
            else:
                water_memory["observed"] = actual_water
                water_memory["count"] = 1
            chance = self.policy_chance(
                int(water_memory["count"]), modifier, policy
            )
            if randomizer.random() < chance:
                water_memory["remembered"] = actual_water
            data["built_today"] = False
            barrel.state = data
            persistent_data = dict(data)
            persistent_data["water_uses"] = int(water_memory["remembered"])
            self.sync_barrel_sprite_state(persistent_data)
            if barrel.persistent_state is None:
                barrel.persistent_state = ObjectState(
                    barrel.x, barrel.y, barrel.orientation, barrel.quality, True, {}, True
                )
            barrel.persistent_state.state = persistent_data
            if memory is not None:
                memory.persistent = True
                memory.state = dict(persistent_data)

    def advance_stump_memories(self) -> None:
        randomizer = random.Random(97_331 + self.day.attempts)
        for tree in (obj for obj in self.objects.values() if obj.kind is ObjectKind.TREE):
            state = tree_state_data(tree.state)
            count = int(state["stump_memory_count"])
            policy = self.object_persistence_policy(tree, "object", "stump")
            if policy is None:
                continue
            if state["persistence_modifier"] is None:
                state["persistence_modifier"] = self.persistence_modifier(
                    f"tree:{tree.object_id}:stump", policy.modifier_range
                )
            modifier = float(state["persistence_modifier"])
            if tree.form == "stump":
                count += 1
                state["stump_memory_count"] = count
                chance = self.policy_chance(count, modifier, policy)
                if randomizer.random() < chance:
                    tree.persistent = True
                    remembered_state = encode_tree_state(state)
                    tree.state = remembered_state
                    tree.persistent_state = ObjectState(
                        tree.x,
                        tree.y,
                        tree.orientation,
                        tree.quality,
                        True,
                        remembered_state,
                        True,
                        variant=tree.variant,
                        form="stump",
                        flavor=tree.flavor,
                    )
                else:
                    state["branch_taken"] = False
                    tree.state = encode_tree_state(state)
                    self.apply_object_form(
                        tree, self.map.object_types["tree"].default_form
                    )
                    if tree.persistent_state is not None:
                        baseline = dict(state)
                        baseline["branch_taken"] = False
                        tree.persistent_state.state = encode_tree_state(baseline)
                        tree.persistent_state.form = self.map.object_types["tree"].default_form
                continue
            tree.state = encode_tree_state(state)
            if tree.persistent_state is not None:
                baseline = tree_state_data(tree.persistent_state.state)
                baseline["stump_memory_count"] = state["stump_memory_count"]
                tree.persistent_state.state = encode_tree_state(baseline)

    def update_day_transition(self, dt: float) -> None:
        if self.day_transition_phase == "planner":
            return
        if self.day_transition_phase == "night_bumper":
            self.day_transition_progress = min(
                1.0,
                self.day_transition_progress + dt / NIGHT_BUMPER_DURATION_SECONDS,
            )
            if self.day_transition_progress >= 1.0:
                if len(self.day_position_history) > 1:
                    self.day_transition_phase = "rewind"
                    self.day_transition_progress = 0.0
                else:
                    self.day_transition_phase = "fade_out"
                    self.day_transition_progress = 1.0
            return
        if self.day_transition_phase == "rewind":
            self.update_day_rewind(dt)
            return
        duration = (
            MORNING_OPENING_FADE_SECONDS
            if self.day_transition_phase == "fade_in"
            else DAY_FADE_DURATION_SECONDS
        )
        self.day_transition_progress = min(
            1.0, self.day_transition_progress + dt / duration
        )
        if self.day_transition_progress < 1.0:
            return
        if self.day_transition_phase == "fade_out":
            if self.day_transition_prepared and self.dawn_transition_state is not None:
                # Checkpoint playback temporarily restores earlier command
                # states; persist the actual bedtime state, not a rewound one.
                self.restore_transition_state(self.dawn_transition_state)
            if self.finish_day():
                self.day_transition_prepared = False
                self.dawn_transition_state = None
                self.rewind_checkpoints.clear()
                self.rewind_checkpoint_index = -1
                self.day_transition_phase = "planner"
                self.day_transition_progress = 0.0
            else:
                self.day_transition_phase = None
                self.day_transition_progress = 0.0
        else:
            self.day_transition_phase = None
            self.day_transition_progress = 0.0

    def update_day_rewind(self, dt: float) -> None:
        """Play the recorded movement route backward at the selected speed."""
        self.rewind_speed = self.time_speed
        samples_per_second = self.time_speed / FIXED_SIMULATION_TICK_SECONDS
        self.rewind_cursor = max(
            0.0, self.rewind_cursor - samples_per_second * dt
        )
        snapshot_to_restore: dict[str, object] | None = None
        while (
            self.rewind_checkpoint_index >= 0
            and self.rewind_cursor
            <= self.rewind_checkpoints[self.rewind_checkpoint_index][0]
        ):
            _sample, _action, snapshot_to_restore = self.rewind_checkpoints[
                self.rewind_checkpoint_index
            ]
            self.rewind_checkpoint_index -= 1
        # At high playback speeds a frame can cross several commands. Only the
        # oldest crossed snapshot can be visible, so avoid deep-copying the
        # whole world for every intermediate checkpoint.
        if snapshot_to_restore is not None:
            self.restore_rewind_checkpoint(snapshot_to_restore)
        lower = int(self.rewind_cursor)
        upper = min(lower + 1, len(self.day_position_history) - 1)
        fraction = self.rewind_cursor - lower
        start = self.day_position_history[lower]
        end = self.day_position_history[upper]
        self.player.x = start[0] + (end[0] - start[0]) * fraction
        self.player.y = start[1] + (end[1] - start[1]) * fraction
        total = max(1, len(self.day_position_history) - 1)
        self.day_transition_progress = 1.0 - self.rewind_cursor / total
        if self.rewind_cursor <= 0.0:
            self.day_transition_phase = "fade_out"
            # The next rendered frame must be fully masked. Otherwise restoring
            # the bedtime snapshot can expose a one-frame jump back to bed.
            self.day_transition_progress = 1.0

    def finish_day(self) -> bool:
        quest_state_before_dawn = self.quests.state_data()
        if (
            self.day.mode is Mode.DIRECT
            and self.day.today_routine
            and not self.auto_cheat_memory
        ):
            self.day.remembered_routine = list(self.day.today_routine)
        self.quests.record_bedtime(self.player.conditions)
        learn_dawn_conditions(self.player)
        self.sync_controlled_character()
        self.quests.record_event("days_survived")
        for message in self.quests.update(self.player, self.day):
            self.log(message)
        try:
            advance_level_tile_states(
                self.map.tile_states,
                day_number=self.day.attempts,
                reverted_till_progress_range=self.map.reverted_till_progress_range,
                persistence_modifier_range=self.map.tile_persistence_modifier_range,
            )
            self.advance_storage_memories()
            self.advance_barrel_memories()
            self.advance_stump_memories()
            save_persistent_objects(
                self.objects,
                self.persistence_path,
                tile_size=self.map.tile_map.tile_size,
                tile_states=self.map.tile_states,
                remembered_routine=self.day.remembered_routine,
                build_memories=self.build_memories,
                quest_state=self.quests.state_data(),
                characters=self.map.characters,
                controlled_character_id=self.map.controlled_character_id,
            )
            next_day_map = load_map(
                persistence_path=self.persistence_path,
                day_number=self.day.attempts + 1,
                reset_for_morning=True,
            )
            next_character = next_day_map.characters.get(self.player.character_id)
            if next_character is not None:
                next_character.conditions = dict(next_character.condition_memory)
                sleep_object = next_day_map.objects[next_character.last_sleep_id]
                next_character.conditions["fatigue"] = max(
                    0.0, min(99.0, 100.0 - sleep_object.quality)
                )
                next_character.used_nap_windows.clear()
                self.quests.record_start_day(next_character.conditions)
            else:
                self.quests.record_start_day(self.player.conditions)
            save_persistent_objects(
                next_day_map.objects,
                self.persistence_path,
                tile_size=next_day_map.tile_map.tile_size,
                tile_states=next_day_map.tile_states,
                remembered_routine=self.day.remembered_routine,
                build_memories=next_day_map.build_memories,
                quest_state=self.quests.state_data(),
                characters=next_day_map.characters,
                controlled_character_id=next_day_map.controlled_character_id,
            )
        except (MapLoadError, ObjectPersistenceError) as exc:
            self.quests.restore_state(quest_state_before_dawn)
            self.log(f"Could not finish the day: {exc}")
            return False
        self.map = next_day_map
        self.objects = next_day_map.objects
        self.build_memories = next_day_map.build_memories
        self.sync_all_barrel_sprite_states()
        self.player.carried_objects = [
            obj for obj in self.objects.values() if obj.container == "player"
        ]
        self.storage_memories = self.load_storage_memories()
        self.restore_persistent_tools()
        character = self.map.characters.get(self.player.character_id)
        if character is not None:
            self.player.conditions = dict(character.conditions)
            self.player.condition_memory = dict(character.condition_memory)
            self.player.skills = character.skills
            self.player.last_sleep_id = character.last_sleep_id
            self.player.used_nap_windows = set(character.used_nap_windows)
        self.day.attempts += 1
        self.day.mode = Mode.MORNING
        self.day.today_routine.clear()
        self.day.command_history.clear()
        self.day.replay_index = 0
        self.day.current_time_minutes = START_OF_DAY_MINUTES
        self.shown_night_hint_minutes.clear()
        self.time_accumulator = 0.0
        self.active_illnesses.clear()
        self.vomiting_timer_seconds = 0.0
        self.menu_index = 0
        self.player.inventory.clear()
        self.player.x, self.player.y = self.player_spawn()
        self.day_position_history = [(self.player.x, self.player.y)]
        self.rewind_cursor = 0.0
        self.rewind_speed = self.time_speed
        self.navigation_path = []
        self.preview_path = []
        self.walk_target = None
        self.path_target = None
        self.area_targets.clear()
        self.pending_area_target = None
        self.area_job_timer = 0.0
        self.barrel_source_selection_id = None
        self.barrel_fill_job = None
        self.pending_water_crop_selection = None
        self.field_water_job = None
        self.pending_source_areas.clear()
        self.pending_target_areas.clear()
        self.active_command = None
        self.selected_id = None
        self.selected_tile = None
        self.log(
            f"Day {self.day.number}, Attempt {self.day.attempts}. "
            "The previous attempt's completed jobs are remembered."
        )
        self.dawn_object_signatures = self.object_persistence_signatures()
        self.dawn_tile_signatures = self.tile_persistence_signatures()
        return True

    def object_persistence_signatures(self) -> dict[int, tuple[object, ...]]:
        ignored_state_fields = {
            "build_count",
            "built_today",
            "persistence_modifier",
            "stump_memory_count",
            "water_memory",
        }
        return {
            object_id: (
                obj.active,
                obj.x,
                obj.y,
                obj.orientation,
                obj.quality,
                obj.variant,
                obj.form,
                obj.container,
                json.dumps(
                    {
                        key: value
                        for key, value in obj.state.items()
                        if key not in ignored_state_fields
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
            for object_id, obj in self.objects.items()
        }

    def tile_persistence_signatures(
        self,
    ) -> dict[tuple[int, int], TileKind | None]:
        return {
            (column, row): (
                tile.kind
                if (tile := self.map.tile_map.tile_at(column, row)) is not None
                else None
            )
            for row in range(self.map.tile_map.rows)
            for column in range(self.map.tile_map.columns)
        }

    def unlock(self, name: str) -> None:
        if name not in self.player.achievements:
            self.player.achievements.add(name)
            self.log(f"Achievement unlocked: {name}")

    def log(self, message: str) -> None:
        self.messages.append(message)
        self.messages = self.messages[-MAX_MESSAGE_HISTORY:]
        self.message_scroll_offset = 0

    def draw(self) -> None:
        if self.adjusting_memory:
            self._editor_camera_restore = (
                self.camera.x,
                self.camera.y,
                self.camera.zoom,
            )
            if self.memory_editor_map_camera is None:
                self.camera.center_on(
                    self.command_editor_preview_position(),
                    (self.map.width, self.map.height),
                )
            else:
                camera_x, camera_y, zoom = self.memory_editor_map_camera
                self.camera.x = camera_x
                self.camera.y = camera_y
                self.camera.zoom = zoom * self.camera.world_scale
                self.camera.clamp((self.map.width, self.map.height))
        self.screen.fill((76, 104, 74))
        self.screen.set_clip(MAP_VIEWPORT)
        self.draw_tiles()
        self.draw_boundaries()
        self.draw_room_labels()
        self.draw_objects()
        self.draw_command_selection()
        self.draw_path()
        if not self.adjusting_memory:
            self.draw_player()
            self.draw_thought_bubble()
        self.screen.set_clip(None)
        self.draw_top_bar()
        self.draw_ui()
        if self.adjusting_memory:
            self.draw_memory_editor()
        elif self.day.mode is Mode.MORNING and self.day_transition_phase is None:
            self.draw_morning_menu()
        if self.context_menu_options:
            self.draw_context_menu()
        self.draw_day_transition()
        pygame.display.flip()

    def draw_day_transition(self) -> None:
        if self.day_transition_phase is None:
            return
        if self.day_transition_phase == "night_bumper":
            self.draw_night_bumper()
            return
        if self.day_transition_phase == "rewind":
            self.draw_rewind_vcr_effect()
            overlay = pygame.Surface(MAP_VIEWPORT.size, pygame.SRCALPHA)
            pulse = 28 + round(20 * self.day_transition_progress)
            overlay.fill((42, 62, 96, pulse))
            self.screen.blit(overlay, MAP_VIEWPORT.topleft)
            label = self.title_font.render(
                f"REWIND  {self.time_speed:g}x", True, (225, 235, 255)
            )
            self.screen.blit(
                label,
                (
                    MAP_VIEWPORT.centerx - label.get_width() // 2,
                    MAP_VIEWPORT.top + 28,
                ),
            )
            return
        if self.day_transition_phase == "planner":
            self.screen.fill((0, 0, 0))
            self.draw_morning_menu()
            return
        alpha = round(
            255 * (
                self.day_transition_progress
                if self.day_transition_phase == "fade_out"
                else 1.0 - self.day_transition_progress
            )
        )
        overlay = pygame.Surface(MAP_VIEWPORT.size, pygame.SRCALPHA)
        overlay.fill((0, 0, 0, alpha))
        self.screen.blit(overlay, MAP_VIEWPORT.topleft)
        if self.day_transition_phase == "fade_in":
            self.draw_wake_status(alpha)

    def draw_night_bumper(self) -> None:
        self.screen.fill((5, 7, 12))
        rendered = self.title_font.render(
            NIGHT_BUMPER_TEXT, True, (225, 222, 210)
        )
        self.screen.blit(rendered, rendered.get_rect(center=self.screen.get_rect().center))

    def draw_rewind_vcr_effect(self) -> None:
        """Distort the map like a worn tape without touching simulation state."""
        source = self.screen.subsurface(MAP_VIEWPORT).copy()
        phase = self.day_transition_progress * 37.0 + self.rewind_cursor * 0.071

        # A few displaced horizontal strips give the characteristic frame tear.
        for index, height in enumerate((7, 13, 5, 19)):
            wave = math.sin(phase * (1.3 + index * 0.23) + index * 2.1)
            y_range = max(1, MAP_VIEWPORT.height - height)
            y = int((phase * (47 + index * 29) + index * 113) % y_range)
            shift = round(wave * (5 + index * 3))
            strip = source.subsurface(pygame.Rect(0, y, MAP_VIEWPORT.width, height))
            self.screen.blit(strip, (MAP_VIEWPORT.left + shift, MAP_VIEWPORT.top + y))

        artifacts = pygame.Surface(MAP_VIEWPORT.size, pygame.SRCALPHA)
        # Alternating scanlines and two rolling tracking bands remain cheap to draw.
        scan_alpha = 18 + round(10 * abs(math.sin(phase)))
        for y in range(0, MAP_VIEWPORT.height, 4):
            pygame.draw.line(artifacts, (8, 12, 20, scan_alpha), (0, y), (MAP_VIEWPORT.width, y))
        for index in range(2):
            y = int((phase * (73 + index * 31) + index * 181) % MAP_VIEWPORT.height)
            pygame.draw.rect(artifacts, (220, 230, 255, 34), (0, y, MAP_VIEWPORT.width, 3))
            pygame.draw.rect(artifacts, (20, 28, 45, 54), (0, min(y + 4, MAP_VIEWPORT.height - 1), MAP_VIEWPORT.width, 8))

        # Offset cyan/red edges imply chromatic separation without per-pixel work.
        edge_alpha = 28 + round(18 * abs(math.cos(phase * 0.7)))
        pygame.draw.line(artifacts, (80, 220, 235, edge_alpha), (2, 0), (2, MAP_VIEWPORT.height))
        pygame.draw.line(artifacts, (235, 70, 90, edge_alpha), (MAP_VIEWPORT.width - 3, 0), (MAP_VIEWPORT.width - 3, MAP_VIEWPORT.height))
        self.screen.blit(artifacts, MAP_VIEWPORT.topleft)

    def wake_status_segments(
        self,
    ) -> list[tuple[str, pygame.font.Font, tuple[int, int, int]]]:
        segments = [("You wake up ", self.wake_fonts["plain"], (238, 238, 228))]
        character_type = self.map.character_types.get(
            self.map.characters.get(self.player.character_id).type_id
            if self.player.character_id in self.map.characters
            else ""
        )
        if character_type is None:
            return segments
        descriptor_segments: list[
            tuple[str, pygame.font.Font, tuple[int, int, int]]
        ] = []
        for condition_id in ("trauma", "fatigue", "thirst", "hunger"):
            row = condition_descriptor(
                character_type.conditions.get(condition_id, {}),
                self.player.conditions.get(condition_id, 0.0),
            )
            if row is None:
                continue
            color_value = row.get("color", [238, 238, 228])
            color = (
                tuple(int(component) for component in color_value)
                if isinstance(color_value, list) and len(color_value) == 3
                else (238, 238, 228)
            )
            font = self.wake_fonts.get(str(row.get("font", "plain")), self.wake_fonts["plain"])
            descriptor_segments.append((str(row["text"]), font, color))
        for index, segment in enumerate(descriptor_segments):
            if index:
                separator = (
                    ", and " if index == len(descriptor_segments) - 1 else ", "
                )
                segments.append(
                    (separator, self.wake_fonts["plain"], (238, 238, 228))
                )
            segments.append(segment)
        segments.append((".", self.wake_fonts["plain"], (238, 238, 228)))
        return segments

    def draw_wake_status(self, overlay_alpha: int) -> None:
        # Keep the message strongest while the world is still concealed.
        text_alpha = min(255, overlay_alpha * 2)
        rendered = [
            (font.render(text, True, color), font)
            for text, font, color in self.wake_status_segments()
        ]
        total_width = sum(surface.get_width() for surface, _font in rendered)
        x = MAP_VIEWPORT.centerx - total_width // 2
        baseline = MAP_VIEWPORT.centery
        for surface, font in rendered:
            surface.set_alpha(text_alpha)
            y = baseline - font.get_ascent() // 2
            self.screen.blit(surface, (x, y))
            x += surface.get_width()

    def displayed_time_minutes(self) -> int:
        """Return the clock value represented by the current visual frame."""
        if self.day_transition_phase != "rewind":
            return self.day.current_time_minutes
        total = max(1, len(self.day_position_history) - 1)
        route_fraction = max(0.0, min(1.0, self.rewind_cursor / total))
        return round(
            START_OF_DAY_MINUTES
            + (self.rewind_start_time_minutes - START_OF_DAY_MINUTES)
            * route_fraction
        )

    def draw_top_bar(self) -> None:
        panel = pygame.Rect(0, 0, WIDTH, TOP_BAR_HEIGHT)
        pygame.draw.rect(self.screen, (34, 40, 45), panel)
        pygame.draw.rect(self.screen, (188, 170, 105), panel, 2)
        time_area = pygame.Rect(MAP_LEFT, 0, MAP_RIGHT - MAP_LEFT, TOP_BAR_HEIGHT)
        time_background = pygame.Rect(time_area.x + 8, 2, 148, 24)
        pygame.draw.rect(
            self.screen, (34, 40, 45), time_background, border_radius=3
        )
        displayed_minutes = self.displayed_time_minutes()
        time_surface = self.small_font.render(
            f"Day {self.day.number}  {format_clock_time(displayed_minutes)}",
            True,
            (238, 238, 228),
        )
        self.screen.blit(
            time_surface, time_surface.get_rect(center=time_background.center)
        )

        controls_width = 58 + 4 + 58 + 4 + 28 + 2 + 52 + 2 + 28
        track_x = time_background.right + 8
        track_width = time_area.right - track_x - controls_width - 12
        track_y = 5
        pygame.draw.rect(self.screen, (84, 93, 98), (track_x, track_y, track_width, 18))
        progress = day_progress_ratio(displayed_minutes)
        sun_x = sun_track_position(progress, track_x, track_width)
        filled_width = sun_x - track_x
        pygame.draw.rect(self.screen, (234, 180, 84), (track_x, track_y, filled_width, 18))
        pygame.draw.circle(self.screen, (255, 220, 120), (sun_x, track_y + 9), 8)

        button_x = track_x + track_width + 8
        self.pause_button = pygame.Rect(button_x, 2, 58, 24)
        pygame.draw.rect(
            self.screen,
            (205, 194, 126) if self.simulation_paused else (74, 82, 87),
            self.pause_button,
            border_radius=3,
        )
        pygame.draw.rect(self.screen, (117, 124, 128), self.pause_button, 1, border_radius=3)
        pause_label = "Play" if self.simulation_paused else "Pause"
        rendered = self.small_font.render(pause_label, True, (232, 232, 222))
        self.screen.blit(rendered, rendered.get_rect(center=self.pause_button.center))
        button_x += 62
        self.step_button = pygame.Rect(button_x, 2, 58, 24)
        pygame.draw.rect(
            self.screen,
            (205, 194, 126) if self.single_step_active else (74, 82, 87),
            self.step_button,
            border_radius=3,
        )
        pygame.draw.rect(self.screen, (117, 124, 128), self.step_button, 1, border_radius=3)
        rendered = self.small_font.render("1 Cmd", True, (232, 232, 222))
        self.screen.blit(rendered, rendered.get_rect(center=self.step_button.center))
        button_x += 62

        self.speed_down_button = pygame.Rect(button_x, 2, 28, 24)
        self.speed_display = pygame.Rect(button_x + 30, 2, 52, 24)
        self.speed_up_button = pygame.Rect(button_x + 84, 2, 28, 24)
        for rect, label in (
            (self.speed_down_button, "−"),
            (
                self.speed_display,
                next(
                    (
                        label
                        for label, speed in TIME_SPEED_OPTIONS
                        if speed == self.time_speed
                    ),
                    f"{self.time_speed:g}x",
                ),
            ),
            (self.speed_up_button, "+"),
        ):
            pygame.draw.rect(self.screen, (74, 82, 87), rect, border_radius=3)
            pygame.draw.rect(self.screen, (117, 124, 128), rect, 1, border_radius=3)
            rendered = self.small_font.render(label, True, (232, 232, 222))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))

    def draw_tiles(self) -> None:
        tile_map = self.map.tile_map
        colors = {
            TileKind.WOODEN_FLOOR: (116, 92, 72),
            TileKind.DIRT: (112, 82, 57),
            TileKind.SOIL: (92, 64, 46),
            TileKind.GRASSLAND: (76, 104, 74),
            TileKind.SHALLOW_WATER: (72, 125, 143),
            TileKind.DESERT: (177, 151, 98),
            TileKind.HILLS: (112, 111, 86),
            TileKind.POND: (57, 105, 125),
            TileKind.MOUNTAIN: (83, 86, 82),
            TileKind.DEEP_WATER: (48, 88, 111),
            TileKind.CHASM: (31, 29, 35),
        }
        first_column = max(0, int(self.camera.x // tile_map.tile_size))
        last_column = min(
            tile_map.columns - 1,
            int(
                (
                    self.camera.x
                    + self.camera.viewport_width / self.camera.effective_zoom
                )
                // tile_map.tile_size
            ),
        )
        first_row = max(0, int(self.camera.y // tile_map.tile_size))
        last_row = min(
            tile_map.rows - 1,
            int(
                (
                    self.camera.y
                    + self.camera.viewport_height / self.camera.effective_zoom
                )
                // tile_map.tile_size
            ),
        )
        scaled_size = max(1, round(tile_map.tile_size * self.camera.effective_zoom))
        room_colors = {
            room.structure_id: room.display_color
            for room in self.map.structures
            if room.display_color is not None
        }
        for row in range(first_row, last_row + 1):
            for column in range(first_column, last_column + 1):
                tile = tile_map.tile_at(column, row)
                screen_x, screen_y = self.camera.world_to_screen(
                    (column * tile_map.tile_size, row * tile_map.tile_size)
                )
                rect = pygame.Rect(screen_x, screen_y, scaled_size + 1, scaled_size + 1)
                color = (37, 73, 43) if "blocked" in tile.properties else colors[tile.kind]
                if tile.kind is TileKind.WOODEN_FLOOR:
                    room_property = next(
                        (value for value in tile.properties if value.startswith("room:")),
                        None,
                    )
                    if room_property is not None:
                        color = room_colors.get(room_property.removeprefix("room:"), color)
                pygame.draw.rect(self.screen, color, rect)
                tile_sprite = self.tile_sprites.image_for(tile.kind.value)
                if tile_sprite is not None:
                    self.screen.blit(
                        pygame.transform.scale(tile_sprite, rect.size), rect.topleft
                    )
                state = self.map.tile_states.get((column, row))
                for overlay in self.map.tile_sprite_overlays.get(tile.kind.value, ()):
                    alpha = overlay_alpha(overlay, state)
                    if alpha <= 0:
                        continue
                    layer = self.tile_sprites.image_for(
                        tile.kind.value, overlay.overlay_id
                    )
                    if layer is None:
                        continue
                    layer = pygame.transform.scale(layer, rect.size)
                    layer.set_alpha(alpha)
                    self.screen.blit(layer, rect.topleft)
                if (
                    self.day_transition_phase == "rewind"
                    and (column, row) in self.rewind_flashing_tiles
                ):
                    flash = (math.sin(pygame.time.get_ticks() / 95.0) + 1.0) / 2.0
                    glow = pygame.Surface(rect.size, pygame.SRCALPHA)
                    glow.fill((255, 244, 150, round(45 + 125 * flash)))
                    self.screen.blit(glow, rect.topleft)
    def draw_boundaries(self) -> None:
        """Draw edge sprites, retaining geometry placeholders for missing assets."""
        authored: dict[tuple[int, int, str], BoundaryObject] = {
            (item.column, item.row, item.edge): item for item in self.map.boundaries
        }
        size = self.map.tile_map.tile_size
        def draw_key(boundary):
            return (boundary.edge == "west", boundary.row, boundary.column)
        colors = {"wall": (58, 48, 40), "fence": (126, 86, 48), "door": (176, 120, 58)}
        for boundary in sorted(authored.values(), key=draw_key):
            vertical = boundary.edge == "west"
            draw_column = boundary.column
            draw_row = boundary.row
            if boundary.kind == "door" and boundary.open:
                # Door sprites are authored closed, hinge-left, with the leaf
                # extending east. Opening swings the leaf 90 degrees CCW about
                # that hinge: north -> west above; west -> north to the right.
                counterclockwise = boundary.swing != "clockwise"
                if vertical:
                    vertical = False
                    if not counterclockwise:
                        draw_column -= 1
                        draw_row += 1
                else:
                    vertical = True
                    if counterclockwise:
                        draw_row -= 1
                    else:
                        draw_column += 1
            thickness = size * 8 / 64
            target_size = (thickness, size) if vertical else (size, thickness)
            scaled_size = (
                max(1, round(target_size[0] * self.camera.effective_zoom)),
                max(1, round(target_size[1] * self.camera.effective_zoom)),
            )
            world_x = draw_column * size
            world_y = draw_row * size
            screen_x, screen_y = self.camera.world_to_screen((world_x, world_y))
            rect = pygame.Rect(0, 0, *scaled_size)
            rect.center = round(screen_x), round(screen_y)
            if vertical:
                rect.centery = round(screen_y + size * self.camera.effective_zoom / 2)
            else:
                rect.centerx = round(screen_x + size * self.camera.effective_zoom / 2)
            source = self.boundary_sprites.image_for(boundary.kind)
            if source is not None:
                if vertical:
                    angle = (
                        90
                        if boundary.kind == "door"
                        and boundary.open
                        and boundary.swing != "clockwise"
                        else -90
                    )
                    source = pygame.transform.rotate(source, angle)
                elif (
                    boundary.kind == "door"
                    and boundary.open
                    and boundary.swing == "clockwise"
                    and boundary.edge == "west"
                ):
                    source = pygame.transform.rotate(source, 180)
                sprite = pygame.transform.scale(source, rect.size)
                self.screen.blit(sprite, rect)
                continue
            color = colors[boundary.kind]
            pygame.draw.rect(self.screen, color, rect)

    def draw_room_labels(self) -> None:
        for structure in self.map.structures:
            self.world_text(structure.name, (structure.x + 10, structure.y + 10), self.small_font)
            if structure.quality is not None:
                self.world_text(
                    structure.quality.value.title(),
                    (structure.x + 10, structure.y + 28),
                    self.small_font,
                    (218, 208, 188),
                )

    def draw_objects(self) -> None:
        for obj in self.objects.values():
            if not obj.active or obj.container is not None:
                continue
            screen_x, screen_y = self.camera.world_to_screen((obj.x, obj.y))
            rect = pygame.Rect(
                screen_x,
                screen_y,
                max(1, round(obj.width * self.camera.effective_zoom)),
                max(1, round(obj.height * self.camera.effective_zoom)),
            )
            if not MAP_VIEWPORT.colliderect(rect.inflate(8, 8)):
                continue
            selected = obj.object_id == self.selected_id
            border = max(1, round((4 if selected else 2) * self.camera.zoom))
            border_color = (245, 225, 120) if selected else (35, 35, 35)
            show_border = selected
            if (
                self.day_transition_phase == "rewind"
                and obj.object_id in self.rewind_flashing_objects
            ):
                flash = (math.sin(pygame.time.get_ticks() / 95.0) + 1.0) / 2.0
                show_border = True
                border_color = (255, round(205 + 45 * flash), round(90 + 90 * flash))
                border = max(border, max(2, round((3 + 3 * flash) * self.camera.zoom)))
            if (
                obj.kind is ObjectKind.WORKBENCH
                and not selected
                and bool(available_actions(obj, self.player))
            ):
                show_border = True
                pulse = (math.sin(pygame.time.get_ticks() / 350.0) + 1.0) / 2.0
                low = (74, 67, 48)
                high = (205, 177, 91)
                border_color = tuple(
                    round(low[channel] + (high[channel] - low[channel]) * pulse)
                    for channel in range(3)
                )
                border = max(border, max(1, round((2 + pulse) * self.camera.zoom)))
            definition = self.map.object_types[obj.type_id]
            loaded_sprite = self.object_sprites.sprite_for(obj, definition)
            if loaded_sprite is not None:
                sprite = loaded_sprite.image
                form = definition.form_definition(obj.form, obj.variant)
                for overlay in form.sprite_overlays:
                    alpha = overlay_alpha(overlay, obj.state, obj.capacity)
                    if alpha <= 0:
                        continue
                    loaded_overlay = self.object_sprites.overlay_for(
                        obj, definition, overlay.overlay_id
                    )
                    if loaded_overlay is None:
                        continue
                    layer = loaded_overlay.image
                    if layer.get_size() != sprite.get_size():
                        layer = pygame.transform.scale(layer, sprite.get_size())
                    else:
                        layer = layer.copy()
                    layer.set_alpha(alpha)
                    if sprite is loaded_sprite.image:
                        sprite = sprite.copy()
                    sprite.blit(layer, (0, 0))
                if loaded_sprite.anchor.mode == "random_within_tile":
                    margin = loaded_sprite.anchor.margin
                    margin = 0.2 if margin is None else margin
                    anchor_x, anchor_y = 0.5, 0.5
                else:
                    margin = None
                    placement_x, placement_y = 0.5, 0.5
                    anchor_x, anchor_y = loaded_sprite.anchor.point or (0.5, 0.5)
                orientation_angle = {"E": 0, "E/W": 0, "S": -90, "W": 180, "N": 90, "N/S": 90}.get(
                    obj.orientation, 0
                )
                if orientation_angle:
                    sprite = pygame.transform.rotate(sprite, orientation_angle)
                    if orientation_angle == 90:
                        anchor_x, anchor_y = anchor_y, 1.0 - anchor_x
                    elif orientation_angle == -90:
                        anchor_x, anchor_y = 1.0 - anchor_y, anchor_x
                    else:
                        anchor_x, anchor_y = 1.0 - anchor_x, 1.0 - anchor_y
                if loaded_sprite.rotation_angles:
                    rotation_randomizer = random.Random(
                        f"remembering:sprite-rotation:{obj.type_id}:{obj.object_id}"
                    )
                    angle = (
                        rotation_randomizer.uniform(0.0, 360.0)
                        if loaded_sprite.rotation_angles == "all"
                        else rotation_randomizer.choice(loaded_sprite.rotation_angles)
                    )
                    if angle:
                        sprite = pygame.transform.rotate(sprite, -angle)
                        radians = math.radians(angle)
                        offset_x, offset_y = anchor_x - 0.5, anchor_y - 0.5
                        anchor_x = (
                            0.5
                            + math.cos(radians) * offset_x
                            - math.sin(radians) * offset_y
                        )
                        anchor_y = (
                            0.5
                            + math.sin(radians) * offset_x
                            + math.cos(radians) * offset_y
                        )
                maximum_size = (
                    (
                        max(1, round(obj.width * (1.0 - margin * 2.0))),
                        max(1, round(obj.height * (1.0 - margin * 2.0))),
                    )
                    if margin is not None
                    else (obj.width, obj.height)
                )
                render_width, render_height = sprite_size_within_footprint(
                    sprite.get_size(), maximum_size
                )
                if margin is not None:
                    placement_x, placement_y = random_within_tile_anchor(
                        obj.object_id,
                        obj.type_id,
                        (render_width, render_height),
                        (obj.width, obj.height),
                        margin,
                    )
                scaled = pygame.transform.scale(
                    sprite,
                    (
                        max(1, round(render_width * self.camera.zoom)),
                        max(1, round(render_height * self.camera.zoom)),
                    ),
                )
                sprite_rect = scaled.get_rect()
                sprite_rect.x = round(
                    rect.left + rect.width * placement_x - sprite_rect.width * anchor_x
                )
                sprite_rect.y = round(
                    rect.top + rect.height * placement_y - sprite_rect.height * anchor_y
                )
                self.screen.blit(scaled, sprite_rect)
                if show_border:
                    pygame.draw.rect(self.screen, border_color, rect, border)
                continue

            pygame.draw.rect(self.screen, self.object_color(obj), rect)
            if show_border:
                pygame.draw.rect(self.screen, border_color, rect, border)
            label = object_map_label(obj)
            display_label = compact_label(
                label, obj.width, self.small_font.size(label)[0]
            )
            self.world_centered_text(display_label, obj, self.small_font)

    def object_color(self, obj: WorldObject) -> tuple[int, int, int]:
        return {
            ObjectKind.OBJECT: (154, 84, 111),
            ObjectKind.BED: (115, 91, 91),
            ObjectKind.TABLE: (117, 84, 54),
            ObjectKind.FOOD_PREP_STATION: (103, 103, 103),
            ObjectKind.BUSH: (74, 113, 69),
            ObjectKind.WORKBENCH: (125, 86, 52),
            ObjectKind.TOOL_STORAGE: (91, 75, 62),
            ObjectKind.BRANCH: (121, 84, 51),
            ObjectKind.PEBBLE: (119, 123, 124),
            ObjectKind.GRASS: (75, 130, 63),
            ObjectKind.WILD_PLANT: (178, 149, 62),
            ObjectKind.TREE: (54, 105, 61),
            ObjectKind.BOULDER: (91, 94, 91),
            ObjectKind.BARREL: (115, 78, 46),
            ObjectKind.CROP: (111, 139, 66),
            ObjectKind.BUCKET: (139, 99, 57),
            ObjectKind.BASKET: (155, 118, 72),
            ObjectKind.CUPBOARD: (112, 77, 48),
        }[obj.kind]

    def draw_command_selection(self) -> None:
        for bounds in self.pending_target_areas:
            self.draw_selection_bounds(bounds, (235, 205, 92))
        if self.pending_water_crop_selection is not None:
            self.draw_selection_bounds(
                self.pending_water_crop_selection[0], (117, 190, 105)
            )
        for bounds in self.pending_source_areas:
            self.draw_selection_bounds(bounds, (83, 165, 220))
        if self.command_drag_start is None or self.command_drag_current is None:
            return
        selection_start = (
            self.command_drag_current
            if self.target_selection_mode == "target"
            and self.active_command not in {"Fill Barrel", "Water Crops Source"}
            else self.command_drag_start
        )
        bounds = tile_aligned_area_bounds(
            selection_start, self.command_drag_current, self.map.tile_map.tile_size
        )
        color = (
            (83, 165, 220)
            if self.active_command in {"Fill Barrel", "Water Crops Source"}
            else (235, 205, 92)
        )
        self.draw_selection_bounds(bounds, color)

    def draw_selection_bounds(
        self,
        bounds: tuple[int, int, int, int],
        color: tuple[int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        start = self.camera.world_to_screen((left, top))
        end = self.camera.world_to_screen((right, bottom))
        rect = pygame.Rect(
            min(start[0], end[0]),
            min(start[1], end[1]),
            abs(end[0] - start[0]),
            abs(end[1] - start[1]),
        )
        overlay = pygame.Surface(rect.size, pygame.SRCALPHA)
        overlay.fill((*color, 55))
        self.screen.blit(overlay, rect.topleft)
        pygame.draw.rect(self.screen, color, rect, 2)

    def visible_path_world_points(self) -> list[tuple[float, float]]:
        """Return the path represented by the current frame."""
        player_position = (self.player.x, self.player.y)
        if self.day_transition_phase == "rewind":
            lower = min(
                int(self.rewind_cursor), len(self.day_position_history) - 1
            )
            earliest = max(0, lower - 24)
            candidates = [
                player_position,
                *reversed(self.day_position_history[earliest : lower + 1]),
            ]
            points: list[tuple[float, float]] = []
            for point in candidates:
                if not points or point != points[-1]:
                    points.append(point)
            return points
        route = self.navigation_path or self.preview_path
        return [player_position, *route] if route else []

    def draw_path(self) -> None:
        world_points = self.visible_path_world_points()
        if len(world_points) < 2:
            return
        points = [self.camera.world_to_screen(point) for point in world_points]
        for index in range(1, len(points)):
            pygame.draw.line(
                self.screen,
                (255, 215, 120),
                points[index - 1],
                points[index],
                max(
                    1,
                    round(
                        2 * self.world_scale * self.camera.effective_zoom
                    ),
                ),
            )

    def draw_player(self) -> None:
        px, py = self.camera.world_to_screen((self.player.x, self.player.y))
        radius = max(
            2, round(self.player_radius * self.camera.effective_zoom)
        )
        outline = max(
            1,
            round(2 * self.world_scale * self.camera.effective_zoom),
        )
        if self.active_command in AREA_COMMAND_TYPES:
            pygame.draw.circle(
                self.screen,
                (235, 205, 92),
                (px, py),
                radius + max(5, outline * 3),
                max(2, outline),
            )
        pygame.draw.circle(self.screen, (255, 220, 120), (px, py), radius + outline)
        visible_illnesses = self.visible_illness_factors()
        body_color = (
            max(visible_illnesses, key=lambda item: item[1])[0].tint_color
            if visible_illnesses
            else (222, 214, 187)
        )
        pygame.draw.circle(self.screen, body_color, (px, py), radius)
        pygame.draw.circle(self.screen, (35, 35, 35), (px, py), radius, outline)
        visual_zoom = self.world_scale * self.camera.effective_zoom
        eye_offset_x = round(4 * visual_zoom)
        eye_offset_y = round(3 * visual_zoom)
        eye_radius = max(1, round(2 * visual_zoom))
        pygame.draw.circle(self.screen, (35, 35, 35), (px - eye_offset_x, py - eye_offset_y), eye_radius)
        pygame.draw.circle(self.screen, (35, 35, 35), (px + eye_offset_x, py - eye_offset_y), eye_radius)
        mouth_rect = pygame.Rect(
            px - round(6 * visual_zoom),
            py + round(2 * visual_zoom),
            max(2, round(12 * visual_zoom)),
            max(2, round(8 * visual_zoom)),
        )
        pygame.draw.arc(self.screen, (35, 35, 35), mouth_rect, 0, math.pi, outline)
    def draw_thought_bubble(self) -> None:
        bubble_text = (
            self.thought_bubble_text
            if self.thought_bubble_source == "illness"
            else self.work_thought_text() or self.thought_bubble_text
        )
        if bubble_text is None:
            return
        px, py = self.camera.world_to_screen((self.player.x, self.player.y))
        lines = wrap_text(
            bubble_text,
            self.small_font,
            min(420, MAP_VIEWPORT.width - 40),
        )
        rendered_lines = [
            self.small_font.render(line, True, (35, 35, 35)) for line in lines
        ]
        line_height = self.small_font.get_linesize()
        bubble = pygame.Rect(
            0,
            0,
            max(rendered.get_width() for rendered in rendered_lines) + 20,
            len(rendered_lines) * line_height + 14,
        )
        bubble.midbottom = (
            px,
            py
            - max(
                18,
                round(self.player_radius * self.camera.effective_zoom),
            ),
        )
        bubble.clamp_ip(MAP_VIEWPORT.inflate(-8, -8))
        pygame.draw.rect(self.screen, (244, 239, 219), bubble, border_radius=10)
        pygame.draw.rect(self.screen, (55, 55, 50), bubble, 2, border_radius=10)
        tail_x = min(max(px, bubble.left + 12), bubble.right - 12)
        pygame.draw.circle(self.screen, (244, 239, 219), (tail_x, bubble.bottom + 5), 5)
        pygame.draw.circle(self.screen, (55, 55, 50), (tail_x, bubble.bottom + 5), 5, 1)
        pygame.draw.circle(self.screen, (244, 239, 219), (px, bubble.bottom + 12), 3)
        pygame.draw.circle(self.screen, (55, 55, 50), (px, bubble.bottom + 12), 3, 1)
        text_top = bubble.top + 7
        for index, rendered in enumerate(rendered_lines):
            self.screen.blit(
                rendered,
                rendered.get_rect(
                    centerx=bubble.centerx,
                    top=text_top + index * line_height,
                ),
            )

    def draw_player_dock(self) -> None:
        self.inventory_food_buttons.clear()
        layout = player_dock_layout(
            equipment_collapsed=self.equipment_collapsed
        )
        panel_color = (31, 35, 38)
        border_color = (92, 95, 101)
        for rect in (layout.player_info, layout.equipment, layout.inventory):
            pygame.draw.rect(self.screen, panel_color, rect)
            pygame.draw.rect(self.screen, border_color, rect, 2)

        content_x = layout.player_info.x + 12
        right_column_x = layout.player_info.x + layout.player_info.width // 2 + 8
        self.text("Conditions", (content_x, 14), self.small_font, (172, 176, 182))
        self.text("Stats", (right_column_x, 14), self.small_font, (172, 176, 182))
        for index, condition_id in enumerate(CONDITION_IDS):
            value = self.player.conditions[condition_id]
            if condition_id != "trauma" or critical_trauma_visible(
                value, pygame.time.get_ticks()
            ):
                self.text(
                    f"{CONDITION_LABELS[condition_id]}: "
                    f"{round(value)}",
                    (content_x, 44 + index * 28),
                    self.small_font,
                    condition_color(value),
                )
        self.text(
            f"Day: {self.day.number}",
            (right_column_x, 44),
            self.small_font,
        )
        self.text(
            f"Attempts: {self.day.attempts}",
            (right_column_x, 72),
            self.small_font,
        )
        self.text(
            f"Movement: {movement_speed_multiplier(self.player) * 100:.1f}%",
            (right_column_x, 100),
            self.small_font,
        )
        self.text(
            f"Task Speed: {task_speed_multiplier(self.player) * 100:.1f}%",
            (right_column_x, 128),
            self.small_font,
        )
        rate = healing_rate(self.player)
        self.text(
            f"Healing: {rate:+.2f}/hr",
            (right_column_x, 156),
            self.small_font,
            (112, 198, 112) if rate > 0 else (235, 88, 88) if rate < 0 else (210, 214, 202),
        )
        for index, (definition, illness_value) in enumerate(
            self.visible_illness_factors()
        ):
            self.text(
                f"{definition.name}: {illness_value:.0f}%",
                (right_column_x, 184 + index * 24),
                self.small_font,
                definition.tint_color,
            )
        self.text("Skills", (content_x, 174), self.small_font, (172, 176, 182))
        for index, (skill_id, skill) in enumerate(
            sorted(self.player.skills.items())
        ):
            per_level = float(
                self.skill_config(skill_id).get("experience_per_level", 10.0)
            )
            current_xp = skill.experience - skill.level * per_level
            self.text(
                f"{skill_id.title()}: L{skill.level}  "
                f"{current_xp:g}/{per_level:g} XP",
                (content_x, 202 + index * 24),
                self.small_font,
            )

        self.draw_equipment_inventory(layout, border_color)

    def draw_selection_details(self, panel: pygame.Rect) -> None:
        x = panel.x + 12
        self.text("Selection", (x, panel.y + 12), self.font)
        self.text(
            "Selected object or tile",
            (x, panel.y + 40),
            self.small_font,
            (172, 176, 182),
        )
        if self.selected_id is not None:
            obj = self.objects[self.selected_id]
            display_name = (
                object_map_label(obj) if obj.kind is ObjectKind.TREE else obj.name
            )
            self.text(display_name, (x, panel.y + 68), self.font)
            kind_label = obj.kind.name.lower().replace("_", " ").title()
            self.text(
                f"{kind_label} | {obj.quality_stage.title()}",
                (x, panel.y + 96),
                self.small_font,
                (172, 176, 182),
            )
            lines = wrap_text(
                obj.description, self.small_font, panel.width - 24
            )
            lines.extend(crop_inspection_lines(obj))
            if "edible" in obj.traits:
                lines.append(f"Nutrition: {obj.nutrition}")
            if obj.kind is ObjectKind.CUPBOARD:
                lines.append(
                    f"Food stored: {len(obj.state.get('food_ids', []))}/"
                    f"{obj.capacity.get('food', 0)}"
                )
            if obj.kind is ObjectKind.BARREL:
                lines.append(
                    f"Water: {self.barrel_state(obj)['water_uses']}/"
                    f"{self.barrel_capacity} uses"
                )
            if pygame.key.get_mods() & pygame.KMOD_SHIFT:
                lines.extend(self.object_persistence_details(obj))
            for index, line in enumerate(lines):
                y = panel.y + 124 + index * 20
                if y + 20 >= panel.bottom:
                    break
                self.text(line, (x, y), self.small_font, (210, 214, 202))
            return

        if self.selected_tile is None:
            self.text(
                "Click an object or tile in the world to inspect it.",
                (x, panel.y + 76),
                self.small_font,
                (210, 214, 202),
            )
            return
        column, row = self.selected_tile
        tile = self.map.tile_map.tile_at(column, row)
        crop = self.crop_at_tile(column, row)
        tile_name = (
            tile.kind.value.replace("_", " ").title()
            if tile is not None
            else "Tile"
        )
        self.text(tile_name, (x, panel.y + 68), self.font)
        lines = [f"Tile: {column}, {row}"]
        if crop is None:
            lines.append("Plant: none")
        else:
            lines.extend(
                (
                    f"Plant: {(crop.variant or crop.name).title()}",
                    f"Form: {crop.form.title()}",
                    f"Growth: {float(crop.state.get('growth_progress', 0.0)) * 100:.1f}%",
                    f"Water: {float(crop.state.get('water', 0.0)):.1f}%",
                    f"Tended: {float(crop.state.get('tended', 0.0)):.1f}%",
                )
            )
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            lines.extend(self.tile_persistence_details(column, row))
        for index, line in enumerate(lines):
            self.text(
                line,
                (x, panel.y + 100 + index * 22),
                self.small_font,
                (210, 214, 202),
            )

    def draw_equipment_inventory(
        self, layout: PlayerDockLayout, border_color: tuple[int, int, int]
    ) -> None:
        self.text(
            "Equipment",
            (layout.equipment.x + 12, layout.equipment.y + 8),
            self.small_font,
            (172, 176, 182),
        )
        self.equipment_toggle_button = layout.equipment_toggle
        pygame.draw.rect(
            self.screen, (74, 82, 87), self.equipment_toggle_button, border_radius=3
        )
        pygame.draw.rect(
            self.screen, border_color, self.equipment_toggle_button, 1, border_radius=3
        )
        toggle_label = "+" if self.equipment_collapsed else "−"
        rendered = self.small_font.render(toggle_label, True, (232, 232, 222))
        self.screen.blit(
            rendered, rendered.get_rect(center=self.equipment_toggle_button.center)
        )

        if not self.equipment_collapsed:
            hoe = (
                f"{'carried' if self.player.carrying_hoe else 'stored'} "
                f"(Q{self.player.hoe_quality})"
                if self.player.has_hoe
                else "none"
            )
            axe = (
                "carried"
                if self.player.carrying_axe
                else "stored" if self.player.has_axe else "none"
            )
            bucket = (
                f"{self.player.bucket_water_uses}/{self.bucket_capacity} uses"
                if self.player.has_bucket
                else "none"
            )
            basket = "carried" if self.player.has_basket else "none"
            equipment_lines = (
                f"Hoe: {hoe}",
                f"Axe: {axe}",
                f"Bucket: {bucket}",
                f"Basket: {basket}",
            )
            for index, line in enumerate(equipment_lines):
                self.text(
                    line,
                    (layout.equipment.x + 12, layout.equipment.y + 42 + index * 26),
                    self.small_font,
                )

        tab_color = (64, 71, 76)
        selected_tab_color = (116, 111, 83)
        self.player_info_tab_buttons = [
            (layout.inventory_tab, InventoryPage.INVENTORY),
            (layout.quests_tab, InventoryPage.QUESTS),
        ]
        for rect, page in self.player_info_tab_buttons:
            selected = page is self.inventory_page
            pygame.draw.rect(
                self.screen,
                selected_tab_color if selected else tab_color,
                rect,
                border_radius=3,
            )
            pygame.draw.rect(self.screen, border_color, rect, 1, border_radius=3)
            rendered = self.small_font.render(
                page.value.title(), True, (232, 232, 222)
            )
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))

        self.macro_buttons.clear()
        self.macro_dropdown_buttons.clear()
        if self.inventory_page is InventoryPage.MACROS:
            content_x = layout.inventory.x + 12
            content_y = layout.inventory.y + 42
            status = "Recording" if self.macro_recording else "Ready"
            name = self.memory_file_name or "Unnamed set"
            self.text("Current Routine", (content_x, content_y), self.small_font, (172, 176, 182))
            dropdown = pygame.Rect(
                content_x, content_y + 22, layout.inventory.width - 24, 32
            )
            pygame.draw.rect(self.screen, (218, 213, 190), dropdown)
            pygame.draw.rect(self.screen, (92, 95, 101), dropdown, 2)
            self.text(name, (dropdown.x + 9, dropdown.y + 8), self.small_font, (35, 35, 35))
            arrow = self.small_font.render("▲" if self.macro_dropdown_open else "▼", True, (35, 35, 35))
            self.screen.blit(arrow, arrow.get_rect(center=(dropdown.right - 17, dropdown.centery)))
            self.macro_buttons.append((dropdown, "Macro Dropdown"))
            self.text(
                f"Status: {status}",
                (content_x, content_y + 62),
                self.small_font,
                (235, 205, 120) if self.macro_recording else (172, 176, 182),
            )
            actions = ["Play", "Stop", "Edit"] if self.macro_recording else ["Play", "Record", "Edit"]
            for index, action in enumerate(actions):
                rect = pygame.Rect(
                    content_x, content_y + 88 + index * 38, layout.inventory.width - 24, 32
                )
                pygame.draw.rect(self.screen, (174, 169, 145), rect)
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 2)
                rendered = self.small_font.render(action, True, (25, 25, 25))
                self.screen.blit(rendered, rendered.get_rect(center=rect.center))
                self.macro_buttons.append((rect, action))
            if self.macro_dropdown_open:
                entries = [("__new__", 0.0), *sorted(
                    self.available_command_sets(), key=lambda item: item[0].lower()
                )]
                for index, (macro_name, _modified) in enumerate(entries):
                    option = pygame.Rect(
                        dropdown.x,
                        dropdown.bottom + index * 30,
                        dropdown.width,
                        30,
                    )
                    if option.bottom > layout.inventory.bottom - 8:
                        break
                    pygame.draw.rect(self.screen, (234, 229, 207), option)
                    pygame.draw.rect(self.screen, (92, 95, 101), option, 1)
                    self.text(
                        "New…" if macro_name == "__new__" else macro_name,
                        (option.x + 9, option.y + 7),
                        self.small_font,
                        (35, 35, 35),
                    )
                    self.macro_dropdown_buttons.append((option, macro_name))
            return

        if self.inventory_page is InventoryPage.QUESTS:
            active_quests = self.quests.active_quests
            content_x = layout.inventory.x + 12
            content_y = layout.inventory.y + 42
            if not active_quests:
                self.text("No active quests.", (content_x, content_y), self.small_font)
            for index, quest in enumerate(active_quests):
                y = content_y + index * 48
                if y + 42 >= layout.inventory.bottom:
                    break
                self.text(quest.title, (content_x, y), self.small_font)
                lines = wrap_text(
                    quest.description,
                    self.small_font,
                    layout.inventory.width - 24,
                )
                if lines:
                    self.text(
                        lines[0],
                        (content_x, y + 21),
                        self.small_font,
                        (172, 176, 182),
                    )
            return

        inventory_items = [
            (name, quantity)
            for name, quantity in sorted(self.player.inventory.items())
            if quantity
        ]
        carried_food = [
            obj
            for obj in self.player.carried_objects
            if obj.active and "edible" in obj.traits
        ]
        inventory_lines = [
            *(f"{name}: {quantity}" for name, quantity in inventory_items),
            *(f"{obj.name} (food)" for obj in carried_food),
        ]
        if inventory_lines:
            for index, line in enumerate(inventory_lines):
                y = layout.inventory.y + 42 + index * 24
                if y + 20 >= layout.inventory.bottom:
                    break
                self.text(line, (layout.inventory.x + 12, y), self.small_font)
                food_index = index - len(inventory_items)
                if food_index >= 0:
                    self.inventory_food_buttons.append(
                        (
                            pygame.Rect(
                                layout.inventory.x + 8,
                                y - 2,
                                layout.inventory.width - 16,
                                22,
                            ),
                            carried_food[food_index],
                        )
                    )
        else:
            self.text(
                "empty",
                (layout.inventory.x + 12, layout.inventory.y + 42),
                self.small_font,
            )

    def draw_ui(self) -> None:
        sidebar = LEFT_DOCK_RECT
        right_panel = RIGHT_DOCK_RECT
        pygame.draw.rect(self.screen, (25, 28, 31), sidebar)
        pygame.draw.rect(self.screen, (25, 28, 31), right_panel)
        pygame.draw.rect(self.screen, (92, 95, 101), sidebar, 2)
        pygame.draw.rect(self.screen, (92, 95, 101), right_panel, 2)
        if self.routine_moves_expanded:
            self.draw_routine_moves_dock()
        else:
            self.draw_player_dock()
        self.draw_selection_details(LEFT_SELECTION_RECT)
        pygame.draw.rect(self.screen, (92, 95, 101), LEFT_SELECTION_RECT, 2)
        self.text(
            "Command Menu",
            (LEFT_COMMAND_RECT.x + 12, LEFT_COMMAND_RECT.y + 12),
            self.font,
        )

        self.text(
            "Routines",
            (LEFT_COMMAND_RECT.x + 12, LEFT_COMMAND_RECT.y + 42),
            self.small_font,
            (172, 176, 182),
        )
        self.macro_command_buttons.clear()
        current_name = self.memory_file_name or "Select routine…"
        dropdown = pygame.Rect(
            LEFT_COMMAND_RECT.x + 12,
            LEFT_COMMAND_RECT.y + 60,
            LEFT_COMMAND_RECT.width - 142,
            30,
        )
        play = pygame.Rect(dropdown.right + 8, dropdown.y, 110, 30)
        pygame.draw.rect(self.screen, (218, 213, 190), dropdown)
        pygame.draw.rect(self.screen, (92, 95, 101), dropdown, 2)
        self.text(current_name, (dropdown.x + 8, dropdown.y + 7), self.small_font, (35, 35, 35))
        arrow = self.small_font.render("▲" if self.macro_dropdown_open else "▼", True, (35, 35, 35))
        self.screen.blit(arrow, arrow.get_rect(center=(dropdown.right - 16, dropdown.centery)))
        pygame.draw.rect(self.screen, (174, 169, 145), play)
        pygame.draw.rect(self.screen, (35, 35, 35), play, 1)
        rendered = self.small_font.render("Play", True, (25, 25, 25))
        self.screen.blit(rendered, rendered.get_rect(center=play.center))
        self.macro_command_buttons.extend(((dropdown, "Macro Dropdown"), (play, "Play")))
        macro_gap = 6
        macro_button_width = (
            LEFT_COMMAND_RECT.width - 24 - macro_gap * 2
        ) // 3
        for index, action in enumerate(("Record", "Stop", "Edit")):
            rect = pygame.Rect(
                LEFT_COMMAND_RECT.x + 12 + index * (macro_button_width + macro_gap),
                LEFT_COMMAND_RECT.y + 98,
                macro_button_width,
                28,
            )
            enabled = (
                action == "Edit"
                or (action == "Record" and not self.macro_recording)
                or (action == "Stop" and self.macro_recording)
            )
            pygame.draw.rect(
                self.screen, (174, 169, 145) if enabled else (75, 78, 78), rect
            )
            pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
            rendered = self.small_font.render(
                action, True, (25, 25, 25) if enabled else (145, 145, 138)
            )
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            if enabled:
                self.macro_command_buttons.append((rect, action))

        self.action_buttons.clear()
        action_y = LEFT_COMMAND_RECT.y + 162
        if self.selected_id is not None:
            obj = self.objects[self.selected_id]
            actions = (
                object_action_menu_options(obj, self.player)
                if self.day.mode is Mode.DIRECT
                else []
            )
            self.text(
                "Selection Actions",
                (12, LEFT_COMMAND_RECT.y + 136),
                self.small_font,
                (172, 176, 182),
            )
            for index, action in enumerate(actions):
                workbench_recipe = obj.kind is ObjectKind.WORKBENCH
                spacing = 48 if workbench_recipe else 38
                height = 42 if workbench_recipe else 32
                rect = pygame.Rect(12, action_y + index * spacing, SIDEBAR_WIDTH - 24, height)
                disabled = not action_option_enabled(action)
                pygame.draw.rect(self.screen, (95, 96, 92) if disabled else (223, 220, 196), rect)
                pygame.draw.rect(self.screen, (75, 76, 73) if disabled else (30, 30, 30), rect, 2)
                label = action.removesuffix(DISABLED_INTERACTION_SUFFIX)
                self.text(f"{index + 1}. {label}", (rect.x + 8, rect.y + 5), self.small_font, (145, 145, 138) if disabled else (25, 25, 25))
                if disabled and workbench_recipe:
                    missing = missing_recipe_ingredients(obj, self.player, action)
                    detail = (
                        "Missing: "
                        + ", ".join(
                            ingredient_requirement_label(item, amount)
                            for item, amount in missing
                        )
                        if missing
                        else "Requirements not met"
                    )
                    self.text(detail, (rect.x + 24, rect.y + 23), self.small_font, (145, 145, 138))
                self.action_buttons.append((rect, action))
            if not actions:
                label = "Inspection only during replay" if self.day.mode is Mode.REPLAY else "No available action"
                self.text(label, (12, action_y), self.small_font, (210, 214, 202))
        else:
            self.text(
                "Select an object to show its actions.",
                (12, LEFT_COMMAND_RECT.y + 136),
                self.small_font,
                (210, 214, 202),
            )
        action_bottom = max(
            (rect.bottom for rect, _action in self.action_buttons),
            default=LEFT_COMMAND_RECT.y + 156,
        )

        self.command_buttons.clear()
        menu_title = "Area Commands"
        if self.active_command_category is not None:
            menu_title += f" > {self.active_command_category}"
        area_menu_y = max(LEFT_COMMAND_RECT.y + 242, action_bottom + 24)
        self.text(menu_title, (12, area_menu_y), self.small_font, (172, 176, 182))
        self.text("Quantity", (12, area_menu_y + 28), self.small_font, (172, 176, 182))
        self.area_quantity_buttons.clear()
        decrease_rect = pygame.Rect(112, area_menu_y + 23, 22, 24)
        increase_rect = pygame.Rect(160, area_menu_y + 23, 22, 24)
        for rect, label, change in (
            (decrease_rect, "-", -1),
            (increase_rect, "+", 1),
        ):
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
            rendered = self.small_font.render(label, True, (25, 25, 25))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.area_quantity_buttons.append((rect, change))
        quantity_surface = self.small_font.render(
            str(self.area_command_quantity), True, (225, 225, 214)
        )
        self.screen.blit(
            quantity_surface,
            quantity_surface.get_rect(center=(147, area_menu_y + 35)),
        )

        self.till_time_buttons.clear()
        command_start_y = area_menu_y + 52
        if self.active_command == "Till Grassland":
            self.text("Till time", (12, command_start_y + 6), self.small_font, (172, 176, 182))
            decrease_time = pygame.Rect(82, command_start_y + 1, 24, 24)
            increase_time = pygame.Rect(154, command_start_y + 1, 24, 24)
            until_done = pygame.Rect(184, command_start_y + 1, 94, 24)
            for rect, label, action in (
                (decrease_time, "-", "decrease"),
                (increase_time, "+", "increase"),
                (until_done, "Until Done", "until_done"),
            ):
                selected = action == "until_done" and self.till_until_done
                pygame.draw.rect(
                    self.screen,
                    (205, 194, 126) if selected else (174, 169, 145),
                    rect,
                )
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
                rendered = self.small_font.render(label, True, (25, 25, 25))
                self.screen.blit(rendered, rendered.get_rect(center=rect.center))
                self.till_time_buttons.append((rect, action))
            budget_label = (
                "done"
                if self.till_until_done
                else (
                    f"{self.till_max_game_minutes // 60}h"
                    if self.till_max_game_minutes % 60 == 0
                    else f"{self.till_max_game_minutes / 60:.1f}h"
                )
            )
            self.text(budget_label, (112, command_start_y + 6), self.small_font, (225, 225, 214))
            command_start_y += 30

        self.command_category_buttons.clear()
        if self.active_command_category is None:
            for index, category in enumerate(AREA_COMMAND_CATEGORIES):
                rect = pygame.Rect(12, command_start_y + index * 30, SIDEBAR_WIDTH - 24, 27)
                pygame.draw.rect(self.screen, (103, 105, 94), rect)
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 2)
                self.text(f"{index + 1}. {category}", (rect.x + 7, rect.y + 6), self.small_font, (230, 230, 218))
                self.command_category_buttons.append((rect, category))

        visible_commands = (
            [*area_commands_for_category(self.player, self.active_command_category), "Back"]
            if self.active_command_category is not None
            else []
        )
        for index, command in enumerate(visible_commands):
            rect = pygame.Rect(12, command_start_y + index * 30, SIDEBAR_WIDTH - 24, 27)
            selected = command == self.active_command
            pygame.draw.rect(self.screen, (205, 194, 126) if selected else (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (245, 225, 120) if selected else (35, 35, 35), rect, 2)
            self.text(f"{index + 1}. {command}", (rect.x + 7, rect.y + 6), self.small_font, (25, 25, 25))
            self.command_buttons.append((rect, command))

        self.action_buttons = [
            (rect, action)
            for rect, action in self.action_buttons
            if rect.bottom <= LEFT_MESSAGE_HISTORY_RECT.top
        ]
        self.command_buttons = [
            (rect, command)
            for rect, command in self.command_buttons
            if rect.bottom <= LEFT_MESSAGE_HISTORY_RECT.top
        ]
        self.command_category_buttons = [
            (rect, category)
            for rect, category in self.command_category_buttons
            if rect.bottom <= LEFT_MESSAGE_HISTORY_RECT.top
        ]

        self.draw_message_history()
        self.draw_compact_command_menu()
        self.draw_routine_bar()

        hovered = RELOAD_BUTTON.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (205, 198, 166) if hovered else (174, 169, 145), RELOAD_BUTTON)
        pygame.draw.rect(self.screen, (35, 35, 35), RELOAD_BUTTON, 2)
        self.text("Reload Map (F5)", (RELOAD_BUTTON.x + 37, RELOAD_BUTTON.y + 8), self.small_font, (25, 25, 25))

        pygame.draw.rect(self.screen, (34, 40, 45), MESSAGE_BAR)
        pygame.draw.rect(self.screen, (188, 170, 105), MESSAGE_BAR, 2)
        previous_clip = self.screen.get_clip()
        self.screen.set_clip(MESSAGE_BAR.inflate(-8, -2))
        work_status = self.active_work_progress()
        if work_status is None:
            self.text(
                self.messages[-1],
                (MESSAGE_BAR.x + 10, MESSAGE_BAR.y + 6),
                self.small_font,
            )
        else:
            action, progress = work_status
            label_width = min(330, MESSAGE_BAR.width // 3)
            self.text(
                action,
                (MESSAGE_BAR.x + 10, MESSAGE_BAR.y + 6),
                self.small_font,
            )
            progress_rect = pygame.Rect(
                MESSAGE_BAR.x + label_width,
                MESSAGE_BAR.y + 6,
                MESSAGE_BAR.width - label_width - 58,
                MESSAGE_BAR.height - 12,
            )
            pygame.draw.rect(self.screen, (69, 74, 77), progress_rect)
            fill_rect = progress_rect.copy()
            fill_rect.width = round(progress_rect.width * progress)
            pygame.draw.rect(self.screen, (205, 194, 126), fill_rect)
            pygame.draw.rect(self.screen, (188, 170, 105), progress_rect, 1)
            self.text(
                f"{round(progress * 100)}%",
                (progress_rect.right + 8, MESSAGE_BAR.y + 6),
                self.small_font,
            )
        self.screen.set_clip(previous_clip)

    def draw_compact_command_menu(self) -> None:
        """Draw the nested main command menu over the legacy flat layout."""
        panel = LEFT_COMMAND_RECT
        pygame.draw.rect(self.screen, (25, 28, 31), panel)
        pygame.draw.rect(self.screen, (92, 95, 101), panel, 2)
        self.text("Command Menu", (panel.x + 12, panel.y + 12), self.font)
        self.action_buttons.clear()
        self.command_buttons.clear()
        self.command_category_buttons.clear()
        self.area_quantity_buttons.clear()
        self.target_selection_buttons.clear()
        self.macro_command_buttons.clear()
        self.main_macro_dropdown_buttons.clear()

        y = panel.y + 44
        if self.selected_id is not None:
            obj = self.objects[self.selected_id]
            actions = object_action_menu_options(obj, self.player) if self.day.mode is Mode.DIRECT else []
            if actions:
                self.text("Selection Actions", (panel.x + 12, y), self.small_font, (172, 176, 182))
                y += 24
                for index, action in enumerate(actions):
                    rect = pygame.Rect(panel.x + 12, y, panel.width - 24, 32)
                    disabled = not action_option_enabled(action)
                    pygame.draw.rect(self.screen, (95, 96, 92) if disabled else (223, 220, 196), rect)
                    pygame.draw.rect(self.screen, (75, 76, 73) if disabled else (30, 30, 30), rect, 2)
                    self.text(f"{index + 1}. {action}", (rect.x + 8, rect.y + 7), self.small_font, (145, 145, 138) if disabled else (25, 25, 25))
                    self.action_buttons.append((rect, action))
                    y += 38
                y += 8

        title = "Commands" if self.active_command_category is None else self.active_command_category
        self.text(title, (panel.x + 12, y), self.small_font, (172, 176, 182))
        y += 25
        if self.active_command_category is None:
            for index, category in enumerate(AREA_COMMAND_CATEGORIES):
                rect = pygame.Rect(panel.x + 12, y, panel.width - 24, 30)
                pygame.draw.rect(self.screen, (103, 105, 94), rect)
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 2)
                self.text(f"{index + 1}. {category}", (rect.x + 8, rect.y + 7), self.small_font, (230, 230, 218))
                self.command_category_buttons.append((rect, category))
                y += 35
            return

        if False and self.active_command_category == "Routines":
            current_name = self.memory_file_name or "Select routine…"
            current_status = self.routine_status(self.day.remembered_routine)
            dropdown = pygame.Rect(panel.x + 12, y, panel.width - 142, 30)
            play = pygame.Rect(dropdown.right + 8, y, 110, 30)
            pygame.draw.rect(self.screen, (218, 213, 190), dropdown)
            pygame.draw.rect(self.screen, (92, 95, 101), dropdown, 2)
            self.text(
                current_name,
                (dropdown.x + 8, dropdown.y + 7),
                self.small_font,
                self.routine_status_color(current_status, (35, 35, 35)),
            )
            arrow = self.small_font.render("▲" if self.macro_dropdown_open else "▼", True, (35, 35, 35))
            self.screen.blit(arrow, arrow.get_rect(center=(dropdown.right - 16, dropdown.centery)))
            pygame.draw.rect(self.screen, (174, 169, 145), play)
            pygame.draw.rect(self.screen, (35, 35, 35), play, 1)
            rendered = self.small_font.render("Play", True, (25, 25, 25))
            self.screen.blit(rendered, rendered.get_rect(center=play.center))
            self.macro_command_buttons.extend(((dropdown, "Macro Dropdown"), (play, "Play")))
            y += 38
            width = (panel.width - 36) // 3
            for index, action in enumerate(("Record", "Stop", "Edit")):
                rect = pygame.Rect(panel.x + 12 + index * (width + 6), y, width, 28)
                enabled = action == "Edit" or (action == "Record" and not self.macro_recording) or (action == "Stop" and self.macro_recording)
                pygame.draw.rect(self.screen, (174, 169, 145) if enabled else (75, 78, 78), rect)
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
                rendered = self.small_font.render(action, True, (25, 25, 25) if enabled else (145, 145, 138))
                self.screen.blit(rendered, rendered.get_rect(center=rect.center))
                if enabled:
                    self.macro_command_buttons.append((rect, action))
            y += 36
            toggle = pygame.Rect(panel.x + 12, y, panel.width - 24, 28)
            pygame.draw.rect(self.screen, (205, 194, 126) if self.routine_moves_expanded else (103, 105, 94), toggle)
            pygame.draw.rect(self.screen, (35, 35, 35), toggle, 1)
            toggle_label = f"Moves on Right (R): {'On' if self.routine_moves_expanded else 'Off'}"
            rendered = self.small_font.render(toggle_label, True, (25, 25, 25))
            self.screen.blit(rendered, rendered.get_rect(center=toggle.center))
            self.macro_command_buttons.append((toggle, "Toggle Moves"))
            y += 36
            self.text("Moves", (panel.x + 12, y), self.small_font, (172, 176, 182))
            y += 22
            for move_index, step in enumerate(self.day.remembered_routine):
                if y + 22 > panel.bottom - 38:
                    break
                prefix = "▶" if self.day.mode is Mode.REPLAY and move_index == self.day.replay_index else " "
                move_color = self.routine_status_color(
                    self.routine_step_status(step), (220, 222, 214)
                )
                indent = min(18 * self.routine_step_depth(move_index), 108)
                label = self.routine_step_display_label(move_index)
                self.text(f"{prefix} {label}", (panel.x + 18 + indent, y), self.small_font, move_color)
                y += 22
            back = pygame.Rect(panel.x + 12, y, panel.width - 24, 30)
            pygame.draw.rect(self.screen, (174, 169, 145), back)
            pygame.draw.rect(self.screen, (35, 35, 35), back, 2)
            self.text("Back", (back.x + 8, back.y + 7), self.small_font, (25, 25, 25))
            self.command_buttons.append((back, "Back"))
            self.draw_main_macro_dropdown(dropdown)
            return

        self.text("Quantity", (panel.x + 12, y + 5), self.small_font, (172, 176, 182))
        for x, label, change in ((panel.x + 112, "-", -1), (panel.x + 160, "+", 1)):
            rect = pygame.Rect(x, y, 22, 24)
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
            rendered = self.small_font.render(label, True, (25, 25, 25))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.area_quantity_buttons.append((rect, change))
        value = self.small_font.render(str(self.area_command_quantity), True, (225, 225, 214))
        self.screen.blit(value, value.get_rect(center=(panel.x + 147, y + 12)))
        if self.active_command_category in AREA_COMMAND_CATEGORIES:
            self.text("Target", (panel.x + 198, y + 5), self.small_font, (172, 176, 182))
            for rect, label, mode in (
                (pygame.Rect(panel.x + 246, y, 68, 24), "Nearest", "nearest"),
                (pygame.Rect(panel.x + 318, y, 58, 24), "Target", "target"),
                (pygame.Rect(panel.x + 380, y, 56, 24), "Area", "area"),
            ):
                selected_mode = self.target_selection_mode == mode
                pygame.draw.rect(
                    self.screen,
                    (205, 194, 126) if selected_mode else (103, 105, 94),
                    rect,
                )
                pygame.draw.rect(self.screen, (35, 35, 35), rect, 1)
                rendered = self.small_font.render(label, True, (25, 25, 25))
                self.screen.blit(rendered, rendered.get_rect(center=rect.center))
                self.target_selection_buttons.append((rect, mode))
        y += 32
        if self.active_command is not None and self.target_selection_mode in {"target", "area"}:
            self.text(
                self.target_selection_summary(),
                (panel.x + 198, y - 3),
                self.small_font,
                (225, 225, 214),
            )
            y += 22
        commands = [*area_commands_for_category(self.player, self.active_command_category), "Back"]
        for index, command in enumerate(commands):
            rect = pygame.Rect(panel.x + 12, y, panel.width - 24, 27)
            pygame.draw.rect(self.screen, (205, 194, 126) if command == self.active_command else (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (35, 35, 35), rect, 2)
            self.text(f"{index + 1}. {command}", (rect.x + 7, rect.y + 6), self.small_font, (25, 25, 25))
            self.command_buttons.append((rect, command))
            y += 30

    def draw_routine_bar(self) -> None:
        panel = LEFT_ROUTINE_RECT
        pygame.draw.rect(self.screen, (31, 35, 38), panel)
        pygame.draw.rect(self.screen, (92, 95, 101), panel, 2)
        self.macro_command_buttons.clear()
        self.main_macro_dropdown_buttons.clear()
        dropdown = pygame.Rect(panel.x + 10, panel.y + 10, panel.width - 102, 30)
        play = pygame.Rect(dropdown.right + 6, dropdown.y, 76, 30)
        manager = pygame.Rect(panel.x + 10, panel.y + 50, panel.width - 20, 28)
        current_name = self.memory_file_name or "Select routine..."
        pygame.draw.rect(self.screen, (218, 213, 190), dropdown)
        pygame.draw.rect(self.screen, (92, 95, 101), dropdown, 2)
        self.text(current_name, (dropdown.x + 8, dropdown.y + 7), self.small_font, (35, 35, 35))
        arrow = self.small_font.render("^" if self.macro_dropdown_open else "v", True, (35, 35, 35))
        self.screen.blit(arrow, arrow.get_rect(center=(dropdown.right - 14, dropdown.centery)))
        play_label = "Stop" if self.macro_recording else "Play"
        pygame.draw.rect(self.screen, (174, 169, 145), play)
        pygame.draw.rect(self.screen, (35, 35, 35), play, 1)
        rendered = self.small_font.render(play_label, True, (25, 25, 25))
        self.screen.blit(rendered, rendered.get_rect(center=play.center))
        pygame.draw.rect(self.screen, (103, 105, 94), manager)
        pygame.draw.rect(self.screen, (35, 35, 35), manager, 1)
        manager_label = (
            f"Recording: {current_name} - Routine Manager"
            if self.macro_recording
            else "Routine Manager"
        )
        rendered = self.small_font.render(manager_label, True, (230, 230, 218))
        self.screen.blit(rendered, rendered.get_rect(center=manager.center))
        self.macro_command_buttons.extend(
            ((dropdown, "Macro Dropdown"), (play, play_label), (manager, "Routine Manager"))
        )
        self.draw_main_macro_dropdown(dropdown)

    def draw_routine_moves_dock(self) -> None:
        panel = RIGHT_DOCK_RECT
        pygame.draw.rect(self.screen, (25, 28, 31), panel)
        pygame.draw.rect(self.screen, (92, 95, 101), panel, 2)
        self.text("Routine Moves", (panel.x + 14, panel.y + 14), self.font)
        name = self.memory_file_name or "Unnamed routine"
        self.text(name, (panel.x + 14, panel.y + 48), self.small_font, (172, 176, 182))
        self.text("Press R to restore player panels", (panel.x + 14, panel.y + 74), self.small_font, (172, 176, 182))
        y = panel.y + 110
        for index, step in enumerate(self.day.remembered_routine):
            if y + 34 > panel.bottom - 12:
                break
            row = pygame.Rect(panel.x + 10, y, panel.width - 20, 30)
            active = self.day.mode is Mode.REPLAY and index == self.day.replay_index
            pygame.draw.rect(self.screen, (116, 111, 83) if active else (47, 52, 55), row)
            pygame.draw.rect(self.screen, (188, 170, 105) if active else (92, 95, 101), row, 2 if active else 1)
            move_color = self.routine_status_color(
                self.routine_step_status(step), (235, 235, 225)
            )
            indent = min(18 * self.routine_step_depth(index), 108)
            self.text(self.routine_step_display_label(index), (row.x + 9 + indent, row.y + 7), self.small_font, move_color)
            y += 35

    def draw_main_macro_dropdown(self, dropdown: pygame.Rect) -> None:
        self.main_macro_dropdown_buttons.clear()
        if not self.macro_dropdown_open:
            return
        entries = [("History", 0.0), ("__new__", 0.0), *sorted(
            self.available_command_sets(), key=lambda item: item[0].lower()
        )]
        for index, (name, _modified) in enumerate(entries):
            option = pygame.Rect(dropdown.x, dropdown.bottom + index * 30, dropdown.width, 30)
            if option.bottom > LEFT_MESSAGE_HISTORY_RECT.top:
                break
            pygame.draw.rect(self.screen, (234, 229, 207), option)
            pygame.draw.rect(self.screen, (92, 95, 101), option, 1)
            label = "New…" if name == "__new__" else name
            label_color = (
                (35, 35, 35)
                if name in {"__new__", "History"}
                else self.routine_status_color(
                    self.named_routine_status(name), (35, 35, 35)
                )
            )
            self.text(label, (option.x + 8, option.y + 7), self.small_font, label_color)
            self.main_macro_dropdown_buttons.append((option, name))

    def draw_message_history(self, panel: pygame.Rect = LEFT_MESSAGE_HISTORY_RECT) -> None:
        pygame.draw.rect(self.screen, (31, 35, 38), panel)
        pygame.draw.rect(self.screen, (92, 95, 101), panel, 2)
        self.text(
            "Messages",
            (panel.x + 12, panel.y + 10),
            self.small_font,
            (172, 176, 182),
        )

        content = pygame.Rect(
            panel.x + 12,
            panel.y + 38,
            panel.width - 32,
            panel.height - 50,
        )
        line_height = self.small_font.get_linesize() + 2
        visible_line_count = max(1, content.height // line_height)
        history_lines: list[str] = []
        for message in self.messages:
            history_lines.extend(
                wrap_text(message, self.small_font, content.width)
            )

        maximum_offset = max(0, len(history_lines) - visible_line_count)
        self.message_scroll_offset = min(
            self.message_scroll_offset, maximum_offset
        )
        end = len(history_lines) - self.message_scroll_offset
        start = max(0, end - visible_line_count)

        previous_clip = self.screen.get_clip()
        self.screen.set_clip(content)
        for index, line in enumerate(history_lines[start:end]):
            self.text(
                line,
                (content.x, content.y + index * line_height),
                self.small_font,
                (220, 222, 214),
            )
        self.screen.set_clip(previous_clip)

        if maximum_offset:
            track = pygame.Rect(panel.right - 12, content.y, 5, content.height)
            pygame.draw.rect(self.screen, (57, 63, 67), track)
            thumb_height = max(
                24,
                round(
                    track.height
                    * visible_line_count
                    / max(visible_line_count, len(history_lines))
                ),
            )
            scroll_fraction = self.message_scroll_offset / maximum_offset
            thumb_y = track.bottom - thumb_height - round(
                (track.height - thumb_height) * scroll_fraction
            )
            pygame.draw.rect(
                self.screen,
                (174, 169, 145),
                (track.x, thumb_y, track.width, thumb_height),
            )

    def morning_button_rects(self) -> list[tuple[pygame.Rect, str]]:
        return [(rect, action) for rect, action, _ in self.day_plan_buttons if action in {"start", "editor"}]

    def draw_morning_menu(self) -> None:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 155))
        self.screen.blit(overlay, (0, 0))
        panel = pygame.Rect(185, 90, WIDTH - 370, HEIGHT - 180)
        pygame.draw.rect(self.screen, (232, 227, 205), panel)
        pygame.draw.rect(self.screen, (43, 43, 43), panel, 4)
        self.text(f"Day Planner — Day {self.day.number}", (panel.x + 28, panel.y + 20), self.title_font, (35, 35, 35))
        left = pygame.Rect(panel.x + 24, panel.y + 78, 890, panel.height - 112)
        right = pygame.Rect(left.right + 18, left.y, panel.right - left.right - 42, left.height)
        for box in (left, right):
            pygame.draw.rect(self.screen, (218, 213, 190), box)
            pygame.draw.rect(self.screen, (90, 88, 78), box, 2)
        self.text("Selected Activities", (left.x + 14, left.y + 12), self.font, (35, 35, 35))
        self.text("Add to the Day", (right.x + 14, right.y + 12), self.font, (35, 35, 35))
        self.day_plan_rows.clear()
        self.day_plan_buttons.clear()
        cursor_y = left.y + 54
        for index, activity in enumerate(self.day_plan):
            nested_height = 36 * len(activity.children) + (34 if activity.kind == "conditional" else 0)
            row_height = 46 + nested_height
            y = cursor_y
            if y + row_height > left.bottom - 12:
                break
            row = pygame.Rect(left.x + 12, y, left.width - 24, row_height)
            pygame.draw.rect(self.screen, (234, 229, 207), row)
            selected_conditional = activity.kind == "conditional" and index == self.selected_conditional_index
            pygame.draw.rect(self.screen, (76, 105, 68) if selected_conditional else (110, 106, 92), row, 3 if selected_conditional else 1)
            detail = " — 11:00 PM" if activity.scheduled_minutes == 23 * 60 else ""
            self.text(f"{index + 1}. {activity.label}{detail}", (row.x + 12, row.y + 13), self.font, (35, 35, 35))
            for button_index, (symbol, action) in enumerate((("↑", "up"), ("↓", "down"), ("×", "delete"))):
                button = pygame.Rect(row.right - 102 + button_index * 32, row.y + 8, 27, 29)
                pygame.draw.rect(self.screen, (174, 169, 145), button)
                pygame.draw.rect(self.screen, (55, 55, 50), button, 1)
                rendered = self.small_font.render(symbol, True, (35, 35, 35))
                self.screen.blit(rendered, rendered.get_rect(center=button.center))
                self.day_plan_buttons.append((button, action, index))
            self.day_plan_rows.append((row, index))
            if activity.kind == "conditional":
                select_rect = pygame.Rect(row.x + 6, row.y + 5, row.width - 118, 36)
                self.day_plan_buttons.append((select_rect, "select_conditional", index))
                for child_index, child in enumerate(activity.children):
                    child_y = row.y + 46 + child_index * 36
                    child_rect = pygame.Rect(row.x + 38, child_y, row.width - 82, 30)
                    pygame.draw.rect(self.screen, (214, 210, 190), child_rect)
                    pygame.draw.rect(self.screen, (125, 120, 104), child_rect, 1)
                    self.text(f"↳ {child.label}", (child_rect.x + 8, child_rect.y + 7), self.small_font, (45, 45, 40))
                    remove = pygame.Rect(child_rect.right + 7, child_rect.y + 2, 27, 26)
                    pygame.draw.rect(self.screen, (174, 169, 145), remove)
                    pygame.draw.rect(self.screen, (55, 55, 50), remove, 1)
                    rendered = self.small_font.render("×", True, (35, 35, 35))
                    self.screen.blit(rendered, rendered.get_rect(center=remove.center))
                    self.day_plan_buttons.append((remove, f"remove_slot:{child_index}", index))
                add_slot = pygame.Rect(row.x + 38, row.bottom - 30, 150, 24)
                pygame.draw.rect(self.screen, (174, 169, 145), add_slot)
                pygame.draw.rect(self.screen, (55, 55, 50), add_slot, 1)
                self.text("+ Command Slot", (add_slot.x + 9, add_slot.y + 4), self.small_font, (35, 35, 35))
                self.day_plan_buttons.append((add_slot, "add_slot", index))
            cursor_y = row.bottom + 8
        for option_index, (label, kind) in enumerate((("Explore", "explore"), ("Sleep", "sleep"), ("Power Nap", "power_nap"), ("Break", "break"))):
            column = option_index % 2
            row_index = option_index // 2
            rect = pygame.Rect(right.x + 12 + column * ((right.width - 30) // 2 + 6), right.y + 48 + row_index * 34, (right.width - 30) // 2, 28)
            pygame.draw.rect(self.screen, (184, 179, 154), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            self.text(f"+ {label}", (rect.x + 8, rect.y + 6), self.small_font, (35, 35, 35))
            self.day_plan_buttons.append((rect, f"add_activity:{kind}", None))
        self.text("Saved Routines", (right.x + 14, right.y + 119), self.small_font, (70, 70, 65))
        routines = sorted(self.available_command_sets(), key=lambda item: item[0].lower())
        for index, (name, _modified) in enumerate(routines):
            y = right.y + 142 + index * 34
            if y + 30 > right.bottom - 164:
                break
            rect = pygame.Rect(right.x + 12, y, right.width - 24, 30)
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            self.text(f"+ {name}", (rect.x + 10, rect.y + 8), self.small_font, (35, 35, 35))
            self.day_plan_buttons.append((rect, "add_macro", index))
        if not routines:
            self.text("No saved routines yet.", (right.x + 16, right.y + 146), self.small_font, (80, 78, 70))
        conditional = pygame.Rect(right.x + 12, right.bottom - 150, right.width - 24, 36)
        pygame.draw.rect(self.screen, (174, 169, 145), conditional)
        pygame.draw.rect(self.screen, (43, 43, 43), conditional, 1)
        rendered = self.small_font.render("+ Add Conditional", True, (35, 35, 35))
        self.screen.blit(rendered, rendered.get_rect(center=conditional.center))
        self.day_plan_buttons.append((conditional, "add_conditional", None))
        editor = pygame.Rect(right.x + 12, right.bottom - 104, right.width - 24, 38)
        start = pygame.Rect(right.x + 12, right.bottom - 56, right.width - 24, 42)
        for rect, label, action, color in ((editor, "Routine Manager", "editor", (174, 169, 145)), (start, "Start Day", "start", (195, 194, 157))):
            pygame.draw.rect(self.screen, color, rect)
            pygame.draw.rect(self.screen, (43, 43, 43), rect, 2)
            rendered = self.font.render(label, True, (35, 35, 35))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.day_plan_buttons.append((rect, action, None))

    def draw_legacy_morning_menu(self) -> None:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 155))
        self.screen.blit(overlay, (0, 0))
        panel = pygame.Rect(285 + SIDEBAR_WIDTH, 90, 530, 575)
        pygame.draw.rect(self.screen, (232, 227, 205), panel)
        pygame.draw.rect(self.screen, (43, 43, 43), panel, 4)
        self.text(f"Morning — Day {self.day.number}", (390 + SIDEBAR_WIDTH, 120), self.title_font, (35, 35, 35))
        self.text("Choose with mouse, arrows, Enter, or Space", (375 + SIDEBAR_WIDTH, 175), self.small_font, (35, 35, 35))
        for index, (rect, label) in enumerate(self.morning_button_rects()):
            hovered = rect.collidepoint(pygame.mouse.get_pos())
            selected = index == self.menu_index
            color = (195, 194, 157) if hovered or selected else (213, 209, 183)
            pygame.draw.rect(self.screen, color, rect)
            pygame.draw.rect(self.screen, (35, 35, 35), rect, 3 if selected else 1)
            self.text(f"{index + 1}. {label}", (rect.x + 22, rect.y + 13), self.font, (35, 35, 35))
        routine_count = len(self.day.remembered_routine)
        self.text(f"Routine Memory: {routine_count} orders", (420 + SIDEBAR_WIDTH, 625), self.small_font, (35, 35, 35))

    def draw_memory_editor(self) -> None:
        map_snapshot = self.screen.subsurface(MAP_VIEWPORT).copy()
        preview_screen = self.camera.world_to_screen(
            self.command_editor_preview_position()
        )
        self.memory_editor_map_camera = (
            self.camera.x,
            self.camera.y,
            self.camera.effective_zoom,
        )
        if self._editor_camera_restore is not None:
            self.camera.x, self.camera.y, self.camera.zoom = self._editor_camera_restore
            self._editor_camera_restore = None
        self.screen.fill((25, 28, 31))
        scaled_map = pygame.transform.smoothscale(
            map_snapshot, COMMAND_EDITOR_MAP_RECT.size
        )
        self.screen.blit(scaled_map, COMMAND_EDITOR_MAP_RECT.topleft)
        preview_x = COMMAND_EDITOR_MAP_RECT.x + round(
            (preview_screen[0] - MAP_VIEWPORT.x)
            * COMMAND_EDITOR_MAP_RECT.width
            / MAP_VIEWPORT.width
        )
        preview_y = COMMAND_EDITOR_MAP_RECT.y + round(
            (preview_screen[1] - MAP_VIEWPORT.y)
            * COMMAND_EDITOR_MAP_RECT.height
            / MAP_VIEWPORT.height
        )
        self.draw_command_editor_preview_avatar((preview_x, preview_y))
        pygame.draw.rect(self.screen, (188, 170, 105), COMMAND_EDITOR_MAP_RECT, 2)
        if self.day.remembered_routine:
            step = self.day.remembered_routine[self.memory_edit_index]
            if step.action == "Move To":
                hint = (
                    "Click the map to choose the new destination"
                    if self.memory_map_field_selection == "target_point"
                    else "Target point: click the field, then click the map"
                )
                self.text(
                    hint,
                    (COMMAND_EDITOR_MAP_RECT.x + 10, COMMAND_EDITOR_MAP_RECT.bottom - 24),
                    self.small_font,
                    (255, 245, 190),
                )
            elif step.action == "Harvest and Eat Berries":
                mode = routine_field_editor_value(step, "target_mode")
                hint = {
                    "nearest": "Target: nearest available berry bush",
                    "area": "Target: drag an area on the map",
                    "specific": "Target: click a berry bush on the map",
                }[mode]
                self.text(
                    hint,
                    (COMMAND_EDITOR_MAP_RECT.x + 10, COMMAND_EDITOR_MAP_RECT.bottom - 24),
                    self.small_font,
                    (255, 245, 190),
                )
        self.draw_message_history(COMMAND_EDITOR_MESSAGES_RECT)

        panel = COMMAND_EDITOR_RECT.inflate(-24, -24)
        pygame.draw.rect(self.screen, (232, 227, 205), panel)
        pygame.draw.rect(self.screen, (43, 43, 43), panel, 4)
        self.text("Routine Manager", (panel.x + 28, panel.y + 18), self.title_font, (35, 35, 35))
        self.text(
            "C/Esc closes · Enter saves a field · Values use JSON syntax.",
            (panel.x + 360, panel.y + 31),
            self.small_font,
            (35, 35, 35),
        )
        routine = self.day.remembered_routine
        self.memory_editor_rows.clear()
        self.memory_editor_buttons.clear()
        self.text("Routine name", (panel.x + 24, panel.y + 62), self.small_font, (35, 35, 35))
        self.memory_file_name_rect = pygame.Rect(panel.x + 120, panel.y + 54, 146, 30)
        editing_name = self.memory_edit_field == "__memory_file_name__"
        pygame.draw.rect(
            self.screen,
            (255, 250, 225) if editing_name else (218, 213, 190),
            self.memory_file_name_rect,
        )
        pygame.draw.rect(self.screen, (76, 105, 68), self.memory_file_name_rect, 2)
        shown_name = self.memory_edit_buffer if editing_name else self.memory_file_name
        self.text(
            shown_name,
            (self.memory_file_name_rect.x + 6, self.memory_file_name_rect.y + 6),
            self.small_font,
            (35, 35, 35),
        )
        next_button_x = self.memory_file_name_rect.right + 8
        for label, action in [
            ("New Routine", "New Set"),
            ("Save", "Save"),
            ("Load", "Load"),
            ("Run Now", "Run Now"),
            ("Record Routine", "Record Routine"),
        ]:
            width = (
                100
                if label == "Record Routine"
                else 84
                if action in {"New Set", "Run Now"}
                else 72
            )
            x = next_button_x
            rect = pygame.Rect(x, panel.y + 54, width, 30)
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            rendered = self.small_font.render(label, True, (35, 35, 35))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.memory_editor_buttons.append((rect, action))
            next_button_x = rect.right + 8

        for index, label in enumerate(("Duplicate Routine", "Rename Routine")):
            rect = pygame.Rect(panel.x + 24 + index * 142, panel.y + 90, 134, 28)
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            rendered = self.small_font.render(label, True, (35, 35, 35))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.memory_editor_buttons.append((rect, label))

        list_rect = pygame.Rect(panel.x + 24, panel.y + 126, 330, panel.height - 194)
        pygame.draw.rect(self.screen, (218, 213, 190), list_rect)
        visible_count = 12
        start = max(0, min(self.memory_edit_index - visible_count // 2, len(routine) - visible_count))
        row_y = list_rect.y + 8
        for routine_index in range(start, min(len(routine), start + visible_count)):
            step = routine[routine_index]
            if row_y + 32 > list_rect.bottom - 8:
                break
            depth = self.routine_step_depth(routine_index)
            indent = min(24 * depth, list_rect.width // 2)
            rect = pygame.Rect(list_rect.x + 8 + indent, row_y, list_rect.width - 16 - indent, 32)
            selected = routine_index == self.memory_edit_index
            pygame.draw.rect(self.screen, (195, 194, 157) if selected else (213, 209, 183), rect)
            next_command = (
                self.day.replay_index < len(routine)
                and routine_index == self.day.replay_index
            )
            pygame.draw.rect(
                self.screen,
                (56, 112, 62) if next_command else (55, 55, 50),
                rect,
                4 if next_command else 2 if selected else 1,
            )
            label = self.routine_step_display_label(routine_index)
            label_color = (
                (92, 67, 38)
                if step.action in ROUTINE_CONTROL_ACTIONS
                else (35, 35, 35)
            )
            self.text(label, (rect.x + 10, rect.y + 8), self.small_font, label_color)
            self.memory_editor_rows.append((rect, routine_index))
            row_y += 38
        if not routine:
            self.text("No remembered commands.", (list_rect.x + 18, list_rect.y + 18), self.font, (70, 70, 65))

        self.memory_editor_fields.clear()
        self.memory_field_dropdown_buttons.clear()
        if routine:
            step = routine[self.memory_edit_index]
            field_x = list_rect.right + 24
            field_width = panel.right - field_x - 24
            if step.action == ROUTINE_REFERENCE_ACTION:
                expand = pygame.Rect(field_x, panel.y + 126, 180, 32)
                pygame.draw.rect(self.screen, (174, 169, 145), expand)
                pygame.draw.rect(self.screen, (55, 55, 50), expand, 1)
                rendered = self.small_font.render("Expand Routine", True, (35, 35, 35))
                self.screen.blit(rendered, rendered.get_rect(center=expand.center))
                self.memory_editor_buttons.append((expand, "Expand Routine"))
            for index, field_name in enumerate(routine_step_editable_fields(step)):
                y = panel.y + 126 + index * 38
                value_rect = pygame.Rect(field_x + 164, y, field_width - 164, 32)
                field_label = {
                    "target_mode": "Target",
                    "target_point": "Target point",
                    "area_bounds": "Target area",
                }.get(field_name, field_name)
                self.text(field_label, (field_x, y + 7), self.small_font, (35, 35, 35))
                active = (
                    field_name == self.memory_edit_field
                    or field_name == self.memory_map_field_selection
                )
                pygame.draw.rect(
                    self.screen,
                    (255, 250, 225) if active else (218, 213, 190),
                    value_rect,
                )
                pygame.draw.rect(
                    self.screen,
                    (76, 105, 68) if active else (90, 88, 78),
                    value_rect,
                    2,
                )
                choices = self.memory_field_choices(field_name)
                raw_value = routine_field_editor_value(step, field_name)
                if field_name == self.memory_map_field_selection:
                    value_text = "Click a destination on the map…"
                elif active:
                    value_text = self.memory_edit_buffer
                elif choices is not None:
                    value_text = next(
                        (label for label, value in choices if value == raw_value),
                        str(raw_value),
                    )
                else:
                    value_text = json.dumps(raw_value)
                while value_text and self.small_font.size(value_text)[0] > value_rect.width - 12:
                    value_text = "…" + value_text[2:]
                self.text(value_text, (value_rect.x + 6, value_rect.y + 7), self.small_font, (35, 35, 35))
                if choices is not None:
                    arrow = self.small_font.render(
                        "▲" if self.memory_field_dropdown_open == field_name else "▼",
                        True,
                        (35, 35, 35),
                    )
                    self.screen.blit(
                        arrow,
                        arrow.get_rect(center=(value_rect.right - 14, value_rect.centery)),
                    )
                self.memory_editor_fields.append((value_rect, field_name))
        labels = [
            "Move Up",
            "Move Down",
            "Remove",
            "Add Command",
            "Add Conditional",
            "Add Loop",
            "Done",
        ]
        button_width = (panel.width - 48 - (len(labels) - 1) * 8) // len(labels)
        for index, label in enumerate(labels):
            rect = pygame.Rect(
                panel.x + 24 + index * (button_width + 8),
                panel.bottom - 50,
                button_width,
                32,
            )
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            rendered = self.small_font.render(label, True, (35, 35, 35))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.memory_editor_buttons.append((rect, label))

        if routine and self.memory_field_dropdown_open is not None:
            self.draw_memory_field_dropdown(panel)

        if self.memory_browser_open:
            self.draw_memory_browser(panel)

    def draw_memory_field_dropdown(self, panel: pygame.Rect) -> None:
        field_name = self.memory_field_dropdown_open
        if field_name is None:
            return
        field_rect = next(
            (rect for rect, name in self.memory_editor_fields if name == field_name),
            None,
        )
        choices = self.memory_field_choices(field_name)
        if field_rect is None or choices is None:
            self.memory_field_dropdown_open = None
            return
        max_rows = max(1, (panel.bottom - field_rect.bottom - 12) // 28)
        column_count = min(2, max(1, math.ceil(len(choices) / max_rows)))
        option_width = field_rect.width // column_count
        for index, (label, value) in enumerate(choices[: max_rows * column_count]):
            column = index // max_rows
            row = index % max_rows
            option = pygame.Rect(
                field_rect.x + column * option_width,
                field_rect.bottom + row * 28,
                option_width,
                28,
            )
            pygame.draw.rect(self.screen, (238, 233, 211), option)
            pygame.draw.rect(self.screen, (90, 88, 78), option, 1)
            self.text(label, (option.x + 7, option.y + 6), self.small_font, (35, 35, 35))
            self.memory_field_dropdown_buttons.append((option, field_name, value))

    def command_editor_preview_position(self) -> tuple[float, float]:
        position = (float(self.player.x), float(self.player.y))
        routine = self.day.remembered_routine
        if not routine:
            return position
        for step in routine[: min(self.memory_edit_index + 1, len(routine))]:
            candidate: tuple[float, float] | None = None
            if step.target_point is not None:
                candidate = step.target_point
            elif step.target_id is not None and step.target_id in self.objects:
                candidate = self.objects[step.target_id].center
            else:
                bounds = (
                    step.target_areas[-1]
                    if step.target_areas
                    else step.area_bounds
                )
                if bounds is not None:
                    candidate = (
                        (bounds[0] + bounds[2]) / 2,
                        (bounds[1] + bounds[3]) / 2,
                    )
            if candidate is not None:
                position = (float(candidate[0]), float(candidate[1]))
        return position

    def draw_command_editor_preview_avatar(
        self, position: tuple[int, int]
    ) -> None:
        radius = 12
        pygame.draw.circle(self.screen, (255, 220, 120), position, radius + 2)
        pygame.draw.circle(self.screen, (222, 214, 187), position, radius)
        pygame.draw.circle(self.screen, (35, 35, 35), position, radius, 2)
        pygame.draw.circle(self.screen, (35, 35, 35), (position[0] - 4, position[1] - 3), 2)
        pygame.draw.circle(self.screen, (35, 35, 35), (position[0] + 4, position[1] - 3), 2)

    def draw_memory_browser(self, editor_panel: pygame.Rect) -> None:
        dialog = editor_panel.inflate(-24, -150)
        dialog.top = editor_panel.top + 112
        pygame.draw.rect(self.screen, (238, 233, 211), dialog)
        pygame.draw.rect(self.screen, (43, 43, 43), dialog, 4)
        self.text("Load Command Set", (dialog.x + 18, dialog.y + 14), self.font, (35, 35, 35))
        self.text(
            "Click a name to load · click the star to toggle favorite · Esc closes",
            (dialog.x + 210, dialog.y + 18),
            self.small_font,
            (70, 70, 65),
        )

        entries = self.available_command_sets()
        by_name = sorted(entries, key=lambda item: item[0].lower())
        favorites = [item for item in by_name if item[0] in self.memory_favorites]
        recent = sorted(entries, key=lambda item: item[1], reverse=True)
        columns = (("Favorites", favorites), ("Recent", recent), ("All", by_name))
        gap = 12
        column_width = (dialog.width - 36 - gap * 2) // 3
        top = dialog.y + 54
        self.memory_browser_rows.clear()
        self.memory_favorite_buttons.clear()
        for column_index, (title, items) in enumerate(columns):
            x = dialog.x + 12 + column_index * (column_width + gap)
            column = pygame.Rect(x, top, column_width, dialog.bottom - top - 12)
            pygame.draw.rect(self.screen, (218, 213, 190), column)
            pygame.draw.rect(self.screen, (90, 88, 78), column, 1)
            self.text(title, (x + 10, top + 9), self.small_font, (35, 35, 35))
            for row_index, (name, modified) in enumerate(items):
                y = top + 36 + row_index * 30
                if y + 26 > column.bottom:
                    break
                row = pygame.Rect(x + 6, y, column_width - 12, 26)
                pygame.draw.rect(self.screen, (231, 226, 203), row)
                pygame.draw.rect(self.screen, (120, 116, 101), row, 1)
                favorite_rect = pygame.Rect(row.right - 27, row.y + 2, 23, 22)
                name_rect = pygame.Rect(row.x, row.y, row.width - 30, row.height)
                label = name
                if title == "Recent":
                    label += "  " + datetime.fromtimestamp(modified).strftime("%b %d")
                while label and self.small_font.size(label)[0] > name_rect.width - 12:
                    label = label[:-2] + "…"
                self.text(label, (row.x + 7, row.y + 5), self.small_font, (35, 35, 35))
                star = "★" if name in self.memory_favorites else "☆"
                rendered = self.small_font.render(star, True, (115, 88, 25))
                self.screen.blit(rendered, rendered.get_rect(center=favorite_rect.center))
                self.memory_browser_rows.append((name_rect, name))
                self.memory_favorite_buttons.append((favorite_rect, name))

    def draw_context_menu(self) -> None:
        if self.context_menu_pos is None:
            return
        self.context_menu.clear()
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 60))
        self.screen.blit(overlay, (0, 0))
        panel_x, panel_y = self.context_menu_pos
        panel_width = 360
        panel_height = 32 + 28 * len(self.context_menu_options)
        panel = pygame.Rect(
            min(panel_x + 8, WIDTH - panel_width - 8),
            min(panel_y + 8, HEIGHT - panel_height - 8),
            panel_width,
            panel_height,
        )
        pygame.draw.rect(self.screen, (244, 239, 219), panel)
        pygame.draw.rect(self.screen, (35, 35, 35), panel, 2)
        self.text("Actions", (panel.x + 10, panel.y + 8), self.small_font, (35, 35, 35))
        for index, option in enumerate(self.context_menu_options):
            disabled = not action_option_enabled(option)
            rect = pygame.Rect(panel.x + 10, panel.y + 34 + index * 26, 340, 22)
            pygame.draw.rect(self.screen, (218, 214, 200) if disabled else (233, 226, 200), rect)
            pygame.draw.rect(self.screen, (135, 135, 128) if disabled else (70, 70, 70), rect, 1)
            self.text(
                f"{index + 1}. {option}",
                (rect.x + 6, rect.y + 3),
                self.small_font,
                (145, 145, 138) if disabled else (35, 35, 35),
            )
            self.context_menu.append((rect, option))

    def text(self, value: str, pos: tuple[int, int], font: pygame.font.Font, color: tuple[int, int, int] = (238, 238, 228)) -> None:
        self.screen.blit(font.render(value, True, color), pos)

    def world_text(
        self,
        value: str,
        world_pos: tuple[float, float],
        font: pygame.font.Font,
        color: tuple[int, int, int] = (238, 238, 228),
    ) -> None:
        rendered = font.render(value, True, color)
        visual_zoom = self.world_scale * self.camera.effective_zoom
        if visual_zoom != 1.0:
            rendered = pygame.transform.smoothscale(
                rendered,
                (
                    max(1, round(rendered.get_width() * visual_zoom)),
                    max(1, round(rendered.get_height() * visual_zoom)),
                ),
            )
        self.screen.blit(rendered, self.camera.world_to_screen(world_pos))

    def world_centered_text(
        self,
        value: str,
        obj: WorldObject,
        font: pygame.font.Font,
        color: tuple[int, int, int] = (238, 238, 228),
    ) -> None:
        rendered = font.render(value, True, color)
        logical_scale = min(
            1.0,
            max(1, obj.width - 4) / rendered.get_width(),
            max(1, obj.height - 4) / rendered.get_height(),
        )
        final_scale = (
            logical_scale
            * self.world_scale
            * self.camera.effective_zoom
        )
        rendered = pygame.transform.smoothscale(
            rendered,
            (
                max(1, round(rendered.get_width() * final_scale)),
                max(1, round(rendered.get_height() * final_scale)),
            ),
        )
        center = self.camera.world_to_screen(obj.center)
        self.screen.blit(rendered, rendered.get_rect(center=center))
