import pygame

from remembering.model import DayState, day_progress_ratio, format_clock_time
from remembering.game import AreaTarget, Game, sun_track_position
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


def test_idle_game_pauses_and_new_command_resumes_last_speed() -> None:
    game = Game(fullscreen=False)
    game.time_speed = 4.0

    game.update(1.0)
    assert game.simulation_paused is True
    assert game.time_speed == 4.0
    assert game.day.current_time_minutes == 360

    stick = game.object_of_type("stick")
    game.queue_job(stick.object_id, "Gather", record=True)
    assert game.pending_job is not None
    assert game.simulation_paused is False
    assert game.time_speed == 4.0


def test_sun_remains_fully_inside_track_at_both_ends() -> None:
    assert sun_track_position(0.0, 180, 360, radius=8) == 188
    assert sun_track_position(1.0, 180, 360, radius=8) == 532


def test_time_controls_stay_above_map_between_sidebars() -> None:
    game = Game(fullscreen=False)
    game.draw_top_bar()

    assert game.time_speed_buttons
    assert all(rect.left >= 190 for rect, _ in game.time_speed_buttons)
    assert all(rect.right <= 880 for rect, _ in game.time_speed_buttons)
    assert all(rect.bottom <= 46 for rect, _ in game.time_speed_buttons)
