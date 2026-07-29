import math

from remembering.game import Game
from remembering.model import Mode, RoutineStep


def test_clicking_player_queues_nearest_gather_targets(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.active_command = "Gather Pebbles"
    game.area_command_quantity = 3
    game.command_drag_start = (round(game.player.x), round(game.player.y))
    screen_position = game.camera.world_to_screen((game.player.x, game.player.y))

    game.finish_command_drag(screen_position)

    assert len(game.area_targets) == 3
    distances = [
        math.dist((game.player.x, game.player.y), target.point)
        for target in game.area_targets
    ]
    assert distances == sorted(distances)
    remembered = game.day.today_routine[-1]
    assert remembered.action == "Gather Pebbles"
    assert remembered.quantity == 3
    assert remembered.nearest_to_player is True
    assert remembered.area_bounds is None


def test_nearest_gather_replay_rescans_from_current_player_location(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.day.mode = Mode.REPLAY
    game.player.x = game.map.width - 256
    game.player.y = game.map.height // 2
    game.day.remembered_routine = [
        RoutineStep(
            None,
            "Gather Branches",
            quantity=2,
            nearest_to_player=True,
        )
    ]

    game.queue_nearest_gather_command("Gather Branches", 2, record=False)

    assert len(game.area_targets) <= 2
    distances = [
        math.dist((game.player.x, game.player.y), target.point)
        for target in game.area_targets
    ]
    assert distances == sorted(distances)
