from pathlib import Path

import pytest

from remembering.game import AreaTarget, Game, crop_inspection_lines
from remembering.model import LevelTileState
from remembering.tiles import TileKind
from remembering.world import advance_level_tile_states


def test_till_progress_converts_grassland_at_one_hundred_percent(tmp_path: Path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    target = next(
        target
        for target in game.build_area_targets(
            "Till Grassland", (0, 0, game.map.width, game.map.height)
        )
        if not target.prerequisite_target_ids
    )
    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    state = LevelTileState(
        column,
        row,
        till_percentage=99.95,
        persistence_modifier=1.0,
    )
    game.map.tile_states[(column, row)] = state
    game.map.till_progress_per_action = 1.0
    game.player.x, game.player.y = target.point
    game.pending_area_target = target
    game.simulation_paused = False

    for _ in range(500):
        game.update(0.05)
        if game.pending_area_target is None:
            break

    assert state.till_percentage == 100.0
    assert state.kind_override == TileKind.SOIL.value
    assert state.soil_persistence_percentage == pytest.approx(1.0)
    assert tile.kind is TileKind.SOIL


def test_planting_creates_a_wheat_crop_entity_in_seed_form(tmp_path: Path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    column, row = 10, 10
    tile = game.map.tile_map.tile_at(column, row)
    tile.kind = TileKind.SOIL
    game.map.tile_states[(column, row)] = LevelTileState(
        column,
        row,
        till_percentage=100.0,
        kind_override=TileKind.SOIL.value,
    )

    crop = game.plant_crop(column, row, "wheat")

    assert crop.type_id == "crop"
    assert crop.variant == "wheat"
    assert crop.form == "seed"
    assert crop.state["growth_progress"] == 0.0
    assert 0.0 <= crop.state["water"] <= 5.0
    assert crop.state["tended"] == 0.0
    assert game.crop_at_tile(column, row) is crop
    assert game.map.tile_states[(column, row)].kind_override == TileKind.SOIL.value


def test_selected_seed_crop_reports_growth_water_and_tended_percentages(
    tmp_path: Path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")
    crop.state.update(
        {"growth_progress": 0.125, "water": 63.25, "tended": 18.5}
    )

    assert crop_inspection_lines(crop) == [
        "Growth: 12.5%",
        "Water: 63.2%",
        "Tended: 18.5%",
    ]


def test_soil_persistence_roll_can_keep_or_revert_soil() -> None:
    unused = LevelTileState(
        1, 1, till_percentage=100.0, kind_override=TileKind.SOIL.value
    )
    used = LevelTileState(
        2, 1, till_percentage=100.0,
        soil_persistence_percentage=100.0,
        kind_override=TileKind.SOIL.value,
    )
    states = {(1, 1): unused, (2, 1): used}

    advance_level_tile_states(
        states,
        day_number=1,
        reverted_till_progress_range=(80.0, 100.0),
    )

    assert unused.kind_override is None
    assert 80.0 <= unused.till_percentage <= 100.0
    assert used.kind_override == TileKind.SOIL.value


def test_crop_growth_changes_the_crop_form(tmp_path: Path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")

    game.advance_crop_growth(90.0)
    assert crop.form == "sprout"

    game.advance_crop_growth(510.0)
    assert crop.form == "mature"
    assert crop.state["growth_progress"] == pytest.approx(1.0)


def test_crop_care_percentages_scale_growth_multipliers_directly(
    tmp_path: Path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")
    crop.state.update({"water": 50.0, "tended": 50.0})
    growth = game.map.object_types["crop"].growth
    expected_multiplier = (
        1.0 + (float(growth["water_multiplier"]) - 1.0) * 0.5
    ) * (1.0 + (float(growth["tended_multiplier"]) - 1.0) * 0.5)

    game.advance_crop_growth(1.0)

    assert crop.state["growth_progress"] == pytest.approx(
        expected_multiplier / float(growth["base_minutes"])
    )


def test_each_whole_growth_percent_decays_water_and_tending(
    tmp_path: Path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")
    crop.state.update({"water": 100.0, "tended": 100.0})

    game.advance_crop_growth(4.0)

    assert crop.state["growth_progress"] >= 0.01
    assert 0.0 < crop.state["water"] < 100.0
    assert 0.0 < crop.state["tended"] < 100.0


def test_planted_crop_marks_soil_used_and_crop_is_not_restored_at_dawn(
    tmp_path: Path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    column, row = 10, 10
    game.map.tile_map.tile_at(column, row).kind = TileKind.SOIL
    game.map.tile_states[(column, row)] = LevelTileState(
        column,
        row,
        till_percentage=100.0,
        kind_override=TileKind.SOIL.value,
    )
    game.map.reverted_till_progress_range = (80.0, 100.0)
    game.map.tile_states[(column, row)].soil_persistence_percentage = 100.0
    game.plant_crop(column, row, "wheat")

    assert game.finish_day() is True

    assert game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL
    assert game.crop_at_tile(column, row) is None


def test_area_tilling_respects_a_one_hour_budget(
    tmp_path: Path, monkeypatch
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    game.player.hoe_quality = 100
    point = game.map.tile_map.tile_center(10, 10)
    monkeypatch.setattr(
        game,
        "build_area_targets",
        lambda command, bounds: [AreaTarget("Till Grassland", point)],
    )

    game.queue_area_command(
        "Till Grassland",
        (0, 0, 64, 64),
        99,
        record=True,
        max_game_minutes=60,
    )

    assert len(game.area_targets) == 4
    assert game.area_targets[-1].work_fraction == pytest.approx(1.0)
    remembered = game.day.today_routine[-1]
    assert remembered.max_game_minutes == 60
    assert remembered.till_until_done is False


def test_till_until_done_ignores_the_time_and_quantity_limits(
    tmp_path: Path, monkeypatch
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    point = game.map.tile_map.tile_center(10, 10)
    state = LevelTileState(10, 10, till_percentage=97.0, persistence_modifier=1.0)
    game.map.tile_states[(10, 10)] = state
    game.map.till_progress_per_action = 1.0
    monkeypatch.setattr(
        game,
        "build_area_targets",
        lambda command, bounds: [AreaTarget("Till Grassland", point)],
    )

    game.queue_area_command(
        "Till Grassland",
        (0, 0, 64, 64),
        1,
        record=True,
        till_until_done=True,
    )

    assert len(game.area_targets) == 3
    remembered = game.day.today_routine[-1]
    assert remembered.max_game_minutes is None
    assert remembered.till_until_done is True


def test_till_time_controls_use_one_hour_increments(tmp_path: Path) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    game.active_command_category = "Farm"
    game.active_command = "Till Grassland"

    game.draw_ui()

    assert game.till_time_buttons
    game.till_max_game_minutes = 120
    game.till_max_game_minutes = max(60, game.till_max_game_minutes - 60)
    assert game.till_max_game_minutes == 60
