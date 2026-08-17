"""Authoritative screenxy geometry for the Remembering user interface.

screenxy begins at the logical window's upper-left corner. Positive x points
right and positive y points down. Gameplay coordinates do not belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pygame


WINDOW_TITLE = "Remembering — Python Prototype v0.1"
WINDOW_ORIGIN_SCREENXY = (0, 0)
WINDOW_WIDTH_PX = 1920
WINDOW_HEIGHT_PX = 1080
WINDOW_SIZE_PX = (WINDOW_WIDTH_PX, WINDOW_HEIGHT_PX)

NATIVE_TILE_SIZE_PX = 64
TIMELINE_HEIGHT_PX = 28
MESSAGE_BAR_HEIGHT_PX = 28
MAP_SIZE_PX = 1024
SIDEBAR_WIDTH_PX = (WINDOW_WIDTH_PX - MAP_SIZE_PX) // 2

LEFT_DOCK_RECT = pygame.Rect(0, 0, SIDEBAR_WIDTH_PX, WINDOW_HEIGHT_PX)
LEFT_SELECTION_HEIGHT_PX = 300
LEFT_ROUTINE_HEIGHT_PX = 92
LEFT_MESSAGE_HISTORY_HEIGHT_PX = 120
LEFT_SELECTION_RECT = pygame.Rect(
    LEFT_DOCK_RECT.x,
    LEFT_DOCK_RECT.y,
    LEFT_DOCK_RECT.width,
    LEFT_SELECTION_HEIGHT_PX,
)
LEFT_MESSAGE_HISTORY_RECT = pygame.Rect(
    LEFT_DOCK_RECT.x,
    LEFT_DOCK_RECT.bottom - LEFT_MESSAGE_HISTORY_HEIGHT_PX,
    LEFT_DOCK_RECT.width,
    LEFT_MESSAGE_HISTORY_HEIGHT_PX,
)
LEFT_ROUTINE_RECT = pygame.Rect(
    LEFT_DOCK_RECT.x,
    LEFT_SELECTION_RECT.bottom,
    LEFT_DOCK_RECT.width,
    LEFT_ROUTINE_HEIGHT_PX,
)
LEFT_COMMAND_RECT = pygame.Rect(
    LEFT_DOCK_RECT.x,
    LEFT_ROUTINE_RECT.bottom,
    LEFT_DOCK_RECT.width,
    LEFT_MESSAGE_HISTORY_RECT.top - LEFT_ROUTINE_RECT.bottom,
)
TIMELINE_RECT = pygame.Rect(
    SIDEBAR_WIDTH_PX, 0, MAP_SIZE_PX, TIMELINE_HEIGHT_PX
)
MAP_RECT = pygame.Rect(
    SIDEBAR_WIDTH_PX, TIMELINE_HEIGHT_PX, MAP_SIZE_PX, MAP_SIZE_PX
)
MESSAGE_BAR_RECT = pygame.Rect(
    SIDEBAR_WIDTH_PX, MAP_RECT.bottom, MAP_SIZE_PX, MESSAGE_BAR_HEIGHT_PX
)
RIGHT_DOCK_RECT = pygame.Rect(
    MAP_RECT.right, 0, SIDEBAR_WIDTH_PX, WINDOW_HEIGHT_PX
)

# Command-edit mode temporarily replaces the play layout with a two-column
# workspace while leaving the normal gameplay regions above unchanged.
COMMAND_EDITOR_RECT = pygame.Rect(0, 0, WINDOW_WIDTH_PX // 2, WINDOW_HEIGHT_PX)
COMMAND_EDITOR_MAP_RECT = pygame.Rect(
    COMMAND_EDITOR_RECT.right, 0, WINDOW_WIDTH_PX // 2, 720
)
COMMAND_EDITOR_MESSAGES_RECT = pygame.Rect(
    COMMAND_EDITOR_RECT.right,
    COMMAND_EDITOR_MAP_RECT.bottom,
    WINDOW_WIDTH_PX // 2,
    WINDOW_HEIGHT_PX - COMMAND_EDITOR_MAP_RECT.bottom,
)

PANEL_HEADER_HEIGHT_PX = 32
PLAYER_INFO_HEIGHT_PX = 300
EQUIPMENT_HEIGHT_PX = 260


class InventoryPage(Enum):
    INVENTORY = "inventory"
    QUESTS = "quests"
    MACROS = "routines"


@dataclass(frozen=True, slots=True)
class PlayerDockLayout:
    player_info: pygame.Rect
    equipment: pygame.Rect
    inventory: pygame.Rect
    inventory_tab: pygame.Rect
    quests_tab: pygame.Rect
    macros_tab: pygame.Rect
    equipment_toggle: pygame.Rect


def player_dock_layout(*, equipment_collapsed: bool) -> PlayerDockLayout:
    """Lay out the player dock, giving collapsed equipment space to inventory."""
    player_info = pygame.Rect(
        RIGHT_DOCK_RECT.x,
        RIGHT_DOCK_RECT.y,
        RIGHT_DOCK_RECT.width,
        PLAYER_INFO_HEIGHT_PX,
    )
    equipment_height = (
        PANEL_HEADER_HEIGHT_PX if equipment_collapsed else EQUIPMENT_HEIGHT_PX
    )
    equipment = pygame.Rect(
        RIGHT_DOCK_RECT.x,
        player_info.bottom,
        RIGHT_DOCK_RECT.width,
        equipment_height,
    )
    inventory = pygame.Rect(
        RIGHT_DOCK_RECT.x,
        equipment.bottom,
        RIGHT_DOCK_RECT.width,
        RIGHT_DOCK_RECT.bottom - equipment.bottom,
    )
    tab_width = 92
    inventory_tab = pygame.Rect(
        inventory.x + 10, inventory.y + 6, tab_width, 24
    )
    quests_tab = pygame.Rect(
        inventory_tab.right + 4, inventory_tab.y, tab_width, 24
    )
    macros_tab = pygame.Rect(
        quests_tab.right + 4, inventory_tab.y, tab_width, 24
    )
    equipment_toggle = pygame.Rect(
        equipment.right - 34, equipment.y + 4, 24, 24
    )
    return PlayerDockLayout(
        player_info=player_info,
        equipment=equipment,
        inventory=inventory,
        inventory_tab=inventory_tab,
        quests_tab=quests_tab,
        macros_tab=macros_tab,
        equipment_toggle=equipment_toggle,
    )


RELOAD_BUTTON_RECT = pygame.Rect(
    RIGHT_DOCK_RECT.x + 12,
    WINDOW_HEIGHT_PX - 46,
    RIGHT_DOCK_RECT.width - 24,
    32,
)


def validate_layout() -> None:
    if WINDOW_ORIGIN_SCREENXY != (0, 0):
        raise ValueError("The logical screenxy origin must be (0, 0).")
    if MAP_SIZE_PX % NATIVE_TILE_SIZE_PX:
        raise ValueError("MAP_SIZE_PX must contain a whole number of native tiles.")
    if LEFT_DOCK_RECT.width != RIGHT_DOCK_RECT.width:
        raise ValueError("The centered map requires equal sidebar widths.")
    if MESSAGE_BAR_RECT.bottom != WINDOW_HEIGHT_PX:
        raise ValueError("Timeline, map, and message bar must fill the window height.")
    window_rect = pygame.Rect(WINDOW_ORIGIN_SCREENXY, WINDOW_SIZE_PX)
    for name, rect in {
        "left dock": LEFT_DOCK_RECT,
        "left selection": LEFT_SELECTION_RECT,
        "left routines": LEFT_ROUTINE_RECT,
        "left commands": LEFT_COMMAND_RECT,
        "left message history": LEFT_MESSAGE_HISTORY_RECT,
        "timeline": TIMELINE_RECT,
        "map": MAP_RECT,
        "message bar": MESSAGE_BAR_RECT,
        "right dock": RIGHT_DOCK_RECT,
    }.items():
        if not window_rect.contains(rect):
            raise ValueError(f"The {name} rectangle lies outside the logical window.")


validate_layout()
