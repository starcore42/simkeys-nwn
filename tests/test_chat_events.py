import os
import unittest

from src.simkeys_app import simkeys_hgx_combat as combat
from src.simkeys_app import simkeys_hgx_data as hgx_data
from src.simkeys_app.simkeys_script_host import (
    AutoAAScript,
    ChatLineEvent,
    ClientScriptBase,
    ClientScriptHost,
    InGameTimersScript,
    WeaponRecommendation,
    _default_status_rules_dir,
    _load_hgx_spell_timer_specs,
    _spell_key,
    parse_chat_line_event,
)


class FakeClient:
    pid = 1234
    display_name = "Starcore-StormReaper [2.0]"
    character_name = "Starcore-StormReaper [2.0]"
    query = {}


class FakeHost:
    def __init__(self):
        self.client = FakeClient()
        self.latest_sequence = 0
        self.events = []
        self.chats = []
        self.slots = []
        self.mask = 1 << 0

    def emit(self, level, message, script_id=None):
        self.events.append((level, message, script_id))

    def notify_state_changed(self):
        pass

    def format_slot(self, page, slot):
        return f"F{slot}" if page == 0 else f"P{page}F{slot}"

    def send_chat(self, text, mode=2):
        self.chats.append(text)
        return {"success": 1, "rc": 0, "err": 0}

    def trigger_slot(self, slot, page=0):
        self.slots.append((page, slot))
        if slot == 2:
            self.mask = 1 << 1
        return {"success": 1, "rc": 0, "aux_rc": 0, "path": 1, "err": 0}

    def query_state(self):
        return {"quickbar_equipped_mask": self.mask}


class RecordingScript(ClientScriptBase):
    script_id = "recording"

    def __init__(self, event_types):
        super().__init__(FakeClient(), {}, None)
        self.event_types = tuple(event_types)
        self.events = []

    def chat_event_types(self):
        return self.event_types

    def on_chat_event(self, event: ChatLineEvent):
        self.events.append(event)

    def on_chat_line(self, sequence: int, text: str):
        raise AssertionError("router should use on_chat_event")


class ChatEventTests(unittest.TestCase):
    def test_spell_effect_keys_ignore_apostrophes(self):
        self.assertEqual(_spell_key("Tenser's Transformation"), _spell_key("Tensers Transformation"))
        self.assertEqual(_spell_key("Nature's Balance"), _spell_key("Natures Balance"))

    def test_default_spell_timer_rules_include_shadow_evade_and_aura_fear(self):
        xml_files = sorted(
            name
            for name in os.listdir(_default_status_rules_dir())
            if name.lower().endswith(".xml")
        )
        self.assertEqual(xml_files, ["statusrules.xml"])
        specs = {
            _spell_key(spec.spell): spec.effect
            for spec in _load_hgx_spell_timer_specs(_default_status_rules_dir())
        }
        self.assertEqual(specs[_spell_key("Shadow Evade")], "Shadow Evade")
        self.assertEqual(specs[_spell_key("Aura Fear")], "Aura Fear")

    def test_parse_combat_and_shifter_events(self):
        attack = parse_chat_line_event(1, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:00] Rapid Shot : Starcore-StormReaper [2.0] attacks Dummy : *hit*")
        self.assertIn("attack", attack.kinds)
        self.assertEqual(attack.attack.attacker, "Starcore-StormReaper [2.0]")
        self.assertEqual(attack.attack.defender, "Dummy")
        self.assertEqual(attack.attack.attack_mode, "Rapid Shot")

        damage = parse_chat_line_event(2, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:01] Starcore-StormReaper [2.0] damages Dummy : 42 (12 fire 30 physical)")
        self.assertIn("damage", damage.kinds)
        self.assertEqual(damage.damage.total, 42)
        self.assertEqual([component.type_name for component in damage.damage.components], ["Fire", "Physical"])

        shifted = parse_chat_line_event(3, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:02] Starcore-StormReaper [2.0] shifts into undead form.")
        self.assertIn("shifter_state", shifted.kinds)
        self.assertEqual(shifted.shifter_shift_actor, "Starcore-StormReaper [2.0]")

        essence = parse_chat_line_event(4, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:03] You have 419/420 essence points remaining.")
        self.assertIn("shifter_state", essence.kinds)
        self.assertEqual(essence.shifter_essence_current, 419)
        self.assertEqual(essence.shifter_essence_maximum, 420)

        player_hide = parse_chat_line_event(5, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:04] Acquired Item: Player Hide")
        self.assertTrue(player_hide.player_hide)
        self.assertIn("player_hide", player_hide.kinds)

        shadow_evade = parse_chat_line_event(6, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:05] Starcore-SD [4.0] casts Shadow Evade")
        self.assertIn("spell_cast", shadow_evade.kinds)
        self.assertEqual(shadow_evade.spell_caster, "Starcore-SD [4.0]")
        self.assertEqual(shadow_evade.spell_name, "Shadow Evade")

        aura_fear = parse_chat_line_event(7, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:06] Starcore-DSM [1.4] is surrounded by an aura.")
        self.assertIn("ability_trigger", aura_fear.kinds)
        self.assertIn("spell_cast", aura_fear.kinds)
        self.assertEqual(aura_fear.spell_caster, "Starcore-DSM [1.4]")
        self.assertEqual(aura_fear.spell_name, "Aura Fear")

    def test_ingame_timers_queries_aura_fear_and_reads_effect_duration(self):
        host = FakeHost()
        host.client.display_name = "Starcore-DSM [1.4]"
        host.client.character_name = "Starcore-DSM [1.4]"
        script = InGameTimersScript(host.client, {}, host)

        self.assertTrue(script._handle_spell_cast_line("Starcore-DSM [1.4] is surrounded by an aura.", 100.0))
        self.assertIn(_spell_key("Aura Fear"), script.pending_effect_queries)

        self.assertTrue(script._handle_effect_timer_line("#198 Aura Fear [4m39s left]", 101.0))
        self.assertNotIn(_spell_key("Aura Fear"), script.pending_effect_queries)
        timer = script.active["spell:aura fear"]
        self.assertEqual(timer.label, "Aura Fear")
        self.assertEqual(timer.duration_seconds, 279.0)

    def test_ingame_timers_reads_effect_duration_with_source_suffix(self):
        host = FakeHost()
        host.client.display_name = "Starcore-Ranger [4.3]"
        host.client.character_name = "Starcore-Ranger [4.3]"
        script = InGameTimersScript(host.client, {}, host)
        spec = next(
            spec
            for spec in _load_hgx_spell_timer_specs(_default_status_rules_dir())
            if _spell_key(spec.spell) == _spell_key("Invisibility Purge")
        )
        script.spell_specs_by_key = {spec.key: spec}
        script.spell_specs_by_effect_key = {_spell_key(spec.effect): spec}

        self.assertTrue(script._handle_spell_cast_line("Starcore-Ranger [4.3] casts Invisibility Purge", 100.0))
        self.assertIn(_spell_key("Invisibility Purge"), script.pending_effect_queries)

        effects = (
            "[Server] Effects on you:\n"
            "    #91 Invisibility Purge [11m34s left] (Shaundakul's Sense)"
        )
        self.assertTrue(script._handle_effect_timer_line(effects, 101.0))
        self.assertNotIn(_spell_key("Invisibility Purge"), script.pending_effect_queries)
        timer = script.active["spell:invisibility purge"]
        self.assertEqual(timer.label, "Invisibility Purge")
        self.assertEqual(timer.description, "Invisibility Purge")
        self.assertEqual(timer.duration_seconds, 694.0)

    def test_host_routes_typed_events_without_broadcasting_to_every_script(self):
        delivered = []
        host = ClientScriptHost(FakeClient(), delivered.append)
        damage_script = RecordingScript(("damage",))
        attack_script = RecordingScript(("attack",))
        raw_script = RecordingScript(("raw",))
        host.scripts = {
            "damage": damage_script,
            "attack": attack_script,
            "raw": raw_script,
        }

        damage_event = parse_chat_line_event(10, "Starcore-StormReaper [2.0] damages Dummy : 7 (7 fire)")
        host._dispatch_chat_event(damage_event)
        self.assertEqual([event.sequence for event in damage_script.events], [10])
        self.assertEqual(attack_script.events, [])
        self.assertEqual([event.sequence for event in raw_script.events], [10])

        attack_event = parse_chat_line_event(11, "Starcore-StormReaper [2.0] attacks Dummy : *hit*")
        host._dispatch_chat_event(attack_event)
        self.assertEqual([event.sequence for event in damage_script.events], [10])
        self.assertEqual([event.sequence for event in attack_script.events], [11])
        self.assertEqual([event.sequence for event in raw_script.events], [10, 11])

    def test_overlay_and_password_are_handled_before_script_dispatch(self):
        delivered = []
        host = ClientScriptHost(FakeClient(), delivered.append)
        raw_script = RecordingScript(("raw",))
        host.scripts = {"raw": raw_script}

        overlay = parse_chat_line_event(20, "\x1eSIMKEYS_OVERLAY_TOGGLE:auto_aa", password_prompt_text=host.PASSWORD_PROMPT_TEXT)
        stopped = host._process_chat_event(overlay, dispatch=True)
        self.assertFalse(stopped)
        self.assertEqual(raw_script.events, [])
        self.assertEqual(delivered[-1]["type"], "overlay-script-toggle")
        self.assertEqual(delivered[-1]["script_id"], "auto_aa")

        password = parse_chat_line_event(
            21,
            "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:00] You must speak your password before you can continue.",
            password_prompt_text=host.PASSWORD_PROMPT_TEXT,
        )
        stopped = host._process_chat_event(password, dispatch=True)
        self.assertTrue(stopped)
        self.assertEqual(raw_script.events, [])
        self.assertFalse(host.scripts)

    def test_auto_damage_shifter_sequence_from_parsed_events(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
                "swap_cooldown_seconds": 0.1,
            },
            host,
        )
        script.on_start()
        script.on_chat_event(parse_chat_line_event(1, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:00] Starcore-StormReaper [2.0] shifts into undead form."))
        self.assertEqual(script.shifter_shift_state, "shifted")

        self.assertTrue(script._request_weapon_swap(script.weapon_bindings["W2"], "Dummy", "learn"))
        self.assertEqual(host.chats[:2], ["!lock opponent", "!cancel poly"])

        script.on_chat_event(parse_chat_line_event(2, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:01] Acquired Item: Player Hide"))
        script.on_tick()
        self.assertEqual(host.slots[-1], (0, 2))

        script.on_chat_event(parse_chat_line_event(3, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:02] Weapon equipped as a one-handed weapon."))
        self.assertEqual(host.slots[-1], (0, 9))

        script.on_chat_event(parse_chat_line_event(4, "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:03] You have 419/420 essence points remaining."))
        self.assertEqual(host.chats[-1], "!action attack locked")
        self.assertEqual(script.shifter_swap_stage, "")
        self.assertEqual(script.shifter_shift_state, "shifted")

    def test_shifter_mode_only_swaps_when_current_weapon_heals(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
                "shifter_healing_only": True,
            },
            host,
        )
        script.on_start()
        script.current_weapon_key = "W1"
        script._profile_learning_complete = lambda profile: True
        script._next_weapon_to_learn = lambda target: None
        script.db.lookup = lambda name: True

        def recommendation(binding, score, healing=()):
            return WeaponRecommendation(
                binding=binding,
                expected_damage=score,
                selection_damage=score,
                actual_damage=None,
                actual_observations=0,
                matched_name="Dummy",
                paragon_ranks=0,
                learned_types=(3,),
                estimated_components=((3, 100),),
                healing_types=tuple(healing),
                ignored_types=(),
                special_name="",
                signature_observations=2,
                estimate_observations=1,
            )

        attack = combat.parse_attack_line("Starcore-StormReaper [2.0] attacks Dummy : *hit*")
        script._weapon_candidates_for_target = lambda name: [
            recommendation(script.weapon_bindings["W1"], 20, ()),
            recommendation(script.weapon_bindings["W2"], 100, ()),
        ]
        script._handle_weapon_attack(attack)
        self.assertEqual(host.chats, [])
        self.assertEqual(host.slots, [])
        self.assertIn("keep W1", script.status_text)

        script._weapon_candidates_for_target = lambda name: [
            recommendation(script.weapon_bindings["W1"], 0, (3,)),
            recommendation(script.weapon_bindings["W2"], 50, ()),
        ]
        script._handle_weapon_attack(attack)
        self.assertEqual(host.chats[:2], ["!lock opponent", "!cancel poly"])

    def test_shifter_mode_swaps_for_large_damage_gain_by_default(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
            },
            host,
        )
        script.on_start()
        script.current_weapon_key = "W1"
        script._profile_learning_complete = lambda profile: True
        script._next_weapon_to_learn = lambda target: None
        script.db.lookup = lambda name: True

        def recommendation(binding, score, healing=()):
            return WeaponRecommendation(
                binding=binding,
                expected_damage=score,
                selection_damage=score,
                actual_damage=None,
                actual_observations=0,
                matched_name="Dummy",
                paragon_ranks=0,
                learned_types=(3,),
                estimated_components=((3, 100),),
                healing_types=tuple(healing),
                ignored_types=(),
                special_name="",
                signature_observations=2,
                estimate_observations=1,
            )

        script._weapon_candidates_for_target = lambda name: [
            recommendation(script.weapon_bindings["W1"], 20, ()),
            recommendation(script.weapon_bindings["W2"], 100, ()),
        ]

        attack = combat.parse_attack_line("Starcore-StormReaper [2.0] attacks Dummy : *hit*")
        script._handle_weapon_attack(attack)

        self.assertEqual(host.chats[:2], ["!lock opponent", "!cancel poly"])

    def test_shifter_mode_holds_when_damage_gain_is_below_threshold(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
                "shifter_min_swap_gain_percent": 300.0,
            },
            host,
        )
        script.on_start()
        script.current_weapon_key = "W1"
        script._profile_learning_complete = lambda profile: True
        script._next_weapon_to_learn = lambda target: None
        script.db.lookup = lambda name: True

        def recommendation(binding, score):
            return WeaponRecommendation(
                binding=binding,
                expected_damage=score,
                selection_damage=score,
                actual_damage=None,
                actual_observations=0,
                matched_name="Dummy",
                paragon_ranks=0,
                learned_types=(3,),
                estimated_components=((3, 100),),
                healing_types=(),
                ignored_types=(),
                special_name="",
                signature_observations=2,
                estimate_observations=1,
            )

        script._weapon_candidates_for_target = lambda name: [
            recommendation(script.weapon_bindings["W1"], 100),
            recommendation(script.weapon_bindings["W2"], 350),
        ]

        attack = combat.parse_attack_line("Starcore-StormReaper [2.0] attacks Dummy : *hit*")
        script._handle_weapon_attack(attack)

        self.assertEqual(host.chats, [])
        self.assertEqual(host.slots, [])
        self.assertIn("< 300.0", script.status_text)

    def test_shifter_learning_keeps_current_weapon_when_shifted_mask_is_empty(self):
        host = FakeHost()
        host.mask = 0
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
            },
            host,
        )
        script.on_start()
        script.current_weapon_key = "W1"
        script.shifter_shift_state = "shifted"

        attack = combat.parse_attack_line("Starcore-StormReaper [2.0] attacks Barbazu : *hit*")
        script._handle_weapon_attack(attack)

        self.assertEqual(host.chats, [])
        self.assertEqual(host.slots, [])
        self.assertEqual(script.current_weapon_key, "W1")
        self.assertIn("learning W1", script.status_text)

    def test_shifter_recovers_unknown_weapon_from_outgoing_damage(self):
        host = FakeHost()
        host.mask = 1 << 0  # Stale shifted quickbar mask says W1.
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
            },
            host,
        )
        script.on_start()
        script.db = hgx_data.load_character_database()
        script.shifter_shift_state = "shifted"
        script.current_weapon_key = "Unknown"
        script.weapon_external_unknown = True
        script.weapon_external_unknown_feedback = "weapon equipped"
        script.weapon_profiles["W1"].stable_signature = (4,)
        script.weapon_profiles["W1"].stable_signature_observations = 2
        script.weapon_profiles["W2"].stable_signature = (6,)
        script.weapon_profiles["W2"].stable_signature_observations = 2

        damage = parse_chat_line_event(
            10,
            "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:01] Starcore-StormReaper [2.0] damages Dummy : 42 (12 fire 30 physical)",
        )
        script.on_chat_event(damage)

        self.assertEqual(script.current_weapon_key, "W2")
        self.assertFalse(script.weapon_external_unknown)
        self.assertNotIn("unknown after external swap", script.status_text)

    def test_shifter_damage_recovery_requires_unique_learned_signature(self):
        host = FakeHost()
        host.mask = 0
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "weapon_slot_2": "F2",
                "shift_slot": "F9",
            },
            host,
        )
        script.on_start()
        script.db = hgx_data.load_character_database()
        script.shifter_shift_state = "shifted"
        script.current_weapon_key = "Unknown"
        script.weapon_external_unknown = True
        script.weapon_external_unknown_feedback = "weapon equipped"
        script.weapon_profiles["W1"].stable_signature = (6,)
        script.weapon_profiles["W1"].stable_signature_observations = 2
        script.weapon_profiles["W2"].stable_signature = (6,)
        script.weapon_profiles["W2"].stable_signature_observations = 2

        damage = parse_chat_line_event(
            10,
            "[CHAT WINDOW TEXT] [Sun Apr 26 12:00:01] Starcore-StormReaper [2.0] damages Dummy : 42 (12 fire 30 physical)",
        )
        script.on_chat_event(damage)

        self.assertEqual(script.current_weapon_key, "Unknown")
        self.assertTrue(script.weapon_external_unknown)
        self.assertIn("unknown after external swap", script.status_text)

    def test_weapon_swap_rejects_shift_ctrl_weapon_slots(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_WEAPON_SWAP,
                "weapon_slot_1": "S+F1",
            },
            host,
        )

        with self.assertRaisesRegex(RuntimeError, "base F1-F12"):
            script.on_start()

    def test_shifter_shift_ability_can_still_use_shift_or_ctrl_slot(self):
        host = FakeHost()
        script = AutoAAScript(
            host.client,
            {
                "mode": AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                "weapon_slot_1": "F1",
                "shift_slot": "S+F9",
            },
            host,
        )

        script.on_start()

        self.assertEqual(script.shifter_shift_page, 1)
        self.assertEqual(script.shifter_shift_slot, 9)


if __name__ == "__main__":
    unittest.main()
