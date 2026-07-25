from __future__ import annotations

import json

from remembering.model import ObjectKind, PlayerState, WorldObject, tree_state_data


def available_actions(obj: WorldObject, player: PlayerState) -> list[str]:
    if not obj.active:
        return []
    match obj.kind:
        case ObjectKind.STICK | ObjectKind.STONE | ObjectKind.GRASS | ObjectKind.WILD_GRAIN:
            return ["Gather"]
        case ObjectKind.BERRY_BUSH:
            return ["Harvest Berries", "Pull Berry Bush"]
        case ObjectKind.TREE:
            state = tree_state_data(obj.state)
            if state["form"] == "stump":
                return []
            actions = ["Break Off Branch"] if not state["branch_taken"] else []
            if player.carrying_axe:
                actions.append("Chop Down Tree")
            return actions
        case ObjectKind.WORKBENCH:
            actions = []
            if can_craft_hoe(player):
                actions.append("Craft Crude Hoe")
            if can_craft_axe(player):
                actions.append("Craft Crude Axe")
            if can_craft_bucket(player):
                actions.append("Craft Wooden Bucket")
            if can_craft_basket(player):
                actions.append("Weave Fiber Basket")
            return actions
        case ObjectKind.TOOL_STORAGE:
            actions: list[str] = []
            try:
                stored_tools = json.loads(obj.state).get("tools", {}) if obj.state else {}
            except (json.JSONDecodeError, TypeError, AttributeError):
                stored_tools = {}
            if player.has_hoe and player.carrying_hoe:
                actions.append("Store Hoe")
            if player.has_hoe and not player.carrying_hoe and stored_tools.get("hoe", {}).get("present", False):
                actions.append("Take Hoe")
            if player.has_axe and player.carrying_axe:
                actions.append("Store Axe")
            if player.has_axe and not player.carrying_axe and stored_tools.get("axe", {}).get("present", False):
                actions.append("Take Axe")
            return actions
        case ObjectKind.FIELD:
            if obj.state == "wild" and player.carrying_hoe:
                return ["Prepare Soil"]
            if obj.state == "prepared" and player.inventory["seed"]:
                return ["Plant Wheat"]
            if obj.state == "planted":
                return ["Whisper to Wheat"]
            if obj.state == "mature":
                return ["Harvest Wheat"]
            return []
        case ObjectKind.FOOD_PREP_STATION:
            return ["Cook Wheat"] if player.inventory["wheat"] and not player.meal_ready else []
        case ObjectKind.TABLE:
            return ["Eat Meal"] if player.meal_ready else []
        case ObjectKind.BED:
            return ["Sleep"]
        case ObjectKind.BARREL:
            try:
                water_uses = int(json.loads(obj.state or "{}").get("water_uses", 0))
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                water_uses = 0
            actions = []
            if player.has_bucket and water_uses < 30:
                actions.append("Fill Barrel")
            if player.has_bucket and player.bucket_water_uses > 0 and water_uses < 30:
                actions.append("Pour Water Into Barrel")
            if player.has_bucket and player.bucket_water_uses < 5 and water_uses > 0:
                actions.append("Fill Bucket From Barrel")
            return actions
    return []


def can_craft_hoe(player: PlayerState) -> bool:
    return (
        not player.has_hoe
        and player.inventory["stick"] >= 1
        and player.inventory["stone"] >= 1
        and player.inventory["fiber"] >= 1
    )


def can_craft_axe(player: PlayerState) -> bool:
    return (
        not player.has_axe
        and player.inventory["stick"] >= 1
        and player.inventory["stone"] >= 2
        and player.inventory["fiber"] >= 1
    )


def has_new_craftable_tool(player: PlayerState) -> bool:
    return can_craft_hoe(player) or can_craft_axe(player) or can_craft_bucket(player) or can_craft_basket(player)


def can_craft_bucket(player: PlayerState) -> bool:
    return (
        not player.has_bucket
        and player.inventory["wood"] >= 2
        and player.inventory["fiber"] >= 1
    )


def can_craft_basket(player: PlayerState) -> bool:
    return not player.has_basket and player.inventory["fiber"] >= 3
