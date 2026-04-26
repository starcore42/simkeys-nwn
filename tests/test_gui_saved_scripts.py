import os
import tempfile
import unittest
from types import SimpleNamespace

from src.simkeys_app import simkeys_gui
from src.simkeys_app.simkeys_gui import SimKeysDesktopApp
from src.simkeys_app.simkeys_script_host import ScriptManager


def make_persistence_app(path):
    app = SimKeysDesktopApp.__new__(SimKeysDesktopApp)
    app.script_manager = ScriptManager(lambda _event: None)
    app.script_configs = {}
    app.script_autostart = {}
    app.character_script_configs = {}
    app.character_script_autostart = {}
    app.character_script_autostart_disabled = {}
    app.character_display_names = {}
    app.auto_loaded_character_keys = {}
    app.default_started_scripts = set()
    app.character_defaults_path = path
    app.clients_by_pid = {}
    app.log_messages = []
    app.log = lambda message, level="info": app.log_messages.append((level, message))
    return app


class FakeScriptManager:
    def __init__(self):
        self.registry = {
            "auto_attack": SimpleNamespace(name="Auto Attack"),
            "always_on": SimpleNamespace(name="Basic Functions"),
            "ingame_timers": SimpleNamespace(name="Timers"),
        }
        self.hosts = {}
        self.started = []
        self.stopped = []
        self.running = {}

    def default_config(self, script_id):
        return {"script_id": script_id}

    def get_state(self, client_pid, script_id):
        running = bool(self.running.get((client_pid, script_id)))
        return {"running": running, "status": "Running" if running else "Stopped"}

    def start_script(self, client, script_id, config):
        self.started.append((client.pid, script_id, dict(config)))
        self.running[(client.pid, script_id)] = True

    def stop_script(self, client_pid, script_id):
        self.stopped.append((client_pid, script_id))
        self.running[(client_pid, script_id)] = False


def make_bulk_app():
    app = SimKeysDesktopApp.__new__(SimKeysDesktopApp)
    app.script_manager = FakeScriptManager()
    app.script_configs = {}
    app.script_autostart = {}
    app.character_script_configs = {}
    app.character_script_autostart = {}
    app.character_script_autostart_disabled = {}
    app.character_display_names = {}
    app.auto_loaded_character_keys = {}
    app.default_started_scripts = set()
    app.selected_pid = None
    app.clients_by_pid = {}
    app.clients = []
    app.last_background = None
    app.log_messages = []
    app.log = lambda message, level="info": app.log_messages.append((level, message))
    app.persist_loaded_configs = lambda _pid: None

    def run_background(label, fn, refresh_after=False):
        app.last_background = (label, fn(), refresh_after)

    app.run_background = run_background
    return app


class GuiSavedScriptsTests(unittest.TestCase):
    def test_saved_script_flags_round_trip_with_character_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "character_defaults.user.json")
            app = make_persistence_app(path)
            app.clients_by_pid[101] = SimpleNamespace(character_name="Starcore-Bob")

            app.set_script_autostart(101, "always_on", True)
            app.set_script_autostart(101, "auto_attack", True)

            reloaded = make_persistence_app(path)
            reloaded._load_character_defaults_store()
            record = SimpleNamespace(pid=202, character_name="Starcore-Bob", display_name="Starcore-Bob")

            loaded = reloaded._auto_load_character_defaults(record)

            self.assertTrue(loaded)
            self.assertTrue(reloaded.get_script_autostart(202, "always_on"))
            self.assertTrue(reloaded.get_script_autostart(202, "auto_attack"))
            self.assertFalse(reloaded.get_script_autostart(202, "auto_aa"))

    def test_default_scripts_autostart_can_be_disabled_per_character(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "character_defaults.user.json")
            app = make_persistence_app(path)
            app.clients_by_pid[101] = SimpleNamespace(character_name="Starcore-Bob")

            app.set_script_autostart(101, "always_on", False)
            app.set_script_autostart(101, "ingame_timers", False)

            reloaded = make_persistence_app(path)
            reloaded._load_character_defaults_store()
            record = SimpleNamespace(pid=202, character_name="Starcore-Bob", display_name="Starcore-Bob")

            loaded = reloaded._auto_load_character_defaults(record)

            self.assertTrue(loaded)
            self.assertFalse(reloaded.get_script_autostart(202, "always_on"))
            self.assertFalse(reloaded.get_script_autostart(202, "ingame_timers"))

    def test_start_saved_scripts_starts_only_checked_scripts_for_injected_clients(self):
        app = make_bulk_app()
        app.clients = [
            SimpleNamespace(pid=1, injected=True, display_name="Alpha"),
            SimpleNamespace(pid=2, injected=True, display_name="Beta"),
            SimpleNamespace(pid=3, injected=False, display_name="Gamma"),
        ]
        app.script_autostart[(1, "always_on")] = True
        app.script_autostart[(2, "auto_attack")] = True

        app.start_saved_scripts_all_async()

        self.assertEqual(
            app.script_manager.started,
            [
                (1, "always_on", {"script_id": "always_on"}),
                (1, "ingame_timers", {"script_id": "ingame_timers"}),
                (2, "auto_attack", {"script_id": "auto_attack"}),
                (2, "always_on", {"script_id": "always_on"}),
                (2, "ingame_timers", {"script_id": "ingame_timers"}),
            ],
        )
        self.assertEqual(app.last_background[0], "Start Saved Scripts")

    def test_default_scripts_start_once_for_injected_client(self):
        app = make_bulk_app()
        client = SimpleNamespace(pid=1, injected=True, display_name="Alpha")

        app._ensure_default_scripts_running(client)
        app.script_manager.stop_script(client.pid, "always_on")
        app.script_manager.stop_script(client.pid, "ingame_timers")
        app._ensure_default_scripts_running(client)

        self.assertEqual(
            app.script_manager.started,
            [
                (1, "always_on", {"script_id": "always_on"}),
                (1, "ingame_timers", {"script_id": "ingame_timers"}),
            ],
        )
        self.assertEqual(
            app.default_started_scripts,
            {(1, "always_on"), (1, "ingame_timers")},
        )

    def test_stop_all_scripts_leaves_overlay_hosts_and_stops_running_scripts(self):
        app = make_bulk_app()
        app.clients_by_pid = {
            1: SimpleNamespace(display_name="Alpha"),
            2: SimpleNamespace(display_name="Beta"),
        }
        app.script_manager.hosts = {
            1: SimpleNamespace(running_script_ids=lambda: ["always_on"]),
            2: SimpleNamespace(running_script_ids=lambda: ["auto_attack"]),
        }

        app.stop_all_scripts_async()

        self.assertEqual(app.script_manager.stopped, [(1, "always_on"), (2, "auto_attack")])
        self.assertEqual(app.last_background[0], "Stop All Scripts")

    def test_assign_auto_attack_lead_targets_selected_lead_from_all_other_clients(self):
        app = make_bulk_app()
        lead = SimpleNamespace(pid=1, injected=True, display_name="Lead [1.0]", character_name="Lead [1.0]")
        follower = SimpleNamespace(pid=2, injected=True, display_name="Follower [1.0]", character_name="Follower [1.0]")
        offline = SimpleNamespace(pid=3, injected=False, display_name="Offline [1.0]", character_name="Offline [1.0]")
        app.clients = [lead, follower, offline]
        app.clients_by_pid = {record.pid: record for record in app.clients}
        app.selected_pid = lead.pid
        app.script_manager.running[(lead.pid, "auto_attack")] = True
        sent = []

        original_send_chat = simkeys_gui.runtime.send_chat
        try:
            simkeys_gui.runtime.send_chat = lambda client, text, mode=2: sent.append((client.pid, text, mode)) or {
                "success": 1,
                "rc": 0,
                "err": 0,
            }
            app.assign_auto_attack_lead_async()
        finally:
            simkeys_gui.runtime.send_chat = original_send_chat

        self.assertEqual(app.script_manager.stopped, [(lead.pid, "auto_attack")])
        self.assertEqual(
            sent,
            [
                (follower.pid, "!role lead", 2),
                (follower.pid, '/tell "Lead [1.0]" !target', 2),
            ],
        )
        self.assertEqual(app.last_background[0], "Assign Auto Attack Lead")


if __name__ == "__main__":
    unittest.main()
