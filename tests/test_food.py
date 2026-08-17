import pygame
import pytest

import remembering.game as game_module
from remembering.game import AreaTarget, Game, PendingJob, object_action_menu_options
from remembering.model import Mode, RoutineStep
from remembering.tiles import TileKind


def test_prep_station_turns_grain_and_water_into_terrible_porridge(
    tmp_path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    station = game.object_of_type("food_prep_station")
    game.player.inventory["grains"] = 3
    bucket = game.create_carried_object("bucket", quality=50)
    game.player.bucket_water_uses = 1

    assert "Prepare Porridge" in object_action_menu_options(station, game.player)
    game.complete_job(PendingJob(station.object_id, "Prepare Porridge"))

    porridge = next(
        obj for obj in game.player.carried_objects if obj.type_id == "porridge"
    )
    assert game.player.inventory["grains"] == 0
    assert game.player.bucket_water_uses == 0
    assert porridge.quality == 10
    assert porridge.quality_stage == "ruined"
    assert porridge.traits == ("edible",)
    assert porridge.nutrition == 30


def test_porridge_can_be_eaten_at_table_for_its_nutrition(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    table = game.object_of_type("table")
    porridge = game.create_carried_object("porridge", quality=10)
    game.player.hunger = 20

    assert "Eat Porridge" in object_action_menu_options(table, game.player)
    game.complete_job(PendingJob(table.object_id, "Eat Porridge"))

    assert game.player.hunger == 50
    assert porridge.active is False
    assert porridge not in game.player.carried_objects


def test_harvested_berries_are_carried_edible_objects(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    bush = game.object_of_type("bush")
    table = game.object_of_type("table")

    game.complete_job(PendingJob(bush.object_id, "Harvest Berries"))

    berry = next(obj for obj in game.player.carried_objects if obj.type_id == "berry")
    assert bush.active is True
    assert bush.state["has_berries"] is False
    assert game.player.skills["harvesting"].experience == 1
    assert "Harvest Berries" not in object_action_menu_options(bush, game.player)
    assert "Pull Berry Bush" in object_action_menu_options(bush, game.player)
    assert berry.traits == ("edible",)
    assert berry.nutrition == 8
    assert "Eat Berries" in object_action_menu_options(table, game.player)


def test_berry_bush_offers_combined_harvest_and_eat_action(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    bush = game.object_of_type("bush")
    game.player.conditions["hunger"] = 50

    assert "Harvest and Eat Berries" in object_action_menu_options(bush, game.player)

    game.complete_job(PendingJob(bush.object_id, "Harvest and Eat Berries"))

    assert bush.state["has_berries"] is False
    assert game.player.conditions["hunger"] < 50
    assert not any(obj.type_id == "berry" for obj in game.player.carried_objects)
    assert game.player.skills["harvesting"].experience == 1


def test_right_clicking_inventory_berries_opens_eat_context_menu(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    berry = game.create_carried_object("berry")
    game.player.conditions["hunger"] = 50
    game.player.conditions["thirst"] = 50
    game.draw_player_dock()
    berry_rect = next(
        rect
        for rect, carried in game.inventory_food_buttons
        if carried is berry
    )

    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=3,
            pos=berry_rect.center,
        )
    )
    game.handle_events()

    assert berry.active is True
    assert berry in game.player.carried_objects
    assert game.context_menu_options == ["Eat Berries"]

    game.activate_context_option(0)

    assert berry.active is False
    assert berry not in game.player.carried_objects
    assert game.player.conditions["hunger"] == 42
    assert game.player.conditions["thirst"] == 47
    assert "from inventory" in game.messages[-1]


def test_eating_inventory_berries_is_recorded_and_replayed(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    berry = game.create_carried_object("berry")
    game.context_inventory_item_id = berry.object_id
    game.context_menu_options = ["Eat Berries"]

    game.activate_context_option(0)

    assert game.day.today_routine[-1] == RoutineStep(None, "Eat Berries", "berry")

    replay_berry = game.create_carried_object("berry")
    game.day.remembered_routine = [RoutineStep(None, "Eat Berries", "berry")]
    game.day.replay_index = 0
    game.day.mode = Mode.REPLAY
    game.simulation_paused = False
    game.update(0.05)

    assert replay_berry.active is False
    assert game.day.replay_index == 1


def test_pulling_a_harvested_bush_does_not_create_more_berries(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    bush = game.object_of_type("bush")

    game.complete_job(PendingJob(bush.object_id, "Harvest Berries"))
    berries_before_pull = sum(
        obj.type_id == "berry" for obj in game.player.carried_objects
    )
    game.complete_job(PendingJob(bush.object_id, "Pull Berry Bush"))

    assert bush.active is False
    assert sum(obj.type_id == "berry" for obj in game.player.carried_objects) == berries_before_pull


def test_pond_illness_is_delayed_then_causes_vomiting(monkeypatch, tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    delays = iter((120, 30))
    monkeypatch.setattr(game_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(game_module.random, "randint", lambda *_args: next(delays))
    game.day.current_time_minutes = 6 * 60
    game.player.conditions.update({"trauma": 10, "hunger": 10, "thirst": 10})

    game.attempt_illness_exposures({"contaminated_water": 0.20})
    illness = game.active_illnesses["contaminated_water"]
    assert illness.onset_minute == 8 * 60
    assert game.visible_illness_factors() == []

    game.day.current_time_minutes = 8 * 60
    game.update_illnesses(1)
    assert game.thought_bubble_text == "I feel ill..."
    assert game.player_looks_ill() is True
    assert game.visible_illness_factors()[0][1] == 60

    game.day.current_time_minutes = 8 * 60 + 30
    game.update_illnesses(1)
    assert game.player.conditions["trauma"] == 13
    assert game.player.conditions["hunger"] == 18
    assert game.player.conditions["thirst"] == 22
    assert "BLEURGH" in (game.thought_bubble_text or "")
    assert illness.value == 30
    assert game.vomiting_timer_seconds == game_module.VOMITING_DURATION_SECONDS


def test_vomiting_pauses_then_resumes_the_current_action(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    branch = game.object_of_type("branch")
    game.player.x, game.player.y = branch.center
    game.pending_job = PendingJob(branch.object_id, "Gather", branch.center)
    game.job_timer = 0.0
    game.vomiting_timer_seconds = 0.1

    game._update_simulation_tick(0.05)
    assert game.pending_job is not None
    assert game.job_timer == 0.0
    assert game.vomiting_timer_seconds == pytest.approx(0.05)

    game._update_simulation_tick(0.05)
    assert game.pending_job is not None
    assert game.job_timer == 0.0
    assert game.vomiting_timer_seconds == 0.0

    game._update_simulation_tick(0.05)
    assert game.pending_job is not None
    assert game.job_timer > 0.0


def test_only_pond_drinking_rolls_for_illness(monkeypatch, tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    assert game.map.tile_illness_exposures[TileKind.POND] == {
        "contaminated_water": 0.20
    }
    assert game.map.tile_illness_exposures[TileKind.SHALLOW_WATER] == {
        "contaminated_water": 0.0
    }
    assert (
        game.map.object_types["barrel"]
        .form_definition()
        .illness_exposures
        == {"contaminated_water": 0.0}
    )
    rolls = 0

    def record_roll(_exposures) -> None:
        nonlocal rolls
        rolls += 1

    monkeypatch.setattr(
        game,
        "attempt_illness_exposures",
        record_roll,
    )
    tile_map = game.map.tile_map
    pond_index = next(
        index for index, tile in enumerate(tile_map.tiles) if tile.kind is TileKind.POND
    )
    pond_point = tile_map.tile_center(
        pond_index % tile_map.columns, pond_index // tile_map.columns
    )
    game.player.x, game.player.y = pond_point
    game.pending_area_target = AreaTarget("Drink Water", pond_point)

    game._update_simulation_tick(0.0)

    assert rolls == 1


def test_stats_hide_incubating_illness_until_symptoms_appear(
    monkeypatch, tmp_path
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    shown: list[str] = []
    monkeypatch.setattr(game, "text", lambda value, *_args, **_kwargs: shown.append(value))
    monkeypatch.setattr(game_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(game_module.random, "randint", lambda *_args: 120)
    game.attempt_illness_exposures({"contaminated_water": 1.0})

    game.draw_player_dock()
    assert "Contaminated Water: 60%" not in shown

    shown.clear()
    game.active_illnesses["contaminated_water"].revealed = True
    game.draw_player_dock()
    assert "Contaminated Water: 60%" in shown


def test_raw_berries_have_a_separate_milder_food_poisoning(monkeypatch, tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    water_illness = game.illness_types["contaminated_water"]
    berry_illness = game.illness_types["raw_berry_food_poisoning"]
    assert water_illness.incubation_minutes == (120, 240)
    assert berry_illness.incubation_minutes == (5, 15)
    assert berry_illness.effect_type == "vomit"
    assert berry_illness.effect_grace_minutes == (2, 10)
    assert berry_illness.effect_clear_rate == 1.0
    assert water_illness.effect_clear_rate == 0.5
    berry_exposures = (
        game.map.object_types["berry"].form_definition().illness_exposures
    )
    assert berry_exposures == {"raw_berry_food_poisoning": 0.02}
    monkeypatch.setattr(game_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(game_module.random, "randint", lambda *_args: 30)

    berry = game.create_carried_object("berry")
    game.consume_carried_food(berry, "from inventory")
    game.attempt_illness_exposures({"contaminated_water": 1.0})

    assert set(game.active_illnesses) == {
        "contaminated_water",
        "raw_berry_food_poisoning",
    }
    assert game.active_illnesses["contaminated_water"].value == 60
    assert game.active_illnesses["raw_berry_food_poisoning"].value == 20
    berry_case = game.active_illnesses["raw_berry_food_poisoning"]
    berry_case.revealed = True
    berry_case.next_vomit_check_minute = game.day.current_time_minutes

    game.update_illnesses(1)

    assert "raw_berry_food_poisoning" not in game.active_illnesses
    assert "contaminated_water" in game.active_illnesses


def test_mature_wheat_harvest_yields_between_three_and_five_grains(
    tmp_path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")
    crop.state["growth_progress"] = 1.0
    game.apply_object_form(crop, "mature")

    game.complete_job(PendingJob(crop.object_id, "Harvest Wheat"))

    assert 3 <= game.player.inventory["grains"] <= 5
    assert game.player.skills["harvesting"].experience == 1


def test_cupboard_is_buildable_and_stores_edible_entities(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.player.inventory.update({"wood": 4, "fiber": 2})
    target = game.build_area_targets(
        "Build Cupboard", (0, 0, game.map.width, game.map.height)
    )[0]

    cupboard = game.build_fixed_object(
        "cupboard", target.placement_point or target.point
    )
    porridge = game.create_carried_object("porridge", quality=10)

    assert cupboard.type_id == "cupboard"
    assert cupboard.blocks_movement is True
    assert cupboard.capacity["food"] == 10
    assert game.player.inventory["wood"] == 0
    assert game.player.inventory["fiber"] == 0
    assert "Store Food" in object_action_menu_options(cupboard, game.player)

    game.complete_job(PendingJob(cupboard.object_id, "Store Food"))
    assert porridge.container == f"object:{cupboard.object_id}"
    assert porridge not in game.player.carried_objects
    assert "Take Food" in object_action_menu_options(cupboard, game.player)

    game.complete_job(PendingJob(cupboard.object_id, "Take Food"))
    assert porridge.container == "player"
    assert porridge in game.player.carried_objects
