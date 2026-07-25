from __future__ import annotations

import math
import json
import random
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pygame

from remembering.camera import Camera
from remembering.navigation import find_tile_path, find_tile_path_to_any
from remembering.model import (
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
    can_craft_axe,
    can_craft_basket,
    can_craft_bucket,
    can_craft_hoe,
    has_new_craftable_tool,
)
from remembering.tiles import Tile, TileEdge, TileKind
from remembering.world import (
    DEFAULT_CURRENT_LEVEL_PATH,
    advance_level_tile_states,
    initialize_current_level_from_map,
    MapLoadError,
    ObjectPersistenceError,
    load_map,
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


WORKBENCH_RECIPES = (
    ("Craft Crude Hoe", can_craft_hoe),
    ("Craft Crude Axe", can_craft_axe),
    ("Craft Wooden Bucket", can_craft_bucket),
    ("Weave Fiber Basket", can_craft_basket),
)
DISABLED_RECIPE_OPTIONS = {
    action: f"{action} (materials required)" for action, _ in WORKBENCH_RECIPES
}


def object_action_menu_options(obj: WorldObject, player: PlayerState) -> list[str]:
    if obj.kind is not ObjectKind.WORKBENCH:
        return available_actions(obj, player)
    return [
        action if can_craft(player) else DISABLED_RECIPE_OPTIONS[action]
        for action, can_craft in WORKBENCH_RECIPES
    ]


def action_option_enabled(option: str) -> bool:
    return option != EMPTY_BUCKET_REQUIRED and option not in DISABLED_RECIPE_OPTIONS.values()


def build_ground_context_menu_options(
    tile: Tile, player: PlayerState, state: LevelTileState | None = None
) -> list[str]:
    options = ["Move To"]
    if tile.kind in {TileKind.SHALLOW_WATER, TileKind.POND}:
        options.append(
            "Gather Water"
            if player.has_bucket and not player.bucket_filled
            else EMPTY_BUCKET_REQUIRED
        )
    if tile.kind is TileKind.SOIL and state is not None and state.crop is not None:
        if player.has_bucket and player.bucket_filled and not state.watered:
            options.append("Water Crop")
        if not state.tended and state.crop_growth < 1.0:
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
    if obj.kind is ObjectKind.TREE and tree_state_data(obj.state)["form"] == "stump":
        return "Stump"
    return obj.kind.name.lower().replace("_", " ").title()


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

WIDTH, HEIGHT = 1100, 720
TOP_BAR_HEIGHT = 46
SIDEBAR_WIDTH = 190
RIGHT_SIDEBAR_WIDTH = 220
PLAYER_RADIUS = 14
INTERACTION_DISTANCE = 45
GAME_MINUTES_PER_REAL_SECOND = 1.0
DAY_FADE_DURATION_SECONDS = 0.75
CROP_BASE_GROWTH_MINUTES = 600.0
CROP_WATERED_MULTIPLIER = 3.0
CROP_TENDED_MULTIPLIER = 1.15
EMPTY_BUCKET_REQUIRED = "Gather Water (empty bucket required)"
BUCKET_CAPACITY = 5
BARREL_CAPACITY = 30
TIME_SPEED_OPTIONS = ((".5x", 0.5), ("1x", 1.0), ("2x", 2.0), ("4x", 4.0), ("10x", 10.0))
AREA_COMMAND_TYPES = {
    "Gather Pebbles": {"pebble"},
    "Gather Branches": {"branch"},
    "Gather Seeds": {"wheat", "wild_grain"},
    "Gather Tall Grass": {"grass"},
}
AREA_COMMANDS = list(AREA_COMMAND_TYPES)
AREA_COMMAND_CATEGORIES = ("Gather", "Farm", "Build")
MAP_LEFT = SIDEBAR_WIDTH
MAP_RIGHT = WIDTH - RIGHT_SIDEBAR_WIDTH
MAP_TOP = TOP_BAR_HEIGHT
MAP_BOTTOM = HEIGHT
RELOAD_BUTTON = pygame.Rect(WIDTH - RIGHT_SIDEBAR_WIDTH + 12, HEIGHT - 46, RIGHT_SIDEBAR_WIDTH - 24, 32)
MAP_VIEWPORT = pygame.Rect(MAP_LEFT, MAP_TOP, MAP_RIGHT - MAP_LEFT, MAP_BOTTOM - MAP_TOP)


def tilling_duration_seconds(hoe_quality: int) -> float:
    quality = max(1, min(100, hoe_quality))
    return 6.0 + (100 - quality) * 0.18


def planting_duration_seconds(has_basket: bool) -> float:
    return 1.5 if has_basket else 4.0


def harvesting_duration_seconds(has_basket: bool) -> float:
    return 2.0 if has_basket else 6.0


def sun_track_position(progress: float, track_x: int, track_width: int, radius: int = 8) -> int:
    clamped = max(0.0, min(1.0, progress))
    return track_x + radius + round((track_width - radius * 2) * clamped)


def object_job_duration_seconds(action: str, obj: WorldObject, has_basket: bool) -> float:
    if action == "Harvest Wheat":
        return harvesting_duration_seconds(has_basket)
    if action == "Harvest Berries" or (action == "Gather" and obj.kind is ObjectKind.WILD_GRAIN):
        return 1.0 if has_basket else 3.0
    return 0.55


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
        return [command for command in commands if command == "Build Barrel"]
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
        self.day = DayState(mode=Mode.DIRECT)
        self.persistence_path = persistence_path
        initialize_current_level_from_map(current_level_path=self.persistence_path)
        self.map = load_map(
            persistence_path=self.persistence_path,
            day_number=self.day.number,
            reset_for_morning=True,
        )
        self.objects = self.map.objects
        spawn_x, spawn_y = self.player_spawn()
        self.player = PlayerState(x=spawn_x, y=spawn_y)
        self.storage_memories = self.load_storage_memories()
        self.camera = Camera(MAP_LEFT, MAP_TOP, MAP_RIGHT - MAP_LEFT, MAP_BOTTOM - MAP_TOP)
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
        self.time_speed = 1.0
        self.simulation_paused = False
        self.pause_button = pygame.Rect(0, 0, 0, 0)
        self.time_speed_buttons: list[tuple[pygame.Rect, float]] = []
        self.day_transition_phase: str | None = None
        self.day_transition_progress = 0.0
        self.replay_outcome = "expand"
        self.record_routine_commands = True
        self.adjusting_memory = False
        self.memory_edit_index = 0
        self.memory_editor_rows: list[tuple[pygame.Rect, int]] = []
        self.memory_editor_buttons: list[tuple[pygame.Rect, str]] = []

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

                if event.key == pygame.K_F5:
                    self.reload_map()
                elif event.key == pygame.K_w:
                    self.player.inventory["wood"] += 1
                elif event.key == pygame.K_b:
                    self.player.has_bucket = True
                    self.player.bucket_water_uses = 0
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
                    if self.adjusting_memory:
                        self.handle_memory_editor_key(event.key, pygame.key.get_mods())
                    else:
                        self.handle_morning_key(event.key)
                elif self.day.mode is Mode.DIRECT and self.selected_id and action_index is not None:
                    self.activate_sidebar_action(action_index)
                elif self.day.mode is Mode.DIRECT and action_index is not None:
                    self.activate_area_menu_index(action_index)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 2 and MAP_VIEWPORT.collidepoint(event.pos):
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
                    if self.adjusting_memory:
                        self.handle_memory_editor_click(event.pos)
                    else:
                        self.handle_morning_click(event.pos)
                elif self.day.mode is Mode.DIRECT:
                    if event.button == 1:
                        quantity_change = next(
                            (change for rect, change in self.area_quantity_buttons if rect.collidepoint(event.pos)),
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
                                self.log(f"{command}: drag a rectangle on the map.")
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
        self.thought_bubble_text = None
        self.thought_bubble_timer = 0.0
        self.command_drag_start = None
        self.command_drag_current = None
        self.log(f"Reloaded map: {self.map.name}")

    def handle_morning_key(self, key: int) -> None:
        options = self.morning_options()
        number_index = {
            pygame.K_1: 0,
            pygame.K_2: 1,
            pygame.K_3: 2,
            pygame.K_4: 3,
            pygame.K_5: 4,
            pygame.K_KP1: 0,
            pygame.K_KP2: 1,
            pygame.K_KP3: 2,
            pygame.K_KP4: 3,
            pygame.K_KP5: 4,
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
            self.adjusting_memory = True
            self.memory_edit_index = min(
                self.memory_edit_index, max(0, len(self.day.remembered_routine) - 1)
            )
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

    def handle_memory_editor_key(self, key: int, modifiers: int) -> None:
        routine = self.day.remembered_routine
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
            self.adjusting_memory = False
            self.menu_index = 0
            return
        if not routine:
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
            routine.pop(self.memory_edit_index)
            self.memory_edit_index = min(self.memory_edit_index, max(0, len(routine) - 1))
        elif key == pygame.K_r:
            self.replace_memory_step()

    def handle_memory_editor_click(self, pos: tuple[int, int]) -> None:
        for rect, index in self.memory_editor_rows:
            if rect.collidepoint(pos):
                self.memory_edit_index = index
                return
        for rect, action in self.memory_editor_buttons:
            if not rect.collidepoint(pos):
                continue
            if action == "Done":
                self.adjusting_memory = False
                self.menu_index = 0
            elif action == "Move Up":
                self.move_memory_step(-1)
            elif action == "Move Down":
                self.move_memory_step(1)
            elif action == "Remove" and self.day.remembered_routine:
                self.day.remembered_routine.pop(self.memory_edit_index)
                self.memory_edit_index = min(
                    self.memory_edit_index,
                    max(0, len(self.day.remembered_routine) - 1),
                )
            elif action == "Replace":
                self.replace_memory_step()
            return

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
                located[2], self.player, self.map.tile_states.get((located[0], located[1]))
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
        if option in {"Gather Water", "Water Crop", "Tend Plant"} and self.selected_id is None:
            target = self.context_ground_target
            located = self.map.tile_map.tile_at_world(*target) if target is not None else None
            state = self.map.tile_states.get((located[0], located[1])) if located is not None else None
            valid = located is not None and (
                (
                    option == "Gather Water"
                    and located[2].kind in {TileKind.SHALLOW_WATER, TileKind.POND}
                    and self.player.has_bucket
                    and not self.player.bucket_filled
                )
                or (
                    option == "Water Crop"
                    and state is not None
                    and state.crop is not None
                    and self.player.has_bucket
                    and self.player.bucket_filled
                    and not state.watered
                )
                or (
                    option == "Tend Plant"
                    and state is not None
                    and state.crop is not None
                    and not state.tended
                    and state.crop_growth < 1.0
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
        self.log(f"{selected}: drag a rectangle on the map.")

    def screen_to_world(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        return self.camera.screen_to_world(pos)

    def finish_command_drag(self, screen_pos: tuple[int, int]) -> None:
        end = self.camera.screen_to_world(screen_pos)
        start = self.command_drag_start
        self.command_drag_start = None
        self.command_drag_current = None
        if start is None or end is None or self.active_command is None:
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
        if self.active_command == "Build Barrel":
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
                self.visit_failed_memory(None)
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
                self.visit_failed_memory(center)
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
    ) -> None:
        target_areas = target_areas or (bounds,)
        if command == "Build Barrel" and not self.can_afford_build("barrel"):
            cost = self.build_cost("barrel")
            requirements = " and ".join(
                f"{amount} {item}" for item, amount in cost.items()
            )
            self.log(f"Need {requirements} to build a barrel.")
            if self.day.mode is Mode.REPLAY:
                center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
                self.visit_failed_memory(center)
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
        if command == "Plant Wheat":
            targets = targets[: self.player.inventory["seed"]]
        elif command in {"Gather Water", "Water Crops"}:
            targets = targets[:1]
        elif command == "Build Barrel":
            cost = self.build_cost("barrel")
            affordable = min(
                (self.player.inventory[item] // amount for item, amount in cost.items()),
                default=0,
            )
            targets = targets[:min(1, affordable)]
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
                )
            )
        if targets:
            self.resume_for_command()
        elif self.day.mode is Mode.REPLAY:
            center = (
                (bounds[0] + bounds[2]) / 2,
                (bounds[1] + bounds[3]) / 2,
            )
            self.pending_empty_area_memory = True
            if not self.plan_path(center):
                self.show_empty_area_memory_thought()
            self.resume_for_command()
        elif command == "Build Barrel":
            self.log("A barrel cannot be built on that tile.")
        self.log(f"Area command queued {len(targets)} target{'s' if len(targets) != 1 else ''}.")

    def show_empty_area_memory_thought(self) -> None:
        self.pending_empty_area_memory = False
        self.thought_bubble_text = "Why did I come here?"
        self.thought_bubble_timer = 2.5
        self.log(self.thought_bubble_text)

    def visit_failed_memory(self, point: tuple[float, float] | None) -> None:
        destination = point or (self.player.x, self.player.y)
        self.pending_empty_area_memory = True
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
        elif command in {"Gather Water", "Till Grassland", "Plant Wheat", "Water Crops", "Tend Crops", "Harvest Wheat", "Build Barrel"}:
            if command == "Gather Water" and (not self.player.has_bucket or self.player.bucket_filled):
                return []
            if command == "Till Grassland" and not self.player.carrying_hoe:
                return []
            if command == "Plant Wheat" and self.player.inventory["seed"] <= 0:
                return []
            if command == "Water Crops" and not self.player.has_bucket:
                return []
            if command == "Build Barrel" and (
                not self.can_afford_build("barrel")
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
                    elif command == "Build Barrel":
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
                                        "Build Barrel",
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
                        state = self.map.tile_states.get((column, row))
                        if state is not None and state.crop is None:
                            targets.append(AreaTarget("Plant Wheat", point))
                    elif command == "Water Crops" and tile.kind is TileKind.SOIL:
                        state = self.map.tile_states.get((column, row))
                        if state is not None and state.crop is not None and not state.watered:
                            targets.append(AreaTarget("Water Crops", point))
                    elif command == "Tend Crops" and tile.kind is TileKind.SOIL:
                        state = self.map.tile_states.get((column, row))
                        if (
                            state is not None
                            and state.crop is not None
                            and not state.tended
                            and state.crop_growth < 1.0
                        ):
                            targets.append(AreaTarget("Tend Plant", point))
                    elif command == "Harvest Wheat" and tile.kind is TileKind.SOIL:
                        state = self.map.tile_states.get((column, row))
                        if state is not None and state.crop == "wheat" and state.crop_growth >= 1.0:
                            targets.append(AreaTarget("Harvest Wheat", point))
        return targets

    def queue_job(
        self,
        target_id: int,
        action: str,
        *,
        record: bool,
        advances_replay: bool = True,
    ) -> None:
        if self.pending_job is not None:
            return
        obj = self.objects.get(target_id)
        if obj is None or action not in available_actions(obj, self.player):
            self.log(f"Skipped unavailable job: {action}.")
            if self.day.mode is Mode.REPLAY and advances_replay:
                self.day.replay_index += 1
                self.visit_failed_memory(obj.center if obj is not None else None)
            return
        if not self.plan_path_to_object(obj):
            self.log(f"No route to {obj.name}.")
            if self.day.mode is Mode.REPLAY and advances_replay:
                self.day.replay_index += 1
                self.visit_failed_memory(obj.center)
            return
        self.pending_job = PendingJob(target_id, action, self.walk_target, advances_replay)
        self.resume_for_command()
        self.selected_id = None
        self.preview_path = []
        self.job_timer = 0.0
        if record and self.record_routine_commands and action != "Sleep":
            self.day.today_routine.append(
                RoutineStep(target_id, action, obj.type_id, target_point=obj.center)
            )

    def resolve_routine_target(self, step: RoutineStep) -> int | None:
        """Resolve an object-specific order without changing what it referred to."""
        original = self.objects.get(step.target_id)
        if original is not None and step.action in available_actions(original, self.player):
            return original.object_id
        return None

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
                    and distance_to_object(center, obj) <= INTERACTION_DISTANCE
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
        while self.navigation_path:
            target_x, target_y = self.navigation_path[0]
            dx = target_x - self.player.x
            dy = target_y - self.player.y
            distance = math.hypot(dx, dy)
            if distance <= 4:
                self.player.x, self.player.y = target_x, target_y
                self.navigation_path.pop(0)
                continue
            amount = min(distance, self.player.speed * dt)
            next_x = self.player.x + dx / distance * amount
            next_y = self.player.y + dy / distance * amount
            if not self.can_stand_at(next_x, next_y):
                self.navigation_path = []
                self.walk_target = None
                return False
            self.player.x = next_x
            self.player.y = next_y
            return True
        self.walk_target = None
        return True

    def can_stand_at(self, x: float, y: float) -> bool:
        if (
            x < PLAYER_RADIUS
            or y < PLAYER_RADIUS
            or x > self.map.width - PLAYER_RADIUS
            or y > self.map.height - PLAYER_RADIUS
        ):
            return False

        if not self.map.tile_map.can_stand_at(x, y, PLAYER_RADIUS):
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
        standable = [point for point in candidates if tile_map.can_stand_at(*point, PLAYER_RADIUS)]
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
        for (column, row), state in self.map.tile_states.items():
            if state.crop is None or state.crop_growth >= 1.0:
                continue
            multiplier = (
                (CROP_WATERED_MULTIPLIER if state.watered else 1.0)
                * (CROP_TENDED_MULTIPLIER if state.tended else 1.0)
            )
            state.crop_growth = min(
                1.0,
                state.crop_growth + game_minutes * multiplier / CROP_BASE_GROWTH_MINUTES,
            )

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
        barrel = self.objects.get(job.barrel_id)
        if barrel is None or not barrel.active or barrel.kind is not ObjectKind.BARREL:
            self.barrel_fill_job = None
            self.visit_failed_memory(barrel.center if barrel is not None else None)
            return True
        data = self.barrel_state(barrel)
        if int(data["water_uses"]) >= BARREL_CAPACITY:
            self.barrel_fill_job = None
            self.navigation_path = []
            self.walk_target = None
            self.log("The barrel is full.")
            return True
        if job.phase == "choose":
            if self.player.bucket_water_uses > 0:
                if not self.plan_path_to_object(barrel):
                    self.barrel_fill_job = None
                    self.visit_failed_memory(barrel.center)
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
            self.visit_failed_memory(center)
            return True
        if self.navigation_path:
            if not self.move_along_path(dt):
                job.phase = "choose"
            return True
        if job.phase == "source":
            self.player.bucket_water_uses = BUCKET_CAPACITY
            self.log("Filled the bucket from the selected water area.")
            job.phase = "choose"
            return True
        if job.phase == "barrel":
            moved = min(
                self.player.bucket_water_uses,
                BARREL_CAPACITY - int(data["water_uses"]),
            )
            self.player.bucket_water_uses -= moved
            data["water_uses"] = int(data["water_uses"]) + moved
            barrel.state = json.dumps(data, separators=(",", ":"))
            self.log(f"Added {moved} water uses to the barrel.")
            if int(data["water_uses"]) >= BARREL_CAPACITY:
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
        while job.crop_points:
            point = job.crop_points[0]
            located = self.map.tile_map.tile_at_world(*point)
            state = (
                self.map.tile_states.get((located[0], located[1]))
                if located is not None
                else None
            )
            if state is not None and state.crop is not None and not state.watered:
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
            self.visit_failed_memory(center)
            return True
        if self.navigation_path:
            if not self.move_along_path(dt):
                job.phase = "choose"
            return True
        if job.phase == "source":
            self.player.bucket_water_uses = BUCKET_CAPACITY
            self.log("Refilled the bucket for the field.")
            job.phase = "choose"
            return True
        if job.phase == "crop" and job.current_crop is not None:
            located = self.map.tile_map.tile_at_world(*job.current_crop)
            state = (
                self.map.tile_states.get((located[0], located[1]))
                if located is not None
                else None
            )
            if state is not None and state.crop is not None and not state.watered:
                state.watered = True
                self.player.bucket_water_uses -= 1
                self.log("Watered a selected crop.")
            job.crop_points.pop(0)
            job.current_crop = None
            job.phase = "choose"
            return True
        return True

    def update(self, dt: float) -> None:
        real_dt = dt
        if self.day_transition_phase is not None:
            self.update_day_transition(dt)
            return
        if self.day.mode is Mode.DIRECT and not self.has_queued_command():
            self.simulation_paused = True
        simulation_dt = (
            0.0
            if self.day.mode is Mode.MORNING or self.simulation_paused
            else dt * self.time_speed
        )
        self.advance_crop_growth(simulation_dt * GAME_MINUTES_PER_REAL_SECOND)
        if self.day.mode is not Mode.MORNING:
            self.time_accumulator += simulation_dt * GAME_MINUTES_PER_REAL_SECOND
            elapsed_minutes = int(self.time_accumulator)
            if elapsed_minutes:
                self.time_accumulator -= elapsed_minutes
                end_of_day = START_OF_DAY_MINUTES + DAY_LENGTH_MINUTES
                self.day.current_time_minutes = min(
                    end_of_day, self.day.current_time_minutes + elapsed_minutes
                )
        dt = simulation_dt
        if self.thought_bubble_timer > 0.0:
            self.thought_bubble_timer = max(0.0, self.thought_bubble_timer - real_dt)
            if self.thought_bubble_timer == 0.0:
                self.thought_bubble_text = None
            return
        if self.pending_empty_area_memory and not self.navigation_path:
            self.show_empty_area_memory_thought()
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
            timed_duration = 0.0
            progress_message = ""
            if target.action == "Till Grassland":
                timed_duration = tilling_duration_seconds(self.player.hoe_quality)
                progress_message = "Tilling the ground..."
            elif target.action == "Plant Wheat":
                timed_duration = planting_duration_seconds(self.player.has_basket)
                progress_message = "Planting wheat..."
            elif target.action == "Harvest Wheat":
                timed_duration = harvesting_duration_seconds(self.player.has_basket)
                progress_message = "Harvesting wheat..."
            elif target.action == "Build Barrel":
                timed_duration = 4.0
                progress_message = "Building a barrel..."
            if timed_duration:
                if self.area_job_timer == 0.0:
                    self.log(progress_message)
                self.area_job_timer += dt
                if self.area_job_timer < timed_duration:
                    return
            if target.action == "Gather Water":
                if self.player.has_bucket and not self.player.bucket_filled:
                    self.player.bucket_water_uses = BUCKET_CAPACITY
                    self.log("Filled the wooden bucket with 5 uses of water.")
            elif target.action == "Till Grassland" and self.player.carrying_hoe:
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None and located[2].kind is TileKind.GRASSLAND:
                    column, row, tile = located
                    state = self.map.tile_states.setdefault(
                        (column, row), LevelTileState(column=column, row=row)
                    )
                    state.till_count += 1
                    state.tilled_today = True
                    tile.kind = TileKind.SOIL
                    self.log("Tilled grassland into soil.")
            elif target.action == "Plant Wheat":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None and located[2].kind is TileKind.SOIL and self.player.inventory["seed"] > 0:
                    column, row, tile = located
                    state = self.map.tile_states.get((column, row))
                    if state is not None and state.crop is None:
                        state.crop = "wheat"
                        state.crop_growth = 0.0
                        tile.properties.append("crop:wheat")
                        self.player.inventory["seed"] -= 1
                        self.log("Planted wheat seed.")
            elif target.action == "Water Crops" and self.player.bucket_filled:
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    column, row, tile = located
                    state = self.map.tile_states.get((column, row))
                    if state is not None and state.crop is not None and not state.watered:
                        state.watered = True
                        self.player.bucket_water_uses -= 1
                        tile.properties = [
                            prop for prop in tile.properties if not prop.startswith("crop_growth:")
                        ]
                        tile.properties.append(f"crop_growth:{state.crop_growth}")
                        self.log("Watered the wheat; it will now grow much faster.")
            elif target.action == "Build Barrel":
                if self.can_afford_build("barrel"):
                    self.build_barrel(target.placement_point or target.point)
            elif target.action == "Tend Plant":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    state = self.map.tile_states.get((located[0], located[1]))
                    if state is not None and state.crop is not None and not state.tended:
                        state.tended = True
                        self.log("Tended the plant; it will grow a little faster.")
            elif target.action == "Harvest Wheat":
                located = self.map.tile_map.tile_at_world(*target.point)
                if located is not None:
                    column, row, tile = located
                    state = self.map.tile_states.get((column, row))
                    if state is not None and state.crop == "wheat" and state.crop_growth >= 1.0:
                        state.crop = None
                        state.crop_growth = 0.0
                        tile.properties = [
                            prop
                            for prop in tile.properties
                            if not prop.startswith("crop:") and not prop.startswith("crop_growth:")
                        ]
                        self.player.inventory["wheat"] += 3
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
                    self.queue_job(
                        bed.object_id,
                        "Sleep",
                        record=False,
                        advances_replay=False,
                    )
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
                    if step.target_id is None:
                        self.visit_failed_memory(step.target_point)
                    else:
                        self.queue_barrel_fill_command(
                            step.target_id,
                            step.source_areas or (step.area_bounds,),
                            record=False,
                        )
                elif step.area_bounds is not None:
                    self.day.replay_index += 1
                    self.queue_area_command(
                        step.action,
                        step.area_bounds,
                        step.quantity or 1,
                        record=False,
                        target_areas=step.target_areas,
                    )
                else:
                    target_id = self.resolve_routine_target(step)
                    if target_id is None:
                        self.log(f"Skipped unavailable remembered job: {step.action}.")
                        self.day.replay_index += 1
                        original = self.objects.get(step.target_id)
                        self.visit_failed_memory(
                            step.target_point
                            or (original.center if original is not None else None)
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
            item_by_kind = {
                ObjectKind.STICK: "stick",
                ObjectKind.STONE: "stone",
                ObjectKind.GRASS: "fiber",
                ObjectKind.WILD_GRAIN: "seed",
                ObjectKind.BERRY_BUSH: "berries",
            }
            item = item_by_kind[obj.kind]
            self.player.inventory[item] += 1
            obj.active = False
            self.log(f"Gathered {item}.")
        elif action == "Harvest Berries":
            self.player.inventory["berries"] += 1
            obj.active = False
            self.log("Harvested berries.")
        elif action == "Pull Berry Bush":
            self.player.inventory["berries"] += 1
            self.player.inventory["fiber"] += 1
            self.player.inventory["stick"] += 1
            obj.active = False
            rebuild_tile_map(self.map)
            self.log("Pulled the berry bush and gathered berries, fiber, and a branch.")
        elif action == "Break Off Branch":
            state = tree_state_data(obj.state)
            if state["form"] == "stump" or state["branch_taken"]:
                return
            self.player.inventory["stick"] += 1
            state["branch_taken"] = True
            obj.state = encode_tree_state(state)
            self.log("Broke a branch from the tree.")
        elif action == "Chop Down Tree":
            if not self.player.carrying_axe or not obj.active:
                return
            state = tree_state_data(obj.state)
            state["form"] = "stump"
            state["branch_taken"] = True
            obj.state = encode_tree_state(state)
            self.player.inventory["wood"] += 3
            rebuild_tile_map(self.map)
            self.log("Chopped down the tree and gathered 3 wood.")
        elif action == "Craft Crude Hoe":
            if not can_craft_hoe(self.player):
                self.log("Need 1 stick, 1 stone, and 1 fiber.")
                return
            for item in ("stick", "stone", "fiber"):
                self.player.inventory[item] -= 1
            self.player.has_hoe = True
            self.player.carrying_hoe = True
            self.player.hoe_quality = 20
            self.unlock("First Tool")
            self.log("Crafted and equipped a crude hoe.")
        elif action == "Craft Crude Axe":
            if not can_craft_axe(self.player):
                self.log("Need 1 stick, 2 stone, and 1 fiber.")
                return
            self.player.inventory["stick"] -= 1
            self.player.inventory["stone"] -= 2
            self.player.inventory["fiber"] -= 1
            self.player.has_axe = True
            self.player.carrying_axe = True
            self.log("Crafted and equipped a crude axe.")
        elif action == "Craft Wooden Bucket":
            if not can_craft_bucket(self.player):
                self.log("Need 2 wood and 1 fiber.")
                return
            self.player.inventory["wood"] -= 2
            self.player.inventory["fiber"] -= 1
            self.player.has_bucket = True
            self.player.bucket_water_uses = 0
            self.log("Crafted a wooden bucket.")
        elif action in {"Pour Water Into Barrel", "Fill Bucket From Barrel"}:
            data = self.barrel_state(obj)
            barrel_water = int(data["water_uses"])
            if action == "Pour Water Into Barrel":
                moved = min(self.player.bucket_water_uses, BARREL_CAPACITY - barrel_water)
                self.player.bucket_water_uses -= moved
                data["water_uses"] = barrel_water + moved
                self.log(f"Poured {moved} use{'s' if moved != 1 else ''} into the barrel.")
            else:
                moved = min(BUCKET_CAPACITY - self.player.bucket_water_uses, barrel_water)
                self.player.bucket_water_uses += moved
                data["water_uses"] = barrel_water - moved
                self.log(f"Took {moved} use{'s' if moved != 1 else ''} from the barrel.")
            obj.state = json.dumps(data, separators=(",", ":"))
        elif action == "Weave Fiber Basket":
            if not can_craft_basket(self.player):
                self.log("Need 3 fiber.")
                return
            self.player.inventory["fiber"] -= 3
            self.player.has_basket = True
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
        elif action == "Prepare Soil":
            obj.state = "prepared"
            self.log("Prepared the old field.")
        elif action == "Plant Wheat":
            self.player.inventory["seed"] -= 1
            obj.state = "planted"
            self.log("Planted wild wheat seed.")
        elif action == "Whisper to Wheat":
            obj.state = "mature"
            self.log("The wheat remembers how to grow.")
        elif action == "Harvest Wheat":
            obj.state = "wild"
            self.player.inventory["wheat"] += 3
            self.unlock("First Harvest")
            self.log("Harvested 3 wheat.")
        elif action == "Cook Wheat":
            self.player.inventory["wheat"] -= 1
            self.player.meal_ready = True
            self.log("Cooked a simple wheat meal.")
        elif action == "Eat Meal":
            self.player.meal_ready = False
            self.player.hunger = min(100, self.player.hunger + 60)
            self.log("Ate at the broken table.")
        elif action == "Sleep":
            self.unlock("Homecoming")
            self.begin_day_transition()

    def barrel_state(self, barrel: WorldObject) -> dict[str, object]:
        try:
            loaded = json.loads(barrel.state) if barrel.state else {}
        except (json.JSONDecodeError, TypeError):
            loaded = {}
        memory = loaded.get("water_memory", {}) if isinstance(loaded, dict) else {}
        return {
            "water_uses": max(0, min(BARREL_CAPACITY, int(loaded.get("water_uses", 0)))),
            "build_count": max(0, int(loaded.get("build_count", 0))),
            "built_today": bool(loaded.get("built_today", False)),
            "water_memory": {
                "observed": max(0, min(BARREL_CAPACITY, int(memory.get("observed", 0)))),
                "count": max(0, int(memory.get("count", 0))),
                "remembered": max(0, min(BARREL_CAPACITY, int(memory.get("remembered", 0)))),
            },
        }

    def persistence_chance_text(self, count: int) -> str:
        chance = min(1.0, self.map.permanent_soil_chance_per_till * count)
        return f"{chance * 100:.3f}%"

    def object_persistence_details(self, obj: WorldObject) -> list[str]:
        if obj.kind is ObjectKind.TREE:
            state = tree_state_data(obj.state)
            count = int(state["stump_memory_count"])
            if state["form"] == "stump":
                count += 1
            return [
                f"Stump memory count: {count}",
                f"Persistence chance: {self.persistence_chance_text(count)}",
            ]
        if obj.kind is ObjectKind.BARREL:
            state = self.barrel_state(obj)
            if not obj.persistent:
                count = int(state["build_count"]) + (1 if state["built_today"] else 0)
                return [
                    f"Barrel memory count: {count}",
                    f"Persistence chance: {self.persistence_chance_text(count)}",
                ]
            memory = state["water_memory"]
            count = int(memory["count"]) + 1
            return [
                "Barrel persistence: remembered",
                f"Water-level count: {count}",
                f"Level-memory chance: {self.persistence_chance_text(count)}",
                f"Remembered water: {memory['remembered']}/{BARREL_CAPACITY}",
            ]
        if obj.kind is ObjectKind.TOOL_STORAGE:
            lines = []
            for tool, progress in self.storage_memories.get(obj.object_id, {}).items():
                count = int(progress["store_count"]) + (1 if progress["present"] else 0)
                status = "remembered" if progress["persistent"] else self.persistence_chance_text(count)
                lines.append(f"{tool.title()}: {count} ({status})")
            return lines
        return ["Persistence: remembered"] if obj.persistent else ["Persistence: not remembered"]

    def tile_persistence_details(self, column: int, row: int) -> list[str]:
        state = self.map.tile_states.get((column, row))
        if state is None:
            return ["Persistence chance: 0.000%"]
        if state.permanent_kind is not None:
            return ["Terrain persistence: remembered"]
        return [
            f"Till memory count: {state.till_count}",
            f"Persistence chance: {self.persistence_chance_text(state.till_count)}",
        ]

    def build_cost(self, type_id: str) -> dict[str, int]:
        return dict(self.map.object_types[type_id].build_cost)

    def can_afford_build(self, type_id: str) -> bool:
        cost = self.build_cost(type_id)
        return bool(cost) and all(
            self.player.inventory[item] >= amount for item, amount in cost.items()
        )

    def build_barrel(self, point: tuple[float, float]) -> None:
        definition = self.map.object_types["barrel"]
        existing = next(
            (
                obj for obj in self.objects.values()
                if obj.type_id == "barrel" and not obj.active
                and math.dist(obj.center, point) < self.map.tile_map.tile_size / 2
            ),
            None,
        )
        if existing is None:
            object_id = max(self.objects, default=0) + 1
            x = round(point[0] - definition.width / 2)
            y = round(point[1] - definition.height / 2)
            existing = WorldObject(
                object_id,
                definition.name,
                definition.kind,
                x,
                y,
                definition.width,
                definition.height,
                state="",
                blocks_movement=definition.blocks_movement,
                descriptions=dict(definition.descriptions),
                type_id=definition.type_id,
                quality=20,
            )
            existing.persistent_state = ObjectState(
                x, y, quality=20, active=True, state="", persistent=False
            )
            self.objects[object_id] = existing
        data = self.barrel_state(existing)
        data["built_today"] = True
        data["water_uses"] = 0
        existing.state = json.dumps(data, separators=(",", ":"))
        existing.active = True
        for item, amount in self.build_cost("barrel").items():
            self.player.inventory[item] -= amount
        rebuild_tile_map(self.map)
        self.log("Built a wooden barrel.")

    def begin_day_transition(self) -> None:
        if self.day_transition_phase is not None:
            return
        self.day_transition_phase = "fade_out"
        self.day_transition_progress = 0.0
        self.simulation_paused = True

    def load_storage_memories(self) -> dict[int, dict[str, dict[str, object]]]:
        memories: dict[int, dict[str, dict[str, object]]] = {}
        for storage in (obj for obj in self.objects.values() if obj.type_id == "tool_storage"):
            try:
                data = json.loads(storage.state) if storage.state else {}
            except (json.JSONDecodeError, TypeError):
                data = {}
            tools = data.get("tools", {}) if isinstance(data, dict) else {}
            memories[storage.object_id] = {
                tool: {
                    "store_count": max(0, int(tools.get(tool, {}).get("store_count", 0))),
                    "present": bool(tools.get(tool, {}).get("present", False)),
                    "persistent": bool(tools.get(tool, {}).get("persistent", False)),
                    "quality": max(1, min(100, int(tools.get(tool, {}).get("quality", 20)))),
                }
                for tool in ("hoe", "axe")
            }
        return memories

    def sync_storage_memory(self, storage: WorldObject) -> None:
        storage.state = json.dumps(
            {"tools": self.storage_memories[storage.object_id]}, separators=(",", ":")
        )

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
            for tool in ("hoe", "axe"):
                progress = tools[tool]
                if progress["present"]:
                    progress["store_count"] = int(progress["store_count"]) + 1
                    if not progress["persistent"]:
                        chance = min(1.0, self.map.permanent_soil_chance_per_till * int(progress["store_count"]))
                        if randomizer.random() < chance:
                            progress["persistent"] = True
                        else:
                            progress["present"] = False
                elif int(progress["store_count"]) > 0:
                    if randomizer.random() < self.map.till_count_loss_chance:
                        progress["store_count"] = int(progress["store_count"]) - 1
            storage = self.objects[storage_id]
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
        for barrel in (obj for obj in self.objects.values() if obj.type_id == "barrel"):
            data = self.barrel_state(barrel)
            if not barrel.persistent:
                if not barrel.active or not data["built_today"]:
                    if int(data["build_count"]) > 0 and randomizer.random() < self.map.till_count_loss_chance:
                        data["build_count"] = int(data["build_count"]) - 1
                        barrel.state = json.dumps(data, separators=(",", ":"))
                    continue
                data["build_count"] = int(data["build_count"]) + 1
                chance = min(
                    1.0,
                    self.map.permanent_soil_chance_per_till * int(data["build_count"]),
                )
                data["built_today"] = False
                data["water_uses"] = 0
                if randomizer.random() < chance:
                    barrel.persistent = True
                    barrel.active = True
                    barrel.persistent_state = ObjectState(
                        barrel.x,
                        barrel.y,
                        barrel.orientation,
                        barrel.quality,
                        True,
                        json.dumps(data, separators=(",", ":")),
                        True,
                    )
                else:
                    barrel.active = False
                barrel.state = json.dumps(data, separators=(",", ":"))
                continue

            actual_water = int(data["water_uses"])
            memory = data["water_memory"]
            if actual_water == int(memory["observed"]):
                memory["count"] = int(memory["count"]) + 1
            else:
                memory["observed"] = actual_water
                memory["count"] = 1
            chance = min(
                1.0,
                self.map.permanent_soil_chance_per_till * int(memory["count"]),
            )
            if randomizer.random() < chance:
                memory["remembered"] = actual_water
            data["built_today"] = False
            barrel.state = json.dumps(data, separators=(",", ":"))
            persistent_data = dict(data)
            persistent_data["water_uses"] = int(memory["remembered"])
            if barrel.persistent_state is None:
                barrel.persistent_state = ObjectState(
                    barrel.x, barrel.y, barrel.orientation, barrel.quality, True, "", True
                )
            barrel.persistent_state.state = json.dumps(
                persistent_data, separators=(",", ":")
            )

    def advance_stump_memories(self) -> None:
        randomizer = random.Random(97_331 + self.day.number)
        for tree in (obj for obj in self.objects.values() if obj.kind is ObjectKind.TREE):
            state = tree_state_data(tree.state)
            count = int(state["stump_memory_count"])
            if state["form"] == "stump":
                count += 1
                state["stump_memory_count"] = count
                chance = min(1.0, self.map.permanent_soil_chance_per_till * count)
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
                    )
                else:
                    state["form"] = "tree"
                    state["branch_taken"] = False
                    tree.state = encode_tree_state(state)
                    if tree.persistent_state is not None:
                        baseline = dict(state)
                        baseline["form"] = "tree"
                        baseline["branch_taken"] = False
                        tree.persistent_state.state = encode_tree_state(baseline)
                continue
            if count > 0 and randomizer.random() < self.map.till_count_loss_chance:
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
        try:
            advance_level_tile_states(
                self.map.tile_states,
                day_number=self.day.number,
                permanent_chance_per_till=self.map.permanent_soil_chance_per_till,
                till_count_loss_chance=self.map.till_count_loss_chance,
            )
            self.advance_storage_memories()
            self.advance_barrel_memories()
            self.advance_stump_memories()
            save_persistent_objects(
                self.objects,
                self.persistence_path,
                tile_size=self.map.tile_map.tile_size,
                tile_states=self.map.tile_states,
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
            )
        except (MapLoadError, ObjectPersistenceError) as exc:
            self.log(f"Could not finish the day: {exc}")
            return False
        if self.day.mode is Mode.DIRECT and self.day.today_routine:
            self.day.remembered_routine = list(self.day.today_routine)
        self.map = next_day_map
        self.objects = next_day_map.objects
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
        if self.day.mode is Mode.MORNING and self.day_transition_phase is None:
            if self.adjusting_memory:
                self.draw_memory_editor()
            else:
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
        track_width = 260
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
        for label, speed in TIME_SPEED_OPTIONS:
            button_width = 45
            rect = pygame.Rect(button_x, 8, button_width, 28)
            selected = speed == self.time_speed
            pygame.draw.rect(self.screen, (205, 194, 126) if selected else (74, 82, 87), rect, border_radius=3)
            pygame.draw.rect(self.screen, (238, 220, 132) if selected else (117, 124, 128), rect, 1, border_radius=3)
            rendered = self.small_font.render(label, True, (30, 32, 33) if selected else (232, 232, 222))
            self.screen.blit(rendered, rendered.get_rect(center=rect.center))
            self.time_speed_buttons.append((rect, speed))
            button_x += button_width + 4

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
            int((self.camera.x + self.camera.viewport_width / self.camera.zoom) // tile_map.tile_size),
        )
        first_row = max(0, int(self.camera.y // tile_map.tile_size))
        last_row = min(
            tile_map.rows - 1,
            int((self.camera.y + self.camera.viewport_height / self.camera.zoom) // tile_map.tile_size),
        )
        scaled_size = max(1, round(tile_map.tile_size * self.camera.zoom))
        wall_width = max(1, round(4 * self.camera.zoom))
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
                crop_state = self.map.tile_states.get((column, row))
                if crop_state is not None and crop_state.crop == "wheat":
                    growth = min(1.0, crop_state.crop_growth)
                    stem_color = (196, 174, 73) if growth >= 1.0 else (112, 151, 73)
                    center_x = rect.centerx
                    stem_top = rect.bottom - max(6, round((8 + growth * 15) * self.camera.zoom))
                    pygame.draw.line(
                        self.screen,
                        stem_color,
                        (center_x, rect.bottom - 4),
                        (center_x, stem_top),
                        max(1, round(2 * self.camera.zoom)),
                    )
                    pygame.draw.line(self.screen, stem_color, (center_x, rect.centery), (center_x - 5, rect.centery - 5), 1)
                    pygame.draw.line(self.screen, stem_color, (center_x, rect.centery - 3), (center_x + 5, rect.centery - 8), 1)
                if self.camera.zoom >= 0.75:
                    pygame.draw.rect(self.screen, (55, 70, 58), rect, 1)
                visible_tiles.append((tile, rect))

        # Walls are a separate top layer. Drawing them during the fill pass lets
        # later neighboring tile rectangles cover south and west boundaries.
        for tile, rect in visible_tiles:
            if tile.kind is TileKind.WOODEN_FLOOR:
                self.draw_tile_edges(tile, rect, wall_width)

    def draw_tile_edges(self, tile, rect: pygame.Rect, width: int) -> None:
        color = (48, 42, 37)
        if not tile.passable[TileEdge.NORTH]:
            pygame.draw.line(self.screen, color, rect.topleft, rect.topright, width)
        if not tile.passable[TileEdge.EAST]:
            pygame.draw.line(self.screen, color, rect.topright, rect.bottomright, width)
        if not tile.passable[TileEdge.SOUTH]:
            pygame.draw.line(self.screen, color, rect.bottomleft, rect.bottomright, width)
        if not tile.passable[TileEdge.WEST]:
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
            if not obj.active:
                continue
            color = self.object_color(obj)
            if obj.kind is ObjectKind.TREE and tree_state_data(obj.state)["form"] == "stump":
                center = self.camera.world_to_screen(obj.center)
                radius_x = max(3, round(13 * self.camera.zoom))
                radius_y = max(2, round(9 * self.camera.zoom))
                rect = pygame.Rect(0, 0, radius_x * 2, radius_y * 2)
                rect.center = center
                selected = obj.object_id == self.selected_id
                border_color = (245, 225, 120) if selected else (45, 31, 20)
                pygame.draw.ellipse(self.screen, (103, 67, 39), rect)
                pygame.draw.ellipse(
                    self.screen,
                    border_color,
                    rect,
                    max(1, round((4 if selected else 2) * self.camera.zoom)),
                )
                self.world_text("Stump", (obj.center[0] - 20, obj.center[1] - 7), self.small_font)
                continue
            if obj.kind is ObjectKind.BARREL:
                center = self.camera.world_to_screen(obj.center)
                radius = max(3, round(min(obj.width, obj.height) * self.camera.zoom / 2))
                selected = obj.object_id == self.selected_id
                border = max(1, round((4 if selected else 2) * self.camera.zoom))
                border_color = (245, 225, 120) if selected else (45, 31, 20)
                pygame.draw.circle(self.screen, border_color, center, radius + border)
                pygame.draw.circle(self.screen, color, center, radius)
                pygame.draw.circle(self.screen, (76, 48, 27), center, max(1, round(radius * 0.68)), border)
                label = self.small_font.render("B", True, (244, 225, 184))
                if self.camera.zoom != 1.0:
                    label = pygame.transform.smoothscale(
                        label,
                        (
                            max(1, round(label.get_width() * self.camera.zoom)),
                            max(1, round(label.get_height() * self.camera.zoom)),
                        ),
                    )
                self.screen.blit(label, label.get_rect(center=center))
                continue
            screen_x, screen_y = self.camera.world_to_screen((obj.x, obj.y))
            rect = pygame.Rect(
                screen_x,
                screen_y,
                max(1, round(obj.width * self.camera.zoom)),
                max(1, round(obj.height * self.camera.zoom)),
            )
            pygame.draw.rect(self.screen, color, rect)
            selected = obj.object_id == self.selected_id
            border = max(1, round((4 if selected else 2) * self.camera.zoom))
            border_color = (245, 225, 120) if selected else (35, 35, 35)
            if obj.kind is ObjectKind.WORKBENCH and not selected and has_new_craftable_tool(self.player):
                pulse = (math.sin(pygame.time.get_ticks() / 350.0) + 1.0) / 2.0
                low = (74, 67, 48)
                high = (205, 177, 91)
                border_color = tuple(
                    round(low[channel] + (high[channel] - low[channel]) * pulse)
                    for channel in range(3)
                )
                border = max(border, max(1, round((2 + pulse) * self.camera.zoom)))
            pygame.draw.rect(self.screen, border_color, rect, border)
            label = object_map_label(obj)
            rendered_width = self.small_font.size(label)[0]
            available_width = obj.width
            display_label = compact_label(label, available_width, rendered_width)
            self.world_centered_text(display_label, obj, self.small_font)

    def object_color(self, obj: WorldObject) -> tuple[int, int, int]:
        if obj.kind is ObjectKind.FIELD:
            return {
                "wild": (89, 94, 55),
                "prepared": (105, 69, 45),
                "planted": (111, 139, 66),
                "mature": (190, 164, 73),
            }[obj.state]
        return {
            ObjectKind.BED: (115, 91, 91),
            ObjectKind.TABLE: (117, 84, 54),
            ObjectKind.FOOD_PREP_STATION: (103, 103, 103),
            ObjectKind.BERRY_BUSH: (74, 113, 69),
            ObjectKind.WORKBENCH: (125, 86, 52),
            ObjectKind.TOOL_STORAGE: (91, 75, 62),
            ObjectKind.STICK: (121, 84, 51),
            ObjectKind.STONE: (119, 123, 124),
            ObjectKind.GRASS: (75, 130, 63),
            ObjectKind.WILD_GRAIN: (178, 149, 62),
            ObjectKind.TREE: (54, 105, 61),
            ObjectKind.BOULDER: (91, 94, 91),
            ObjectKind.BARREL: (115, 78, 46),
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
                max(1, round(2 * self.camera.zoom)),
            )

    def draw_player(self) -> None:
        px, py = self.camera.world_to_screen((self.player.x, self.player.y))
        radius = max(2, round(PLAYER_RADIUS * self.camera.zoom))
        outline = max(1, round(2 * self.camera.zoom))
        pygame.draw.circle(self.screen, (255, 220, 120), (px, py), radius + outline)
        pygame.draw.circle(self.screen, (222, 214, 187), (px, py), radius)
        pygame.draw.circle(self.screen, (35, 35, 35), (px, py), radius, outline)
        eye_offset_x = round(4 * self.camera.zoom)
        eye_offset_y = round(3 * self.camera.zoom)
        eye_radius = max(1, round(2 * self.camera.zoom))
        pygame.draw.circle(self.screen, (35, 35, 35), (px - eye_offset_x, py - eye_offset_y), eye_radius)
        pygame.draw.circle(self.screen, (35, 35, 35), (px + eye_offset_x, py - eye_offset_y), eye_radius)
        mouth_rect = pygame.Rect(
            px - round(6 * self.camera.zoom),
            py + round(2 * self.camera.zoom),
            max(2, round(12 * self.camera.zoom)),
            max(2, round(8 * self.camera.zoom)),
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
        rendered = self.small_font.render(self.thought_bubble_text, True, (35, 35, 35))
        bubble = rendered.get_rect()
        bubble.inflate_ip(20, 14)
        bubble.midbottom = (px, py - max(18, round(PLAYER_RADIUS * self.camera.zoom)))
        bubble.clamp_ip(MAP_VIEWPORT.inflate(-8, -8))
        pygame.draw.rect(self.screen, (244, 239, 219), bubble, border_radius=10)
        pygame.draw.rect(self.screen, (55, 55, 50), bubble, 2, border_radius=10)
        tail_x = min(max(px, bubble.left + 12), bubble.right - 12)
        pygame.draw.circle(self.screen, (244, 239, 219), (tail_x, bubble.bottom + 5), 5)
        pygame.draw.circle(self.screen, (55, 55, 50), (tail_x, bubble.bottom + 5), 5, 1)
        pygame.draw.circle(self.screen, (244, 239, 219), (px, bubble.bottom + 12), 3)
        pygame.draw.circle(self.screen, (55, 55, 50), (px, bubble.bottom + 12), 3, 1)
        self.screen.blit(rendered, rendered.get_rect(center=bubble.center))

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
            f"{self.player.bucket_water_uses}/{BUCKET_CAPACITY} uses"
            if self.player.has_bucket
            else "none"
        )
        basket = "owned" if self.player.has_basket else "none"
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
        self.text(f"Meal: {'ready' if self.player.meal_ready else 'none'}", (WIDTH - RIGHT_SIDEBAR_WIDTH + 12, 278), self.small_font)

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
            if obj.kind is ObjectKind.BARREL:
                description_lines.append(
                    f"Water: {self.barrel_state(obj)['water_uses']}/{BARREL_CAPACITY} uses"
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
            tile_name = tile.kind.value.replace("_", " ").title() if tile is not None else "Tile"
            self.text(tile_name, (12, 72), self.font)
            self.text(f"Tile: {column}, {row}", (12, 100), self.small_font, (172, 176, 182))
            if state is not None and state.crop is not None:
                self.text(f"Plant: {state.crop.title()}", (12, 128), self.small_font)
                self.text(f"Growth: {state.crop_growth * 100:.1f}%", (12, 152), self.small_font)
                self.text(f"Watered: {'yes' if state.watered else 'no'}", (12, 176), self.small_font)
                self.text(f"Tended: {'yes' if state.tended else 'no'}", (12, 200), self.small_font)
            else:
                self.text("Plant: none", (12, 128), self.small_font, (210, 214, 202))
            if show_persistence:
                start_y = 224 if state is not None and state.crop is not None else 152
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

        self.command_category_buttons.clear()
        if self.active_command_category is None:
            for index, category in enumerate(AREA_COMMAND_CATEGORIES):
                rect = pygame.Rect(12, 466 + index * 30, SIDEBAR_WIDTH - 24, 27)
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
            rect = pygame.Rect(12, 466 + index * 30, SIDEBAR_WIDTH - 24, 27)
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
        panel = pygame.Rect(SIDEBAR_WIDTH + 245, 65, 650, 630)
        pygame.draw.rect(self.screen, (232, 227, 205), panel)
        pygame.draw.rect(self.screen, (43, 43, 43), panel, 4)
        self.text("Adjust Memory", (panel.x + 190, panel.y + 24), self.title_font, (35, 35, 35))
        self.text(
            "Select an order. Shift+Up/Down moves; Delete removes; R replaces.",
            (panel.x + 36, panel.y + 72),
            self.small_font,
            (35, 35, 35),
        )
        routine = self.day.remembered_routine
        self.memory_editor_rows.clear()
        visible_count = 9
        start = max(0, min(self.memory_edit_index - visible_count // 2, len(routine) - visible_count))
        for visible_index, routine_index in enumerate(range(start, min(len(routine), start + visible_count))):
            step = routine[routine_index]
            rect = pygame.Rect(panel.x + 30, panel.y + 108 + visible_index * 42, panel.width - 60, 35)
            selected = routine_index == self.memory_edit_index
            pygame.draw.rect(self.screen, (195, 194, 157) if selected else (213, 209, 183), rect)
            pygame.draw.rect(self.screen, (55, 55, 50), rect, 2 if selected else 1)
            quantity = f" ×{step.quantity}" if step.quantity is not None else ""
            self.text(f"{routine_index + 1}. {step.action}{quantity}", (rect.x + 10, rect.y + 8), self.small_font, (35, 35, 35))
            self.memory_editor_rows.append((rect, routine_index))
        self.memory_editor_buttons.clear()
        for index, label in enumerate(["Move Up", "Move Down", "Remove", "Replace", "Done"]):
            rect = pygame.Rect(panel.x + 30 + index * 118, panel.bottom - 62, 108, 34)
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
        if self.camera.zoom != 1.0:
            rendered = pygame.transform.smoothscale(
                rendered,
                (
                    max(1, round(rendered.get_width() * self.camera.zoom)),
                    max(1, round(rendered.get_height() * self.camera.zoom)),
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
        final_scale = logical_scale * self.camera.zoom
        rendered = pygame.transform.smoothscale(
            rendered,
            (
                max(1, round(rendered.get_width() * final_scale)),
                max(1, round(rendered.get_height() * final_scale)),
            ),
        )
        center = self.camera.world_to_screen(obj.center)
        self.screen.blit(rendered, rendered.get_rect(center=center))
