from __future__ import annotations

import math
import json
import random
import sys
from dataclasses import dataclass, replace
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
from remembering.model import (
    BuildMemory,
    DAY_LENGTH_MINUTES,
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
from remembering.sprites import ObjectSpriteCatalog
from remembering.tiles import Tile, TileEdge, TileKind
from remembering.world import (
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
    fields = ["action"]
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
                "target_id",
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
        fields.extend(("target_id", "target_type", "target_point"))

    if step.action in AREA_COMMAND_TYPES:
        fields.append("nearest_to_player")
    if step.action == "Water Crops":
        fields.extend(("secondary_bounds", "source_areas"))
    if step.action == "Till Grassland":
        fields.extend(("max_game_minutes", "till_until_done"))

    # Never hide data already carried by a command, even when it came from an
    # older schema or a hand-edited memory file.
    for field_name in RoutineStep.__dataclass_fields__:
        if field_name == "action":
            continue
        value = getattr(step, field_name)
        populated = value is not None and value is not False
        if populated and field_name not in fields:
            fields.append(field_name)
    return tuple(fields)


def routine_field_editor_value(step: RoutineStep, field_name: str) -> object:
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

WIDTH, HEIGHT = 1050, 686
TOP_BAR_HEIGHT = 46
SIDEBAR_WIDTH = 190
RIGHT_SIDEBAR_WIDTH = 220
PLAYER_RADIUS = 14
INTERACTION_DISTANCE = 45
GAME_MINUTES_PER_REAL_SECOND = 1.0
FIXED_SIMULATION_TICK_SECONDS = 0.05
DAY_FADE_DURATION_SECONDS = 0.75
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
}
AREA_COMMANDS = list(AREA_COMMAND_TYPES)
BUILD_COMMAND_TYPES = {
    "Build Barrel": "barrel",
    "Build Cupboard": "cupboard",
}
AREA_COMMAND_CATEGORIES = ("Gather", "Farm", "Build")
MAP_LEFT = SIDEBAR_WIDTH
MAP_RIGHT = MAP_LEFT + 640
MAP_TOP = TOP_BAR_HEIGHT
MAP_BOTTOM = MAP_TOP + 640
RELOAD_BUTTON = pygame.Rect(WIDTH - RIGHT_SIDEBAR_WIDTH + 12, HEIGHT - 46, RIGHT_SIDEBAR_WIDTH - 24, 32)
MAP_VIEWPORT = pygame.Rect(MAP_LEFT, MAP_TOP, MAP_RIGHT - MAP_LEFT, MAP_BOTTOM - MAP_TOP)


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
            if command.startswith("Gather ") or command == "Chop Trees"
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
        persistence_path: Path = DEFAULT_CURRENT_LEVEL_PATH,
    ) -> None:
        pygame.init()
        display_flags = pygame.SCALED if fullscreen else pygame.HIDDEN
        pygame.display.set_mode((WIDTH, HEIGHT), display_flags)
        pygame.display.set_caption("Remembering — Python Prototype v0.1")
        self.screen = pygame.display.get_surface()
        if fullscreen:
            pygame.display.toggle_fullscreen()
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 25)
        self.small_font = pygame.font.Font(None, 20)
        self.title_font = pygame.font.Font(None, 42)
        self.object_sprites = ObjectSpriteCatalog()
        self.day = DayState(mode=Mode.DIRECT)
        self.persistence_path = persistence_path
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
        self.player.speed *= self.world_scale
        self.player.carried_objects = [
            obj for obj in self.objects.values() if obj.container == "player"
        ]
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
        self.walk_target: tuple[float, float] | None = None
        self.path_target: tuple[float, float] | None = None
        self.navigation_path: list[tuple[float, float]] = []
        self.preview_path: list[tuple[float, float]] = []
        self.context_menu: list[tuple[pygame.Rect, str]] = []
        self.context_menu_options: list[str] = []
        self.context_menu_pos: tuple[int, int] | None = None
        self.context_ground_target: tuple[float, float] | None = None
        self.camera_dragging = False
        self.camera_drag_position: tuple[int, int] | None = None
        self.messages: list[str] = ["Day 1 begins in Direct Control. Click an object to begin."]
        self.running = True
        self.menu_index = 0
        self.job_timer = 0.0
        self.job_duration = 0.55
        self.time_accumulator = 0.0
        self.simulation_step_accumulator = 0.0
        self.time_speed = 1.0
        self.simulation_paused = False
        self.pause_button = pygame.Rect(0, 0, 0, 0)
        self.time_speed_buttons: list[tuple[pygame.Rect, float]] = []
        self.day_transition_phase: str | None = None
        self.day_transition_progress = 0.0
        self.replay_outcome = "expand"
        self.record_routine_commands = True
        self.auto_cheat_memory = False
        self.adjusting_memory = False
        self.memory_edit_index = 0
        self.memory_editor_rows: list[tuple[pygame.Rect, int]] = []
        self.memory_editor_buttons: list[tuple[pygame.Rect, str]] = []
        self.memory_editor_fields: list[tuple[pygame.Rect, str]] = []
        self.memory_file_name_rect = pygame.Rect(0, 0, 0, 0)
        self.memory_file_name = "homestead"
        self.memory_edit_field: str | None = None
        self.memory_edit_buffer = ""
        self.memory_editor_previous_pause = False

    def run(self) -> None:
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

    def handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif self.day_transition_phase is not None:
                continue
            elif event.type == pygame.MOUSEWHEEL:
                mouse_pos = pygame.mouse.get_pos()
                if MAP_VIEWPORT.collidepoint(mouse_pos):
                    self.camera.set_zoom(
                        self.camera.zoom * (1.12**event.y),
                        mouse_pos,
                        (self.map.width, self.map.height),
                    )
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 2:
                self.camera_dragging = False
                self.camera_drag_position = None
            elif event.type == pygame.KEYUP and event.key in (pygame.K_LCTRL, pygame.K_RCTRL):
                self.finish_additive_selection()
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
            elif event.type == pygame.MOUSEMOTION and self.command_drag_start is not None:
                if MAP_VIEWPORT.collidepoint(event.pos):
                    self.command_drag_current = self.camera.screen_to_world(event.pos)
            elif event.type == pygame.KEYDOWN:
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

                if self.adjusting_memory:
                    self.handle_memory_editor_key(
                        event.key, pygame.key.get_mods(), event.unicode
                    )
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
                    self.simulation_paused = not self.simulation_paused
                    self.log("Paused." if self.simulation_paused else "Playing.")
                elif event.key == pygame.K_w:
                    self.player.inventory["wood"] += 1
                elif event.key == pygame.K_b:
                    if not self.player.has_bucket:
                        self.create_carried_object("bucket")
                elif self.context_menu_options:
                    if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                        self.context_menu_options = []
                        self.context_menu_pos = None
                        self.context_ground_target = None
                    elif action_index is not None:
                        self.activate_context_option(action_index)
                elif event.key == pygame.K_ESCAPE:
                    if self.day.mode is Mode.MORNING:
                        self.running = False
                    else:
                        self.cancel_current_command()
                elif event.key == pygame.K_q and pygame.key.get_mods() & pygame.KMOD_CTRL:
                    self.running = False
                elif self.day.mode is Mode.MORNING:
                    self.handle_morning_key(event.key)
                elif self.day.mode is Mode.DIRECT and self.selected_id and action_index is not None:
                    self.activate_sidebar_action(action_index)
                elif self.day.mode is Mode.DIRECT and action_index is not None:
                    self.activate_area_menu_index(action_index)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if self.adjusting_memory and event.button == 1:
                    self.handle_memory_editor_click(event.pos)
                elif event.button == 2 and MAP_VIEWPORT.collidepoint(event.pos):
                    self.camera_dragging = True
                    self.camera_drag_position = event.pos
                elif event.button == 1 and self.pause_button.collidepoint(event.pos):
                    self.simulation_paused = not self.simulation_paused
                    self.log("Paused." if self.simulation_paused else "Playing.")
                elif event.button == 1 and any(
                    rect.collidepoint(event.pos) for rect, _ in self.time_speed_buttons
                ):
                    self.time_speed = next(
                        speed for rect, speed in self.time_speed_buttons if rect.collidepoint(event.pos)
                    )
                    label = next(label for label, speed in TIME_SPEED_OPTIONS if speed == self.time_speed)
                    self.log(f"Time speed: {label}.")
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
                elif event.button == 1 and RELOAD_BUTTON.collidepoint(event.pos):
                    self.reload_map()
                elif self.day.mode is Mode.MORNING:
                    self.handle_morning_click(event.pos)
                elif self.day.mode is Mode.DIRECT:
                    if event.button == 1:
                        quantity_change = next(
                            (change for rect, change in self.area_quantity_buttons if rect.collidepoint(event.pos)),
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
                                self.active_command = command
                                self.pending_target_areas.clear()
                                suffix = (
                                    " Or click the character to gather the nearest amount."
                                    if command in AREA_COMMAND_TYPES
                                    else ""
                                )
                                self.log(f"{command}: drag a rectangle on the map.{suffix}")
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
            reloaded_map = load_map(persistence_path=self.persistence_path, day_number=self.day.number)
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
        self.command_drag_start = None
        self.command_drag_current = None
        self.log(f"Reloaded map: {self.map.name}")

    def reload_sprites(self) -> None:
        self.object_sprites.reload()
        self.log("Sprites reloaded.")

    def handle_morning_key(self, key: int) -> None:
        options = self.morning_options()
        number_index = {
            pygame.K_1: 0,
            pygame.K_2: 1,
            pygame.K_3: 2,
            pygame.K_4: 3,
            pygame.K_5: 4,
            pygame.K_6: 5,
            pygame.K_KP1: 0,
            pygame.K_KP2: 1,
            pygame.K_KP3: 2,
            pygame.K_KP4: 3,
            pygame.K_KP5: 4,
            pygame.K_KP6: 5,
        }.get(key)
        if number_index is not None and number_index < len(options):
            self.menu_index = number_index
            self.choose_morning_option(options[number_index])
        elif key in (pygame.K_UP, pygame.K_w):
            self.menu_index = (self.menu_index - 1) % len(options)
        elif key in (pygame.K_DOWN, pygame.K_s):
            self.menu_index = (self.menu_index + 1) % len(options)
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.choose_morning_option(options[self.menu_index])
        elif key == pygame.K_d:
            self.choose_morning_option("Direct Control")
        elif key == pygame.K_r and self.day.remembered_routine:
            self.choose_morning_option("Replay Remembered Routine")

    def handle_morning_click(self, pos: tuple[int, int]) -> None:
        for index, (rect, label) in enumerate(self.morning_button_rects()):
            if rect.collidepoint(pos):
                self.menu_index = index
                self.choose_morning_option(label)
                return

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
                "homestead", tile_size=self.map.tile_map.tile_size
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
        self.memory_editor_previous_pause = self.simulation_paused
        self.simulation_paused = True
        self.adjusting_memory = True
        self.context_menu_options.clear()
        self.context_menu_pos = None
        self.memory_edit_field = None
        self.memory_edit_buffer = ""
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
        self.menu_index = 0
        self.simulation_paused = self.memory_editor_previous_pause

    def handle_memory_editor_key(
        self, key: int, modifiers: int, text_input: str = ""
    ) -> None:
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
        if not routine:
            if key == pygame.K_n:
                self.add_memory_step()
            return
        if key == pygame.K_UP:
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
        if self.memory_file_name_rect.collidepoint(pos):
            self.memory_edit_field = "__memory_file_name__"
            self.memory_edit_buffer = self.memory_file_name
            return
        for rect, index in self.memory_editor_rows:
            if rect.collidepoint(pos):
                self.memory_edit_index = index
                self.memory_edit_field = None
                return
        for rect, field_name in self.memory_editor_fields:
            if rect.collidepoint(pos) and self.day.remembered_routine:
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
            elif action == "Save":
                self.save_named_memory(self.memory_file_name)
            elif action == "Load":
                self.load_named_memory(self.memory_file_name)
            elif action == "Save Homestead":
                self.memory_file_name = "homestead"
                self.save_named_memory("homestead")
            return

    def remove_memory_step(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        removed = self.memory_edit_index
        routine.pop(removed)
        if self.day.mode is Mode.REPLAY and removed < self.day.replay_index:
            self.day.replay_index -= 1
        self.day.replay_index = min(self.day.replay_index, len(routine))
        self.memory_edit_index = min(removed, max(0, len(routine) - 1))

    def duplicate_memory_step(self) -> None:
        routine = self.day.remembered_routine
        if not routine:
            return
        insertion = self.memory_edit_index + 1
        routine.insert(insertion, routine[self.memory_edit_index])
        if self.day.mode is Mode.REPLAY and insertion <= self.day.replay_index:
            self.day.replay_index += 1
        self.memory_edit_index = insertion

    def add_memory_step(self) -> None:
        self.day.remembered_routine.append(RoutineStep(None, "Move To"))
        self.memory_edit_index = len(self.day.remembered_routine) - 1

    def commit_memory_field(self) -> None:
        if self.memory_edit_field == "__memory_file_name__":
            try:
                # Resolving validates the name without touching the filesystem.
                memory_file_path(self.memory_edit_buffer)
            except MapLoadError as exc:
                self.log(str(exc))
                return
            self.memory_file_name = self.memory_edit_buffer.removesuffix(".memory")
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
            elif field_name in {"target_type", "target_build_memory"} and not (
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
            )
        except (OSError, MapLoadError) as exc:
            self.log(f"Could not save memory: {exc}")
            return
        self.log(
            f"Saved {len(self.day.remembered_routine)} orders to {path.name}."
        )

    def load_named_memory(self, name: str) -> None:
        try:
            routine = load_memory_file(
                name, tile_size=self.map.tile_map.tile_size
            )
        except MapLoadError as exc:
            self.log(f"Could not load memory: {exc}")
            return
        self.day.remembered_routine = list(routine)
        self.day.replay_index = 0
        self.memory_edit_index = 0
        self.memory_edit_field = None
        self.memory_edit_buffer = ""
        self.log(
            f"Loaded {len(routine)} orders from {memory_file_path(name).name}."
        )

    def move_memory_step(self, direction: int) -> None:
        routine = self.day.remembered_routine
        if not routine:
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
        world_pos = self.screen_to_world(pos)
        if world_pos is None:
            return
        for obj in reversed(list(self.objects.values())):
            if obj.contains(world_pos):
                self.selected_id = obj.object_id
                self.selected_tile = None
                self.path_target = (float(obj.x + obj.width // 2), float(obj.y + obj.height // 2))
                self.preview_path = self.build_navigation_path_to_object(obj)[1:]
                if self.day.mode is Mode.DIRECT:
                    self.context_ground_target = None
                    self.show_context_menu(obj, pos, world_pos)
                return
        self.selected_id = None
        located = self.map.tile_map.tile_at_world(*world_pos)
        self.selected_tile = (located[0], located[1]) if located is not None else None
        target = self.map.tile_map.center_at_world(*world_pos)
        self.path_target = target
        self.context_menu_options = (
            build_ground_context_menu_options(
                located[2],
                self.player,
                self.map.tile_states.get((located[0], located[1])),
                self.crop_at_tile(located[0], located[1]),
            )
            if target is not None and located is not None
            else []
        )
        self.context_menu_pos = pos if target is not None else None
        self.context_ground_target = target
        self.preview_path = []

    def show_context_menu(self, obj: WorldObject, screen_pos: tuple[int, int], world_pos: tuple[int, int]) -> None:
        options = build_context_menu_options(obj, self.player, world_pos, self.objects)
        if not options:
            self.log("No available action.")
            self.context_menu_options = []
            self.context_menu_pos = None
            return
        self.context_menu_options = options
        self.context_menu_pos = screen_pos
        self.context_ground_target = None
        self.selected_id = obj.object_id

    def activate_context_option(self, index: int) -> None:
        if not 0 <= index < len(self.context_menu_options):
            return
        option = self.context_menu_options[index]
        if not action_option_enabled(option):
            if option == RUINED_BUCKET_REQUIRED:
                self.log("The ruined bucket cannot hold water.")
            return
        self.context_menu_options = []
        self.context_menu_pos = None
        if option == "Fill Barrel" and self.selected_id is not None:
            self.begin_barrel_source_selection(self.selected_id)
            return
        if option == "Move To":
            if self.selected_id is None:
                if self.context_ground_target is not None and self.plan_path(self.context_ground_target):
                    self.resume_for_command()
                    self.path_target = self.context_ground_target
                    self.log("Moving to selected location.")
                elif self.context_ground_target is not None:
                    self.log("No route to selected location.")
                self.context_ground_target = None
                return
            target = self.objects[self.selected_id]
            if self.plan_path_to_object(target):
                self.resume_for_command()
                self.path_target = target.center
                self.log(f"Moving to {target.name}.")
            else:
                self.log(f"No route to {target.name}.")
            self.context_ground_target = None
            return
        if option == "Drop Bucket" and self.selected_id is None:
            target = self.context_ground_target
            if target is not None and self.plan_path(target):
                self.pending_area_target = AreaTarget("Drop Bucket", target)
                self.area_job_timer = 0.0
                self.resume_for_command()
                self.path_target = target
            self.context_ground_target = None
            return
        if option in {"Gather Water", "Water Crop", "Tend Plant"} and self.selected_id is None:
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
        if self.selected_id is None:
            return
        self.queue_job(self.selected_id, option, record=True)

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
        self.active_command = selected
        suffix = (
            " Or click the character to gather the nearest amount."
            if selected in AREA_COMMAND_TYPES
            else ""
        )
        self.log(f"{selected}: drag a rectangle on the map.{suffix}")

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
            self.active_command in AREA_COMMAND_TYPES
            and math.dist(end, (self.player.x, self.player.y)) <= self.player_radius
            and math.dist(start, end) <= self.player_radius
        ):
            command = self.active_command
            self.active_command = None
            self.pending_target_areas.clear()
            self.queue_nearest_gather_command(
                command, self.area_command_quantity, record=True
            )
            return
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
            left, top, right, bottom = tile_aligned_area_bounds(
                end, end, self.map.tile_map.tile_size
            )
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
            self.day.today_routine.append(
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
            self.day.today_routine.append(
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
        elif command in {"Gather Water", "Water Crops"}:
            targets = targets[:1]
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
            self.day.today_routine.append(
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
        if command not in AREA_COMMAND_TYPES:
            return
        targets = self.build_area_targets(
            command, (0, 0, self.map.width, self.map.height)
        )
        targets.sort(
            key=lambda target: math.dist(
                (self.player.x, self.player.y), target.point
            )
        )
        self.area_targets = targets[:quantity]
        self.pending_area_target = None
        self.area_job_timer = 0.0
        if record and self.record_routine_commands:
            self.day.today_routine.append(
                RoutineStep(
                    None,
                    command,
                    quantity=quantity,
                    nearest_to_player=True,
                )
            )
        if self.area_targets:
            self.resume_for_command()
        elif self.day.mode is Mode.REPLAY:
            self.show_failed_nearest_gather(command)
            self.resume_for_command()
        self.log(
            f"Nearest gather queued {len(self.area_targets)} "
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
                if obj.active and obj.type_id in type_ids and left <= center_x <= right and top <= center_y <= bottom:
                    targets.append(AreaTarget("Gather", obj.center, obj.object_id))
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
            self.day.today_routine.append(
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
        tile_map = self.map.tile_map
        size = tile_map.tile_size
        first_column = obj.x // size
        last_column = (obj.x + obj.width - 1) // size
        first_row = obj.y // size
        last_row = (obj.y + obj.height - 1) // size
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
                    and self.tile_can_access_object(column, row, obj)
                ):
                    candidates.append(center)

        path = find_tile_path_to_any((self.player.x, self.player.y), candidates, tile_map)
        return path or []

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
        remaining_distance = max(0.0, self.player.speed * dt)
        while self.navigation_path and remaining_distance > 0.0:
            target_x, target_y = self.navigation_path[0]
            dx = target_x - self.player.x
            dy = target_y - self.player.y
            distance = math.hypot(dx, dy)
            if distance <= 0.001:
                self.player.x, self.player.y = target_x, target_y
                self.navigation_path.pop(0)
                continue
            amount = min(distance, remaining_distance)
            next_x = self.player.x + dx / distance * amount
            next_y = self.player.y + dy / distance * amount
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
        bed = self.object_of_type("bed")
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
            raise MapLoadError("The bed has no valid adjacent player spawn tile")
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

    def cancel_current_command(self) -> None:
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
        if self.adjusting_memory:
            return
        if self.day_transition_phase is not None:
            self.update_day_transition(dt)
            return
        if self.day.mode is Mode.MORNING and self.auto_cheat_memory:
            self.choose_morning_option("Replay Memory and Sleep")
        if self.day.mode is Mode.DIRECT and not self.has_queued_command():
            self.simulation_paused = True
        if self.thought_bubble_timer > 0.0:
            self.thought_bubble_timer = max(0.0, self.thought_bubble_timer - dt)
            if self.thought_bubble_timer == 0.0:
                self.thought_bubble_text = None
        if self.day.mode is Mode.MORNING or self.simulation_paused:
            self.simulation_step_accumulator = 0.0
            self._update_simulation_tick(0.0)
            return
        self.simulation_step_accumulator += dt * self.time_speed
        processed_tick = False
        while (
            self.simulation_step_accumulator + 1e-12
            >= FIXED_SIMULATION_TICK_SECONDS
        ):
            self.simulation_step_accumulator -= FIXED_SIMULATION_TICK_SECONDS
            self._update_simulation_tick(FIXED_SIMULATION_TICK_SECONDS)
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

    def _update_simulation_tick(self, dt: float) -> None:
        if self.pending_empty_area_memory and not self.navigation_path:
            self.show_empty_area_memory_thought()
        self.advance_crop_growth(dt * GAME_MINUTES_PER_REAL_SECOND)
        if self.day.mode is not Mode.MORNING:
            self.time_accumulator += dt * GAME_MINUTES_PER_REAL_SECOND
            elapsed_minutes = int(self.time_accumulator)
            if elapsed_minutes:
                self.time_accumulator -= elapsed_minutes
                end_of_day = START_OF_DAY_MINUTES + DAY_LENGTH_MINUTES
                self.day.current_time_minutes = min(
                    end_of_day, self.day.current_time_minutes + elapsed_minutes
                )
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
            timed_duration = 0.0
            progress_message = ""
            if target.action == "Till Grassland":
                timed_duration = (
                    tilling_duration_seconds(self.player.hoe_quality)
                    * target.work_fraction
                )
                progress_message = "Tilling the ground..."
            elif target.action == "Plant Wheat":
                timed_duration = planting_duration_seconds(self.player.has_basket)
                progress_message = "Planting wheat..."
            elif target.action == "Harvest Wheat":
                interaction = self.map.object_types["crop"].form_definition(
                    "mature", "wheat"
                ).interactions["Harvest Wheat"]
                duration = interaction["duration_seconds"]
                duration_key = (
                    "with_basket"
                    if self.player.has_basket and "with_basket" in duration
                    else "base"
                )
                timed_duration = float(duration[duration_key])
                progress_message = "Harvesting wheat..."
            elif target.action in BUILD_COMMAND_TYPES:
                build_type = BUILD_COMMAND_TYPES[target.action]
                timed_duration = self.map.object_types[
                    build_type
                ].form_definition().build_duration_seconds
                progress_message = f"Building a {build_type.replace('_', ' ')}..."
            if timed_duration:
                if self.area_job_timer == 0.0:
                    self.log(progress_message)
                self.area_job_timer += dt
                if self.area_job_timer < timed_duration:
                    return
            if target.action == "Gather Water":
                if self.player.has_bucket and not self.player.bucket_filled:
                    self.player.bucket_water_uses = self.bucket_capacity
                    self.log("Filled the wooden bucket with 5 uses of water.")
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
                        self.grant_interaction_loot(crop, "Harvest Wheat")
                        crop.active = False
                        self.log("Harvested 3 wheat.")
            self.pending_area_target = None
            self.area_job_timer = 0.0
            self.navigation_path = []
            self.walk_target = None
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
                    if self.replay_outcome == "explore":
                        self.log("Routine complete. Explore freely; new orders will not be remembered.")
                    elif self.replay_outcome == "legacy":
                        self.log("Routine complete. Returning to Direct Control.")
                    else:
                        self.log("Routine complete. Expand the remembered routine with new orders.")
            else:
                step = self.day.remembered_routine[self.day.replay_index]
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
                elif step.nearest_to_player and step.action in AREA_COMMAND_TYPES:
                    self.day.replay_index += 1
                    self.queue_nearest_gather_command(
                        step.action, step.quantity or 1, record=False
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
            self.job_timer += dt
            target = self.objects[self.pending_job.target_id]
            required_duration = object_job_duration_seconds(
                self.pending_job.action, target, self.player.has_basket
            )
            if self.job_timer >= required_duration:
                job = self.pending_job
                self.pending_job = None
                self.complete_job(job)
                if self.day.mode is Mode.REPLAY and job.advances_replay:
                    self.day.replay_index += 1

    def complete_job(self, job: PendingJob) -> None:
        obj = self.objects[job.target_id]
        action = job.action
        if action == "Gather":
            loot = self.grant_interaction_loot(obj, action)
            obj.active = False
            self.log(f"Gathered {', '.join(loot)}.")
        elif action == "Harvest Berries":
            self.grant_interaction_loot(obj, action)
            self.create_interaction_objects(obj, action)
            obj.active = False
            self.log("Harvested berries.")
        elif action == "Pull Berry Bush":
            self.grant_interaction_loot(obj, action)
            self.create_interaction_objects(obj, action)
            obj.active = False
            rebuild_tile_map(self.map)
            self.log("Pulled the berry bush and gathered berries, fiber, and a branch.")
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
            self.log("Prepared a terrible bowl of porridge.")
        elif action in {"Eat Porridge", "Eat Berries"}:
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
            self.player.hunger = min(100, self.player.hunger + food.nutrition)
            food.active = False
            food.container = None
            self.player.carried_objects.remove(food)
            self.log(
                f"Ate the {food.name.lower()} at the table "
                f"(+{food.nutrition} nutrition)."
            )
        elif action == "Sleep":
            self.unlock("Homecoming")
            self.begin_day_transition()

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
        if obj.orientation == "N/S":
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
        self.day_transition_phase = "fade_out"
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
        randomizer = random.Random(44_701 + self.day.number)
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
                elif int(progress["store_count"]) > 0:
                    if randomizer.random() < policy.decay_chance:
                        progress["store_count"] = int(progress["store_count"]) - 1
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
        randomizer = random.Random(83_119 + self.day.number)
        seen_memories: set[str] = set()
        for barrel in (obj for obj in self.objects.values() if obj.type_id == "barrel"):
            data = self.barrel_state(barrel)
            memory_id = data.get("build_memory_id")
            memory = (
                self.build_memories.get(str(memory_id))
                if memory_id is not None
                else None
            )
            if memory is not None:
                seen_memories.add(memory.memory_id)
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
                    if int(data["build_count"]) > 0 and randomizer.random() < policy.decay_chance:
                        data["build_count"] = int(data["build_count"]) - 1
                        barrel.state = data
                        if memory is not None:
                            memory.build_count = int(data["build_count"])
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
        for memory_id, memory in self.build_memories.items():
            if (
                memory_id not in seen_memories
                and not memory.persistent
                and memory.build_count > 0
                and (
                    (policy := self.map.object_types[memory.object_type]
                     .form_definition()
                     .persistence.get("object"))
                    is not None
                )
                and randomizer.random() < policy.decay_chance
            ):
                memory.build_count -= 1

    def advance_stump_memories(self) -> None:
        randomizer = random.Random(97_331 + self.day.number)
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
            if count > 0 and randomizer.random() < policy.decay_chance:
                state["stump_memory_count"] = count - 1
            tree.state = encode_tree_state(state)
            if tree.persistent_state is not None:
                baseline = tree_state_data(tree.persistent_state.state)
                baseline["stump_memory_count"] = state["stump_memory_count"]
                tree.persistent_state.state = encode_tree_state(baseline)

    def update_day_transition(self, dt: float) -> None:
        self.day_transition_progress = min(
            1.0, self.day_transition_progress + dt / DAY_FADE_DURATION_SECONDS
        )
        if self.day_transition_progress < 1.0:
            return
        if self.day_transition_phase == "fade_out":
            if self.finish_day():
                self.day_transition_phase = "fade_in"
                self.day_transition_progress = 0.0
            else:
                self.day_transition_phase = None
                self.day_transition_progress = 0.0
        else:
            self.day_transition_phase = None
            self.day_transition_progress = 0.0

    def finish_day(self) -> bool:
        if (
            self.day.mode is Mode.DIRECT
            and self.day.today_routine
            and not self.auto_cheat_memory
        ):
            self.day.remembered_routine = list(self.day.today_routine)
        try:
            advance_level_tile_states(
                self.map.tile_states,
                day_number=self.day.number,
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
            )
            next_day_map = load_map(
                persistence_path=self.persistence_path,
                day_number=self.day.number + 1,
                reset_for_morning=True,
            )
            save_persistent_objects(
                next_day_map.objects,
                self.persistence_path,
                tile_size=next_day_map.tile_map.tile_size,
                tile_states=next_day_map.tile_states,
                remembered_routine=self.day.remembered_routine,
                build_memories=next_day_map.build_memories,
            )
        except (MapLoadError, ObjectPersistenceError) as exc:
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
        self.day.number += 1
        self.day.mode = Mode.MORNING
        self.day.today_routine.clear()
        self.day.replay_index = 0
        self.day.current_time_minutes = START_OF_DAY_MINUTES
        self.time_accumulator = 0.0
        self.menu_index = 0
        self.player.energy = 100
        self.player.hunger = max(0, self.player.hunger - 25)
        self.player.inventory.clear()
        self.player.x, self.player.y = self.player_spawn()
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
        self.log(f"Day {self.day.number}. Yesterday's completed jobs are remembered.")
        return True

    def unlock(self, name: str) -> None:
        if name not in self.player.achievements:
            self.player.achievements.add(name)
            self.log(f"Achievement unlocked: {name}")

    def log(self, message: str) -> None:
        self.messages.append(message)
        self.messages = self.messages[-5:]

    def draw(self) -> None:
        self.screen.fill((76, 104, 74))
        self.screen.set_clip(MAP_VIEWPORT)
        self.draw_tiles()
        self.draw_room_labels()
        self.draw_objects()
        self.draw_command_selection()
        self.draw_path()
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

    def draw_top_bar(self) -> None:
        panel = pygame.Rect(0, 0, WIDTH, TOP_BAR_HEIGHT)
        pygame.draw.rect(self.screen, (34, 40, 45), panel)
        pygame.draw.rect(self.screen, (188, 170, 105), panel, 2)
        self.text("Day", (12, 12), self.small_font)
        self.text(f"{self.day.number}", (44, 12), self.small_font)
        time_area = pygame.Rect(MAP_LEFT, 0, MAP_RIGHT - MAP_LEFT, TOP_BAR_HEIGHT)
        speed_button_gap = 4
        speed_button_widths = [
            max(45, self.small_font.size(label)[0] + 10)
            for label, _ in TIME_SPEED_OPTIONS
        ]
        speed_controls_width = (
            sum(speed_button_widths)
            + (len(TIME_SPEED_OPTIONS) - 1) * speed_button_gap
        )
        track_width = (
            time_area.width
            - 96  # fixed time display and its gap
            - 16  # gap between sun track and pause
            - 58  # pause button
            - 4   # gap after pause
            - speed_controls_width
            - 8   # right inset
        )
        track_x = time_area.x + 96
        track_y = 12
        pygame.draw.rect(self.screen, (84, 93, 98), (track_x, track_y, track_width, 18))
        progress = day_progress_ratio(self.day.current_time_minutes)
        sun_x = sun_track_position(progress, track_x, track_width)
        filled_width = sun_x - track_x
        pygame.draw.rect(self.screen, (234, 180, 84), (track_x, track_y, filled_width, 18))
        time_surface = self.small_font.render(
            format_clock_time(self.day.current_time_minutes), True, (238, 238, 228)
        )
        time_background = pygame.Rect(time_area.x + 8, 8, 80, 28)
        pygame.draw.rect(self.screen, (34, 40, 45), time_background, border_radius=3)
        self.screen.blit(time_surface, time_surface.get_rect(center=time_background.center))
        pygame.draw.circle(self.screen, (255, 220, 120), (sun_x, track_y + 9), 8)
        self.time_speed_buttons.clear()
        button_x = track_x + track_width + 16
        self.pause_button = pygame.Rect(button_x, 8, 58, 28)
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
        for (label, speed), button_width in zip(
            TIME_SPEED_OPTIONS, speed_button_widths
        ):
            rect = pygame.Rect(button_x, 8, button_width, 28)
            selected = speed == self.time_speed
            pygame.draw.rect(self.screen, (205, 194, 126) if selected else (74, 82, 87), rect, border_radius=3)
            pygame.draw.rect(self.screen, (238, 220, 132) if selected else (117, 124, 128), rect, 1, border_radius=3)
            rendered = self.small_font.render(label, True, (30, 32, 33) if selected else (232, 232, 222))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.time_speed_buttons.append((rect, speed))
            button_x += button_width + speed_button_gap

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
        wall_width = max(
            1, round(4 * self.world_scale * self.camera.effective_zoom)
        )
        room_colors = {
            room.structure_id: room.display_color
            for room in self.map.structures
            if room.display_color is not None
        }
        visible_tiles: list[tuple[Tile, pygame.Rect]] = []
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
                visible_tiles.append((tile, rect))

        # Walls are a separate top layer. Drawing them during the fill pass lets
        # later neighboring tile rectangles cover south and west boundaries.
        for tile, rect in visible_tiles:
            if tile.kind is TileKind.WOODEN_FLOOR:
                self.draw_tile_edges(tile, rect, wall_width)

    def draw_tile_edges(self, tile, rect: pygame.Rect, width: int) -> None:
        color = (48, 42, 37)
        if "wall:north" in tile.properties:
            pygame.draw.line(self.screen, color, rect.topleft, rect.topright, width)
        if "wall:east" in tile.properties:
            pygame.draw.line(self.screen, color, rect.topright, rect.bottomright, width)
        if "wall:south" in tile.properties:
            pygame.draw.line(self.screen, color, rect.bottomleft, rect.bottomright, width)
        if "wall:west" in tile.properties:
            pygame.draw.line(self.screen, color, rect.topleft, rect.bottomleft, width)

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
                if loaded_sprite.anchor.mode == "random_within_tile":
                    margin = loaded_sprite.anchor.margin
                    margin = 0.2 if margin is None else margin
                    anchor_x, anchor_y = 0.5, 0.5
                else:
                    margin = None
                    placement_x, placement_y = 0.5, 0.5
                    anchor_x, anchor_y = loaded_sprite.anchor.point or (0.5, 0.5)
                if obj.orientation == "N/S" and obj.width != obj.height:
                    sprite = pygame.transform.rotate(sprite, 90)
                    anchor_x, anchor_y = anchor_y, 1.0 - anchor_x
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
        bounds = tile_aligned_area_bounds(
            self.command_drag_start, self.command_drag_current, self.map.tile_map.tile_size
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

    def draw_path(self) -> None:
        route = self.navigation_path or self.preview_path
        if not route:
            return
        points = [self.camera.world_to_screen((self.player.x, self.player.y))]
        points.extend(self.camera.world_to_screen((x, y)) for x, y in route)
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
        pygame.draw.circle(self.screen, (222, 214, 187), (px, py), radius)
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
        if self.pending_job:
            self.world_text(
                self.pending_job.action,
                (self.player.x - 35, self.player.y - 35),
                self.small_font,
            )

    def draw_thought_bubble(self) -> None:
        if self.thought_bubble_text is None:
            return
        px, py = self.camera.world_to_screen((self.player.x, self.player.y))
        lines = wrap_text(
            self.thought_bubble_text,
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

    def draw_ui(self) -> None:
        sidebar = pygame.Rect(0, 0, SIDEBAR_WIDTH, HEIGHT)
        right_panel = pygame.Rect(WIDTH - RIGHT_SIDEBAR_WIDTH, 0, RIGHT_SIDEBAR_WIDTH, HEIGHT)
        pygame.draw.rect(self.screen, (25, 28, 31), sidebar)
        pygame.draw.rect(self.screen, (25, 28, 31), right_panel)
        pygame.draw.rect(self.screen, (92, 95, 101), sidebar, 2)
        pygame.draw.rect(self.screen, (92, 95, 101), right_panel, 2)
        self.text("Selection View", (12, 16), self.font)
        self.text("Selected object or tile", (12, 42), self.small_font, (172, 176, 182))

        inventory = ", ".join(f"{k}: {v}" for k, v in sorted(self.player.inventory.items()) if v) or "empty"
        hoe = (
            f"{'carried' if self.player.carrying_hoe else 'stored'} (Q{self.player.hoe_quality})"
            if self.player.has_hoe
            else "none"
        )
        axe = "carried" if self.player.carrying_axe else "stored" if self.player.has_axe else "none"
        bucket = (
            f"{self.player.bucket_water_uses}/{self.bucket_capacity} uses"
            if self.player.has_bucket
            else "none"
        )
        basket = "carried" if self.player.has_basket else "none"
        carried_food = [
            obj.name
            for obj in self.player.carried_objects
            if obj.active and "edible" in obj.traits
        ]
        food = ", ".join(carried_food) if carried_food else "none"
        stats_height = HEIGHT // 3 + 24
        inventory_top = 46 + stats_height
        stats_panel = pygame.Rect(WIDTH - RIGHT_SIDEBAR_WIDTH, 46, RIGHT_SIDEBAR_WIDTH, stats_height)
        inventory_panel = pygame.Rect(WIDTH - RIGHT_SIDEBAR_WIDTH, inventory_top, RIGHT_SIDEBAR_WIDTH, HEIGHT - inventory_top)
        pygame.draw.rect(self.screen, (31, 35, 38), stats_panel)
        pygame.draw.rect(self.screen, (31, 35, 38), inventory_panel)
        pygame.draw.line(self.screen, (92, 95, 101), (WIDTH - RIGHT_SIDEBAR_WIDTH, inventory_top), (WIDTH, inventory_top), 2)

        self.text("Stats", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 58), self.small_font, (172, 176, 182))
        self.text(f"Day {self.day.number}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 86), self.small_font)
        self.text(f"Energy: {self.player.energy}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 110), self.small_font)
        self.text(f"Hunger: {self.player.hunger}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 134), self.small_font)
        self.text(f"Speed: {self.player.speed:g}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 158), self.small_font)
        self.text(f"Hoe: {hoe}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 182), self.small_font)
        self.text(f"Axe: {axe}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 206), self.small_font)
        self.text(f"Bucket: {bucket}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 230), self.small_font)
        self.text(f"Basket: {basket}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 254), self.small_font)
        self.text(f"Food: {food}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 278), self.small_font)

        self.text("Inventory", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, inventory_top + 12), self.small_font, (172, 176, 182))
        if inventory != "empty":
            for index, item in enumerate(sorted(self.player.inventory.items())):
                if not item[1]:
                    continue
                y = inventory_top + 42 + index * 24
                self.text(f"{item[0]}: {item[1]}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, y), self.small_font)
        else:
            self.text("empty", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, inventory_top + 42), self.small_font)

        self.action_buttons.clear()
        show_persistence = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)
        if self.selected_id is not None:
            obj = self.objects[self.selected_id]
            actions = (
                object_action_menu_options(obj, self.player)
                if self.day.mode is Mode.DIRECT
                else []
            )
            display_name = object_map_label(obj) if obj.kind is ObjectKind.TREE else obj.name
            self.text(display_name, (12, 72), self.font)
            kind_label = obj.kind.name.lower().replace("_", " ").title()
            self.text(f"{kind_label} | {obj.quality_stage.title()}", (12, 100), self.small_font, (172, 176, 182))
            description_lines = wrap_text(obj.description, self.small_font, SIDEBAR_WIDTH - 24)
            description_lines.extend(crop_inspection_lines(obj))
            if "edible" in obj.traits:
                description_lines.append(f"Nutrition: {obj.nutrition}")
            if obj.kind is ObjectKind.CUPBOARD:
                description_lines.append(
                    f"Food stored: {len(obj.state.get('food_ids', []))}/"
                    f"{obj.capacity.get('food', 0)}"
                )
            if obj.kind is ObjectKind.BARREL:
                description_lines.append(
                    f"Water: {self.barrel_state(obj)['water_uses']}/{self.barrel_capacity} uses"
                )
            if show_persistence:
                description_lines.extend(self.object_persistence_details(obj))
            for index, line in enumerate(description_lines):
                self.text(line, (12, 126 + index * 19), self.small_font, (210, 214, 202))
            action_y = 164 + len(description_lines) * 19
            self.text("Actions", (12, action_y - 24), self.small_font, (172, 176, 182))
            for index, action in enumerate(actions):
                workbench_recipe = obj.kind is ObjectKind.WORKBENCH
                spacing = 48 if workbench_recipe else 38
                height = 42 if workbench_recipe else 32
                rect = pygame.Rect(12, action_y + index * spacing, SIDEBAR_WIDTH - 24, height)
                disabled = not action_option_enabled(action)
                pygame.draw.rect(self.screen, (95, 96, 92) if disabled else (223, 220, 196), rect)
                pygame.draw.rect(self.screen, (75, 76, 73) if disabled else (30, 30, 30), rect, 2)
                label = action.removesuffix(" (materials required)")
                self.text(f"{index + 1}. {label}", (rect.x + 8, rect.y + 5), self.small_font, (145, 145, 138) if disabled else (25, 25, 25))
                if disabled and workbench_recipe:
                    self.text("materials required", (rect.x + 24, rect.y + 23), self.small_font, (145, 145, 138))
                self.action_buttons.append((rect, action))
            if not actions:
                label = "Inspection only during replay" if self.day.mode is Mode.REPLAY else "No available action"
                self.text(label, (12, action_y), self.small_font, (210, 214, 202))
        elif self.selected_tile is not None:
            column, row = self.selected_tile
            tile = self.map.tile_map.tile_at(column, row)
            state = self.map.tile_states.get((column, row))
            crop = self.crop_at_tile(column, row)
            tile_name = tile.kind.value.replace("_", " ").title() if tile is not None else "Tile"
            self.text(tile_name, (12, 72), self.font)
            self.text(f"Tile: {column}, {row}", (12, 100), self.small_font, (172, 176, 182))
            if crop is not None:
                self.text(f"Plant: {(crop.variant or crop.name).title()}", (12, 128), self.small_font)
                self.text(f"Form: {crop.form.title()}", (12, 152), self.small_font)
                self.text(f"Growth: {float(crop.state.get('growth_progress', 0.0)) * 100:.1f}%", (12, 176), self.small_font)
                self.text(
                    f"Water: {float(crop.state.get('water', 0.0)):.1f}%",
                    (12, 200),
                    self.small_font,
                )
                self.text(
                    f"Tended: {float(crop.state.get('tended', 0.0)):.1f}%",
                    (12, 224),
                    self.small_font,
                )
            else:
                self.text("Plant: none", (12, 128), self.small_font, (210, 214, 202))
            if show_persistence:
                start_y = 248 if crop is not None else 152
                for index, line in enumerate(self.tile_persistence_details(column, row)):
                    self.text(line, (12, start_y + index * 22), self.small_font, (235, 205, 120))
        else:
            self.text("Click an object or tile", (12, 72), self.small_font, (210, 214, 202))
            self.text("in the world to inspect it.", (12, 92), self.small_font, (210, 214, 202))

        self.command_buttons.clear()
        menu_title = "Area Commands"
        if self.active_command_category is not None:
            menu_title += f" > {self.active_command_category}"
        self.text(menu_title, (12, 414), self.small_font, (172, 176, 182))
        self.text("Quantity", (12, 442), self.small_font, (172, 176, 182))
        self.area_quantity_buttons.clear()
        decrease_rect = pygame.Rect(112, 437, 22, 24)
        increase_rect = pygame.Rect(160, 437, 22, 24)
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
        self.screen.blit(quantity_surface, quantity_surface.get_rect(center=(147, 449)))

        self.till_time_buttons.clear()
        command_start_y = 466
        if self.active_command == "Till Grassland":
            self.text("Till time", (12, 472), self.small_font, (172, 176, 182))
            decrease_time = pygame.Rect(82, 467, 24, 24)
            increase_time = pygame.Rect(154, 467, 24, 24)
            until_done = pygame.Rect(184, 467, 94, 24)
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
            self.text(budget_label, (112, 472), self.small_font, (225, 225, 214))
            command_start_y = 496

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

        hovered = RELOAD_BUTTON.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, (205, 198, 166) if hovered else (174, 169, 145), RELOAD_BUTTON)
        pygame.draw.rect(self.screen, (35, 35, 35), RELOAD_BUTTON, 2)
        self.text("Reload Map (F5)", (RELOAD_BUTTON.x + 37, RELOAD_BUTTON.y + 8), self.small_font, (25, 25, 25))

        self.text(self.messages[-1], (SIDEBAR_WIDTH + 14, 690), self.small_font)

    def morning_button_rects(self) -> list[tuple[pygame.Rect, str]]:
        options = self.morning_options()
        return [(pygame.Rect(335 + SIDEBAR_WIDTH, 215 + i * 62, 430, 46), label) for i, label in enumerate(options)]

    def draw_morning_menu(self) -> None:
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
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 165))
        self.screen.blit(overlay, (0, 0))
        panel = pygame.Rect(20, 18, WIDTH - 40, HEIGHT - 36)
        pygame.draw.rect(self.screen, (232, 227, 205), panel)
        pygame.draw.rect(self.screen, (43, 43, 43), panel, 4)
        self.text("Live Memory Editor", (panel.x + 28, panel.y + 18), self.title_font, (35, 35, 35))
        self.text(
            "C/Esc closes · Enter saves a field · Values use JSON syntax.",
            (panel.x + 390, panel.y + 31),
            self.small_font,
            (35, 35, 35),
        )
        routine = self.day.remembered_routine
        self.memory_editor_rows.clear()
        self.memory_editor_buttons.clear()
        self.text("File name", (panel.x + 390, panel.y + 62), self.small_font, (35, 35, 35))
        self.memory_file_name_rect = pygame.Rect(panel.x + 470, panel.y + 54, 170, 30)
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
        for index, label in enumerate(["Save", "Load", "Save Homestead"]):
            width = 130 if label == "Save Homestead" else 72
            x = (
                self.memory_file_name_rect.right + 8
                if index == 0
                else self.memory_file_name_rect.right + 88
                if index == 1
                else self.memory_file_name_rect.right + 168
            )
            rect = pygame.Rect(x, panel.y + 54, width, 30)
            pygame.draw.rect(self.screen, (174, 169, 145), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 1)
            rendered = self.small_font.render(label, True, (35, 35, 35))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.memory_editor_buttons.append((rect, label))

        list_rect = pygame.Rect(panel.x + 24, panel.y + 94, 330, panel.height - 162)
        pygame.draw.rect(self.screen, (218, 213, 190), list_rect)
        visible_count = 12
        start = max(0, min(self.memory_edit_index - visible_count // 2, len(routine) - visible_count))
        for visible_index, routine_index in enumerate(range(start, min(len(routine), start + visible_count))):
            step = routine[routine_index]
            rect = pygame.Rect(list_rect.x + 8, list_rect.y + 8 + visible_index * 38, list_rect.width - 16, 32)
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
            quantity = f" ×{step.quantity}" if step.quantity is not None else ""
            self.text(f"{routine_index + 1}. {step.action}{quantity}", (rect.x + 10, rect.y + 8), self.small_font, (35, 35, 35))
            self.memory_editor_rows.append((rect, routine_index))
        if not routine:
            self.text("No remembered commands.", (list_rect.x + 18, list_rect.y + 18), self.font, (70, 70, 65))

        self.memory_editor_fields.clear()
        if routine:
            step = routine[self.memory_edit_index]
            field_x = list_rect.right + 24
            field_width = panel.right - field_x - 24
            for index, field_name in enumerate(routine_step_editable_fields(step)):
                y = panel.y + 94 + index * 38
                value_rect = pygame.Rect(field_x + 164, y, field_width - 164, 32)
                self.text(field_name, (field_x, y + 7), self.small_font, (35, 35, 35))
                active = field_name == self.memory_edit_field
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
                value_text = (
                    self.memory_edit_buffer
                    if active
                    else json.dumps(routine_field_editor_value(step, field_name))
                )
                while value_text and self.small_font.size(value_text)[0] > value_rect.width - 12:
                    value_text = "…" + value_text[2:]
                self.text(value_text, (value_rect.x + 6, value_rect.y + 7), self.small_font, (35, 35, 35))
                self.memory_editor_fields.append((value_rect, field_name))
        labels = ["Move Up", "Move Down", "Duplicate", "Remove", "New", "Done"]
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
