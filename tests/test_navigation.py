import math

from remembering.navigation import center_path_on_portals, find_path, find_tile_path
from remembering.tiles import Tile, TileEdge, TileKind, TileMap


def test_path_routes_through_wall_opening() -> None:
    def can_stand(x: float, y: float) -> bool:
        in_bounds = 0 <= x <= 100 and 0 <= y <= 100
        in_wall = 45 <= x <= 55 and not 42 <= y <= 58
        return in_bounds and not in_wall

    path = find_path((10, 20), (90, 20), can_stand, grid_size=4)

    assert path is not None
    assert all(can_stand(x, y) for x, y in path)
    assert any(42 <= y <= 58 for x, y in path if 40 <= x <= 60)


def test_path_returns_none_when_wall_has_no_opening() -> None:
    def can_stand(x: float, y: float) -> bool:
        return 0 <= x <= 100 and 0 <= y <= 100 and not 45 <= x <= 55

    assert find_path((10, 20), (90, 20), can_stand, grid_size=4, max_cells=5_000) is None


def test_doorway_crossing_gets_centered_clearance_nodes() -> None:
    path = [(10.0, 45.0), (90.0, 55.0)]
    portals = [("vertical", 50, 40, 60)]

    centered = center_path_on_portals(path, portals, clearance=18)

    assert (32.0, 50.0) in centered
    assert (68.0, 50.0) in centered


def test_homestead_path_connects_bedroom_to_workshop() -> None:
    from remembering.game import Game

    game = Game(fullscreen=False)
    game.player.x, game.player.y = game.player_spawn()
    workbench = game.object_of_type("workbench")
    path = game.build_navigation_path_to_object(workbench)

    assert path
    assert len(path) >= 3
    assert all(game.map.tile_map.is_tile_center(x, y) for x, y in path)
    assert not workbench.contains(path[-1])


def test_large_object_routes_to_reachable_interaction_tile() -> None:
    from remembering.game import Game, INTERACTION_DISTANCE, distance_to_object

    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")
    path = game.build_navigation_path_to_object(bed)

    assert path
    assert distance_to_object(path[-1], bed) <= INTERACTION_DISTANCE
    assert game.can_stand_at(*path[-1])


def test_bed_interaction_cannot_reach_through_house_wall() -> None:
    from remembering.game import Game

    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")
    tile_map = game.map.tile_map
    size = tile_map.tile_size
    first_column = bed.x // size
    last_column = (bed.x + bed.width - 1) // size
    first_row = bed.y // size
    last_row = (bed.y + bed.height - 1) // size
    adjacent = (
        [(column, first_row - 1) for column in range(first_column, last_column + 1)]
        + [(column, last_row + 1) for column in range(first_column, last_column + 1)]
        + [(first_column - 1, row) for row in range(first_row, last_row + 1)]
        + [(last_column + 1, row) for row in range(first_row, last_row + 1)]
    )
    blocked = next(
        (column, row)
        for column, row in adjacent
        if game.can_stand_at(*tile_map.tile_center(column, row))
        and not game.tile_can_access_object(column, row, bed)
    )
    outside = tile_map.tile_center(*blocked)
    game.player.x, game.player.y = outside

    path = game.build_navigation_path_to_object(bed)

    assert path
    assert path[-1] != outside
    end_column, end_row, _ = tile_map.tile_at_world(*path[-1])
    assert game.tile_can_access_object(end_column, end_row, bed)


def test_tile_path_respects_mutable_door_edge() -> None:
    tile_map = TileMap(2, 1, 32, [Tile(TileKind.WOODEN_FLOOR), Tile(TileKind.WOODEN_FLOOR)])
    tile_map.set_edge_passable(0, 0, TileEdge.EAST, False)
    assert find_tile_path((16, 16), (48, 16), tile_map) is None

    tile_map.set_edge_passable(0, 0, TileEdge.EAST, True)
    assert find_tile_path((16, 16), (48, 16), tile_map) == [(16, 16), (48, 16)]


def test_tile_path_uses_diagonals_with_square_root_two_distance() -> None:
    tile_map = TileMap(3, 3, 32, [Tile(TileKind.GRASSLAND) for _ in range(9)])

    path = find_tile_path((16, 16), (80, 80), tile_map)

    assert path == [(16.0, 16.0), (48.0, 48.0), (80.0, 80.0)]
    distance = sum(math.dist(start, end) for start, end in zip(path, path[1:]))
    assert distance == math.sqrt(2) * 64


def test_diagonal_path_cannot_cut_across_blocked_corner() -> None:
    tile_map = TileMap(2, 2, 32, [Tile(TileKind.GRASSLAND) for _ in range(4)])
    tile_map.set_edge_passable(0, 0, TileEdge.EAST, False)

    path = find_tile_path((16, 16), (48, 48), tile_map)

    assert path == [(16.0, 16.0), (16.0, 48.0), (48.0, 48.0)]
