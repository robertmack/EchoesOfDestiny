import pygame
import pytest

import remembering.game as game_module
from remembering.model import DayState, RoutineStep, day_progress_ratio, format_clock_time
from remembering.game import (
    AreaTarget,
    FIXED_SIMULATION_TICK_SECONDS,
    Game,
    HEIGHT,
    MAP_LEFT,
    MAP_BOTTOM,
    MAP_RIGHT,
    MAP_SIZE,
    MAP_TOP,
    MESSAGE_BAR,
    MESSAGE_BAR_HEIGHT,
    RIGHT_SIDEBAR_WIDTH,
    SIDEBAR_WIDTH,
    TOP_BAR_HEIGHT,
    WIDTH,
    sun_track_position,
)
from remembering.model import Mode
from remembering.tiles import TileKind


def test_clock_starts_at_six_am() -> None:
    day = DayState()
    assert format_clock_time(day.current_time_minutes) == "6:00 AM"


def test_day_progress_ratio_clamps_across_the_day() -> None:
    assert day_progress_ratio(360) == 0.0
    assert day_progress_ratio(1320) == 1.0
    assert day_progress_ratio(840) > 0.4
    assert day_progress_ratio(840) < 0.6


def test_day_and_attempt_are_separate_counters() -> None:
    day = DayState()

    assert day.number == 1
    assert day.attempts == 1


def test_restore_advances_attempt_without_advancing_day(tmp_path) -> None:
    game = Game(
        fullscreen=False,
        persistence_path=tmp_path / "current_level.jsonc",
    )

    assert game.finish_day() is True
    assert game.day.number == 1
    assert game.day.attempts == 2


def test_day_transition_rewinds_route_at_selected_speed(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    start = (game.player.x, game.player.y)
    game.day_position_history = [
        start,
        (start[0] + 20, start[1]),
        (start[0] + 40, start[1]),
    ]
    game.player.x, game.player.y = game.day_position_history[-1]
    game.day.current_time_minutes = 23 * 60

    game.begin_day_transition()
    assert game.day_transition_phase == "night_bumper"
    assert game.displayed_time_minutes() == 23 * 60
    game.update_day_transition(game_module.NIGHT_BUMPER_DURATION_SECONDS)
    game.time_speed = 1.0
    game.update_day_transition(0.05)

    assert game.day_transition_phase == "rewind"
    assert game.rewind_speed == 1.0
    assert game.player.x == start[0] + 20
    assert game.displayed_time_minutes() < 23 * 60

    for _ in range(100):
        game.update_day_transition(0.05)
        if game.day_transition_phase != "rewind":
            break

    assert game.day_transition_phase == "fade_out"
    assert (game.player.x, game.player.y) == start


def test_rewind_finishes_the_day_once_without_restarting(monkeypatch) -> None:
    game = Game(fullscreen=False)
    start = (game.player.x, game.player.y)
    game.day_position_history = [start, (start[0] + 20, start[1])]
    game.player.x, game.player.y = game.day_position_history[-1]
    finish_calls = 0

    def finish_day() -> bool:
        nonlocal finish_calls
        finish_calls += 1
        return True

    monkeypatch.setattr(game, "finish_day", finish_day)
    game.begin_day_transition()

    game.update_day_transition(game_module.NIGHT_BUMPER_DURATION_SECONDS)
    game.update_day_transition(FIXED_SIMULATION_TICK_SECONDS)
    assert game.day_transition_phase == "fade_out"
    assert game.day_transition_progress == 1.0
    game.update_day_transition(0.0)

    assert finish_calls == 1
    assert game.day_transition_phase == "planner"


def test_rewind_speed_can_be_changed_while_it_is_playing(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    start = (game.player.x, game.player.y)
    game.day_position_history = [
        (start[0] + index, start[1]) for index in range(20)
    ]
    game.player.x, game.player.y = game.day_position_history[-1]
    game.begin_day_transition()
    game.update_day_transition(game_module.NIGHT_BUMPER_DURATION_SECONDS)

    pygame.event.post(
        pygame.event.Event(
            pygame.KEYDOWN,
            key=pygame.K_PLUS,
            mod=0,
            unicode="+",
        )
    )
    game.handle_events()

    assert game.time_speed == 2.0
    game.update_day_transition(0.05)
    assert game.rewind_cursor == pytest.approx(17.0)


def test_fast_rewind_batches_checkpoint_restoration(monkeypatch) -> None:
    game = Game(fullscreen=False)
    start = (game.player.x, game.player.y)
    game.day_position_history = [
        (start[0] + index, start[1]) for index in range(101)
    ]
    game.player.x, game.player.y = game.day_position_history[-1]
    snapshots = [{"marker": index} for index in range(3)]
    game.rewind_checkpoints = [
        (70, "First", snapshots[0]),
        (80, "Second", snapshots[1]),
        (90, "Third", snapshots[2]),
    ]
    restored: list[dict[str, object]] = []
    monkeypatch.setattr(game, "restore_rewind_checkpoint", restored.append)

    game.begin_day_transition()
    game.time_speed = 40.0
    game.update_day_rewind(FIXED_SIMULATION_TICK_SECONDS)

    assert game.rewind_cursor == pytest.approx(60.0)
    assert restored == [snapshots[0]]
    assert game.rewind_checkpoint_index == -1


def test_rewind_path_points_back_along_recorded_route_not_to_bed() -> None:
    game = Game(fullscreen=False)
    start = (game.player.x, game.player.y)
    game.day_position_history = [
        start,
        (start[0] + 10, start[1]),
        (start[0] + 20, start[1]),
        (start[0] + 30, start[1]),
    ]
    bed_destination = game.object_of_type("bed").center
    game.navigation_path = [bed_destination]
    game.player.x, game.player.y = (start[0] + 25, start[1])
    game.day_transition_phase = "rewind"
    game.rewind_cursor = 2.5

    points = game.visible_path_world_points()

    assert points == [
        (start[0] + 25, start[1]),
        (start[0] + 20, start[1]),
        (start[0] + 10, start[1]),
        start,
    ]
    assert bed_destination not in points


def test_clock_continues_after_ten_pm(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.current_time_minutes = 21 * 60 + 59

    game._update_simulation_tick(2.0)

    assert game.day.current_time_minutes == 22 * 60 + 1


def test_late_night_hints_are_data_driven_and_shown_once() -> None:
    game = Game(fullscreen=False)
    game.day.current_time_minutes = 1499

    game._update_simulation_tick(1.0)

    assert game.day.current_time_minutes == 1500
    assert game.day_transition_phase is None
    assert game.messages[-1] == "The night has grown very still."
    assert game.thought_bubble_source == "night_hint"
    message_count = game.messages.count("The night has grown very still.")

    game._update_simulation_tick(1.0)

    assert game.messages.count("The night has grown very still.") == message_count


def test_three_thirteen_forces_night_and_caps_the_clock() -> None:
    game = Game(fullscreen=False)
    game.day.current_time_minutes = 1632

    game._update_simulation_tick(5.0)

    assert game.day.current_time_minutes == 1633
    assert game.day_transition_phase == "night_bumper"
    assert game.messages[-1] == (
        "The hour arrives. Your eyes close before you decide to close them."
    )


def test_rewind_restores_command_state_when_it_reaches_the_command(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    branch = game.object_of_type("branch")
    tile_index = next(
        index
        for index, candidate in enumerate(game.map.tile_map.tiles)
        if candidate.kind is TileKind.GRASSLAND
    )
    tile = game.map.tile_map.tiles[tile_index]
    game.record_rewind_checkpoint("Gather")
    branch.active = False
    tile.kind = TileKind.SOIL
    start = (game.player.x, game.player.y)
    game.day_position_history = [
        start,
        (start[0] + 20, start[1]),
        (start[0] + 40, start[1]),
    ]
    game.player.x, game.player.y = game.day_position_history[-1]

    game.begin_day_transition()
    game.update_day_transition(game_module.NIGHT_BUMPER_DURATION_SECONDS)
    assert game.objects[branch.object_id].active is False
    assert tile.kind is TileKind.SOIL
    game.update_day_transition(0.01)
    assert game.objects[branch.object_id].active is False

    for _ in range(100):
        game.update_day_transition(0.05)
        if game.day_transition_phase != "rewind":
            break

    assert game.objects[branch.object_id].active is True
    assert game.map.tile_map.tiles[tile_index].kind is TileKind.GRASSLAND


def test_game_clock_advances_during_play_but_pauses_at_morning() -> None:
    game = Game(fullscreen=False)
    game.pending_area_target = AreaTarget(
        "Till Grassland", (game.player.x, game.player.y)
    )
    start = game.day.current_time_minutes

    game.update(2.5)
    assert game.day.current_time_minutes == start + 2

    game.day.mode = Mode.MORNING
    game.update(10.0)
    assert game.day.current_time_minutes == start + 2


def test_time_speed_controls_scale_and_pause_clock() -> None:
    game = Game(fullscreen=False)
    game.pending_area_target = AreaTarget(
        "Till Grassland", (game.player.x, game.player.y)
    )
    start = game.day.current_time_minutes

    game.time_speed = 4.0
    game.update(1.0)
    assert game.day.current_time_minutes == start + 4

    game.time_speed = 0.5
    game.update(2.0)
    assert game.day.current_time_minutes == start + 5

    game.draw_top_bar()
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=game.pause_button.center))
    game.handle_events()
    game.update(10.0)
    assert game.simulation_paused is True
    assert game.time_speed == 0.5
    assert game.day.current_time_minutes == start + 5


def test_extreme_speed_processes_more_fixed_size_simulation_ticks(monkeypatch) -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.REPLAY
    game.simulation_paused = False
    game.time_speed = 40.0
    ticks: list[float] = []
    monkeypatch.setattr(game, "_update_simulation_tick", ticks.append)

    game.update(0.2)

    assert len(ticks) == 160
    assert set(ticks) == {FIXED_SIMULATION_TICK_SECONDS}


def test_idle_game_pauses_and_new_command_resumes_last_speed() -> None:
    game = Game(fullscreen=False)
    game.time_speed = 4.0

    game.update(1.0)
    assert game.simulation_paused is True
    assert game.time_speed == 4.0
    assert game.day.current_time_minutes == 360

    branch = next(
        obj
        for obj in game.objects.values()
        if "Gather" in obj.interactions and game.build_navigation_path(obj.center)
    )
    game.queue_job(branch.object_id, "Gather", record=True)
    assert game.pending_job is not None
    assert game.simulation_paused is False
    assert game.time_speed == 4.0


def test_p_toggles_pause_without_changing_time_speed() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.REPLAY
    game.simulation_paused = False
    game.time_speed = 20.0

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_p))
    game.handle_events()

    assert game.simulation_paused is True
    assert game.time_speed == 20.0

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_p))
    game.handle_events()

    assert game.simulation_paused is False
    assert game.time_speed == 20.0


def test_sun_remains_fully_inside_track_at_both_ends() -> None:
    assert sun_track_position(0.0, 180, 360, radius=8) == 188
    assert sun_track_position(1.0, 180, 360, radius=8) == 532


def test_time_controls_stay_above_map_between_sidebars() -> None:
    game = Game(fullscreen=False)
    game.draw_top_bar()

    assert game.pause_button.left >= MAP_LEFT
    assert game.pause_button.right <= MAP_RIGHT
    controls = [
        game.pause_button,
        game.step_button,
        game.speed_down_button,
        game.speed_display,
        game.speed_up_button,
    ]
    assert all(rect.left >= MAP_LEFT for rect in controls)
    assert all(rect.right <= MAP_RIGHT for rect in controls)
    assert all(rect.bottom <= TOP_BAR_HEIGHT for rect in controls)


def test_map_viewport_and_message_bar_fill_available_height() -> None:
    assert MAP_RIGHT - MAP_LEFT == MAP_BOTTOM - MAP_TOP == MAP_SIZE
    assert MAP_SIZE == 1024
    assert MAP_LEFT == WIDTH - MAP_RIGHT
    assert SIDEBAR_WIDTH == RIGHT_SIDEBAR_WIDTH
    assert MAP_TOP == TOP_BAR_HEIGHT
    assert MESSAGE_BAR.topleft == (MAP_LEFT, MAP_BOTTOM)
    assert MESSAGE_BAR.size == (MAP_SIZE, MESSAGE_BAR_HEIGHT)
    assert MESSAGE_BAR.bottom == HEIGHT


def test_plus_and_minus_keys_change_to_adjacent_speed() -> None:
    game = Game(fullscreen=False)
    game.time_speed = 1.0

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_PLUS))
    game.handle_events()
    assert game.time_speed == 2.0

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_MINUS))
    game.handle_events()
    assert game.time_speed == 1.0


def test_single_command_step_pauses_replay_before_the_next_order() -> None:
    game = Game(fullscreen=False)
    gatherable = [
        obj
        for obj in game.objects.values()
        if obj.active
        and "Gather" in obj.interactions
        and game.build_navigation_path(obj.center)
    ][:2]
    assert len(gatherable) == 2
    game.day.remembered_routine = [
        RoutineStep(obj.object_id, "Gather", obj.type_id, target_point=obj.center)
        for obj in gatherable
    ]
    game.day.mode = Mode.REPLAY
    game.day.replay_index = 0
    game.simulation_paused = True
    game.time_speed = 40.0

    game.start_single_command_step()
    for _ in range(4_000):
        game.update(0.05)
        if game.simulation_paused and game.day.replay_index == 1:
            break

    assert gatherable[0].active is False
    assert gatherable[1].active is True
    assert game.day.replay_index == 1
    assert game.simulation_paused is True
    game.update(10.0)
    assert gatherable[1].active is True
    assert game.day.replay_index == 1
    assert game.simulation_paused is True
