from collections import Counter

from remembering.model import PlayerState, WorldObject
from remembering.rules import available_actions
from remembering.world import load_object_types


def catalog_object(
    type_id: str,
    *,
    form_id: str | None = None,
    variant: str | None = None,
    state: dict[str, object] | None = None,
    container: str | None = None,
) -> WorldObject:
    definition = load_object_types()[type_id]
    form = definition.form_definition(form_id, variant)
    return WorldObject(
        1,
        form.name or definition.name_for(variant),
        definition.kind,
        0,
        0,
        32,
        32,
        state=state or {},
        interactions={
            label: dict(action) for label, action in form.interactions.items()
        },
        capacity=form.capacity_for(100),
        type_id=type_id,
        variant=variant,
        form=form.form_id,
        mobility=form.mobility,
        traits=form.traits,
        container=container,
    )


def test_hoe_recipe_requires_all_three_resources() -> None:
    workbench = catalog_object("workbench")
    player = PlayerState(inventory=Counter({"branch": 1, "pebble": 1, "fiber": 1}))

    assert "Craft Crude Hoe" in available_actions(workbench, player)
    player.inventory["fiber"] = 0
    assert "Craft Crude Hoe" not in available_actions(workbench, player)


def test_workbench_only_offers_crafting_when_recipe_is_available() -> None:
    workbench = catalog_object("workbench")
    player = PlayerState()

    assert available_actions(workbench, player) == []
    player.inventory.update({"branch": 1, "pebble": 1, "fiber": 1})
    assert available_actions(workbench, player) == ["Craft Crude Hoe"]
    player.has_hoe = True
    assert available_actions(workbench, player) == []


def test_mature_wheat_exposes_its_variant_loot() -> None:
    crop = catalog_object("crop", form_id="mature", variant="wheat")

    assert available_actions(crop, PlayerState()) == ["Harvest Wheat"]
    assert crop.interactions["Harvest Wheat"]["loot"]["wheat"] == {"grains": 3}


def test_berry_bush_offers_explicit_harvest_actions() -> None:
    bush = catalog_object("bush", variant="berry")

    assert available_actions(bush, PlayerState()) == [
        "Harvest Berries",
        "Harvest and Eat Berries",
        "Pull Berry Bush",
    ]
    bush.state["has_berries"] = False
    assert available_actions(bush, PlayerState()) == ["Pull Berry Bush"]
    bush.active = False
    assert available_actions(bush, PlayerState()) == []


def test_tree_only_offers_one_branch() -> None:
    tree = catalog_object(
        "tree", form_id="standing", state={"branch_taken": False}
    )

    assert available_actions(tree, PlayerState()) == ["Break Off Branch"]
    tree.state["branch_taken"] = True
    assert available_actions(tree, PlayerState()) == []


def test_tree_chopping_requires_a_carried_chopping_item() -> None:
    tree = catalog_object(
        "tree", form_id="standing", state={"branch_taken": True}
    )
    player = PlayerState()

    assert available_actions(tree, player) == []
    axe = catalog_object("axe", container="player")
    player.carried_objects.append(axe)
    assert available_actions(tree, player) == ["Chop Down Tree"]


def test_workbench_notification_inputs_come_from_catalog_actions() -> None:
    workbench = catalog_object("workbench")
    player = PlayerState(inventory=Counter({"branch": 1, "pebble": 1, "fiber": 1}))

    assert available_actions(workbench, player) == ["Craft Crude Hoe"]
    player.has_hoe = True
    player.inventory["pebble"] = 2
    assert available_actions(workbench, player) == ["Craft Crude Axe"]
    player.has_axe = True
    player.inventory.update({"wood": 2, "fiber": 1})
    assert available_actions(workbench, player) == ["Craft Wooden Bucket"]
    player.carried_objects.append(catalog_object("bucket", container="player"))
    player.inventory["wood"] = 0
    player.inventory["fiber"] = 3
    assert available_actions(workbench, player) == ["Weave Fiber Basket"]
    player.carried_objects.append(catalog_object("basket", container="player"))
    assert available_actions(workbench, player) == []
