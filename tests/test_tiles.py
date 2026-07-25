from remembering.tiles import TRAVERSABLE_TILE_KINDS, Tile, TileEdge, TileKind, TileMap


def test_initial_tile_kind_lists_match_design() -> None:
    assert TRAVERSABLE_TILE_KINDS == {
        TileKind.WOODEN_FLOOR,
        TileKind.DIRT,
        TileKind.SOIL,
        TileKind.GRASSLAND,
        TileKind.SHALLOW_WATER,
        TileKind.DESERT,
        TileKind.HILLS,
        TileKind.POND,
    }
    assert {TileKind.MOUNTAIN, TileKind.DEEP_WATER, TileKind.CHASM}.isdisjoint(
        TRAVERSABLE_TILE_KINDS
    )


def test_tile_properties_and_passable_edges_are_mutable() -> None:
    tile = Tile(TileKind.WOODEN_FLOOR, properties=["room:bedroom"])

    tile.properties.append("remembered")
    tile.passable[TileEdge.EAST] = False

    assert tile.properties == ["room:bedroom", "remembered"]
    assert tile.passable[TileEdge.EAST] is False


def test_hills_are_traversable() -> None:
    tile_map = TileMap(1, 1, 32, [Tile(TileKind.HILLS)])

    assert tile_map.can_stand_at(16, 16, 14)


def test_setting_tile_edge_can_update_neighbor_for_a_door() -> None:
    tile_map = TileMap(2, 1, 32, [Tile(TileKind.WOODEN_FLOOR), Tile(TileKind.WOODEN_FLOOR)])
    tile_map.set_edge_passable(0, 0, TileEdge.EAST, False)

    assert tile_map.tile_at(0, 0).passable[TileEdge.EAST] is False
    assert tile_map.tile_at(1, 0).passable[TileEdge.WEST] is False

    tile_map.set_edge_passable(0, 0, TileEdge.EAST, True)
    assert tile_map.tile_at(0, 0).passable[TileEdge.EAST] is True
    assert tile_map.tile_at(1, 0).passable[TileEdge.WEST] is True


def test_tile_map_snaps_world_positions_to_tile_centers() -> None:
    tile_map = TileMap(2, 2, 32, [Tile(TileKind.GRASSLAND) for _ in range(4)])

    assert tile_map.center_at_world(2, 3) == (16, 16)
    assert tile_map.center_at_world(40, 50) == (48, 48)
    assert tile_map.is_tile_center(48, 48)
    assert not tile_map.is_tile_center(40, 50)
