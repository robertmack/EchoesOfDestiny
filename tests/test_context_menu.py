import json
import math

import pygame
import pytest

from remembering.game import (
    AreaTarget,
    DAY_FADE_DURATION_SECONDS,
    Game,
    MAP_VIEWPORT,
    MORNING_OPENING_FADE_SECONDS,
    PendingJob,
    area_commands_for_category,
    available_area_commands,
    build_context_menu_options,
    build_ground_context_menu_options,
    build_path_points,
    compact_label,
    missing_recipe_ingredients,
    object_map_label,
    object_action_menu_options,
    object_job_duration_seconds,
    planting_duration_seconds,
    random_within_tile_anchor,
    sprite_size_within_footprint,
    tilling_duration_seconds,
    tile_aligned_area_bounds,
)
from remembering.model import (
    DayState,
    LevelTileState,
    Mode,
    ObjectKind,
    PlayerState,
    RoutineStep,
    WorldObject,
    encode_tree_state,
    tree_state_data,
)
from remembering.tiles import TileKind
from remembering.ui_layout import RIGHT_DOCK_RECT
from remembering.world import (
    DEFAULT_CURRENT_LEVEL_PATH,
    create_world,
    load_memory_file,
    load_map,
    load_object_types,
    save_persistent_objects,
)


def memory_test_game() -> Game:
    game = Game.__new__(Game)
    game.map = load_map(persistence_path=None)
    game.objects = game.map.objects
    game.player = PlayerState()
    game.player_radius = 28
    game.interaction_distance = 90
    game.stagger_phase = 0.0
    game.day = DayState(mode=Mode.DIRECT)
    game.thought_bubble_text = None
    game.thought_bubble_source = None
    game.thought_bubble_timer = 0.0
    game.object_memory_check_accumulator = 0.0
    game.messages = []
    game.message_scroll_offset = 0
    return game


def test_crossing_an_unlocked_closed_door_stops_once_and_opens_it() -> None:
    game = memory_test_game()
    door = next(
        boundary
        for boundary in game.map.boundaries
        if boundary.boundary_id == "bedroom_door_1"
    )
    start = game.map.tile_map.tile_center(66, 64)
    destination = game.map.tile_map.tile_center(67, 64)
    game.player.x, game.player.y = start
    game.navigation_path = [destination]
    game.walk_target = destination

    assert door.open is False
    assert door.locked is False
    assert game.move_along_path(0.05)
    assert door.open is True
    assert (game.player.x, game.player.y) == start

    assert game.move_along_path(1.0)
    assert (game.player.x, game.player.y) != start


def test_object_memory_is_once_daily_and_halves_after_each_spoken_recall(monkeypatch) -> None:
    game = memory_test_game()
    bowl = next(obj for obj in game.objects.values() if obj.object_id == 2711)
    game.player.x, game.player.y = bowl.center
    rolls = iter((0.49, 0.49, 0.49, 0.8))
    monkeypatch.setattr("remembering.game.random.random", lambda: next(rolls))

    game.update_object_memories(0.5)
    assert game.thought_bubble_text == "Warm porridge. Someone was looking after me."
    state = bowl.state["object_memory_state"]["porridge_comfort"]
    assert state == {"last_roll_day": 1, "times_said": 1}

    game.thought_bubble_text = None
    game.update_object_memories(0.5)
    assert game.thought_bubble_text is None

    game.day.attempts = 2
    game.update_object_memories(0.5)
    assert game.thought_bubble_text == "Warm porridge. Someone was looking after me."
    assert state["times_said"] == 2

    game.thought_bubble_text = None
    game.day.attempts = 3
    game.update_object_memories(0.5)
    assert game.thought_bubble_text is None
    assert state["last_roll_day"] == 3
    assert state["times_said"] == 2


def test_persistent_objects_do_not_passively_decay() -> None:
    game = memory_test_game()
    bowl = game.objects[2711]
    table = game.objects[2]

    game.advance_simple_object_persistence()

    assert bowl.active is True
    assert bowl.persistent is True
    assert "missing_table_food" not in table.state.get("memory_refs", [])


def test_tabletop_porridge_can_be_reached_and_eaten_directly() -> None:
    game = memory_test_game()
    bowl = game.objects[2711]
    game.player.x, game.player.y = game.map.tile_map.tile_center(68, 64)

    assert "Eat Porridge" in object_action_menu_options(bowl, game.player)
    assert game.build_navigation_path_to_object(bowl)

    consumed: list[tuple[int, str]] = []
    game.consume_carried_food = lambda food, location: consumed.append(
        (food.object_id, location)
    )
    game.complete_job(PendingJob(bowl.object_id, "Eat Porridge"))
    assert consumed == [(bowl.object_id, "from the table")]


def current_level_copy(tmp_path):
    path = tmp_path / "current_level.jsonc"
    path.write_text(DEFAULT_CURRENT_LEVEL_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def give_bucket(game: Game, quality: int = 61) -> WorldObject:
    bucket = game.create_carried_object("bucket")
    bucket.quality = quality
    bucket.capacity = game.map.object_types["bucket"].form_definition().capacity_for(
        quality
    )
    return bucket


def give_axe(game: Game) -> WorldObject:
    axe = game.create_carried_object("axe")
    game.player.has_axe = True
    game.player.carrying_axe = True
    return axe


def test_context_menu_includes_move_to_and_actions() -> None:
    game = Game(fullscreen=False)
    player = PlayerState()
    obj = game.object_of_type("branch")
    options = build_context_menu_options(obj, player, (120, 120), {obj.object_id: obj})

    assert options[0] == "Move To"
    assert options[1:] == ["Gather"]


def test_workbench_shows_disabled_recipes_and_sidebar_buttons_are_clickable() -> None:
    game = Game(fullscreen=False)
    workbench = game.object_of_type("workbench")
    options = object_action_menu_options(workbench, game.player)
    assert options == [
        "Craft Crude Hoe (requirements not met)",
        "Craft Crude Axe (requirements not met)",
        "Craft Wooden Bucket (requirements not met)",
        "Weave Fiber Basket (requirements not met)",
    ]
    assert missing_recipe_ingredients(workbench, game.player, options[0]) == [
        ("branch", 1),
        ("pebble", 1),
        ("fiber", 1),
    ]
    game.player.inventory["branch"] = 1
    assert missing_recipe_ingredients(workbench, game.player, options[0]) == [
        ("pebble", 1),
        ("fiber", 1),
    ]

    game.selected_id = workbench.object_id
    game.activate_sidebar_action(0)
    assert game.pending_job is None

    game.player.inventory.update({"branch": 1, "pebble": 1, "fiber": 1})
    game.draw_ui()
    hoe_rect = game.action_buttons[0][0]
    pygame.event.post(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=hoe_rect.center)
    )
    game.handle_events()
    assert game.pending_job is not None
    assert game.pending_job.action == "Craft Crude Hoe"


def test_shallow_water_context_menu_gathers_with_empty_bucket() -> None:
    game = Game(fullscreen=False)
    target = game.build_area_targets(
        "Gather Water", (0, 0, game.map.width, game.map.height)
    )
    assert target == []

    give_bucket(game)
    assert "Gather Water" not in available_area_commands(game.player)
    water_target = game.build_area_targets(
        "Gather Water", (0, 0, game.map.width, game.map.height)
    )[0]
    tile = game.map.tile_map.tile_at_world(*water_target.point)[2]
    assert build_ground_context_menu_options(tile, game.player) == [
        "Move To",
        "Drop Bucket",
        "Drink Water",
        "Drink Until Full",
        "Gather Water",
    ]

    game.player.x, game.player.y = water_target.point
    game.selected_id = None
    game.context_ground_target = water_target.point
    game.context_menu_options = ["Move To", "Gather Water"]
    game.activate_context_option(1)
    assert game.pending_area_target == water_target
    game.update(0.0)
    assert game.player.bucket_filled is True
    assert game.player.bucket_water_uses == 5
    disabled_options = build_ground_context_menu_options(tile, game.player)
    assert disabled_options == [
        "Move To",
        "Drop Bucket",
        "Drink Water",
        "Drink Until Full",
        "Gather Water (empty bucket required)",
    ]
    game.context_ground_target = water_target.point
    game.context_menu_options = disabled_options
    game.activate_context_option(4)
    assert game.context_menu_options == disabled_options
    assert game.pending_area_target is None

    pond = next(
        tile
        for tile in game.map.tile_map.tiles
        if tile.kind is TileKind.POND
    )
    game.player.bucket_filled = False
    assert build_ground_context_menu_options(pond, game.player) == [
        "Move To",
        "Drop Bucket",
        "Drink Water",
        "Drink Until Full",
        "Gather Water",
    ]


def test_object_on_water_keeps_object_and_underlying_tile_context_options() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    give_bucket(game)
    water_target = game.water_sources_in_bounds(
        (0, 0, game.map.width, game.map.height)
    )[0]
    grass = game.object_of_type("grass")
    grass.x = round(water_target[0] - grass.width / 2)
    grass.y = round(water_target[1] - grass.height / 2)
    game.camera.center_on(water_target, (game.map.width, game.map.height))

    game.handle_context_click(game.camera.world_to_screen(water_target))

    assert "Gather" in game.context_menu_options
    assert "Drink Water" in game.context_menu_options
    assert "Drink Until Full" in game.context_menu_options
    assert "Gather Water" in game.context_menu_options
    gather_index = game.context_menu_options.index("Gather")
    water_index = game.context_menu_options.index("Gather Water")
    assert game.context_option_target_ids[gather_index] == grass.object_id
    assert game.context_option_target_ids[water_index] is None

    game.player.x, game.player.y = water_target
    game.activate_context_option(water_index)
    assert game.pending_area_target is not None
    assert game.pending_area_target.action == "Gather Water"


def test_drinking_from_terrain_is_recorded_and_replayed() -> None:
    game = Game(fullscreen=False)
    target = game.water_sources_in_bounds(
        (0, 0, game.map.width, game.map.height)
    )[0]
    game.player.x, game.player.y = target
    game.player.conditions["thirst"] = 50

    assert game.queue_terrain_drink(target, record=True) is True
    assert game.day.today_routine[-1] == RoutineStep(
        None, "Drink Water", target_point=target
    )

    game.cancel_current_command()
    game.day.remembered_routine = [
        RoutineStep(None, "Drink Water", target_point=target)
    ]
    game.day.replay_index = 0
    game.day.mode = Mode.REPLAY
    game.simulation_paused = False
    game.update(0.05)
    game.update(0.05)

    assert game.day.replay_index == 1
    assert game.player.conditions["thirst"] < 50


def test_drink_until_full_repeats_until_thirst_is_zero() -> None:
    game = Game(fullscreen=False)
    target = game.water_sources_in_bounds(
        (0, 0, game.map.width, game.map.height)
    )[0]
    game.player.x, game.player.y = target
    game.player.conditions["thirst"] = 80
    give_bucket(game)

    assert game.queue_terrain_drink(target, record=True, until_full=True) is True
    assert game.day.today_routine[-1] == RoutineStep(
        None, "Drink Until Full", target_point=target
    )
    for _ in range(4):
        game.update(0.0)

    assert game.player.conditions["thirst"] == 0
    assert game.pending_area_target is None


def test_ruined_bucket_cannot_start_or_repeat_water_gathering() -> None:
    game = Game(fullscreen=False)
    bucket = game.create_carried_object("bucket", quality=20)
    assert bucket.capacity["water"] == 0
    water_target = game.water_sources_in_bounds(
        (0, 0, game.map.width, game.map.height)
    )[0]
    tile = game.map.tile_map.tile_at_world(*water_target)[2]
    options = build_ground_context_menu_options(tile, game.player)

    assert options == [
        "Move To",
        "Drop Bucket",
        "Gather Water (bucket is ruined)",
    ]
    game.context_ground_target = water_target
    game.context_menu_options = options
    game.activate_context_option(2)

    assert game.pending_area_target is None
    assert game.messages[-1] == "The ruined bucket cannot hold water."
    assert game.build_area_targets(
        "Gather Water", (0, 0, game.map.width, game.map.height)
    ) == []


def test_area_commands_are_grouped_into_submenus() -> None:
    player = PlayerState()
    assert area_commands_for_category(player, "Gather") == [
        "Gather Pebbles",
        "Gather Branches",
        "Gather Seeds",
        "Gather Tall Grass",
        "Harvest Berries",
    ]
    assert area_commands_for_category(player, "Farm") == ["Tend Crops", "Harvest Wheat"]
    assert area_commands_for_category(player, "Build") == [
        "Build Barrel",
        "Build Cupboard",
    ]
    player.has_axe = True
    player.carrying_axe = True
    assert area_commands_for_category(player, "Gather")[-1] == "Chop Trees"


def test_chop_trees_area_command_requires_axe_and_queues_tree_actions() -> None:
    game = Game(fullscreen=False)
    bounds = (0, 0, game.map.width, game.map.height)
    assert "Chop Trees" not in available_area_commands(game.player)
    assert game.build_area_targets("Chop Trees", bounds) == []

    give_axe(game)
    targets = game.build_area_targets("Chop Trees", bounds)

    assert targets
    assert all(target.action == "Chop Down Tree" for target in targets)
    assert all(game.objects[target.target_id].kind is ObjectKind.TREE for target in targets)


def test_harvest_berries_area_command_only_targets_bushes_with_berries() -> None:
    game = Game(fullscreen=False)
    bushes = [
        obj
        for obj in game.objects.values()
        if obj.type_id == "bush" and obj.active
    ]
    assert len(bushes) >= 2
    bushes[0].state["has_berries"] = False

    targets = game.build_area_targets(
        "Harvest Berries", (0, 0, game.map.width, game.map.height)
    )

    assert targets
    assert all(target.action == "Harvest Berries" for target in targets)
    assert bushes[0].object_id not in {target.target_id for target in targets}
    assert all(
        game.objects[target.target_id].state["has_berries"]
        for target in targets
        if target.target_id is not None
    )


def test_basket_makes_berry_harvesting_six_times_faster() -> None:
    game = Game(fullscreen=False)
    bush = game.object_of_type("bush")

    base = object_job_duration_seconds("Harvest Berries", bush, False)
    with_basket = object_job_duration_seconds("Harvest Berries", bush, True)

    assert base == pytest.approx(3.0)
    assert with_basket == pytest.approx(0.5)
    assert base / with_basket == pytest.approx(6.0)


def test_timed_work_reports_progress_and_thought_milestones(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    bush = game.object_of_type("bush")
    duration = object_job_duration_seconds("Harvest Berries", bush, False)
    point = (game.player.x, game.player.y)
    game.pending_job = PendingJob(bush.object_id, "Harvest Berries", point)

    expected = (
        (0.0, "Harvest Berries"),
        (0.25, "Harvest Berries — 1/4 done"),
        (0.5, "Harvest Berries — 1/2 done"),
        (0.75, "Harvest Berries — almost done"),
    )
    for progress, thought in expected:
        game.job_timer = duration * progress
        action, actual_progress = game.active_work_progress()
        assert action == "Harvest Berries"
        assert actual_progress == pytest.approx(progress)
        assert game.work_thought_text() == thought


def test_barrel_build_cost_comes_from_object_catalog_and_water_transfers() -> None:
    game = Game(fullscreen=False)
    assert dict(game.map.object_types["barrel"].form_definition().build_cost) == {
        "wood": 5,
        "fiber": 2,
    }
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]

    game.build_barrel(target.placement_point or target.point)

    barrel = game.object_of_type("barrel")
    assert barrel.active is True
    assert barrel.persistent is False
    assert barrel.state["sprite_state"] == "empty"
    barrel_tile = game.map.tile_map.tile_at_world(*barrel.center)[2]
    assert f"object:{barrel.object_id}" in barrel_tile.properties
    assert "blocked" in barrel_tile.properties
    assert game.object_color(barrel) == (115, 78, 46)
    assert game.player.inventory["wood"] == 0
    assert game.player.inventory["fiber"] == 0
    give_bucket(game)
    game.player.bucket_water_uses = 5
    game.complete_job(PendingJob(barrel.object_id, "Pour Water Into Barrel"))
    assert game.player.bucket_water_uses == 0
    assert game.barrel_state(barrel)["water_uses"] == 5
    assert "sprite_state" not in barrel.state
    game.complete_job(PendingJob(barrel.object_id, "Fill Bucket From Barrel"))
    assert game.player.bucket_water_uses == 5
    assert game.barrel_state(barrel)["water_uses"] == 0
    assert barrel.state["sprite_state"] == "empty"


@pytest.mark.parametrize(
    ("key", "resource"),
    [
        (pygame.K_w, "wood"),
        (pygame.K_f, "fiber"),
        (pygame.K_b, "branch"),
        (pygame.K_p, "pebble"),
        (pygame.K_s, "seed"),
        (pygame.K_g, "grains"),
    ],
)
def test_ctrl_resource_cheats_add_one_resource_silently(
    key: int, resource: str
) -> None:
    game = Game(fullscreen=False)
    starting_quantity = game.player.inventory[resource]
    starting_messages = list(game.messages)

    pygame.event.post(
        pygame.event.Event(
            pygame.KEYDOWN,
            key=key,
            mod=pygame.KMOD_CTRL,
            unicode="",
        )
    )
    game.handle_events()

    assert game.player.inventory[resource] == starting_quantity + 1
    assert game.messages == starting_messages


def test_w_without_ctrl_does_not_add_wood() -> None:
    game = Game(fullscreen=False)
    starting_wood = game.player.inventory["wood"]

    pygame.event.post(
        pygame.event.Event(
            pygame.KEYDOWN,
            key=pygame.K_w,
            mod=pygame.KMOD_NONE,
            unicode="w",
        )
    )
    game.handle_events()

    assert game.player.inventory["wood"] == starting_wood


def test_f6_reloads_sprites_without_reloading_the_map(monkeypatch) -> None:
    game = Game(fullscreen=False)
    original_map = game.map
    reload_calls: list[bool] = []
    monkeypatch.setattr(
        game.object_sprites, "reload", lambda: reload_calls.append(True)
    )

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F6))
    game.handle_events()

    assert reload_calls == [True]
    assert game.map is original_map
    assert game.messages[-1] == "Sprites reloaded."


def test_b_cheat_gives_an_empty_bucket_silently() -> None:
    game = Game(fullscreen=False)
    game.player.carried_objects.clear()
    starting_messages = list(game.messages)

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_b))
    game.handle_events()

    assert game.player.has_bucket is True
    assert game.player.bucket_water_uses == 0
    assert game.messages == starting_messages


def test_crafted_bucket_starts_at_quality_fifty() -> None:
    game = Game(fullscreen=False)
    workbench = game.object_of_type("workbench")
    game.player.inventory.update({"wood": 2, "fiber": 1})

    game.complete_job(PendingJob(workbench.object_id, "Craft Wooden Bucket"))

    assert game.player.bucket is not None
    assert game.player.bucket.quality == 50
    assert game.player.bucket.capacity["water"] == 4


def test_player_issued_build_barrel_command_places_visible_map_object() -> None:
    game = Game(fullscreen=False)
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    candidate = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    placement = candidate.placement_point or candidate.point

    game.queue_area_command(
        "Build Barrel",
        tile_aligned_area_bounds(placement, placement, game.map.tile_map.tile_size),
        1,
        record=True,
    )
    for _ in range(2_000):
        game.update(0.05)
        if any(obj.type_id == "barrel" and obj.active for obj in game.objects.values()):
            break

    barrel = game.object_of_type("barrel")
    assert barrel.active is True
    assert math.dist(barrel.center, placement) <= 1
    game.draw_objects()


def test_barrel_and_then_its_water_level_can_be_remembered() -> None:
    game = Game(fullscreen=False)
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    game.build_barrel(target.placement_point or target.point)
    barrel = game.object_of_type("barrel")
    game.map.permanent_soil_chance_per_till = 1.0

    game.advance_barrel_memories()
    assert barrel.persistent is True
    assert barrel.persistent_state.state["water_uses"] == 0

    data = game.barrel_state(barrel)
    data["water_uses"] = 12
    barrel.state = data
    game.advance_barrel_memories()
    remembered = barrel.persistent_state.state
    assert remembered["water_uses"] == 12


def test_replayed_barrel_command_resolves_rebuilt_barrel_through_build_memory(
    tmp_path,
) -> None:
    persistence_path = current_level_copy(tmp_path)
    game = Game(fullscreen=False, persistence_path=persistence_path)
    game.player.inventory.update({"wood": 5, "fiber": 2})
    target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    game.build_barrel(target.placement_point or target.point)
    original = game.object_of_type("barrel")
    memory_id = str(original.state["build_memory_id"])
    source = game.water_sources_in_bounds(
        (0, 0, game.map.width, game.map.height)
    )[0]
    bounds = tile_aligned_area_bounds(source, source, game.map.tile_map.tile_size)
    routine = [
        RoutineStep(
            original.object_id,
            "Fill Barrel",
            "barrel",
            area_bounds=bounds,
            target_point=original.center,
            source_areas=(bounds,),
        )
    ]
    game.map.permanent_soil_chance_per_till = 0.0
    game.advance_barrel_memories()
    save_persistent_objects(
        game.objects,
        persistence_path,
        tile_size=game.map.tile_map.tile_size,
        remembered_routine=routine,
        build_memories=game.build_memories,
    )

    reloaded = Game(fullscreen=False, persistence_path=persistence_path)
    assert not any(obj.type_id == "barrel" for obj in reloaded.objects.values())
    reloaded.create_carried_object("bucket", quality=50)
    reloaded.player.inventory.update({"wood": 5, "fiber": 2})
    tile_size = reloaded.map.tile_map.tile_size
    memory = reloaded.build_memories[memory_id]
    reloaded.build_barrel(
        ((memory.column + 0.5) * tile_size, (memory.row + 0.5) * tile_size)
    )
    rebuilt = reloaded.object_of_type("barrel")

    assert rebuilt.object_id != original.object_id
    rebuilt.state.pop("build_memory_id")
    assert reloaded.build_memory_id_for_step(routine[0]) == memory_id
    reloaded.day.mode = Mode.MORNING
    reloaded.choose_morning_option("Replay Remembered Routine")
    reloaded.update(0.0)

    assert reloaded.barrel_fill_job is not None
    assert reloaded.barrel_fill_job.barrel_id == rebuilt.object_id
    assert rebuilt.state["build_memory_id"] == memory_id


def test_fill_barrel_uses_selected_water_area_and_repeats_until_full() -> None:
    game = Game(fullscreen=False)
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    build_target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    game.build_barrel(build_target.placement_point or build_target.point)
    barrel = game.object_of_type("barrel")
    barrel_data = game.barrel_state(barrel)
    barrel_data["water_uses"] = 25
    barrel.state = barrel_data
    give_bucket(game)
    game.player.bucket_water_uses = 0
    assert "Fill Barrel" in object_action_menu_options(barrel, game.player)
    water_source = next(
        source
        for source in game.water_sources_in_bounds(
            (0, 0, game.map.width, game.map.height)
        )
        if game.build_navigation_path(source)
    )
    game.camera.center_on(water_source, (game.map.width, game.map.height))

    game.begin_barrel_source_selection(barrel.object_id)
    game.command_drag_start = water_source
    game.finish_command_drag(game.camera.world_to_screen(water_source))

    remembered = game.day.today_routine[-1]
    assert remembered.action == "Fill Barrel"
    assert remembered.target_id == barrel.object_id
    assert remembered.target_build_memory == barrel.state["build_memory_id"]
    assert remembered.area_bounds == tile_aligned_area_bounds(
        water_source, water_source, game.map.tile_map.tile_size
    )
    game.time_speed = 10.0
    for _ in range(3_000):
        game.update(0.05)
        if game.barrel_state(barrel)["water_uses"] == 30:
            break

    assert game.barrel_state(barrel)["water_uses"] == 30
    assert game.barrel_fill_job is None


def test_ctrl_adds_multiple_separate_water_source_areas(monkeypatch) -> None:
    game = Game(fullscreen=False)
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    build_target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    game.build_barrel(build_target.placement_point or build_target.point)
    barrel = game.object_of_type("barrel")
    give_bucket(game)
    sources = game.water_sources_in_bounds((0, 0, game.map.width, game.map.height))[:2]
    assert len(sources) == 2

    game.begin_barrel_source_selection(barrel.object_id)
    game.camera.center_on(sources[0], (game.map.width, game.map.height))
    game.command_drag_start = sources[0]
    monkeypatch.setattr(pygame.key, "get_mods", lambda: pygame.KMOD_CTRL)
    game.finish_command_drag(game.camera.world_to_screen(sources[0]))

    assert game.barrel_fill_job is None
    assert game.active_command == "Fill Barrel"
    assert len(game.pending_source_areas) == 1

    game.camera.center_on(sources[1], (game.map.width, game.map.height))
    game.command_drag_start = sources[1]
    monkeypatch.setattr(pygame.key, "get_mods", lambda: pygame.KMOD_CTRL)
    game.finish_command_drag(game.camera.world_to_screen(sources[1]))

    assert game.barrel_fill_job is None
    assert len(game.pending_source_areas) == 2
    pygame.event.post(pygame.event.Event(pygame.KEYUP, key=pygame.K_LCTRL))
    game.handle_events()

    assert game.barrel_fill_job is not None
    assert len(game.barrel_fill_job.source_areas) == 2
    assert game.day.today_routine[-1].source_areas == game.barrel_fill_job.source_areas


def test_ctrl_adds_multiple_separate_gathering_areas(monkeypatch) -> None:
    game = Game(fullscreen=False)
    pebbles = [obj for obj in game.objects.values() if obj.type_id == "pebble" and obj.active][:2]
    assert len(pebbles) == 2
    game.active_command = "Gather Pebbles"
    game.area_command_quantity = 10

    game.camera.center_on(pebbles[0].center, (game.map.width, game.map.height))
    game.command_drag_start = pebbles[0].center
    monkeypatch.setattr(pygame.key, "get_mods", lambda: pygame.KMOD_CTRL)
    game.finish_command_drag(game.camera.world_to_screen(pebbles[0].center))

    assert game.active_command == "Gather Pebbles"
    assert len(game.pending_target_areas) == 1
    assert game.area_targets == []

    game.camera.center_on(pebbles[1].center, (game.map.width, game.map.height))
    game.command_drag_start = pebbles[1].center
    monkeypatch.setattr(pygame.key, "get_mods", lambda: pygame.KMOD_CTRL)
    game.finish_command_drag(game.camera.world_to_screen(pebbles[1].center))

    assert game.area_targets == []
    assert len(game.pending_target_areas) == 2
    pygame.event.post(pygame.event.Event(pygame.KEYUP, key=pygame.K_LCTRL))
    game.handle_events()

    remembered = game.day.today_routine[-1]
    assert remembered.action == "Gather Pebbles"
    assert remembered.target_areas is not None
    assert len(remembered.target_areas) == 2
    queued_ids = {target.target_id for target in game.area_targets}
    assert {pebbles[0].object_id, pebbles[1].object_id} <= queued_ids


def test_releasing_ctrl_without_selecting_keeps_selection_active() -> None:
    game = Game(fullscreen=False)
    game.active_command = "Gather Pebbles"

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LCTRL))
    pygame.event.post(pygame.event.Event(pygame.KEYUP, key=pygame.K_LCTRL))
    game.handle_events()

    assert game.active_command == "Gather Pebbles"
    assert game.pending_target_areas == []
    assert game.area_targets == []
    assert game.day.today_routine == []


def test_escape_cancels_complex_command_and_removes_its_memory() -> None:
    game = Game(fullscreen=False)
    bounds = (0, 0, game.map.width, game.map.height)
    game.queue_area_command("Gather Pebbles", bounds, 2, record=True)
    assert game.area_targets
    assert game.day.today_routine[-1].action == "Gather Pebbles"

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    game.handle_events()

    assert game.area_targets == []
    assert game.navigation_path == []
    assert game.active_command is None
    assert game.pending_source_areas == []
    assert game.pending_target_areas == []
    assert game.day.today_routine == []
    assert game.messages[-1] == "Command cancelled."


def test_water_crops_command_uses_crop_and_water_source_areas() -> None:
    game = Game(fullscreen=False)
    give_bucket(game)
    game.player.bucket_water_uses = 0
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    candidates = [
        target
        for target in game.build_area_targets(
            "Till Grassland", (0, 0, game.map.width, game.map.height)
        )
        if not target.prerequisite_target_ids
    ][:2]
    assert len(candidates) == 2
    for point in (target.point for target in candidates):
        column, row, tile = game.map.tile_map.tile_at_world(*point)
        tile.kind = TileKind.SOIL
        game.map.tile_states[(column, row)] = LevelTileState(column, row, crop="wheat")
    crop_bounds = tile_aligned_area_bounds(
        candidates[0].point, candidates[1].point, game.map.tile_map.tile_size
    )
    water_source = next(
        source
        for source in game.water_sources_in_bounds(
            (0, 0, game.map.width, game.map.height)
        )
        if game.build_navigation_path(source)
    )
    source_bounds = tile_aligned_area_bounds(
        water_source, water_source, game.map.tile_map.tile_size
    )

    game.queue_field_water_command(crop_bounds, source_bounds, 2, record=True)
    game.time_speed = 10.0
    for _ in range(5_000):
        game.update(0.05)
        if game.field_water_job is None:
            break

    states = [
        game.map.tile_states[game.map.tile_map.tile_at_world(*target.point)[:2]]
        for target in candidates
    ]
    assert all(state.watered for state in states)
    assert game.player.bucket_water_uses == 3
    remembered = game.day.today_routine[-1]
    assert remembered.action == "Water Crops"
    assert remembered.area_bounds == crop_bounds
    assert remembered.secondary_bounds == source_bounds

    game = Game(fullscreen=False)
    game.draw_ui()
    farm_rect = next(
        rect for rect, category in game.command_category_buttons if category == "Farm"
    )
    pygame.event.post(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=farm_rect.center)
    )
    game.handle_events()
    game.draw_ui()
    assert game.active_command_category == "Farm"
    assert [command for _, command in game.command_buttons] == [
        "Tend Crops",
        "Harvest Wheat",
        "Back",
    ]


def test_area_menu_number_keys_enter_submenu_choose_command_and_go_back() -> None:
    game = Game(fullscreen=False)
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1))
    game.handle_events()
    assert game.active_command_category == "Gather"

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_2))
    game.handle_events()
    assert game.active_command == "Gather Branches"

    back_index = len(area_commands_for_category(game.player, "Gather"))
    back_key = (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5, pygame.K_6)[back_index]
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=back_key))
    game.handle_events()
    assert game.active_command_category is None
    assert game.active_command is None


def test_day_one_starts_in_direct_control() -> None:
    game = Game(fullscreen=False)

    assert game.day.number == 1
    assert game.day.mode is Mode.DIRECT


def test_compact_label_uses_first_letter_when_too_large() -> None:
    assert compact_label("Broken Workshop", 40, 120) == "B"
    assert compact_label("Bed", 80, 30) == "Bed"


def test_small_sprites_keep_native_size_and_large_sprites_shrink_to_fit() -> None:
    assert sprite_size_within_footprint((8, 8), (32, 32)) == (8, 8)
    assert sprite_size_within_footprint((64, 32), (32, 32)) == (32, 16)
    assert sprite_size_within_footprint((64, 128), (32, 64)) == (32, 64)


def test_random_tile_anchor_is_stable_and_respects_margin() -> None:
    first = random_within_tile_anchor(42, "pebble", (8, 8), (32, 32))
    assert first == random_within_tile_anchor(
        42, "pebble", (8, 8), (32, 32)
    )
    assert first != random_within_tile_anchor(
        43, "pebble", (8, 8), (32, 32)
    )
    # With a 20% tile margin and a 4px half-width, the center must remain
    # between 32.5% and 67.5% so no sprite pixels enter the margin.
    assert all(0.325 <= coordinate <= 0.675 for coordinate in first)


def test_map_label_uses_object_type_instead_of_state() -> None:
    crop = WorldObject(1, "Wheat", ObjectKind.CROP, 0, 0, 32, 32, form="mature")
    bed = WorldObject(2, "Bed", ObjectKind.BED, 0, 0, 32, 32, quality=30)

    assert object_map_label(crop) == "Wheat"
    assert object_map_label(bed) == "Bed"


def test_can_stand_at_rejects_house_walls_and_world_edges() -> None:
    game = Game(fullscreen=False)
    assert game.can_stand_at(game.player.x, game.player.y) is True
    size = game.map.tile_map.tile_size
    scale = size / 32
    assert game.can_stand_at(61 * size - 10 * scale, 64 * size) is False
    assert game.can_stand_at(64 * size + size / 2, 63 * size + size / 2) is True
    assert game.can_stand_at(-5, 120) is False
    assert game.can_stand_at(game.map.width + 5, 120) is False


def test_player_spawns_at_center_of_a_tile() -> None:
    game = Game(fullscreen=False)

    assert game.map.tile_map.is_tile_center(game.player.x, game.player.y)


def test_player_starts_north_of_bed() -> None:
    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")

    assert game.player.y < bed.y


def test_persistent_objects_reset_and_inventory_clears_at_end_of_day(tmp_path) -> None:
    persistence_path = current_level_copy(tmp_path)
    game = Game(fullscreen=False, persistence_path=persistence_path)
    game.object_of_type("branch").active = False
    game.object_of_type("bed").state = "repaired"
    game.player.inventory["branch"] = 3

    game.finish_day()

    saved = persistence_path.read_text(encoding="utf-8")
    assert '"type": "branch"' in saved
    assert '"persistent_state"' in saved
    assert '"state": "repaired"' not in saved
    assert game.object_of_type("branch").active is True
    assert game.object_of_type("bed").state == {}
    assert not game.player.inventory
    assert game.day.number == 1
    assert game.day.attempts == 2


def test_stored_tool_can_become_persistent_and_unstored_progress_is_retained(
    tmp_path, monkeypatch
) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    storage = game.object_of_type("tool_storage")
    axe_memory = game.storage_memories[storage.object_id]["axe"]
    axe_memory["store_count"] = 3
    axe_memory["present"] = False
    game.advance_storage_memories()
    assert axe_memory["store_count"] == 3

    game.player.has_hoe = True
    game.player.carrying_hoe = True
    game.player.hoe_quality = 20
    game.complete_job(PendingJob(storage.object_id, "Store Hoe"))
    hoe_memory = game.storage_memories[storage.object_id]["hoe"]
    assert hoe_memory["store_count"] == 0
    assert hoe_memory["present"] is True

    monkeypatch.setattr(game, "policy_chance", lambda *args: 1.0)
    game.finish_day()
    hoe_memory = game.storage_memories[storage.object_id]["hoe"]
    assert hoe_memory["store_count"] == 1
    assert hoe_memory["persistent"] is True
    assert game.player.has_hoe is True
    assert game.player.carrying_hoe is False

    game.complete_job(PendingJob(game.object_of_type("tool_storage").object_id, "Take Hoe"))
    assert game.storage_memories[storage.object_id]["hoe"]["present"] is False
    game.finish_day()
    assert game.storage_memories[storage.object_id]["hoe"]["persistent"] is True
    assert game.player.has_hoe is False


def test_nonpersistent_stored_tool_is_lost_at_dawn_but_keeps_progress(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    storage = game.object_of_type("tool_storage")
    game.player.has_axe = True
    game.player.carrying_axe = True
    game.complete_job(PendingJob(storage.object_id, "Store Axe"))
    game.map.permanent_soil_chance_per_till = 0.0
    game.map.till_count_loss_chance = 0.0

    game.finish_day()

    assert game.player.has_axe is False
    axe_memory = game.storage_memories[storage.object_id]["axe"]
    assert axe_memory["store_count"] == 1
    assert axe_memory["persistent"] is False
    assert axe_memory["present"] is False


def test_sleep_fades_out_resets_persistent_level_and_fades_in(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    branch = game.object_of_type("branch")
    branch.active = False
    game.player.inventory["branch"] = 1
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    tilled_target = game.build_area_targets(
        "Till Grassland", (0, 0, game.map.width, game.map.height)
    )[0]
    column, row, tile = game.map.tile_map.tile_at_world(*tilled_target.point)
    tile.kind = TileKind.SOIL
    tile.properties.append("crop:wheat")
    game.map.tile_states[(column, row)] = LevelTileState(
        column, row, till_count=1, tilled_today=True, crop="wheat"
    )
    permanent_target = game.build_area_targets(
        "Till Grassland", (0, 0, game.map.width, game.map.height)
    )[1]
    permanent_column, permanent_row, permanent_tile = game.map.tile_map.tile_at_world(
        *permanent_target.point
    )
    permanent_tile.kind = TileKind.SOIL
    permanent_tile.properties.append("crop:wheat")
    game.map.tile_states[(permanent_column, permanent_row)] = LevelTileState(
        permanent_column,
        permanent_row,
        permanent_kind=TileKind.SOIL.value,
        crop="wheat",
        crop_growth=0.5,
    )
    game.map.permanent_soil_chance_per_till = 0.0

    game.complete_job(PendingJob(game.object_of_type("bed").object_id, "Sleep"))
    assert game.day_transition_phase == "fade_out"
    assert game.day.number == 1

    game.update(DAY_FADE_DURATION_SECONDS / 2)
    assert game.day.number == 1
    assert game.day_transition_progress == pytest.approx(0.5)

    game.update(DAY_FADE_DURATION_SECONDS / 2)
    assert game.day_transition_phase == "fade_in"
    assert game.day.number == 1
    assert game.day.attempts == 2
    assert game.object_of_type("branch").active is True
    assert not game.player.inventory
    restored_tile = game.map.tile_map.tile_at(column, row)
    assert restored_tile.kind is TileKind.GRASSLAND
    assert not any(prop.startswith("crop:") for prop in restored_tile.properties)
    assert game.map.tile_states[(column, row)].crop is None
    restored_permanent = game.map.tile_map.tile_at(permanent_column, permanent_row)
    assert restored_permanent.kind is TileKind.SOIL
    assert game.map.tile_states[(permanent_column, permanent_row)].crop is None
    assert not any(prop.startswith("crop:") for prop in restored_permanent.properties)

    game.update(MORNING_OPENING_FADE_SECONDS)
    assert game.day_transition_phase is None
    assert game.day_transition_progress == 0.0


def test_queued_sleep_completes_night_reset_while_auto_pause_is_enabled(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    game.day.current_time_minutes = 1_000
    game.time_speed = 10.0
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    target = game.build_area_targets(
        "Till Grassland", (0, 0, game.map.width, game.map.height)
    )
    target = next(candidate for candidate in target if not candidate.prerequisite_target_ids)
    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    tile.kind = TileKind.SOIL
    game.map.tile_states[(column, row)] = LevelTileState(
        column, row, till_count=1, tilled_today=True
    )
    game.map.permanent_soil_chance_per_till = 0.0

    bed = game.object_of_type("bed")
    game.queue_job(bed.object_id, "Sleep", record=True)
    assert game.simulation_paused is False
    for _ in range(2_000):
        game.update(0.05)
        if game.day.attempts == 2 and game.day_transition_phase is None:
            break

    assert game.day.number == 1
    assert game.day.attempts == 2
    assert game.day.current_time_minutes == 360
    assert game.map.tile_map.tile_at(column, row).kind is TileKind.GRASSLAND
    assert game.simulation_paused is True


def test_auto_cheat_memory_forces_dawn_if_bed_cannot_be_reached(
    tmp_path, monkeypatch
) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    game.day.mode = Mode.REPLAY
    game.day.remembered_routine = list(game.map.cheat_memory)
    game.day.replay_index = len(game.day.remembered_routine)
    game.replay_outcome = "sleep"
    game.auto_cheat_memory = True
    game.day.current_time_minutes = 1_100
    monkeypatch.setattr(game, "plan_path_to_object", lambda _obj: False)

    game.update(0.0)

    assert game.day_transition_phase == "fade_out"
    game.update(DAY_FADE_DURATION_SECONDS)
    assert game.day.number == 1
    assert game.day.attempts == 2
    assert game.day.current_time_minutes == 360
    assert game.day_transition_phase == "fade_in"


def test_replay_can_begin_with_a_gather_job_on_the_next_day(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    branch = next(
        obj
        for obj in game.objects.values()
        if obj.persistent
        and "Gather" in obj.interactions
        and game.build_navigation_path(obj.center)
    )
    branch.active = False
    game.player.inventory["branch"] = 1
    game.day.today_routine = [
        RoutineStep(branch.object_id, "Gather", branch.type_id, target_point=branch.center)
    ]

    game.finish_day()
    saved = json.loads(game.persistence_path.read_text(encoding="utf-8"))
    assert saved["remembered_routine"][0]["action"] == "Gather"
    assert saved["remembered_routine"][0]["target_type"] == branch.type_id
    game.choose_morning_option("Replay Remembered Routine")
    game.update(0.0)

    assert game.pending_job is not None
    assert game.pending_job.target_id == branch.object_id
    assert game.pending_job.action == "Gather"


def test_replay_does_not_substitute_a_different_object_for_a_failed_object_order(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    pebble = game.object_of_type("pebble")
    missing_id = max(game.objects) + 10_000
    destination = (game.player.x, game.player.y)
    game.day.remembered_routine = [
        RoutineStep(missing_id, "Gather", pebble.type_id, target_point=destination)
    ]
    game.day.mode = Mode.MORNING

    game.choose_morning_option("Replay Remembered Routine")
    game.update(0.0)
    game.update(0.0)

    assert game.simulation_paused is False
    assert game.pending_job is None
    assert game.thought_bubble_text == (
        "I came here to gather, but the remembered target is no longer available."
    )


def test_morning_options_offer_replay_outcomes_and_memory_editor() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.MORNING
    game.day.remembered_routine = [RoutineStep(game.object_of_type("bed").object_id, "Sleep")]

    assert game.morning_options() == [
        "Direct Control",
        "Replay Memory and Sleep",
        "Replay Memory and Expand Routine",
        "Replay Memory and Explore",
        "Adjust Memory",
    ]


def test_secret_a_key_loads_scenario_memory_and_automates_replay_until_escape() -> None:
    game = Game(fullscreen=False)
    authored = load_memory_file(
        "homestead", tile_size=game.map.tile_map.tile_size
    )
    game.day.mode = Mode.MORNING

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a))
    game.handle_events()

    assert game.day.remembered_routine == list(authored)
    assert game.auto_cheat_memory is True
    assert game.day.mode is Mode.REPLAY
    assert game.replay_outcome == "sleep"
    assert game.record_routine_commands is False

    game.day.mode = Mode.MORNING
    game.update(0.0)
    assert game.day.mode is Mode.REPLAY
    assert game.replay_outcome == "sleep"

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    game.handle_events()

    assert game.auto_cheat_memory is False
    assert game.day.mode is Mode.DIRECT
    assert game.simulation_paused is True
    assert game.messages[-1] == "Automatic cheat-memory replay stopped."


def test_replay_expand_and_explore_control_routine_recording() -> None:
    game = Game(fullscreen=False)
    remembered = RoutineStep(game.object_of_type("bed").object_id, "Sleep", "bed")
    game.day.remembered_routine = [remembered]
    game.day.mode = Mode.MORNING

    game.choose_morning_option("Replay Memory and Expand Routine")
    assert game.day.today_routine == [remembered]
    assert game.record_routine_commands is True
    game.day.replay_index = 1
    game.update(0.0)
    assert game.day.mode is Mode.DIRECT

    game.day.mode = Mode.MORNING
    game.choose_morning_option("Replay Memory and Explore")
    game.day.replay_index = 1
    game.update(0.0)
    assert game.day.mode is Mode.DIRECT
    assert game.record_routine_commands is False
    game.queue_job(game.object_of_type("bed").object_id, "Sleep", record=True)
    assert game.day.today_routine == []


def test_replay_and_sleep_queues_bed_without_changing_memory() -> None:
    game = Game(fullscreen=False)
    remembered = [RoutineStep(999_999, "Gather", "missing")]
    game.day.remembered_routine = list(remembered)
    game.day.mode = Mode.MORNING
    game.choose_morning_option("Replay Memory and Sleep")
    game.day.replay_index = len(remembered)

    game.update(0.0)

    assert game.pending_job is not None
    assert game.pending_job.action == "Sleep"
    assert game.day.remembered_routine == remembered


def test_sleep_is_an_outcome_and_is_not_recorded_as_a_routine_order() -> None:
    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")

    game.queue_job(bed.object_id, "Sleep", record=True)

    assert game.pending_job is not None
    assert game.day.today_routine == []


def test_adjust_memory_can_reorder_remove_and_replace_area_orders() -> None:
    game = Game(fullscreen=False)
    bounds = (0, 0, 32, 32)
    game.day.remembered_routine = [
        RoutineStep(None, "Gather Pebbles", area_bounds=bounds, quantity=2, target_areas=(bounds,)),
        RoutineStep(None, "Gather Branches", area_bounds=bounds, quantity=3, target_areas=(bounds,)),
    ]
    game.day.mode = Mode.MORNING
    game.choose_morning_option("Adjust Memory")
    assert game.adjusting_memory is True

    game.memory_edit_index = 1
    game.move_memory_step(-1)
    assert game.day.remembered_routine[0].action == "Gather Branches"
    game.replace_memory_step()
    assert game.day.remembered_routine[0].action == "Gather Seeds"
    game.handle_memory_editor_key(pygame.K_DELETE, 0)
    assert [step.action for step in game.day.remembered_routine] == ["Gather Pebbles"]


def test_command_editor_map_ignores_right_context_clicks() -> None:
    game = Game(fullscreen=False)
    table = game.objects[2]
    game.camera.center_on(table.center, (game.map.width, game.map.height))
    game.open_memory_editor()
    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=3,
            pos=game.camera.world_to_screen(table.center),
        )
    )

    game.handle_events()

    assert game.adjusting_memory is True
    assert game.context_menu_options == []
    assert game.selected_id is None


def test_activate_sidebar_action_queues_selected_action() -> None:
    game = Game(fullscreen=False)
    game.selected_id = game.object_of_type("bed").object_id
    game.activate_sidebar_action(0)
    assert game.pending_job is not None
    assert game.pending_job.action == "Sleep"


def test_object_action_requires_reaching_planned_interaction_tile() -> None:
    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")
    game.queue_job(bed.object_id, "Sleep", record=False)
    assert game.pending_job is not None
    assert game.pending_job.interaction_point is not None

    # Proximity by itself is insufficient; this position is on the object and
    # not the reachable interaction tile selected by pathfinding.
    game.player.x, game.player.y = bed.center
    game.navigation_path = []
    game.update(1.0)

    assert game.day.number == 1
    assert game.pending_job is None


def test_selecting_object_previews_route_without_moving() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    workbench = game.object_of_type("workbench")
    start = (game.player.x, game.player.y)
    game.camera.center_on(workbench.center, (game.map.width, game.map.height))

    game.handle_world_click(game.camera.world_to_screen(workbench.center))

    assert game.selected_id == workbench.object_id
    assert game.preview_path
    assert game.navigation_path == []
    assert (game.player.x, game.player.y) == start


def test_replay_allows_inspection_without_interrupting_navigation() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.REPLAY
    bed = game.object_of_type("bed")
    game.camera.center_on(bed.center, (game.map.width, game.map.height))
    route = [(game.player.x + 32, game.player.y)]
    game.navigation_path = list(route)
    game.walk_target = route[-1]

    game.handle_world_click(game.camera.world_to_screen(bed.center))

    assert game.selected_id == bed.object_id
    assert game.navigation_path == route
    assert game.walk_target == route[-1]


def test_shift_persistence_details_show_current_chances(monkeypatch) -> None:
    game = Game(fullscreen=False)
    tree = game.object_of_type("tree")
    tree.state = encode_tree_state(
        {"form": "stump", "branch_taken": True, "stump_memory_count": 2}
    )
    tree.form = "stump"
    assert game.object_persistence_details(tree) == [
        "Stump memory count: 3",
        "Persistence chance: 0.300%",
    ]
    game.map.tile_states[(1, 1)] = LevelTileState(1, 1, till_count=4)
    assert game.tile_persistence_details(1, 1) == [
        "Till memory count: 4",
        "Persistence chance: 0.400%",
    ]

    game.selected_id = tree.object_id
    monkeypatch.setattr(pygame.key, "get_mods", lambda: pygame.KMOD_SHIFT)
    game.draw_ui()


def test_left_clicking_empty_ground_does_not_move_player() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    start = (game.player.x, game.player.y)
    tile_map = game.map.tile_map
    player_column, player_row, _ = tile_map.tile_at_world(*start)
    target = next(
        tile_map.tile_center(column, row)
        for row in range(player_row - 3, player_row + 4)
        for column in range(player_column - 3, player_column + 4)
        if tile_map.can_stand_at(*tile_map.tile_center(column, row), 14)
        and not any(prop.startswith("object:") for prop in tile_map.tile_at(column, row).properties)
        and tile_map.tile_center(column, row) != start
    )
    game.camera.center_on(start, (game.map.width, game.map.height))

    game.handle_world_click(game.camera.world_to_screen(target))

    assert game.navigation_path == []
    assert game.walk_target is None
    assert (game.player.x, game.player.y) == start


def test_right_clicking_empty_ground_offers_move_to() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    tile_map = game.map.tile_map
    start = (game.player.x, game.player.y)
    player_column, player_row, _ = tile_map.tile_at_world(*start)
    target = next(
        tile_map.tile_center(column, row)
        for row in range(player_row - 3, player_row + 4)
        for column in range(player_column - 3, player_column + 4)
        if tile_map.can_stand_at(*tile_map.tile_center(column, row), 14)
        and not any(prop.startswith("object:") for prop in tile_map.tile_at(column, row).properties)
        and tile_map.tile_center(column, row) != start
    )
    game.camera.center_on(start, (game.map.width, game.map.height))

    game.handle_context_click(game.camera.world_to_screen(target))

    assert game.context_menu_options == ["Move To"]
    assert game.context_ground_target == target
    game.activate_context_option(0)
    assert game.navigation_path
    assert game.walk_target == target


def test_number_key_activates_selected_sidebar_action() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    game.selected_id = game.object_of_type("bed").object_id
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_1))

    game.handle_events()

    assert game.pending_job is not None
    assert game.pending_job.action == "Sleep"


def test_out_of_range_context_menu_shortcut_is_ignored() -> None:
    game = Game(fullscreen=False)
    game.day.mode = Mode.DIRECT
    game.selected_id = game.object_of_type("bed").object_id
    game.context_menu_options = ["Move To", "Sleep"]
    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_6))

    game.handle_events()

    assert game.pending_job is None
    assert game.context_menu_options == ["Move To", "Sleep"]


def test_build_path_points_include_start_and_end() -> None:
    points = build_path_points((0, 0), (10, 0), steps=2)
    assert points[0] == (0, 0)
    assert points[-1] == (10, 0)
    assert len(points) == 3


def test_screen_to_world_ignores_panel_clicks() -> None:
    game = Game(fullscreen=False)
    assert game.screen_to_world((10, 80)) is None
    assert game.screen_to_world((RIGHT_DOCK_RECT.x + 10, 80)) is None
    assert game.screen_to_world((MAP_VIEWPORT.centerx, MAP_VIEWPORT.top + 40)) is not None


def test_tall_grass_stays_outside_buildings() -> None:
    world = create_world()
    grass = next(obj for obj in world.values() if obj.type_id == "grass")
    assert grass.x >= 550 or grass.y >= 320


def test_area_gather_command_selects_only_matching_objects() -> None:
    game = Game(fullscreen=False)

    targets = game.build_area_targets("Gather Pebbles", (0, 0, game.map.width, game.map.height))

    assert targets
    assert all(game.objects[target.target_id].type_id == "pebble" for target in targets)


def test_water_command_targets_shallow_water_tile_centers() -> None:
    game = Game(fullscreen=False)
    give_bucket(game)

    targets = game.build_area_targets("Gather Water", (0, 0, game.map.width, game.map.height))

    assert targets
    assert all(target.target_id is None for target in targets)
    assert all(game.map.tile_map.is_tile_center(*target.point) for target in targets)

    game.player.x, game.player.y = targets[0].point
    game.pending_area_target = targets[0]
    game.update(0.0)
    assert game.player.bucket_filled is True
    assert "Gather Water" not in available_area_commands(game.player)


def test_carrying_hoe_unlocks_tilling_grassland_into_soil() -> None:
    game = Game(fullscreen=False)
    assert "Till Grassland" not in available_area_commands(game.player)
    assert game.build_area_targets("Till Grassland", (0, 0, game.map.width, game.map.height)) == []

    game.player.has_hoe = True
    game.player.carrying_hoe = True
    assert "Till Grassland" in available_area_commands(game.player)
    targets = game.build_area_targets("Till Grassland", (0, 0, game.map.width, game.map.height))
    assert targets

    target = targets[0]
    game.player.x, game.player.y = target.point
    game.pending_area_target = AreaTarget("Till Grassland", target.point)
    duration = tilling_duration_seconds(game.player.hoe_quality)
    game.update(duration - 0.1)
    assert game.map.tile_map.tile_at_world(*target.point)[2].kind is TileKind.GRASSLAND
    game.update(0.1)

    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    assert tile.kind is TileKind.GRASSLAND
    assert game.map.tile_states[(column, row)].till_percentage > 0
    state = game.map.tile_states[game.map.tile_map.tile_at_world(*target.point)[:2]]
    assert state.till_count == 1
    assert state.tilled_today is True

    game.player.inventory["seed"] = 1
    assert "Plant Wheat" in available_area_commands(game.player)
    planting_targets = game.build_area_targets("Plant Wheat", (*target.point, *target.point))
    assert len(planting_targets) == 1
    assert state.permanent_kind is None
    game.pending_area_target = planting_targets[0]
    planting_time = planting_duration_seconds(game.player.has_basket)
    game.update(planting_time - 0.1)
    assert state.crop is None
    game.update(0.1)

    assert state.crop == "wheat"
    assert game.player.inventory["seed"] == 0
    assert "crop:wheat" in game.map.tile_map.tile_at_world(*target.point)[2].properties

    give_bucket(game)
    game.player.bucket_filled = True
    watering_targets = game.build_area_targets("Water Crops", (*target.point, *target.point))
    assert len(watering_targets) == 1
    game.pending_area_target = watering_targets[0]
    game.update(0.0)
    assert state.watered is True
    assert state.crop_growth == 0.0
    assert game.player.bucket_water_uses == 4

    state.crop_growth = 1.0
    tile = game.map.tile_map.tile_at_world(*target.point)[2]
    tile.properties = [prop for prop in tile.properties if not prop.startswith("crop_growth:")]
    tile.properties.append("crop_growth:1.0")
    harvesting_targets = game.build_area_targets("Harvest Wheat", (*target.point, *target.point))
    assert len(harvesting_targets) == 1
    game.pending_area_target = harvesting_targets[0]
    duration = game.map.object_types["crop"].form_definition(
        "mature", "wheat"
    ).interactions["Harvest Wheat"]["duration_seconds"]
    harvest_time = float(duration["base"])
    game.update(harvest_time)
    assert state.crop is None
    assert game.player.inventory["wheat"] == 3


def test_tilling_gathers_a_loose_item_before_tilling_its_tile() -> None:
    game = Game(fullscreen=False)
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    clear_target = next(
        target
        for target in game.build_area_targets(
            "Till Grassland", (0, 0, game.map.width, game.map.height)
        )
        if not target.prerequisite_target_ids
    )
    column, row, tile = game.map.tile_map.tile_at_world(*clear_target.point)
    pebble = game.object_of_type("pebble")
    pebble.x = round(clear_target.point[0] - pebble.width / 2)
    pebble.y = round(clear_target.point[1] - pebble.height / 2)
    tile.properties.append(f"object:{pebble.object_id}")

    targets = game.build_area_targets(
        "Till Grassland", (*clear_target.point, *clear_target.point)
    )
    assert len(targets) == 1
    assert targets[0].prerequisite_target_ids == (pebble.object_id,)

    starting_pebbles = game.player.inventory["pebble"]
    game.player.x, game.player.y = clear_target.point
    game.queue_area_command(
        "Till Grassland",
        (*clear_target.point, *clear_target.point),
        1,
        record=True,
    )
    assert game.day.today_routine[-1].action == "Till Grassland"
    for _ in range(2_000):
        game.update(0.05)
        if game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL:
            break

    assert pebble.active is False
    assert game.player.inventory["pebble"] == starting_pebbles + 1
    assert game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL


def test_berry_bush_can_be_pulled_and_is_pulled_before_tilling() -> None:
    game = Game(fullscreen=False)
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    target = next(
        candidate
        for candidate in game.build_area_targets(
            "Till Grassland", (0, 0, game.map.width, game.map.height)
        )
        if not candidate.prerequisite_target_ids
    )
    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    bush = game.object_of_type("bush")
    bush.x = round(target.point[0] - bush.width / 2)
    bush.y = round(target.point[1] - bush.height / 2)
    tile.properties.append(f"object:{bush.object_id}")

    assert "Pull Berry Bush" in object_action_menu_options(bush, game.player)
    targets = game.build_area_targets(
        "Till Grassland", (*target.point, *target.point)
    )
    assert len(targets) == 1
    assert targets[0].prerequisite_target_ids == (bush.object_id,)

    starting_inventory = game.player.inventory.copy()
    game.player.x, game.player.y = target.point
    game.queue_area_command(
        "Till Grassland", (*target.point, *target.point), 1, record=True
    )
    for _ in range(2_000):
        game.update(0.05)
        if game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL:
            break

    assert bush.active is False
    assert any(obj.type_id == "berry" for obj in game.player.carried_objects)
    assert game.player.inventory["fiber"] == starting_inventory["fiber"] + 1
    assert game.player.inventory["branch"] == starting_inventory["branch"] + 1
    assert game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL


def test_tilling_uses_a_fixed_fifteen_minute_action_for_now() -> None:
    assert tilling_duration_seconds(1) == pytest.approx(15.0)
    assert tilling_duration_seconds(20) == pytest.approx(15.0)
    assert tilling_duration_seconds(100) == pytest.approx(15.0)


def test_time_speed_scales_tilling_without_changing_game_time_cost() -> None:
    game = Game(fullscreen=False)
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    targets = game.build_area_targets(
        "Till Grassland", (0, 0, game.map.width, game.map.height)
    )
    target = next(candidate for candidate in targets if not candidate.prerequisite_target_ids)
    game.player.x, game.player.y = target.point
    game.pending_area_target = target
    game.time_speed = 10.0
    start_minutes = game.day.current_time_minutes
    duration = tilling_duration_seconds(game.player.hoe_quality)

    game.update(duration / game.time_speed - 0.01)
    assert game.map.tile_map.tile_at_world(*target.point)[2].kind is TileKind.GRASSLAND
    game.update(0.01)

    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    assert tile.kind is TileKind.GRASSLAND
    assert game.map.tile_states[(column, row)].till_percentage > 0
    assert game.day.current_time_minutes == start_minutes + int(duration)


def test_area_command_quantity_selector_limits_nearest_targets(monkeypatch) -> None:
    game = Game(fullscreen=False)
    game.area_command_quantity = 1
    game.draw_ui()
    increase_rect = next(
        rect for rect, change in game.area_quantity_buttons if change == 1
    )
    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, button=1, pos=increase_rect.center
        )
    )
    game.handle_events()
    assert game.area_command_quantity == 2

    targets = [
        AreaTarget("Gather", (game.player.x + distance, game.player.y))
        for distance in (30, 10, 20)
    ]
    monkeypatch.setattr(game, "build_area_targets", lambda command, bounds: targets)
    game.active_command = "Gather Pebbles"
    game.command_drag_start = (game.player.x, game.player.y)
    game.finish_command_drag((200, 100))

    assert [target.point for target in game.area_targets] == [
        (game.player.x + 10, game.player.y),
        (game.player.x + 20, game.player.y),
    ]
    assert len(game.day.today_routine) == 1
    remembered = game.day.today_routine[0]
    assert remembered.action == "Gather Pebbles"
    assert remembered.area_bounds is not None
    assert remembered.quantity == 2


def test_explicit_nearest_mode_chooses_nearest_targets_for_farm_command(
    monkeypatch,
) -> None:
    game = Game(fullscreen=False)
    game.area_command_quantity = 2
    targets = [
        AreaTarget("Tend Plant", (game.player.x + distance, game.player.y))
        for distance in (30, 10, 20)
    ]
    monkeypatch.setattr(game, "build_area_targets", lambda command, bounds: targets)
    game.target_selection_mode = "nearest"
    game.select_area_command("Tend Crops")

    assert [target.point for target in game.area_targets] == [
        (game.player.x + 10, game.player.y),
        (game.player.x + 20, game.player.y),
    ]
    remembered = game.day.today_routine[-1]
    assert remembered.action == "Tend Crops"
    assert remembered.quantity == 2
    assert remembered.nearest_to_player is True
    assert remembered.area_bounds is None


def test_target_and_area_modes_produce_distinct_map_selections(monkeypatch) -> None:
    game = Game(fullscreen=False)
    captured: list[tuple[int, int, int, int]] = []
    monkeypatch.setattr(
        game,
        "queue_area_command",
        lambda _command, bounds, _quantity, **_kwargs: captured.append(bounds),
    )
    size = game.map.tile_map.tile_size
    start = (10 * size + 2, 12 * size + 3)
    end = (13 * size + 4, 15 * size + 5)

    game.target_selection_mode = "target"
    game.active_command = "Gather Pebbles"
    game.command_drag_start = start
    game.camera.center_on(end, (game.map.width, game.map.height))
    game.finish_command_drag(game.camera.world_to_screen(end))
    assert captured[-1] == (13 * size, 15 * size, 14 * size, 16 * size)

    game.target_selection_mode = "area"
    game.active_command = "Gather Pebbles"
    game.command_drag_start = start
    game.camera.center_on(end, (game.map.width, game.map.height))
    game.finish_command_drag(game.camera.world_to_screen(end))
    assert captured[-1] == (10 * size, 12 * size, 14 * size, 16 * size)


def test_command_menu_exposes_three_explicit_target_modes() -> None:
    game = Game(fullscreen=False)
    game.active_command_category = "Gather"

    game.draw_ui()

    assert [mode for _rect, mode in game.target_selection_buttons] == [
        "nearest",
        "target",
        "area",
    ]


def test_replay_reissues_an_area_command_against_the_current_world() -> None:
    game = Game(fullscreen=False)
    bounds = (0, 0, game.map.width, game.map.height)
    available_now = game.build_area_targets("Gather Pebbles", bounds)
    assert len(available_now) >= 2
    game.day.remembered_routine = [
        RoutineStep(None, "Gather Pebbles", area_bounds=bounds, quantity=2)
    ]
    game.day.mode = Mode.MORNING

    game.choose_morning_option("Replay Remembered Routine")
    game.update(0.0)

    assert game.day.replay_index == 1
    assert len(game.area_targets) == 2
    assert game.day.today_routine == []

    game.update(0.0)
    assert game.pending_job is not None
    assert game.pending_job.action == "Gather"
    assert game.pending_job.advances_replay is False


def test_empty_replayed_area_command_visits_area_and_shows_thought(monkeypatch) -> None:
    game = Game(fullscreen=False)
    tile_size = game.map.tile_map.tile_size
    column = int(game.player.x // tile_size)
    row = int(game.player.y // tile_size)
    bounds = (
        column * tile_size,
        row * tile_size,
        (column + 1) * tile_size,
        (row + 1) * tile_size,
    )
    monkeypatch.setattr(game, "build_area_targets", lambda command, area: [])
    game.day.remembered_routine = [
        RoutineStep(None, "Gather Pebbles", area_bounds=bounds, quantity=10)
    ]
    game.day.mode = Mode.MORNING

    game.choose_morning_option("Replay Remembered Routine")
    for _ in range(100):
        game.update(0.05)
        if game.thought_bubble_text is not None:
            break

    assert game.day.mode is Mode.REPLAY
    assert game.thought_bubble_text == (
        "I came here to gather pebbles, but there are no pebbles here."
    )
    assert game.thought_bubble_timer > 0.0
    center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    assert math.dist((game.player.x, game.player.y), center) <= 4


def test_unavailable_replayed_object_command_visits_remembered_spot() -> None:
    game = Game(fullscreen=False)
    destination = (game.player.x, game.player.y)
    game.day.remembered_routine = [
        RoutineStep(999_999, "Gather", "missing_type", target_point=destination)
    ]
    game.day.mode = Mode.MORNING

    game.choose_morning_option("Replay Remembered Routine")
    for _ in range(10):
        game.update(0.05)
        if game.thought_bubble_text is not None:
            break

    assert game.day.mode is Mode.REPLAY
    assert game.thought_bubble_text == (
        "I came here to gather, but the remembered target is no longer available."
    )
    assert math.dist((game.player.x, game.player.y), destination) <= 4


def test_area_selection_snaps_outward_to_tile_edges_in_both_directions() -> None:
    assert tile_aligned_area_bounds((35, 70), (95, 127), 32) == (32, 64, 96, 128)
    assert tile_aligned_area_bounds((95, 127), (35, 70), 32) == (32, 64, 96, 128)
    assert tile_aligned_area_bounds((33, 65), (33, 65), 32) == (32, 64, 64, 96)


def test_multi_target_gather_does_not_pause_between_items() -> None:
    game = Game(fullscreen=False)
    targets = game.build_area_targets(
        "Gather Pebbles", (0, 0, game.map.width, game.map.height)
    )[:2]
    assert len(targets) == 2
    game.area_targets = targets
    game.time_speed = 10.0

    for _ in range(2_000):
        game.update(0.05)
        if game.player.inventory["pebble"] == 1:
            break

    assert game.player.inventory["pebble"] == 1
    assert game.area_targets or game.pending_job is not None
    assert game.time_speed == 10.0
    assert game.simulation_paused is False

    for _ in range(2_000):
        game.update(0.05)
        if game.player.inventory["pebble"] == 2:
            break
    assert game.player.inventory["pebble"] == 2


def test_basket_speeds_planting_and_harvesting() -> None:
    assert planting_duration_seconds(True) < planting_duration_seconds(False)
    duration = load_object_types()["crop"].form_definition(
        "mature", "wheat"
    ).interactions["Harvest Wheat"]["duration_seconds"]
    assert duration["with_basket"] < duration["base"]


def test_weaving_basket_creates_a_carried_object() -> None:
    game = Game(fullscreen=False)
    workbench = game.object_of_type("workbench")
    game.player.inventory["fiber"] = 3

    game.complete_job(PendingJob(workbench.object_id, "Weave Fiber Basket"))

    assert game.player.has_basket is True
    assert game.player.basket is not None
    assert game.player.basket.container == "player"
    assert game.player.basket in game.objects.values()
    assert game.player.inventory["fiber"] == 0


def test_crop_growth_uses_time_water_and_tending_multipliers() -> None:
    game = Game(fullscreen=False)
    growth = game.map.object_types["crop"].growth
    state = LevelTileState(0, 0, crop="wheat")
    game.map.tile_states[(0, 0)] = state

    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(60.0 / growth["base_minutes"])

    state.crop_growth = 0.0
    state.watered = True
    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(
        60.0 * growth["water_multiplier"] / growth["base_minutes"]
    )

    state.crop_growth = 0.0
    state.watered = False
    state.tended = True
    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(
        60.0 * growth["tended_multiplier"] / growth["base_minutes"]
    )


def test_planted_soil_can_be_selected_watered_and_tended() -> None:
    game = Game(fullscreen=False)
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    targets = game.build_area_targets(
        "Till Grassland", (0, 0, game.map.width, game.map.height)
    )
    target = next(candidate for candidate in targets if not candidate.prerequisite_target_ids)
    column, row, tile = game.map.tile_map.tile_at_world(*target.point)
    tile.kind = TileKind.SOIL
    state = LevelTileState(column, row, crop="wheat")
    game.map.tile_states[(column, row)] = state
    give_bucket(game)
    game.player.bucket_filled = True

    assert build_ground_context_menu_options(tile, game.player, state) == [
        "Move To", "Drop Bucket", "Water Crop", "Tend Plant"
    ]
    game.camera.center_on(target.point, (game.map.width, game.map.height))
    game.handle_world_click(game.camera.world_to_screen(target.point))
    assert game.selected_tile == (column, row)

    game.player.x, game.player.y = target.point
    game.context_ground_target = target.point
    game.context_menu_options = ["Move To", "Water Crop", "Tend Plant"]
    game.activate_context_option(2)
    game.update(0.0)
    assert state.tended is True


def test_tend_crops_is_a_multi_target_farm_area_command() -> None:
    game = Game(fullscreen=False)
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    candidates = [
        target
        for target in game.build_area_targets(
            "Till Grassland", (0, 0, game.map.width, game.map.height)
        )
        if not target.prerequisite_target_ids
    ][:2]
    assert len(candidates) == 2
    for target in candidates:
        column, row, tile = game.map.tile_map.tile_at_world(*target.point)
        tile.kind = TileKind.SOIL
        game.map.tile_states[(column, row)] = LevelTileState(
            column, row, crop="wheat", crop_growth=0.25
        )
    bounds = tile_aligned_area_bounds(
        candidates[0].point, candidates[1].point, game.map.tile_map.tile_size
    )

    targets = game.build_area_targets("Tend Crops", bounds)

    assert len(targets) == 2
    assert all(target.action == "Tend Plant" for target in targets)
    game.queue_area_command("Tend Crops", bounds, 2, record=True)
    game.time_speed = 10.0
    for _ in range(2_000):
        game.update(0.05)
        if not game.area_targets and game.pending_area_target is None:
            break
    assert all(
        game.map.tile_states[game.map.tile_map.tile_at_world(*target.point)[:2]].tended
        for target in candidates
    )
    assert game.day.today_routine[-1].action == "Tend Crops"


def test_chopping_tree_yields_wood_and_replaces_it_with_a_stump() -> None:
    game = Game(fullscreen=False)
    tree = next(
        obj
        for obj in game.objects.values()
        if obj.type_id == "tree"
        and game.map.tile_map.tile_at_world(*obj.center)[2].properties.count("blocked") == 1
        and "terrain:western_forest" not in game.map.tile_map.tile_at_world(*obj.center)[2].properties
    )
    occupied_tile = game.map.tile_map.tile_at_world(*tree.center)[:2]
    give_axe(game)

    game.complete_job(PendingJob(tree.object_id, "Chop Down Tree"))

    assert tree.active is True
    assert tree.form == "stump"
    assert game.player.inventory["wood"] == 3
    tile = game.map.tile_map.tile_at(*occupied_tile)
    assert f"object:{tree.object_id}" in tile.properties
    assert "blocked" in tile.properties


def test_chopped_tree_state_can_become_persistent_and_memory_is_retained(
    monkeypatch,
) -> None:
    game = Game(fullscreen=False)
    tree = game.object_of_type("tree")
    give_axe(game)
    game.complete_job(PendingJob(tree.object_id, "Chop Down Tree"))

    initial_count = int(tree_state_data(tree.state)["stump_memory_count"])
    monkeypatch.setattr(game, "policy_chance", lambda *args: 1.0)
    game.advance_stump_memories()

    assert tree.persistent is True
    assert tree.persistent_state.form == "stump"
    assert tree_state_data(tree.persistent_state.state)[
        "stump_memory_count"
    ] == initial_count + 1

    tree.persistent = False
    tree.persistent_state = None
    tree.state = encode_tree_state(
        {"form": "tree", "branch_taken": False, "stump_memory_count": 2}
    )
    tree.form = "standing"
    game.advance_stump_memories()
    assert tree_state_data(tree.state)["stump_memory_count"] == 2
