import math
import json

import pytest

from remembering.jsonc import loads_jsonc

from remembering.model import LevelTileState, ObjectKind, RoomQuality, RoutineStep
from remembering.tiles import TRAVERSABLE_TILE_KINDS, Tile, TileEdge, TileKind, TileMap
from remembering.world import (
    DEFAULT_CURRENT_LEVEL_PATH,
    DEFAULT_MAP_PATH,
    DEFAULT_OBJECT_TYPES_PATH,
    DEFAULT_TILE_TYPES_PATH,
    MapLoadError,
    advance_level_tile_states,
    initialize_current_level_from_map,
    load_memory_file,
    load_map,
    memory_file_path,
    save_memory_file,
    save_persistent_objects,
    sync_current_level_from_map,
    structure_wall_rects,
    terrain_blocks_point,
    _spread_spawn_influence,
    _apply_boundaries,
    _boundary_from_entry,
)
from remembering.sprites import overlay_alpha
from remembering.model import SpriteOverlay


def test_boundary_edges_normalize_to_shared_north_west_addresses() -> None:
    east = _boundary_from_entry({"id": "a", "type": "fence", "x": 2, "y": 3, "edge": "east"})
    south = _boundary_from_entry({"id": "b", "type": "wall", "x": 4, "y": 5, "edge": "south"})

    assert (east.column, east.row, east.edge) == (3, 3, "west")
    assert (south.column, south.row, south.edge) == (4, 6, "north")


def test_boundary_door_controls_both_sides_of_tile_edge() -> None:
    tile_map = TileMap(2, 1, 64, [Tile(TileKind.GRASSLAND), Tile(TileKind.GRASSLAND)])
    closed = _boundary_from_entry({"id": "door", "type": "door", "x": 0, "y": 0, "edge": "east", "locked": True})
    _apply_boundaries(tile_map, [closed])
    assert not tile_map.tile_at(0, 0).passable[TileEdge.EAST]
    assert not tile_map.tile_at(1, 0).passable[TileEdge.WEST]

    opened = _boundary_from_entry({"id": "door", "type": "door", "x": 0, "y": 0, "edge": "east", "open": True})
    _apply_boundaries(tile_map, [opened])
    assert tile_map.tile_at(0, 0).passable[TileEdge.EAST]
    assert tile_map.tile_at(1, 0).passable[TileEdge.WEST]


def test_state_driven_sprite_overlay_alpha_supports_ranges_capacity_and_presence() -> None:
    progress = SpriteOverlay("tilled", "till_percentage", value_max=100)
    assert overlay_alpha(progress, {"till_percentage": 0}) == 0
    assert overlay_alpha(progress, {"till_percentage": 40}) == 102
    assert overlay_alpha(progress, {"till_percentage": 100}) == 255

    capacity = SpriteOverlay("water", "water_uses", capacity_resource="water")
    assert overlay_alpha(capacity, {"water_uses": 15}, {"water": 30}) == 128

    presence = SpriteOverlay("apples", "has_apples")
    assert overlay_alpha(presence, {"has_apples": False}) == 0
    assert overlay_alpha(presence, {"has_apples": True}) == 255


def test_memory_file_round_trip_uses_tile_coordinates(tmp_path) -> None:
    routine = [
        RoutineStep(
            None,
            "Gather Pebbles",
            area_bounds=(64, 128, 192, 256),
            quantity=3,
            nearest_to_player=True,
        )
    ]

    path = save_memory_file(
        "my routine", routine, tile_size=64, directory=tmp_path
    )
    loaded = load_memory_file("my routine.jsonc", tile_size=64, directory=tmp_path)

    assert path == tmp_path / "my routine.jsonc"
    assert loaded == tuple(routine)
    assert json.loads(path.read_text(encoding="utf-8"))["commands"][0][
        "area_bounds"
    ] == {
        "start": {"tilexy": [1, 2], "subtilexy": [0, 0]},
        "end": {"tilexy": [3, 4], "subtilexy": [0, 0]},
    }


def test_memory_filename_rejects_paths(tmp_path) -> None:
    with pytest.raises(MapLoadError, match="safe filename"):
        memory_file_path("../elsewhere", tmp_path)


def test_default_map_loads_authored_structures_and_objects() -> None:
    map_definition = load_map()

    assert map_definition.name == "Broken Homestead"
    bed = next(obj for obj in map_definition.objects.values() if obj.type_id == "bed")
    assert bed.kind is ObjectKind.BED
    assert bed.description
    assert {building.building_id for building in map_definition.buildings} == {"house", "workshop"}
    assert all(structure.kind == "room" for structure in map_definition.structures)
    rooms = [structure for structure in map_definition.structures if structure.kind == "room"]
    assert all(room.doors for room in rooms)
    assert all(room.quality in RoomQuality for room in rooms)
    assert all(room.display_color is not None and len(room.display_color) == 3 for room in rooms)


def test_default_map_has_visible_closed_boundaries_at_every_doorway() -> None:
    map_definition = load_map(persistence_path=None)
    doors = {
        (boundary.column, boundary.row, boundary.edge)
        for boundary in map_definition.boundaries
        if boundary.kind == "door" and not boundary.open and not boundary.locked
    }

    assert doors == {
        (67, 64, "west"),
        (67, 69, "west"),
        (69, 72, "north"),
        (55, 69, "north"),
    }


def test_bed_uses_authored_instance_coordinates() -> None:
    map_definition = load_map()
    map_data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))

    bed = next(obj for obj in map_definition.objects.values() if obj.type_id == "bed")
    authored = next(instance for instance in map_data["objects"] if instance["type"] == "bed")
    tile_size = map_definition.tile_map.tile_size
    assert (bed.x, bed.y) == (authored["x"] * tile_size, authored["y"] * tile_size)


def test_generated_trees_stay_outside_rooms_and_clear_homestead() -> None:
    map_definition = load_map(persistence_path=None)
    bed_center = next(obj for obj in map_definition.objects.values() if obj.type_id == "bed").center
    trees = [obj for obj in map_definition.objects.values() if obj.kind is ObjectKind.TREE]

    assert len(trees) == 240
    assert min(math.dist(tree.center, bed_center) for tree in trees) >= 650
    distances = [math.dist(tree.center, bed_center) for tree in trees]
    assert sum(distance >= 900 for distance in distances) > sum(distance < 900 for distance in distances)
    for tree in trees:
        assert all(
            tree.x + tree.width <= room.x
            or room.x + room.width <= tree.x
            or tree.y + tree.height <= room.y
            or room.y + room.height <= tree.y
            for room in map_definition.structures
        )


def test_cardinal_terrain_features_form_impassable_boundaries() -> None:
    map_definition = load_map()
    terrain = {feature.kind: feature for feature in map_definition.terrain}
    size = map_definition.tile_map.tile_size

    assert terrain_blocks_point(terrain["mountains"], 64 * size, 5 * size)
    assert terrain_blocks_point(terrain["ocean"], 64 * size, 120 * size)
    assert terrain_blocks_point(terrain["dense_forest"], 5 * size, 64 * size)
    assert terrain_blocks_point(terrain["river"], 124 * size, 74 * size)
    assert not any(
        terrain_blocks_point(feature, 64 * size, 66 * size)
        for feature in terrain.values()
    )


def test_western_forest_contains_dense_trees_but_other_terrain_does_not() -> None:
    map_definition = load_map()
    trees = [obj for obj in map_definition.objects.values() if obj.kind is ObjectKind.TREE]
    western_forest = next(feature for feature in map_definition.terrain if feature.kind == "dense_forest")
    forbidden = [feature for feature in map_definition.terrain if feature.kind != "dense_forest"]

    assert sum(terrain_blocks_point(western_forest, *tree.center) for tree in trees) >= 50
    assert not any(
        terrain_blocks_point(feature, *tree.center)
        for tree in trees
        for feature in forbidden
    )


def test_world_is_rasterized_into_expected_tile_types() -> None:
    map_definition = load_map()
    tile_map = map_definition.tile_map
    size = tile_map.tile_size

    bed = next(obj for obj in map_definition.objects.values() if obj.type_id == "bed")
    assert tile_map.tile_at_world(*bed.center)[2].kind is TileKind.WOODEN_FLOOR
    assert tile_map.tile_at_world(64 * size, 5 * size)[2].kind is TileKind.MOUNTAIN
    assert tile_map.tile_at_world(64 * size, 20 * size)[2].kind is TileKind.HILLS
    assert tile_map.tile_at_world(64 * size, 24 * size)[2].kind is TileKind.GRASSLAND
    assert tile_map.tile_at_world(64 * size, 120 * size)[2].kind is TileKind.DEEP_WATER
    assert tile_map.tile_at_world(124 * size, 74 * size)[2].kind in {
        TileKind.SHALLOW_WATER,
        TileKind.DEEP_WATER,
    }


def test_large_objects_occupy_every_intersected_tile() -> None:
    map_definition = load_map()
    tile_map = map_definition.tile_map
    bed = next(obj for obj in map_definition.objects.values() if obj.type_id == "bed")

    first_column = bed.x // tile_map.tile_size
    last_column = (bed.x + bed.width - 1) // tile_map.tile_size
    row = bed.y // tile_map.tile_size
    occupied = [tile_map.tile_at(column, row) for column in range(first_column, last_column + 1)]
    assert all(f"object:{bed.object_id}" in tile.properties for tile in occupied)
    assert all("blocked" in tile.properties for tile in occupied)
    assert f"object:{bed.object_id}" not in tile_map.tile_at(last_column + 1, row).properties


def test_nonblocking_objects_still_record_tile_occupancy() -> None:
    map_definition = load_map()
    pebble = next(obj for obj in map_definition.objects.values() if obj.type_id == "pebble")
    tile = map_definition.tile_map.tile_at_world(*pebble.center)[2]

    assert f"object:{pebble.object_id}" in tile.properties
    assert "blocked" not in tile.properties


def test_room_wall_edges_are_closed_and_door_edges_are_open() -> None:
    tile_map = load_map().tile_map

    bedroom_wall = tile_map.tile_at(66, 63)
    bedroom_west_wall = tile_map.tile_at(61, 63)
    bedroom_south_wall = tile_map.tile_at(62, 66)
    bedroom_door = tile_map.tile_at(66, 64)
    common_door = tile_map.tile_at(67, 64)

    assert bedroom_wall.passable[TileEdge.EAST] is False
    assert bedroom_west_wall.passable[TileEdge.WEST] is False
    assert bedroom_south_wall.passable[TileEdge.SOUTH] is False
    assert bedroom_door.passable[TileEdge.EAST] is True
    assert common_door.passable[TileEdge.WEST] is True


def test_blocking_objects_do_not_create_rendered_wall_markers() -> None:
    map_definition = load_map()
    table = next(
        obj for obj in map_definition.objects.values() if obj.type_id == "table"
    )
    tile_map = map_definition.tile_map
    first_column = table.x // tile_map.tile_size
    first_row = table.y // tile_map.tile_size
    occupied = [
        tile_map.tile_at(column, row)
        for row in range(
            first_row, (table.y + table.height - 1) // tile_map.tile_size + 1
        )
        for column in range(
            first_column, (table.x + table.width - 1) // tile_map.tile_size + 1
        )
    ]

    assert all(not all(tile.passable.values()) for tile in occupied)
    assert all(
        not any(property_.startswith("wall:") for property_ in tile.properties)
        for tile in occupied
    )


def test_house_and_workshop_are_separate_building_groups() -> None:
    map_definition = load_map()
    rooms_by_id = {room.structure_id: room for room in map_definition.structures}

    assert rooms_by_id["bedroom"].building_id == "house"
    assert rooms_by_id["common_room"].building_id == "house"
    assert rooms_by_id["workshop"].building_id == "workshop"
    assert rooms_by_id["workshop"].x + rooms_by_id["workshop"].width < rooms_by_id["bedroom"].x


def test_level_is_128_tiles_square_and_river_hugs_eastern_edge() -> None:
    map_definition = load_map(persistence_path=None)
    river = next(feature for feature in map_definition.terrain if feature.kind == "river")

    assert map_definition.tile_map.columns == 128
    assert map_definition.tile_map.rows == 128
    assert map_definition.width == 128 * map_definition.tile_map.tile_size
    assert map_definition.height == 128 * map_definition.tile_map.tile_size
    assert (
        max(x for x, _ in river.points) + river.width / 2
        >= map_definition.width - 2 * map_definition.tile_map.tile_size
    )


def test_common_room_wall_geometry_leaves_exterior_door_open() -> None:
    common_room = next(
        structure for structure in load_map().structures if structure.structure_id == "common_room"
    )
    walls = structure_wall_rects(common_room)
    door = next(door for door in common_room.doors if door.connects_to is None)
    door_center_x = common_room.x + door.offset + door.width // 2
    door_center_y = common_room.y + common_room.height - 4

    assert not any(
        x <= door_center_x <= x + width and y <= door_center_y <= y + height
        for x, y, width, height in walls
    )


def test_internal_doors_are_reciprocal_connections() -> None:
    map_definition = load_map()
    structures = {structure.structure_id: structure for structure in map_definition.structures}

    bedroom_door = structures["bedroom"].doors[0]
    assert bedroom_door.connects_to == "common_room"
    assert any(door.connects_to == "bedroom" for door in structures["common_room"].doors)


def test_map_loader_reports_invalid_json(tmp_path) -> None:
    map_path = tmp_path / "broken.json"
    map_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(MapLoadError, match="Could not load map"):
        load_map(map_path)


def test_map_loader_rejects_invalid_room_color(tmp_path) -> None:
    data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    bedroom = next(structure for structure in data["structures"] if structure["id"] == "bedroom")
    bedroom["display_color"] = [300, 80]
    map_path = tmp_path / "invalid-color.json"
    map_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MapLoadError, match="display_color"):
        load_map(map_path)


def test_map_file_is_valid_formatted_json() -> None:
    data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))

    assert data["objects"]
    assert data["structures"]


def test_current_level_has_persistent_and_current_state_for_each_instance() -> None:
    data = loads_jsonc(DEFAULT_CURRENT_LEVEL_PATH.read_text(encoding="utf-8"))

    assert data["level_map"] == "homestead.jsonc"
    assert data["objects"]
    assert all("persistent_state" in entry and "current_state" in entry for entry in data["objects"])
    assert all(
        isinstance(entry["persistent_state"].get("persistent"), bool)
        for entry in data["objects"]
    )


def test_f5_sync_upserts_authored_instances_without_removing_runtime_instances(tmp_path) -> None:
    map_data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    bed = next(instance for instance in map_data["objects"] if instance["id"] == 1)
    bed["x"] = 66
    map_path = tmp_path / "homestead.jsonc"
    map_path.write_text(json.dumps(map_data), encoding="utf-8")
    current_data = loads_jsonc(DEFAULT_CURRENT_LEVEL_PATH.read_text(encoding="utf-8"))
    current_data["tiles"] = [{"x": 20, "y": 20, "till_count": 3, "tilled_today": False}]
    current_data["objects"].append(
        {
            "id": 999,
            "type": "pebble",
            "persistent_state": {"persistent": False, "x": 500, "y": 500, "orientation": "E/W", "quality": 50, "active": True, "state": ""},
            "current_state": {"x": 500, "y": 500, "orientation": "E/W", "quality": 50, "active": True, "state": ""},
        }
    )
    current_path = tmp_path / "current_level.jsonc"
    current_path.write_text(json.dumps(current_data), encoding="utf-8")

    sync_current_level_from_map(map_path, current_path)
    synced = loads_jsonc(current_path.read_text(encoding="utf-8"))
    synced_by_id = {entry["id"]: entry for entry in synced["objects"]}

    assert synced_by_id[1]["persistent_state"]["x"] == 66
    assert synced_by_id[1]["current_state"]["x"] == 66
    assert 999 in synced_by_id
    assert synced["tiles"] == [{"x": 20, "y": 20, "till_count": 3, "tilled_today": False}]


def test_startup_initialization_rebuilds_current_level_from_homestead(tmp_path) -> None:
    current_path = tmp_path / "current_level.jsonc"
    current_path.write_text(
        json.dumps(
            {
                "level_map": "homestead.jsonc",
                "objects": [{"id": 999, "type": "pebble"}],
                "tiles": [{"x": 10, "y": 10, "crop": "wheat"}],
            }
        ),
        encoding="utf-8",
    )

    initialize_current_level_from_map(current_level_path=current_path)
    initialized = loads_jsonc(current_path.read_text(encoding="utf-8"))
    authored = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))

    assert initialized["tiles"] == []
    assert {entry["id"] for entry in initialized["objects"]} == {
        entry["id"] for entry in authored["objects"]
    }
    assert all(entry["persistent_state"]["persistent"] for entry in initialized["objects"])


def test_map_instances_reference_master_object_catalog() -> None:
    map_data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    catalog_data = loads_jsonc(DEFAULT_OBJECT_TYPES_PATH.read_text(encoding="utf-8"))
    type_ids = {entry["id"] for entry in catalog_data["object_types"]}

    assert all(
        set(instance)
        <= {
            "id",
            "type",
            "variant",
            "x",
            "y",
            "orientation",
            "quality",
            "width",
            "height",
            "active",
            "state",
        }
        for instance in map_data["objects"]
    )
    assert all(instance["type"] in type_ids for instance in map_data["objects"])
    assert all(isinstance(instance["id"], int) and instance["id"] > 0 for instance in map_data["objects"])
    assert all("persistent" not in object_type for object_type in catalog_data["object_types"])
    assert len({instance["id"] for instance in map_data["objects"]}) == len(map_data["objects"])
    assert next(obj for obj in load_map().objects.values() if obj.type_id == "bed").description
    assert all(
        not form.descriptions
        or set(form.descriptions) == {"ruined", "damaged", "worn", "good", "fine"}
        for object_type in load_map().object_types.values()
        for form in object_type.forms.values()
    )


def test_current_level_is_the_only_authority_for_instance_persistence() -> None:
    without_level_state = load_map(persistence_path=None)
    with_level_state = load_map()

    plain_boulder = next(obj for obj in without_level_state.objects.values() if obj.type_id == "boulder")
    level_boulder = next(obj for obj in with_level_state.objects.values() if obj.type_id == "boulder")
    assert plain_boulder.persistent is False
    assert level_boulder.persistent is True


def test_object_description_tracks_numeric_quality() -> None:
    bed = next(obj for obj in load_map(persistence_path=None).objects.values() if obj.type_id == "bed")
    ruined_description = bed.description

    bed.quality = 100

    assert bed.quality_stage == "fine"
    assert bed.description != ruined_description
    assert bed.description == bed.descriptions["fine"]


def test_orientation_rotates_rectangular_footprint_and_quality_is_derived(tmp_path) -> None:
    data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    bed = next(instance for instance in data["objects"] if instance["type"] == "bed")
    bed["orientation"] = "N/S"
    bed["quality"] = 20
    map_path = tmp_path / "rotated.json"
    map_path.write_text(json.dumps(data), encoding="utf-8")

    rotated_bed = next(
        obj for obj in load_map(map_path, persistence_path=None).objects.values() if obj.type_id == "bed"
    )

    assert (rotated_bed.width, rotated_bed.height) == (64, 128)
    assert rotated_bed.quality_stage == "ruined"


def test_four_direction_orientation_preserves_direction_and_footprint(tmp_path) -> None:
    data = loads_jsonc(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
    bed = next(instance for instance in data["objects"] if instance["type"] == "bed")
    bed["orientation"] = "S"
    map_path = tmp_path / "south.json"
    map_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = next(
        obj for obj in load_map(map_path, persistence_path=None).objects.values()
        if obj.type_id == "bed"
    )
    assert loaded.orientation == "S"
    assert (loaded.width, loaded.height) == (64, 128)


def test_persistent_objects_round_trip_separately_from_map(tmp_path) -> None:
    persistence_path = tmp_path / "current_level.jsonc"
    original = load_map()
    original_branch = next(obj for obj in original.objects.values() if obj.type_id == "branch")
    original_pebble = next(obj for obj in original.objects.values() if obj.type_id == "pebble")
    original_bed = next(obj for obj in original.objects.values() if obj.type_id == "bed")
    original_branch.active = False
    original_pebble.quality = 1
    original_bed.state = "repaired"

    save_persistent_objects(original.objects, persistence_path)
    saved_data = loads_jsonc(persistence_path.read_text(encoding="utf-8"))
    reloaded = load_map(persistence_path=persistence_path, reset_for_morning=True)

    reloaded_branch = next(obj for obj in reloaded.objects.values() if obj.type_id == "branch")
    reloaded_pebble = next(obj for obj in reloaded.objects.values() if obj.type_id == "pebble")
    reloaded_bed = next(obj for obj in reloaded.objects.values() if obj.type_id == "bed")
    assert reloaded_branch.active is True
    assert reloaded_pebble.quality == 91
    assert reloaded_bed.state == {}
    assert reloaded_bed.description == original_bed.description
    assert next(entry for entry in saved_data["objects"] if entry["id"] == original_bed.object_id)["type"] == "bed"
    saved_branch = next(entry for entry in saved_data["objects"] if entry["id"] == original_branch.object_id)
    assert saved_branch["persistent_state"]["active"] is True
    assert any(entry["type"] == "tree" for entry in saved_data["objects"])


def test_remembered_routine_round_trips_and_survives_level_initialization(
    tmp_path,
) -> None:
    persistence_path = tmp_path / "current_level.jsonc"
    original = load_map(persistence_path=None)
    routine = [
        RoutineStep(
            None,
            "Gather Pebbles",
            area_bounds=(64, 128, 256, 320),
            quantity=4,
            target_areas=((64, 128, 128, 192), (192, 256, 256, 320)),
        ),
        RoutineStep(
            1,
            "Gather",
            "branch",
            target_point=(448.0, 512.0),
        ),
    ]

    save_persistent_objects(
        original.objects,
        persistence_path,
        tile_size=original.tile_map.tile_size,
        remembered_routine=routine,
    )
    initialize_current_level_from_map(current_level_path=persistence_path)
    reloaded = load_map(persistence_path=persistence_path)

    assert reloaded.remembered_routine == tuple(routine)
    saved = loads_jsonc(persistence_path.read_text(encoding="utf-8"))
    assert saved["remembered_routine"][0]["area_bounds"] == {
        "start": {"tilexy": [1, 2], "subtilexy": [0, 0]},
        "end": {"tilexy": [4, 5], "subtilexy": [0, 0]},
    }
    assert saved["remembered_routine"][1]["target_point"] == {
        "tilexy": [7, 8],
        "subtilexy": [0.0, 0.0],
    }


def test_sparse_tile_state_round_trips_through_current_level(tmp_path) -> None:
    current_path = tmp_path / "current_level.jsonc"
    original = load_map()
    column, row = next(
        (column, row)
        for row in range(original.tile_map.rows)
        for column in range(original.tile_map.columns)
        if original.tile_map.tile_at(column, row).kind is TileKind.GRASSLAND
        and not original.tile_map.tile_at(column, row).properties
    )
    state = LevelTileState(
        column,
        row,
        till_percentage=42.5,
        tilled_today=True,
        soil_persistence_percentage=37.5,
        kind_override=TileKind.SOIL.value,
        persistence_modifier=1.1,
    )
    original.tile_states[(column, row)] = state

    save_persistent_objects(
        original.objects,
        current_path,
        tile_size=original.tile_map.tile_size,
        tile_states=original.tile_states,
    )
    reloaded = load_map(persistence_path=current_path)

    restored = reloaded.tile_states[(column, row)]
    assert restored.till_percentage == 42.5
    assert restored.tilled_today is True
    assert restored.soil_persistence_percentage == 37.5
    assert restored.kind_override == TileKind.SOIL.value
    assert restored.persistence_modifier == 1.1
    assert reloaded.tile_map.tile_at(column, row).kind is TileKind.SOIL


def test_old_live_save_entries_do_not_replace_new_persistent_baseline(tmp_path) -> None:
    persistence_path = tmp_path / "current_level.jsonc"
    persistence_path.write_text(
        json.dumps({"objects": [{"id": "branch_1", "type": "branch", "x": 1213, "y": 1062, "active": False}]}),
        encoding="utf-8",
    )

    reloaded = load_map(persistence_path=persistence_path, reset_for_morning=True)

    assert next(obj for obj in reloaded.objects.values() if obj.type_id == "branch").active is True


def test_nonpersistent_instance_keeps_live_state_across_days(tmp_path) -> None:
    persistence_path = tmp_path / "current_level.jsonc"
    original = load_map(persistence_path=None)
    tree = next(obj for obj in original.objects.values() if obj.type_id == "tree")
    tree.active = False
    tree.quality = 12

    save_persistent_objects(original.objects, persistence_path)
    reloaded = load_map(persistence_path=persistence_path, reset_for_morning=True)

    assert reloaded.objects[tree.object_id].active is False
    assert reloaded.objects[tree.object_id].quality == 12


def test_grassland_daily_spawn_chances_are_authored_in_tile_catalog() -> None:
    data = loads_jsonc(DEFAULT_TILE_TYPES_PATH.read_text(encoding="utf-8"))
    chances = data["tile_types"]["grassland"]["spawn_chances"]

    assert chances == {
        "pebble": 0.05,
        "wild_plant": 0.04,
        "branch": 0.05,
        "bush": 0.02,
        "grass": 0.10,
    }
    assert data["tile_types"]["hills"]["spawn_chances"]["pebble"] == 0.12
    assert {influence["type"] for influence in data["tile_types"]["pond"]["spawn_influence"]} == {
        "bush",
        "grass",
    }
    for water_kind in ("pond", "shallow_water", "deep_water"):
        assert {influence["type"] for influence in data["tile_types"][water_kind]["spawn_influence"]} >= {
            "grass"
        }


def test_tall_grass_fits_inside_one_tile() -> None:
    map_definition = load_map(persistence_path=None)
    grass = map_definition.object_types["grass"]

    form = grass.form_definition()
    assert form.footprint == (1, 1)


def test_spawn_influence_distance_and_decay_scale_the_boost_per_tile() -> None:
    influences: dict[tuple[int, int], dict[str, float]] = {}

    _spread_spawn_influence(influences, 10, 10, "berry_bush", 0.20, 3, 0.5)

    assert influences[(11, 10)]["berry_bush"] == pytest.approx(0.20)
    assert influences[(12, 10)]["berry_bush"] == pytest.approx(0.10)
    assert influences[(13, 10)]["berry_bush"] == pytest.approx(0.05)


def test_tilling_progress_can_become_permanent_or_decay_when_neglected() -> None:
    used = LevelTileState(
        10,
        10,
        till_percentage=100.0,
        soil_persistence_percentage=100.0,
        kind_override=TileKind.SOIL.value,
    )
    unused = LevelTileState(
        11, 10, till_percentage=100.0, kind_override=TileKind.SOIL.value
    )
    progress = LevelTileState(12, 10, till_percentage=25.0, tilled_today=True)
    states = {(10, 10): used, (11, 10): unused, (12, 10): progress}

    advance_level_tile_states(
        states,
        day_number=1,
        reverted_till_progress_range=(80.0, 100.0),
    )
    assert used.kind_override == TileKind.SOIL.value
    assert used.soil_persistence_percentage == 100.0
    assert unused.kind_override is None
    assert 80.0 <= unused.till_percentage <= 100.0
    assert progress.till_percentage == 25.0
    assert progress.tilled_today is False


def test_tilling_probabilities_are_authored_as_percent_conversion() -> None:
    data = loads_jsonc(DEFAULT_TILE_TYPES_PATH.read_text(encoding="utf-8"))

    tilling = data["tile_types"]["grassland"]["tilling"]
    assert tilling["progress_per_till"] == 5.0
    assert tilling["persistence_gain_per_conversion"] == 1.0
    assert tilling["reverted_till_progress_range"] == [80.0, 100.0]
    assert set(tilling["tracked_fields"]) == {
        "till_percentage",
        "tilled_today",
        "soil_persistence_percentage",
        "kind_override",
        "persistence_modifier",
    }
    assert tilling["persistence_modifier_range"] == [0.75, 1.25]


def test_tilled_tiles_receive_distinct_stable_persistence_affinities() -> None:
    states = {
        (column, 10): LevelTileState(
            column, 10, till_percentage=1.0, tilled_today=True
        )
        for column in range(10, 14)
    }

    advance_level_tile_states(
        states,
        day_number=1,
        reverted_till_progress_range=(80.0, 100.0),
        persistence_modifier_range=(0.75, 1.25),
    )
    first_values = {
        key: state.persistence_modifier for key, state in states.items()
    }

    assert all(0.75 <= value <= 1.25 for value in first_values.values())
    assert len(set(first_values.values())) > 1

    advance_level_tile_states(
        states,
        day_number=2,
        reverted_till_progress_range=(80.0, 100.0),
        persistence_modifier_range=(0.75, 1.25),
    )

    assert {
        key: state.persistence_modifier for key, state in states.items()
    } == first_values


def test_daily_population_changes_by_day_and_branches_spawn_near_trees() -> None:
    first_day = load_map(persistence_path=None, day_number=1)
    second_day = load_map(persistence_path=None, day_number=2)
    first_spawns = [obj for obj in first_day.objects.values() if obj.daily_spawned]
    second_spawns = [obj for obj in second_day.objects.values() if obj.daily_spawned]
    first_layout = {(obj.type_id, obj.x, obj.y) for obj in first_spawns}
    second_layout = {(obj.type_id, obj.x, obj.y) for obj in second_spawns}

    assert first_layout != second_layout
    assert {"pebble", "wild_plant", "branch", "bush"} <= {
        obj.type_id for obj in first_spawns
    }
    wheat = next(obj for obj in first_spawns if obj.type_id == "wild_plant")
    assert wheat.variant == "wheat"
    assert wheat.name == "Wild Wheat"
    trees = [obj for obj in first_day.objects.values() if obj.type_id == "tree"]
    branches = [obj for obj in first_spawns if obj.type_id == "branch"]
    assert first_day.object_types[
        "tree"
    ].form_definition("standing").spawn_influence == (("branch", 0.35, 5, 0.75),)
    assert len(branches) >= 100
    assert any(
        min(math.dist(branch.center, tree.center) for tree in trees)
        < 100 / 32 * first_day.tile_map.tile_size
        for branch in branches
    )
    ground_spawns = [
        obj
        for obj in first_spawns
        if obj.type_id in {"pebble", "wild_plant"}
    ]
    assert all(
        first_day.tile_map.tile_at_world(*obj.center)[2].kind in {TileKind.GRASSLAND, TileKind.HILLS}
        for obj in ground_spawns
    )


def test_ponds_are_traversable_and_boost_nearby_berry_bushes() -> None:
    map_definition = load_map(day_number=1)
    ponds = [feature for feature in map_definition.terrain if feature.kind == "pond"]
    berry_bushes = [
        obj
        for obj in map_definition.objects.values()
        if obj.type_id == "bush" and obj.variant == "berry"
    ]

    assert len(ponds) == 2
    assert map_definition.tile_map.tile_at(39, 40).kind is TileKind.POND
    assert TileKind.POND in TRAVERSABLE_TILE_KINDS
    assert berry_bushes
    assert all(
        map_definition.tile_map.tile_at_world(*bush.center)[2].kind is TileKind.GRASSLAND
        for bush in berry_bushes
    )
    pond_tiles = {
        (column, row)
        for row in range(map_definition.tile_map.rows)
        for column in range(map_definition.tile_map.columns)
        if map_definition.tile_map.tile_at(column, row).kind is TileKind.POND
    }
    berry_tiles = {
        map_definition.tile_map.tile_at_world(*bush.center)[:2] for bush in berry_bushes
    }
    assert any(
        max(abs(berry_column - pond_column), abs(berry_row - pond_row)) <= 2
        for berry_column, berry_row in berry_tiles
        for pond_column, pond_row in pond_tiles
    )


def test_boulders_are_persistent_and_increase_nearby_pebble_chance() -> None:
    map_definition = load_map(day_number=1)
    boulders = [obj for obj in map_definition.objects.values() if obj.type_id == "boulder"]
    pebbles = [obj for obj in map_definition.objects.values() if obj.type_id == "pebble"]

    assert len(boulders) == 4
    assert all(boulder.persistent for boulder in boulders)
    assert map_definition.object_types[
        "boulder"
    ].form_definition().spawn_influence == (("pebble", 0.18, 2, 1.0),)
    assert any(
        min(math.dist(pebble.center, boulder.center) for boulder in boulders)
        < 100 / 32 * map_definition.tile_map.tile_size
        for pebble in pebbles
    )
