import threading
import time
import ctypes as C
import ctypes.wintypes as W
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import simKeys_Client as simkeys
import simkeys_hgx_combat as hgx_combat
import simkeys_hgx_data as hgx_data
import simkeys_runtime as runtime

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
INVALID_HANDLE_VALUE = C.c_void_p(-1).value

_kernel32 = C.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_kernel32.OpenProcess.restype = W.HANDLE
_kernel32.CloseHandle.argtypes = [W.HANDLE]
_kernel32.CloseHandle.restype = W.BOOL
_kernel32.ReadProcessMemory.argtypes = [W.HANDLE, W.LPCVOID, W.LPVOID, C.c_size_t, C.POINTER(C.c_size_t)]
_kernel32.ReadProcessMemory.restype = W.BOOL

kLegacyImageBase = 0x00400000
kLegacyHpPointerOffset = 0x0053165C
kLegacyHpOwnerOffset = 0x2B8
kLegacyCurrentHpOffset = 0x4C
kLegacyMaxHpProbeOffsets = (2, 4, 6, 8, 0xA, 0xC, 0xE, 0x10)


@dataclass
class ScriptField:
    key: str
    label: str
    kind: str
    default: object
    choices: Optional[List[str]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    width: int = 8


@dataclass
class ScriptDefinition:
    script_id: str
    name: str
    description: str
    fields: List[ScriptField]
    factory: Callable


@dataclass
class SlingerTargetState:
    display_name: str
    last_seen_at: float = 0.0
    breach_done: bool = False
    breach_confirmed: bool = False
    breach_pending_until: float = 0.0
    last_breach_command_at: float = 0.0
    blind_done: bool = False
    blind_confirmed: bool = False
    blind_pending_until: float = 0.0
    last_blind_command_at: float = 0.0


class ClientScriptBase:
    def __init__(self, client, config: Dict[str, object], host):
        self.client = client
        self.config = deepcopy(config)
        self.host = host
        self.start_sequence = 0
        self.status_text = "Stopped"

    def on_start(self):
        self.start_sequence = self.host.latest_sequence
        self.status_text = "Running"

    def on_stop(self):
        self.status_text = "Stopped"

    def on_chat_line(self, sequence: int, text: str):
        raise NotImplementedError

    def needs_chat_feed(self) -> bool:
        return True

    def get_poll_interval(self) -> float:
        return max(float(self.config.get("poll_interval", 0.20)), 0.01)

    def get_max_lines(self) -> int:
        return max(int(self.config.get("max_lines", 20)), 1)

    def include_backlog(self) -> bool:
        return bool(self.config.get("include_backlog", False))

    def should_process(self, sequence: int) -> bool:
        return self.include_backlog() or sequence > self.start_sequence

    def set_status(self, text: str):
        if text == self.status_text:
            return
        self.status_text = text
        self.host.notify_state_changed()


class AutoDrinkScript(ClientScriptBase):
    script_id = "autodrink"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.process_handle = None
        self.hp_address = 0
        self.hp_owner_address = 0
        self.max_hp_address = 0
        self.max_hp_observed = 0
        self.drink_generation = 0
        self.drinking = False

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.hp_address = 0
        self.hp_owner_address = 0
        self.max_hp_address = 0
        self.max_hp_observed = 0
        self.drink_generation = 0
        self.drinking = False
        try:
            current_hp, max_hp, percent, source = self._read_health_snapshot()
            self.set_status(f"Armed {current_hp}/{max_hp} ({percent:.1f}%) [{source}]")
        except Exception:
            self.set_status("Armed")
        self.host.emit("info", f"{self.client.display_name}: AutoDrink started", script_id=self.script_id)

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.drinking = False
        self.drink_generation += 1
        self._close_process_handle()
        self.host.emit("info", f"{self.client.display_name}: AutoDrink stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence):
            return

        if not self.enabled or self.drinking:
            return

        lower = text.lower()
        combat_trigger = False
        if " attacks " in lower:
            name = (self.client.character_name or "").strip().lower()
            combat_trigger = not name or name in lower
        elif "casts" in lower:
            combat_trigger = True

        if not combat_trigger:
            return

        current_hp, max_hp, percent, source = self._read_health_snapshot()
        threshold_percent = float(self.config.get("threshold_percent", 80.0))
        self.set_status(f"HP {current_hp}/{max_hp} ({percent:.1f}%) [{source}]")
        if percent > threshold_percent:
            return

        slot = int(self.config.get("slot", 2))
        page = int(self.config.get("page", 0))
        trigger_name = self.host.format_slot(page, slot)
        if bool(self.config.get("lock_target", True)):
            try:
                self.host.send_chat("!lock opponent", 2)
            except Exception as exc:
                self.host.emit("error", f"{self.client.display_name}: !lock opponent failed: {exc}", script_id=self.script_id)

        result = self.host.trigger_slot(slot, page=page)
        self._begin_drink_cooldown()

        self.set_status(f"HP {current_hp}/{max_hp} ({percent:.1f}%) -> {trigger_name}")
        self.host.emit(
            "info",
            (
                f"{self.client.display_name}: autodrink fired at {current_hp}/{max_hp} ({percent:.1f}%) "
                f"threshold={threshold_percent:.1f}% source={source} trigger={trigger_name} "
                f"success={result['success']} rc={result['rc']} aux={result['aux_rc']} "
                f"path={result['path']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", True)):
            self.host.send_console(
                f"SimKeys autodrink {current_hp}/{max_hp} ({percent:.1f}%) -> {trigger_name} rc={result['rc']} err={result['err']}"
            )

    def _close_process_handle(self):
        handle = self.process_handle
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            _kernel32.CloseHandle(handle)
        self.process_handle = None

    def _ensure_process_handle(self):
        handle = self.process_handle
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            return handle

        handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(self.client.pid))
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            raise OSError(simkeys.winerr(f"OpenProcess failed for pid {self.client.pid}"))
        self.process_handle = handle
        return handle

    def _read_u32(self, address: int) -> int:
        handle = self._ensure_process_handle()
        value = C.c_uint32()
        size = C.c_size_t()
        if not _kernel32.ReadProcessMemory(handle, W.LPCVOID(address), C.byref(value), C.sizeof(value), C.byref(size)):
            raise OSError(simkeys.winerr(f"ReadProcessMemory(u32, 0x{address:08X}) failed"))
        return int(value.value)

    def _read_u16(self, address: int) -> int:
        handle = self._ensure_process_handle()
        value = C.c_uint16()
        size = C.c_size_t()
        if not _kernel32.ReadProcessMemory(handle, W.LPCVOID(address), C.byref(value), C.sizeof(value), C.byref(size)):
            raise OSError(simkeys.winerr(f"ReadProcessMemory(u16, 0x{address:08X}) failed"))
        return int(value.value)

    def _resolve_hp_address(self):
        if self.hp_address:
            return self.hp_address

        module_base = int((self.client.query or {}).get("module_base", 0)) or kLegacyImageBase
        pointer1_address = module_base + kLegacyHpPointerOffset
        pointer2_holder = self._read_u32(pointer1_address)
        if pointer2_holder == 0:
            raise RuntimeError(f"hp pointer1 at 0x{pointer1_address:08X} was null")
        hp_owner = self._read_u32(pointer2_holder + kLegacyHpOwnerOffset)
        if hp_owner == 0:
            raise RuntimeError(f"hp owner at 0x{pointer2_holder + kLegacyHpOwnerOffset:08X} was null")

        self.hp_owner_address = hp_owner
        self.hp_address = hp_owner + kLegacyCurrentHpOffset
        self.host.emit(
            "info",
            (
                f"{self.client.display_name}: hp path resolved module=0x{module_base:08X} "
                f"pointer1=0x{pointer1_address:08X} holder=0x{pointer2_holder:08X} "
                f"owner=0x{hp_owner:08X} currentHp=0x{self.hp_address:08X}"
            ),
            script_id=self.script_id,
        )
        return self.hp_address

    def _guess_max_hp(self, current_hp: int):
        best_value = 0
        best_address = 0
        for offset in kLegacyMaxHpProbeOffsets:
            address = self.hp_address + offset
            try:
                candidate = self._read_u16(address)
            except Exception:
                continue
            if candidate < current_hp or candidate <= 0:
                continue
            if candidate > 5000:
                continue
            if best_value == 0 or candidate < best_value or (candidate == best_value and offset < (best_address - self.hp_address)):
                best_value = candidate
                best_address = address
        if best_value and best_address:
            self.max_hp_address = best_address
        return best_value

    def _read_health_snapshot(self):
        self._resolve_hp_address()
        current_hp = self._read_u16(self.hp_address)
        if current_hp <= 0:
            raise RuntimeError(f"current HP at 0x{self.hp_address:08X} was {current_hp}")

        max_hp = 0
        source = "observed"
        if self.max_hp_address:
            try:
                candidate = self._read_u16(self.max_hp_address)
                if candidate >= current_hp and candidate <= 5000:
                    max_hp = candidate
                    source = f"probe+0x{self.max_hp_address - self.hp_address:X}"
            except Exception:
                self.max_hp_address = 0

        if max_hp == 0:
            candidate = self._guess_max_hp(current_hp)
            if candidate >= current_hp:
                max_hp = candidate
                source = f"probe+0x{self.max_hp_address - self.hp_address:X}"

        self.max_hp_observed = max(self.max_hp_observed, current_hp, max_hp)
        if max_hp == 0:
            max_hp = self.max_hp_observed

        percent = (float(current_hp) * 100.0 / float(max_hp)) if max_hp > 0 else 100.0
        return current_hp, max_hp, percent, source

    def _begin_drink_cooldown(self):
        self.drinking = True
        self.drink_generation += 1
        generation = self.drink_generation
        delay = max(float(self.config.get("cooldown_seconds", 3.0)), 0.1)

        def clear_after_delay():
            time.sleep(delay)
            if generation != self.drink_generation:
                return
            self.drinking = False
            if bool(self.config.get("resume_attack", True)):
                try:
                    self.host.send_chat("!action attack locked", 2)
                except Exception as exc:
                    self.host.emit("error", f"{self.client.display_name}: !action attack locked failed: {exc}", script_id=self.script_id)

        threading.Thread(target=clear_after_delay, name=f"AutoDrinkCooldown-{self.client.pid}", daemon=True).start()


class AutoAAScript(ClientScriptBase):
    script_id = "auto_aa"
    MODE_ARCANE_ARCHER = "Arcane Archer"
    MODE_ZEN_RANGER = "Zen Ranger"
    MODE_DIVINE_SLINGER = "Divine Slinger"
    MODE_GNOMISH_INVENTOR = "Gnomish Inventor"
    SLINGER_PENDING_SECONDS = 9.0
    SLINGER_STATE_TTL_SECONDS = 45.0
    SMALL_FETCH_TARGETS = {
        "spinagon",
        "spinarch",
        "superior spinarch",
        "elite spinarch",
        "quasit",
        "greater quasit",
        "superior quasit",
        "elite quasit",
        "fiendish fungus",
        "greater fiendish fungus",
        "superior fiendish fungus",
        "elite fiendish fungus",
    }

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.current_damage_type = 0
        self.current_secondary_mode = "damage"
        self.identity_wait_logged = False
        self.unknown_targets = set()
        self.current_target = ""
        self.canister_stop = threading.Event()
        self.canister_thread = None
        self.last_canister_error_key = ""
        self.slinger_states: Dict[str, SlingerTargetState] = {}
        self.db = hgx_data.load_default_database()

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.current_damage_type = 0
        self.current_secondary_mode = "damage"
        self.identity_wait_logged = False
        self.unknown_targets.clear()
        self.current_target = ""
        self.last_canister_error_key = ""
        self.canister_stop = threading.Event()
        self.slinger_states = {}
        damage_dice = self._damage_dice()
        self.set_status(f"{self._mode_label()} armed ({damage_dice}{self._dice_unit()})")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} started ({damage_dice}{self._dice_unit()})",
            script_id=self.script_id,
        )
        if self._mode_label() == self.MODE_GNOMISH_INVENTOR and bool(self.config.get("auto_canister", True)):
            self.canister_thread = threading.Thread(
                target=self._run_gi_canister_loop,
                name=f"AutoGICanister-{self.client.pid}",
                daemon=True,
            )
            self.canister_thread.start()
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: GI canister loop started ({self._canister_cooldown_seconds():.1f}s)",
                script_id=self.script_id,
            )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.current_secondary_mode = "damage"
        self.slinger_states = {}
        self.canister_stop.set()
        self.host.emit("info", f"{self.host.client.display_name}: {self._mode_label()} stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return

        if self._mode_label() == self.MODE_DIVINE_SLINGER:
            self._observe_slinger_line(text)

        feedback_type = self._parse_feedback_type(text)
        if feedback_type is not None:
            self.current_damage_type = feedback_type
            if self._mode_label() == self.MODE_DIVINE_SLINGER:
                self.current_secondary_mode = "damage"
            selection_name = self._selection_name_for_type(feedback_type)
            self.set_status(f"Current {selection_name}")
            return

        attack = hgx_combat.parse_attack_line(text)
        if attack is None:
            return

        character_name = self._character_name()
        if not character_name:
            self.set_status("Waiting for character name")
            if not self.identity_wait_logged:
                self.identity_wait_logged = True
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Auto-AA is waiting for character identity before parsing attack lines",
                    script_id=self.script_id,
                )
            return

        if attack.attacker.lower() != character_name.lower():
            return

        if self._mode_label() == self.MODE_GNOMISH_INVENTOR:
            self.current_target = attack.defender

        if self._mode_label() == self.MODE_DIVINE_SLINGER:
            self._handle_slinger_attack(attack)
            return

        recommendation = self._recommend_for_target(attack.defender)
        if recommendation is None:
            self.set_status(f"No data: {attack.defender}")
            defender_key = attack.defender.lower()
            if defender_key not in self.unknown_targets:
                self.unknown_targets.add(defender_key)
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: {self._mode_label()} has no characters.d entry for '{attack.defender}'",
                    script_id=self.script_id,
                )
            return

        self.set_status(f"{attack.defender}: {recommendation.selection_name}")
        if self.current_damage_type == recommendation.damage_type:
            return

        try:
            result = self._dispatch_recommendation(recommendation)
        except Exception as exc:
            self.set_status(f"Switch failed: {attack.defender}")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} chat send failed for {recommendation.command}: {exc}",
                script_id=self.script_id,
            )
            return

        if result["success"]:
            self.current_damage_type = recommendation.damage_type
        else:
            self.set_status(f"Switch failed: {attack.defender}")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} target='{attack.defender}' command={recommendation.command} "
                f"type={recommendation.selection_name} expected={recommendation.expected_damage} "
                f"paragon={recommendation.paragon_ranks} success={result['success']} rc={result['rc']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", False)):
            self.host.send_console(
                (
                    f"SimKeys {self._mode_label()} {attack.defender} -> {recommendation.selection_name} "
                    f"({recommendation.command}) rc={result['rc']} err={result['err']}"
                )
            )

    def _character_name(self) -> str:
        live_name = (self.host.client.character_name or "").strip()
        if live_name:
            return hgx_combat.strip_player_level_suffix(live_name)
        cached_name = (self.client.character_name or "").strip()
        if cached_name:
            return hgx_combat.strip_player_level_suffix(cached_name)
        return ""

    def _mode_label(self) -> str:
        mode = str(self.config.get("mode", self.MODE_ARCANE_ARCHER)).strip()
        if mode in (self.MODE_ARCANE_ARCHER, self.MODE_ZEN_RANGER, self.MODE_DIVINE_SLINGER, self.MODE_GNOMISH_INVENTOR):
            return mode
        return self.MODE_ARCANE_ARCHER

    def _damage_dice(self) -> int:
        return max(int(self.config.get("elemental_dice", 10)), 0)

    def _dice_unit(self) -> str:
        return "d20" if self._mode_label() == self.MODE_ARCANE_ARCHER else "d12"

    def _parse_feedback_type(self, text: str) -> Optional[int]:
        if self._mode_label() == self.MODE_GNOMISH_INVENTOR:
            return hgx_combat.parse_gi_feedback_type(text)
        feedback_type = hgx_combat.parse_damage_feedback_type(text)
        if self._mode_label() == self.MODE_DIVINE_SLINGER and feedback_type == 12:
            return None
        return feedback_type

    def _selection_name_for_type(self, damage_type: int) -> str:
        if self._mode_label() == self.MODE_GNOMISH_INVENTOR:
            return hgx_data.GI_TYPE_TO_WORD.get(damage_type, str(damage_type)).title()
        return hgx_data.AA_TYPE_TO_WORD.get(damage_type, str(damage_type))

    def _recommend_for_target(self, creature_name: str):
        damage_dice = self._damage_dice()
        mode = self._mode_label()
        if mode == self.MODE_ZEN_RANGER:
            return self.db.recommend_zen_ranger_damage(creature_name, damage_dice)
        if mode == self.MODE_DIVINE_SLINGER:
            return self.db.recommend_divine_slinger_damage(creature_name, damage_dice)
        if mode == self.MODE_GNOMISH_INVENTOR:
            return self.db.recommend_gnomish_inventor_damage(creature_name, damage_dice)
        return self.db.recommend_arcane_archer_damage(creature_name, damage_dice)

    def _dispatch_recommendation(self, recommendation):
        if self._mode_label() != self.MODE_GNOMISH_INVENTOR:
            return self.host.send_chat(recommendation.command, 2)

        final_result = None
        if recommendation.damage_type != 9:
            final_result = self.host.send_chat("!gi bolt 4", 2)
        target_result = self.host.send_chat(recommendation.command, 2)
        return target_result if target_result is not None else final_result

    def _run_gi_canister_loop(self):
        while not self.canister_stop.is_set():
            target_name = (self.current_target or "").strip()
            if target_name:
                command = self._gi_canister_command_for_target(target_name)
                try:
                    result = self.host.send_chat(command, 2)
                    if result["success"]:
                        if self.last_canister_error_key:
                            self.host.emit(
                                "info",
                                f"{self.host.client.display_name}: GI canister loop recovered",
                                script_id=self.script_id,
                            )
                            self.last_canister_error_key = ""
                    else:
                        error_key = f"send:{result['rc']}:{result['err']}"
                        if error_key != self.last_canister_error_key:
                            self.last_canister_error_key = error_key
                            self.host.emit(
                                "error",
                                (
                                    f"{self.host.client.display_name}: GI canister send failed target='{target_name}' "
                                    f"command={command} rc={result['rc']} err={result['err']}"
                                ),
                                script_id=self.script_id,
                            )
                except Exception as exc:
                    error_key = f"exc:{type(exc).__name__}:{exc}"
                    if error_key != self.last_canister_error_key:
                        self.last_canister_error_key = error_key
                        self.host.emit(
                            "error",
                            f"{self.host.client.display_name}: GI canister send failed: {exc}",
                            script_id=self.script_id,
                        )

            if self.canister_stop.wait(self._canister_cooldown_seconds()):
                break

    def _gi_canister_command_for_target(self, target_name: str) -> str:
        lowered = target_name.strip().lower()
        if lowered in self.SMALL_FETCH_TARGETS:
            return "!gi canister 4"
        return "!gi canister 2"

    def _canister_cooldown_seconds(self) -> float:
        return max(float(self.config.get("canister_cooldown_seconds", 6.1)), 0.1)

    def _observe_slinger_line(self, text: str):
        now = time.monotonic()
        self._cleanup_slinger_states(now)
        self._refresh_slinger_state_timeouts(now)

        breach = hgx_combat.parse_breach_line(text)
        if breach is not None:
            state = self._get_slinger_state(breach.target)
            state.last_seen_at = now
            if not state.breach_confirmed:
                state.breach_confirmed = True
                state.breach_done = True
                state.breach_pending_until = 0.0
                self.host.emit(
                    "info",
                    (
                        f"{self.host.client.display_name}: Divine Slinger breach confirmed "
                        f"target='{breach.target}' effect='{breach.effect}'"
                    ),
                    script_id=self.script_id,
                )

        if self.current_secondary_mode == "blind" and hgx_combat.has_target_blind_marker(text):
            target_name = (self.current_target or "").strip()
            if target_name:
                state = self._get_slinger_state(target_name)
                state.last_seen_at = now
                if not state.blind_confirmed:
                    state.blind_confirmed = True
                    state.blind_done = True
                    state.blind_pending_until = 0.0
                    self.host.emit(
                        "info",
                        f"{self.host.client.display_name}: Divine Slinger blind marker observed for '{target_name}'",
                        script_id=self.script_id,
                    )

    def _handle_slinger_attack(self, attack):
        self.current_target = attack.defender
        recommendation = self._recommend_for_target(attack.defender)
        if recommendation is None:
            self.set_status(f"No data: {attack.defender}")
            defender_key = attack.defender.lower()
            if defender_key not in self.unknown_targets:
                self.unknown_targets.add(defender_key)
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: {self._mode_label()} has no characters.d entry for '{attack.defender}'",
                    script_id=self.script_id,
                )
            return

        now = time.monotonic()
        self._cleanup_slinger_states(now)
        self._refresh_slinger_state_timeouts(now)
        state = self._get_slinger_state(attack.defender)
        state.last_seen_at = now

        wants_breach = hgx_data.is_slinger_breach_target(attack.defender)
        wants_blind = hgx_data.is_slinger_blind_target(attack.defender)

        if self.current_damage_type != recommendation.damage_type:
            self.set_status(f"{attack.defender}: select {recommendation.selection_name}")
            self._send_slinger_command(
                attack.defender,
                recommendation,
                recommendation.command,
                "base damage",
                new_secondary_mode="damage",
                new_damage_type=recommendation.damage_type,
            )
            return

        if wants_breach and not state.breach_done:
            if state.breach_pending_until > now:
                self.set_status(f"{attack.defender}: {recommendation.selection_name} + breach")
                return
            if self.current_secondary_mode == "breach":
                state.last_breach_command_at = now
                state.breach_pending_until = now + self.SLINGER_PENDING_SECONDS
                self.set_status(f"{attack.defender}: {recommendation.selection_name} + breach")
                return
            if self.current_secondary_mode != "breach":
                self.set_status(f"{attack.defender}: {recommendation.selection_name} -> breach")
                if self._send_slinger_command(
                    attack.defender,
                    recommendation,
                    hgx_data.SLINGER_BREACH_COMMAND,
                    "breach",
                    new_secondary_mode="breach",
                ):
                    state.last_breach_command_at = now
                    state.breach_pending_until = now + self.SLINGER_PENDING_SECONDS
                return

        if wants_blind and (not wants_breach or state.breach_done) and not state.blind_done:
            if state.blind_pending_until > now:
                self.set_status(f"{attack.defender}: {recommendation.selection_name} + blind")
                return
            if self.current_secondary_mode == "blind":
                state.last_blind_command_at = now
                state.blind_pending_until = now + self.SLINGER_PENDING_SECONDS
                self.set_status(f"{attack.defender}: {recommendation.selection_name} + blind")
                return
            if self.current_secondary_mode != "blind":
                self.set_status(f"{attack.defender}: {recommendation.selection_name} -> blind")
                if self._send_slinger_command(
                    attack.defender,
                    recommendation,
                    hgx_data.SLINGER_BLIND_COMMAND,
                    "blind",
                    new_secondary_mode="blind",
                ):
                    state.last_blind_command_at = now
                    state.blind_pending_until = now + self.SLINGER_PENDING_SECONDS
                return

        if self.current_secondary_mode != "damage":
            self.set_status(f"{attack.defender}: {recommendation.selection_name} + damage")
            self._send_slinger_command(
                attack.defender,
                recommendation,
                recommendation.command,
                "restore damage",
                new_secondary_mode="damage",
                new_damage_type=recommendation.damage_type,
            )
            return

        status_suffix = "damage"
        if wants_breach and state.breach_confirmed:
            status_suffix = "breached"
        if wants_blind and state.blind_confirmed:
            status_suffix = "blinded"
        self.set_status(f"{attack.defender}: {recommendation.selection_name} + {status_suffix}")

    def _send_slinger_command(
        self,
        target_name: str,
        recommendation,
        command: str,
        action_label: str,
        *,
        new_secondary_mode: str,
        new_damage_type: Optional[int] = None,
    ) -> bool:
        try:
            result = self.host.send_chat(command, 2)
        except Exception as exc:
            self.set_status(f"{target_name}: {action_label} failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: Divine Slinger chat send failed for {command}: {exc}",
                script_id=self.script_id,
            )
            return False

        if result["success"]:
            self.current_secondary_mode = new_secondary_mode
            if new_damage_type is not None:
                self.current_damage_type = new_damage_type
        else:
            self.set_status(f"{target_name}: {action_label} failed")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: Divine Slinger target='{target_name}' action={action_label} "
                f"command={command} base={recommendation.selection_name} expected={recommendation.expected_damage} "
                f"paragon={recommendation.paragon_ranks} success={result['success']} rc={result['rc']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", False)):
            self.host.send_console(
                (
                    f"SimKeys Divine Slinger {target_name} -> {action_label} "
                    f"({command}, base {recommendation.selection_name}) rc={result['rc']} err={result['err']}"
                )
            )
        return bool(result["success"])

    def _get_slinger_state(self, target_name: str) -> SlingerTargetState:
        key = str(target_name or "").strip().lower()
        state = self.slinger_states.get(key)
        if state is None:
            state = SlingerTargetState(display_name=str(target_name or "").strip())
            self.slinger_states[key] = state
        return state

    def _cleanup_slinger_states(self, now: float):
        cutoff = now - self.SLINGER_STATE_TTL_SECONDS
        stale_keys = [key for key, state in self.slinger_states.items() if state.last_seen_at and state.last_seen_at < cutoff]
        for key in stale_keys:
            self.slinger_states.pop(key, None)

    def _refresh_slinger_state_timeouts(self, now: float):
        for state in self.slinger_states.values():
            if state.breach_pending_until and now >= state.breach_pending_until:
                state.breach_pending_until = 0.0
                if not state.breach_done:
                    state.breach_done = True
                    self.host.emit(
                        "info",
                        (
                            f"{self.host.client.display_name}: Divine Slinger breach window elapsed "
                            f"for '{state.display_name}', returning to the next stage"
                        ),
                        script_id=self.script_id,
                    )
            if state.blind_pending_until and now >= state.blind_pending_until:
                state.blind_pending_until = 0.0
                if not state.blind_done:
                    state.blind_done = True
                    self.host.emit(
                        "info",
                        (
                            f"{self.host.client.display_name}: Divine Slinger blind window elapsed "
                            f"for '{state.display_name}', returning to extra damage"
                        ),
                        script_id=self.script_id,
                    )


class AutoActionScript(ClientScriptBase):
    script_id = "auto_action"
    MODE_CONFIG = {
        "Called Shot": ("!action cs opponent", "Called Shot"),
        "Knockdown": ("!action kd opponent", "Knockdown"),
        "Disarm": ("!action dis opponent", "Disarm"),
    }

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.loop_thread = None
        self.loop_stop = threading.Event()
        self.last_error_key = ""

    def needs_chat_feed(self) -> bool:
        return False

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.loop_stop = threading.Event()
        self.last_error_key = ""
        self.loop_thread = threading.Thread(
            target=self._run_loop,
            name=f"AutoAction-{self.client.pid}",
            daemon=True,
        )
        self.loop_thread.start()
        self.set_status(f"{self._mode_label()} every {self._cooldown_seconds():.1f}s")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: Auto Action started ({self._mode_label()} every {self._cooldown_seconds():.1f}s)",
            script_id=self.script_id,
        )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.loop_stop.set()
        self.host.emit("info", f"{self.host.client.display_name}: Auto Action stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        return

    def _run_loop(self):
        while not self.loop_stop.is_set():
            command = self._command_text()
            try:
                result = self.host.send_chat(command, 2)
                if result["success"]:
                    if self.last_error_key:
                        self.host.emit(
                            "info",
                            f"{self.host.client.display_name}: Auto Action recovered",
                            script_id=self.script_id,
                        )
                        self.last_error_key = ""
                else:
                    error_key = f"send:{result['rc']}:{result['err']}"
                    if error_key != self.last_error_key:
                        self.last_error_key = error_key
                        self.host.emit(
                            "error",
                            (
                                f"{self.host.client.display_name}: Auto Action send failed command={command} "
                                f"rc={result['rc']} err={result['err']}"
                            ),
                            script_id=self.script_id,
                        )
            except Exception as exc:
                error_key = f"exc:{type(exc).__name__}:{exc}"
                if error_key != self.last_error_key:
                    self.last_error_key = error_key
                    self.host.emit(
                        "error",
                        f"{self.host.client.display_name}: Auto Action chat send failed: {exc}",
                        script_id=self.script_id,
                    )

            if self.loop_stop.wait(self._cooldown_seconds()):
                break

    def _mode_label(self) -> str:
        mode = str(self.config.get("mode", "Called Shot")).strip()
        if mode in self.MODE_CONFIG:
            return mode
        return "Called Shot"

    def _command_text(self) -> str:
        return self.MODE_CONFIG[self._mode_label()][0]

    def _cooldown_seconds(self) -> float:
        return max(float(self.config.get("cooldown_seconds", 6.2)), 0.1)


class AutoRSMScript(ClientScriptBase):
    script_id = "auto_rsm"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.process_handle = None
        self.rsm_address = 0
        self.cooldown_until = 0.0
        self.identity_wait_logged = False
        self.last_probe_error = ""

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.rsm_address = 0
        self.cooldown_until = 0.0
        self.identity_wait_logged = False
        self.last_probe_error = ""
        try:
            address = self._resolve_rsm_address()
            self.set_status(f"Armed 0x{address:08X}")
        except Exception:
            self.set_status("Armed (probe pending)")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: Auto RSM started",
            script_id=self.script_id,
        )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self._close_process_handle()
        self.host.emit("info", f"{self.host.client.display_name}: Auto RSM stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return

        attack = hgx_combat.parse_attack_line(text)
        if attack is None:
            return

        character_name = self._character_name()
        if not character_name:
            self.set_status("Waiting for character name")
            if not self.identity_wait_logged:
                self.identity_wait_logged = True
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Auto RSM is waiting for character identity before parsing attack lines",
                    script_id=self.script_id,
                )
            return

        if attack.attacker.lower() != character_name.lower():
            return

        now = time.monotonic()
        if now < self.cooldown_until:
            return

        try:
            rsm_status = self._read_rsm_status()
        except Exception as exc:
            error_text = str(exc)
            self.set_status("Probe failed")
            if error_text != self.last_probe_error:
                self.last_probe_error = error_text
                self.host.emit(
                    "error",
                    f"{self.host.client.display_name}: Auto RSM memory probe failed: {error_text}",
                    script_id=self.script_id,
                )
            return

        if self.last_probe_error:
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: Auto RSM memory probe recovered",
                script_id=self.script_id,
            )
            self.last_probe_error = ""

        if rsm_status != 0:
            self.set_status(f"RSM active ({rsm_status})")
            return

        try:
            result = self.host.send_chat("!action rsm self", 2)
        except Exception as exc:
            self.set_status("Trigger failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: Auto RSM chat send failed: {exc}",
                script_id=self.script_id,
            )
            self.cooldown_until = now + 1.0
            return

        self.cooldown_until = now + self._cooldown_seconds()
        if result["success"]:
            self.set_status(f"Triggered ({self._cooldown_seconds():.1f}s)")
        else:
            self.set_status("Trigger failed")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: Auto RSM triggered on '{attack.defender}' "
                f"success={result['success']} rc={result['rc']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", False)):
            self.host.send_console(
                f"SimKeys Auto RSM -> !action rsm self rc={result['rc']} err={result['err']}"
            )

    def _character_name(self) -> str:
        live_name = (self.host.client.character_name or "").strip()
        if live_name:
            return hgx_combat.strip_player_level_suffix(live_name)
        cached_name = (self.client.character_name or "").strip()
        if cached_name:
            return hgx_combat.strip_player_level_suffix(cached_name)
        return ""

    def _close_process_handle(self):
        handle = self.process_handle
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            _kernel32.CloseHandle(handle)
        self.process_handle = None

    def _ensure_process_handle(self):
        handle = self.process_handle
        if handle not in (None, 0, INVALID_HANDLE_VALUE):
            return handle

        handle = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(self.client.pid))
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            raise OSError(simkeys.winerr(f"OpenProcess failed for pid {self.client.pid}"))
        self.process_handle = handle
        return handle

    def _read_u32(self, address: int) -> int:
        handle = self._ensure_process_handle()
        value = C.c_uint32()
        size = C.c_size_t()
        if not _kernel32.ReadProcessMemory(handle, W.LPCVOID(address), C.byref(value), C.sizeof(value), C.byref(size)):
            raise OSError(simkeys.winerr(f"ReadProcessMemory(u32, 0x{address:08X}) failed"))
        return int(value.value)

    def _read_u8(self, address: int) -> int:
        handle = self._ensure_process_handle()
        value = C.c_ubyte()
        size = C.c_size_t()
        if not _kernel32.ReadProcessMemory(handle, W.LPCVOID(address), C.byref(value), C.sizeof(value), C.byref(size)):
            raise OSError(simkeys.winerr(f"ReadProcessMemory(u8, 0x{address:08X}) failed"))
        return int(value.value)

    def _resolve_rsm_address(self) -> int:
        if self.rsm_address:
            return self.rsm_address

        module_base = int((self.client.query or {}).get("module_base", 0)) or kLegacyImageBase
        pointer_holder_address = module_base + kLegacyHpPointerOffset
        pointer_holder = self._read_u32(pointer_holder_address)
        if pointer_holder == 0:
            raise RuntimeError(f"RSM pointer at 0x{pointer_holder_address:08X} was null")

        self.rsm_address = pointer_holder + 0x188
        self.host.emit(
            "info",
            (
                f"{self.client.display_name}: RSM path resolved module=0x{module_base:08X} "
                f"pointer=0x{pointer_holder_address:08X} holder=0x{pointer_holder:08X} "
                f"rsm=0x{self.rsm_address:08X}"
            ),
            script_id=self.script_id,
        )
        return self.rsm_address

    def _read_rsm_status(self) -> int:
        return self._read_u8(self._resolve_rsm_address())

    def _cooldown_seconds(self) -> float:
        return max(float(self.config.get("cooldown_seconds", 7.0)), 0.1)


class ClientScriptHost:
    def __init__(self, client, event_callback: Callable[[dict], None]):
        self.client = client
        self.event_callback = event_callback
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.scripts: Dict[str, ClientScriptBase] = {}
        self.latest_sequence = 0

    def emit(self, level: str, message: str, script_id: Optional[str] = None):
        self.event_callback({
            "type": "log",
            "level": level,
            "client_pid": self.client.pid,
            "client_name": self.client.display_name,
            "script_id": script_id,
            "message": message,
        })

    def notify_state_changed(self):
        with self.lock:
            payload = {
                "type": "script-state",
                "client_pid": self.client.pid,
                "states": {
                    script_id: {
                        "running": True,
                        "status": script.status_text,
                    }
                    for script_id, script in self.scripts.items()
                },
            }
        self.event_callback(payload)

    def start_script(self, definition: ScriptDefinition, config: Dict[str, object]):
        with self.lock:
            if definition.script_id in self.scripts:
                raise RuntimeError(f"{definition.name} is already running for pid {self.client.pid}.")
            script = definition.factory(self.client, config, self)
            self.scripts[definition.script_id] = script
            if self.thread is None or not self.thread.is_alive():
                self.stop_event = threading.Event()
                self.thread = threading.Thread(target=self._run, name=f"SimKeysHost-{self.client.pid}", daemon=True)
                self.thread.start()
            script.on_start()
        self.notify_state_changed()

    def stop_script(self, script_id: str):
        with self.lock:
            script = self.scripts.pop(script_id, None)
            if script is None:
                return
            script.on_stop()
            if not self.scripts:
                self.stop_event.set()
        self.notify_state_changed()

    def is_running(self, script_id: str) -> bool:
        with self.lock:
            return script_id in self.scripts

    def get_state(self, script_id: str) -> dict:
        with self.lock:
            script = self.scripts.get(script_id)
            if script is None:
                return {"running": False, "status": "Stopped"}
            return {"running": True, "status": script.status_text}

    def running_script_ids(self) -> List[str]:
        with self.lock:
            return sorted(self.scripts.keys())

    def trigger_slot(self, slot: int, page: int = 0):
        return runtime.trigger_slot(self.client, slot, page=page)

    def format_slot(self, page: int, slot: int) -> str:
        if page <= 0:
            return f"F{slot}"
        if page == 1:
            return f"Shift+F{slot}"
        if page == 2:
            return f"Ctrl+F{slot}"
        return f"page {page} slot {slot}"

    def send_console(self, text: str):
        return runtime.send_chat(self.client, f"##{text}", 2)

    def send_chat(self, text: str, mode: int = 2):
        return runtime.send_chat(self.client, text, mode)

    def _chat_poll_once(self, after: int, max_lines: int):
        pipe = runtime.open_pipe(self.client.pid, timeout_ms=750)
        try:
            return simkeys.chat_poll(pipe, after=after, max_lines=max_lines)
        finally:
            pipe.close()

    def _run(self):
        after = 0
        initialized = False
        last_poll_error = ""
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    scripts = list(self.scripts.values())
                    if not scripts:
                        break
                    chat_scripts = [script for script in scripts if script.needs_chat_feed()]

                if not chat_scripts:
                    initialized = False
                    self.stop_event.wait(0.25)
                    continue

                poll_interval = min(script.get_poll_interval() for script in chat_scripts)
                max_lines = max(script.get_max_lines() for script in chat_scripts)

                try:
                    request_after = 0 if not initialized else after
                    request_max_lines = 1 if not initialized else max_lines
                    polled = self._chat_poll_once(request_after, request_max_lines)
                except Exception as exc:
                    error_text = str(exc)
                    if error_text != last_poll_error:
                        self.emit("error", f"{self.client.display_name}: chat poll failed: {error_text}")
                        last_poll_error = error_text
                    self.stop_event.wait(max(poll_interval, 0.25))
                    continue

                if last_poll_error:
                    self.emit("info", f"{self.client.display_name}: chat poll recovered")
                    last_poll_error = ""

                after = polled["latest_seq"]
                self.latest_sequence = after
                if not initialized:
                    initialized = True
                    self.emit("info", f"{self.client.display_name}: host connected at seq {after}")
                    self.stop_event.wait(poll_interval)
                    continue

                if polled["lines"]:
                    for line in polled["lines"]:
                        with self.lock:
                            current_scripts = [script for script in self.scripts.values() if script.needs_chat_feed()]
                        for script in current_scripts:
                            try:
                                script.on_chat_line(line["seq"], line["text"])
                            except Exception as exc:
                                script.set_status(f"Error: {exc}")
                                self.emit(
                                    "error",
                                    f"{self.client.display_name}: {type(exc).__name__}: {exc}",
                                    script_id=getattr(script, "script_id", None),
                                )
                self.stop_event.wait(poll_interval)
        except Exception as exc:
            self.emit("error", f"{self.client.display_name}: host stopped after error: {exc}")
        finally:
            with self.lock:
                self.scripts.clear()
                self.thread = None
            self.notify_state_changed()
            self.emit("info", f"{self.client.display_name}: host disconnected")


class ScriptManager:
    def __init__(self, event_callback: Callable[[dict], None]):
        self.event_callback = event_callback
        self.hosts: Dict[int, ClientScriptHost] = {}
        self.registry: Dict[str, ScriptDefinition] = {}
        self._register_defaults()

    def _register_defaults(self):
        autodrink = ScriptDefinition(
            script_id="autodrink",
            name="AutoDrink",
            description="GUI-controlled rewrite of HGX AutoDrink.py.old: when running, watch combat log lines, sample HP, and drink from the configured quickbar slot when HP falls to the chosen percentage.",
            fields=[
                ScriptField("slot", "Slot", "int", 2, minimum=1, maximum=12, step=1, width=4),
                ScriptField("page", "Bank", "int", 0, minimum=0, maximum=2, step=1, width=4),
                ScriptField("threshold_percent", "HP %", "float", 80.0, minimum=1.0, maximum=100.0, step=1.0, width=6),
                ScriptField("cooldown_seconds", "Cooldown", "float", 3.0, minimum=0.1, maximum=10.0, step=0.1, width=6),
                ScriptField("lock_target", "Lock", "bool", True),
                ScriptField("resume_attack", "Resume", "bool", True),
                ScriptField("poll_interval", "Poll", "float", 0.20, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 20, minimum=1, maximum=200, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", True),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AutoDrinkScript,
        )
        self.registry[autodrink.script_id] = autodrink

        auto_aa = ScriptDefinition(
            script_id="auto_aa",
            name="Auto Damage",
            description="GUI-controlled rewrite of HGX auto_aa_2.4.py and auto_slinger.py.old: switch Arcane Archer, Zen Ranger, Divine Slinger, or Gnomish Inventor damage modes from HGX-formatted combat log lines without relying on in-game toggles.",
            fields=[
                ScriptField(
                    "mode",
                    "Mode",
                    "choice",
                    AutoAAScript.MODE_ARCANE_ARCHER,
                    choices=[
                        AutoAAScript.MODE_ARCANE_ARCHER,
                        AutoAAScript.MODE_ZEN_RANGER,
                        AutoAAScript.MODE_DIVINE_SLINGER,
                        AutoAAScript.MODE_GNOMISH_INVENTOR,
                    ],
                    width=18,
                ),
                ScriptField("elemental_dice", "Dice", "int", 10, minimum=1, maximum=30, step=1, width=5),
                ScriptField("auto_canister", "Canister", "bool", True),
                ScriptField("canister_cooldown_seconds", "Can CD", "float", 6.1, minimum=0.1, maximum=30.0, step=0.1, width=6),
                ScriptField("poll_interval", "Poll", "float", 0.10, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 60, minimum=1, maximum=200, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", False),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AutoAAScript,
        )
        self.registry[auto_aa.script_id] = auto_aa

        auto_action = ScriptDefinition(
            script_id="auto_action",
            name="Auto Action",
            description="GUI-controlled rewrite of HGX auto_action.py: repeatedly issue one selected combat action without requiring an in-game toggle.",
            fields=[
                ScriptField("mode", "Mode", "choice", "Called Shot", choices=["Called Shot", "Knockdown", "Disarm"], width=12),
                ScriptField("cooldown_seconds", "Cooldown", "float", 6.2, minimum=0.1, maximum=30.0, step=0.1, width=6),
            ],
            factory=AutoActionScript,
        )
        self.registry[auto_action.script_id] = auto_action

        auto_rsm = ScriptDefinition(
            script_id="auto_rsm",
            name="Auto RSM",
            description="GUI-controlled rewrite of HGX autoRSM.py: when you attack and the RSM status byte is not active, send '!action rsm self' through the unfocused chat path.",
            fields=[
                ScriptField("cooldown_seconds", "Cooldown", "float", 7.0, minimum=0.1, maximum=30.0, step=0.1, width=6),
                ScriptField("poll_interval", "Poll", "float", 0.10, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 60, minimum=1, maximum=200, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", False),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AutoRSMScript,
        )
        self.registry[auto_rsm.script_id] = auto_rsm

    def definitions(self) -> List[ScriptDefinition]:
        return list(self.registry.values())

    def default_config(self, script_id: str) -> Dict[str, object]:
        definition = self.registry[script_id]
        return {field.key: deepcopy(field.default) for field in definition.fields}

    def _get_or_create_host(self, client) -> ClientScriptHost:
        host = self.hosts.get(client.pid)
        if host is None:
            host = ClientScriptHost(client, self.event_callback)
            self.hosts[client.pid] = host
        else:
            host.client = client
        return host

    def sync_client(self, client):
        host = self.hosts.get(client.pid)
        if host is None:
            return
        host.client = client
        with host.lock:
            for script in host.scripts.values():
                script.client = client

    def start_script(self, client, script_id: str, config: Dict[str, object]):
        definition = self.registry[script_id]
        host = self._get_or_create_host(client)
        host.start_script(definition, config)

    def stop_script(self, client_pid: int, script_id: str):
        host = self.hosts.get(client_pid)
        if host is None:
            return
        host.stop_script(script_id)
        if host.running_script_ids():
            return
        self.hosts.pop(client_pid, None)

    def stop_all_for_client(self, client_pid: int):
        host = self.hosts.get(client_pid)
        if host is None:
            return
        for script_id in host.running_script_ids():
            host.stop_script(script_id)
        if not host.running_script_ids():
            self.hosts.pop(client_pid, None)

    def get_state(self, client_pid: int, script_id: str) -> dict:
        host = self.hosts.get(client_pid)
        if host is None:
            return {"running": False, "status": "Stopped"}
        return host.get_state(script_id)

    def running_script_count(self, client_pid: int) -> int:
        host = self.hosts.get(client_pid)
        if host is None:
            return 0
        return len(host.running_script_ids())

    def stop_all(self):
        for pid in list(self.hosts.keys()):
            self.stop_all_for_client(pid)
