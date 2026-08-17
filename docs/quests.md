# Quest authoring

Quests live in `data/quests.jsonc`. The catalog has a schema version plus
`groups` and `quests` arrays. JSONC permits comments and trailing commas.

Every quest has:

- `id`, `title`, and `description`
- `trigger`: when a locked quest becomes active
- `completion`: when an active quest completes
- `rewards`: zero or more rewards
- optional `group` and `prerequisites`

The player may have any number of active quests. A sequential group implicitly
makes each quest depend on the earlier quests listed in that group. Set
`"sequential": false` to use a group only for organization, or use explicit
`prerequisites` for a branching graph.

## Conditions

Conditions support `all`, `any`, and `not`, which can contain other conditions:

```jsonc
{
  "all": [
    { "day_at_least": 2 },
    {
      "any": [
        { "inventory": { "grains": 3 } },
        { "achievement": "First harvest" },
      ],
    },
  ],
}
```

Built-in leaf conditions are:

- `{"always": true}`
- `{"inventory": {"seed": 2}}` (minimum quantities)
- `{"player": {"has_hoe": true}}` (exact `PlayerState` values)
- `{"achievement": "Found a seed"}`
- `{"day_at_least": 2}`
- `{"quest_completed": "quest_id"}`
- `{"event": {"wheat_planted": 3}}` (persistent action counters)
- `{"event_today": {"wheat_planted": 3}}` (resets at the next dawn)
- `{"start_day_condition": {"fatigue": {"at_most": 25}}}`
- `{"bedtime_condition": {"hunger": {"at_most": 20}}}`
- `{"custom": "hook_name", "args": {...}}`

## Rewards

Built-in rewards add inventory, add an achievement, or add to a numeric player
field:

```jsonc
"rewards": [
  { "inventory": { "seed": 2 } },
  { "achievement": "First harvest" },
  { "player": { "energy": 10 } },
]
```

## Complex quest code

Keep executable code out of JSONC. Register a named Python hook and refer to it
by name from the catalog:

```python
hooks = QuestHooks()

@hooks.condition("field_restored")
def field_restored(context, args):
    return count_healthy_crops(args["field_id"]) >= args["crop_count"]

@hooks.reward("reveal_area")
def reveal_area(context, args):
    reveal(args["area_id"])

manager = QuestManager.from_file(hooks=hooks)
```

```jsonc
"completion": {
  "custom": "field_restored",
  "args": { "field_id": "north_field", "crop_count": 12 },
},
"rewards": [
  { "custom": "reveal_area", "args": { "area_id": "old_orchard" } },
],
```

This keeps quest structure readable and reviewable while allowing arbitrary
game-specific logic in testable Python functions.

Gameplay systems can also advance authored event objectives directly:

```python
quests.record_event("sleep_quality_improved")
quests.record_event("health_recovered", 30)
```

Event totals are saved with quest statuses and survive across day transitions.
