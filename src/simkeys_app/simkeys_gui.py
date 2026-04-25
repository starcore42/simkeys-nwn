import argparse
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import simkeys_runtime as runtime
from .simkeys_script_host import AutoAAScript, ScriptManager, WEAPON_CURRENT_UNKNOWN, WEAPON_SLOT_CHOICES, WEAPON_SLOT_NONE


def _probe_error_is_busy(text):
    if not text:
        return False
    lowered = str(text).lower()
    return (
        "err=5" in lowered
        or "err=32" in lowered
        or "err=231" in lowered
        or "access is denied" in lowered
        or "sharing violation" in lowered
        or "all pipe instances are busy" in lowered
        or "pipe busy" in lowered
    )


SCRIPT_CARD_LAYOUTS = {
    "autodrink": {
        "expanded": False,
        "sections": [
            ("Quickbar", ["page", "slot"]),
            ("Trigger", ["threshold_percent", "cooldown_seconds"]),
            ("Behavior", ["lock_target", "resume_attack"]),
        ],
        "advanced": ["poll_interval", "max_lines", "echo_console", "include_backlog"],
    },
    "stop_hitting": {
        "expanded": False,
        "sections": [
            ("Potion", ["page", "slot", "cooldown_seconds"]),
        ],
        "advanced": ["poll_interval", "max_lines", "echo_console", "include_backlog"],
    },
    "auto_action": {
        "expanded": False,
        "sections": [
            ("Action", ["cooldown_seconds"]),
        ],
        "advanced": [],
    },
    "auto_attack": {
        "expanded": False,
        "sections": [
            ("Trigger", ["cooldown_seconds"]),
        ],
        "advanced": [],
    },
    "always_on": {
        "expanded": False,
        "sections": [
            ("Follow", ["cooldown_seconds"]),
            ("Disable", ["disable_follow", "disable_wallet", "disable_spellbook_fill", "disable_fog_off"]),
        ],
        "advanced": ["poll_interval", "max_lines", "echo_console", "include_backlog"],
    },
    "auto_rsm": {
        "expanded": False,
        "sections": [
            ("Trigger", ["cooldown_seconds"]),
        ],
        "advanced": ["poll_interval", "max_lines", "echo_console", "include_backlog"],
    },
    "ingame_timers": {
        "expanded": False,
        "sections": [
            ("Overlay", ["position", "offset_x", "offset_y", "font_size", "color", "max_timers"]),
        ],
        "advanced": ["rules_dir", "poll_interval", "max_lines", "include_backlog"],
    },
}
SCRIPT_CARD_ACCENTS = {
    "autodrink": "#2c7be5",
    "stop_hitting": "#e03131",
    "auto_aa": "#00a878",
    "auto_action": "#f59f00",
    "auto_attack": "#d9480f",
    "always_on": "#087f5b",
    "auto_rsm": "#7950f2",
    "ingame_timers": "#1971c2",
}
BANK_PAGE_TO_VALUE = {"None": 0, "Shift": 1, "Control": 2}
BANK_VALUE_TO_PAGE = {value: label for label, value in BANK_PAGE_TO_VALUE.items()}
WEAPON_SLOT_RENDER_ORDER = [choice for choice in WEAPON_SLOT_CHOICES if choice != WEAPON_SLOT_NONE]
AUTO_DAMAGE_WEAPON_MODES = (AutoAAScript.MODE_WEAPON_SWAP,)
SCRIPT_CONFIG_SOURCE_DEFAULT = "default"
SCRIPT_CONFIG_SOURCE_CHARACTER = "character"
SCRIPT_CONFIG_SOURCE_MANUAL = "manual"


def _weapon_choice_display(choice):
    text = str(choice or "").strip().upper()
    if text.startswith("S+F"):
        return f"Shift+F{text[3:]}"
    if text.startswith("C+F"):
        return f"Ctrl+F{text[3:]}"
    return text


def _weapon_mode_limit(mode):
    if str(mode or "").strip() != AutoAAScript.MODE_WEAPON_SWAP:
        return 0
    return int(AutoAAScript.MAX_WEAPON_BINDINGS)


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.interior = ttk.Frame(self.canvas)

        self.window_id = self.canvas.create_window((0, 0), window=self.interior, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        self.interior.bind("<Configure>", self._on_interior_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.interior.bind("<Enter>", self._bind_mousewheel)
        self.interior.bind("<Leave>", self._unbind_mousewheel)

    def _on_interior_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_mousewheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120))
        if delta:
            self.canvas.yview_scroll(delta, "units")


class ScriptCard:
    def __init__(self, parent, definition, app):
        self.app = app
        self.definition = definition
        self.fields_by_key = {field.key: field for field in definition.fields}
        self.vars = {}
        self.widget_holders = {}
        self.extra_controls = []
        self.wrap_targets = []
        self.expanded = bool(SCRIPT_CARD_LAYOUTS.get(definition.script_id, {}).get("expanded", False))
        self.advanced_expanded = False
        self.loaded_client_pid = None

        self.frame = ttk.Frame(parent, padding=(0, 8))
        self.frame.columnconfigure(1, weight=1)

        accent = tk.Frame(self.frame, width=5, background=SCRIPT_CARD_ACCENTS.get(definition.script_id, "#868e96"))
        accent.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 8))

        header = ttk.Frame(self.frame)
        header.grid(row=0, column=1, sticky="ew")
        header.columnconfigure(1, weight=1)

        self.name_label = ttk.Label(header, text=definition.name)
        self.name_label.grid(row=0, column=0, sticky="w")

        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = ttk.Label(header, textvariable=self.status_var)
        self.status_label.grid(row=0, column=1, padx=(12, 10), sticky="w")

        next_column = 2
        if self.definition.script_id == "auto_aa":
            self._create_header_mode_control(header, next_column, on_change=self.on_auto_damage_mode_changed)
            next_column += 2
        elif self.definition.script_id in ("auto_action", "auto_rsm"):
            self._create_header_mode_control(header, next_column)
            next_column += 2

        self.expand_button = ttk.Button(header, text="", command=self.on_expand_toggle, width=12)
        self.expand_button.grid(row=0, column=next_column, padx=(0, 8), sticky="e")
        next_column += 1

        self.toggle_button = ttk.Button(header, text=self._toggle_button_text(False), command=self.on_toggle, width=18)
        self.toggle_button.grid(row=0, column=next_column, sticky="e")

        self.body = ttk.Frame(self.frame, padding=(16, 8, 0, 0))
        self.body.columnconfigure(0, weight=1)
        self.body.grid(row=1, column=1, sticky="ew")

        self.description_label = ttk.Label(
            self.body,
            text=definition.description,
            justify="left",
            wraplength=560,
        )
        self.description_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.wrap_targets.append((self.description_label, 36))

        self.content = ttk.Frame(self.body)
        self.content.grid(row=1, column=0, sticky="ew")
        self.content.columnconfigure(0, weight=1)

        if self.definition.script_id == "auto_aa":
            self._build_auto_damage_content()
        else:
            self._build_generic_content()

        self.separator = ttk.Separator(self.frame, orient="horizontal")
        self.separator.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        self.frame.bind("<Configure>", self._on_card_resize)
        self._apply_expanded_state()

    def _toggle_button_text(self, running: bool) -> str:
        if self.definition.script_id == "stop_hitting":
            return "Stop Guard" if running else "Start Guard"
        action = "Stop" if running else "Start"
        return f"{action} {self.definition.name}"

    def _create_header_mode_control(self, parent, column, on_change=None):
        field = self.fields_by_key["mode"]
        ttk.Label(parent, text=f"{field.label}:").grid(row=0, column=column, padx=(0, 4), sticky="e")
        var = tk.StringVar(value=str(field.default))
        widget = ttk.Combobox(
            parent,
            textvariable=var,
            values=list(field.choices or []),
            width=field.width,
            state="readonly",
        )
        widget.grid(row=0, column=column + 1, padx=(0, 8), sticky="e")
        if on_change is not None:
            widget.bind("<<ComboboxSelected>>", on_change)
        self.vars[field.key] = (field, var, widget)

    def _build_generic_content(self):
        layout = SCRIPT_CARD_LAYOUTS.get(self.definition.script_id, {"sections": [], "advanced": []})
        row = 0
        for title, field_keys in layout.get("sections", []):
            section = ttk.LabelFrame(self.content, text=title, padding=8)
            section.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            self._build_field_grid(section, field_keys, columns=min(max(len(field_keys), 1), 2))
            row += 1

        advanced_keys = layout.get("advanced", [])
        if advanced_keys:
            self.advanced_toggle_var = tk.StringVar(value="Show Advanced")
            ttk.Button(
                self.content,
                textvariable=self.advanced_toggle_var,
                command=self.on_advanced_toggle,
                width=14,
            ).grid(row=row, column=0, sticky="w")
            row += 1

            self.advanced_body = ttk.LabelFrame(self.content, text="Advanced", padding=8)
            self.advanced_body.grid(row=row, column=0, sticky="ew", pady=(8, 0))
            self._build_field_grid(self.advanced_body, advanced_keys, columns=min(max(len(advanced_keys), 1), 2))
            if not self.advanced_expanded:
                self.advanced_body.grid_remove()

    def _build_auto_damage_content(self):
        self.mode_hint_var = tk.StringVar(value="")
        self.mode_hint_label = ttk.Label(self.content, textvariable=self.mode_hint_var, justify="left", wraplength=520)
        self.mode_hint_label.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        self.wrap_targets.append((self.mode_hint_label, 36))

        self.command_section = ttk.LabelFrame(self.content, text="Command Switching", padding=8)
        self.command_section.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._create_field_holder(self.command_section, "elemental_dice", row=0, column=0)
        self._create_field_holder(self.command_section, "auto_canister", row=0, column=1)
        self._create_field_holder(self.command_section, "canister_cooldown_seconds", row=1, column=0)

        self.weapon_section = ttk.LabelFrame(self.content, text="Weapon Swapping", padding=8)
        self.weapon_section.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.weapon_section.columnconfigure(0, weight=1)

        weapon_top = ttk.Frame(self.weapon_section)
        weapon_top.grid(row=0, column=0, sticky="ew")

        self._create_field_holder(weapon_top, "swap_cooldown_seconds", row=0, column=0)
        self._create_field_holder(weapon_top, "min_swap_gain_percent", row=0, column=1)

        self.weapon_limit_var = tk.StringVar(value="")
        self.weapon_limit_label = ttk.Label(self.weapon_section, textvariable=self.weapon_limit_var, justify="left", wraplength=520)
        self.weapon_limit_label.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        self.wrap_targets.append((self.weapon_limit_label, 48))

        self.weapon_summary_var = tk.StringVar(value="Selected: none")
        self.weapon_summary_label = ttk.Label(self.weapon_section, textvariable=self.weapon_summary_var, justify="left", wraplength=520)
        self.weapon_summary_label.grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        self.wrap_targets.append((self.weapon_summary_label, 48))

        self.weapon_learning_var = tk.StringVar(value="Learned weapons: start Weapon Swap to populate this.")
        self.weapon_learning_label = ttk.Label(self.weapon_section, textvariable=self.weapon_learning_var, justify="left", wraplength=520)
        self.weapon_learning_label.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        self.wrap_targets.append((self.weapon_learning_label, 48))

        self.weapon_slot_hint_label = ttk.Label(
            self.weapon_section,
            text="Tick the slots that contain weapons you want Auto Damage to use.",
            justify="left",
            wraplength=520,
        )
        self.weapon_slot_hint_label.grid(
            row=4,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        self.wrap_targets.append((self.weapon_slot_hint_label, 48))

        grid = ttk.Frame(self.weapon_section)
        grid.grid(row=5, column=0, sticky="w")
        ttk.Label(grid, text="").grid(row=0, column=0, padx=(0, 8))
        for slot in range(1, 13):
            ttk.Label(grid, text=str(slot), width=4, anchor="center").grid(row=0, column=slot, padx=1, pady=(0, 2))

        self.weapon_slot_vars = {}
        for bank_row, bank_name in enumerate(("Base", "Shift", "Ctrl"), start=1):
            ttk.Label(grid, text=bank_name, width=7).grid(row=bank_row, column=0, padx=(0, 8), sticky="w")
            for slot in range(1, 13):
                if bank_name == "Base":
                    choice = f"F{slot}"
                elif bank_name == "Shift":
                    choice = f"S+F{slot}"
                else:
                    choice = f"C+F{slot}"
                var = tk.BooleanVar(value=False)
                widget = ttk.Checkbutton(grid, variable=var, command=self.on_weapon_slots_changed)
                widget.grid(row=bank_row, column=slot, padx=1, pady=1, sticky="w")
                self.weapon_slot_vars[choice] = (var, widget)
                self.extra_controls.append(("bool", widget))

        self.advanced_toggle_var = tk.StringVar(value="Show Advanced")
        ttk.Button(
            self.content,
            textvariable=self.advanced_toggle_var,
            command=self.on_advanced_toggle,
            width=14,
        ).grid(row=3, column=0, sticky="w")

        self.advanced_body = ttk.LabelFrame(self.content, text="Advanced", padding=8)
        self.advanced_body.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._build_field_grid(self.advanced_body, ["poll_interval", "max_lines", "echo_console", "include_backlog"], columns=2)
        if not self.advanced_expanded:
            self.advanced_body.grid_remove()

        self.on_auto_damage_mode_changed()

    def _build_field_grid(self, parent, field_keys, columns=3):
        for index, field_key in enumerate(field_keys):
            if field_key not in self.fields_by_key:
                continue
            row = index // max(columns, 1)
            column = index % max(columns, 1)
            self._create_field_holder(parent, field_key, row=row, column=column)

    def _create_field_holder(self, parent, field_key, row, column):
        field = self.fields_by_key[field_key]
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=column, sticky="nw", padx=(0, 14), pady=(0, 8))
        ttk.Label(holder, text=field.label).grid(row=0, column=0, sticky="w")

        if field.kind == "bool":
            var = tk.BooleanVar(value=bool(field.default))
            widget = ttk.Checkbutton(holder, variable=var)
        elif field.kind == "choice":
            var = tk.StringVar(value=str(field.default))
            widget = ttk.Combobox(
                holder,
                textvariable=var,
                values=list(field.choices or []),
                width=field.width,
                state="readonly",
            )
        else:
            var = tk.StringVar(value=str(field.default))
            widget = ttk.Entry(holder, textvariable=var, width=field.width)

        widget.grid(row=1, column=0, sticky="w")
        self.vars[field.key] = (field, var, widget)
        self.widget_holders[field.key] = holder
        return holder

    def _on_card_resize(self, event):
        width = max(int(event.width) - 40, 260)
        for widget, padding in self.wrap_targets:
            widget.configure(wraplength=max(width - padding, 220))

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    def load_for_client(self, client_pid):
        self.loaded_client_pid = client_pid
        config = self.app.get_script_config(client_pid, self.definition.script_id)
        if self.definition.script_id == "auto_aa":
            self._load_auto_damage_config(config)
        else:
            self._load_standard_config(config)
        self.refresh_state()

    def try_persist_for_client(self, client_pid):
        if client_pid is None or self.loaded_client_pid != client_pid:
            return False
        try:
            config = self.parse_config(validate_for_start=False)
        except Exception:
            return False
        current = self.app.get_script_config(client_pid, self.definition.script_id)
        if config == current:
            return False
        self.app.set_script_config(client_pid, self.definition.script_id, config)
        return True

    def _load_standard_config(self, config):
        for field, var, _widget in self.vars.values():
            value = config.get(field.key, field.default)
            if self.definition.script_id in ("autodrink", "stop_hitting") and field.key == "page":
                if isinstance(value, str) and value.strip() in BANK_PAGE_TO_VALUE:
                    var.set(value.strip())
                else:
                    var.set(BANK_VALUE_TO_PAGE.get(int(value or 0), "None"))
                continue
            if field.kind == "bool":
                var.set(bool(value))
            else:
                var.set(str(value))

    def _load_auto_damage_config(self, config):
        for key in (
            "mode",
            "elemental_dice",
            "auto_canister",
            "canister_cooldown_seconds",
            "swap_cooldown_seconds",
            "min_swap_gain_percent",
            "poll_interval",
            "max_lines",
            "echo_console",
            "include_backlog",
        ):
            field, var, _widget = self.vars[key]
            value = config.get(field.key, field.default)
            if field.kind == "bool":
                var.set(bool(value))
            else:
                var.set(str(value))

        for var, _widget in self.weapon_slot_vars.values():
            var.set(False)

        selected_choices = []
        for index in range(1, 7):
            choice = str(config.get(f"weapon_slot_{index}", WEAPON_SLOT_NONE)).strip() or WEAPON_SLOT_NONE
            if choice != WEAPON_SLOT_NONE and choice in self.weapon_slot_vars:
                selected_choices.append(choice)
                self.weapon_slot_vars[choice][0].set(True)

        self.on_auto_damage_mode_changed()

    def set_enabled(self, enabled):
        button_state = "normal" if enabled else "disabled"
        for field, _var, widget in self.vars.values():
            self._set_widget_state(widget, field.kind, enabled)
        for control_kind, widget in self.extra_controls:
            self._set_widget_state(widget, control_kind, enabled)
        self.toggle_button.configure(state=button_state)
        if not enabled:
            self.status_var.set("Unavailable")
            self.toggle_button.configure(text=self._toggle_button_text(False))

    def _set_widget_state(self, widget, kind, enabled):
        if kind == "choice":
            state = "readonly" if enabled else "disabled"
        else:
            state = "normal" if enabled else "disabled"
        widget.configure(state=state)

    def parse_config(self, validate_for_start=True):
        if self.definition.script_id == "auto_aa":
            return self._parse_auto_damage_config(validate_for_start=validate_for_start)

        config = {}
        for field, var, _widget in self.vars.values():
            value = var.get()
            if field.kind == "bool":
                config[field.key] = bool(value)
            elif field.kind == "choice":
                text_value = str(value).strip()
                if field.choices and text_value not in field.choices:
                    raise RuntimeError(f"{field.label} must be one of: {', '.join(field.choices)}.")
                config[field.key] = text_value
            elif field.kind == "int":
                parsed = int(value)
                if field.minimum is not None and parsed < field.minimum:
                    raise RuntimeError(f"{field.label} must be at least {int(field.minimum)}.")
                if field.maximum is not None and parsed > field.maximum:
                    raise RuntimeError(f"{field.label} must be at most {int(field.maximum)}.")
                config[field.key] = parsed
            elif field.kind == "float":
                parsed = float(value)
                if field.minimum is not None and parsed < field.minimum:
                    raise RuntimeError(f"{field.label} must be at least {field.minimum}.")
                if field.maximum is not None and parsed > field.maximum:
                    raise RuntimeError(f"{field.label} must be at most {field.maximum}.")
                config[field.key] = parsed
            else:
                config[field.key] = str(value)
        return config

    def _parse_auto_damage_config(self, validate_for_start=True):
        config = {}
        for key in (
            "mode",
            "elemental_dice",
            "auto_canister",
            "canister_cooldown_seconds",
            "swap_cooldown_seconds",
            "min_swap_gain_percent",
            "poll_interval",
            "max_lines",
            "echo_console",
            "include_backlog",
        ):
            field, var, _widget = self.vars[key]
            value = var.get()
            if field.kind == "bool":
                config[field.key] = bool(value)
            elif field.kind == "choice":
                text_value = str(value).strip()
                if field.choices and text_value not in field.choices:
                    raise RuntimeError(f"{field.label} must be one of: {', '.join(field.choices)}.")
                config[field.key] = text_value
            elif field.kind == "int":
                parsed = int(value)
                if field.minimum is not None and parsed < field.minimum:
                    raise RuntimeError(f"{field.label} must be at least {int(field.minimum)}.")
                if field.maximum is not None and parsed > field.maximum:
                    raise RuntimeError(f"{field.label} must be at most {int(field.maximum)}.")
                config[field.key] = parsed
            elif field.kind == "float":
                parsed = float(value)
                if field.minimum is not None and parsed < field.minimum:
                    raise RuntimeError(f"{field.label} must be at least {field.minimum}.")
                if field.maximum is not None and parsed > field.maximum:
                    raise RuntimeError(f"{field.label} must be at most {field.maximum}.")
                config[field.key] = parsed
            else:
                config[field.key] = str(value)

        selected = self._selected_weapon_choices()
        for index in range(1, 7):
            config[f"weapon_slot_{index}"] = selected[index - 1] if index <= len(selected) else WEAPON_SLOT_NONE

        config["current_weapon"] = WEAPON_CURRENT_UNKNOWN

        mode = str(config.get("mode", "")).strip()
        if validate_for_start and mode in AUTO_DAMAGE_WEAPON_MODES:
            max_bindings = _weapon_mode_limit(mode)
            if not selected:
                raise RuntimeError(
                    "Weapon Swap needs at least one weapon quickbar button selected. "
                    "Open Show Settings and tick the quickbar slots that contain weapons."
                )
            if len(selected) > max_bindings:
                raise RuntimeError(f"{mode} supports at most {max_bindings} weapon quickbar buttons.")
        return config

    def refresh_state(self):
        client = self.app.selected_client()
        if client is None or not client.injected:
            self.set_enabled(False)
            return

        self.set_enabled(True)
        state = self.app.script_manager.get_state(client.pid, self.definition.script_id)
        busy_label = self.app.script_toggles_in_progress.get((client.pid, self.definition.script_id))
        if busy_label:
            self.status_var.set(busy_label)
        else:
            self.status_var.set(state["status"])
        self.toggle_button.configure(text=self._toggle_button_text(state["running"]))
        if busy_label:
            self.toggle_button.configure(state="disabled")
        if self.definition.script_id == "auto_aa":
            self._refresh_auto_damage_runtime_details(state.get("details", {}), state["running"])

    def _refresh_auto_damage_runtime_details(self, details, running):
        if not hasattr(self, "weapon_learning_var"):
            return
        if not running or not details.get("weapon_mode"):
            self.weapon_learning_var.set("Learned weapons: start Weapon Swap to populate this.")
            return

        weapons = list(details.get("weapons", []))
        if not weapons:
            self.weapon_learning_var.set("Learned weapons: waiting for configured weapon slots.")
            return

        lines = []
        current_display = details.get("current_display") or details.get("current_weapon") or "Unknown"
        pending_display = details.get("pending_display") or ""
        state_line = f"Current state: {current_display}"
        if pending_display:
            state_line += f", pending {pending_display}"
        unarmed_count = int(details.get("unarmed_observations") or 0)
        if unarmed_count:
            state_line += f", unarmed seen {unarmed_count}"
        if details.get("pending_conceal_seen"):
            state_line += ", round boundary seen"
        ignored_damage = int(details.get("pending_ignored_damage") or 0)
        if ignored_damage:
            state_line += f", ignored pre-boundary {ignored_damage}"
        equipped_display = str(details.get("equipped_display") or "").strip()
        if equipped_display:
            state_line += f", hook equipped {equipped_display}"
        lines.append(state_line)
        equipped_error = str(details.get("equipped_probe_error") or "").strip()
        if equipped_error:
            lines.append(f"Equipped probe error: {equipped_error}")
        last_swap_feedback = str(details.get("last_swap_feedback") or "").replace("_", " ")
        if last_swap_feedback:
            lines.append(f"Last swap feedback: {last_swap_feedback}")
        for weapon in weapons:
            marker = "* " if weapon.get("current") else ""
            if weapon.get("pending"):
                marker = "> "
            lines.append(
                f"{marker}{weapon.get('key', '?')}/{weapon.get('label', '?')}: "
                f"{weapon.get('summary', 'Unknown')}"
            )
        combat = dict(details.get("combat", {}))
        if combat:
            lines.append(
                "Combat seen: "
                f"attacks {combat.get('attack_matched', 0)}/{combat.get('attack_seen', 0)}, "
                f"damage {combat.get('damage_matched', 0)}/{combat.get('damage_seen', 0)}, "
                f"parse misses {combat.get('damage_parse_miss', 0)}"
            )
            ignored = combat.get("ignored_attack_actor") or combat.get("ignored_damage_actor")
            if ignored:
                lines.append(f"Last ignored actor: {ignored}")
        self.weapon_learning_var.set("Learned weapons:\n" + "\n".join(lines))

    def on_expand_toggle(self):
        self.expanded = not self.expanded
        self._apply_expanded_state()

    def _apply_expanded_state(self):
        if self.expanded:
            self.body.grid(row=1, column=1, sticky="ew")
            self.expand_button.configure(text="Hide Settings")
        else:
            self.body.grid_remove()
            self.expand_button.configure(text="Show Settings")

    def on_advanced_toggle(self):
        self.advanced_expanded = not self.advanced_expanded
        if self.advanced_expanded:
            self.advanced_body.grid()
            self.advanced_toggle_var.set("Hide Advanced")
        else:
            self.advanced_body.grid_remove()
            self.advanced_toggle_var.set("Show Advanced")

    def on_auto_damage_mode_changed(self, _event=None):
        mode = str(self.vars["mode"][1].get()).strip()
        is_weapon = mode in AUTO_DAMAGE_WEAPON_MODES
        is_gi = mode == AutoAAScript.MODE_GNOMISH_INVENTOR

        if is_weapon:
            max_bindings = _weapon_mode_limit(mode)
            self.mode_hint_var.set(
                f"{mode} swaps weapons by quickbar and learns each weapon's damage profile from combat log lines, including adaptive P2-style signatures and rolling damage estimates. "
                f"Select up to {max_bindings} weapon buttons. The starting weapon is assumed Unknown and reconciled from combat."
            )
            self.weapon_limit_var.set(
                "Round delay: the swap lands at the start of the next combat round. "
                "The script keeps the current weapon unless another clears the configured Gain % margin, "
                "and treats one-off type changes as swap/boundary noise first."
            )
            self.command_section.grid_remove()
            self.weapon_section.grid()
        else:
            self.weapon_section.grid_remove()
            self.command_section.grid()
            if is_gi:
                self.mode_hint_var.set(
                    "Gnomish Inventor switches bolt type by chat command. The canister loop can be enabled or disabled here."
                )
            else:
                self.mode_hint_var.set(
                    "This mode switches damage by unfocused chat command. Weapon quickbar selection stays hidden."
                )

        self._set_holder_visible("auto_canister", is_gi)
        self._set_holder_visible("canister_cooldown_seconds", is_gi)
        self._update_weapon_selector_ui()

    def _set_holder_visible(self, field_key, visible):
        holder = self.widget_holders.get(field_key)
        if holder is None:
            return
        if visible:
            holder.grid()
        else:
            holder.grid_remove()

    def on_weapon_slots_changed(self):
        self._update_weapon_selector_ui()

    def _selected_weapon_choices(self):
        selected = []
        for choice in WEAPON_SLOT_RENDER_ORDER:
            var, _widget = self.weapon_slot_vars[choice]
            if var.get():
                selected.append(choice)
        return selected

    def _update_weapon_selector_ui(self):
        if self.definition.script_id != "auto_aa":
            return

        selected = self._selected_weapon_choices()
        if selected:
            rendered = ", ".join(_weapon_choice_display(choice) for choice in selected)
            self.weapon_summary_var.set(f"Selected: {rendered}")
        else:
            self.weapon_summary_var.set("Selected: none")

        mode = str(self.vars["mode"][1].get()).strip()
        if mode in AUTO_DAMAGE_WEAPON_MODES:
            max_bindings = _weapon_mode_limit(mode)
            if len(selected) > max_bindings:
                self.weapon_limit_var.set(
                    f"Selected {len(selected)} weapon buttons, but {mode} only supports {max_bindings}. Trim the selection before starting."
                )
            else:
                self.weapon_limit_var.set(
                    "Round delay: the swap lands at the start of the next combat round. "
                    "From Unknown, the script probes from combat, treats physical-only hits as Unarmed, and builds approximate per-type damage estimates over time."
                )

    def on_toggle(self):
        try:
            config = self.parse_config()
        except Exception as exc:
            messagebox.showerror("SimKeys", str(exc))
            return
        self.app.toggle_script(self.definition.script_id, config)


class SimKeysDesktopApp:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.root.title("SimKeys Control Center")
        self.root.geometry("1500x930")
        self.root.minsize(1240, 780)

        self.event_queue = queue.Queue()
        self.script_manager = ScriptManager(self.enqueue_event)
        self.clients = []
        self.clients_by_pid = {}
        self.selected_pid = None
        self.refresh_in_progress = False
        self.script_configs = {}
        self.script_toggles_in_progress = {}
        self.character_script_configs = {}
        self.character_display_names = {}
        self.auto_loaded_character_keys = {}
        self.character_defaults_path = os.path.join(runtime.root_dir(), "data", "character_defaults.user.json")

        self.status_var = tk.StringVar(value="Ready")
        self.selected_name_var = tk.StringVar(value="No client selected")
        self.selected_details_var = tk.StringVar(value="Select an NWN client to see details.")
        self.chat_entry_var = tk.StringVar()
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.manual_controls_expanded = False
        self.manual_controls_toggle_var = tk.StringVar(value="Show Test Controls")
        self.target_analysis_expanded = False
        self.target_analysis_toggle_var = tk.StringVar(value="Show Target Analysis")
        self.activity_log_expanded = False
        self.activity_log_toggle_var = tk.StringVar(value="Show Activity Log")
        self.target_analysis_text = None
        self.log_text = None
        self.analysis_paned = None
        self.target_analysis_frame = None
        self.target_analysis_last_height = 300
        self.activity_log_frame = None
        self.activity_log_last_height = 260

        self._configure_style()
        self._build_ui()
        self._load_character_defaults_store()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.process_events)
        self.root.after(150, self.refresh_clients_async)
        self.root.after(self.args.refresh_ms, self.auto_refresh_tick)

    def _normalize_character_key(self, name):
        return str(name or "").strip().casefold()

    def _load_character_defaults_store(self):
        self.character_script_configs = {}
        self.character_display_names = {}
        path = self.character_defaults_path
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            self.log(f"Character defaults load failed: {exc}", "error")
            return

        characters = payload.get("characters", {}) if isinstance(payload, dict) else {}
        if not isinstance(characters, dict):
            return

        for key, entry in characters.items():
            normalized = self._normalize_character_key(key)
            if not normalized or not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or key).strip()
            scripts = entry.get("scripts", {})
            if not isinstance(scripts, dict):
                continue
            cleaned = {}
            for script_id, config in scripts.items():
                if script_id not in self.script_manager.registry or not isinstance(config, dict):
                    continue
                cleaned[script_id] = dict(config)
            if not cleaned:
                continue
            self.character_script_configs[normalized] = cleaned
            self.character_display_names[normalized] = name

    def _save_character_defaults_store(self):
        payload = {"version": 1, "characters": {}}
        for key in sorted(self.character_script_configs.keys()):
            scripts = self.character_script_configs.get(key) or {}
            if not scripts:
                continue
            payload["characters"][key] = {
                "name": self.character_display_names.get(key, key),
                "scripts": scripts,
            }

        os.makedirs(os.path.dirname(self.character_defaults_path), exist_ok=True)
        with open(self.character_defaults_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    def _save_character_defaults_for_client(self, client_pid):
        client = self.clients_by_pid.get(client_pid)
        if client is None or not client.character_name:
            return False

        character_key = self._normalize_character_key(client.character_name)
        if not character_key:
            return False

        scripts = {}
        for script_id in self.script_manager.registry.keys():
            scripts[script_id] = self.get_script_config(client_pid, script_id)

        if self.character_script_configs.get(character_key) == scripts:
            return False

        self.character_script_configs[character_key] = scripts
        self.character_display_names[character_key] = client.character_name
        self._save_character_defaults_store()
        return True

    def _auto_load_character_defaults(self, record):
        if record is None or not record.character_name:
            return False

        character_key = self._normalize_character_key(record.character_name)
        if not character_key:
            return False

        if self.auto_loaded_character_keys.get(record.pid) == character_key:
            return False

        scripts = self.character_script_configs.get(character_key)
        self.auto_loaded_character_keys[record.pid] = character_key
        if not scripts:
            return False

        for script_id, config in scripts.items():
            self.script_configs[(record.pid, script_id)] = dict(config)

        self.log(f"{record.display_name}: loaded saved character defaults", "info")
        return True

    def _configure_style(self):
        style = ttk.Style()
        for theme in ("vista", "xpnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(6, weight=1)

        ttk.Button(toolbar, text="Refresh Clients", command=self.refresh_clients_async).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(toolbar, text="Inject Next", command=self.inject_next_async).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(toolbar, text="Inject All", command=self.inject_all_async).grid(row=0, column=2, padx=(0, 8))
        ttk.Checkbutton(toolbar, text="Auto Refresh", variable=self.auto_refresh_var).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(toolbar, text="Selection:").grid(row=0, column=4, padx=(8, 4))
        ttk.Label(toolbar, textvariable=self.selected_name_var).grid(row=0, column=5, sticky="w")
        ttk.Label(toolbar, text=f"Inject Python: {self.args.inject_python or os.path.basename(sys.executable)}").grid(row=0, column=7, sticky="e")

        paned = ttk.Panedwindow(outer, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew")
        self.main_paned = paned

        left = ttk.Frame(paned, padding=(0, 0, 10, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=1)

        ttk.Label(left, text="Discovered Clients").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.client_tree = ttk.Treeview(
            left,
            columns=("ord", "pid", "injected", "name", "window", "started", "scripts"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        for col, title, width, anchor in (
            ("ord", "#", 45, "center"),
            ("pid", "PID", 75, "center"),
            ("injected", "Injected", 70, "center"),
            ("name", "Character", 170, "w"),
            ("window", "Window", 260, "w"),
            ("started", "Started", 150, "w"),
            ("scripts", "Scripts", 70, "center"),
        ):
            self.client_tree.heading(col, text=title)
            self.client_tree.column(col, width=width, anchor=anchor, stretch=False)
        self.client_tree.grid(row=1, column=0, sticky="nsew")
        client_scroll = ttk.Scrollbar(left, orient="vertical", command=self.client_tree.yview)
        client_scroll.grid(row=1, column=1, sticky="ns")
        client_xscroll = ttk.Scrollbar(left, orient="horizontal", command=self.client_tree.xview)
        client_xscroll.grid(row=2, column=0, sticky="ew")
        self.client_tree.configure(yscrollcommand=client_scroll.set, xscrollcommand=client_xscroll.set)
        self.client_tree.bind("<<TreeviewSelect>>", self.on_client_selected)

        right = ttk.Frame(paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        paned.add(right, weight=5)
        self.root.after(250, self._set_initial_pane_sizes)

        details = ttk.LabelFrame(right, text="Client Details", padding=10)
        details.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        details.columnconfigure(0, weight=1)
        self.details_label = ttk.Label(details, textvariable=self.selected_details_var, justify="left", wraplength=520)
        self.details_label.grid(row=0, column=0, sticky="ew")
        details.bind("<Configure>", self._on_details_resize)

        actions = ttk.LabelFrame(right, text="Manual Test Controls", padding=10)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        actions.columnconfigure(0, weight=1)

        actions_header = ttk.Frame(actions)
        actions_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        actions_header.columnconfigure(0, weight=1)
        self.manual_intro_label = ttk.Label(
            actions_header,
            text="Quickbar button presses and raw chat sends live here for manual testing and reverse engineering.",
            justify="left",
            wraplength=420,
        )
        self.manual_intro_label.grid(row=0, column=0, sticky="ew")
        actions.bind("<Configure>", self._on_manual_controls_resize)
        ttk.Button(
            actions_header,
            textvariable=self.manual_controls_toggle_var,
            command=self.toggle_manual_controls,
            width=18,
        ).grid(row=0, column=1, padx=(12, 0), sticky="e")

        self.manual_controls_body = ttk.Frame(actions)
        self.manual_controls_body.grid(row=1, column=0, sticky="ew")
        self.manual_controls_body.columnconfigure(0, weight=1)

        quickbar = ttk.LabelFrame(self.manual_controls_body, text="Quickbar Banks", padding=8)
        quickbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        quickbar.columnconfigure(1, weight=1)
        ttk.Label(
            quickbar,
            text="Page 0 is the normal bar, page 1 matches Shift+F1..F12, and page 2 matches Ctrl+F1..F12.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        for bank_row, (page, label) in enumerate(((0, "Base"), (1, "Shift"), (2, "Ctrl")), start=1):
            ttk.Label(quickbar, text=label, width=7).grid(row=bank_row, column=0, sticky="w", padx=(0, 8))
            row_frame = ttk.Frame(quickbar)
            row_frame.grid(row=bank_row, column=1, sticky="ew", pady=2)
            for slot in range(1, 13):
                ttk.Button(
                    row_frame,
                    text=f"F{slot}",
                    width=5,
                    command=lambda value=slot, bank_page=page, bank_label=label: self.trigger_slot_async(value, bank_page, bank_label),
                ).grid(
                    row=0,
                    column=slot - 1,
                    padx=2,
                    pady=2,
                    sticky="ew",
                )

        chat_row = ttk.Frame(self.manual_controls_body)
        chat_row.grid(row=1, column=0, sticky="ew")
        chat_row.columnconfigure(1, weight=1)
        ttk.Label(chat_row, text="Chat").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(chat_row, textvariable=self.chat_entry_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(chat_row, text="Send", command=self.send_chat_async).grid(row=0, column=2, padx=(8, 0))

        self._apply_manual_controls_state()

        analysis_paned = ttk.Panedwindow(right, orient="vertical")
        analysis_paned.grid(row=2, column=0, sticky="nsew")
        self.analysis_paned = analysis_paned

        target = ttk.LabelFrame(analysis_paned, text="Target Analysis", padding=10)
        self.target_analysis_frame = target
        target.columnconfigure(0, weight=1)
        target.rowconfigure(1, weight=1)
        target_header = ttk.Frame(target)
        target_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        target_header.columnconfigure(0, weight=1)
        ttk.Label(
            target_header,
            text="Current target resistances, healing, and learned weapon estimates.",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            target_header,
            textvariable=self.target_analysis_toggle_var,
            command=self.toggle_target_analysis,
            width=20,
        ).grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.target_analysis_text = ScrolledText(target, wrap="word", height=14, font=("Consolas", 9))
        self.target_analysis_text.grid(row=1, column=0, sticky="nsew")
        self.target_analysis_text.configure(state="disabled")
        self._set_target_analysis_text("Start Auto Damage in Weapon Swap mode to see target resistances and weapon estimates.")
        analysis_paned.add(target, weight=2)

        scripts = ttk.LabelFrame(analysis_paned, text="Automation", padding=10)
        scripts.columnconfigure(0, weight=1)
        scripts.rowconfigure(0, weight=1)
        self.script_scroller = ScrollableFrame(scripts)
        self.script_scroller.grid(row=0, column=0, sticky="nsew")
        self.script_scroller.interior.columnconfigure(0, weight=1)
        self.script_rows = {}
        for row_index, definition in enumerate(self.script_manager.definitions()):
            row = ScriptCard(self.script_scroller.interior, definition, self)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 4))
            self.script_rows[definition.script_id] = row
        analysis_paned.add(scripts, weight=3)

        logs = ttk.LabelFrame(analysis_paned, text="Activity Log", padding=10)
        self.activity_log_frame = logs
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(1, weight=1)
        logs_header = ttk.Frame(logs)
        logs_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        logs_header.columnconfigure(0, weight=1)
        ttk.Label(
            logs_header,
            text="Recent automation, connection, and script events.",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            logs_header,
            textvariable=self.activity_log_toggle_var,
            command=self.toggle_activity_log,
            width=18,
        ).grid(row=0, column=1, padx=(12, 0), sticky="e")
        self.log_text = ScrolledText(logs, wrap="word", height=18, font=("Consolas", 10))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        analysis_paned.add(logs, weight=2)
        self._apply_target_analysis_state()
        self._apply_activity_log_state()

        status_bar = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        status_bar.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def toggle_manual_controls(self):
        self.manual_controls_expanded = not self.manual_controls_expanded
        self._apply_manual_controls_state()

    def _apply_manual_controls_state(self):
        if self.manual_controls_expanded:
            self.manual_controls_body.grid()
            self.manual_controls_toggle_var.set("Hide Test Controls")
        else:
            self.manual_controls_body.grid_remove()
            self.manual_controls_toggle_var.set("Show Test Controls")

    def toggle_target_analysis(self):
        if self.target_analysis_expanded:
            self._remember_target_analysis_height()
        self.target_analysis_expanded = not self.target_analysis_expanded
        self._apply_target_analysis_state()

    def _apply_target_analysis_state(self):
        if self.target_analysis_text is None:
            return
        if self.target_analysis_expanded:
            self.target_analysis_text.grid()
            self.target_analysis_toggle_var.set("Hide Target Analysis")
            self.root.after_idle(self._restore_target_analysis_height)
        else:
            self.target_analysis_text.grid_remove()
            self.target_analysis_toggle_var.set("Show Target Analysis")
            self.root.after_idle(self._shrink_target_analysis_height)

    def toggle_activity_log(self):
        if self.activity_log_expanded:
            self._remember_activity_log_height()
        self.activity_log_expanded = not self.activity_log_expanded
        self._apply_activity_log_state()

    def _apply_activity_log_state(self):
        if self.log_text is None:
            return
        if self.activity_log_expanded:
            self.log_text.grid()
            self.activity_log_toggle_var.set("Hide Activity Log")
            self.root.after_idle(self._restore_activity_log_height)
        else:
            self.log_text.grid_remove()
            self.activity_log_toggle_var.set("Show Activity Log")
            self.root.after_idle(self._shrink_activity_log_height)

    def _remember_target_analysis_height(self):
        if self.analysis_paned is None:
            return
        try:
            height = int(self.analysis_paned.sashpos(0))
        except tk.TclError:
            return
        if height > 90:
            self.target_analysis_last_height = height

    def _target_analysis_collapsed_height(self) -> int:
        if self.target_analysis_frame is None:
            return 48
        try:
            return max(44, int(self.target_analysis_frame.winfo_reqheight()))
        except tk.TclError:
            return 48

    def _remember_activity_log_height(self):
        if self.analysis_paned is None:
            return
        try:
            total_height = int(self.analysis_paned.winfo_height())
            sash = int(self.analysis_paned.sashpos(1))
        except tk.TclError:
            return
        height = total_height - sash
        if height > 90:
            self.activity_log_last_height = height

    def _shrink_target_analysis_height(self):
        if self.analysis_paned is None:
            return
        try:
            self.analysis_paned.sashpos(0, self._target_analysis_collapsed_height())
        except tk.TclError:
            pass

    def _activity_log_collapsed_height(self):
        if self.activity_log_frame is None:
            return 48
        try:
            return max(44, int(self.activity_log_frame.winfo_reqheight()))
        except tk.TclError:
            return 48

    def _shrink_activity_log_height(self):
        if self.analysis_paned is None:
            return
        try:
            total_height = max(int(self.analysis_paned.winfo_height()), 1)
            self.analysis_paned.sashpos(1, max(0, total_height - self._activity_log_collapsed_height()))
        except tk.TclError:
            pass

    def _restore_target_analysis_height(self):
        if self.analysis_paned is None:
            return
        try:
            total_height = max(int(self.analysis_paned.winfo_height()), 1)
            target_height = max(220, min(int(self.target_analysis_last_height), max(total_height - 260, 120)))
            self.analysis_paned.sashpos(0, target_height)
        except tk.TclError:
            pass

    def _restore_activity_log_height(self):
        if self.analysis_paned is None:
            return
        try:
            total_height = max(int(self.analysis_paned.winfo_height()), 1)
            log_height = max(140, min(int(self.activity_log_last_height), max(total_height - 260, 100)))
            self.analysis_paned.sashpos(1, max(0, total_height - log_height))
        except tk.TclError:
            pass

    def _set_initial_pane_sizes(self):
        try:
            if self.main_paned.winfo_width() > 0:
                self.main_paned.sashpos(0, 430)
        except tk.TclError:
            pass
        try:
            if self.analysis_paned is not None and self.analysis_paned.winfo_height() > 0:
                height = self.analysis_paned.winfo_height()
                if self.target_analysis_expanded:
                    target_height = max(240, min(380, height // 3))
                else:
                    target_height = self._target_analysis_collapsed_height()
                self.analysis_paned.sashpos(0, target_height)
                remaining_height = max(height - target_height, 160)
                if self.activity_log_expanded:
                    log_height = max(140, min(int(self.activity_log_last_height), max(remaining_height // 3, 100)))
                else:
                    log_height = self._activity_log_collapsed_height()
                log_sash = max(target_height + 120, height - log_height)
                self.analysis_paned.sashpos(1, log_sash)
        except tk.TclError:
            pass

    def _on_details_resize(self, event):
        self.details_label.configure(wraplength=max(int(event.width) - 24, 240))

    def _on_manual_controls_resize(self, event):
        self.manual_intro_label.configure(wraplength=max(int(event.width) - 210, 220))

    def enqueue_event(self, event):
        self.event_queue.put(event)

    def log(self, message, level="info"):
        self.enqueue_event({"type": "log", "level": level, "message": message})

    def process_events(self):
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_event(event)
        self.root.after(100, self.process_events)

    def handle_event(self, event):
        event_type = event.get("type")
        if event_type == "clients-refreshed":
            self.apply_client_records(event["records"])
            return
        if event_type == "refresh-finished":
            self.refresh_in_progress = False
            return
        if event_type == "script-state":
            self.persist_loaded_configs(self.selected_pid)
            self.refresh_selected_client_ui()
            self.refresh_client_tree_rows()
            return
        if event_type == "script-toggle-finished":
            self.script_toggles_in_progress.pop((event.get("client_pid"), event.get("script_id")), None)
            self.refresh_selected_client_ui()
            self.refresh_client_tree_rows()
            return
        if event_type == "log":
            self.append_log(event.get("message", ""), event.get("level", "info"))
            return

    def append_log(self, message, level="info"):
        if not message:
            return
        self.status_var.set(message)
        if self.log_text is None:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{level.upper()}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_target_analysis_text(self, text):
        if self.target_analysis_text is None:
            return
        self.target_analysis_text.configure(state="normal")
        self.target_analysis_text.delete("1.0", "end")
        self.target_analysis_text.insert("1.0", text)
        self.target_analysis_text.configure(state="disabled")

    def _format_target_stat_entries(self, entries, value_suffix=""):
        entries = list(entries or [])
        if not entries:
            return "-"
        parts = []
        for entry in entries:
            label = str(entry.get("label") or entry.get("type") or "?")
            if "value" in entry:
                parts.append(f"{label} {entry.get('value')}{value_suffix}")
            else:
                parts.append(label)
        return ", ".join(parts)

    def _target_analysis_weapon_sort_key(self, weapon):
        selection = weapon.get("selection_damage")
        if selection is None:
            selection = -1
        return (
            0 if weapon.get("recommended") else 1,
            0 if weapon.get("current") else 1,
            0 if weapon.get("pending") else 1,
            0 if not weapon.get("healing_types") else 1,
            -int(selection),
            str(weapon.get("key") or ""),
        )

    def _compact_damage_label(self, label):
        label = str(label or "").strip()
        suffix = ""
        while label and not label[-1].isalnum():
            suffix = label[-1] + suffix
            label = label[:-1]
        compact = {
            "Acid": "Acid",
            "Bludgeoning": "Blud",
            "Cold": "Cold",
            "Divine": "Div",
            "Electrical": "Elec",
            "Fire": "Fire",
            "Magical": "Mag",
            "Negative": "Neg",
            "Piercing": "Pier",
            "Positive": "Pos",
            "Slashing": "Slsh",
            "Sonic": "Soni",
        }
        return compact.get(label, label) + suffix

    def _format_target_stat_entries_compact(self, entries, value_suffix=""):
        entries = list(entries or [])
        if not entries:
            return "-"
        parts = []
        for entry in entries:
            label = self._compact_damage_label(entry.get("label") or entry.get("type") or "?")
            if "value" in entry:
                parts.append(f"{label}{entry.get('value')}{value_suffix}")
            else:
                parts.append(label)
        return " ".join(parts)

    def _compact_weapon_type_text(self, weapon):
        summary_text = str(weapon.get("summary") or "").strip()
        if (
            str(weapon.get("special_name") or "").strip() == "P2"
            or "adaptive" in summary_text.lower()
        ):
            return "Adaptive"
        for prefix in ("Current ", "Types ", "Seen ", "Predicted "):
            marker = f"{prefix}"
            if marker in summary_text:
                fragment = summary_text.split(marker, 1)[1].split(", ", 1)[0]
                compact_parts = [
                    self._compact_damage_label(part)
                    for part in str(fragment).split("/")
                    if str(part).strip()
                ]
                if compact_parts:
                    return "/".join(compact_parts)
        if "Unknown" in summary_text:
            return "Unknown"
        return "-"

    def _compact_weapon_state_text(self, weapon):
        summary = str(weapon.get("summary") or "").strip()
        if "Learning Complete" in summary:
            return "done"
        if "P2 check " in summary:
            return summary.split("P2 check ", 1)[1].split(",", 1)[0].strip()
        if "adaptive" in summary.lower() or str(weapon.get("special_name") or "").strip() == "P2":
            return "P2"
        if "Unknown" in summary:
            return "unknown"
        return "learn"

    def _compact_weapon_special_tag_text(self, weapon):
        special_name = str(weapon.get("special_name") or "").strip()
        if special_name == "Mammon's Wrath":
            return "MW"
        if special_name == "P2":
            return "P2"
        return ""

    def _compact_weapon_notes_text(self, weapon):
        flags = []
        healing_types = [
            self._compact_damage_label(value)
            for value in list(weapon.get("healing_types") or [])
        ]
        if healing_types:
            flags.append("heal " + "/".join(healing_types))

        ignored_types = [
            self._compact_damage_label(value)
            for value in list(weapon.get("ignored_types") or [])
        ]
        if ignored_types:
            flags.append("ign " + "/".join(ignored_types))
        return ", ".join(flags)

    def _compact_weapon_damage_text(self, weapon):
        expected = weapon.get("expected_damage")
        actual = weapon.get("actual_damage")
        actual_obs = int(weapon.get("actual_observations") or 0)
        expected_text = f"e{int(expected)}" if expected is not None else "e-"
        if actual is not None and actual_obs > 0:
            actual_text = f"a{int(actual)}"
        else:
            actual_text = "a-"
        return f"{expected_text:<6}{actual_text:<8}".rstrip()

    def _render_target_analysis(self, details):
        analysis = dict(details.get("target_analysis", {}))
        target = str(analysis.get("target") or "").strip()
        if not target:
            return "Waiting for Auto Damage to observe your next attack target."

        lines = []
        if not analysis.get("available"):
            message = str(analysis.get("message") or f"No data for '{target}'.")
            return f"Target: {target}\nStatus: {message}"

        matched = str(analysis.get("matched_name") or target)
        paragon = int(analysis.get("paragon_ranks") or 0)
        target_line = f"Target: {target}"
        if matched and matched != target:
            target_line += f" | Entry: {matched}"
        target_line += f" | Paragon {paragon}"
        lines.append(target_line)

        special_rule = str(analysis.get("special_target_rule") or "").strip()
        if special_rule:
            lines.append(f"Rule: {special_rule}")
        lines.append(f"Imm:  {self._format_target_stat_entries_compact(analysis.get('immunity'), '%')}")
        lines.append(f"Res:  {self._format_target_stat_entries_compact(analysis.get('resistance'))}")
        lines.append(f"Heal: {self._format_target_stat_entries_compact(analysis.get('healing'))}")

        weapons = list(analysis.get("weapons") or [])

        recommended = next((weapon for weapon in weapons if weapon.get("recommended")), None)
        lines.append("")
        if recommended is None:
            lines.append("Best: -")
        else:
            recommendation_name = f"{recommended.get('key', '?')}/{recommended.get('label', '?')}"
            recommendation_damage = self._compact_weapon_damage_text(recommended)
            lines.append(f"Best: {recommendation_name} | {recommendation_damage}")

        lines.append("")
        if not weapons:
            lines.append("No learned weapon profiles yet.")
            return "\n".join(lines)

        for weapon in weapons:
            markers = ""
            if weapon.get("current"):
                markers += "*"
            if weapon.get("pending"):
                markers += ">"
            if weapon.get("recommended"):
                markers += "!"
            marker_text = f"{markers:3}" if markers else "   "

            key = str(weapon.get("key") or "?")
            label = str(weapon.get("label") or "?")
            slot_text = f"{key}/{label}"
            special_tag = self._compact_weapon_special_tag_text(weapon)
            notes_text = self._compact_weapon_notes_text(weapon)
            damage_text = self._compact_weapon_damage_text(weapon)
            type_text = self._compact_weapon_type_text(weapon)
            state_text = self._compact_weapon_state_text(weapon)
            row = f"{marker_text} {slot_text:<12} {special_tag:<4}{damage_text:<14} {type_text:<18} {state_text}"
            if notes_text:
                row = f"{row} {notes_text}"
            lines.append(row)

        lines.append("")
        lines.append("e expected   a actual   MW Mammon's Wrath   P2 Adaptive")
        lines.append("* current   > pending   ! recommended")
        return "\n".join(lines)

    def refresh_target_analysis_panel(self):
        client = self.selected_client()
        if client is None:
            self._set_target_analysis_text("Select an NWN client to see target analysis.")
            return

        state = self.script_manager.get_state(client.pid, "auto_aa")
        if not state.get("running"):
            self._set_target_analysis_text("Start Auto Damage in Weapon Swap mode to see target resistances and weapon estimates.")
            return

        details = dict(state.get("details", {}))
        if not details.get("weapon_mode"):
            self._set_target_analysis_text("Target analysis is currently focused on Weapon Swap mode.")
            return

        self._set_target_analysis_text(self._render_target_analysis(details))

    def auto_refresh_tick(self):
        if self.auto_refresh_var.get():
            self.refresh_clients_async()
        self.root.after(self.args.refresh_ms, self.auto_refresh_tick)

    def run_background(self, label, fn, refresh_after=False):
        def worker():
            try:
                message = fn()
                if message:
                    self.log(message, "info")
            except Exception as exc:
                self.log(f"{label} failed: {exc}", "error")
            finally:
                if refresh_after:
                    self.refresh_clients_async()
        threading.Thread(target=worker, name=f"SimKeysTask-{label}", daemon=True).start()

    def refresh_clients_async(self):
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True

        def worker():
            try:
                records = runtime.discover_clients(process_name=self.args.process_name)
                self.enqueue_event({"type": "clients-refreshed", "records": records})
            except Exception as exc:
                self.log(f"Refresh failed: {exc}", "error")
            finally:
                self.enqueue_event({"type": "refresh-finished"})

        threading.Thread(target=worker, name="SimKeysRefresh", daemon=True).start()

    def persist_loaded_configs(self, client_pid):
        if client_pid is None:
            return
        changed = False
        for row in self.script_rows.values():
            if row.try_persist_for_client(client_pid):
                changed = True
        if changed:
            self._save_character_defaults_for_client(client_pid)

    def apply_client_records(self, records):
        self.persist_loaded_configs(self.selected_pid)
        previous_records = dict(self.clients_by_pid)
        old_selected = self.selected_pid
        for record in records:
            previous = previous_records.get(record.pid)
            if previous is None:
                continue
            preserve_injected = (
                not record.injected and (
                    self.script_manager.running_script_count(record.pid) > 0
                    or (previous.injected and _probe_error_is_busy(record.probe_error))
                )
            )
            if preserve_injected:
                record.injected = True
                if not record.character_name:
                    record.character_name = previous.character_name
                if record.player_object == 0:
                    record.player_object = previous.player_object
                if record.identity_error == 0:
                    record.identity_error = previous.identity_error
                if record.query is None:
                    record.query = previous.query
                if not record.probe_error:
                    record.probe_error = previous.probe_error

        self.clients = records
        self.clients_by_pid = {record.pid: record for record in records}
        live_pids = set(self.clients_by_pid.keys())
        self.auto_loaded_character_keys = {
            pid: key
            for pid, key in self.auto_loaded_character_keys.items()
            if pid in live_pids
        }
        for record in records:
            self._auto_load_character_defaults(record)
        for record in records:
            self.script_manager.sync_client(record)

        for pid in list(self.script_manager.hosts.keys()):
            if pid not in live_pids:
                self.script_manager.stop_all_for_client(pid)

        self.client_tree.delete(*self.client_tree.get_children())
        for record in records:
            self.client_tree.insert(
                "",
                "end",
                iid=str(record.pid),
                values=(
                    record.ordinal,
                    record.pid,
                    "Yes" if record.injected else "No",
                    record.character_name or "-",
                    record.window_title or "-",
                    record.created_text,
                    self.script_manager.running_script_count(record.pid),
                ),
            )

        if old_selected in self.clients_by_pid:
            self.selected_pid = old_selected
        elif records:
            self.selected_pid = records[0].pid
        else:
            self.selected_pid = None

        if self.selected_pid is not None:
            self.client_tree.selection_set(str(self.selected_pid))
            self.client_tree.focus(str(self.selected_pid))
        self.refresh_selected_client_ui()

    def refresh_client_tree_rows(self):
        for record in self.clients:
            if self.client_tree.exists(str(record.pid)):
                self.client_tree.set(str(record.pid), "scripts", self.script_manager.running_script_count(record.pid))

    def on_client_selected(self, _event=None):
        old_selected = self.selected_pid
        self.persist_loaded_configs(old_selected)
        selection = self.client_tree.selection()
        if not selection:
            self.selected_pid = None
        else:
            self.selected_pid = int(selection[0])
        self.refresh_selected_client_ui()

    def selected_client(self):
        if self.selected_pid is None:
            return None
        return self.clients_by_pid.get(self.selected_pid)

    def refresh_selected_client_ui(self):
        client = self.selected_client()
        if client is None:
            self.selected_name_var.set("No client selected")
            self.selected_details_var.set("Select an NWN client to see details.")
            self.refresh_target_analysis_panel()
            for row in self.script_rows.values():
                row.set_enabled(False)
            return

        self.selected_name_var.set(f"#{client.ordinal} {client.display_name}")
        detail_lines = [
            f"PID: {client.pid}    Injected: {'Yes' if client.injected else 'No'}    Scripts: {self.script_manager.running_script_count(client.pid)}",
            f"Character: {client.character_name or '<unknown>'}    Player Object: 0x{client.player_object:08X}",
            f"Window: {client.window_title or '<untitled>'}",
            f"Class: {client.window_class or '<unknown>'}    HWND: 0x{client.hwnd:08X}    Thread: {client.thread_id}",
            f"Started: {client.created_text}    Identity Error: {client.identity_error}",
        ]
        if client.query:
            detail_lines.append(
                "Quickbar: "
                f"panel=0x{int(client.query.get('quickbar_this', 0)):08X} "
                f"page={int(client.query.get('quickbar_page', -1))} "
                f"slot={int(client.query.get('quickbar_slot', -1))} "
                f"slotType={int(client.query.get('quickbar_slot_type', 0))}"
            )
        if client.probe_error and not client.injected:
            detail_lines.append(f"Probe: {client.probe_error}")
        self.selected_details_var.set("\n".join(detail_lines))

        for row in self.script_rows.values():
            row.load_for_client(client.pid)
        self.refresh_target_analysis_panel()

    def get_script_config(self, client_pid, script_id):
        key = (client_pid, script_id)
        if key not in self.script_configs:
            self.script_configs[key] = self.script_manager.default_config(script_id)
        return dict(self.script_configs[key])

    def set_script_config(self, client_pid, script_id, config):
        self.script_configs[(client_pid, script_id)] = dict(config)
        self._save_character_defaults_for_client(client_pid)

    def inject_next_async(self):
        def action():
            records = runtime.discover_clients(process_name=self.args.process_name)
            if not records:
                raise RuntimeError("No nwmain.exe clients are running.")
            target = runtime.find_uninjected_client(records)
            if target is None:
                return "All discovered NWN clients are already injected."
            base, func = runtime.inject_client(
                target,
                self.args.dll,
                self.args.export,
                python_path=self.args.inject_python,
            )
            return f"Injected client #{target.ordinal} pid={target.pid} base=0x{base:08X} init=0x{func:08X}"

        self.run_background("Inject Next", action, refresh_after=True)

    def inject_all_async(self):
        def action():
            records = runtime.discover_clients(process_name=self.args.process_name)
            if not records:
                raise RuntimeError("No nwmain.exe clients are running.")
            targets = [record for record in records if not record.injected]
            if not targets:
                return "All discovered NWN clients are already injected."
            messages = []
            for target in targets:
                base, func = runtime.inject_client(
                    target,
                    self.args.dll,
                    self.args.export,
                    python_path=self.args.inject_python,
                )
                messages.append(f"#{target.ordinal} pid={target.pid} base=0x{base:08X} init=0x{func:08X}")
            return "Injected clients: " + "; ".join(messages)

        self.run_background("Inject All", action, refresh_after=True)

    def trigger_slot_async(self, slot, page=0, bank_label="Base"):
        client = self.selected_client()
        if client is None or not client.injected:
            messagebox.showwarning("SimKeys", "Select an injected client first.")
            return

        def action():
            result = runtime.trigger_slot(client, slot, page=page)
            if page == 0:
                trigger_name = f"F{slot}"
            else:
                trigger_name = f"{bank_label}+F{slot}"
            return (
                f"{client.display_name}: {trigger_name} "
                f"success={result['success']} rc={result['rc']} aux={result['aux_rc']} "
                f"path={result['path']} err={result['err']} page={result['page']}"
            )

        self.run_background(f"Trigger {bank_label} Slot {slot}", action)

    def send_chat_async(self):
        client = self.selected_client()
        text = self.chat_entry_var.get().strip()
        if client is None or not client.injected:
            messagebox.showwarning("SimKeys", "Select an injected client first.")
            return
        if not text:
            messagebox.showwarning("SimKeys", "Enter some chat text first.")
            return

        def action():
            result = runtime.send_chat(client, text, 2)
            return (
                f"{client.display_name}: chat-send success={result['success']} "
                f"mode={result['mode']} rc={result['rc']} err={result['err']}"
            )

        self.run_background("Send Chat", action)
        self.chat_entry_var.set("")

    def toggle_script(self, script_id, config):
        client = self.selected_client()
        if client is None or not client.injected:
            messagebox.showwarning("SimKeys", "Select an injected client first.")
            return

        self.set_script_config(client.pid, script_id, config)
        toggle_key = (client.pid, script_id)
        if toggle_key in self.script_toggles_in_progress:
            self.log(f"{client.display_name}: {script_id} is already changing state", "info")
            return

        state = self.script_manager.get_state(client.pid, script_id)
        starting = not state["running"]
        self.script_toggles_in_progress[toggle_key] = "Starting..." if starting else "Stopping..."

        row = self.script_rows.get(script_id)
        if row is not None:
            row.status_var.set("Starting..." if starting else "Stopping...")
            row.toggle_button.configure(state="disabled", text="Starting..." if starting else "Stopping...")

        def action():
            try:
                current_state = self.script_manager.get_state(client.pid, script_id)
                if current_state["running"]:
                    self.script_manager.stop_script(client.pid, script_id)
                    return f"{client.display_name}: stopped {script_id}"

                self.script_manager.start_script(client, script_id, config)
                return f"{client.display_name}: started {script_id}"
            finally:
                self.enqueue_event({
                    "type": "script-toggle-finished",
                    "client_pid": client.pid,
                    "script_id": script_id,
                })

        self.run_background(f"Toggle {script_id}", action)

    def on_close(self):
        try:
            self.persist_loaded_configs(self.selected_pid)
            self.script_manager.stop_all()
        finally:
            self.root.destroy()


def build_parser():
    parser = argparse.ArgumentParser(description="Desktop SimKeys control client.")
    parser.add_argument("--process-name", default="nwmain.exe", help="Process image name to discover. Default: nwmain.exe")
    parser.add_argument("--dll", default=runtime.default_dll_path())
    parser.add_argument("--export", default="InitSimKeys")
    parser.add_argument("--inject-python", help="Optional alternate Python interpreter to use for injection.")
    parser.add_argument("--refresh-ms", type=int, default=2500, help="Auto-refresh interval in milliseconds. Default: 2500")
    return parser


def main():
    args = build_parser().parse_args()
    root = tk.Tk()
    SimKeysDesktopApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
