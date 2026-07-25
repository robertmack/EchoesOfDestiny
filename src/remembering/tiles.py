from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TileKind(Enum):
    WOODEN_FLOOR = "wooden_floor"
    DIRT = "dirt"
    SOIL = "soil"
    GRASSLAND = "grassland"
    SHALLOW_WATER = "shallow_water"
    DESERT = "desert"
    HILLS = "hills"
    POND = "pond"
    MOUNTAIN = "mountain"
    DEEP_WATER = "deep_water"
    CHASM = "chasm"


TRAVERSABLE_TILE_KINDS = frozenset(
    {
        TileKind.WOODEN_FLOOR,
        TileKind.DIRT,
        TileKind.SOIL,
        TileKind.GRASSLAND,
        TileKind.SHALLOW_WATER,
        TileKind.DESERT,
        TileKind.HILLS,
        TileKind.POND,
    }
)


class TileEdge(Enum):
    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"


OPPOSITE_EDGE = {
    TileEdge.NORTH: TileEdge.SOUTH,
    TileEdge.EAST: TileEdge.WEST,
    TileEdge.SOUTH: TileEdge.NORTH,
    TileEdge.WEST: TileEdge.EAST,
}


EDGE_OFFSET = {
    TileEdge.NORTH: (0, -1),
    TileEdge.EAST: (1, 0),
    TileEdge.SOUTH: (0, 1),
    TileEdge.WEST: (-1, 0),
}


@dataclass(slots=True)
class Tile:
    kind: TileKind
    properties: list[str] = field(default_factory=list)
    passable: dict[TileEdge, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.passable:
            traversable = self.kind in TRAVERSABLE_TILE_KINDS
            self.passable = {edge: traversable for edge in TileEdge}


@dataclass(slots=True)
class TileMap:
    columns: int
    rows: int
    tile_size: int
    tiles: list[Tile]

    @property
    def width(self) -> int:
        return self.columns * self.tile_size

    @property
    def height(self) -> int:
        return self.rows * self.tile_size

    def tile_at(self, column: int, row: int) -> Tile | None:
        if not (0 <= column < self.columns and 0 <= row < self.rows):
            return None
        return self.tiles[row * self.columns + column]

    def tile_at_world(self, x: float, y: float) -> tuple[int, int, Tile] | None:
        column, row = int(x // self.tile_size), int(y // self.tile_size)
        tile = self.tile_at(column, row)
        return (column, row, tile) if tile is not None else None

    def tile_center(self, column: int, row: int) -> tuple[float, float]:
        return (
            column * self.tile_size + self.tile_size / 2,
            row * self.tile_size + self.tile_size / 2,
        )

    def center_at_world(self, x: float, y: float) -> tuple[float, float] | None:
        located = self.tile_at_world(x, y)
        return self.tile_center(located[0], located[1]) if located is not None else None

    def is_tile_center(self, x: float, y: float) -> bool:
        center = self.center_at_world(x, y)
        return center is not None and center == (x, y)

    def set_edge_passable(
        self,
        column: int,
        row: int,
        edge: TileEdge,
        passable: bool,
        *,
        mirror: bool = True,
    ) -> None:
        tile = self.tile_at(column, row)
        if tile is None:
            return
        tile.passable[edge] = passable
        if mirror:
            dx, dy = EDGE_OFFSET[edge]
            neighbor = self.tile_at(column + dx, row + dy)
            if neighbor is not None:
                neighbor.passable[OPPOSITE_EDGE[edge]] = passable

    def can_stand_at(self, x: float, y: float, radius: float) -> bool:
        located = self.tile_at_world(x, y)
        if located is None:
            return False
        column, row, tile = located
        if tile.kind not in TRAVERSABLE_TILE_KINDS or "blocked" in tile.properties:
            return False
        local_x = x - column * self.tile_size
        local_y = y - row * self.tile_size
        return not (
            (local_y < radius and not tile.passable[TileEdge.NORTH])
            or (self.tile_size - local_x < radius and not tile.passable[TileEdge.EAST])
            or (self.tile_size - local_y < radius and not tile.passable[TileEdge.SOUTH])
            or (local_x < radius and not tile.passable[TileEdge.WEST])
        )
