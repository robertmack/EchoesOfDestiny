from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from remembering.jsonc import loads_jsonc
from remembering.model import DayState, PlayerState


DEFAULT_QUESTS_PATH = Path(__file__).resolve().parents[2] / "data" / "quests.jsonc"


class QuestLoadError(ValueError):
    """Raised when authored quest data is invalid."""


class QuestStatus(str, Enum):
    LOCKED = "locked"
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class QuestDefinition:
    quest_id: str
    title: str
    description: str
    trigger: Mapping[str, Any]
    completion: Mapping[str, Any]
    rewards: tuple[Mapping[str, Any], ...] = ()
    group_id: str | None = None
    prerequisites: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestGroup:
    group_id: str
    title: str
    description: str
    quest_ids: tuple[str, ...]
    sequential: bool = True


@dataclass(frozen=True, slots=True)
class QuestContext:
    player: PlayerState
    day: DayState
    manager: QuestManager


ConditionHook = Callable[[QuestContext, Mapping[str, Any]], bool]
RewardHook = Callable[[QuestContext, Mapping[str, Any]], None]


@dataclass(slots=True)
class QuestHooks:
    """Safe extension points for requirements too specific for the JSON vocabulary."""

    conditions: dict[str, ConditionHook] = field(default_factory=dict)
    rewards: dict[str, RewardHook] = field(default_factory=dict)

    def condition(self, name: str) -> Callable[[ConditionHook], ConditionHook]:
        def register(callback: ConditionHook) -> ConditionHook:
            self.conditions[name] = callback
            return callback

        return register

    def reward(self, name: str) -> Callable[[RewardHook], RewardHook]:
        def register(callback: RewardHook) -> RewardHook:
            self.rewards[name] = callback
            return callback

        return register


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestLoadError(f"{label} must be a non-empty string")
    return value


def load_quest_catalog(
    path: Path = DEFAULT_QUESTS_PATH,
) -> tuple[dict[str, QuestDefinition], dict[str, QuestGroup]]:
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise QuestLoadError("quest catalog must be an object")
        if data.get("_schema_version") != 1:
            raise QuestLoadError("unsupported or missing _schema_version")
        raw_quests = data.get("quests", [])
        raw_groups = data.get("groups", [])
        if not isinstance(raw_quests, list) or not isinstance(raw_groups, list):
            raise QuestLoadError("quests and groups must be arrays")

        quests: dict[str, QuestDefinition] = {}
        for index, raw in enumerate(raw_quests):
            if not isinstance(raw, dict):
                raise QuestLoadError(f"quest {index + 1} must be an object")
            quest_id = _string(raw.get("id"), f"quest {index + 1} id")
            if quest_id in quests:
                raise QuestLoadError(f"duplicate quest id {quest_id!r}")
            trigger = raw.get("trigger", {"always": True})
            completion = raw.get("completion")
            rewards = raw.get("rewards", [])
            prerequisites = raw.get("prerequisites", [])
            if not isinstance(trigger, dict) or not isinstance(completion, dict):
                raise QuestLoadError(f"quest {quest_id!r} conditions must be objects")
            if not isinstance(rewards, list) or not all(isinstance(x, dict) for x in rewards):
                raise QuestLoadError(f"quest {quest_id!r} rewards must be an array of objects")
            if not isinstance(prerequisites, list) or not all(
                isinstance(x, str) for x in prerequisites
            ):
                raise QuestLoadError(f"quest {quest_id!r} prerequisites must be strings")
            quests[quest_id] = QuestDefinition(
                quest_id=quest_id,
                title=_string(raw.get("title"), f"quest {quest_id!r} title"),
                description=_string(
                    raw.get("description"), f"quest {quest_id!r} description"
                ),
                trigger=trigger,
                completion=completion,
                rewards=tuple(rewards),
                group_id=str(raw["group"]) if raw.get("group") is not None else None,
                prerequisites=tuple(prerequisites),
            )

        groups: dict[str, QuestGroup] = {}
        for index, raw in enumerate(raw_groups):
            if not isinstance(raw, dict):
                raise QuestLoadError(f"group {index + 1} must be an object")
            group_id = _string(raw.get("id"), f"group {index + 1} id")
            quest_ids = raw.get("quests", [])
            if not isinstance(quest_ids, list) or not all(
                isinstance(x, str) for x in quest_ids
            ):
                raise QuestLoadError(f"group {group_id!r} quests must be strings")
            if group_id in groups:
                raise QuestLoadError(f"duplicate group id {group_id!r}")
            unknown = [quest_id for quest_id in quest_ids if quest_id not in quests]
            if unknown:
                raise QuestLoadError(
                    f"group {group_id!r} references unknown quest {unknown[0]!r}"
                )
            groups[group_id] = QuestGroup(
                group_id,
                _string(raw.get("title"), f"group {group_id!r} title"),
                str(raw.get("description", "")),
                tuple(quest_ids),
                bool(raw.get("sequential", True)),
            )
        for quest in quests.values():
            unknown = [item for item in quest.prerequisites if item not in quests]
            if unknown:
                raise QuestLoadError(
                    f"quest {quest.quest_id!r} has unknown prerequisite {unknown[0]!r}"
                )
            if quest.group_id is not None and quest.group_id not in groups:
                raise QuestLoadError(
                    f"quest {quest.quest_id!r} has unknown group {quest.group_id!r}"
                )
        return quests, groups
    except QuestLoadError:
        raise
    except (OSError, ValueError) as exc:
        raise QuestLoadError(f"Could not load quest catalog {path}: {exc}") from exc


def load_quest_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        state = data.get("quest_state", {}) if isinstance(data, dict) else {}
        if not isinstance(state, dict):
            raise QuestLoadError("quest_state must be an object")
        return dict(state)
    except QuestLoadError:
        raise
    except (OSError, ValueError) as exc:
        raise QuestLoadError(f"Could not load quest state {path}: {exc}") from exc


class QuestManager:
    def __init__(
        self,
        quests: Mapping[str, QuestDefinition],
        groups: Mapping[str, QuestGroup] | None = None,
        hooks: QuestHooks | None = None,
    ) -> None:
        self.quests = dict(quests)
        self.groups = dict(groups or {})
        self.hooks = hooks or QuestHooks()
        self.statuses = {quest_id: QuestStatus.LOCKED for quest_id in self.quests}
        self.events: Counter[str] = Counter()
        self.daily_events: Counter[str] = Counter()
        self.start_day_conditions: dict[str, float] = {}
        self.bedtime_conditions: dict[str, float] = {}

    @classmethod
    def from_file(
        cls, path: Path = DEFAULT_QUESTS_PATH, hooks: QuestHooks | None = None
    ) -> QuestManager:
        quests, groups = load_quest_catalog(path)
        return cls(quests, groups, hooks)

    @property
    def active_quests(self) -> tuple[QuestDefinition, ...]:
        return tuple(
            quest
            for quest_id, quest in self.quests.items()
            if self.statuses[quest_id] is QuestStatus.ACTIVE
        )

    def status(self, quest_id: str) -> QuestStatus:
        return self.statuses[quest_id]

    def record_event(self, name: str, amount: int = 1) -> None:
        """Record durable gameplay progress used by event-based conditions."""
        if amount > 0:
            self.events[name] += amount
            self.daily_events[name] += amount

    def record_start_day(self, conditions: Mapping[str, float]) -> None:
        """Capture dawn condition levels and begin a fresh set of daily counters."""
        self.start_day_conditions = {
            str(name): float(value) for name, value in conditions.items()
        }
        self.daily_events.clear()

    def record_bedtime(self, conditions: Mapping[str, float]) -> None:
        """Capture condition levels at the moment the character goes to bed."""
        self.bedtime_conditions = {
            str(name): float(value) for name, value in conditions.items()
        }

    def update(self, player: PlayerState, day: DayState) -> list[str]:
        """Trigger and complete every eligible quest, returning player-facing messages."""
        context = QuestContext(player, day, self)
        messages: list[str] = []
        changed = True
        while changed:
            changed = False
            for quest_id, quest in self.quests.items():
                status = self.statuses[quest_id]
                if status is QuestStatus.LOCKED and self._can_trigger(quest, context):
                    self.statuses[quest_id] = QuestStatus.ACTIVE
                    messages.append(f"Quest started: {quest.title}")
                    changed = True
                    status = QuestStatus.ACTIVE
                if status is QuestStatus.ACTIVE and self._matches(
                    quest.completion, context
                ):
                    self.statuses[quest_id] = QuestStatus.COMPLETED
                    self._grant_rewards(quest, context)
                    messages.append(f"Quest completed: {quest.title}")
                    changed = True
        return messages

    def _can_trigger(self, quest: QuestDefinition, context: QuestContext) -> bool:
        prerequisites = list(quest.prerequisites)
        if quest.group_id:
            group = self.groups[quest.group_id]
            if group.sequential and quest.quest_id in group.quest_ids:
                index = group.quest_ids.index(quest.quest_id)
                prerequisites.extend(group.quest_ids[:index])
        return all(
            self.statuses[item] is QuestStatus.COMPLETED for item in prerequisites
        ) and self._matches(quest.trigger, context)

    def _matches(self, condition: Mapping[str, Any], context: QuestContext) -> bool:
        if "all" in condition:
            return all(self._matches(item, context) for item in condition["all"])
        if "any" in condition:
            return any(self._matches(item, context) for item in condition["any"])
        if "not" in condition:
            return not self._matches(condition["not"], context)
        if "always" in condition:
            return bool(condition["always"])
        if "inventory" in condition:
            return all(
                context.player.inventory[item] >= int(amount)
                for item, amount in condition["inventory"].items()
            )
        if "player" in condition:
            return all(
                getattr(context.player, name, None) == expected
                for name, expected in condition["player"].items()
            )
        if "achievement" in condition:
            return str(condition["achievement"]) in context.player.achievements
        if "day_at_least" in condition:
            return context.day.number >= int(condition["day_at_least"])
        if "quest_completed" in condition:
            return self.statuses.get(str(condition["quest_completed"])) is QuestStatus.COMPLETED
        if "event" in condition:
            return all(
                self.events[name] >= int(amount)
                for name, amount in condition["event"].items()
            )
        if "event_today" in condition:
            return all(
                self.daily_events[name] >= int(amount)
                for name, amount in condition["event_today"].items()
            )
        if "start_day_condition" in condition:
            return self._condition_levels_match(
                self.start_day_conditions, condition["start_day_condition"]
            )
        if "bedtime_condition" in condition:
            return self._condition_levels_match(
                self.bedtime_conditions, condition["bedtime_condition"]
            )
        if "custom" in condition:
            name = str(condition["custom"])
            try:
                return bool(self.hooks.conditions[name](context, condition.get("args", {})))
            except KeyError as exc:
                raise QuestLoadError(f"unknown custom condition hook {name!r}") from exc
        raise QuestLoadError(f"unknown condition: {dict(condition)!r}")

    @staticmethod
    def _condition_levels_match(
        actual: Mapping[str, float], requirements: Mapping[str, object]
    ) -> bool:
        for name, requirement in requirements.items():
            if name not in actual or not isinstance(requirement, Mapping):
                return False
            value = actual[name]
            if "at_most" in requirement and value > float(requirement["at_most"]):
                return False
            if "at_least" in requirement and value < float(requirement["at_least"]):
                return False
            if "equal" in requirement and value != float(requirement["equal"]):
                return False
            if not any(key in requirement for key in ("at_most", "at_least", "equal")):
                raise QuestLoadError(
                    f"condition level {name!r} needs at_most, at_least, or equal"
                )
        return True

    def _grant_rewards(self, quest: QuestDefinition, context: QuestContext) -> None:
        for reward in quest.rewards:
            if "inventory" in reward:
                for item, amount in reward["inventory"].items():
                    context.player.inventory[item] += int(amount)
            elif "achievement" in reward:
                context.player.achievements.add(str(reward["achievement"]))
            elif "player" in reward:
                for name, amount in reward["player"].items():
                    setattr(
                        context.player,
                        name,
                        getattr(context.player, name) + int(amount),
                    )
            elif "custom" in reward:
                name = str(reward["custom"])
                try:
                    self.hooks.rewards[name](context, reward.get("args", {}))
                except KeyError as exc:
                    raise QuestLoadError(f"unknown custom reward hook {name!r}") from exc
            else:
                raise QuestLoadError(f"unknown reward in quest {quest.quest_id!r}")

    def state_data(self) -> dict[str, object]:
        return {
            "statuses": {
                quest_id: status.value for quest_id, status in self.statuses.items()
            },
            "events": dict(self.events),
            "daily_events": dict(self.daily_events),
            "start_day_conditions": dict(self.start_day_conditions),
            "bedtime_conditions": dict(self.bedtime_conditions),
        }

    def restore_state(self, data: Mapping[str, object]) -> None:
        # Catalogs saved before event counters stored statuses directly.
        statuses = data.get("statuses", data)
        if not isinstance(statuses, Mapping):
            raise QuestLoadError("quest statuses must be an object")
        for quest_id, value in statuses.items():
            if quest_id in self.statuses:
                try:
                    self.statuses[quest_id] = QuestStatus(str(value))
                except ValueError as exc:
                    raise QuestLoadError(
                        f"invalid status {value!r} for quest {quest_id!r}"
                    ) from exc
        events = data.get("events", {})
        if not isinstance(events, Mapping):
            raise QuestLoadError("quest events must be an object")
        self.events = Counter(
            {
                str(name): max(0, int(amount))
                for name, amount in events.items()
            }
        )
        daily_events = data.get("daily_events", {})
        if not isinstance(daily_events, Mapping):
            raise QuestLoadError("daily quest events must be an object")
        self.daily_events = Counter(
            {str(name): max(0, int(amount)) for name, amount in daily_events.items()}
        )
        self.start_day_conditions = self._restore_condition_snapshot(
            data.get("start_day_conditions", {}), "start-day"
        )
        self.bedtime_conditions = self._restore_condition_snapshot(
            data.get("bedtime_conditions", {}), "bedtime"
        )

    @staticmethod
    def _restore_condition_snapshot(value: object, label: str) -> dict[str, float]:
        if not isinstance(value, Mapping):
            raise QuestLoadError(f"{label} quest conditions must be an object")
        return {str(name): float(level) for name, level in value.items()}
