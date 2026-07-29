from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeAlias


SUBTILE_UNITS = 64
TileXY: TypeAlias = tuple[int, int]
SubtileXY: TypeAlias = tuple[float, float]
ScreenXY: TypeAlias = tuple[int, int]
MapXY: TypeAlias = tuple[float, float]


@dataclass(frozen=True, slots=True)
class TilePosition:
    """Resolution-independent gameplay location.

    ``tilexy`` identifies the map tile. ``subtilexy`` is a continuous logical
    coordinate within that tile. Canonical subtile components are in [0, 64).
    """

    tilexy: TileXY
    subtilexy: SubtileXY = (32.0, 32.0)

    def normalized(self) -> TilePosition:
        tile_x, tile_y = self.tilexy
        sub_x, sub_y = self.subtilexy
        carry_x = math.floor(sub_x / SUBTILE_UNITS)
        carry_y = math.floor(sub_y / SUBTILE_UNITS)
        return TilePosition(
            (tile_x + carry_x, tile_y + carry_y),
            (
                sub_x - carry_x * SUBTILE_UNITS,
                sub_y - carry_y * SUBTILE_UNITS,
            ),
        )

    @classmethod
    def from_mapxy(cls, mapxy: MapXY) -> TilePosition:
        map_x, map_y = mapxy
        tile_x = math.floor(map_x / SUBTILE_UNITS)
        tile_y = math.floor(map_y / SUBTILE_UNITS)
        return cls(
            (tile_x, tile_y),
            (
                map_x - tile_x * SUBTILE_UNITS,
                map_y - tile_y * SUBTILE_UNITS,
            ),
        )

    @property
    def mapxy(self) -> MapXY:
        normalized = self.normalized()
        return (
            normalized.tilexy[0] * SUBTILE_UNITS + normalized.subtilexy[0],
            normalized.tilexy[1] * SUBTILE_UNITS + normalized.subtilexy[1],
        )

    def as_data(self) -> dict[str, list[float | int]]:
        normalized = self.normalized()
        return {
            "tilexy": [normalized.tilexy[0], normalized.tilexy[1]],
            "subtilexy": [
                normalized.subtilexy[0],
                normalized.subtilexy[1],
            ],
        }


def tile_center(tilexy: TileXY) -> TilePosition:
    return TilePosition(tilexy, (32.0, 32.0))


def position_from_data(value: object) -> TilePosition:
    if not isinstance(value, dict):
        raise ValueError("position requires tilexy and subtilexy")
    tilexy = value.get("tilexy")
    subtilexy = value.get("subtilexy", [32.0, 32.0])
    if (
        not isinstance(tilexy, list)
        or len(tilexy) != 2
        or not all(isinstance(component, int) for component in tilexy)
    ):
        raise ValueError("position tilexy requires two integers")
    if (
        not isinstance(subtilexy, list)
        or len(subtilexy) != 2
        or not all(isinstance(component, (int, float)) for component in subtilexy)
    ):
        raise ValueError("position subtilexy requires two numbers")
    return TilePosition(
        (tilexy[0], tilexy[1]),
        (float(subtilexy[0]), float(subtilexy[1])),
    ).normalized()


def mapxy_as_position_data(mapxy: MapXY) -> dict[str, list[float | int]]:
    return TilePosition.from_mapxy(mapxy).as_data()


def bounds_as_position_data(
    bounds: tuple[int, int, int, int],
) -> dict[str, dict[str, list[float | int]]]:
    return {
        "start": mapxy_as_position_data((bounds[0], bounds[1])),
        "end": mapxy_as_position_data((bounds[2], bounds[3])),
    }


def bounds_from_position_data(
    value: object,
) -> tuple[int, int, int, int]:
    if not isinstance(value, dict):
        raise ValueError("bounds require start and end positions")
    start = position_from_data(value.get("start")).mapxy
    end = position_from_data(value.get("end")).mapxy
    return round(start[0]), round(start[1]), round(end[0]), round(end[1])
