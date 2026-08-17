import os
import math

import pygame
import pytest

from remembering.game import Game, routine_step_editable_fields
from remembering.model import Mode, RoutineStep
from remembering.ui_layout import COMMAND_EDITOR_MAP_RECT
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


def test_command_condition_can_be_added_and_evaluated() -> None:
    game = Game(fullscreen=False)
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(game.player.x, game.player.y))
    ]
    game.memory_edit_index = 0

    game.toggle_memory_step_condition()
    step = game.day.remembered_routine[0]

    assert [item.action for item in game.day.remembered_routine] == [
        "__if__",
        "Move To",
        "__end__",
    ]
    assert step.condition_kind == "time"
    assert game.routine_condition_label(step) == "If time >= 12:00 PM"
    game.day.current_time_minutes = 11 * 60
    assert game.routine_step_condition_met(step) is False
    game.day.current_time_minutes = 13 * 60
    assert game.routine_step_condition_met(step) is True

    game.memory_edit_index = 1
    game.add_memory_loop()
    assert [game.routine_step_depth(index) for index in range(5)] == [0, 1, 2, 1, 0]
    assert game.matching_routine_end(0) == 4
    assert game.matching_routine_end(1) == 3
    assert game.matching_routine_opener(3) == 1


def test_repeat_until_block_skips_when_its_condition_is_met() -> None:
    game = Game(fullscreen=False)
    game.day.remembered_routine = [
        RoutineStep(
            None,
            "__repeat_until__",
            condition_kind="inventory",
            condition_subject="pebble",
            condition_operator=">=",
            condition_value=1,
        ),
        RoutineStep(None, "Move To", target_point=(game.player.x, game.player.y)),
        RoutineStep(None, "__end__"),
    ]
    game.player.inventory["pebble"] = 1
    game.day.mode = Mode.REPLAY
    game.day.replay_index = 0

    game._update_simulation_tick(0)

    assert game.day.replay_index == 3


def test_repeat_exits_when_no_reachable_command_is_executable() -> None:
    game = Game(fullscreen=False)
    for obj in game.objects.values():
        if obj.type_id == "bush":
            obj.state["has_berries"] = False
    game.day.remembered_routine = [
        RoutineStep(
            None,
            "__repeat_until__",
            condition_kind="stat",
            condition_subject="hunger",
            condition_operator="<=",
            condition_value=10,
        ),
        RoutineStep(None, "Harvest and Eat Berries", "bush", nearest_to_player=True),
        RoutineStep(None, "__end__"),
        RoutineStep(None, "Move To", target_point=(game.player.x, game.player.y)),
    ]
    game.player.conditions["hunger"] = 80
    game.day.mode = Mode.REPLAY
    game.day.replay_index = 0

    game._update_simulation_tick(0)

    assert game.day.replay_index == 3
    assert game.messages[-1] == (
        "Exited repeat: none of its commands are currently executable."
    )


def test_nearest_berry_target_uses_the_closest_valid_path() -> None:
    game = Game(fullscreen=False)
    game.player.x, game.player.y = 7456.0, 5344.0
    bushes = [
        obj for obj in game.objects.values() if obj.active and obj.type_id == "bush"
    ]
    reachable = [obj for obj in bushes if game.build_navigation_path_to_object(obj)]
    unreachable = [obj for obj in bushes if not game.build_navigation_path_to_object(obj)]
    assert reachable and unreachable
    blocked = min(unreachable, key=lambda obj: math.dist(obj.center, (7456.0, 5344.0)))
    valid = min(reachable, key=lambda obj: math.dist(obj.center, (7456.0, 5344.0)))
    for obj in bushes:
        obj.state["has_berries"] = obj.object_id in {
            blocked.object_id,
            valid.object_id,
        }
    step = RoutineStep(
        None, "Harvest and Eat Berries", "bush", nearest_to_player=True
    )

    assert game.berry_routine_target(step) is valid


def test_berry_job_does_not_skip_the_repeat_end(monkeypatch) -> None:
    game = Game(fullscreen=False)
    bush = game.object_of_type("bush")
    game.day.remembered_routine = [
        RoutineStep(
            None,
            "__repeat_until__",
            condition_kind="inventory",
            condition_subject="pebble",
            condition_operator=">=",
            condition_value=1,
        ),
        RoutineStep(None, "Harvest and Eat Berries", "bush", nearest_to_player=True),
        RoutineStep(None, "__end__"),
    ]
    game.day.mode = Mode.REPLAY
    game.day.replay_index = 1
    queued: list[tuple[int, str]] = []
    monkeypatch.setattr(game, "berry_routine_target", lambda _step: bush)
    monkeypatch.setattr(
        game,
        "queue_job",
        lambda target_id, action, **_kwargs: queued.append((target_id, action)) or True,
    )

    game._update_simulation_tick(0)

    assert queued == [(bush.object_id, "Harvest and Eat Berries")]
    assert game.day.replay_index == 1

    game.day.replay_index = 2
    game._update_simulation_tick(0)
    assert game.day.replay_index == 0
    game._update_simulation_tick(0)
    assert game.day.replay_index == 1


def test_routine_numbering_restarts_inside_blocks_and_counts_controls() -> None:
    game = Game(fullscreen=False)
    game.day.remembered_routine = [
        RoutineStep(None, "__repeat_until__", condition_kind="time", condition_value=720),
        RoutineStep(None, "Gather Pebbles"),
        RoutineStep(None, "__if__", condition_kind="time", condition_value=720),
        RoutineStep(None, "Gather Branches"),
        RoutineStep(None, "__end__"),
        RoutineStep(None, "Gather Tall Grass"),
        RoutineStep(None, "__end__"),
        RoutineStep(None, "Move To", target_point=(1.0, 1.0)),
    ]

    assert [game.routine_step_number(index) for index in (0, 1, 2, 3, 5, 7)] == [
        1,
        1,
        2,
        1,
        3,
        2,
    ]
    assert game.routine_step_display_label(0).startswith("1. Repeat until")
    assert game.routine_step_display_label(2).startswith("2. If")
    assert game.routine_step_display_label(4) == "End If"


def test_tab_and_shift_tab_move_commands_across_nest_levels() -> None:
    game = Game(fullscreen=False)
    game.day.remembered_routine = [
        RoutineStep(
            None,
            "__if__",
            condition_kind="time",
            condition_subject="time",
            condition_value=720,
        ),
        RoutineStep(None, "Gather Pebbles"),
        RoutineStep(None, "__end__"),
        RoutineStep(None, "Gather Branches"),
    ]
    game.memory_edit_index = 3

    game.handle_memory_editor_key(pygame.K_TAB, 0)

    assert [step.action for step in game.day.remembered_routine] == [
        "__if__",
        "Gather Pebbles",
        "Gather Branches",
        "__end__",
    ]
    assert game.routine_step_depth(game.memory_edit_index) == 1

    game.handle_memory_editor_key(pygame.K_TAB, pygame.KMOD_SHIFT)

    assert [step.action for step in game.day.remembered_routine] == [
        "__if__",
        "Gather Pebbles",
        "__end__",
        "Gather Branches",
    ]
    assert game.routine_step_depth(game.memory_edit_index) == 0


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
        rect for rect, action in game.memory_editor_buttons if action == "Record Routine"
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

    assert [action for _rect, action in game.macro_buttons] == [
        "Macro Dropdown",
        "Play",
        "Record",
        "Edit",
    ]
    game.start_macro_recording()
    game.draw_ui()
    assert [action for _rect, action in game.macro_buttons] == [
        "Macro Dropdown",
        "Play",
        "Stop",
        "Edit",
    ]

    game.stop_macro_recording()
    game.open_memory_editor()
    assert game.adjusting_memory is True


def test_macros_tab_dropdown_selects_and_plays_saved_macro(tmp_path) -> None:
    memory_directory = tmp_path / "memories"
    save_memory_file(
        "Morning Walk",
        [RoutineStep(None, "Move To", target_point=(64.0, 64.0))],
        tile_size=64,
        directory=memory_directory,
    )
    game = Game(
        fullscreen=False,
        persistence_path=tmp_path / "current_level.jsonc",
        memory_directory=memory_directory,
    )
    game.inventory_page = game.inventory_page.MACROS
    game.draw_ui()
    dropdown = next(
        rect for rect, action in game.macro_buttons if action == "Macro Dropdown"
    )
    pygame.event.post(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=dropdown.center)
    )
    game.handle_events()
    game.draw_ui()

    assert any(name == "__new__" for _rect, name in game.macro_dropdown_buttons)

    option = next(
        rect for rect, name in game.macro_dropdown_buttons if name == "Morning Walk"
    )
    pygame.event.post(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=option.center)
    )
    game.handle_events()
    assert game.memory_file_name == "Morning Walk"

    game.draw_ui()
    play = next(rect for rect, action in game.macro_buttons if action == "Play")
    pygame.event.post(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=play.center)
    )
    game.handle_events()
    assert game.running_command_set_name == "Morning Walk"
    assert game.day.mode is Mode.REPLAY


def test_new_routine_dropdown_entry_opens_blank_named_editor(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [RoutineStep(None, "Gather Pebbles")]

    game.select_routine_dropdown_entry("__new__")

    assert game.adjusting_memory is True
    assert game.day.remembered_routine == []
    assert game.memory_file_name == ""
    assert game.memory_edit_field == "__memory_file_name__"


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
        "target_type",
        "target_point",
    )


def test_memory_editor_hides_fields_that_do_not_apply_to_the_command() -> None:
    step = RoutineStep(
        None,
        "Move To",
        target_point=(10.0, 20.0),
        target_build_memory="legacy-link",
    )

    assert routine_step_editable_fields(step) == ("action", "target_point")


def test_command_dropdown_changes_action_and_refreshes_applicable_fields(
    tmp_path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(10.0, 20.0))
    ]
    game.open_memory_editor()
    game.draw()
    action_field = next(
        rect for rect, field_name in game.memory_editor_fields if field_name == "action"
    )

    game.handle_memory_editor_click(action_field.center)
    game.draw()
    till_option = next(
        rect
        for rect, field_name, value in game.memory_field_dropdown_buttons
        if field_name == "action" and value == "Till Grassland"
    )
    game.handle_memory_editor_click(till_option.center)

    step = game.day.remembered_routine[0]
    assert step.action == "Till Grassland"
    assert step.target_point is None
    assert routine_step_editable_fields(step) == (
        "action",
        "area_bounds",
        "quantity",
        "target_areas",
        "max_game_minutes",
        "till_until_done",
    )
    assert game.memory_field_choices("till_until_done") == [
        ("True", True),
        ("False", False),
    ]


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


def test_move_to_target_field_arms_and_accepts_a_map_destination(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(3000.0, 3200.0))
    ]
    game.open_memory_editor()
    game.draw()
    target_field = next(
        rect
        for rect, field_name in game.memory_editor_fields
        if field_name == "target_point"
    )

    game.handle_memory_editor_click(target_field.center)
    assert game.memory_map_field_selection == "target_point"

    destination = game.memory_editor_world_at(COMMAND_EDITOR_MAP_RECT.center)
    game.handle_memory_editor_click(COMMAND_EDITOR_MAP_RECT.center)

    assert game.day.remembered_routine[0].target_point == destination
    assert game.memory_map_field_selection is None


def test_command_editor_map_can_pan_and_zoom_without_moving_live_camera(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(None, "Move To", target_point=(3000.0, 3200.0))
    ]
    live_camera = (game.camera.x, game.camera.y, game.camera.zoom)
    game.open_memory_editor()
    game.draw()
    game.memory_editor_map_camera = (3000.0, 3000.0, 1.0)

    game.pan_memory_editor_map((100, 50))
    panned = game.memory_editor_map_camera
    assert panned is not None
    assert panned[:2] != (3000.0, 3000.0)

    anchor_before = game.memory_editor_world_at(COMMAND_EDITOR_MAP_RECT.center)
    game.zoom_memory_editor_map(1, COMMAND_EDITOR_MAP_RECT.center)
    anchor_after = game.memory_editor_world_at(COMMAND_EDITOR_MAP_RECT.center)
    assert anchor_after == pytest.approx(anchor_before)

    game.draw()
    assert (game.camera.x, game.camera.y, game.camera.zoom) == live_camera


def test_berry_routine_uses_a_target_mode_instead_of_editable_target_type() -> None:
    specific = RoutineStep(
        12,
        "Harvest and Eat Berries",
        "bush",
        target_point=(100.0, 120.0),
    )
    nearest = RoutineStep(
        None,
        "Harvest and Eat Berries",
        "bush",
        nearest_to_player=True,
    )
    area = RoutineStep(
        None,
        "Harvest and Eat Berries",
        "bush",
        area_bounds=(0, 0, 200, 200),
        target_areas=((0, 0, 200, 200),),
    )

    assert routine_step_editable_fields(specific) == (
        "action",
        "target_mode",
        "target_point",
    )
    assert routine_step_editable_fields(nearest) == ("action", "target_mode")
    assert routine_step_editable_fields(area) == (
        "action",
        "target_mode",
        "area_bounds",
    )


def test_berry_target_mode_choices_set_internal_bush_targeting(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.remembered_routine = [
        RoutineStep(12, "Harvest and Eat Berries", "wrong type")
    ]

    assert game.memory_field_choices("target_mode") == [
        ("Nearest", "nearest"),
        ("Target", "specific"),
        ("Area", "area"),
    ]

    game.select_memory_field_choice("target_mode", "nearest")
    step = game.day.remembered_routine[0]
    assert step.target_type == "bush"
    assert step.nearest_to_player is True
    assert step.target_id is None
