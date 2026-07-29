from remembering.coordinates import (
    SUBTILE_UNITS,
    TilePosition,
    bounds_as_position_data,
    bounds_from_position_data,
    position_from_data,
    tile_center,
)


def test_tile_center_is_exact_with_sixty_four_subtile_units() -> None:
    center = tile_center((10, 20))

    assert SUBTILE_UNITS == 64
    assert center.subtilexy == (32.0, 32.0)
    assert center.mapxy == (672.0, 1312.0)


def test_subtile_position_normalizes_across_tile_boundaries() -> None:
    position = TilePosition((10, 20), (70.5, -2.0)).normalized()

    assert position.tilexy == (11, 19)
    assert position.subtilexy == (6.5, 62.0)


def test_position_data_round_trips_without_screen_or_asset_pixels() -> None:
    source = TilePosition((4, 7), (12.5, 32.0))

    assert position_from_data(source.as_data()) == source


def test_bounds_data_uses_tilexy_and_subtilexy() -> None:
    bounds = (64, 96, 191, 256)

    encoded = bounds_as_position_data(bounds)

    assert encoded["start"] == {
        "tilexy": [1, 1],
        "subtilexy": [0, 32.0],
    }
    assert bounds_from_position_data(encoded) == bounds
