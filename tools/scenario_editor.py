from __future__ import annotations

import argparse
import copy
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image, ImageTk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from remembering.jsonc import loads_jsonc  # noqa: E402


DEFAULT_SCENARIO = PROJECT_ROOT / "data" / "homestead.jsonc"
OBJECT_TYPES = PROJECT_ROOT / "data" / "object_types.jsonc"
SPRITE_ROOT = PROJECT_ROOT / "src" / "assets" / "sprites" / "objects"
ORIENTATION_CYCLE = ("E", "S", "W", "N")


def normalize_orientation(value: object) -> str:
    aliases = {"E/W": "E", "N/S": "N", "EAST": "E", "SOUTH": "S", "WEST": "W", "NORTH": "N"}
    return aliases.get(str(value).upper(), str(value).upper())


def _skip_jsonc(source: str, index: int) -> int:
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated block comment")
            index = end + 2
        else:
            break
    return index


def _matching_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(source):
        character = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated block comment")
            index = end + 1
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError(f"Unterminated {opening}{closing} structure")


def top_level_array_bounds(source: str, key_name: str) -> tuple[int, int]:
    """Return the brackets around a named top-level JSONC array."""
    index = _skip_jsonc(source, 0)
    if index >= len(source) or source[index] != "{":
        raise ValueError("Scenario must be a JSON object")
    root_end = _matching_delimiter(source, index, "{", "}")
    index += 1
    while index < root_end:
        index = _skip_jsonc(source, index)
        if index < root_end and source[index] == ",":
            index += 1
            continue
        if index >= root_end:
            break
        if source[index] != '"':
            raise ValueError(f"Expected a scenario field near character {index}")
        key_end = index + 1
        escaped = False
        while key_end < root_end:
            if escaped:
                escaped = False
            elif source[key_end] == "\\":
                escaped = True
            elif source[key_end] == '"':
                break
            key_end += 1
        key = json.loads(source[index : key_end + 1])
        colon = _skip_jsonc(source, key_end + 1)
        if colon >= root_end or source[colon] != ":":
            raise ValueError(f"Expected ':' after scenario field {key!r}")
        value = _skip_jsonc(source, colon + 1)
        if key == key_name:
            if value >= root_end or source[value] != "[":
                raise ValueError(f"Scenario field {key_name!r} must be an array")
            return value, _matching_delimiter(source, value, "[", "]")
        if source[value] == "{":
            index = _matching_delimiter(source, value, "{", "}") + 1
        elif source[value] == "[":
            index = _matching_delimiter(source, value, "[", "]") + 1
        elif source[value] == '"':
            index = value + 1
            escaped = False
            while index < root_end:
                if escaped:
                    escaped = False
                elif source[index] == "\\":
                    escaped = True
                elif source[index] == '"':
                    index += 1
                    break
                index += 1
        else:
            comma = source.find(",", value, root_end)
            index = root_end if comma < 0 else comma
    raise ValueError(f"Scenario has no {key_name!r} array")


def replace_objects_array(source: str, objects: list[dict[str, Any]]) -> str:
    start, end = top_level_array_bounds(source, "objects")
    line_start = source.rfind("\n", 0, start) + 1
    line = source[line_start:start]
    field_indent = line[: len(line) - len(line.lstrip())]
    if objects:
        body = json.dumps(objects, indent=4, ensure_ascii=False)
        lines = body.splitlines()
        rendered = lines[0] + "".join(f"\n{field_indent}{line}" for line in lines[1:])
    else:
        rendered = "[]"
    return source[:start] + rendered + source[end + 1 :]


def replace_boundaries_array(source: str, boundaries: list[dict[str, Any]]) -> str:
    start, end = top_level_array_bounds(source, "boundaries")
    line_start = source.rfind("\n", 0, start) + 1
    line = source[line_start:start]
    field_indent = line[: len(line) - len(line.lstrip())]
    body = json.dumps(boundaries, indent=4, ensure_ascii=False)
    lines = body.splitlines()
    rendered = lines[0] + "".join(f"\n{field_indent}{line}" for line in lines[1:])
    return source[:start] + rendered + source[end + 1 :]


def flip_door_swing(boundary: dict[str, Any]) -> None:
    """Reverse which way a door leaf swings, defaulting legacy doors to CCW."""
    current = str(boundary.get("swing", "counterclockwise")).lower()
    boundary["swing"] = "counterclockwise" if current == "clockwise" else "clockwise"


def validate_scenario_objects(
    document: dict[str, Any], definitions: dict[str, dict[str, Any]]
) -> None:
    width = document.get("width")
    height = document.get("height")
    objects = document.get("objects")
    if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
        raise ValueError("Scenario width and height must be positive integers")
    if not isinstance(objects, list):
        raise ValueError("Scenario objects must be an array")
    seen: set[int] = set()
    for index, obj in enumerate(objects):
        label = f"objects[{index}]"
        if not isinstance(obj, dict):
            raise ValueError(f"{label} must be an object")
        object_id = obj.get("id")
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id <= 0:
            raise ValueError(f"{label}.id must be a positive integer")
        if object_id in seen:
            raise ValueError(f"Duplicate object ID {object_id}")
        seen.add(object_id)
        type_id = obj.get("type")
        if not isinstance(type_id, str) or type_id not in definitions:
            raise ValueError(f"{label}.type is unknown: {type_id!r}")
        orientation = normalize_orientation(obj.get("orientation", "E"))
        if orientation not in ORIENTATION_CYCLE:
            raise ValueError(f"{label}.orientation must be N, E, S, or W")
        quality = obj.get("quality", 100)
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise ValueError(f"{label}.quality must be an integer from 1 to 100")
        footprint = definitions[type_id].get("footprint", [1, 1])
        fw, fh = int(footprint[0]), int(footprint[1])
        if orientation in {"N", "S"}:
            fw, fh = fh, fw
        x, y = obj.get("x"), obj.get("y")
        if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
            raise ValueError(f"{label}.x and .y must be integers")
        if x < 0 or y < 0 or x + fw > width or y + fh > height:
            raise ValueError(f"{label} falls outside the {width} x {height} map")


def next_object_id(objects: list[dict[str, Any]]) -> int:
    return max((int(obj.get("id", 0)) for obj in objects), default=0) + 1


class ScenarioEditor(tk.Tk):
    def __init__(self, path: Path = DEFAULT_SCENARIO) -> None:
        super().__init__()
        self.path = path.resolve()
        self.source = ""
        self.document: dict[str, Any] = {}
        self.objects: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.definitions = self._load_definitions()
        self.selected_id: int | None = None
        self.selected_boundary_id: str | None = None
        self.zoom = 10.0
        self.drag_offset = (0.0, 0.0)
        self.drag_before: list[dict[str, Any]] | None = None
        self.history: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.future: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.images: list[ImageTk.PhotoImage] = []
        self.title("Remembering — Scenario Editor")
        self.geometry("1320x850")
        self.minsize(900, 600)
        self._build_ui()
        self.reload_file()

    def _load_definitions(self) -> dict[str, dict[str, Any]]:
        data = loads_jsonc(OBJECT_TYPES.read_text(encoding="utf-8"))
        defaults = data.get("_defaults", {})
        return {
            entry["id"]: {**defaults, **entry}
            for entry in data.get("object_types", [])
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=7)
        toolbar.pack(fill=tk.X)
        for label, command in (
            ("Open…", self.open_file), ("Save", self.save_file), ("Reload", self.reload_file),
            ("Undo", self.undo), ("Redo", self.redo), ("Rotate", self.rotate_selected),
            ("Flip", self.flip_selected),
            ("Delete", self.delete_selected), ("Fit Map", self.fit_map),
        ):
            ttk.Button(toolbar, text=label, command=command).pack(side=tk.LEFT, padx=2)
        ttk.Label(toolbar, text="  Wheel: zoom   Middle-drag: pan   Delete: remove   R: rotate   F: flip door").pack(side=tk.LEFT)

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        sidebar = ttk.Frame(body, padding=(7, 0, 4, 7))
        map_frame = ttk.Frame(body, padding=(4, 0, 7, 7))
        body.add(sidebar, weight=1)
        body.add(map_frame, weight=5)

        ttk.Label(sidebar, text="Object palette").pack(anchor=tk.W)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh_palette())
        ttk.Entry(sidebar, textvariable=self.filter_var).pack(fill=tk.X, pady=(3, 5))
        list_frame = ttk.Frame(sidebar)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.palette = tk.Listbox(list_frame, exportselection=False)
        scroll = ttk.Scrollbar(list_frame, command=self.palette.yview)
        self.palette.configure(yscrollcommand=scroll.set)
        self.palette.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.palette.bind("<Double-Button-1>", self.add_selected_type)
        ttk.Button(sidebar, text="Add at view center", command=self.add_selected_type).pack(fill=tk.X, pady=5)
        ttk.Separator(sidebar).pack(fill=tk.X, pady=7)
        self.selection_var = tk.StringVar(value="No object selected")
        ttk.Label(sidebar, textvariable=self.selection_var, wraplength=215).pack(anchor=tk.W)
        form = ttk.Frame(sidebar)
        form.pack(fill=tk.X, pady=5)
        ttk.Label(form, text="Quality").grid(row=0, column=0, sticky="w")
        self.quality_var = tk.IntVar(value=100)
        quality = ttk.Spinbox(form, from_=1, to=100, textvariable=self.quality_var, width=7, command=self.apply_quality)
        quality.grid(row=0, column=1, sticky="e")
        quality.bind("<Return>", self.apply_quality)
        form.columnconfigure(1, weight=1)

        self.canvas = tk.Canvas(map_frame, background="#20262b", highlightthickness=0)
        xbar = ttk.Scrollbar(map_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        ybar = ttk.Scrollbar(map_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        map_frame.rowconfigure(0, weight=1)
        map_frame.columnconfigure(0, weight=1)
        self.canvas.bind("<Button-1>", self.begin_drag)
        self.canvas.bind("<B1-Motion>", self.drag_object)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.canvas.bind("<ButtonPress-2>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<MouseWheel>", self.mouse_wheel)
        self.bind("<Delete>", lambda _e: self.delete_selected())
        self.bind("<Key-r>", lambda _e: self.rotate_selected())
        self.bind("<Key-f>", lambda _e: self.flip_selected())
        self.bind("<Control-s>", lambda _e: self.save_file())
        self.bind("<Control-z>", lambda _e: self.undo())
        self.bind("<Control-y>", lambda _e: self.redo())
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, padding=(8, 3)).pack(fill=tk.X)
        self.refresh_palette()

    def visible_types(self) -> list[str]:
        query = self.filter_var.get().strip().lower()
        return [type_id for type_id, definition in self.definitions.items() if not query or query in type_id.lower() or query in str(definition.get("name", "")).lower()]

    def refresh_palette(self) -> None:
        self.palette.delete(0, tk.END)
        for type_id in self.visible_types():
            self.palette.insert(tk.END, f"{self.definitions[type_id].get('name') or type_id}  [{type_id}]")

    def open_file(self) -> None:
        chosen = filedialog.askopenfilename(parent=self, initialdir=self.path.parent, filetypes=[("Scenario JSONC", "*.jsonc"), ("JSON", "*.json"), ("All files", "*.*")])
        if chosen:
            self.path = Path(chosen).resolve()
            self.reload_file()

    def reload_file(self) -> None:
        try:
            source = self.path.read_text(encoding="utf-8")
            document = loads_jsonc(source)
            validate_scenario_objects(document, self.definitions)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Cannot load scenario", str(exc), parent=self)
            return
        self.source, self.document = source, document
        self.objects = copy.deepcopy(document["objects"])
        self.boundaries = copy.deepcopy(document.get("boundaries", []))
        self.selected_id = None
        self.selected_boundary_id = None
        self.history.clear()
        self.future.clear()
        self.redraw()
        self.after_idle(self.fit_map)
        self.status_var.set(f"Loaded {len(self.objects)} authored objects from {self.path}")

    def checkpoint(self, prior: list[dict[str, Any]] | None = None) -> None:
        self.history.append(
            (
                copy.deepcopy(self.objects if prior is None else prior),
                copy.deepcopy(self.boundaries),
            )
        )
        self.future.clear()

    def undo(self) -> None:
        if not self.history:
            return
        self.future.append((copy.deepcopy(self.objects), copy.deepcopy(self.boundaries)))
        self.objects, self.boundaries = self.history.pop()
        if self.selected_id not in {obj["id"] for obj in self.objects}:
            self.selected_id = None
        self.redraw()

    def redo(self) -> None:
        if not self.future:
            return
        self.history.append((copy.deepcopy(self.objects), copy.deepcopy(self.boundaries)))
        self.objects, self.boundaries = self.future.pop()
        self.redraw()

    def save_file(self) -> None:
        try:
            candidate = replace_objects_array(self.source, self.objects)
            if "boundaries" in self.document:
                candidate = replace_boundaries_array(candidate, self.boundaries)
            document = loads_jsonc(candidate)
            validate_scenario_objects(document, self.definitions)
            self.path.write_text(candidate, encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Could not save scenario", str(exc), parent=self)
            return
        self.source, self.document = candidate, document
        self.status_var.set(f"Saved {len(self.objects)} objects to {self.path.name}")

    def world_xy(self, event: tk.Event) -> tuple[float, float]:
        return self.canvas.canvasx(event.x) / self.zoom, self.canvas.canvasy(event.y) / self.zoom

    def object_at(self, x: float, y: float) -> dict[str, Any] | None:
        for obj in reversed(self.objects):
            width, height = self.footprint(obj)
            if obj["x"] <= x < obj["x"] + width and obj["y"] <= y < obj["y"] + height:
                return obj
        return None

    def boundary_at(self, x: float, y: float) -> dict[str, Any] | None:
        tolerance = max(0.15, 4.0 / self.zoom)
        for boundary in reversed(self.boundaries):
            bx, by = float(boundary["x"]), float(boundary["y"])
            edge = str(boundary.get("edge", "north")).lower()
            if edge == "east":
                bx += 1
                edge = "west"
            elif edge == "south":
                by += 1
                edge = "north"
            if edge == "west" and abs(x - bx) <= tolerance and by <= y <= by + 1:
                return boundary
            if edge == "north" and abs(y - by) <= tolerance and bx <= x <= bx + 1:
                return boundary
        return None

    def footprint(self, obj: dict[str, Any]) -> tuple[int, int]:
        raw = self.definitions[obj["type"]].get("footprint", [1, 1])
        width, height = int(raw[0]), int(raw[1])
        return (height, width) if normalize_orientation(obj.get("orientation", "E")) in {"N", "S"} else (width, height)

    def begin_drag(self, event: tk.Event) -> None:
        x, y = self.world_xy(event)
        obj = self.object_at(x, y)
        boundary = None if obj is not None else self.boundary_at(x, y)
        self.selected_id = obj["id"] if obj else None
        self.selected_boundary_id = boundary["id"] if boundary else None
        self.drag_before = copy.deepcopy(self.objects) if obj else None
        if obj:
            self.drag_offset = (x - obj["x"], y - obj["y"])
        self.redraw()

    def drag_object(self, event: tk.Event) -> None:
        obj = self.selected_object()
        if obj is None or self.drag_before is None:
            return
        x, y = self.world_xy(event)
        width, height = self.footprint(obj)
        obj["x"] = max(0, min(int(round(x - self.drag_offset[0])), int(self.document["width"]) - width))
        obj["y"] = max(0, min(int(round(y - self.drag_offset[1])), int(self.document["height"]) - height))
        self.redraw()

    def end_drag(self, _event: tk.Event) -> None:
        if self.drag_before is not None and self.drag_before != self.objects:
            self.checkpoint(self.drag_before)
        self.drag_before = None

    def selected_object(self) -> dict[str, Any] | None:
        return next((obj for obj in self.objects if obj["id"] == self.selected_id), None)

    def selected_boundary(self) -> dict[str, Any] | None:
        return next(
            (
                boundary
                for boundary in self.boundaries
                if boundary.get("id") == self.selected_boundary_id
            ),
            None,
        )

    def add_selected_type(self, _event: object | None = None) -> None:
        selection = self.palette.curselection()
        if not selection:
            return
        type_id = self.visible_types()[selection[0]]
        center_x = self.canvas.canvasx(self.canvas.winfo_width() / 2) / self.zoom
        center_y = self.canvas.canvasy(self.canvas.winfo_height() / 2) / self.zoom
        definition = self.definitions[type_id]
        width, height = (int(v) for v in definition.get("footprint", [1, 1]))
        obj = {"id": next_object_id(self.objects), "type": type_id, "x": max(0, min(round(center_x - width / 2), int(self.document["width"]) - width)), "y": max(0, min(round(center_y - height / 2), int(self.document["height"]) - height)), "orientation": "E", "quality": 100}
        self.checkpoint()
        self.objects.append(obj)
        self.selected_id = obj["id"]
        self.selected_boundary_id = None
        self.redraw()

    def delete_selected(self) -> None:
        if self.selected_object() is None:
            return
        self.checkpoint()
        deleted = self.selected_id
        self.objects = [obj for obj in self.objects if obj["id"] != deleted]
        self.selected_id = None
        self.redraw()
        self.status_var.set(f"Deleted object {deleted} (unsaved)")

    def rotate_selected(self) -> None:
        obj = self.selected_object()
        if obj is None:
            return
        self.checkpoint()
        current = normalize_orientation(obj.get("orientation", "E"))
        obj["orientation"] = ORIENTATION_CYCLE[(ORIENTATION_CYCLE.index(current) + 1) % 4]
        width, height = self.footprint(obj)
        obj["x"] = min(obj["x"], int(self.document["width"]) - width)
        obj["y"] = min(obj["y"], int(self.document["height"]) - height)
        self.redraw()

    def flip_selected(self) -> None:
        boundary = self.selected_boundary()
        if boundary is None or boundary.get("type") != "door":
            self.status_var.set("Select a door boundary to flip its swing direction")
            return
        self.checkpoint()
        flip_door_swing(boundary)
        self.redraw()
        self.status_var.set(
            f"Door {boundary['id']} swings {boundary['swing']} (unsaved)"
        )

    def apply_quality(self, _event: object | None = None) -> None:
        obj = self.selected_object()
        if obj is None:
            return
        value = max(1, min(100, int(self.quality_var.get())))
        if value != obj.get("quality", 100):
            self.checkpoint()
            obj["quality"] = value
            self.redraw()

    def mouse_wheel(self, event: tk.Event) -> None:
        old = self.zoom
        self.zoom = max(3.0, min(64.0, self.zoom * (1.18 if event.delta > 0 else 1 / 1.18)))
        if self.zoom != old:
            wx, wy = self.canvas.canvasx(event.x) / old, self.canvas.canvasy(event.y) / old
            self.redraw()
            self.canvas.xview_moveto(max(0.0, (wx * self.zoom - event.x) / max(1, int(self.document["width"]) * self.zoom)))
            self.canvas.yview_moveto(max(0.0, (wy * self.zoom - event.y) / max(1, int(self.document["height"]) * self.zoom)))

    def fit_map(self) -> None:
        if not self.document:
            return
        width, height = int(self.document["width"]), int(self.document["height"])
        self.zoom = max(3.0, min(32.0, min(max(1, self.canvas.winfo_width() - 4) / width, max(1, self.canvas.winfo_height() - 4) / height)))
        self.redraw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def sprite_path(self, obj: dict[str, Any]) -> Path | None:
        candidates = []
        if obj.get("variant"):
            candidates.append(SPRITE_ROOT / f"{obj['type']}_{obj['variant']}.png")
        candidates.append(SPRITE_ROOT / f"{obj['type']}.png")
        return next((path for path in candidates if path.is_file()), None)

    def redraw(self) -> None:
        self.canvas.delete("all")
        self.images.clear()
        if not self.document:
            return
        z = self.zoom
        map_w, map_h = int(self.document["width"]), int(self.document["height"])
        self.canvas.configure(scrollregion=(0, 0, map_w * z, map_h * z))
        self.canvas.create_rectangle(0, 0, map_w * z, map_h * z, fill="#71915a", outline="#b8c9aa")
        for feature in self.document.get("terrain", []):
            points = [coordinate * z for point in feature.get("points", []) for coordinate in point]
            color = "#%02x%02x%02x" % tuple(feature.get("display_color", [70, 90, 70]))
            if feature.get("kind") == "river":
                self.canvas.create_line(*points, fill=color, width=max(1, float(feature.get("width", 1)) * z), smooth=True)
            elif len(points) >= 6:
                self.canvas.create_polygon(*points, fill=color, outline="#313b35")
        for room in self.document.get("structures", []):
            x, y = room["x"] * z, room["y"] * z
            w, h = room["width"] * z, room["height"] * z
            color = "#%02x%02x%02x" % tuple(room.get("display_color", [110, 95, 80]))
            self.canvas.create_rectangle(x, y, x + w, y + h, fill=color, outline="#332b25", width=max(1, z / 5))
        if z >= 8:
            for column in range(map_w + 1):
                self.canvas.create_line(column * z, 0, column * z, map_h * z, fill="#ffffff", stipple="gray12")
            for row in range(map_h + 1):
                self.canvas.create_line(0, row * z, map_w * z, row * z, fill="#ffffff", stipple="gray12")
        boundary_colors = {"wall": "#3a3028", "fence": "#7e5630", "door": "#d28a38"}
        for boundary in self.boundaries:
            x, y = float(boundary["x"]), float(boundary["y"])
            edge = str(boundary.get("edge", "north")).lower()
            if edge == "east":
                x += 1
                edge = "west"
            elif edge == "south":
                y += 1
                edge = "north"
            if edge == "west":
                points = (x * z, y * z, x * z, (y + 1) * z)
            else:
                points = (x * z, y * z, (x + 1) * z, y * z)
            selected_boundary = boundary.get("id") == self.selected_boundary_id
            self.canvas.create_line(
                *points,
                fill="#ffcf40" if selected_boundary else boundary_colors.get(str(boundary.get("type")), "#60564c"),
                width=max(3, round(z / 5)) if selected_boundary else max(2, round(z / 8)),
            )
        for obj in self.objects:
            width, height = self.footprint(obj)
            x, y = obj["x"] * z, obj["y"] * z
            sprite = self.sprite_path(obj)
            if sprite:
                with Image.open(sprite) as source:
                    image = source.convert("RGBA")
                angle = {"E": 0, "S": -90, "W": 180, "N": 90}[normalize_orientation(obj.get("orientation", "E"))]
                if angle:
                    image = image.rotate(angle, expand=True)
                image.thumbnail((max(1, round(width * z)), max(1, round(height * z))), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                self.images.append(photo)
                self.canvas.create_image(x + width * z / 2, y + height * z / 2, image=photo)
            else:
                self.canvas.create_rectangle(x + 1, y + 1, x + width * z - 1, y + height * z - 1, fill="#a98254", outline="#3b2d1f")
                if z >= 8:
                    self.canvas.create_text(x + width * z / 2, y + height * z / 2, text=obj["type"], fill="white", width=width * z)
            if obj["id"] == self.selected_id:
                self.canvas.create_rectangle(x, y, x + width * z, y + height * z, outline="#ffcf40", width=3)
        selected = self.selected_object()
        if selected:
            self.selection_var.set(f"#{selected['id']}  {selected['type']}\nTile ({selected['x']}, {selected['y']})  {normalize_orientation(selected.get('orientation', 'E'))}")
            self.quality_var.set(selected.get("quality", 100))
        elif (boundary := self.selected_boundary()) is not None:
            swing = str(boundary.get("swing", "counterclockwise"))
            self.selection_var.set(
                f"{boundary['id']}  {boundary.get('type', 'boundary')}\n"
                f"Tile edge ({boundary['x']}, {boundary['y']}) {boundary.get('edge')}\n"
                f"Swing: {swing}"
            )
        else:
            self.selection_var.set("No object selected")


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit authored objects in a Remembering scenario.")
    parser.add_argument("scenario", nargs="?", type=Path, default=DEFAULT_SCENARIO)
    args = parser.parse_args()
    ScenarioEditor(args.scenario).mainloop()


if __name__ == "__main__":
    main()
