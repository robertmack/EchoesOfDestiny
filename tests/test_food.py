from remembering.game import Game, PendingJob, object_action_menu_options


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
    assert berry.traits == ("edible",)
    assert berry.nutrition == 8
    assert "Eat Berries" in object_action_menu_options(table, game.player)


def test_mature_wheat_harvest_yields_between_three_and_five_grains(
    tmp_path,
) -> None:
    game = Game(fullscreen=False, persistence_path=tmp_path / "current_level.jsonc")
    crop = game.plant_crop(10, 10, "wheat")
    crop.state["growth_progress"] = 1.0
    game.apply_object_form(crop, "mature")

    game.complete_job(PendingJob(crop.object_id, "Harvest Wheat"))

    assert 3 <= game.player.inventory["grains"] <= 5


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
