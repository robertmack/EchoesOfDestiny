import json

import pytest

from remembering.model import DayState, PlayerState
from remembering.quests import (
    DEFAULT_QUESTS_PATH,
    QuestHooks,
    QuestLoadError,
    QuestManager,
    QuestStatus,
    load_quest_catalog,
)


def write_catalog(tmp_path, quests, groups=()):
    path = tmp_path / "quests.jsonc"
    path.write_text(
        json.dumps({"_schema_version": 1, "groups": groups, "quests": quests}),
        encoding="utf-8",
    )
    return path


def quest(quest_id, completion, **extra):
    return {
        "id": quest_id,
        "title": quest_id.title(),
        "description": f"Complete {quest_id}.",
        "trigger": {"always": True},
        "completion": completion,
        **extra,
    }


def test_multiple_quests_can_be_active_and_rewarded(tmp_path):
    path = write_catalog(
        tmp_path,
        [
            quest("seeds", {"inventory": {"seed": 2}}, rewards=[{"inventory": {"grains": 1}}]),
            quest("hoe", {"player": {"has_hoe": True}}, rewards=[{"achievement": "Prepared"}]),
        ],
    )
    manager = QuestManager.from_file(path)
    player = PlayerState()
    day = DayState()

    manager.update(player, day)
    assert {item.quest_id for item in manager.active_quests} == {"seeds", "hoe"}

    player.inventory["seed"] = 2
    player.has_hoe = True
    messages = manager.update(player, day)
    assert manager.status("seeds") is QuestStatus.COMPLETED
    assert manager.status("hoe") is QuestStatus.COMPLETED
    assert player.inventory["grains"] == 1
    assert "Prepared" in player.achievements
    assert len([message for message in messages if "completed" in message]) == 2


def test_sequential_group_links_quests(tmp_path):
    groups = [
        {
            "id": "arc",
            "title": "Arc",
            "description": "",
            "sequential": True,
            "quests": ["one", "two"],
        }
    ]
    manager = QuestManager.from_file(
        write_catalog(
            tmp_path,
            [
                quest("one", {"inventory": {"seed": 1}}, group="arc"),
                quest("two", {"inventory": {"grains": 1}}, group="arc"),
            ],
            groups,
        )
    )
    player = PlayerState()
    manager.update(player, DayState())
    assert manager.status("one") is QuestStatus.ACTIVE
    assert manager.status("two") is QuestStatus.LOCKED
    player.inventory["seed"] = 1
    manager.update(player, DayState())
    assert manager.status("two") is QuestStatus.ACTIVE


def test_named_custom_hooks_handle_complex_code(tmp_path):
    hooks = QuestHooks()

    @hooks.condition("well_restored")
    def well_restored(context, args):
        return context.player.conditions["fatigue"] <= args["maximum_fatigue"] and context.day.number >= 2

    @hooks.reward("restore_hunger")
    def restore_hunger(context, args):
        context.player.conditions["hunger"] = args["value"]

    manager = QuestManager.from_file(
        write_catalog(
            tmp_path,
            [
                quest(
                    "custom",
                    {"custom": "well_restored", "args": {"maximum_fatigue": 20}},
                    rewards=[{"custom": "restore_hunger", "args": {"value": 0}}],
                )
            ],
        ),
        hooks,
    )
    player = PlayerState()
    player.conditions["fatigue"] = 10
    manager.update(player, DayState(number=2))
    assert manager.status("custom") is QuestStatus.COMPLETED
    assert player.conditions["hunger"] == 0


def test_state_round_trip_ignores_removed_quests(tmp_path):
    manager = QuestManager.from_file(
        write_catalog(tmp_path, [quest("one", {"always": True})])
    )
    manager.restore_state({"one": "active", "old_quest": "completed"})
    restored = QuestManager(manager.quests, manager.groups)
    restored.restore_state(manager.state_data())
    assert restored.status("one") is QuestStatus.ACTIVE


def test_event_progress_is_counted_and_persisted(tmp_path):
    manager = QuestManager.from_file(
        write_catalog(tmp_path, [quest("plant", {"event": {"planted": 3}})])
    )
    player = PlayerState()
    manager.update(player, DayState())
    manager.record_event("planted", 2)
    manager.update(player, DayState())
    assert manager.status("plant") is QuestStatus.ACTIVE

    restored = QuestManager(manager.quests, manager.groups)
    restored.restore_state(manager.state_data())
    restored.record_event("planted")
    restored.update(player, DayState())
    assert restored.status("plant") is QuestStatus.COMPLETED


def test_daily_events_and_dawn_and_bedtime_snapshots():
    path_quests = {
        item.quest_id: item
        for item in QuestManager.from_file(DEFAULT_QUESTS_PATH).quests.values()
    }
    manager = QuestManager(
        {
            "daily": path_quests["plant_wheat"].__class__(
                "daily",
                "Daily",
                "Plant today.",
                {"always": True},
                {"event_today": {"planted": 2}},
            ),
            "dawn": path_quests["plant_wheat"].__class__(
                "dawn",
                "Dawn",
                "Wake rested.",
                {"always": True},
                {"start_day_condition": {"fatigue": {"at_most": 20}}},
            ),
            "bed": path_quests["plant_wheat"].__class__(
                "bed",
                "Bed",
                "Go to bed fed.",
                {"always": True},
                {"bedtime_condition": {"hunger": {"at_most": 10}}},
            ),
        }
    )
    player = PlayerState()
    manager.record_start_day({"fatigue": 15, "hunger": 50})
    manager.record_event("planted", 2)
    manager.record_bedtime({"fatigue": 80, "hunger": 10})
    manager.update(player, DayState())
    assert all(status is QuestStatus.COMPLETED for status in manager.statuses.values())

    manager.record_start_day({"fatigue": 50})
    assert manager.daily_events["planted"] == 0


def test_catalog_rejects_broken_group_link(tmp_path):
    path = write_catalog(
        tmp_path,
        [quest("one", {"always": True})],
        [{"id": "arc", "title": "Arc", "quests": ["missing"]}],
    )
    with pytest.raises(QuestLoadError, match="unknown quest"):
        load_quest_catalog(path)


def test_survival_line_branches_into_three_active_mini_quests():
    manager = QuestManager.from_file(DEFAULT_QUESTS_PATH)
    player = PlayerState()
    manager.update(player, DayState())
    assert manager.status("survive_the_day") is QuestStatus.ACTIVE

    manager.record_event("days_survived")
    manager.update(player, DayState())

    assert manager.status("survive_the_day") is QuestStatus.COMPLETED
    assert {
        "improve_sleep_quality",
        "secure_food",
        "restore_health",
    }.issubset({quest.quest_id for quest in manager.active_quests})
