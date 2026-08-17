from __future__ import annotations

from remembering.model import PlayerState, WorldObject, tree_state_data


def available_actions(obj: WorldObject, player: PlayerState) -> list[str]:
    if not obj.active:
        return []
    return [
        action
        for action, definition in obj.interactions.items()
        if interaction_is_available(obj, player, definition)
    ]


def interaction_is_available(
    obj: WorldObject, player: PlayerState, definition: dict[str, object]
) -> bool:
    variants = definition.get("variants")
    if variants and obj.variant not in variants:
        return False
    requirements = definition.get("requires", {})
    player_requirements = requirements.get("player", {})
    if any(
        getattr(player, field, None) != expected
        for field, expected in player_requirements.items()
    ):
        return False
    required_carried_type = requirements.get("carried_type")
    if required_carried_type and not any(
        carried.active
        and carried.container == "player"
        and carried.type_id == required_carried_type
        for carried in player.carried_objects
    ):
        return False
    required_trait = requirements.get("item_trait")
    if required_trait and not any(
        required_trait in carried.traits for carried in player.carried_objects
    ):
        return False
    cost = definition.get("cost", {})
    if any(player.inventory[item] < int(amount) for item, amount in cost.items()):
        return False

    availability = definition.get("availability")
    stored_tools = obj.state.get("tools", {})
    water_uses = int(obj.state.get("water_uses", 0))
    water_capacity = int(obj.capacity.get("water", 0))
    predicates = {
        None: True,
        "tree_has_branch": not bool(tree_state_data(obj.state)["branch_taken"]),
        "bush_has_berries": bool(obj.state.get("has_berries", True)),
        "object_is_uncontained": obj.container is None,
        "stored_hoe_available": (
            player.has_hoe
            and not player.carrying_hoe
            and bool(stored_tools.get("hoe", {}).get("present", False))
        ),
        "stored_axe_available": (
            player.has_axe
            and not player.carrying_axe
            and bool(stored_tools.get("axe", {}).get("present", False))
        ),
        "stored_bucket_available": (
            not player.has_bucket and bool(obj.state.get("bucket_ids", []))
        ),
        "barrel_can_fill": (
            player.has_bucket
            and int(player.bucket.capacity.get("water", 0)) > 0
            and water_uses < water_capacity
        ),
        "barrel_can_receive_bucket": (
            player.has_bucket
            and player.bucket_water_uses > 0
            and water_uses < water_capacity
        ),
        "barrel_can_fill_bucket": (
            player.has_bucket
            and player.bucket_water_uses
            < int(player.bucket.capacity.get("water", 0))
            and water_uses > 0
        ),
        "barrel_has_water": water_uses > 0,
        "porridge_ingredients": (
            player.inventory["grains"] >= 3
            and player.has_bucket
            and player.bucket_water_uses >= 1
        ),
        "carried_edible": any(
            carried.active
            and carried.container == "player"
            and "edible" in carried.traits
            for carried in player.carried_objects
        ),
        "cupboard_can_store_food": (
            any(
                carried.active
                and carried.container == "player"
                and "edible" in carried.traits
                for carried in player.carried_objects
            )
            and len(obj.state.get("food_ids", []))
            < int(obj.capacity.get("food", 0))
        ),
        "cupboard_has_food": bool(obj.state.get("food_ids", [])),
    }
    return predicates.get(availability, False)
