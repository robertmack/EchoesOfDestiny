from collections import Counter

from remembering.model import ObjectKind, PlayerState, WorldObject
from remembering.rules import (
    available_actions,
    can_craft_axe,
    can_craft_basket,
    can_craft_bucket,
    can_craft_hoe,
    has_new_craftable_tool,
)


def test_hoe_recipe_requires_all_three_resources() -> None:
    player = PlayerState(inventory=Counter({"stick": 1, "stone": 1, "fiber": 1}))
    assert can_craft_hoe(player)
    player.inventory["fiber"] = 0
    assert not can_craft_hoe(player)


def test_workbench_only_offers_crafting_when_recipe_is_available() -> None:
    workbench = WorldObject("bench", "Workbench", ObjectKind.WORKBENCH, 0, 0, 32, 32)
    player = PlayerState()

    assert available_actions(workbench, player) == []
    player.inventory.update({"stick": 1, "stone": 1, "fiber": 1})
    assert available_actions(workbench, player) == ["Craft Crude Hoe"]
    player.has_hoe = True
    assert available_actions(workbench, player) == []


def test_prepared_field_can_be_planted_when_seed_is_available() -> None:
    player = PlayerState(inventory=Counter({"seed": 1}))
    field = WorldObject("field", "Field", ObjectKind.FIELD, 0, 0, 10, 10, state="prepared")
    assert available_actions(field, player) == ["Plant Wheat"]


def test_berry_bush_offers_an_explicit_harvest_action() -> None:
    bush = WorldObject("bush", "Berry Bush", ObjectKind.BERRY_BUSH, 0, 0, 28, 28)

    assert available_actions(bush, PlayerState()) == [
        "Harvest Berries",
        "Pull Berry Bush",
    ]
    bush.active = False
    assert available_actions(bush, PlayerState()) == []


def test_tree_only_offers_one_branch() -> None:
    tree = WorldObject("tree", "Tree", ObjectKind.TREE, 0, 0, 30, 38)

    assert available_actions(tree, PlayerState()) == ["Break Off Branch"]
    tree.state = "branch_taken"
    assert available_actions(tree, PlayerState()) == []


def test_axe_recipe_and_tree_chopping_action() -> None:
    player = PlayerState(inventory=Counter({"stick": 1, "stone": 2, "fiber": 1}))
    tree = WorldObject("tree", "Tree", ObjectKind.TREE, 0, 0, 30, 38, state="branch_taken")

    assert can_craft_axe(player)
    assert available_actions(tree, player) == []
    player.has_axe = True
    player.carrying_axe = True
    assert not can_craft_axe(player)
    assert available_actions(tree, player) == ["Chop Down Tree"]


def test_workbench_notification_requires_a_new_craftable_tool() -> None:
    player = PlayerState(inventory=Counter({"stick": 1, "stone": 1, "fiber": 1}))
    assert has_new_craftable_tool(player)

    player.has_hoe = True
    assert not has_new_craftable_tool(player)
    player.inventory["stone"] = 2
    assert has_new_craftable_tool(player)

    player.has_axe = True
    assert not has_new_craftable_tool(player)

    player.inventory.update({"wood": 2, "fiber": 1})
    assert can_craft_bucket(player)
    assert has_new_craftable_tool(player)
    player.has_bucket = True
    assert not can_craft_bucket(player)
    assert not has_new_craftable_tool(player)

    player.inventory["fiber"] = 3
    assert can_craft_basket(player)
    assert has_new_craftable_tool(player)
    player.has_basket = True
    assert not can_craft_basket(player)
    assert not has_new_craftable_tool(player)
