from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Camera:
    viewport_left: int
    viewport_top: int
    viewport_width: int
    viewport_height: int
    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0
    min_zoom: float = 0.5
    max_zoom: float = 2.0
    dead_zone_ratio: float = 0.3

    def world_to_screen(self, point: tuple[float, float]) -> tuple[int, int]:
        return (
            round(self.viewport_left + (point[0] - self.x) * self.zoom),
            round(self.viewport_top + (point[1] - self.y) * self.zoom),
        )

    def screen_to_world(self, point: tuple[int, int]) -> tuple[int, int] | None:
        screen_x, screen_y = point
        if not (
            self.viewport_left <= screen_x < self.viewport_left + self.viewport_width
            and self.viewport_top <= screen_y < self.viewport_top + self.viewport_height
        ):
            return None
        return (
            round(self.x + (screen_x - self.viewport_left) / self.zoom),
            round(self.y + (screen_y - self.viewport_top) / self.zoom),
        )

    def set_zoom(
        self,
        zoom: float,
        anchor_screen: tuple[int, int],
        world_size: tuple[int, int],
    ) -> None:
        anchor_x, anchor_y = anchor_screen
        world_x = self.x + (anchor_x - self.viewport_left) / self.zoom
        world_y = self.y + (anchor_y - self.viewport_top) / self.zoom
        self.zoom = max(self.min_zoom, min(zoom, self.max_zoom))
        self.x = world_x - (anchor_x - self.viewport_left) / self.zoom
        self.y = world_y - (anchor_y - self.viewport_top) / self.zoom
        self.clamp(world_size)

    def center_on(self, target: tuple[float, float], world_size: tuple[int, int]) -> None:
        self.x = target[0] - self.viewport_width / self.zoom / 2
        self.y = target[1] - self.viewport_height / self.zoom / 2
        self.clamp(world_size)

    def pan_by_screen(
        self,
        delta: tuple[int, int],
        world_size: tuple[int, int],
    ) -> None:
        """Move the viewed world by a screen-space drag delta."""
        self.x -= delta[0] / self.zoom
        self.y -= delta[1] / self.zoom
        self.clamp(world_size)

    def follow(self, target: tuple[float, float], world_size: tuple[int, int]) -> None:
        target_x, target_y = target
        visible_width = self.viewport_width / self.zoom
        visible_height = self.viewport_height / self.zoom
        inset_x = visible_width * self.dead_zone_ratio
        inset_y = visible_height * self.dead_zone_ratio
        local_x = target_x - self.x
        local_y = target_y - self.y

        if local_x < inset_x:
            self.x = target_x - inset_x
        elif local_x > visible_width - inset_x:
            self.x = target_x - (visible_width - inset_x)
        if local_y < inset_y:
            self.y = target_y - inset_y
        elif local_y > visible_height - inset_y:
            self.y = target_y - (visible_height - inset_y)
        self.clamp(world_size)

    def clamp(self, world_size: tuple[int, int]) -> None:
        world_width, world_height = world_size
        visible_width = self.viewport_width / self.zoom
        visible_height = self.viewport_height / self.zoom
        self.x = max(0.0, min(self.x, max(0, world_width - visible_width)))
        self.y = max(0.0, min(self.y, max(0, world_height - visible_height)))
