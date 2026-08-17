import pygame

import remembering.game as game_module
from remembering.game import DayPlanActivity, Game
from remembering.model import Mode, RoutineStep
from remembering.ui_layout import (
    COMMAND_EDITOR_MAP_RECT,
    COMMAND_EDITOR_MESSAGES_RECT,
    COMMAND_EDITOR_RECT,
    LEFT_DOCK_RECT,
    LEFT_COMMAND_RECT,
    LEFT_MESSAGE_HISTORY_RECT,
    LEFT_SELECTION_RECT,
    MAP_RECT,
    MESSAGE_BAR_RECT,
    NATIVE_TILE_SIZE_PX,
    InventoryPage,
    RIGHT_DOCK_RECT,
    TIMELINE_RECT,
    WINDOW_ORIGIN_SCREENXY,
    WINDOW_SIZE_PX,
    player_dock_layout,
)


def test_root_regions_define_the_complete_logical_window() -> None:
    assert WINDOW_ORIGIN_SCREENXY == (0, 0)
    assert MAP_RECT.width == MAP_RECT.height == 1024
    assert MAP_RECT.width % NATIVE_TILE_SIZE_PX == 0
    assert LEFT_DOCK_RECT.width == RIGHT_DOCK_RECT.width == 448
    assert TIMELINE_RECT.bottom == MAP_RECT.top
    assert MESSAGE_BAR_RECT.top == MAP_RECT.bottom
    assert MESSAGE_BAR_RECT.bottom == WINDOW_SIZE_PX[1]
    assert LEFT_MESSAGE_HISTORY_RECT.width == LEFT_DOCK_RECT.width
    assert LEFT_MESSAGE_HISTORY_RECT.height == 120
    assert LEFT_MESSAGE_HISTORY_RECT.bottom == LEFT_DOCK_RECT.bottom
    assert LEFT_SELECTION_RECT.top == LEFT_DOCK_RECT.top
    assert LEFT_SELECTION_RECT.height == 300
    assert LEFT_COMMAND_RECT.top == LEFT_SELECTION_RECT.bottom
    assert LEFT_COMMAND_RECT.bottom == LEFT_MESSAGE_HISTORY_RECT.top


def test_command_edit_mode_uses_a_two_column_workspace() -> None:
    assert COMMAND_EDITOR_RECT == pygame.Rect(0, 0, 960, 1080)
    assert COMMAND_EDITOR_MAP_RECT == pygame.Rect(960, 0, 960, 720)
    assert COMMAND_EDITOR_MESSAGES_RECT == pygame.Rect(960, 720, 960, 360)


def test_message_history_is_retained_and_scrollable() -> None:
    game = Game(fullscreen=False)
    for index in range(30):
        game.log(f"History message {index}")

    assert len(game.messages) == 31
    assert game.message_scroll_offset == 0

    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEWHEEL,
            y=1,
            pos=LEFT_MESSAGE_HISTORY_RECT.center,
        )
    )
    game.handle_events()

    assert game.message_scroll_offset == 3


def test_collapsed_equipment_space_is_given_to_inventory() -> None:
    expanded = player_dock_layout(equipment_collapsed=False)
    collapsed = player_dock_layout(equipment_collapsed=True)

    released_height = expanded.equipment.height - collapsed.equipment.height
    assert collapsed.inventory.top == collapsed.equipment.bottom
    assert collapsed.inventory.height == expanded.inventory.height + released_height
    assert collapsed.inventory.bottom == expanded.inventory.bottom


def test_inventory_tabs_and_equipment_toggle_share_click_and_hotkey_paths() -> None:
    game = Game(fullscreen=False)
    game.draw_ui()
    expanded_inventory_height = player_dock_layout(
        equipment_collapsed=False
    ).inventory.height

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_e))
    game.handle_events()
    assert game.equipment_collapsed is True
    assert (
        player_dock_layout(equipment_collapsed=True).inventory.height
        > expanded_inventory_height
    )

    game.draw_ui()
    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=1,
            pos=game.equipment_toggle_button.center,
        )
    )
    game.handle_events()
    assert game.equipment_collapsed is False

    game.draw_ui()
    quests_button = next(
        rect
        for rect, page in game.player_info_tab_buttons
        if page is InventoryPage.QUESTS
    )
    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=1,
            pos=quests_button.center,
        )
    )
    game.handle_events()
    assert game.inventory_page is InventoryPage.QUESTS


def test_selection_and_actions_share_the_left_dock_without_replacing_stats() -> None:
    game = Game(fullscreen=False)
    workbench = game.object_of_type("workbench")
    game.selected_id = workbench.object_id

    game.draw_ui()

    assert game.action_buttons
    assert all(
        LEFT_COMMAND_RECT.contains(rect) for rect, _ in game.action_buttons
    )

    inventory_button = next(
        rect
        for rect, page in game.player_info_tab_buttons
        if page is InventoryPage.INVENTORY
    )
    pygame.event.post(
        pygame.event.Event(
            pygame.MOUSEBUTTONDOWN,
            button=1,
            pos=inventory_button.center,
        )
    )
    game.handle_events()

    assert game.selected_id == workbench.object_id
    assert game.selected_tile is None
    assert game.inventory_page is InventoryPage.INVENTORY


def test_regular_command_menu_exposes_macro_controls() -> None:
    game = Game(fullscreen=False)

    game.draw_ui()
    assert [action for _rect, action in game.macro_command_buttons] == [
        "Start",
        "Edit",
    ]

    game.start_macro_recording()
    game.draw_ui()
    assert [action for _rect, action in game.macro_command_buttons] == [
        "Stop",
        "Edit",
    ]


def test_day_planner_starts_with_explore_and_sleep_at_eleven() -> None:
    game = Game(fullscreen=False)
    game.draw_morning_menu()

    assert [(item.kind, item.scheduled_minutes) for item in game.day_plan] == [
        ("explore", None),
        ("sleep", 23 * 60),
    ]
    assert {action for _rect, action, _index in game.day_plan_buttons} >= {
        "start",
        "editor",
        "up",
        "down",
        "delete",
        "add_conditional",
        "add_activity:explore",
        "add_activity:sleep",
        "add_activity:power_nap",
        "add_activity:break",
    }


def test_day_planner_conditional_has_indented_addable_command_slots() -> None:
    game = Game(fullscreen=False)
    game.draw_morning_menu()
    add_conditional = next(
        rect
        for rect, action, _index in game.day_plan_buttons
        if action == "add_conditional"
    )
    game.handle_morning_click(add_conditional.center)
    game.draw_morning_menu()
    conditional_index = next(
        index for index, item in enumerate(game.day_plan) if item.kind == "conditional"
    )
    add_slot = next(
        rect
        for rect, action, index in game.day_plan_buttons
        if action == "add_slot" and index == conditional_index
    )
    game.handle_morning_click(add_slot.center)
    game.draw_morning_menu()

    conditional = game.day_plan[conditional_index]
    assert conditional.label == "If time ≥ 12:00 PM"
    assert [child.label for child in conditional.children] == ["Choose routine…"]
    assert any(
        action == "remove_slot:0" and index == conditional_index
        for _rect, action, index in game.day_plan_buttons
    )


def test_planner_pauses_between_fade_out_and_wake_fade() -> None:
    game = Game(fullscreen=False)
    game.day_transition_phase = "fade_out"
    game.day_transition_progress = 1.0
    game.day_transition_prepared = True

    game.update_day_transition(0.01)
    assert game.day_transition_phase == "planner"

    game.start_planned_day()
    assert game.day_transition_phase == "fade_in"


def test_planned_routines_begin_replay_and_unpause(monkeypatch) -> None:
    game = Game(fullscreen=False)
    command = RoutineStep(None, "Move To", target_point=(game.player.x, game.player.y))
    monkeypatch.setattr(game_module, "load_memory_file", lambda *args, **kwargs: [command])
    game.day_plan = [DayPlanActivity("macro", "Routine: Chores", macro_name="Chores")]
    game.simulation_paused = True

    game.start_planned_day()

    assert game.day.mode is Mode.REPLAY
    assert game.day.remembered_routine == [command]
    assert game.replay_outcome == "planned_day"
    assert game.simulation_paused is False


def test_rewind_vcr_effect_draws_without_changing_rewind_state() -> None:
    game = Game(fullscreen=False)
    game.day_transition_progress = 0.4
    game.rewind_cursor = 12.5
    before = (game.day_transition_progress, game.rewind_cursor)

    game.draw_rewind_vcr_effect()

    assert (game.day_transition_progress, game.rewind_cursor) == before
