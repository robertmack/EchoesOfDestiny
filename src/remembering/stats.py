from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from remembering.model import PlayerState


def gain_skill_experience(
    player: PlayerState,
    skill_id: str,
    amount: float,
    *,
    experience_per_level: float = 10.0,
) -> bool:
    """Award cumulative XP and return whether the skill gained a level."""
    if experience_per_level <= 0:
        raise ValueError("experience_per_level must be positive")
    skill = player.skills.get(skill_id)
    if skill is None:
        return False
    old_level = skill.level
    skill.experience = max(0.0, skill.experience + amount)
    skill.level = int(skill.experience // experience_per_level)
    return skill.level > old_level


def harvesting_speed_multiplier(
    player: PlayerState, *, speed_per_level: float = 0.05
) -> float:
    skill = player.skills.get("harvesting")
    return 1.0 + max(0, skill.level if skill is not None else 0) * speed_per_level


CONDITION_IDS = ("trauma", "hunger", "thirst", "fatigue")
CONDITION_LABELS = {
    "trauma": "Trauma",
    "hunger": "Hunger",
    "thirst": "Thirst",
    "fatigue": "Fatigue",
}

CONDITION_SEVERITY_COLORS = {
    "normal": (112, 198, 112),
    "elevated": (225, 205, 105),
    "severe": (230, 151, 78),
    "critical": (235, 88, 88),
}


def condition_descriptor(
    condition_config: dict[str, object], value: float
) -> dict[str, object] | None:
    """Return the configured descriptor row for a condition percentage."""
    table = condition_config.get("descriptor_table", [])
    if not isinstance(table, list):
        return None
    matches = [
        row
        for row in table
        if isinstance(row, dict)
        and isinstance(row.get("minimum"), (int, float))
        and float(row["minimum"]) <= value
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: float(row["minimum"]))


def condition_severity(value: float) -> str:
    if value >= 95.0:
        return "critical"
    if value >= 75.0:
        return "severe"
    if value >= 50.0:
        return "elevated"
    return "normal"


def condition_color(value: float) -> tuple[int, int, int]:
    return CONDITION_SEVERITY_COLORS[condition_severity(value)]


def critical_trauma_visible(value: float, elapsed_ms: int) -> bool:
    """Blink critical Trauma twice per second; otherwise remain visible."""
    return value <= 95.0 or (elapsed_ms // 250) % 2 == 0


def clamp_condition(value: float) -> float:
    return max(0.0, min(99.0, float(value)))


def fatigue_factor(fatigue: float) -> float:
    return 1.0 if fatigue <= 50.0 else max(0.02, (100.0 - fatigue) / 50.0)


def _geometric_mean(values: tuple[float, ...]) -> float:
    return math.prod(values) ** (1.0 / len(values))


def movement_speed_multiplier(player: PlayerState) -> float:
    conditions = player.conditions
    return _geometric_mean(
        (
            (100.0 - conditions["trauma"]) / 100.0,
            (100.0 - conditions["thirst"]) / 100.0,
            fatigue_factor(conditions["fatigue"]),
        )
    )


def task_speed_multiplier(player: PlayerState) -> float:
    conditions = player.conditions
    return _geometric_mean(
        (
            (100.0 - conditions["hunger"]) / 100.0,
            (100.0 - conditions["thirst"]) / 100.0,
            fatigue_factor(conditions["fatigue"]),
        )
    )


HEALING_CURVE = (
    (1.0, 1.0),
    (10.0, 0.5),
    (20.0, 0.0),
    (80.0, 0.0),
    (90.0, -0.5),
    (99.0, -1.0),
)


def condition_healing_contribution(value: float) -> float:
    """Map a burden condition to signed Trauma recovery per game hour."""
    value = clamp_condition(value)
    if value <= HEALING_CURVE[0][0]:
        return HEALING_CURVE[0][1]
    for (low_value, low_rate), (high_value, high_rate) in zip(
        HEALING_CURVE, HEALING_CURVE[1:]
    ):
        if value <= high_value:
            progress = (value - low_value) / (high_value - low_value)
            return low_rate + (high_rate - low_rate) * progress
    return HEALING_CURVE[-1][1]


def healing_rate(player: PlayerState) -> float:
    return sum(
        condition_healing_contribution(player.conditions[condition_id])
        for condition_id in ("hunger", "thirst", "fatigue")
    )


@dataclass(frozen=True, slots=True)
class SecondaryStatDefinition:
    stat_id: str
    label: str
    calculator: Callable[[PlayerState], float]
    parent: str | None = None


SECONDARY_STATS = {
    "movement_speed": SecondaryStatDefinition(
        "movement_speed", "Movement Speed", movement_speed_multiplier
    ),
    "task_speed": SecondaryStatDefinition(
        "task_speed", "Task Speed", task_speed_multiplier
    ),
    "healing_rate": SecondaryStatDefinition(
        "healing_rate", "Healing Rate", healing_rate
    ),
}


def secondary_stat(player: PlayerState, stat_id: str) -> float:
    definition = SECONDARY_STATS.get(stat_id)
    if definition is None:
        raise KeyError(f"Unknown secondary stat {stat_id!r}")
    return definition.calculator(player)


def apply_condition_effects(
    player: PlayerState, recovery: dict[str, float]
) -> bool:
    """Apply signed recovery values and report whether Trauma became lethal."""
    lethal = False
    for condition_id, amount in recovery.items():
        if condition_id not in CONDITION_IDS:
            raise KeyError(f"Unknown condition {condition_id!r}")
        raw = player.conditions[condition_id] - float(amount)
        if condition_id == "trauma" and raw > 99.0:
            lethal = True
        player.conditions[condition_id] = clamp_condition(raw)
    return lethal


def learn_dawn_conditions(player: PlayerState, rate: float = 0.10) -> None:
    targets = {
        "trauma": player.conditions["trauma"],
        "hunger": max(80.0, min(95.0, 80.0 + player.conditions["hunger"] / 5.0)),
        "thirst": max(80.0, min(95.0, 80.0 + player.conditions["thirst"] / 5.0)),
    }
    for condition_id, target in targets.items():
        old = player.condition_memory[condition_id]
        learned = old + rate * (target - old)
        if condition_id == "trauma":
            learned = min(90.0, learned)
        player.condition_memory[condition_id] = clamp_condition(learned)
