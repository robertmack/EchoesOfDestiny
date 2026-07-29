<<<<<<< HEAD
# Remembering — Python Prototype v0.1

A gray-box Python/Pygame version of the first-day prototype. The project deliberately uses plain shapes and text so the code focuses on reusable programming concepts rather than art assets.

## Related context

- Previous ChatGPT conversation: New Video Game Idea

## What is implemented

- Broken house with a usable broken bed, table, and food prep station
- Broken workshop with workbench and tool storage
- Sticks, stones, tall grass, wild grain, and an old field
- Semantic jobs: gather, craft, till, chop, prepare, plant, whisper, harvest, cook, eat, store/take tool, and sleep
- Automatic movement to a job target
- One remembered routine slot
- Direct-control recording and next-day routine replay
- Left-click inspection of objects and tiles remains available during replay without interrupting the active route or order
- Holding Shift while inspecting reveals applicable persistence-memory counts and nightly chances for terrain, stumps, barrels, remembered water levels, and tool storage
- First Tool, First Harvest, and Homecoming achievements
- Mouse and keyboard support for the morning choice (click, Enter, Space, D, or R)
- Human-editable JSON map data with in-game reload support
- Day 1 begins automatically in Direct Control; later mornings offer Direct Control, replay-and-sleep, replay-and-expand, replay-and-explore, and memory-adjustment choices
- The clock advances one game minute per real second during play, pauses at the morning choice, and resets to 6:00 AM after sleep
- Time multipliers scale the whole simulation, including walking and job timers; work consumes the same game time at every speed
- Pause/Play is independent of the selected time multiplier. The simulation pauses automatically when no movement or work is queued, and issuing a command returns it to Play without changing its multiplier
- Sleeping fades the map to black, restores the level's dawn persistent state at full black, then fades the next-day map in before showing the morning choice

## Editing the map

`data/homestead.jsonc` is the fixed-level authoring definition for the Homestead scenario. It contains terrain, structures, generation rules, and the scenario's authored object instances. Runtime play never writes to it, but it remains editable while refining the level. New fixed levels will use separate level files. Shared object definitions remain in `data/object_types.jsonc`.

`data/current_level.jsonc` is the mutable active-session file. Every application launch rebuilds it from `homestead.jsonc`, so state from an earlier run is discarded. During the session it contains live object instances plus sparse state for modified tiles and remains active across in-game days. Each object instance has a unique numeric ID and two state records: `persistent_state` and `current_state`. Both contain position, orientation, quality, active status, and type-specific state. `E/W` uses the catalog footprint horizontally; `N/S` swaps width and height so a two-tile object spans vertically.

Pressing F5 first reads `homestead.jsonc`. Every authored object ID is added to or updated in `current_level.jsonc`, replacing both its persistent and current state so placement changes are immediately visible. Current-level-only instances whose IDs are absent from Homestead are preserved. The game then reloads the synchronized current level.

Quality is mutable instance data from 1 to 100: 1–20 is ruined, 21–40 damaged, 41–60 worn, 61–79 good, and 80–100 fine. Every object type provides one description for each quality stage, and Object View automatically displays the description matching the selected instance. The `instance_fields` section at the top of `data/object_types.jsonc` documents every supported instance field.

`data/tile_types.jsonc` controls random morning population by tile type. Grassland currently has a 5% pebble chance, 4% wheat chance, 10% tall-grass chance, and 2% berry-bush chance per available tile. Hills increase pebble chance to 12%. Tile and object types can add local `spawn_influence` entries containing `type`, `chance`, `distance`, and `decay`. The first affected tile receives the full boost; each farther tile multiplies it by `decay`, so `1.0` remains constant while `0.5` halves the boost per tile. A day-based seed creates a different layout each morning while an F5 reload during the same day recreates the same layout. Daily-spawned items are omitted from the nightly state file because the next morning replaces them.

The Area Commands panel is a numbered hierarchical menu. Its top level lists Gather, Farm, and Build; click an entry or press its number to open that submenu. The breadcrumb heading shows the current menu, commands are numbered for both mouse and keyboard selection, and Back is always the final entry. Gather contains Pebbles, Branches, Seeds, Tall Grass, and—while carrying an axe—Chop Trees. Farm contains tilling, planting, watering, tending, and harvesting commands as they become available. Tend Crops scans the selected area for growing crops that have not yet been tended and processes them nearest-first up to the quantity limit. Build contains fixed-location construction commands. The number selector defaults to 10 targets; choose the desired quantity, then select a command and drag a rectangle over the world. Matching objects or tiles are queued nearest-first up to that quantity. Tilling is intentionally slow and scales with hoe quality: the crude quality-20 hoe takes about 20 seconds per tile, while a quality-100 hoe still takes 6 seconds. The grassland type declares which tilling fields its instances track and the progression percentages; worked tile instances store their actual values in `current_level.jsonc`. At day-end, a worked tile has a `0.001% × till_count` chance to become permanent soil; otherwise temporary soil returns to grassland while its hidden till-count progress remains. Crops do not yet have a persistence mechanism, so every crop is removed at dawn even when planted on permanent soil. An untended tile has a configurable 10% chance to lose one till count. A crude axe can be crafted at the workbench from one stick, two stone, and one fiber, then used to chop trees into wood. The Chop Trees area command queues trees nearest-first up to the selected quantity and is replayed by rescanning its remembered area. Boulders are persistent blocking objects and add an 18% pebble chance within two tiles, using the same object-driven influence system as tree branches.

Right-clicking a shallow-water or pond tile always shows water collection. With an empty bucket, Gather Water is actionable and the character paths to that tile to fill it. Without an empty bucket, the disabled gray entry reads `Gather Water (empty bucket required)`. Water collection is a terrain context action and does not appear in the Area Commands menu.

The `persistent_state` record contains a `persistent` boolean. Actions modify only `current_state` during the day. At dawn, an instance with `persistent: true` copies its persistent state into current state, so a gathered stone returns in its original place and quality. An instance with `persistent: false` retains its current state and does not respawn. After the dawn reset, `current_level.jsonc` is written again so its current state matches the world actually loaded for the new day.

A chopped tree becomes the stump form of the same tree instance rather than creating a second object. The stump retains the tree's ID, tile, quality, and blocking footprint but has no tree actions. Each night ending in stump form adds one stump-memory count and gives a `0.001% × count` chance for the stump to replace the tree's persistent baseline. Until that succeeds, the tree form returns in the morning so it can be chopped again. A night when the tree is not chopped has the configured decay chance to remove one stump-memory count.

A wooden bucket can be crafted at the workbench from two wood and one fiber. An empty bucket unlocks Gather Water; filling it stores five uses of water, and watering one planted tile consumes one use. The Build > Build Barrel command selects one fixed tile and constructs an immovable 30-use water barrel from the `build_cost` declared by the barrel type in `object_types.jsonc` (currently five wood and two fiber). Bucket water can be poured into a barrel and taken back out. Fill Barrel prompts for tile-aligned source areas: left-click selects one water tile and left-drag selects several possible sources. Holding Ctrl makes every release add another highlighted region; releasing Ctrl finishes the selection and begins the command. Each trip uses the nearest currently reachable pond or shallow-water tile across those separate regions and repeats until the barrel is full; routine memory retains the barrel and source regions rather than individual trips. The Water Crops area command similarly prompts first for a crop area and then for one or more acceptable water-source areas, with the same Ctrl-held additive selection. It waters matching crops up to the selected quantity, automatically refilling its five-use bucket at the nearest reachable selected water tile as needed; memory retains and rescans all selected areas. Gather commands and Chop Trees use the same Ctrl-held selection to combine separate highlighted target regions under one shared quantity limit. Rebuilding a nonpersistent barrel increases its chance to be remembered using the same nightly repetition and decay rates as other memories. After the barrel itself persists, repeatedly ending days with a particular stored-water amount gives that level a chance to become its remembered dawn amount. Wheat grows continuously and takes 10 game-hours to mature without help. Watered plants grow at 3× speed, and tending a plant once per day raises its rate to 1.15×; the effects combine. Watering and tending status reset overnight. Clicking a soil tile shows its plant, exact growth percentage, watered status, and tended status in Selection View. Right-clicking a planted tile offers the currently available Water Crop and Tend Plant actions.

The workbench always lists all four recipes in both its context menu and Selection View. Recipes without the necessary materials remain visible in gray with a materials-required note and cannot be activated. Available recipes can be chosen by clicking their row or pressing the displayed number.

A fiber basket can be woven at the workbench from three fiber. Planting one wheat tile takes 4 seconds without a basket and 1.5 seconds with one. Harvesting mature planted wheat takes 6 seconds without a basket and 2 seconds with one; the basket also speeds gathering seed-bearing wild grain and harvesting berries. Mature planted wheat yields three wheat and leaves its permanent-soil tile ready for another crop.

Carried inventory is cleared when the character sleeps, and non-persistent crafted tools are destroyed. Each Tool Storage instance remembers what has repeatedly been left in that particular spot. At night, a stored tool gives that location one store count and a `0.001% × store_count` chance to remember and recreate that tool; repeated Store/Take actions during one day cannot inflate the count. When that tool is absent at night, the location's count has the same configurable 10% chance to decay by one. Memory progress, recorded quality, and persistence live in the Tool Storage instance's persistent state. A second storage spot therefore develops its own independent memory. Remembered routines are evaluated against the new day's state one step at a time: renewable resources can be gathered again, while actions whose result is already satisfied—such as crafting a tool already remembered in storage—are skipped as unavailable.

Objects also have an editable `description`. Map labels show only the stable object type—such as `Bed` or `Field`—and never expose dynamic state. Selecting an object shows its name, type, description, and available actions in the Object View panel.

Reloading replaces the authored map state and cancels the current movement or job, but preserves the character's inventory, achievements, remembered routine, and current day. If the JSON is invalid, the current map stays in place and an error is shown in the game message bar.

Rooms have a `quality` of `ruined`, `damaged`, `normal`, `fine`, or `great`. Their editable `display_color` is an RGB list containing three values from 0 to 255. Rooms can also define door openings with a wall side, an offset measured from the room's top or left edge, a width, and an optional connected room:

```json
"quality": "damaged",
"display_color": [112, 96, 82],
"doors": [{"side": "right", "offset": 70, "width": 44, "connects_to": "common_room"}]
```

Valid sides are `top`, `bottom`, `left`, and `right`. Connected rooms must contain matching reciprocal door entries. A door without `connects_to` is an exterior entrance. Rooms with `"blocks_movement": true` have solid walls everywhere except these openings.

The `buildings` list contains only identity and name metadata—buildings have no rectangle or collision geometry. Every room references a `building_id`. The house groups the bedroom, kitchen, and common room; the physically separate workshop room belongs to its own workshop building. Room geometry alone determines floors, walls, doors, and building placement.

Movement and semantic jobs use A* pathfinding over this wall geometry. Selecting an object previews its route without moving the character. Door crossings receive centered approach and departure waypoints beyond the character's collision radius, preventing routes from catching on door edges. The displayed route is simplified into collision-safe waypoints, so characters automatically travel through connected doors instead of attempting a direct line through walls.

The authored map is larger than the visible play area. It is currently 128×128 tiles (`4096×4096` runtime pixels). A dead-zone camera keeps the view steady while the character is near the center and scrolls only as the character approaches a viewport edge. UI panels remain fixed, and all map clicks, drawing, collision, and paths use world coordinates through the camera transforms. Map `width` and `height` are tile counts and can be increased in `data/homestead.jsonc` without changing the screen layout.

Move the pointer over the map and use the mouse wheel to zoom from 50% to 200%. Zoom is anchored beneath the pointer, scales world geometry and labels, and leaves the interface panels unchanged.

Hold the middle mouse button over the map and drag to pan the camera. Drag distance respects the current zoom and is clamped at the world boundaries.

Characters move between tile centers in eight directions. Diagonal steps use their true √2 distance, so they take about 1.414 times as long as cardinal steps at the same character Speed stat. A diagonal is blocked unless both adjoining cardinal passages are open, preventing movement through wall corners while allowing doors to remain edge-based.

The bed is positioned at the exact center of the authored map, making the homestead the world origin for play and camera placement. The `tree_generation` block controls deterministic surrounding woodland: `seed`, `count`, clearing radius, density curve, spacing, and tree dimensions are editable. Trees never spawn inside rooms, remain clear of authored objects, block movement, and become more concentrated farther from the homestead.

The `terrain` list defines large editable barriers using tile-coordinate points and colors. The current map is bounded by polygonal mountains in the north, a wavy ocean coastline in the south, an impassable old-growth forest in the west, and a thick meandering river pressed close to the eastern map edge. Two traversable ponds sit within the interior. The workshop now stands independently west of the house. Polygon and river geometry drives both rendering and collision, so changing terrain points in the JSON changes the visible boundary and its playable shape together.

## Tile map

The physical world is a 32-pixel tile grid. Traversable types are `wooden_floor`, `dirt`, `soil`, `grassland`, `shallow_water`, `desert`, `hills`, and `pond`. Non-traversable types are `mountain`, `deep_water`, and `chasm`. Mountain polygons automatically receive a traversable one-tile foothill band along their playable boundary. Each tile contains a mutable property list and mutable `north`, `east`, `south`, and `west` passability edges. Room walls close tile edges, while authored door openings reopen matching edges on both neighboring tiles. Every tile intersected by an object records an `object:<id>` property, so a bed, table, or other large object can occupy several tiles. Objects with `"blocks_movement": true` make all those tiles impassable; loose objects can remain nonblocking.

All authored level geometry uses tile units: map dimensions, terrain points and widths, room positions and dimensions, door offsets and widths, and object instance `x`/`y` positions. The loader converts these values to runtime pixels. Object-type footprint `width` and `height` values in `object_types.jsonc` remain pixel dimensions.

Characters rest and path only on tile centers. Movement is animated continuously between centers. Left-click selects objects but never moves the character. Right-click an object or empty tile and choose **Move To** from its context menu to travel there. Object movement and jobs choose the nearest reachable interaction tile beside the object's complete footprint. Pathfinding moves cardinally across mutually passable edges, so changing a door edge immediately changes which tiles are reachable.

## Windows setup in VS Code

1. Install Python 3.11 or newer from python.org. During installation, enable **Add Python to PATH**.
2. Open this folder in VS Code.
3. Open the VS Code terminal (`Ctrl+backtick`).
4. Create a virtual environment:

```powershell
py -m venv .venv
```

5. Activate it:

```powershell
.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in that terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

6. Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

7. Run the game:

```powershell
$env:PYTHONPATH="src"
py -m remembering
```

You can also open **Run and Debug** in VS Code and choose **Run Remembering Prototype** after selecting the `.venv` interpreter.

## First-day sequence

1. Select **Direct Control**.
2. Gather the stick, stone, tall grass, and wild grain.
3. Click the broken workbench and craft a crude hoe.
4. Click the old field: prepare soil, plant wheat, whisper to it, and harvest.
5. Click the damaged food prep station and cook wheat.
6. Click the broken table and eat.
7. Click the broken bed and sleep.
8. On Day 2, choose one of the replay outcomes: **Replay Memory and Sleep**, **Replay Memory and Expand Routine**, or **Replay Memory and Explore**. **Adjust Memory** opens a list where orders can be reordered, removed, or compatibly replaced before replay.

## Architecture

- `model.py`: dataclasses, enums, routine data, and game state
- `rules.py`: action availability and recipe rules
- `world.py`: initial world construction
- `game.py`: Pygame input, update loop, job execution, and rendering
- `tests/`: small unit tests for rules independent of graphics

## Current limitations

The replay system stores object IDs and semantic actions. One-time resource nodes remain depleted, so replay skips those gathering jobs. This is useful prototype behavior: it demonstrates that routines need future concepts such as resource selection, fallback targets, and job preconditions.
=======
# EchoesOfDestiny
A simple game attempt
>>>>>>> 9a55d1b08e1ba17a2fbfc1fe81522ceb853c0173
