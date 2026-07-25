# Architecture Notes

## High-level direction

The prototype should be built around a small simulation loop with semantic jobs, a routine system, and a shared execution path for both direct control and replay.

## Core concepts

- Job: a semantic action such as harvest field, craft hoe, cook wheat, sleep.
- Routine: an ordered list of jobs recorded from one day.
- Character: an agent that can execute jobs, carry state, and remember routines.
- World: the environment containing locations, stations, resources, and objects.
- Day cycle: each day begins with a morning choice and ends with a reset or replay step.

## Recommended implementation shape

- Keep simulation state in plain data structures.
- Keep rendering separate from game logic.
- Use a single job execution system for both player-driven actions and routine playback.
- Record semantic jobs rather than raw movement commands.
- Keep the first prototype focused on one character and one small map.

## First milestone

Version 0.1 should include:

- one character
- one broken homestead
- gatherable sticks, stones, grass, and wild grain
- crafting a crude hoe
- preparing soil, planting wheat, tending the field, and harvesting
- cooking, eating, and sleeping
- recording a routine and replaying it the next day
