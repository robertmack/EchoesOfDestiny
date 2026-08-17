from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from remembering.jsonc import loads_jsonc


DEFAULT_ILLNESS_TYPES_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "illness_types.jsonc"
)


class IllnessLoadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IllnessType:
    illness_id: str
    name: str
    description: str
    initial_value: float
    incubation_minutes: tuple[int, int]
    effect_type: str
    effect_grace_minutes: tuple[int, int]
    effect_condition_effects: dict[str, float]
    effect_clear_rate: float
    recovery_threshold: float
    onset_thought: str
    effect_thought: str
    tint_color: tuple[int, int, int]


@dataclass(slots=True)
class ActiveIllness:
    illness_id: str
    value: float
    onset_minute: int
    revealed: bool = False
    next_vomit_check_minute: int | None = None


def _range(value: object, owner: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise IllnessLoadError(f"{owner} must be a two-value array")
    result = int(value[0]), int(value[1])
    if result[0] < 0 or result[1] < result[0]:
        raise IllnessLoadError(f"{owner} has an invalid range")
    return result


def load_illness_types(
    path: Path = DEFAULT_ILLNESS_TYPES_PATH,
) -> dict[str, IllnessType]:
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        defaults = dict(data.get("defaults", {}))
        definitions: dict[str, IllnessType] = {}
        for authored in data.get("illness_types", []):
            entry = {**defaults, **dict(authored)}
            illness_id = str(entry["id"])
            effect = dict(entry["effect"])
            effects = {
                str(condition): float(amount)
                for condition, amount in effect["condition_effects"].items()
            }
            if set(effects) - {"trauma", "hunger", "thirst", "fatigue"}:
                raise IllnessLoadError(
                    f"Illness {illness_id!r} has unknown condition effects"
                )
            effect_type = str(effect["type"])
            clear_rate = float(effect["clear_rate"])
            tint = tuple(int(channel) for channel in entry["tint_color"])
            if (
                effect_type != "vomit"
                or not 0.0 <= clear_rate <= 1.0
                or len(tint) != 3
            ):
                raise IllnessLoadError(f"Illness {illness_id!r} has invalid effects")
            definitions[illness_id] = IllnessType(
                illness_id=illness_id,
                name=str(entry["name"]),
                description=str(entry["description"]),
                initial_value=float(entry["initial_value"]),
                incubation_minutes=_range(
                    entry["incubation_minutes"], f"{illness_id} incubation_minutes"
                ),
                effect_type=effect_type,
                effect_grace_minutes=_range(
                    effect["grace_minutes"], f"{illness_id} effect grace_minutes"
                ),
                effect_condition_effects=effects,
                effect_clear_rate=clear_rate,
                recovery_threshold=float(entry["recovery_threshold"]),
                onset_thought=str(entry["onset_thought"]),
                effect_thought=str(effect["thought"]),
                tint_color=(tint[0], tint[1], tint[2]),
            )
        return definitions
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, IllnessLoadError):
            raise
        raise IllnessLoadError(f"Could not load illness catalog {path}: {exc}") from exc
