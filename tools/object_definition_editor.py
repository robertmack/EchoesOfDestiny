from __future__ import annotations

import json
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from remembering.jsonc import loads_jsonc  # noqa: E402
from remembering.world import MapLoadError, load_object_types  # noqa: E402


OBJECTS_PATH = PROJECT_ROOT / "data" / "object_types.jsonc"


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
    index = start
    in_string = False
    escaped = False
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


def object_array_bounds(source: str) -> tuple[int, int]:
    index = 0
    while index < len(source):
        index = _skip_jsonc(source, index)
        if index >= len(source):
            break
        if source[index] != '"':
            index += 1
            continue
        end = index + 1
        escaped = False
        while end < len(source):
            if escaped:
                escaped = False
            elif source[end] == "\\":
                escaped = True
            elif source[end] == '"':
                break
            end += 1
        if end >= len(source):
            raise ValueError("Unterminated JSON string")
        key = json.loads(source[index : end + 1])
        after_key = _skip_jsonc(source, end + 1)
        if (
            key == "object_types"
            and after_key < len(source)
            and source[after_key] == ":"
        ):
            array_start = _skip_jsonc(source, after_key + 1)
            if array_start >= len(source) or source[array_start] != "[":
                raise ValueError("object_types is not an array")
            return (
                array_start,
                _matching_delimiter(source, array_start, "[", "]"),
            )
        index = end + 1
    raise ValueError("object_types.jsonc has no object_types array")


def object_spans(source: str) -> list[tuple[int, int, dict[str, Any]]]:
    array_start, array_end = object_array_bounds(source)
    spans: list[tuple[int, int, dict[str, Any]]] = []
    index = array_start + 1
    while True:
        index = _skip_jsonc(source, index)
        while index < array_end and source[index] == ",":
            index = _skip_jsonc(source, index + 1)
        if index >= array_end:
            break
        if source[index] != "{":
            raise ValueError(f"Expected an object definition near character {index}")
        end = _matching_delimiter(source, index, "{", "}") + 1
        value = loads_jsonc(source[index:end])
        if not isinstance(value, dict):
            raise ValueError("An object_types entry is not an object")
        spans.append((index, end, value))
        index = end
    return spans


def formatted_definition(definition: dict[str, Any], base_indent: str) -> str:
    lines = json.dumps(definition, indent=4, ensure_ascii=False).splitlines()
    return lines[0] + "".join(f"\n{base_indent}{line}" for line in lines[1:])


def replace_definition(
    source: str, original_id: str, definition: dict[str, Any]
) -> str:
    for start, end, existing in object_spans(source):
        if existing.get("id") != original_id:
            continue
        line_start = source.rfind("\n", 0, start) + 1
        base_indent = source[line_start:start]
        return (
            source[:start]
            + formatted_definition(definition, base_indent)
            + source[end:]
        )
    raise ValueError(f"Object {original_id!r} no longer exists in the file")


def append_definition(source: str, definition: dict[str, Any]) -> str:
    _start, array_end = object_array_bounds(source)
    spans = object_spans(source)
    base_indent = "        "
    rendered = formatted_definition(definition, base_indent)
    if spans:
        insertion = f",\n{base_indent}{rendered}"
    else:
        insertion = f"\n{base_indent}{rendered}\n    "
    return source[:array_end] + insertion + source[array_end:]


def delete_definition(source: str, object_id: str) -> str:
    spans = object_spans(source)
    for index, (start, end, definition) in enumerate(spans):
        if definition.get("id") != object_id:
            continue
        if index + 1 < len(spans):
            next_start = spans[index + 1][0]
            return source[:start] + source[next_start:]
        if index > 0:
            previous_end = spans[index - 1][1]
            comma = source.find(",", previous_end, start)
            if comma >= 0:
                return source[:comma] + source[end:]
        return source[:start] + source[end:]
    raise ValueError(f"Object {object_id!r} no longer exists in the file")


def validate_catalog(source: str) -> None:
    parsed = loads_jsonc(source)
    definitions = parsed.get("object_types")
    if not isinstance(definitions, list):
        raise ValueError("object_types must be an array")
    ids = [entry.get("id") for entry in definitions if isinstance(entry, dict)]
    if len(ids) != len(definitions) or any(
        not isinstance(object_id, str) or not object_id for object_id in ids
    ):
        raise ValueError("Every object definition requires a non-empty string id")
    if len(set(ids)) != len(ids):
        raise ValueError("Object IDs must be unique")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonc", encoding="utf-8", delete=False
        ) as temporary:
            temporary.write(source)
            temporary_name = temporary.name
        load_object_types(Path(temporary_name))
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


class ObjectDefinitionEditor(tk.Tk):
    def __init__(self, path: Path = OBJECTS_PATH) -> None:
        super().__init__()
        self.path = path
        self.source = ""
        self.definitions: list[dict[str, Any]] = []
        self.selected_original_id: str | None = None
        self.title("Remembering — Object Definition Editor")
        self.geometry("1180x780")
        self.minsize(900, 600)
        self._build_ui()
        self.reload_file()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_args: self.refresh_list())
        ttk.Entry(toolbar, textvariable=self.filter_var, width=28).pack(
            side=tk.LEFT, padx=(6, 14)
        )
        for label, command in (
            ("New", self.new_object),
            ("Duplicate", self.duplicate_object),
            ("Delete", self.delete_object),
            ("Validate", self.validate_current),
            ("Save Object", self.save_object),
            ("Revert", self.revert_object),
            ("Reload File", self.reload_file),
        ):
            ttk.Button(toolbar, text=label, command=command).pack(
                side=tk.LEFT, padx=3
            )

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)

        self.object_list = tk.Listbox(left, exportselection=False)
        list_scroll = ttk.Scrollbar(
            left, orient=tk.VERTICAL, command=self.object_list.yview
        )
        self.object_list.configure(yscrollcommand=list_scroll.set)
        self.object_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.object_list.bind("<<ListboxSelect>>", self.select_object)

        ttk.Label(
            right,
            text=(
                "Selected object definition — strict JSON. "
                "The surrounding JSONC comments remain untouched."
            ),
        ).pack(anchor=tk.W, pady=(0, 5))
        editor_frame = ttk.Frame(right)
        editor_frame.pack(fill=tk.BOTH, expand=True)
        self.editor = tk.Text(
            editor_frame,
            wrap=tk.NONE,
            undo=True,
            font=("Consolas", 11),
            tabs=("2c",),
        )
        vertical = ttk.Scrollbar(
            editor_frame, orient=tk.VERTICAL, command=self.editor.yview
        )
        horizontal = ttk.Scrollbar(
            editor_frame, orient=tk.HORIZONTAL, command=self.editor.xview
        )
        self.editor.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(right, textvariable=self.status_var).pack(
            anchor=tk.W, pady=(5, 0)
        )

    def reload_file(self) -> None:
        try:
            source = self.path.read_text(encoding="utf-8")
            definitions = [entry[2] for entry in object_spans(source)]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Cannot load objects", str(exc), parent=self)
            return
        self.source = source
        self.definitions = definitions
        self.selected_original_id = None
        self.refresh_list()
        if self.definitions:
            self.object_list.selection_set(0)
            self.select_object()
        self.status_var.set(f"Loaded {len(definitions)} objects from {self.path.name}")

    def filtered_definitions(self) -> list[dict[str, Any]]:
        query = self.filter_var.get().strip().lower()
        if not query:
            return self.definitions
        return [
            definition
            for definition in self.definitions
            if query in str(definition.get("id", "")).lower()
            or query in str(definition.get("name", "")).lower()
        ]

    def refresh_list(self, select_id: str | None = None) -> None:
        visible = self.filtered_definitions()
        self.object_list.delete(0, tk.END)
        selected_index = None
        for index, definition in enumerate(visible):
            object_id = str(definition.get("id", "<missing id>"))
            self.object_list.insert(tk.END, object_id)
            if object_id == select_id:
                selected_index = index
        if selected_index is not None:
            self.object_list.selection_set(selected_index)
            self.object_list.see(selected_index)
            self.select_object()

    def select_object(self, _event: object | None = None) -> None:
        selection = self.object_list.curselection()
        if not selection:
            return
        definition = self.filtered_definitions()[selection[0]]
        self.selected_original_id = str(definition["id"])
        self.set_editor_value(definition)
        self.status_var.set(f"Editing {self.selected_original_id}")

    def set_editor_value(self, definition: dict[str, Any]) -> None:
        self.editor.delete("1.0", tk.END)
        self.editor.insert(
            "1.0", json.dumps(definition, indent=4, ensure_ascii=False)
        )
        self.editor.edit_reset()

    def editor_value(self) -> dict[str, Any]:
        value = json.loads(self.editor.get("1.0", "end-1c"))
        if not isinstance(value, dict):
            raise ValueError("An object definition must be a JSON object")
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError("The object requires a non-empty string id")
        return value

    def candidate_source(self) -> tuple[str, dict[str, Any]]:
        definition = self.editor_value()
        if self.selected_original_id is None:
            candidate = append_definition(self.source, definition)
        else:
            candidate = replace_definition(
                self.source, self.selected_original_id, definition
            )
        validate_catalog(candidate)
        return candidate, definition

    def validate_current(self) -> None:
        try:
            self.candidate_source()
        except (ValueError, json.JSONDecodeError, MapLoadError) as exc:
            messagebox.showerror("Invalid object definition", str(exc), parent=self)
            return
        self.status_var.set("Definition and complete catalog are valid.")

    def save_object(self) -> None:
        try:
            candidate, definition = self.candidate_source()
            self.path.write_text(candidate, encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError, MapLoadError) as exc:
            messagebox.showerror("Could not save object", str(exc), parent=self)
            return
        object_id = str(definition["id"])
        self.reload_file()
        self.refresh_list(select_id=object_id)
        self.status_var.set(f"Saved {object_id} explicitly.")

    def revert_object(self) -> None:
        if self.selected_original_id is None:
            self.editor.delete("1.0", tk.END)
            return
        definition = next(
            (
                entry
                for entry in self.definitions
                if entry.get("id") == self.selected_original_id
            ),
            None,
        )
        if definition is not None:
            self.set_editor_value(definition)
            self.status_var.set(f"Reverted unsaved changes to {self.selected_original_id}.")

    def new_object(self) -> None:
        object_id = simpledialog.askstring(
            "New object", "Stable object ID:", parent=self
        )
        if not object_id:
            return
        object_id = object_id.strip()
        self.selected_original_id = None
        self.set_editor_value(
            {"id": object_id, "name": object_id.replace("_", " ").title()}
        )
        self.status_var.set("New unsaved object. Use Save Object when ready.")

    def duplicate_object(self) -> None:
        try:
            definition = self.editor_value()
        except (ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Cannot duplicate", str(exc), parent=self)
            return
        object_id = simpledialog.askstring(
            "Duplicate object",
            "ID for the duplicated object:",
            initialvalue=f"{definition['id']}_copy",
            parent=self,
        )
        if not object_id:
            return
        definition["id"] = object_id.strip()
        definition["name"] = object_id.strip().replace("_", " ").title()
        self.selected_original_id = None
        self.set_editor_value(definition)
        self.status_var.set("Duplicated in the editor; not saved yet.")

    def delete_object(self) -> None:
        if self.selected_original_id is None:
            return
        object_id = self.selected_original_id
        if not messagebox.askyesno(
            "Delete object",
            f"Delete {object_id!r} from object_types.jsonc?\n\n"
            "This is immediate and may break existing map instances.",
            parent=self,
        ):
            return
        try:
            candidate = delete_definition(self.source, object_id)
            validate_catalog(candidate)
            self.path.write_text(candidate, encoding="utf-8")
        except (OSError, ValueError, json.JSONDecodeError, MapLoadError) as exc:
            messagebox.showerror("Could not delete object", str(exc), parent=self)
            return
        self.reload_file()
        self.status_var.set(f"Deleted {object_id}.")


def main() -> None:
    ObjectDefinitionEditor().mainloop()


if __name__ == "__main__":
    main()
