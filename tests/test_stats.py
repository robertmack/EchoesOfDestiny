import pytest
import pygame

from remembering.game import (
    MORNING_OPENING_FADE_SECONDS,
    AreaTarget,
    Game,
    PendingJob,
)
from remembering.model import PlayerState
from remembering.stats import (
    CONDITION_IDS,
    apply_condition_effects,
    condition_color,
    condition_descriptor,
    condition_healing_contribution,
    condition_severity,
    fatigue_factor,
    gain_skill_experience,
    harvesting_speed_multiplier,
    healing_rate,
    critical_trauma_visible,
    learn_dawn_conditions,
    movement_speed_multiplier,
    task_speed_multiplier,
)
from remembering.world import MapLoadError, load_character_types, load_map


def test_character_instance_is_loaded_from_homestead() -> None:
    level = load_map(persistence_path=None)

    character = level.characters[level.controlled_character_id]

    assert character.type_id == "human"
    assert character.last_sleep_id == 1
    assert character.conditions == {
        "trauma": 90,
        "hunger": 95,
        "thirst": 95,
    }
    assert set(character.skills) == {"farming", "crafting", "harvesting"}
    assert set(level.character_types) == {"human", "rabbit", "wolf"}
    assert level.character_types["human"].description
    assert level.character_types["rabbit"].description
    assert level.character_types["wolf"].description
    assert set(level.character_types["human"].conditions) == {
        "trauma", "hunger", "thirst", "fatigue"
    }
    assert set(level.character_types["rabbit"].secondary_stats) == {
        "movement_speed", "task_speed", "healing_rate"
    }
    rabbit_behavior = level.character_types["rabbit"].behavior
    assert rabbit_behavior["predator"] is False
    assert rabbit_behavior["diet"]["crops"]["preferred"] is True
    assert rabbit_behavior["reproduction"]["enabled"] is True
    assert level.character_types["wolf"].behavior["predator"] is True


def test_condition_descriptor_uses_highest_matching_percentage() -> None:
    trauma = load_character_types()["human"].conditions["trauma"]

    assert condition_descriptor(trauma, 89)["text"] == "badly traumatized"
    assert condition_descriptor(trauma, 90)["text"] == "critically traumatized"


def test_wake_status_is_composed_from_live_condition_percentages() -> None:
    game = Game(fullscreen=False)

    segments = game.wake_status_segments()

    assert "".join(text for text, _font, _color in segments) == (
        "You wake up critically traumatized, exhausted, dying of thirst, "
        "and dying of hunger."
    )
    descriptor_segments = [
        segment
        for segment in segments
        if segment[0]
        in {"severely injured", "exhausted", "dying of thirst", "dying of hunger"}
    ]
    assert len({id(font) for _text, font, _color in descriptor_segments}) > 1
    assert len({color for _text, _font, color in descriptor_segments}) > 1


def test_morning_opening_fades_slowly_and_any_key_skips_it() -> None:
    game = Game(fullscreen=False)
    game.day_transition_phase = "fade_in"
    game.day_transition_progress = 0.0

    game.update_day_transition(MORNING_OPENING_FADE_SECONDS / 2)

    assert game.day_transition_phase == "fade_in"
    assert game.day_transition_progress == pytest.approx(0.5)

    pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a))
    game.handle_events()

    assert game.day_transition_phase is None
    assert game.day_transition_progress == 0.0


def test_ctrl_h_clears_all_negative_conditions() -> None:
    game = Game(fullscreen=False)
    for condition_id in CONDITION_IDS:
        game.player.conditions[condition_id] = 75.0

    pygame.event.post(
        pygame.event.Event(
            pygame.KEYDOWN,
            key=pygame.K_h,
            mod=pygame.KMOD_CTRL,
            unicode="h",
        )
    )
    game.handle_events()

    assert all(game.player.conditions[condition_id] == 0.0 for condition_id in CONDITION_IDS)
    assert game.messages[-1] == "Cheat: all negative conditions removed."


def test_character_type_inheritance_deep_merges_parent_and_child(tmp_path) -> None:
    catalog = tmp_path / "character_types.jsonc"
    catalog.write_text(
        """
        {
          "_defaults": {
            "conditions": {"trauma": {}},
            "secondary_stats": {"movement_speed": {}},
            "skills": {},
            "behavior": {
              "predator": false,
              "priorities": {},
              "diet": {},
              "senses": {
                "hearing_distance": 0,
                "smell_distance": 0,
                "vision_distance": 0
              },
              "sleep": {},
              "reproduction": {"enabled": false},
              "attacks": {},
              "abilities": {}
            }
          },
          "character_types": [
            {
              "id": "wolf",
              "name": "Wolf",
              "description": "A wolf.",
              "behavior": {
                "predator": true,
                "senses": {"smell_distance": 12},
                "attacks": {"bite": {"damage": 5}}
              }
            },
            {
              "id": "dire_wolf",
              "inherits": "wolf",
              "name": "Dire Wolf",
              "behavior": {
                "senses": {"smell_distance": 18},
                "attacks": {"bite": {"damage": 9}}
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    types = load_character_types(catalog)
    dire_wolf = types["dire_wolf"]

    assert dire_wolf.inherits == "wolf"
    assert dire_wolf.description == "A wolf."
    assert dire_wolf.behavior["predator"] is True
    assert dire_wolf.behavior["senses"] == {
        "hearing_distance": 0,
        "smell_distance": 18,
        "vision_distance": 0,
    }
    assert dire_wolf.behavior["attacks"]["bite"]["damage"] == 9


def test_character_type_inheritance_rejects_cycles(tmp_path) -> None:
    catalog = tmp_path / "character_types.jsonc"
    catalog.write_text(
        """
        {
          "_defaults": {},
          "character_types": [
            {"id": "one", "inherits": "two"},
            {"id": "two", "inherits": "one"}
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(MapLoadError, match="inheritance cycle"):
        load_character_types(catalog)


def test_fatigue_has_no_effect_until_it_exceeds_fifty() -> None:
    assert fatigue_factor(0) == 1
    assert fatigue_factor(50) == 1
    assert fatigue_factor(75) == pytest.approx(0.5)
    assert fatigue_factor(95) == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("condition", "expected"),
    ((1, 1.0), (10, 0.5), (20, 0.0), (50, 0.0), (80, 0.0), (90, -0.5), (99, -1.0)),
)
def test_healing_contribution_curve(condition, expected) -> None:
    assert condition_healing_contribution(condition) == pytest.approx(expected)


def test_healing_rate_sums_hunger_thirst_and_fatigue() -> None:
    player = PlayerState()
    player.conditions.update({"hunger": 1, "thirst": 10, "fatigue": 90})

    assert healing_rate(player) == pytest.approx(1.0)

    player.conditions.update({"hunger": 50, "thirst": 50, "fatigue": 50})
    assert healing_rate(player) == 0.0


def test_secondary_speeds_use_distinct_geometric_means() -> None:
    player = PlayerState()
    player.conditions = {
        "trauma": 90,
        "hunger": 0,
        "thirst": 0,
        "fatigue": 0,
    }

    assert movement_speed_multiplier(player) == pytest.approx(0.1 ** (1 / 3))
    assert task_speed_multiplier(player) == 1

    player.conditions["trauma"] = 0
    player.conditions["hunger"] = 90
    assert movement_speed_multiplier(player) == 1
    assert task_speed_multiplier(player) == pytest.approx(0.1 ** (1 / 3))


def test_injured_player_staggers_and_varies_speed_while_following_path() -> None:
    game = Game(fullscreen=False)
    door = next(
        boundary
        for boundary in game.map.boundaries
        if boundary.boundary_id == "bedroom_door_1"
    )
    door.open = True
    start = game.map.tile_map.tile_center(66, 64)
    destination = game.map.tile_map.tile_center(67, 64)
    game.player.x, game.player.y = start
    game.player.conditions["trauma"] = 90
    game.navigation_path = [destination]

    positions = [start]
    for _ in range(8):
        assert game.move_along_path(0.05)
        positions.append((game.player.x, game.player.y))

    step_lengths = [
        ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        for (x1, y1), (x2, y2) in zip(positions, positions[1:])
    ]
    assert any(abs(y - start[1]) > 0.01 for _x, y in positions[1:])
    assert max(step_lengths) - min(step_lengths) > 0.01


def test_harvesting_skill_levels_from_cumulative_xp_and_increases_speed() -> None:
    player = PlayerState()

    assert gain_skill_experience(
        player, "harvesting", 9, experience_per_level=10
    ) is False
    assert player.skills["harvesting"].level == 0
    assert gain_skill_experience(
        player, "harvesting", 1, experience_per_level=10
    ) is True
    assert player.skills["harvesting"].level == 1
    assert player.skills["harvesting"].experience == 10
    assert harvesting_speed_multiplier(player, speed_per_level=0.05) == pytest.approx(
        1.05
    )


def test_signed_condition_recovery_and_lethal_trauma() -> None:
    player = PlayerState()
    player.conditions = {
        "trauma": 95,
        "hunger": 50,
        "thirst": 50,
        "fatigue": 50,
    }

    lethal = apply_condition_effects(
        player, {"trauma": -5, "hunger": 20, "fatigue": -10}
    )

    assert lethal is True
    assert player.conditions == {
        "trauma": 99,
        "hunger": 30,
        "thirst": 50,
        "fatigue": 60,
    }


def test_dawn_condition_memory_moves_ten_percent_toward_targets() -> None:
    player = PlayerState()
    player.conditions.update({"trauma": 50, "hunger": 0, "thirst": 0})

    learn_dawn_conditions(player)

    assert player.condition_memory["trauma"] == pytest.approx(86)
    assert player.condition_memory["hunger"] == pytest.approx(93.5)
    assert player.condition_memory["thirst"] == pytest.approx(93.5)


def test_power_nap_uses_current_bed_quality_at_any_time() -> None:
    game = Game(fullscreen=False)
    bed = game.object_of_type("bed")
    game.day.current_time_minutes = 8 * 60
    game.player.conditions["fatigue"] = 82

    assert game.queue_job(bed.object_id, "Power Nap", record=True)

    bed.quality = 40
    game.complete_job(PendingJob(bed.object_id, "Power Nap"))

    assert game.player.conditions["fatigue"] == 42
    game.pending_job = None
    game.day.current_time_minutes = 23 * 60
    assert game.queue_job(bed.object_id, "Power Nap", record=True)


def test_hourly_needs_and_critical_damage() -> None:
    game = Game(fullscreen=False)
    game.player.conditions.update(
        {"trauma": 90, "hunger": 99, "thirst": 99, "fatigue": 50}
    )

    assert game.advance_player_conditions(60) is False

    assert game.player.conditions["trauma"] == 92
    assert game.player.conditions["fatigue"] == 51


def test_healing_rate_changes_trauma_during_condition_advance() -> None:
    game = Game(fullscreen=False)
    game.player.conditions.update(
        {"trauma": 50, "hunger": 0, "thirst": 0, "fatigue": 0}
    )

    assert game.advance_player_conditions(60) is False
    assert game.player.conditions["trauma"] == pytest.approx(47)

    game.player.conditions.update(
        {"trauma": 50, "hunger": 50, "thirst": 50, "fatigue": 50}
    )
    assert game.advance_player_conditions(60) is False
    assert game.player.conditions["trauma"] == pytest.approx(50)


def test_negative_healing_rate_can_trigger_death() -> None:
    game = Game(fullscreen=False)
    game.player.conditions.update(
        {"trauma": 98, "hunger": 98, "thirst": 98, "fatigue": 98}
    )

    assert game.advance_player_conditions(60) is True
    assert game.player.conditions["trauma"] == 99
    assert game.day_transition_phase == "fade_out"


def test_natural_water_is_inefficient_until_player_has_bucket() -> None:
    game = Game(fullscreen=False)
    source = next(
        point
        for point in game.water_sources_in_bounds(
            (0, 0, game.map.width, game.map.height)
        )
        if game.build_navigation_path(point)
    )
    game.player.x, game.player.y = source
    game.player.conditions["thirst"] = 95
    game.pending_area_target = AreaTarget("Drink Water", source)

    game._update_simulation_tick(0)
    assert game.player.conditions["thirst"] == 90

    game.create_carried_object("bucket", quality=50)
    game.pending_area_target = AreaTarget("Drink Water", source)
    game._update_simulation_tick(0)
    assert game.player.conditions["thirst"] == 65


def test_drinking_from_barrel_consumes_one_water_use() -> None:
    game = Game(fullscreen=False)
    game.player.inventory.update({"wood": 5, "fiber": 2})
    target = game.build_area_targets(
        "Build Barrel", (0, 0, game.map.width, game.map.height)
    )[0]
    game.build_barrel(target.placement_point or target.point)
    barrel = game.object_of_type("barrel")
    state = game.barrel_state(barrel)
    state["water_uses"] = 2
    barrel.state = state
    game.player.conditions["thirst"] = 50

    game.complete_job(PendingJob(barrel.object_id, "Drink from Barrel"))

    assert game.player.conditions["thirst"] == 25
    assert game.barrel_state(barrel)["water_uses"] == 1


def test_condition_thoughts_rotate_between_active_complaints() -> None:
    game = Game(fullscreen=False)
    game.player.conditions.update(
        {"trauma": 80, "hunger": 80, "thirst": 0, "fatigue": 0}
    )
    game.condition_thought_cooldown = 0

    game.update_condition_thoughts(0)
    assert game.thought_bubble_text == "Every step hurts."
    assert game.thought_bubble_source == "condition"

    game.thought_bubble_timer = 0
    game.thought_bubble_text = None
    game.thought_bubble_source = None
    game.condition_thought_cooldown = 0
    game.update_condition_thoughts(0)

    assert game.thought_bubble_text == "I need to find something to eat."


def test_memory_thought_takes_precedence_over_condition_complaint() -> None:
    game = Game(fullscreen=False)
    game.player.conditions["trauma"] = 99
    game.pending_failed_memory_thought = "Why did I come here?"

    game.show_empty_area_memory_thought()
    game.update_condition_thoughts(1)

    assert game.thought_bubble_text == "Why did I come here?"
    assert game.thought_bubble_source == "memory"
def test_condition_severity_colors_and_critical_trauma_blink() -> None:
    assert condition_severity(25) == "normal"
    assert condition_severity(50) == "elevated"
    assert condition_severity(75) == "severe"
    assert condition_severity(95) == "critical"
    assert condition_color(95) == (235, 88, 88)

    assert critical_trauma_visible(95, 250) is True
    assert critical_trauma_visible(96, 0) is True
    assert critical_trauma_visible(96, 250) is False
    assert critical_trauma_visible(96, 500) is True
