import os

import pygame

from remembering.game import Game, routine_step_editable_fields
from remembering.model import Mode, RoutineStep
from remembering.world import save_memory_file


def test_live_memory_editor_pauses_and_restores_play_state(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.simulation_paused = False

    game.open_memory_editor()

    assert game.adjusting_memory is True
    assert game.simulation_paused is True

    game.close_memory_editor()

    assert game.adjusting_memory is False
    assert game.simulation_paused is False


def test_ctrl_q_quits_while_command_editor_has_keyboard_focus(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.open_memory_editor()
    game.new_command_set()

    pygame.event.post(
        pygame.event.Event(
            pygame.KEYDOWN,
            key=pygame.K_q,
            unicode="q",
            mod=pygame.KMOD_CTRL,
        )
    )
    game.handle_events()

    assert game.running is False
    assert game.memory_edit_buffer == ""


def test_memory_editor_can_edit_every_command_field(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [RoutineStep(None, "Move To")]
    game.open_memory_editor()

    game.memory_edit_field = "area_bounds"
    game.memory_edit_buffer = (
        '{"start":{"tilexy":[0,0],"subtilexy":[1,2]},'
        '"end":{"tilexy":[0,0],"subtilexy":[3,4]}}'
    )
    game.commit_memory_field()
    game.memory_edit_field = "action"
    game.memory_edit_buffer = '"Gather Pebbles"'
    game.commit_memory_field()

    step = game.day.remembered_routine[0]
    assert step.area_bounds == (1, 2, 3, 4)
    assert step.action == "Gather Pebbles"


def test_memory_editor_mutations_preserve_the_next_replay_command(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    first = RoutineStep(None, "Gather Pebbles")
    next_step = RoutineStep(None, "Gather Branches")
    game.day.remembered_routine = [first, next_step]
    game.day.mode = Mode.REPLAY
    game.day.replay_index = 1
    game.open_memory_editor()

    game.move_memory_step(-1)

    assert game.day.remembered_routine[game.day.replay_index] == next_step
    game.duplicate_memory_step()
    assert game.day.remembered_routine[game.day.replay_index] == next_step
    game.remove_memory_step()
    assert game.day.replay_index <= len(game.day.remembered_routine)


def test_memory_editor_can_add_and_duplicate_commands(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine.clear()
    game.open_memory_editor()

    game.handle_memory_editor_key(pygame.K_n, 0)
    game.duplicate_memory_step()

    assert [step.action for step in game.day.remembered_routine] == [
        "Move To",
        "Move To",
    ]


def test_new_set_button_clears_commands_and_focuses_blank_name(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [RoutineStep(None, "Gather Pebbles")]
    game.memory_file_name = "Old Chores"
    game.open_memory_editor()
    game.draw()

    new_set_button = next(
        rect for rect, action in game.memory_editor_buttons if action == "New Set"
    )
    game.handle_memory_editor_click(new_set_button.center)

    assert game.day.remembered_routine == []
    assert game.day.replay_index == 0
    assert game.memory_edit_index == 0
    assert game.memory_file_name == ""
    assert game.memory_edit_field == "__memory_file_name__"
    assert game.memory_edit_buffer == ""


def test_save_button_commits_a_new_name_without_requiring_enter(
    tmp_path, monkeypatch
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.open_memory_editor()
    game.draw()
    game.new_command_set()
    game.memory_edit_buffer = "Morning Farm Chores"
    saved_names: list[str] = []
    monkeypatch.setattr(game, "save_named_memory", saved_names.append)

    save_button = next(
        rect for rect, action in game.memory_editor_buttons if action == "Save"
    )
    game.handle_memory_editor_click(save_button.center)

    assert game.memory_file_name == "Morning Farm Chores"
    assert game.memory_edit_field is None
    assert saved_names == ["Morning Farm Chores"]


def test_load_opens_three_column_browser_with_favorites_and_recent_order(
    tmp_path,
) -> None:
    memory_directory = tmp_path / "memories"
    older = save_memory_file(
        "Older", [RoutineStep(None, "Move To")], tile_size=64, directory=memory_directory
    )
    newer = save_memory_file(
        "Newer", [RoutineStep(None, "Gather Pebbles")], tile_size=64, directory=memory_directory
    )
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    game = Game(
        fullscreen=False,
        persistence_path=tmp_path / "current_level.jsonc",
        memory_directory=memory_directory,
    )
    game.toggle_memory_favorite("Older")
    game.open_memory_editor()
    game.draw()

    load_button = next(
        rect for rect, action in game.memory_editor_buttons if action == "Load"
    )
    game.handle_memory_editor_click(load_button.center)
    game.draw()

    assert game.memory_browser_open is True
    assert game.memory_favorites == {"Older"}
    assert game.memory_favorites_path.is_file()
    assert [name for _rect, name in game.memory_browser_rows[:3]] == [
        "Older",
        "Newer",
        "Older",
    ]
    assert {name for _rect, name in game.memory_browser_rows} == {"Older", "Newer"}
    assert all(
        action != "Save Homestead" for _rect, action in game.memory_editor_buttons
    )


def test_record_macro_returns_to_play_and_appends_to_working_set(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    existing = RoutineStep(None, "Gather Pebbles")
    recorded = RoutineStep(None, "Gather Branches")
    earlier_today = RoutineStep(None, "Move To")
    game.day.remembered_routine = [existing]
    game.day.today_routine = [earlier_today]
    game.open_memory_editor()
    game.draw()

    record_button = next(
        rect for rect, action in game.memory_editor_buttons if action == "Record Macro"
    )
    game.handle_memory_editor_click(record_button.center)

    assert game.adjusting_memory is False
    assert game.macro_recording is True
    game.day.today_routine.append(recorded)
    assert game.day.remembered_routine == [existing, recorded]

    assert game.stop_macro_recording() is True
    assert game.macro_recording is False
    assert game.day.today_routine == [earlier_today, recorded]


def test_macros_player_tab_offers_start_stop_and_edit(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.inventory_page = game.inventory_page.MACROS
    game.draw_ui()

    assert [action for _rect, action in game.macro_buttons] == ["Start", "Edit"]
    game.start_macro_recording()
    game.draw_ui()
    assert [action for _rect, action in game.macro_buttons] == ["Stop", "Edit"]

    game.stop_macro_recording()
    game.open_memory_editor()
    assert game.adjusting_memory is True


def test_command_set_can_be_launched_during_direct_play(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(game.player.x, game.player.y))
    ]
    game.memory_file_name = "Farm Chores"
    game.open_memory_editor()

    assert game.run_command_set("Farm Chores") is True
    assert game.adjusting_memory is False
    assert game.day.mode is Mode.REPLAY
    assert game.replay_outcome == "command_set"
    assert game.simulation_paused is False

    for _ in range(10):
        game.update(0.05)
        if game.day.mode is Mode.DIRECT:
            break

    assert game.day.mode is Mode.DIRECT
    assert game.running_command_set_name is None
    assert game.messages[-1] == "Command set 'Farm Chores' complete."


def test_memory_editor_fields_are_command_specific() -> None:
    gather = RoutineStep(None, "Gather Pebbles", quantity=4)
    till = RoutineStep(None, "Till Grassland")
    object_job = RoutineStep(4, "Craft Crude Hoe", "workbench")

    assert routine_step_editable_fields(gather) == (
        "action",
        "area_bounds",
        "quantity",
        "target_areas",
        "nearest_to_player",
    )
    assert routine_step_editable_fields(till) == (
        "action",
        "area_bounds",
        "quantity",
        "target_areas",
        "max_game_minutes",
        "till_until_done",
    )
    assert routine_step_editable_fields(object_job) == (
        "action",
        "target_id",
        "target_type",
        "target_point",
    )


def test_memory_editor_keeps_populated_nonstandard_fields_visible() -> None:
    step = RoutineStep(
        None,
        "Move To",
        target_point=(10.0, 20.0),
        target_build_memory="legacy-link",
    )

    assert "target_build_memory" in routine_step_editable_fields(step)


def test_editor_avatar_preview_follows_the_selected_command(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(400.0, 500.0)),
        RoutineStep(None, "Gather Pebbles", area_bounds=(800, 900, 1000, 1100)),
    ]
    game.open_memory_editor()

    game.memory_edit_index = 0
    assert game.command_editor_preview_position() == (400.0, 500.0)
    game.memory_edit_index = 1
    assert game.command_editor_preview_position() == (900.0, 1000.0)


def test_editor_preview_does_not_move_the_live_camera(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(3000.0, 3200.0))
    ]
    game.open_memory_editor()
    original_camera = (game.camera.x, game.camera.y)

    game.draw()

    assert (game.camera.x, game.camera.y) == original_camera
