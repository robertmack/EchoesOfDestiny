import pygame

from remembering.model import DayState, day_progress_ratio, format_clock_time
from remembering.game import (
    AreaTarget,
    FIXED_SIMULATION_TICK_SECONDS,
    Game,
    MAP_LEFT,
    MAP_RIGHT,
    sun_track_position,
)
from remembering.model import Mode


def test_clock_starts_at_six_am() -> None:
    day = DayState()
    assert format_clock_time(day.current_time_minutes) == "6:00 AM"


def test_day_progress_ratio_clamps_across_the_day() -> None:
    assert day_progress_ratio(360) == 0.0
    assert day_progress_ratio(1320) == 1.0
    assert day_progress_ratio(840) > 0.4
    assert day_progress_ratio(840) < 0.6


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

    assert game.time_speed_buttons
    assert game.pause_button.left >= MAP_LEFT
    assert game.pause_button.right <= MAP_RIGHT
    assert all(rect.left >= MAP_LEFT for rect, _ in game.time_speed_buttons)
    assert all(rect.right <= MAP_RIGHT for rect, _ in game.time_speed_buttons)
    assert all(rect.bottom <= 46 for rect, _ in game.time_speed_buttons)
    assert [speed for _, speed in game.time_speed_buttons][-2:] == [20.0, 40.0]
