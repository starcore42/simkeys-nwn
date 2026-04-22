import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import simkeys_runtime as runtime
from simkeys_script_host import ScriptManager


def _probe_error_is_busy(text):
    if not text:
        return False
    lowered = str(text).lower()
    return "err=231" in lowered or "all pipe instances are busy" in lowered or "pipe busy" in lowered


class ScriptRow:
    def __init__(self, parent, definition, app):
        self.app = app
        self.definition = definition
        self.vars = {}

        self.frame = ttk.Frame(parent, padding=(0, 6))
        self.frame.columnconfigure(1, weight=1)

        self.name_label = ttk.Label(self.frame, text=definition.name, width=14)
        self.name_label.grid(row=0, column=0, sticky="w")

        self.controls_frame = ttk.Frame(self.frame)
        self.controls_frame.grid(row=0, column=1, sticky="w")

        col = 0
        for field in definition.fields:
            ttk.Label(self.controls_frame, text=field.label).grid(row=0, column=col, padx=(0, 4), sticky="w")
            col += 1
            if field.kind == "bool":
                var = tk.BooleanVar(value=bool(field.default))
                widget = ttk.Checkbutton(self.controls_frame, variable=var)
                widget.grid(row=0, column=col, padx=(0, 10), sticky="w")
            elif field.kind == "choice":
                default_value = str(field.default)
                var = tk.StringVar(value=default_value)
                widget = ttk.Combobox(
                    self.controls_frame,
                    textvariable=var,
                    values=list(field.choices or []),
                    width=field.width,
                    state="readonly",
                )
                widget.grid(row=0, column=col, padx=(0, 10), sticky="w")
            else:
                var = tk.StringVar(value=str(field.default))
                widget = ttk.Entry(self.controls_frame, textvariable=var, width=field.width)
                widget.grid(row=0, column=col, padx=(0, 10), sticky="w")
            self.vars[field.key] = (field, var, widget)
            col += 1

        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = ttk.Label(self.frame, textvariable=self.status_var, width=20)
        self.status_label.grid(row=0, column=2, padx=(12, 8), sticky="w")

        self.toggle_button = ttk.Button(self.frame, text="Start", command=self.on_toggle, width=10)
        self.toggle_button.grid(row=0, column=3, sticky="e")

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    def load_for_client(self, client_pid):
        config = self.app.get_script_config(client_pid, self.definition.script_id)
        for field, var, _widget in self.vars.values():
            value = config.get(field.key, field.default)
            if field.kind == "bool":
                var.set(bool(value))
            else:
                var.set(str(value))
        self.refresh_state()

    def set_enabled(self, enabled):
        button_state = "normal" if enabled else "disabled"
        for field, _var, widget in self.vars.values():
            if field.kind == "choice":
                state = "readonly" if enabled else "disabled"
            else:
                state = "normal" if enabled else "disabled"
            widget.configure(state=state)
        self.toggle_button.configure(state=button_state)
        if not enabled:
            self.status_var.set("Unavailable")

    def parse_config(self):
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

    def refresh_state(self):
        client = self.app.selected_client()
        if client is None or not client.injected:
            self.set_enabled(False)
            return

        self.set_enabled(True)
        state = self.app.script_manager.get_state(client.pid, self.definition.script_id)
        self.status_var.set(state["status"])
        self.toggle_button.configure(text="Stop" if state["running"] else "Start")

    def on_toggle(self):
        self.app.toggle_script(self.definition.script_id, self.parse_config())


class SimKeysDesktopApp:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.root.title("SimKeys Control Center")
        self.root.geometry("1440x900")
        self.root.minsize(1180, 760)

        self.event_queue = queue.Queue()
        self.script_manager = ScriptManager(self.enqueue_event)
        self.clients = []
        self.clients_by_pid = {}
        self.selected_pid = None
        self.refresh_in_progress = False
        self.script_configs = {}

        self.status_var = tk.StringVar(value="Ready")
        self.selected_name_var = tk.StringVar(value="No client selected")
        self.selected_details_var = tk.StringVar(value="Select an NWN client to see details.")
        self.chat_entry_var = tk.StringVar()
        self.auto_refresh_var = tk.BooleanVar(value=True)

        self._configure_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.process_events)
        self.root.after(150, self.refresh_clients_async)
        self.root.after(self.args.refresh_ms, self.auto_refresh_tick)

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

        left = ttk.Frame(paned, padding=(0, 0, 10, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=3)

        ttk.Label(left, text="Discovered Clients").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.client_tree = ttk.Treeview(
            left,
            columns=("ord", "pid", "injected", "name", "window", "started", "scripts"),
            show="headings",
            selectmode="browse",
            height=18,
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
            self.client_tree.column(col, width=width, anchor=anchor, stretch=(col in ("name", "window")))
        self.client_tree.grid(row=1, column=0, sticky="nsew")
        client_scroll = ttk.Scrollbar(left, orient="vertical", command=self.client_tree.yview)
        client_scroll.grid(row=1, column=1, sticky="ns")
        self.client_tree.configure(yscrollcommand=client_scroll.set)
        self.client_tree.bind("<<TreeviewSelect>>", self.on_client_selected)

        right = ttk.Frame(paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        paned.add(right, weight=5)

        details = ttk.LabelFrame(right, text="Client Details", padding=10)
        details.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        details.columnconfigure(0, weight=1)
        ttk.Label(details, textvariable=self.selected_details_var, justify="left").grid(row=0, column=0, sticky="w")

        actions = ttk.LabelFrame(right, text="Selected Client Actions", padding=10)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        actions.columnconfigure(0, weight=1)

        quickbar = ttk.LabelFrame(actions, text="Quickbar Banks", padding=8)
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

        chat_row = ttk.Frame(actions)
        chat_row.grid(row=1, column=0, sticky="ew")
        chat_row.columnconfigure(1, weight=1)
        ttk.Label(chat_row, text="Chat").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(chat_row, textvariable=self.chat_entry_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(chat_row, text="Send", command=self.send_chat_async).grid(row=0, column=2, padx=(8, 0))

        scripts = ttk.LabelFrame(right, text="Scripts", padding=10)
        scripts.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        scripts.columnconfigure(0, weight=1)
        self.script_rows = {}
        for row_index, definition in enumerate(self.script_manager.definitions()):
            row = ScriptRow(scripts, definition, self)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 4))
            self.script_rows[definition.script_id] = row

        logs = ttk.LabelFrame(right, text="Activity Log", padding=10)
        logs.grid(row=3, column=0, sticky="nsew")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(logs, wrap="word", height=18, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        status_bar = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        status_bar.grid(row=2, column=0, sticky="ew", pady=(10, 0))

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
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{level.upper()}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

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

    def apply_client_records(self, records):
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
        for record in records:
            self.script_manager.sync_client(record)

        live_pids = set(self.clients_by_pid.keys())
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

    def get_script_config(self, client_pid, script_id):
        key = (client_pid, script_id)
        if key not in self.script_configs:
            self.script_configs[key] = self.script_manager.default_config(script_id)
        return dict(self.script_configs[key])

    def set_script_config(self, client_pid, script_id, config):
        self.script_configs[(client_pid, script_id)] = dict(config)

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
        state = self.script_manager.get_state(client.pid, script_id)
        try:
            if state["running"]:
                self.script_manager.stop_script(client.pid, script_id)
                self.log(f"{client.display_name}: stopped {script_id}", "info")
            else:
                self.script_manager.start_script(client, script_id, config)
                self.log(f"{client.display_name}: started {script_id}", "info")
        except Exception as exc:
            self.log(f"{client.display_name}: could not toggle {script_id}: {exc}", "error")
        self.refresh_selected_client_ui()
        self.refresh_client_tree_rows()

    def on_close(self):
        try:
            self.script_manager.stop_all()
        finally:
            self.root.destroy()


def build_parser():
    parser = argparse.ArgumentParser(description="Desktop SimKeys control client.")
    parser.add_argument("--process-name", default="nwmain.exe", help="Process image name to discover. Default: nwmain.exe")
    parser.add_argument("--dll", default=runtime.default_dll_path())
    parser.add_argument("--export", default="InitSimKeys")
    parser.add_argument("--inject-python", help="32-bit Python interpreter to use for injection if this process is not x86.")
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
