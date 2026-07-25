from __future__ import annotations

import heapq
import math
from collections import deque
from collections.abc import Callable

from remembering.model import MapDefinition, MapDoor, MapStructure
from remembering.tiles import EDGE_OFFSET, OPPOSITE_EDGE, TRAVERSABLE_TILE_KINDS, TileEdge, TileMap


Point = tuple[float, float]
Cell = tuple[int, int]
Portal = tuple[str, int, int, int]
OUTSIDE = "__outside__"


def find_tile_path(start: Point, goal: Point, tile_map: TileMap) -> list[Point] | None:
    """Find an eight-direction route whose resting nodes are tile centers."""
    return find_tile_path_to_any(start, [goal], tile_map)


def find_tile_path_to_any(start: Point, goals: list[Point], tile_map: TileMap) -> list[Point] | None:
    """Find the cheapest eight-direction route to any supplied goal."""
    start_location = tile_map.tile_at_world(*start)
    goal_locations = [tile_map.tile_at_world(*goal) for goal in goals]
    goal_locations = [location for location in goal_locations if location is not None and _tile_is_standable(location[2])]
    if start_location is None or not goal_locations:
        return None
    start_cell = start_location[0], start_location[1]
    goal_cells = {(location[0], location[1]) for location in goal_locations}

    frontier: list[tuple[float, Cell]] = [(0.0, start_cell)]
    came_from: dict[Cell, Cell | None] = {start_cell: None}
    cost: dict[Cell, float] = {start_cell: 0.0}
    while frontier:
        _, current = heapq.heappop(frontier)
        if current in goal_cells:
            cells = _reconstruct_path(came_from, current)
            return [tile_map.tile_center(column, row) for column, row in cells]
        for edge in TileEdge:
            dx, dy = EDGE_OFFSET[edge]
            neighbor = current[0] + dx, current[1] + dy
            if not _can_cross_cardinal(tile_map, current, edge):
                continue
            new_cost = cost[current] + 1
            if neighbor in cost and new_cost >= cost[neighbor]:
                continue
            cost[neighbor] = new_cost
            priority = new_cost + min(_octile_distance(neighbor, goal) for goal in goal_cells)
            heapq.heappush(frontier, (priority, neighbor))
            came_from[neighbor] = current
        diagonal_steps = (
            (-1, -1, TileEdge.WEST, TileEdge.NORTH),
            (1, -1, TileEdge.EAST, TileEdge.NORTH),
            (1, 1, TileEdge.EAST, TileEdge.SOUTH),
            (-1, 1, TileEdge.WEST, TileEdge.SOUTH),
        )
        for dx, dy, horizontal_edge, vertical_edge in diagonal_steps:
            horizontal = current[0] + dx, current[1]
            vertical = current[0], current[1] + dy
            neighbor = current[0] + dx, current[1] + dy
            if not (
                _can_cross_cardinal(tile_map, current, horizontal_edge)
                and _can_cross_cardinal(tile_map, current, vertical_edge)
                and _can_cross_cardinal(tile_map, horizontal, vertical_edge)
                and _can_cross_cardinal(tile_map, vertical, horizontal_edge)
            ):
                continue
            new_cost = cost[current] + math.sqrt(2)
            if neighbor in cost and new_cost >= cost[neighbor]:
                continue
            cost[neighbor] = new_cost
            heuristic = min(_octile_distance(neighbor, goal) for goal in goal_cells)
            heapq.heappush(frontier, (new_cost + heuristic, neighbor))
            came_from[neighbor] = current
    return None


def _can_cross_cardinal(tile_map: TileMap, cell: Cell, edge: TileEdge) -> bool:
    tile = tile_map.tile_at(*cell)
    if tile is None or not _tile_is_standable(tile) or not tile.passable[edge]:
        return False
    dx, dy = EDGE_OFFSET[edge]
    neighbor = tile_map.tile_at(cell[0] + dx, cell[1] + dy)
    return bool(
        neighbor is not None
        and _tile_is_standable(neighbor)
        and neighbor.passable[OPPOSITE_EDGE[edge]]
    )


def _octile_distance(start: Cell, goal: Cell) -> float:
    dx, dy = abs(start[0] - goal[0]), abs(start[1] - goal[1])
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def _tile_is_standable(tile) -> bool:
    return tile.kind in TRAVERSABLE_TILE_KINDS and "blocked" not in tile.properties


def find_path(
    start: Point,
    goal: Point,
    can_stand_at: Callable[[float, float], bool],
    *,
    grid_size: int = 8,
    max_cells: int = 20_000,
    search_margin: int = 256,
) -> list[Point] | None:
    """Find a collision-safe grid path and return world-space waypoints."""
    start_cell = _nearest_open_cell(start, can_stand_at, grid_size)
    goal_cell = _nearest_open_cell(goal, can_stand_at, grid_size)
    if start_cell is None or goal_cell is None:
        return None
    resolved_goal = goal if can_stand_at(*goal) else _cell_point(goal_cell, grid_size)
    if start_cell == goal_cell:
        return [start, resolved_goal]

    margin_cells = math.ceil(search_margin / grid_size)
    search_bounds = (
        min(start_cell[0], goal_cell[0]) - margin_cells,
        max(start_cell[0], goal_cell[0]) + margin_cells,
        min(start_cell[1], goal_cell[1]) - margin_cells,
        max(start_cell[1], goal_cell[1]) + margin_cells,
    )

    frontier: list[tuple[float, Cell]] = [(0.0, start_cell)]
    came_from: dict[Cell, Cell | None] = {start_cell: None}
    cost_so_far: dict[Cell, float] = {start_cell: 0.0}

    while frontier and len(came_from) <= max_cells:
        _, current = heapq.heappop(frontier)
        if current == goal_cell:
            cells = _reconstruct_path(came_from, current)
            points = [start, *(_cell_point(cell, grid_size) for cell in cells[1:-1]), resolved_goal]
            return _compress_collinear(points)

        for neighbor, move_cost in _neighbors(current, can_stand_at, grid_size, search_bounds):
            new_cost = cost_so_far[current] + move_cost
            if neighbor in cost_so_far and new_cost >= cost_so_far[neighbor]:
                continue
            cost_so_far[neighbor] = new_cost
            priority = new_cost + math.dist(neighbor, goal_cell)
            heapq.heappush(frontier, (priority, neighbor))
            came_from[neighbor] = current
    return None


def center_path_on_portals(path: list[Point], portals: list[Portal], clearance: float) -> list[Point]:
    """Insert centered approach/departure points wherever a route crosses a doorway."""
    if len(path) < 2:
        return path
    centered = [path[0]]
    for start, end in zip(path, path[1:]):
        crossings: list[tuple[float, Point, Point]] = []
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        for orientation, fixed, span_start, span_end in portals:
            if orientation == "vertical" and dx:
                ratio = (fixed - start[0]) / dx
                crossing = start[1] + dy * ratio
                if 0 < ratio < 1 and span_start <= crossing <= span_end:
                    direction = 1 if dx > 0 else -1
                    center = (float(fixed), float((span_start + span_end) / 2))
                    crossings.append(
                        (
                            ratio,
                            (center[0] - direction * clearance, center[1]),
                            (center[0] + direction * clearance, center[1]),
                        )
                    )
            elif orientation == "horizontal" and dy:
                ratio = (fixed - start[1]) / dy
                crossing = start[0] + dx * ratio
                if 0 < ratio < 1 and span_start <= crossing <= span_end:
                    direction = 1 if dy > 0 else -1
                    center = (float((span_start + span_end) / 2), float(fixed))
                    crossings.append(
                        (
                            ratio,
                            (center[0], center[1] - direction * clearance),
                            (center[0], center[1] + direction * clearance),
                        )
                    )
        for _, approach, departure in sorted(crossings):
            if centered[-1] != approach:
                centered.append(approach)
            centered.append(departure)
        centered.append(end)
    return centered


def door_route_waypoints(
    map_definition: MapDefinition,
    start: Point,
    goal: Point,
    clearance: float,
) -> list[Point] | None:
    """Find a high-level room/outside route and return centered portal waypoints."""
    start_node = _room_at(map_definition, start)
    goal_node = _room_at(map_definition, goal)
    start_id = start_node.structure_id if start_node else OUTSIDE
    goal_id = goal_node.structure_id if goal_node else OUTSIDE
    if start_id == goal_id:
        return []

    adjacency: dict[str, list[tuple[str, MapStructure, MapDoor]]] = {}
    for room in map_definition.structures:
        for door in room.doors:
            target_id = door.connects_to or OUTSIDE
            adjacency.setdefault(room.structure_id, []).append((target_id, room, door))
            if target_id == OUTSIDE:
                adjacency.setdefault(OUTSIDE, []).append((room.structure_id, room, door))

    frontier = deque([start_id])
    came_from: dict[str, tuple[str, MapStructure, MapDoor] | None] = {start_id: None}
    while frontier:
        current = frontier.popleft()
        if current == goal_id:
            break
        for neighbor, room, door in adjacency.get(current, []):
            if neighbor in came_from:
                continue
            came_from[neighbor] = current, room, door
            frontier.append(neighbor)
    if goal_id not in came_from:
        return None

    transitions: list[tuple[str, str, MapStructure, MapDoor]] = []
    current = goal_id
    while current != start_id:
        previous, room, door = came_from[current]  # type: ignore[misc]
        transitions.append((previous, current, room, door))
        current = previous
    transitions.reverse()

    waypoints: list[Point] = []
    for from_id, _, room, door in transitions:
        inside, outside = _door_clearance_points(room, door, clearance)
        if from_id == room.structure_id:
            waypoints.extend((inside, outside))
        else:
            waypoints.extend((outside, inside))
    return waypoints


def _room_at(map_definition: MapDefinition, point: Point) -> MapStructure | None:
    x, y = point
    for room in map_definition.structures:
        if room.x < x < room.x + room.width and room.y < y < room.y + room.height:
            return room
    return None


def _door_clearance_points(room: MapStructure, door: MapDoor, clearance: float) -> tuple[Point, Point]:
    if door.side == "top":
        center = (room.x + door.offset + door.width / 2, float(room.y))
        return (center[0], center[1] + clearance), (center[0], center[1] - clearance)
    if door.side == "bottom":
        center = (room.x + door.offset + door.width / 2, float(room.y + room.height))
        return (center[0], center[1] - clearance), (center[0], center[1] + clearance)
    if door.side == "left":
        center = (float(room.x), room.y + door.offset + door.width / 2)
        return (center[0] + clearance, center[1]), (center[0] - clearance, center[1])
    center = (float(room.x + room.width), room.y + door.offset + door.width / 2)
    return (center[0] - clearance, center[1]), (center[0] + clearance, center[1])


def _nearest_open_cell(
    point: Point,
    can_stand_at: Callable[[float, float], bool],
    grid_size: int,
) -> Cell | None:
    origin = (round(point[0] / grid_size), round(point[1] / grid_size))
    for radius in range(5):
        candidates = [
            (origin[0] + dx, origin[1] + dy)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if max(abs(dx), abs(dy)) == radius
        ]
        candidates.sort(key=lambda cell: math.dist(_cell_point(cell, grid_size), point))
        for cell in candidates:
            x, y = _cell_point(cell, grid_size)
            if can_stand_at(x, y):
                return cell
    return None


def _neighbors(
    cell: Cell,
    can_stand_at: Callable[[float, float], bool],
    grid_size: int,
    search_bounds: tuple[int, int, int, int],
) -> list[tuple[Cell, float]]:
    result: list[tuple[Cell, float]] = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        neighbor = cell[0] + dx, cell[1] + dy
        min_x, max_x, min_y, max_y = search_bounds
        if not (min_x <= neighbor[0] <= max_x and min_y <= neighbor[1] <= max_y):
            continue
        x, y = _cell_point(neighbor, grid_size)
        if not can_stand_at(x, y):
            continue
        if dx and dy:
            horizontal = _cell_point((cell[0] + dx, cell[1]), grid_size)
            vertical = _cell_point((cell[0], cell[1] + dy), grid_size)
            if not can_stand_at(*horizontal) or not can_stand_at(*vertical):
                continue
        result.append((neighbor, math.sqrt(2) if dx and dy else 1.0))
    return result


def _cell_point(cell: Cell, grid_size: int) -> Point:
    return float(cell[0] * grid_size), float(cell[1] * grid_size)


def _reconstruct_path(came_from: dict[Cell, Cell | None], current: Cell) -> list[Cell]:
    path = [current]
    while came_from[current] is not None:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path


def _compress_collinear(points: list[Point]) -> list[Point]:
    if len(points) <= 2:
        return points
    compressed = [points[0]]
    for index in range(1, len(points) - 1):
        previous = compressed[-1]
        current = points[index]
        following = points[index + 1]
        first_dx, first_dy = current[0] - previous[0], current[1] - previous[1]
        second_dx, second_dy = following[0] - current[0], following[1] - current[1]
        if first_dx * second_dy != first_dy * second_dx:
            compressed.append(current)
    compressed.append(points[-1])
    return compressed
