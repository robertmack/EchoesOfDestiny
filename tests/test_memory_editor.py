import pygame

from remembering.game import Game, routine_step_editable_fields
from remembering.model import Mode, RoutineStep


def test_live_memory_editor_pauses_and_restores_play_state(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.simulation_paused = False

    game.open_memory_editor()

    assert game.adjusting_memory is True
    assert game.simulation_paused is True

    game.close_memory_editor()

    assert game.adjusting_memory is False
    assert game.simulation_paused is False


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
