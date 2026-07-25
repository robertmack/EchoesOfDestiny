import json
import math

import pygame
import pytest

from remembering.game import (
    AreaTarget,
    CROP_BASE_GROWTH_MINUTES,
    CROP_TENDED_MULTIPLIER,
    CROP_WATERED_MULTIPLIER,
    DAY_FADE_DURATION_SECONDS,
    Game,
    PendingJob,
    area_commands_for_category,
    available_area_commands,
    build_context_menu_options,
    build_ground_context_menu_options,
    build_path_points,
    compact_label,
    harvesting_duration_seconds,
    object_map_label,
    object_action_menu_options,
    planting_duration_seconds,
    tilling_duration_seconds,
    tile_aligned_area_bounds,
)
from remembering.model import (
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
from remembering.world import DEFAULT_CURRENT_LEVEL_PATH, create_world


def current_level_copy(tmp_path):
    path = tmp_path / "current_level.json"
    path.write_text(DEFAULT_CURRENT_LEVEL_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_context_menu_includes_move_to_and_actions() -> None:
    player = PlayerState()
    obj = WorldObject("stick-1", "Stick", ObjectKind.STICK, 50, 50, 40, 40)
    options = build_context_menu_options(obj, player, (120, 120), {"stick-1": obj})

    assert options[0] == "Move To"
    assert options[1:] == ["Gather"]


def test_workbench_shows_disabled_recipes_and_sidebar_buttons_are_clickable() -> None:
    game = Game(fullscreen=False)
    workbench = game.object_of_type("workbench")
    options = object_action_menu_options(workbench, game.player)
    assert options == [
        "Craft Crude Hoe (materials required)",
        "Craft Crude Axe (materials required)",
        "Craft Wooden Bucket (materials required)",
        "Weave Fiber Basket (materials required)",
    ]

    game.selected_id = workbench.object_id
    game.activate_sidebar_action(0)
    assert game.pending_job is None

    game.player.inventory.update({"stick": 1, "stone": 1, "fiber": 1})
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

    game.player.has_bucket = True
    assert "Gather Water" not in available_area_commands(game.player)
    water_target = game.build_area_targets(
        "Gather Water", (0, 0, game.map.width, game.map.height)
    )[0]
    tile = game.map.tile_map.tile_at_world(*water_target.point)[2]
    assert build_ground_context_menu_options(tile, game.player) == [
        "Move To",
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
        "Gather Water (empty bucket required)",
    ]
    game.context_ground_target = water_target.point
    game.context_menu_options = disabled_options
    game.activate_context_option(1)
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
        "Gather Water",
    ]


def test_area_commands_are_grouped_into_submenus() -> None:
    player = PlayerState()
    assert area_commands_for_category(player, "Gather") == [
        "Gather Pebbles",
        "Gather Branches",
        "Gather Seeds",
        "Gather Tall Grass",
    ]
    assert area_commands_for_category(player, "Farm") == ["Tend Crops", "Harvest Wheat"]
    assert area_commands_for_category(player, "Build") == ["Build Barrel"]
    player.has_axe = True
    player.carrying_axe = True
    assert area_commands_for_category(player, "Gather")[-1] == "Chop Trees"


def test_chop_trees_area_command_requires_axe_and_queues_tree_actions() -> None:
    game = Game(fullscreen=False)
    bounds = (0, 0, game.map.width, game.map.height)
    assert "Chop Trees" not in available_area_commands(game.player)
    assert game.build_area_targets("Chop Trees", bounds) == []

    game.player.has_axe = True
    game.player.carrying_axe = True
    targets = game.build_area_targets("Chop Trees", bounds)

    assert targets
    assert all(target.action == "Chop Down Tree" for target in targets)
    assert all(game.objects[target.target_id].kind is ObjectKind.TREE for target in targets)


def test_barrel_build_cost_comes_from_object_catalog_and_water_transfers() -> None:
    game = Game(fullscreen=False)
    assert dict(game.map.object_types["barrel"].build_cost) == {"wood": 5, "fiber": 2}
    game.player.inventory["wood"] = 5
    game.player.inventory["fiber"] = 2
    target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]

    game.build_barrel(target.placement_point or target.point)

    barrel = game.object_of_type("barrel")
    assert barrel.active is True
    assert barrel.persistent is False
    barrel_tile = game.map.tile_map.tile_at_world(*barrel.center)[2]
    assert f"object:{barrel.object_id}" in barrel_tile.properties
    assert "blocked" in barrel_tile.properties
    assert game.object_color(barrel) == (115, 78, 46)
    assert game.player.inventory["wood"] == 0
    assert game.player.inventory["fiber"] == 0
    game.player.has_bucket = True
    game.player.bucket_water_uses = 5
    game.complete_job(PendingJob(barrel.object_id, "Pour Water Into Barrel"))
    assert game.player.bucket_water_uses == 0
    assert game.barrel_state(barrel)["water_uses"] == 5
    game.complete_job(PendingJob(barrel.object_id, "Fill Bucket From Barrel"))
    assert game.player.bucket_water_uses == 5
    assert game.barrel_state(barrel)["water_uses"] == 0


def test_w_cheat_adds_one_wood_silently() -> None:
    game = Game(fullscreen=False)
    starting_wood = game.player.inventory["wood"]
    starting_messages = list(game.messages)

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_w))
    game.handle_events()

    assert game.player.inventory["wood"] == starting_wood + 1
    assert game.messages == starting_messages


def test_b_cheat_gives_an_empty_bucket_silently() -> None:
    game = Game(fullscreen=False)
    game.player.has_bucket = False
    game.player.bucket_water_uses = 3
    starting_messages = list(game.messages)

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_b))
    game.handle_events()

    assert game.player.has_bucket is True
    assert game.player.bucket_water_uses == 0
    assert game.messages == starting_messages


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
    assert json.loads(barrel.persistent_state.state)["water_uses"] == 0

    data = game.barrel_state(barrel)
    data["water_uses"] = 12
    barrel.state = json.dumps(data)
    game.advance_barrel_memories()
    remembered = json.loads(barrel.persistent_state.state)
    assert remembered["water_uses"] == 12


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
    barrel.state = json.dumps(barrel_data)
    game.player.has_bucket = True
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
    game.player.has_bucket = True
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
    game.player.has_bucket = True
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
    assert [command for _, command in game.command_buttons] == ["Harvest Wheat", "Back"]


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


def test_map_label_uses_object_type_instead_of_state() -> None:
    field = WorldObject("field", "Overgrown Field", ObjectKind.FIELD, 0, 0, 32, 32, state="mature")
    bed = WorldObject("bed", "Broken Bed", ObjectKind.BED, 0, 0, 32, 32, state="damaged")

    assert object_map_label(field) == "Field"
    assert object_map_label(bed) == "Bed"


def test_can_stand_at_rejects_house_walls_and_world_edges() -> None:
    game = Game(fullscreen=False)
    assert game.can_stand_at(game.player.x, game.player.y) is True
    assert game.can_stand_at(61 * 32 - 10, 64 * 32) is False
    assert game.can_stand_at(64 * 32 + 16, 63 * 32 + 16) is True
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
    game.object_of_type("stick").active = False
    game.object_of_type("bed").state = "repaired"
    game.player.inventory["stick"] = 3

    game.finish_day()

    saved = persistence_path.read_text(encoding="utf-8")
    assert '"type": "stick"' in saved
    assert '"persistent_state"' in saved
    assert '"state": "repaired"' not in saved
    assert game.object_of_type("stick").active is True
    assert game.object_of_type("bed").state == ""
    assert not game.player.inventory
    assert game.day.number == 2


def test_stored_tool_can_become_persistent_and_unstored_progress_decays(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    storage = game.object_of_type("tool_storage")
    game.player.has_hoe = True
    game.player.carrying_hoe = True
    game.player.hoe_quality = 20
    game.complete_job(PendingJob(storage.object_id, "Store Hoe"))
    hoe_memory = game.storage_memories[storage.object_id]["hoe"]
    assert hoe_memory["store_count"] == 0
    assert hoe_memory["present"] is True

    game.map.permanent_soil_chance_per_till = 1.0
    game.map.till_count_loss_chance = 0.0
    game.finish_day()
    hoe_memory = game.storage_memories[storage.object_id]["hoe"]
    assert hoe_memory["store_count"] == 1
    assert hoe_memory["persistent"] is True
    assert game.player.has_hoe is True
    assert game.player.carrying_hoe is False

    axe_memory = game.storage_memories[storage.object_id]["axe"]
    axe_memory["store_count"] = 3
    axe_memory["present"] = False
    game.map.till_count_loss_chance = 1.0
    game.advance_storage_memories()
    assert axe_memory["store_count"] == 2

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
    stick = game.object_of_type("stick")
    stick.active = False
    game.player.inventory["stick"] = 1
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
    assert game.day.number == 2
    assert game.object_of_type("stick").active is True
    assert not game.player.inventory
    restored_tile = game.map.tile_map.tile_at(column, row)
    assert restored_tile.kind is TileKind.GRASSLAND
    assert not any(prop.startswith("crop:") for prop in restored_tile.properties)
    assert game.map.tile_states[(column, row)].crop is None
    restored_permanent = game.map.tile_map.tile_at(permanent_column, permanent_row)
    assert restored_permanent.kind is TileKind.SOIL
    assert game.map.tile_states[(permanent_column, permanent_row)].crop is None
    assert not any(prop.startswith("crop:") for prop in restored_permanent.properties)

    game.update(DAY_FADE_DURATION_SECONDS)
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
        if game.day.number == 2 and game.day_transition_phase is None:
            break

    assert game.day.number == 2
    assert game.day.current_time_minutes == 360
    assert game.map.tile_map.tile_at(column, row).kind is TileKind.GRASSLAND
    assert game.simulation_paused is True


def test_replay_can_begin_with_a_gather_job_on_the_next_day(tmp_path) -> None:
    game = Game(fullscreen=False, persistence_path=current_level_copy(tmp_path))
    stick = game.object_of_type("stick")
    stick.active = False
    game.player.inventory["stick"] = 1
    game.day.today_routine = [RoutineStep(stick.object_id, "Gather")]

    game.finish_day()
    game.choose_morning_option("Replay Remembered Routine")
    game.update(0.0)

    assert game.pending_job is not None
    assert game.pending_job.target_id == stick.object_id
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
    assert game.thought_bubble_text == "Why did I come here?"


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
    assert game.object_persistence_details(tree) == [
        "Stump memory count: 3",
        "Persistence chance: 0.003%",
    ]
    game.map.tile_states[(1, 1)] = LevelTileState(1, 1, till_count=4)
    assert game.tile_persistence_details(1, 1) == [
        "Till memory count: 4",
        "Persistence chance: 0.004%",
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
    assert game.screen_to_world((1000, 80)) is None
    assert game.screen_to_world((220, 80)) is not None


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
    game.player.has_bucket = True

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

    assert game.map.tile_map.tile_at_world(*target.point)[2].kind is TileKind.SOIL
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

    game.player.has_bucket = True
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
    harvest_time = harvesting_duration_seconds(game.player.has_basket)
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

    starting_stones = game.player.inventory["stone"]
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
    assert game.player.inventory["stone"] == starting_stones + 1
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
    bush = game.object_of_type("berry_bush")
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
    assert game.player.inventory["berries"] == starting_inventory["berries"] + 1
    assert game.player.inventory["fiber"] == starting_inventory["fiber"] + 1
    assert game.player.inventory["stick"] == starting_inventory["stick"] + 1
    assert game.map.tile_map.tile_at(column, row).kind is TileKind.SOIL


def test_low_quality_hoe_takes_much_longer_to_till() -> None:
    assert tilling_duration_seconds(20) == pytest.approx(20.4)
    assert tilling_duration_seconds(20) > tilling_duration_seconds(100) * 3


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

    assert game.map.tile_map.tile_at_world(*target.point)[2].kind is TileKind.SOIL
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
    assert game.thought_bubble_text == "Why did I come here?"
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
    assert game.thought_bubble_text == "Why did I come here?"
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
        if game.player.inventory["stone"] == 1:
            break

    assert game.player.inventory["stone"] == 1
    assert game.area_targets or game.pending_job is not None
    assert game.time_speed == 10.0
    assert game.simulation_paused is False

    for _ in range(2_000):
        game.update(0.05)
        if game.player.inventory["stone"] == 2:
            break
    assert game.player.inventory["stone"] == 2


def test_basket_speeds_planting_and_harvesting() -> None:
    assert planting_duration_seconds(True) < planting_duration_seconds(False)
    assert harvesting_duration_seconds(True) < harvesting_duration_seconds(False)


def test_crop_growth_uses_time_water_and_tending_multipliers() -> None:
    game = Game(fullscreen=False)
    state = LevelTileState(0, 0, crop="wheat")
    game.map.tile_states[(0, 0)] = state

    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(60.0 / CROP_BASE_GROWTH_MINUTES)

    state.crop_growth = 0.0
    state.watered = True
    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(
        60.0 * CROP_WATERED_MULTIPLIER / CROP_BASE_GROWTH_MINUTES
    )

    state.crop_growth = 0.0
    state.watered = False
    state.tended = True
    game.advance_crop_growth(60.0)
    assert state.crop_growth == pytest.approx(
        60.0 * CROP_TENDED_MULTIPLIER / CROP_BASE_GROWTH_MINUTES
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
    game.player.has_bucket = True
    game.player.bucket_filled = True

    assert build_ground_context_menu_options(tile, game.player, state) == [
        "Move To", "Water Crop", "Tend Plant"
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
    game.player.has_axe = True
    game.player.carrying_axe = True

    game.complete_job(PendingJob(tree.object_id, "Chop Down Tree"))

    assert tree.active is True
    assert tree_state_data(tree.state)["form"] == "stump"
    assert game.player.inventory["wood"] == 3
    tile = game.map.tile_map.tile_at(*occupied_tile)
    assert f"object:{tree.object_id}" in tile.properties
    assert "blocked" in tile.properties


def test_chopped_tree_state_can_become_persistent_and_memory_can_decay() -> None:
    game = Game(fullscreen=False)
    tree = game.object_of_type("tree")
    game.player.has_axe = True
    game.player.carrying_axe = True
    game.complete_job(PendingJob(tree.object_id, "Chop Down Tree"))
    game.map.permanent_soil_chance_per_till = 1.0

    game.advance_stump_memories()

    assert tree.persistent is True
    assert tree_state_data(tree.persistent_state.state)["form"] == "stump"
    assert tree_state_data(tree.persistent_state.state)["stump_memory_count"] == 1

    tree.persistent = False
    tree.persistent_state = None
    tree.state = encode_tree_state(
        {"form": "tree", "branch_taken": False, "stump_memory_count": 2}
    )
    game.map.till_count_loss_chance = 1.0
    game.advance_stump_memories()
    assert tree_state_data(tree.state)["stump_memory_count"] == 1
