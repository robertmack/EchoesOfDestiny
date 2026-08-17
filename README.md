# EchoesOfDestiny
A simple game attempt

# Remembering — Python Prototype v0.1

A gray-box Python/Pygame version of the first-day prototype. The project deliberately uses plain shapes and text so the code focuses on reusable programming concepts rather than art assets.

## Related context

- Previous ChatGPT conversation: New Video Game Idea

## What is implemented

- Broken house with a usable broken bed, table, and food prep station
- Broken workshop with workbench and tool storage
- Branches, pebbles, tall grass, wild wheat plants, and an old field
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
- The timeline shows `Day X` and the clock together. Pause/Play is independent
  of the selected time multiplier. A single rate box is flanked by minus and
  plus controls; the keyboard `-` and `+` keys select the adjacent rate.
  **1 Cmd** runs one complete queued or remembered command—including its
  movement and batch work—and then pauses before another begins. The simulation
  also pauses automatically when no movement or work is queued.
- Sleeping fades the map to black, restores the level's dawn persistent state at full black, then fades the next-day map in before showing the morning choice

## Editing the map

### Characters and survival conditions

`data/character_types.jsonc` defines reusable character templates. A level
authors character instances in its `characters` array and selects the active
one with `controlled_character_id`. Each instance references the object where
it last slept through `last_sleep_id`; that object supplies dawn fatigue and
the adjacent spawn location. Mutable character state is copied into
`current_level.jsonc` for the active run and is rebuilt from the authored level
on the next application launch.

Live conditions are Trauma, Hunger, Thirst, and Fatigue on a 0–99 burden scale,
where zero is best. They derive Movement Speed, Task Speed, and a signed
Healing Rate. Hunger, Thirst, and Fatigue values from 20 through 80 are neutral;
healthy boundary values heal Trauma while critical values worsen it. Natural water
can be drunk directly, with a bucket making the action more effective, while a
barrel provides portable remembered water. Foods declare signed
`condition_recovery` values; positive values recover a condition and negative
values impose a penalty.

The bed offers one 45-minute Power Nap in each of the 1–5 PM and 5–9 PM
windows. A nap removes Fatigue equal to the bed's current quality. Normal sleep
updates the character's last-sleep reference and begins the nightly condition
memory transition.

`data/homestead.jsonc` is the fixed-level authoring definition for the Homestead scenario. It contains terrain, structures, generation rules, and the scenario's authored object instances. Runtime play never writes to it, but it remains editable while refining the level. New fixed levels will use separate level files. Shared object definitions remain in `data/object_types.jsonc`.

`data/current_level.jsonc` is the mutable active-session file. Every application launch rebuilds it from `homestead.jsonc`, so state from an earlier run is discarded. During the session it contains live object instances plus sparse state for modified tiles and remains active across in-game days. Each object instance has a unique numeric ID and two state records: `persistent_state` and `current_state`. Both contain position, orientation, quality, active status, and type-specific state. Orientations are `N`, `E`, `S`, and `W`; north/south swap a rectangular catalog footprint's width and height. Legacy `E/W` and `N/S` values remain loadable as east and north.

### Object sprite metadata

Object sprites are discovered by the canonical naming convention documented at the
top of `data/object_types.jsonc`; definitions do not contain asset paths. The order is
object type, form, variant, state, then the `_f-<flavor>` suffix. Default and
inapplicable components are omitted, and nonconforming files are ignored. Resolution
tries the specific form/variant asset, then the generic form asset, then the base
object asset; for example, `crop_seed_wheat.png` falls back to `crop_seed.png`.
In the asset tool, **Save Asset** overwrites the currently resolved file, including a
generic fallback. **Create Variant** instead writes the full specific filename, such
as `crop_seed_wheat.png`, so that slot subsequently resolves to its own image.

The resolved PNG may contain a
`remembering.render` text chunk whose JSON declares frame rectangles, projection,
and a normalized anchor. The canvas size comes directly from the PNG. PNG metadata never defines gameplay identity or
behavior. A PNG without the chunk is treated as one full-image `"default"` frame
anchored at `[0.5, 0.5]`. If embedded metadata omits `frames`, the same full-image
`"default"` frame is inferred. A present but empty or malformed `frames` value remains
an asset-loading error.

Small sprites may request a stable per-instance position inside their tile:

```json
"anchor": {
  "mode": "random_within_tile",
  "margin": 0.2
}
```

`margin` is optional and defaults to `0.2` (20% from each tile edge). It must be at
least `0.0` and less than `0.5`. The margin is an exclusion band for the complete
sprite—not merely its anchor—so no visible sprite pixels are placed inside that band.
An oversized sprite is proportionally reduced to fit the remaining inner area. This
affects rendering only; the object remains logically contained by its tile.

Inspect the parsed metadata for a PNG with:

```powershell
$env:PYTHONPATH="src"
py -m remembering.render_metadata src\assets\sprites\objects\tree.png
```

Press F6 while the game is running to clear cached PNG pixels and metadata and reload
edited sprites on the next frame.

### Scenario editor

Launch the map editor from the project root (an optional scenario path may follow the script name):

```powershell
.venv\Scripts\python.exe tools\scenario_editor.py data\homestead.jsonc
```

The editor displays terrain, rooms, and the authored `objects` array. Double-click a palette entry (or use **Add at view center**) to create an object. Drag objects to move them on the tile grid, press **R** to rotate clockwise through `E`, `S`, `W`, and `N`, and press **Delete** to remove the selection. The mouse wheel zooms and middle-drag pans. Save validates IDs, types, quality, orientation, footprints, and map bounds, then replaces only the `objects` array so unrelated JSONC comments and scenario fields remain intact. Undo and redo are available from the toolbar or with Ctrl+Z/Ctrl+Y.

### Object definition editor

Launch the object-definition editor from the project root:

```powershell
.venv\Scripts\python.exe tools\object_definition_editor.py
```

The editor lists every definition in `data/object_types.jsonc` and provides filter,
new, duplicate, delete, validate, explicit save, revert, and reload controls. The
selected definition is edited as strict JSON. Saving validates both the definition
and the complete runtime catalog, then replaces only that object's block so the
file's schema documentation and unrelated JSONC comments remain intact.

### Object asset editor

Launch the object/form asset browser from the project root:

```powershell
.venv\Scripts\python.exe tools\object_asset_editor.py
```

The editor reads `data/object_types.jsonc` and `data/tile_types.jsonc`, derives the
conventional filename for every object/form/variant and tile slot, previews an existing PNG, and shows the parsed
`remembering.render` metadata. The metadata panel is editable; **Save Asset**
explicitly validates the JSON and render schema before writing staged pixel and
metadata edits. **Revert** discards staged edits and reloads the saved PNG.
Invalid edits are reported and do not alter the asset. Choose
**Replace Image from Clipboard** after copying image pixels or an image file.
**Copy Image to Clipboard** exports the selected PNG for pasting into an image
editor. It publishes alpha-preserving PNG data plus a standard bitmap fallback.
**Open in LibreSprite** launches the resolved PNG in
`C:\Users\robma\dev\libreArt\libresprite.exe`. Set
`REMEMBERING_LIBRESPRITE_EXE` before launching the tool to override that location.
After saving in an external editor, **Reload Image from File** rereads the PNG and
embedded metadata from disk. Any unsaved staged edits in the asset tool are discarded.
The **Sprite pixels** width and height fields show the current PNG canvas and can
resize it to exact dimensions; explicit frame rectangles are updated with the image.
The scrollable preview has a 25%-3200% zoom bar and uses nearest-neighbor scaling so
individual sprite pixels remain crisp while inspecting them.
Metadata shortcut buttons add Rotation, Random Placement, Centered Anchor, or Frames
to the editable JSON. **Add All** inserts rotation, random placement, and a default
full-image frame together. Its `"angles": "all"` setting selects a free random angle
from 0° to 360°, consistently per object instance. An explicit angle array can still
restrict rotation to selected quarter turns.
**Make Background Transparent** samples the top-left pixel and clears matching
background pixels connected to the canvas edges. Enclosed details of the same color
are preserved, as are the PNG's text metadata.
Enable **Erase Pixels** and click or drag over the zoomable preview to turn individual
source pixels transparent. **Set Pixel** paints opaque source pixels using the simple
palette or **Select Color** system picker. Ctrl-Z undoes the preceding staged pixel
stroke. Ctrl-C copies the selected sprite image and Ctrl-V replaces it from the
clipboard.
Tile assets use `src/assets/sprites/tiles/<tile-type>.png` and have an expected
64×64-pixel canvas.

Boundary assets appear in the same editor with a canonical horizontal 64×8-pixel
canvas. Their paths are `src/assets/sprites/boundaries/wall.png`, `fence.png`,
`door.png` (closed), and `door_open.png`. The horizontal asset is the canonical
authoring orientation; vertical boundary rendering can rotate it, so no duplicate
vertical editor slots are needed. Clipboard images wider than they are tall remain
horizontal; portrait images are rotated clockwise; square images are treated as
horizontal. The result is centered on an exact transparent 64×8 canvas.

The canonical door sprite has its hinge at the left (west) end and its leaf extends
right (east). An open door reuses this sprite and rotates it 90 degrees
counterclockwise around that hinge. On a north boundary it swings onto the west edge
of the tile above; on a west boundary it swings onto the north edge of the tile to
the right.

Replacement uses the selected form's `footprint × 64` as its maximum size.
Oversized clipboard images shrink proportionally; smaller images remain at native
size without upscaling. If the conventional PNG does not exist, replacement creates
it. Existing PNG text chunks—including `remembering.render`—are preserved.

**Scale Image to Correct Size** applies the same shrink-only rule to the current PNG.
When its canvas changes, explicit metadata frame rectangles are scaled with it.
The editor also displays the selected object's tile footprint and expected pixel
canvas. **Increase Image to Tile Size** proportionally enlarges the artwork to fit
that canvas, centers it with transparent padding when needed, and updates explicit
frame rectangles. This is opt-in so intentionally small sprites remain small.
**Rotate Sprite 90° Clockwise** rotates the PNG toward the E/W authoring convention
and transforms explicit frame rectangles and normalized anchors with the image.
Press F6 in the running game to see either change.

Missing conventional assets remain visible as `[missing sprite]`; rename or create the
expected file before using replacement.

Objects and tiles can also declare state-driven transparent overlays:

```json
"sprite_overlays": [{
  "id": "apples",
  "state_field": "apple_count",
  "value_range": [0, 8],
  "alpha_range": [0, 255]
}]
```

An object overlay is named `<resolved-sprite-stem>_overlay-<id>.png`, with fallback
through the same flavor/state/variant/form chain as the base sprite. For example,
`tree_overlay-apples.png` appears progressively as `apple_count` approaches 8.
A tile overlay is named `<tile-type>_overlay-<id>.png`; the grassland example uses
`grassland_overlay-tilled.png` and `till_percentage`. Overlay PNGs retain their own
per-pixel transparency, which is multiplied by the computed state alpha. A boolean
or other nonnumeric field switches an overlay off/on. Numeric fields use
`value_range`, or may replace its upper bound with an object's quality-adjusted
capacity by declaring `capacity_resource`, as the barrel water example does.

Tile base sprites are loaded from `src/assets/sprites/tiles/<tile-type>.png` when
present, with the catalog display color remaining as the fallback. F6 reloads base
and overlay tile images along with object sprites.

Pressing F5 first reads `homestead.jsonc`. Every authored object ID is added to or updated in `current_level.jsonc`, replacing both its persistent and current state so placement changes are immediately visible. Current-level-only instances whose IDs are absent from Homestead are preserved. The game then reloads the synchronized current level.

Quality is mutable instance data from 1 to 100: 1–20 is ruined, 21–40 damaged, 41–60 worn, 61–79 good, and 80–100 fine. Every object type provides one description for each quality stage, and Object View automatically displays the description matching the selected instance. The `instance_fields` section at the top of `data/object_types.jsonc` documents every supported instance field.

`data/tile_types.jsonc` controls random morning population by tile type. Grassland currently has a 5% pebble chance, 4% wild-plant chance (currently the wheat variant), 10% tall-grass chance, and 2% berry-bush chance per available tile. Hills increase pebble chance to 12%. Tile and object types can add local `spawn_influence` entries containing `type`, `chance`, `distance`, and `decay`. The first affected tile receives the full boost; each farther tile multiplies it by `decay`, so `1.0` remains constant while `0.5` halves the boost per tile. A day-based seed creates a different layout each morning while an F5 reload during the same day recreates the same layout. Daily-spawned items are omitted from the nightly state file because the next morning replaces them.

Harvest Berries is also available as a nearest-first, quantity-limited Gather area
command. Harvesting takes 3 seconds without a basket and 0.5 seconds with one, then
clears the bush's `has_berries` state without removing the bush.

For Gather and Farm area commands, clicking the character instead of dragging a
rectangle chooses the nearest eligible targets across the map, up to the current
quantity. Remembered routines retain this as a nearest-to-character instruction and
rescan from the character's current position on replay. Construction placement and
water-source authorization still require explicit map selections.

Harvesting berries or mature wheat awards one Harvesting XP. Every 10 cumulative XP
grants a Harvesting level, and each level increases harvesting work speed by 5%.
The Stats tab lists every character skill on its own row with level and XP progress.
The fruit cannot be harvested twice, but the remaining bush can still be pulled for
fiber and a branch. Its berries use the state-overlay slot
`bush_berry_overlay-berries.png`.

The Area Commands panel is a numbered hierarchical menu. Its top level lists Gather, Farm, and Build; click an entry or press its number to open that submenu. The breadcrumb heading shows the current menu, commands are numbered for both mouse and keyboard selection, and Back is always the final entry. Gather contains Pebbles, Branches, Seeds, Tall Grass, and—while carrying an axe—Chop Trees. Farm contains tilling, planting, watering, tending, and harvesting commands as they become available. Tilling currently uses a fixed 15 game minutes per action; skill, equipment, and quality modifiers will be added later. The till-area controls allow a time budget in one-hour increments or Until Done. Each till action adds `5.0% × affinity`, and grassland becomes soil at 100%. At night, soil rolls against its remembered-soil percentage; a failed roll returns it to grassland with 80–100% till progress. Crops are object entities contained spatially by soil tiles and begin in the seed form.

Right-clicking a shallow-water or pond tile always shows water collection. With an empty bucket, Gather Water is actionable and the character paths to that tile to fill it. Without an empty bucket, the disabled gray entry reads `Gather Water (empty bucket required)`. Water collection is a terrain context action and does not appear in the Area Commands menu.

The `persistent_state` record contains a `persistent` boolean. Actions modify only `current_state` during the day. At dawn, an instance with `persistent: true` copies its persistent state into current state, so a gathered pebble returns in its original place and quality. An instance with `persistent: false` retains its current state and does not respawn. After the dawn reset, `current_level.jsonc` is written again so its current state matches the world actually loaded for the new day.

A chopped tree becomes the stump form of the same tree instance rather than creating a second object. The stump retains the tree's ID, tile, quality, and blocking footprint but has no tree actions. Each night ending in stump form adds one stump-memory count and gives a `0.1% × count` chance for the stump to replace the tree's persistent baseline. Until that succeeds, the tree form returns in the morning so it can be chopped again. A night when the tree is not chopped has the configured decay chance to remove one stump-memory count.

A wooden bucket can be crafted at the workbench from two wood and one fiber. An empty bucket unlocks Gather Water; filling it stores five uses of water, and watering one planted tile consumes one use. The Build > Build Barrel command selects one fixed tile and constructs an immovable 30-use water barrel from the `build_cost` declared by the barrel type in `object_types.jsonc` (currently five wood and two fiber). Bucket water can be poured into a barrel and taken back out. Fill Barrel prompts for tile-aligned source areas: left-click selects one water tile and left-drag selects several possible sources. Holding Ctrl makes every release add another highlighted region; releasing Ctrl finishes the selection and begins the command. Each trip uses the nearest currently reachable pond or shallow-water tile across those separate regions and repeats until the barrel is full; routine memory retains the barrel and source regions rather than individual trips. The Water Crops area command similarly prompts first for a crop area and then for one or more acceptable water-source areas, with the same Ctrl-held additive selection. It waters matching crops up to the selected quantity, automatically refilling its five-use bucket at the nearest reachable selected water tile as needed; memory retains and rescans all selected areas. Gather commands and Chop Trees use the same Ctrl-held selection to combine separate highlighted target regions under one shared quantity limit. Rebuilding a nonpersistent barrel increases its chance to be remembered using the same nightly repetition and decay rates as other memories. After the barrel itself persists, repeatedly ending days with a particular stored-water amount gives that level a chance to become its remembered dawn amount. Wheat grows continuously and takes 10 game-hours to mature without help. Watered plants grow at 3× speed, and tending a plant once per day raises its rate to 1.15×; the effects combine. Watering and tending status reset overnight. Clicking a soil tile shows its plant, exact growth percentage, watered status, and tended status in Selection View. Right-clicking a planted tile offers the currently available Water Crop and Tend Plant actions.

The workbench always lists all four recipes in both its context menu and Selection View. Recipes without the necessary materials remain visible in gray with a materials-required note and cannot be activated. Available recipes can be chosen by clicking their row or pressing the displayed number.

A fiber basket can be woven at the workbench from three fiber. Planting one wheat tile takes 4 seconds without a basket and 1.5 seconds with one. Harvesting mature planted wheat takes 6 seconds without a basket and 2 seconds with one; the basket also speeds gathering seed-bearing wild wheat plants and harvesting berries. Mature planted wheat yields three wheat and leaves its permanent-soil tile ready for another crop.

Carried inventory is cleared when the character sleeps, and non-persistent crafted tools are destroyed. Each Tool Storage instance remembers what has repeatedly been left in that particular spot. At night, a stored tool gives that location one store count and a `0.1% × store_count` chance to remember and recreate that tool; repeated Store/Take actions during one day cannot inflate the count. When that tool is absent at night, the location's count has the same configurable 10% chance to decay by one. Memory progress, recorded quality, and persistence live in the Tool Storage instance's persistent state. A second storage spot therefore develops its own independent memory. Remembered routines are evaluated against the new day's state one step at a time: renewable resources can be gathered again, while actions whose result is already satisfied—such as crafting a tool already remembered in storage—are skipped as unavailable.

Objects also have an editable `description`. Map labels show only the stable object type—such as `Bed` or `Field`—and never expose dynamic state. Selecting an object shows its name, type, description, and available actions in the Object View panel.

Reloading replaces the authored map state and cancels the current movement or job, but preserves the character's inventory, achievements, remembered routine, and current day. If the JSON is invalid, the current map stays in place and an error is shown in the game message bar.

Rooms have a `quality` of `ruined`, `damaged`, `normal`, `fine`, or `great`. Their editable `display_color` is an RGB list containing three values from 0 to 255. Rooms can also define door openings with a wall side, an offset measured from the room's top or left edge, a width, and an optional connected room:

```json
"quality": "damaged",
"display_color": [112, 96, 82],
"doors": [{"side": "right", "offset": 70, "width": 44, "connects_to": "common_room"}]
```

Valid sides are `top`, `bottom`, `left`, and `right`. Connected rooms must contain matching reciprocal door entries. A door without `connects_to` is an exterior entrance. Rooms with `"blocks_movement": true` have solid walls everywhere except these openings.

Standalone walls, fences, and doors use the optional top-level `boundaries` array. A
boundary occupies the edge between tiles rather than either tile's area:

```json
"boundaries": [
  {"id": "garden_fence_1", "type": "fence", "x": 20, "y": 18, "edge": "east"},
  {"id": "garden_gate", "type": "door", "x": 20, "y": 19, "edge": "east", "open": false}
]
```

Types are `wall`, `fence`, and `door`; edges may be `north`, `east`, `south`, or
`west`. The loader normalizes east and south to the neighboring tile's west and
north edge, so a shared edge has exactly one address and duplicate occupancy is an
error. Walls and fences block crossing. An unlocked door (`"locked": false`, the
default) is passable to route planning even while visually closed; the character
pauses and opens it before crossing. A locked door blocks route planning. Right-click
a nearby door to open or close it. Rendering derives corner junctions from the meeting edges;
corners are not separately authored objects.

The `buildings` list contains only identity and name metadata—buildings have no rectangle or collision geometry. Every room references a `building_id`. The house groups the bedroom, kitchen, and common room; the physically separate workshop room belongs to its own workshop building. Room geometry alone determines floors, walls, doors, and building placement.

Movement and semantic jobs use A* pathfinding over this wall geometry. Selecting an object previews its route without moving the character. Door crossings receive centered approach and departure waypoints beyond the character's collision radius, preventing routes from catching on door edges. The displayed route is simplified into collision-safe waypoints, so characters automatically travel through connected doors instead of attempting a direct line through walls.

The authored map is larger than the visible play area. It is currently 128×128 tiles (`8192×8192` logical subtile units). A dead-zone camera keeps the view steady while the character is near the center and scrolls only as the character approaches a viewport edge. UI panels remain fixed, and map rendering crosses into screen coordinates through the camera transforms. Map `width` and `height` are tile counts and can be increased in `data/homestead.jsonc` without changing the screen layout.

Gameplay positions use resolution-independent `tilexy` plus `subtilexy`.
`tilexy` is the integer map tile; `subtilexy` is a continuous logical position
inside it with 64 units per tile and canonical values from 0 (inclusive) to 64
(exclusive). The exact tile center is `(32, 32)`. Crossing a subtile boundary
normalizes into the adjacent tile. Rendering converts gameplay map positions to
`screenxy` through the camera, while PNG-local coordinates remain separate
`assetxy`. Memory files store coordinate fields explicitly using `tilexy` and
`subtilexy`; older tile-array memories remain readable.

Move the pointer over the map and use the mouse wheel to zoom from 25% to 100%
native scale. Zoom is anchored beneath the pointer, scales world geometry and
labels, and leaves the interface panels unchanged.

Hold the middle mouse button over the map and drag to pan the camera. Drag distance respects the current zoom and is clamped at the world boundaries.

Characters move between tile centers in eight directions. Diagonal steps use their true √2 distance, so they take about 1.414 times as long as cardinal steps at the same character Speed stat. A diagonal is blocked unless both adjoining cardinal passages are open, preventing movement through wall corners while allowing doors to remain edge-based.

The bed is positioned at the exact center of the authored map, making the homestead the world origin for play and camera placement. The `tree_generation` block controls deterministic surrounding woodland: `seed`, `count`, clearing radius, density curve, spacing, and tree dimensions are editable. Trees never spawn inside rooms, remain clear of authored objects, block movement, and become more concentrated farther from the homestead.

The `terrain` list defines large editable barriers using tile-coordinate points and colors. The current map is bounded by polygonal mountains in the north, a wavy ocean coastline in the south, an impassable old-growth forest in the west, and a thick meandering river pressed close to the eastern map edge. Two traversable ponds sit within the interior. The workshop now stands independently west of the house. Polygon and river geometry drives both rendering and collision, so changing terrain points in the JSON changes the visible boundary and its playable shape together.

## Tile map

The physical world is a 64-pixel tile grid displayed in a 640×640 map viewport.
At the default 100% native zoom, exactly 10×10 tiles are visible and one tile PNG
pixel equals one screen pixel. Zooming out shows more of the map. Traversable types are `wooden_floor`, `dirt`, `soil`, `grassland`, `shallow_water`, `desert`, `hills`, and `pond`. Non-traversable types are `mountain`, `deep_water`, and `chasm`. Mountain polygons automatically receive a traversable one-tile foothill band along their playable boundary. Each tile contains a mutable property list and mutable `north`, `east`, `south`, and `west` passability edges. Room walls close tile edges, while authored door openings reopen matching edges on both neighboring tiles. Every tile intersected by an object records an `object:<id>` property, so a bed, table, or other large object can occupy several tiles. Objects with `"blocks_movement": true` make all those tiles impassable; loose objects can remain nonblocking.

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
2. Gather the branch, pebble, tall grass, and wild wheat.
3. Click the broken workbench and craft a crude hoe.
4. Click the old field: prepare soil, plant wheat, whisper to it, and harvest.
5. Click the damaged food prep station and cook wheat.
6. Click the broken table and eat.
7. Click the broken bed and sleep.
8. On Day 2, choose one of the replay outcomes: **Replay Memory and Sleep**, **Replay Memory and Expand Routine**, or **Replay Memory and Explore**. Press **C** during play to open the paused Command Set Editor. It can edit, reorder, duplicate, add, and remove commands; save or load named `.jsonc` command sets from `data/memories`; and launch the open set immediately with **Run Now**. **Save Homestead** writes `homestead.jsonc` with one click. The secret **A** shortcut loads that file and automatically selects Replay Memory and Sleep until Escape is pressed.

## Architecture

- `model.py`: dataclasses, enums, routine data, and game state
- `rules.py`: action availability and recipe rules
- `world.py`: initial world construction
- `game.py`: Pygame input, update loop, job execution, and rendering
- `tests/`: small unit tests for rules independent of graphics

## Current limitations

The replay system stores object IDs and semantic actions. One-time resource nodes remain depleted, so replay skips those gathering jobs. This is useful prototype behavior: it demonstrates that routines need future concepts such as resource selection, fallback targets, and job preconditions.
