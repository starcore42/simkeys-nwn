import threading
import time
import ctypes as C
import ctypes.wintypes as W
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import simKeys_Client as simkeys
from . import simkeys_hgx_combat as hgx_combat
from . import simkeys_hgx_data as hgx_data
from . import simkeys_runtime as runtime

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

WEAPON_SLOT_NONE = "-"
WEAPON_CURRENT_UNKNOWN = "Unknown"
WEAPON_CURRENT_UNARMED = "Unarmed"
WEAPON_BINDING_KEYS = tuple(f"W{index}" for index in range(1, 7))
WEAPON_ELEMENTAL_TYPES = frozenset({3, 4, 5, 6, 7})
WEAPON_EXOTIC_TYPES = frozenset({8, 9, 10, 11})
_DAMAGE_TYPE_LABEL_BY_ID = {
    value: name.replace("raw", "raw ").replace("negative", "negative energy").replace("positive", "positive energy").title()
    for name, value in hgx_data.DAMAGE_TYPE_NAME_TO_ID.items()
}
_DAMAGE_TYPE_LABEL_BY_ID.update({
    5: "Electrical",
    9: "Magical",
    10: "Negative",
    11: "Positive",
})


def _build_quickbar_slot_choices() -> List[str]:
    values = [WEAPON_SLOT_NONE]
    values.extend(f"F{slot}" for slot in range(1, 13))
    values.extend(f"S+F{slot}" for slot in range(1, 13))
    values.extend(f"C+F{slot}" for slot in range(1, 13))
    return values


WEAPON_SLOT_CHOICES = _build_quickbar_slot_choices()
WEAPON_CURRENT_CHOICES = [WEAPON_CURRENT_UNKNOWN, *WEAPON_BINDING_KEYS]


def _parse_quickbar_slot_choice(value: object) -> Optional[Tuple[int, int]]:
    text = str(value or "").strip().upper()
    if not text or text == WEAPON_SLOT_NONE:
        return None
    if text.startswith("S+F"):
        return 1, int(text[3:])
    if text.startswith("C+F"):
        return 2, int(text[3:])
    if text.startswith("F"):
        return 0, int(text[1:])
    return None


def _parse_quickbar_bank_page(value: object) -> int:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "base", "normal", "0"):
            return 0
        if text in ("shift", "1"):
            return 1
        if text in ("control", "ctrl", "2"):
            return 2
    return int(value)


def _format_damage_type_label(damage_type: int) -> str:
    if damage_type in _DAMAGE_TYPE_LABEL_BY_ID:
        return _DAMAGE_TYPE_LABEL_BY_ID[damage_type]
    if damage_type in hgx_data.AA_TYPE_TO_WORD:
        return hgx_data.AA_TYPE_TO_WORD[damage_type].title()
    return hgx_data.GI_TYPE_TO_WORD.get(damage_type, str(damage_type)).title()


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


@dataclass(frozen=True)
class WeaponFamilyConfig:
    family_key: str
    elemental_count: int
    elemental_dice: int
    exotic_count: int
    exotic_dice: int


@dataclass(frozen=True)
class WeaponBinding:
    key: str
    choice: str
    page: int
    slot: int
    label: str


@dataclass
class WeaponLearningProfile:
    binding: WeaponBinding
    observations: int = 0
    attack_attempts: int = 0
    last_seen_at: float = 0.0
    last_attack_at: float = 0.0
    locked_family_key: str = ""
    locked_elemental: Tuple[int, ...] = ()
    locked_exotic: Tuple[int, ...] = ()
    mismatch_streak: int = 0
    mismatch_total: int = 0
    last_mismatch_at: float = 0.0
    rediscoveries: int = 0
    elemental_counts: Dict[int, int] = field(default_factory=dict)
    exotic_counts: Dict[int, int] = field(default_factory=dict)
    elemental_max_amounts: Dict[int, int] = field(default_factory=dict)
    exotic_max_amounts: Dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WeaponRecommendation:
    binding: WeaponBinding
    expected_damage: int
    matched_name: str
    paragon_ranks: int
    family_label: str
    learned_elemental: Tuple[int, ...]
    learned_exotic: Tuple[int, ...]
    healing_types: Tuple[int, ...]
    missing_elemental: int
    missing_exotic: int


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

    def get_state_details(self) -> dict:
        return {}


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
        page = _parse_quickbar_bank_page(self.config.get("page", 0))
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
    MODE_WEAPON_SWAP = "Weapon Swap"
    MAX_WEAPON_BINDINGS = len(WEAPON_BINDING_KEYS)
    WEAPON_FAMILY_CONFIG = {
        "DB": WeaponFamilyConfig("DB", elemental_count=3, elemental_dice=5, exotic_count=3, exotic_dice=2),
        "P1": WeaponFamilyConfig("P1", elemental_count=1, elemental_dice=9, exotic_count=2, exotic_dice=6),
        "XR": WeaponFamilyConfig("XR", elemental_count=2, elemental_dice=11, exotic_count=1, exotic_dice=8),
    }
    WEAPON_FAMILY_BY_SIGNATURE = {
        (config.elemental_count, config.exotic_count): family_key
        for family_key, config in WEAPON_FAMILY_CONFIG.items()
    }
    WEAPON_LEARNING_ATTACKS_BEFORE_ROTATE = 3
    WEAPON_REDISCOVERY_MISMATCH_THRESHOLD = 8
    WEAPON_EQUIPPED_PROBE_INTERVAL_SECONDS = 0.50
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
        self.weapon_bindings: Dict[str, WeaponBinding] = {}
        self.weapon_profiles: Dict[str, WeaponLearningProfile] = {}
        self.current_weapon_key = ""
        self.pending_weapon_key = ""
        self.pending_weapon_ready_at = 0.0
        self.pending_weapon_requested_at = 0.0
        self.pending_weapon_request_sequence = 0
        self.pending_weapon_feedback_seen = False
        self.pending_weapon_equipped_feedback_seen = False
        self.pending_weapon_feedback_sequence = 0
        self.pending_weapon_conceal_seen = False
        self.pending_weapon_conceal_sequence = 0
        self.pending_weapon_ignored_damage_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_last_equipped_mask = 0
        self.weapon_equipped_key = ""
        self.weapon_equipped_keys: Tuple[str, ...] = ()
        self.weapon_equipped_probe_at = 0.0
        self.weapon_equipped_probe_error = ""
        self.weapon_equipped_probe_error_logged = False
        self.weapon_unarmed_observations = 0
        self.current_chat_sequence = 0
        self.weapon_attack_seen_count = 0
        self.weapon_attack_matched_count = 0
        self.weapon_damage_seen_count = 0
        self.weapon_damage_matched_count = 0
        self.weapon_damage_parse_miss_count = 0
        self.weapon_last_ignored_attack_actor = ""
        self.weapon_last_ignored_damage_actor = ""
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
        self.weapon_bindings = {}
        self.weapon_profiles = {}
        self.current_weapon_key = ""
        self.pending_weapon_key = ""
        self.pending_weapon_ready_at = 0.0
        self.pending_weapon_requested_at = 0.0
        self.pending_weapon_request_sequence = 0
        self.pending_weapon_feedback_seen = False
        self.pending_weapon_equipped_feedback_seen = False
        self.pending_weapon_feedback_sequence = 0
        self.pending_weapon_conceal_seen = False
        self.pending_weapon_conceal_sequence = 0
        self.pending_weapon_ignored_damage_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_last_equipped_mask = 0
        self.weapon_equipped_key = ""
        self.weapon_equipped_keys = ()
        self.weapon_equipped_probe_at = 0.0
        self.weapon_equipped_probe_error = ""
        self.weapon_equipped_probe_error_logged = False
        self.weapon_unarmed_observations = 0
        self.weapon_attack_seen_count = 0
        self.weapon_attack_matched_count = 0
        self.weapon_damage_seen_count = 0
        self.weapon_damage_matched_count = 0
        self.weapon_damage_parse_miss_count = 0
        self.weapon_last_ignored_attack_actor = ""
        self.weapon_last_ignored_damage_actor = ""

        if self._is_weapon_mode():
            self._initialize_weapon_mode()
            current_label = self._binding_display(self.current_weapon_key)
            self.set_status(f"{self._mode_label()} armed ({current_label})")
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: {self._mode_label()} started ({current_label})",
                script_id=self.script_id,
            )
            return

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
        self.weapon_bindings = {}
        self.weapon_profiles = {}
        self.current_weapon_key = ""
        self.pending_weapon_key = ""
        self.pending_weapon_ready_at = 0.0
        self.pending_weapon_requested_at = 0.0
        self.pending_weapon_request_sequence = 0
        self.pending_weapon_feedback_seen = False
        self.pending_weapon_equipped_feedback_seen = False
        self.pending_weapon_feedback_sequence = 0
        self.pending_weapon_conceal_seen = False
        self.pending_weapon_conceal_sequence = 0
        self.pending_weapon_ignored_damage_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_last_equipped_mask = 0
        self.weapon_equipped_key = ""
        self.weapon_equipped_keys = ()
        self.weapon_equipped_probe_at = 0.0
        self.weapon_equipped_probe_error = ""
        self.weapon_equipped_probe_error_logged = False
        self.weapon_unarmed_observations = 0
        self.canister_stop.set()
        self.host.emit("info", f"{self.host.client.display_name}: {self._mode_label()} stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return
        self.current_chat_sequence = int(sequence)

        if self._is_weapon_mode():
            self._observe_weapon_swap_feedback(text)
            self._observe_weapon_damage_line(text)

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

        if self._is_weapon_mode():
            self.weapon_attack_seen_count += 1

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

        character_key = hgx_combat.normalize_actor_name(character_name).lower()
        if attack.attacker.lower() != character_key:
            if self._is_weapon_mode():
                self.weapon_last_ignored_attack_actor = attack.attacker
                self.host.notify_state_changed()
            return

        if self._is_weapon_mode():
            self.weapon_attack_matched_count += 1
            self._observe_pending_conceal_attack(attack)

        if self._mode_label() == self.MODE_GNOMISH_INVENTOR:
            self.current_target = attack.defender

        if self._mode_label() == self.MODE_DIVINE_SLINGER:
            self._handle_slinger_attack(attack)
            return

        if self._is_weapon_mode():
            self._handle_weapon_attack(attack)
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
            return live_name
        cached_name = (self.client.character_name or "").strip()
        if cached_name:
            return cached_name
        return ""

    def _mode_label(self) -> str:
        mode = str(self.config.get("mode", self.MODE_ARCANE_ARCHER)).strip()
        if mode in (
            self.MODE_ARCANE_ARCHER,
            self.MODE_ZEN_RANGER,
            self.MODE_DIVINE_SLINGER,
            self.MODE_GNOMISH_INVENTOR,
            self.MODE_WEAPON_SWAP,
        ):
            return mode
        return self.MODE_ARCANE_ARCHER

    def _is_weapon_mode(self) -> bool:
        return self._mode_label() == self.MODE_WEAPON_SWAP

    def _weapon_binding_keys(self) -> Tuple[str, ...]:
        if not self._is_weapon_mode():
            return ()
        return WEAPON_BINDING_KEYS[:self.MAX_WEAPON_BINDINGS]

    def _initialize_weapon_mode(self):
        if not self._is_weapon_mode():
            return

        bindings: Dict[str, WeaponBinding] = {}
        used_choices: Dict[str, str] = {}
        for index, binding_key in enumerate(self._weapon_binding_keys(), start=1):
            choice = str(self.config.get(f"weapon_slot_{index}", WEAPON_SLOT_NONE)).strip() or WEAPON_SLOT_NONE
            if choice == WEAPON_SLOT_NONE:
                continue

            parsed = _parse_quickbar_slot_choice(choice)
            if parsed is None:
                raise RuntimeError(f"{binding_key} uses an invalid quickbar selector: {choice}")
            if choice in used_choices:
                raise RuntimeError(f"{binding_key} duplicates {used_choices[choice]} ({choice}).")

            page, slot = parsed
            bindings[binding_key] = WeaponBinding(
                key=binding_key,
                choice=choice,
                page=page,
                slot=slot,
                label=self.host.format_slot(page, slot),
            )
            used_choices[choice] = binding_key

        if not bindings:
            raise RuntimeError(f"{self.MODE_WEAPON_SWAP} requires at least one weapon quickbar slot.")

        current_weapon_key = str(self.config.get("current_weapon", WEAPON_CURRENT_UNKNOWN)).strip() or WEAPON_CURRENT_UNKNOWN
        if current_weapon_key not in (WEAPON_CURRENT_UNKNOWN, WEAPON_CURRENT_UNARMED) and current_weapon_key not in bindings:
            raise RuntimeError(f"Current weapon {current_weapon_key} is not assigned to a quickbar slot.")

        self.weapon_bindings = bindings
        self.weapon_profiles = {
            binding_key: WeaponLearningProfile(binding=binding)
            for binding_key, binding in bindings.items()
        }
        self.current_weapon_key = current_weapon_key

        if len(bindings) < self.MAX_WEAPON_BINDINGS:
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self.MODE_WEAPON_SWAP} is configured with "
                    f"{len(bindings)}/{self.MAX_WEAPON_BINDINGS} weapon slots; missing slots will be skipped"
                ),
                script_id=self.script_id,
            )

    def _binding_display(self, binding_key: str) -> str:
        if binding_key == WEAPON_CURRENT_UNKNOWN:
            return WEAPON_CURRENT_UNKNOWN
        if binding_key == WEAPON_CURRENT_UNARMED:
            return WEAPON_CURRENT_UNARMED
        binding = self.weapon_bindings.get(str(binding_key or "").strip())
        if binding is None:
            return str(binding_key or WEAPON_CURRENT_UNKNOWN)
        return f"{binding.key}/{binding.label}"

    def _clear_pending_weapon_state(self):
        self.pending_weapon_key = ""
        self.pending_weapon_ready_at = 0.0
        self.pending_weapon_requested_at = 0.0
        self.pending_weapon_request_sequence = 0
        self.pending_weapon_feedback_seen = False
        self.pending_weapon_equipped_feedback_seen = False
        self.pending_weapon_feedback_sequence = 0
        self.pending_weapon_conceal_seen = False
        self.pending_weapon_conceal_sequence = 0
        self.pending_weapon_ignored_damage_count = 0

    def _equipped_binding_keys_from_mask(self, mask: int) -> Tuple[str, ...]:
        matches = []
        for binding_key, binding in self.weapon_bindings.items():
            if simkeys.quickbar_mask_has(mask, binding.page, binding.slot):
                matches.append(binding_key)
        return tuple(matches)

    def _query_equipped_binding_keys(self, force: bool = False) -> Tuple[str, ...]:
        now = time.monotonic()
        if (
            not force
            and self.weapon_equipped_probe_at > 0.0
            and now - self.weapon_equipped_probe_at < self.WEAPON_EQUIPPED_PROBE_INTERVAL_SECONDS
        ):
            return self.weapon_equipped_keys

        self.weapon_equipped_probe_at = now
        try:
            state = self.host.query_state()
            mask = int(state.get("quickbar_equipped_mask") or 0)
        except Exception as exc:
            self.weapon_equipped_key = ""
            self.weapon_equipped_keys = ()
            self.weapon_equipped_probe_error = str(exc)
            if not self.weapon_equipped_probe_error_logged:
                self.weapon_equipped_probe_error_logged = True
                self.host.emit(
                    "error",
                    f"{self.host.client.display_name}: {self._mode_label()} equipped-slot query failed: {exc}",
                    script_id=self.script_id,
                )
            self.host.notify_state_changed()
            return ()

        if self.weapon_equipped_probe_error:
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: {self._mode_label()} equipped-slot query recovered",
                script_id=self.script_id,
            )
        self.weapon_equipped_probe_error = ""
        self.weapon_equipped_probe_error_logged = False
        self.weapon_last_equipped_mask = mask

        matches = self._equipped_binding_keys_from_mask(mask)
        self.weapon_equipped_keys = matches
        if len(matches) == 1:
            self.weapon_equipped_key = matches[0]
        elif len(matches) > 1:
            self.weapon_equipped_key = "Multiple"
        else:
            self.weapon_equipped_key = ""
        self.host.notify_state_changed()
        return matches

    def _set_current_weapon_from_equipped_key(self, binding_key: str, reason: str) -> bool:
        if binding_key not in self.weapon_profiles:
            return False
        if self.current_weapon_key == binding_key:
            return False

        previous = self._binding_display(self.current_weapon_key)
        self.current_weapon_key = binding_key
        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} current weapon reconciled "
                f"from {previous} to {self._binding_display(binding_key)} ({reason})"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _reconcile_current_weapon_from_equipped_mask(self, force: bool = False) -> str:
        if self.pending_weapon_key:
            return ""

        matches = self._query_equipped_binding_keys(force=force)
        if len(matches) != 1:
            return ""

        binding_key = matches[0]
        self._set_current_weapon_from_equipped_key(binding_key, "equipped quickbar mask")
        return binding_key

    def _equipped_mask_confirms_binding(self, binding_key: str, force: bool = False) -> Optional[bool]:
        matches = self._query_equipped_binding_keys(force=force)
        if matches:
            return binding_key in matches
        if self.weapon_equipped_probe_at > 0.0 and not self.weapon_equipped_probe_error:
            return False
        return None

    def _cancel_pending_weapon(self, reason: str) -> bool:
        if not self.pending_weapon_key:
            return False

        pending_display = self._binding_display(self.pending_weapon_key)
        self._clear_pending_weapon_state()
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} canceled pending {pending_display} ({reason})",
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _matching_locked_profile_for_observed_types(
        self,
        observed_types: Set[int],
        exclude_key: str = "",
    ) -> Optional[WeaponLearningProfile]:
        if not observed_types:
            return None

        for profile in self.weapon_profiles.values():
            if profile.binding.key == exclude_key:
                continue
            if self._profile_is_locked(profile) and self._observed_types_exactly_match_profile(profile, observed_types):
                return profile
        return None

    def _family_amounts_compatible(self, profile: WeaponLearningProfile, family: WeaponFamilyConfig) -> bool:
        # Critical hits, sneak attacks, and server-side multipliers can inflate
        # component amounts far beyond the base dice. The stable signal is the
        # type signature: DB=3/3, P1=1/2, XR=2/1.
        return True

    def _exact_weapon_family(self, profile: WeaponLearningProfile) -> Optional[WeaponFamilyConfig]:
        if profile.locked_family_key:
            return self.WEAPON_FAMILY_CONFIG.get(profile.locked_family_key)

        family_key = self.WEAPON_FAMILY_BY_SIGNATURE.get((len(profile.elemental_counts), len(profile.exotic_counts)))
        if not family_key:
            return None
        family = self.WEAPON_FAMILY_CONFIG[family_key]
        if not self._family_amounts_compatible(profile, family):
            return None
        return family

    def _profile_is_locked(self, profile: Optional[WeaponLearningProfile]) -> bool:
        return bool(profile and profile.locked_family_key)

    def _profile_locked_types(self, profile: Optional[WeaponLearningProfile]) -> Set[int]:
        if profile is None or not profile.locked_family_key:
            return set()
        return set(profile.locked_elemental) | set(profile.locked_exotic)

    def _lock_weapon_profile(self, profile: WeaponLearningProfile, family: WeaponFamilyConfig):
        if profile.locked_family_key:
            return

        profile.locked_family_key = family.family_key
        profile.locked_elemental = self._top_damage_types(profile.elemental_counts, family.elemental_count)
        profile.locked_exotic = self._top_damage_types(profile.exotic_counts, family.exotic_count)
        profile.mismatch_streak = 0
        profile.mismatch_total = 0
        profile.last_mismatch_at = 0.0

    def _reset_weapon_profile_for_rediscovery(self, profile: WeaponLearningProfile):
        profile.observations = 0
        profile.attack_attempts = 0
        profile.last_seen_at = 0.0
        profile.last_attack_at = 0.0
        profile.locked_family_key = ""
        profile.locked_elemental = ()
        profile.locked_exotic = ()
        profile.mismatch_streak = 0
        profile.mismatch_total = 0
        profile.last_mismatch_at = 0.0
        profile.rediscoveries += 1
        profile.elemental_counts.clear()
        profile.exotic_counts.clear()
        profile.elemental_max_amounts.clear()
        profile.exotic_max_amounts.clear()

    def _compatible_weapon_families(self, profile: WeaponLearningProfile) -> Tuple[WeaponFamilyConfig, ...]:
        exact_family = self._exact_weapon_family(profile)
        if exact_family is not None:
            return (exact_family,)

        seen_elemental = len(profile.elemental_counts)
        seen_exotic = len(profile.exotic_counts)
        compatible = []
        for family in self.WEAPON_FAMILY_CONFIG.values():
            if seen_elemental > family.elemental_count or seen_exotic > family.exotic_count:
                continue
            if not self._family_amounts_compatible(profile, family):
                continue
            compatible.append(family)
        compatible.sort(key=lambda family: family.family_key)
        return tuple(compatible)

    def _top_damage_types(self, counts: Dict[int, int], limit: int) -> Tuple[int, ...]:
        if limit <= 0 or not counts:
            return ()
        ranked = sorted(
            counts.items(),
            key=lambda item: (-item[1], _format_damage_type_label(item[0]), item[0]),
        )
        return tuple(damage_type for damage_type, _count in ranked[:limit])

    def _weapon_profile_context(
        self,
        profile: WeaponLearningProfile,
    ) -> Optional[Tuple[Dict[int, float], str, Tuple[int, ...], Tuple[int, ...], int, int]]:
        exact_family = self._exact_weapon_family(profile)
        if exact_family is not None and profile.locked_family_key:
            learned_elemental = profile.locked_elemental
            learned_exotic = profile.locked_exotic
            missing_elemental = 0
            missing_exotic = 0
            elemental_dice = exact_family.elemental_dice
            exotic_dice = exact_family.exotic_dice
            family_label = exact_family.family_key
        elif exact_family is not None:
            learned_elemental = self._top_damage_types(profile.elemental_counts, exact_family.elemental_count)
            learned_exotic = self._top_damage_types(profile.exotic_counts, exact_family.exotic_count)
            missing_elemental = max(exact_family.elemental_count - len(learned_elemental), 0)
            missing_exotic = max(exact_family.exotic_count - len(learned_exotic), 0)
            elemental_dice = exact_family.elemental_dice
            exotic_dice = exact_family.exotic_dice
            family_label = exact_family.family_key
        else:
            compatible = self._compatible_weapon_families(profile)
            if not compatible:
                return None

            if len(compatible) == 1:
                family = compatible[0]
                learned_elemental = self._top_damage_types(profile.elemental_counts, family.elemental_count)
                learned_exotic = self._top_damage_types(profile.exotic_counts, family.exotic_count)
                missing_elemental = max(family.elemental_count - len(learned_elemental), 0)
                missing_exotic = max(family.exotic_count - len(learned_exotic), 0)
                elemental_dice = family.elemental_dice
                exotic_dice = family.exotic_dice
                family_label = f"{family.family_key}?"
            else:
                learned_elemental = self._top_damage_types(
                    profile.elemental_counts,
                    max(family.elemental_count for family in compatible),
                )
                learned_exotic = self._top_damage_types(
                    profile.exotic_counts,
                    max(family.exotic_count for family in compatible),
                )
                missing_elemental = min(
                    max(family.elemental_count - len(learned_elemental), 0)
                    for family in compatible
                )
                missing_exotic = min(
                    max(family.exotic_count - len(learned_exotic), 0)
                    for family in compatible
                )
                elemental_dice = min(family.elemental_dice for family in compatible)
                exotic_dice = min(family.exotic_dice for family in compatible)
                family_label = "/".join(family.family_key for family in compatible) + "?"

        components: Dict[int, float] = {}
        elemental_base_damage = 6.5 * float(elemental_dice)
        exotic_base_damage = 6.5 * float(exotic_dice)
        for damage_type in learned_elemental:
            components[damage_type] = components.get(damage_type, 0.0) + elemental_base_damage
        for damage_type in learned_exotic:
            components[damage_type] = components.get(damage_type, 0.0) + exotic_base_damage

        return components, family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic

    def _weapon_profile_summary(
        self,
        family_label: str,
        learned_elemental: Tuple[int, ...],
        learned_exotic: Tuple[int, ...],
        missing_elemental: int,
        missing_exotic: int,
    ) -> str:
        parts = []
        if family_label:
            parts.append(family_label)
        if learned_elemental:
            parts.append("Elem " + "/".join(_format_damage_type_label(value) for value in learned_elemental))
        if learned_exotic:
            parts.append("Exo " + "/".join(_format_damage_type_label(value) for value in learned_exotic))
        if missing_elemental or missing_exotic:
            parts.append(f"Learn E{missing_elemental}/X{missing_exotic}")
        return ", ".join(parts) if parts else "Unlearned"

    def _weapon_swap_cooldown_seconds(self) -> float:
        return max(float(self.config.get("swap_cooldown_seconds", 6.2)), 0.1)

    def _confirm_pending_weapon(self, reason: str) -> bool:
        if not self.pending_weapon_key:
            return False

        pending_key = self.pending_weapon_key
        self.current_weapon_key = pending_key
        self._clear_pending_weapon_state()
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} confirmed {self._binding_display(pending_key)} ({reason})",
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _confirm_unarmed_state(self, reason: str) -> bool:
        previous_key = self.current_weapon_key
        previous_pending = self.pending_weapon_key
        self.current_weapon_key = WEAPON_CURRENT_UNARMED
        self._clear_pending_weapon_state()
        self.weapon_unarmed_observations += 1
        if previous_key != WEAPON_CURRENT_UNARMED or previous_pending:
            source = self._binding_display(previous_pending) if previous_pending else self._binding_display(previous_key)
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: {self._mode_label()} confirmed unarmed from {source} ({reason})",
                script_id=self.script_id,
            )
        self.host.notify_state_changed()
        return True

    def _observe_weapon_swap_feedback(self, text: str):
        feedback = hgx_combat.parse_weapon_swap_feedback(text)
        if not feedback:
            return

        self.weapon_last_swap_feedback = feedback
        if feedback == "item_swapped":
            self.pending_weapon_feedback_seen = bool(self.pending_weapon_key) or self.pending_weapon_feedback_seen
            if self.pending_weapon_key:
                self.pending_weapon_feedback_sequence = self.current_chat_sequence
            self.host.notify_state_changed()
            return

        if feedback == "weapon_equipped":
            self.pending_weapon_feedback_seen = bool(self.pending_weapon_key) or self.pending_weapon_feedback_seen
            self.pending_weapon_equipped_feedback_seen = bool(self.pending_weapon_key) or self.pending_weapon_equipped_feedback_seen
            if self.pending_weapon_key:
                self.pending_weapon_feedback_sequence = self.current_chat_sequence
            if self.pending_weapon_key:
                if time.monotonic() >= self.pending_weapon_ready_at:
                    self.set_status(f"awaiting {self._binding_display(self.pending_weapon_key)} damage")
                else:
                    self.set_status(f"waiting {self._binding_display(self.pending_weapon_key)} damage")
                self.host.notify_state_changed()
            else:
                self.host.notify_state_changed()

    def _profile_damage_types(self, profile: Optional[WeaponLearningProfile]) -> Set[int]:
        if profile is None:
            return set()
        locked_types = self._profile_locked_types(profile)
        if locked_types:
            return locked_types
        return set(profile.elemental_counts.keys()) | set(profile.exotic_counts.keys())

    def _observed_types_fit_profile(self, profile: Optional[WeaponLearningProfile], observed_types: Set[int]) -> bool:
        if profile is None or not observed_types:
            return False

        known_types = self._profile_damage_types(profile)
        if not known_types:
            return False

        return observed_types == known_types

    def _observed_types_exactly_match_profile(self, profile: Optional[WeaponLearningProfile], observed_types: Set[int]) -> bool:
        if profile is None or not observed_types:
            return False
        known_types = self._profile_damage_types(profile)
        return bool(known_types) and observed_types == known_types

    def _pending_weapon_can_accept_damage(self, now: float) -> bool:
        if not self.pending_weapon_key:
            return True
        if self.pending_weapon_conceal_seen:
            return True
        return now >= self.pending_weapon_ready_at

    def _note_pending_damage_ignored(self, defender_name: str, observed_types: Set[int]):
        if not self.pending_weapon_key:
            return
        self.pending_weapon_ignored_damage_count += 1
        self.set_status(
            f"{defender_name}: waiting round boundary for {self._binding_display(self.pending_weapon_key)}"
        )
        if self.pending_weapon_ignored_damage_count in (1, 4):
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} ignored pre-boundary "
                    f"damage while waiting for {self._binding_display(self.pending_weapon_key)}; "
                    f"observed {self._format_weapon_type_set(observed_types)}"
                ),
                script_id=self.script_id,
            )
        self.host.notify_state_changed()

    def _observe_pending_conceal_attack(self, attack):
        if not self.pending_weapon_key:
            return
        if "target concealed: 100%" not in str(attack.normalized_text or "").lower():
            return

        self.pending_weapon_conceal_seen = True
        self.pending_weapon_conceal_sequence = self.current_chat_sequence
        self.set_status(f"{attack.defender}: round boundary for {self._binding_display(self.pending_weapon_key)}")
        self.host.notify_state_changed()

    def _damage_line_is_physical_only(self, damage_line) -> bool:
        has_physical = False
        for component in damage_line.components:
            if component.damage_type in WEAPON_ELEMENTAL_TYPES or component.damage_type in WEAPON_EXOTIC_TYPES:
                return False
            type_name = str(getattr(component, "type_name", "") or "").strip().lower()
            if type_name in {"physical", "bludgeoning", "piercing", "slashing"}:
                has_physical = True
            else:
                return False
        return has_physical

    def _observed_weapon_damage_types(self, damage_line) -> Set[int]:
        observed_types: Set[int] = set()
        for component in damage_line.components:
            if component.damage_type in WEAPON_ELEMENTAL_TYPES or component.damage_type in WEAPON_EXOTIC_TYPES:
                observed_types.add(component.damage_type)
        return observed_types

    def _profile_for_observed_damage(
        self,
        observed_types: Set[int],
        now: float,
        defender_name: str,
    ) -> Optional[WeaponLearningProfile]:
        pending_can_accept = self._pending_weapon_can_accept_damage(now) if self.pending_weapon_key else True
        force_equipped_probe = bool(
            self.pending_weapon_key
            and pending_can_accept
            and self.weapon_equipped_probe_at < self.pending_weapon_ready_at
        )
        equipped_keys = self._query_equipped_binding_keys(
            force=force_equipped_probe
        )
        equipped_key = equipped_keys[0] if len(equipped_keys) == 1 else ""

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if not self.pending_weapon_key:
            if equipped_key:
                self._set_current_weapon_from_equipped_key(equipped_key, "equipped quickbar mask")
                current_profile = self.weapon_profiles.get(equipped_key)

            matching_profile = self._matching_locked_profile_for_observed_types(
                observed_types,
                exclude_key=self.current_weapon_key,
            )
            if (
                matching_profile is not None
                and not equipped_key
                and (
                    current_profile is None
                    or not self._observed_types_exactly_match_profile(current_profile, observed_types)
                )
            ):
                self._set_current_weapon_from_equipped_key(
                    matching_profile.binding.key,
                    "damage matched a learned slot",
                )
                return matching_profile
            return current_profile

        pending_profile = self.weapon_profiles.get(self.pending_weapon_key)
        if pending_profile is None:
            self._clear_pending_weapon_state()
            return current_profile

        current_types = self._profile_damage_types(current_profile)
        pending_types = self._profile_damage_types(pending_profile)

        if not pending_can_accept:
            if current_types and self._observed_types_exactly_match_profile(current_profile, observed_types):
                return current_profile
            self._note_pending_damage_ignored(defender_name, observed_types)
            return None

        if equipped_key == self.pending_weapon_key:
            self.pending_weapon_feedback_seen = True
            self.pending_weapon_equipped_feedback_seen = True
        elif equipped_key:
            equipped_profile = self.weapon_profiles.get(equipped_key)
            if equipped_profile is not None and self._observed_types_exactly_match_profile(equipped_profile, observed_types):
                equipped_display = self._binding_display(equipped_key)
                self._cancel_pending_weapon(f"equipped mask reports {equipped_display}")
                self._set_current_weapon_from_equipped_key(equipped_key, "equipped quickbar mask")
                return equipped_profile

        if pending_types and self._observed_types_fit_profile(pending_profile, observed_types):
            self._confirm_pending_weapon("matched pending damage")
            return pending_profile

        if current_types and self._observed_types_fit_profile(current_profile, observed_types):
            self.set_status(f"{defender_name}: awaiting {self._binding_display(self.pending_weapon_key)} damage")
            return current_profile

        matching_profile = self._matching_locked_profile_for_observed_types(
            observed_types,
            exclude_key="",
        )
        if matching_profile is not None:
            matching_key = matching_profile.binding.key
            if matching_key == self.pending_weapon_key:
                self._confirm_pending_weapon("damage matched pending learned slot")
            elif equipped_key and equipped_key != matching_key:
                self.set_status(f"{defender_name}: awaiting {self._binding_display(self.pending_weapon_key)} damage")
                return None
            else:
                matching_display = self._binding_display(matching_key)
                self._cancel_pending_weapon(f"damage matched {matching_display}")
                self._set_current_weapon_from_equipped_key(matching_key, "damage matched a learned slot")
            return matching_profile

        strong_pending_evidence = (
            equipped_key == self.pending_weapon_key
            or self.pending_weapon_equipped_feedback_seen
            or self.pending_weapon_conceal_seen
        )
        equipped_mask_does_not_contradict_pending = not equipped_key or equipped_key == self.pending_weapon_key
        pending_has_no_exact_family = self._exact_weapon_family(pending_profile) is None
        if (
            pending_has_no_exact_family
            and observed_types
            and strong_pending_evidence
            and equipped_mask_does_not_contradict_pending
        ):
            self._confirm_pending_weapon("new pending damage after equip feedback")
            return pending_profile

        if (
            observed_types
            and (not current_types or observed_types != current_types)
            and strong_pending_evidence
            and equipped_mask_does_not_contradict_pending
        ):
            self._confirm_pending_weapon("damage types changed")
            return pending_profile

        self.set_status(f"{defender_name}: awaiting {self._binding_display(self.pending_weapon_key)} damage")
        return None

    def _format_weapon_type_set(self, damage_types: Set[int]) -> str:
        if not damage_types:
            return "physical-only"
        return "/".join(_format_damage_type_label(value) for value in sorted(damage_types))

    def _record_locked_profile_mismatch(
        self,
        profile: WeaponLearningProfile,
        observed_types: Set[int],
        defender_name: str,
        now: float,
    ) -> bool:
        expected_types = self._profile_damage_types(profile)
        profile.mismatch_streak += 1
        profile.mismatch_total += 1
        profile.last_mismatch_at = now

        threshold = self.WEAPON_REDISCOVERY_MISMATCH_THRESHOLD
        self.set_status(
            f"{defender_name}: {self._binding_display(profile.binding.key)} mismatch "
            f"{profile.mismatch_streak}/{threshold}"
        )
        if profile.mismatch_streak in (1, threshold // 2):
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} ignored possible "
                    f"{self._binding_display(profile.binding.key)} mismatch "
                    f"{profile.mismatch_streak}/{threshold}; expected "
                    f"{self._format_weapon_type_set(expected_types)} observed "
                    f"{self._format_weapon_type_set(observed_types)} on '{defender_name}'"
                ),
                script_id=self.script_id,
            )
        if profile.mismatch_streak < threshold:
            self.host.notify_state_changed()
            return False

        if self.weapon_equipped_key != profile.binding.key:
            if self.weapon_equipped_key in self.weapon_profiles:
                self._set_current_weapon_from_equipped_key(
                    self.weapon_equipped_key,
                    "mismatch belonged to a different equipped slot",
                )
            elif self.current_weapon_key == profile.binding.key:
                previous = self._binding_display(self.current_weapon_key)
                self.current_weapon_key = WEAPON_CURRENT_UNKNOWN
                self.host.emit(
                    "error",
                    (
                        f"{self.host.client.display_name}: {self._mode_label()} stopped trusting "
                        f"{previous} after {profile.mismatch_streak} mismatched damage lines; "
                        "the equipped quickbar mask does not confirm that slot"
                    ),
                    script_id=self.script_id,
                )
            profile.mismatch_streak = 0
            self.host.notify_state_changed()
            return False

        self.host.emit(
            "error",
            (
                f"{self.host.client.display_name}: {self._mode_label()} rediscovering "
                f"{self._binding_display(profile.binding.key)} after {profile.mismatch_streak} "
                f"consecutive mismatched damage lines; expected {self._format_weapon_type_set(expected_types)} "
                f"observed {self._format_weapon_type_set(observed_types)}"
            ),
            script_id=self.script_id,
        )
        self._reset_weapon_profile_for_rediscovery(profile)
        return True

    def _apply_weapon_profile_observation(
        self,
        profile: WeaponLearningProfile,
        damage_line,
        observed_types: Set[int],
        now: float,
    ) -> bool:
        if self._profile_is_locked(profile):
            locked_types = self._profile_locked_types(profile)
            if observed_types != locked_types:
                if not self._record_locked_profile_mismatch(profile, observed_types, damage_line.defender, now):
                    return False
            else:
                profile.mismatch_streak = 0

        profile.observations += 1
        profile.last_seen_at = now
        allowed_types = self._profile_locked_types(profile)
        for component in damage_line.components:
            damage_type = component.damage_type
            if allowed_types and damage_type not in allowed_types:
                continue
            if damage_type in WEAPON_ELEMENTAL_TYPES:
                profile.elemental_counts[damage_type] = profile.elemental_counts.get(damage_type, 0) + 1
                profile.elemental_max_amounts[damage_type] = max(
                    profile.elemental_max_amounts.get(damage_type, 0),
                    int(component.amount),
                )
            elif damage_type in WEAPON_EXOTIC_TYPES:
                profile.exotic_counts[damage_type] = profile.exotic_counts.get(damage_type, 0) + 1
                profile.exotic_max_amounts[damage_type] = max(
                    profile.exotic_max_amounts.get(damage_type, 0),
                    int(component.amount),
                )

        family = self._exact_weapon_family(profile)
        if family is not None and not profile.locked_family_key:
            self._lock_weapon_profile(profile, family)
        return True

    def _observe_weapon_damage_line(self, text: str):
        if " damages " in str(text or "").lower():
            self.weapon_damage_parse_miss_count += 1

        damage_line = hgx_combat.parse_damage_line(text)
        if damage_line is None:
            return

        self.weapon_damage_parse_miss_count = max(self.weapon_damage_parse_miss_count - 1, 0)
        self.weapon_damage_seen_count += 1
        character_name = self._character_name()
        character_key = hgx_combat.normalize_actor_name(character_name).lower() if character_name else ""
        if not character_key or damage_line.attacker.lower() != character_key:
            self.weapon_last_ignored_damage_actor = damage_line.attacker
            self.host.notify_state_changed()
            return
        self.weapon_damage_matched_count += 1

        now = time.monotonic()
        observed_types = self._observed_weapon_damage_types(damage_line)
        if not observed_types and self._damage_line_is_physical_only(damage_line):
            if self.pending_weapon_key and not self._pending_weapon_can_accept_damage(now):
                self._note_pending_damage_ignored(damage_line.defender, set())
                return
            reason = "pending press produced physical-only damage" if self.pending_weapon_key else "physical-only damage"
            self._confirm_unarmed_state(reason)
            self.set_status(f"{damage_line.defender}: unarmed detected")
            return
        if not observed_types:
            return

        profile = self._profile_for_observed_damage(observed_types, now, damage_line.defender)
        if profile is None:
            return

        before_context = self._weapon_profile_context(profile)
        if not self._apply_weapon_profile_observation(profile, damage_line, observed_types, now):
            return

        after_context = self._weapon_profile_context(profile)
        if after_context != before_context and after_context is not None:
            _components, family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic = after_context
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} learned {self._binding_display(profile.binding.key)} "
                    f"from '{damage_line.defender}' -> "
                    f"{self._weapon_profile_summary(family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic)}"
                ),
                script_id=self.script_id,
            )

    def _weapon_candidates_for_target(self, creature_name: str) -> List[WeaponRecommendation]:
        candidates: List[WeaponRecommendation] = []
        for profile in self.weapon_profiles.values():
            context = self._weapon_profile_context(profile)
            if context is None:
                continue

            components, family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic = context
            if not components:
                continue

            estimate = self.db.estimate_custom_damage(creature_name, components)
            if estimate is None:
                continue

            candidates.append(
                WeaponRecommendation(
                    binding=profile.binding,
                    expected_damage=estimate.expected_damage,
                    matched_name=estimate.matched_name,
                    paragon_ranks=estimate.paragon_ranks,
                    family_label=family_label,
                    learned_elemental=learned_elemental,
                    learned_exotic=learned_exotic,
                    healing_types=estimate.healing_types,
                    missing_elemental=missing_elemental,
                    missing_exotic=missing_exotic,
                )
            )
        return candidates

    def _choose_best_weapon(self, candidates: List[WeaponRecommendation]) -> Optional[WeaponRecommendation]:
        if not candidates:
            return None

        return max(
            candidates,
            key=lambda candidate: (
                candidate.expected_damage,
                0 if candidate.family_label.endswith("?") else 1,
                1 if candidate.binding.key == self.current_weapon_key else 0,
                -(candidate.missing_elemental + candidate.missing_exotic),
                candidate.binding.key,
            ),
        )

    def _weapon_learning_status(self, target_name: str) -> str:
        learned_count = 0
        resolved_count = 0
        for profile in self.weapon_profiles.values():
            context = self._weapon_profile_context(profile)
            if context is not None and context[0]:
                learned_count += 1
            if self._exact_weapon_family(profile) is not None:
                resolved_count += 1
        return (
            f"{target_name}: learning weapons "
            f"({resolved_count}/{len(self.weapon_profiles)} typed, {learned_count} seen)"
        )

    def _weapon_runtime_summary(self, profile: WeaponLearningProfile) -> str:
        context = self._weapon_profile_context(profile)
        if context is None:
            compatible = "/".join(family.family_key for family in self._compatible_weapon_families(profile))
            suffix = f"obs {profile.observations}, attacks {profile.attack_attempts}"
            if profile.mismatch_streak:
                suffix += f", mismatch {profile.mismatch_streak}/{self.WEAPON_REDISCOVERY_MISMATCH_THRESHOLD}"
            if profile.rediscoveries:
                suffix += f", rediscovered {profile.rediscoveries}"
            return f"Unknown ({compatible or 'no compatible family'}), {suffix}"

        _components, family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic = context
        prefix = "" if self._exact_weapon_family(profile) is not None else "Unknown "
        suffix = f"obs {profile.observations}, attacks {profile.attack_attempts}"
        if profile.locked_family_key:
            suffix += ", locked"
        if profile.mismatch_streak:
            suffix += f", mismatch {profile.mismatch_streak}/{self.WEAPON_REDISCOVERY_MISMATCH_THRESHOLD}"
        if profile.rediscoveries:
            suffix += f", rediscovered {profile.rediscoveries}"
        return f"{prefix}{self._weapon_profile_summary(family_label, learned_elemental, learned_exotic, missing_elemental, missing_exotic)}, {suffix}"

    def _next_weapon_to_learn(self) -> Optional[WeaponLearningProfile]:
        if not self.weapon_profiles:
            return None

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if (
            current_profile is not None
            and self._exact_weapon_family(current_profile) is None
            and current_profile.observations == 0
            and current_profile.attack_attempts < self.WEAPON_LEARNING_ATTACKS_BEFORE_ROTATE
        ):
            return current_profile

        candidates = [
            profile
            for profile in self.weapon_profiles.values()
            if profile.binding.key != self.current_weapon_key and self._exact_weapon_family(profile) is None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda profile: (profile.observations, profile.attack_attempts, profile.last_seen_at, profile.binding.key))

    def _request_weapon_swap(self, binding: WeaponBinding, target_name: str, reason: str) -> bool:
        equipped_keys = self._query_equipped_binding_keys(force=True)
        if binding.key in equipped_keys:
            self._cancel_pending_weapon(f"{self._binding_display(binding.key)} is already equipped")
            self._set_current_weapon_from_equipped_key(binding.key, "already equipped before quickbar press")
            self.set_status(f"{target_name}: already using {self._binding_display(binding.key)}")
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} skipped "
                    f"{self._binding_display(binding.key)} press for target='{target_name}' "
                    f"reason={reason}; equipped mask already reports that slot"
                ),
                script_id=self.script_id,
            )
            self.host.notify_state_changed()
            return True

        try:
            result = self.host.trigger_slot(binding.slot, page=binding.page)
        except Exception as exc:
            self.set_status(f"{target_name}: swap failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} trigger failed for {self._binding_display(binding.key)}: {exc}",
                script_id=self.script_id,
            )
            return False

        if result["success"]:
            now = time.monotonic()
            self.pending_weapon_key = binding.key
            self.pending_weapon_ready_at = now + self._weapon_swap_cooldown_seconds()
            self.pending_weapon_requested_at = now
            self.pending_weapon_request_sequence = self.current_chat_sequence
            self.pending_weapon_feedback_seen = False
            self.pending_weapon_equipped_feedback_seen = False
            self.pending_weapon_feedback_sequence = 0
            self.pending_weapon_conceal_seen = False
            self.pending_weapon_conceal_sequence = 0
            self.pending_weapon_ignored_damage_count = 0
            self.weapon_equipped_key = ""
            self.weapon_equipped_keys = ()
            self.weapon_equipped_probe_at = 0.0
            self.set_status(f"{target_name}: {reason} {self._binding_display(binding.key)} ({self._weapon_swap_cooldown_seconds():.1f}s)")
        else:
            self.set_status(f"{target_name}: swap failed")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} target='{target_name}' "
                f"weapon={self._binding_display(binding.key)} reason={reason} success={result['success']} "
                f"rc={result['rc']} aux={result['aux_rc']} path={result['path']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return bool(result["success"])

    def _weapon_recommendation_summary(self, recommendation: WeaponRecommendation) -> str:
        return self._weapon_profile_summary(
            recommendation.family_label,
            recommendation.learned_elemental,
            recommendation.learned_exotic,
            recommendation.missing_elemental,
            recommendation.missing_exotic,
        )

    def _target_stat_entries(self, values: Tuple[int, ...], include_values: bool = True) -> List[dict]:
        entries = []
        for damage_type, value in enumerate(values):
            if int(value or 0) == 0:
                continue
            entry = {
                "type": damage_type,
                "label": _format_damage_type_label(damage_type),
            }
            if include_values:
                entry["value"] = int(value)
            entries.append(entry)
        return entries

    def _target_analysis_for_weapon_mode(self) -> dict:
        target_name = (self.current_target or "").strip()
        analysis = {
            "available": False,
            "target": target_name,
            "matched_name": "",
            "paragon_ranks": 0,
            "immunity": [],
            "resistance": [],
            "healing": [],
            "weapons": [],
            "recommended_weapon": "",
            "message": "",
        }
        if not target_name:
            analysis["message"] = "Waiting for a combat target."
            return analysis

        profile = self.db._resolve_combat_profile(target_name)
        if profile is None:
            analysis["message"] = f"No characters.d entry for '{target_name}'."
            return analysis

        analysis.update({
            "available": True,
            "matched_name": profile.matched_name,
            "paragon_ranks": profile.paragon_ranks,
            "immunity": self._target_stat_entries(profile.immunity),
            "resistance": self._target_stat_entries(profile.resistance),
            "healing": self._target_stat_entries(profile.healing, include_values=False),
        })

        candidates = self._weapon_candidates_for_target(target_name)
        safe_candidates = [candidate for candidate in candidates if not candidate.healing_types]
        recommendation = self._choose_best_weapon(safe_candidates) if safe_candidates else None
        if recommendation is not None:
            analysis["recommended_weapon"] = recommendation.binding.key

        estimates_by_key = {candidate.binding.key: candidate for candidate in candidates}
        weapons = []
        for binding_key in self._weapon_binding_keys():
            binding = self.weapon_bindings.get(binding_key)
            weapon_profile = self.weapon_profiles.get(binding_key)
            if binding is None or weapon_profile is None:
                continue

            estimate = estimates_by_key.get(binding_key)
            weapon = {
                "key": binding.key,
                "label": binding.label,
                "current": binding.key == self.current_weapon_key,
                "pending": binding.key == self.pending_weapon_key,
                "recommended": binding.key == analysis["recommended_weapon"],
                "summary": self._weapon_runtime_summary(weapon_profile),
                "expected_damage": None,
                "matched_name": "",
                "paragon_ranks": 0,
                "healing_types": [],
            }
            if estimate is not None:
                weapon.update({
                    "expected_damage": estimate.expected_damage,
                    "matched_name": estimate.matched_name,
                    "paragon_ranks": estimate.paragon_ranks,
                    "healing_types": [
                        _format_damage_type_label(damage_type)
                        for damage_type in estimate.healing_types
                    ],
                })
            weapons.append(weapon)

        analysis["weapons"] = weapons
        return analysis

    def _handle_weapon_attack(self, attack):
        self.current_target = attack.defender

        now = time.monotonic()
        if self.pending_weapon_key and now < self.pending_weapon_ready_at:
            remaining = self.pending_weapon_ready_at - now
            self.set_status(f"{attack.defender}: waiting {self._binding_display(self.pending_weapon_key)} {remaining:.1f}s")
            return

        if self.pending_weapon_key:
            self.set_status(f"{attack.defender}: awaiting {self._binding_display(self.pending_weapon_key)} damage")
            return

        self._reconcile_current_weapon_from_equipped_mask()

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if current_profile is not None and self._exact_weapon_family(current_profile) is None:
            current_profile.attack_attempts += 1
            current_profile.last_attack_at = now

        learning_profile = self._next_weapon_to_learn()
        if learning_profile is not None:
            if learning_profile.binding.key == self.current_weapon_key:
                mask_confirms_current = self._equipped_mask_confirms_binding(learning_profile.binding.key)
                if mask_confirms_current is not False:
                    self.set_status(f"{attack.defender}: learning {self._binding_display(self.current_weapon_key)}")
                    self.host.notify_state_changed()
                    return
                self.host.emit(
                    "info",
                    (
                        f"{self.host.client.display_name}: {self._mode_label()} no longer trusts "
                        f"{self._binding_display(self.current_weapon_key)} while learning; "
                        "equipped mask does not confirm it"
                    ),
                    script_id=self.script_id,
                )
                self.current_weapon_key = WEAPON_CURRENT_UNKNOWN
            self._request_weapon_swap(learning_profile.binding, attack.defender, "learn")
            return

        if self.db.lookup(attack.defender) is None:
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

        candidates = self._weapon_candidates_for_target(attack.defender)
        if not candidates:
            self.set_status(self._weapon_learning_status(attack.defender))
            return

        safe_candidates = [candidate for candidate in candidates if not candidate.healing_types]
        if not safe_candidates:
            unsafe_candidate = self._choose_best_weapon(candidates)
            healing_text = ", ".join(_format_damage_type_label(value) for value in unsafe_candidate.healing_types) if unsafe_candidate else "unknown"
            self.set_status(f"{attack.defender}: unsafe ({healing_text})")
            return

        recommendation = self._choose_best_weapon(safe_candidates)
        if recommendation is None:
            self.set_status(self._weapon_learning_status(attack.defender))
            return

        selection_summary = self._weapon_recommendation_summary(recommendation)
        if recommendation.binding.key == self.current_weapon_key:
            mask_confirms_current = self._equipped_mask_confirms_binding(recommendation.binding.key)
            if mask_confirms_current is not False:
                self.set_status(f"{attack.defender}: {self._binding_display(recommendation.binding.key)} {selection_summary}")
                return
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} no longer trusts "
                    f"{self._binding_display(self.current_weapon_key)} for '{attack.defender}'; "
                    "equipped mask does not confirm it"
                ),
                script_id=self.script_id,
            )
            self.current_weapon_key = WEAPON_CURRENT_UNKNOWN

        if not self._request_weapon_swap(recommendation.binding, attack.defender, "swap"):
            return

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} target='{attack.defender}' "
                f"weapon={self._binding_display(recommendation.binding.key)} "
                f"expected={recommendation.expected_damage} paragon={recommendation.paragon_ranks} "
                f"profile={selection_summary}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", False)):
            self.host.send_console(
                (
                    f"SimKeys {self._mode_label()} {attack.defender} -> {self._binding_display(recommendation.binding.key)} "
                    f"profile={selection_summary}"
                )
            )

    def get_max_lines(self) -> int:
        if self._is_weapon_mode():
            return min(super().get_max_lines(), 20)
        return super().get_max_lines()

    def get_state_details(self) -> dict:
        if not self._is_weapon_mode():
            return {}
        if self.weapon_equipped_key in self.weapon_profiles:
            equipped_display = self._binding_display(self.weapon_equipped_key)
        elif self.weapon_equipped_key == "Multiple":
            equipped_display = "Multiple configured slots"
        elif self.weapon_last_equipped_mask:
            equipped_display = "No configured slot"
        else:
            equipped_display = ""
        weapons = []
        for binding_key in self._weapon_binding_keys():
            binding = self.weapon_bindings.get(binding_key)
            profile = self.weapon_profiles.get(binding_key)
            if binding is None or profile is None:
                continue
            weapons.append({
                "key": binding.key,
                "label": binding.label,
                "choice": binding.choice,
                "current": binding.key == self.current_weapon_key,
                "pending": binding.key == self.pending_weapon_key,
                "observations": profile.observations,
                "locked": bool(profile.locked_family_key),
                "mismatch_streak": profile.mismatch_streak,
                "rediscoveries": profile.rediscoveries,
                "summary": self._weapon_runtime_summary(profile),
            })
        return {
            "weapon_mode": True,
            "current_weapon": self.current_weapon_key,
            "current_display": self._binding_display(self.current_weapon_key),
            "pending_weapon": self.pending_weapon_key,
            "pending_display": self._binding_display(self.pending_weapon_key) if self.pending_weapon_key else "",
            "equipped_weapon": self.weapon_equipped_key,
            "equipped_display": equipped_display,
            "equipped_mask": self.weapon_last_equipped_mask,
            "equipped_slots": simkeys.format_quickbar_slots(self.weapon_last_equipped_mask),
            "equipped_probe_error": self.weapon_equipped_probe_error,
            "last_swap_feedback": self.weapon_last_swap_feedback,
            "unarmed_observations": self.weapon_unarmed_observations,
            "pending_conceal_seen": self.pending_weapon_conceal_seen,
            "pending_ignored_damage": self.pending_weapon_ignored_damage_count,
            "combat": {
                "attack_seen": self.weapon_attack_seen_count,
                "attack_matched": self.weapon_attack_matched_count,
                "damage_seen": self.weapon_damage_seen_count,
                "damage_matched": self.weapon_damage_matched_count,
                "damage_parse_miss": self.weapon_damage_parse_miss_count,
                "ignored_attack_actor": self.weapon_last_ignored_attack_actor,
                "ignored_damage_actor": self.weapon_last_ignored_damage_actor,
            },
            "weapons": weapons,
            "target_analysis": self._target_analysis_for_weapon_mode(),
        }

    def _damage_dice(self) -> int:
        return max(int(self.config.get("elemental_dice", 10)), 0)

    def _dice_unit(self) -> str:
        return "d20" if self._mode_label() == self.MODE_ARCANE_ARCHER else "d12"

    def _parse_feedback_type(self, text: str) -> Optional[int]:
        if self._is_weapon_mode():
            return None
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


class AutoAttackScript(ClientScriptBase):
    script_id = "auto_attack"
    COMMAND = "!action attack lead:opponent"

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
            name=f"AutoAttack-{self.client.pid}",
            daemon=True,
        )
        self.loop_thread.start()
        self.set_status(f"Attacking every {self._cooldown_seconds():.1f}s")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: Auto Attack started ({self.COMMAND} every {self._cooldown_seconds():.1f}s)",
            script_id=self.script_id,
        )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.loop_stop.set()
        self.host.emit("info", f"{self.host.client.display_name}: Auto Attack stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        return

    def _run_loop(self):
        while not self.loop_stop.is_set():
            try:
                result = self.host.send_chat(self.COMMAND, 2)
                if result["success"]:
                    if self.last_error_key:
                        self.host.emit(
                            "info",
                            f"{self.host.client.display_name}: Auto Attack recovered",
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
                                f"{self.host.client.display_name}: Auto Attack send failed "
                                f"command={self.COMMAND} rc={result['rc']} err={result['err']}"
                            ),
                            script_id=self.script_id,
                        )
            except Exception as exc:
                error_key = f"exc:{type(exc).__name__}:{exc}"
                if error_key != self.last_error_key:
                    self.last_error_key = error_key
                    self.host.emit(
                        "error",
                        f"{self.host.client.display_name}: Auto Attack chat send failed: {exc}",
                        script_id=self.script_id,
                    )

            if self.loop_stop.wait(self._cooldown_seconds()):
                break

    def _cooldown_seconds(self) -> float:
        return max(float(self.config.get("cooldown_seconds", 3.0)), 0.1)


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

        character_key = hgx_combat.normalize_actor_name(character_name).lower()
        if attack.attacker.lower() != character_key:
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
            return live_name
        cached_name = (self.client.character_name or "").strip()
        if cached_name:
            return cached_name
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
                        "details": script.get_state_details(),
                    }
                    for script_id, script in self.scripts.items()
                },
            }
        self.event_callback(payload)

    def start_script(self, definition: ScriptDefinition, config: Dict[str, object]):
        with self.lock:
            if definition.script_id in self.scripts:
                raise RuntimeError(f"{definition.name} is already running for pid {self.client.pid}.")

            started_at = time.perf_counter()
            self.emit("info", f"{self.client.display_name}: starting {definition.name}", script_id=definition.script_id)

            factory_started_at = time.perf_counter()
            try:
                script = definition.factory(self.client, config, self)
            except Exception as exc:
                elapsed = time.perf_counter() - started_at
                self.emit(
                    "error",
                    f"{self.client.display_name}: {definition.name} failed during setup after {elapsed:.2f}s: {exc}",
                    script_id=definition.script_id,
                )
                raise
            factory_elapsed = time.perf_counter() - factory_started_at

            self.scripts[definition.script_id] = script
            if self.thread is None or not self.thread.is_alive():
                self.stop_event = threading.Event()
                self.thread = threading.Thread(target=self._run, name=f"SimKeysHost-{self.client.pid}", daemon=True)
                self.thread.start()
            on_start_started_at = time.perf_counter()
            try:
                script.on_start()
            except Exception as exc:
                self.scripts.pop(definition.script_id, None)
                if not self.scripts:
                    self.stop_event.set()
                elapsed = time.perf_counter() - started_at
                self.emit(
                    "error",
                    f"{self.client.display_name}: {definition.name} failed during on_start after {elapsed:.2f}s: {exc}",
                    script_id=definition.script_id,
                )
                raise
            on_start_elapsed = time.perf_counter() - on_start_started_at
            elapsed = time.perf_counter() - started_at
            self.emit(
                "info",
                (
                    f"{self.client.display_name}: {definition.name} ready in {elapsed:.2f}s "
                    f"(setup {factory_elapsed:.2f}s, arm {on_start_elapsed:.2f}s)"
                ),
                script_id=definition.script_id,
            )
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
                return {"running": False, "status": "Stopped", "details": {}}
            return {"running": True, "status": script.status_text, "details": script.get_state_details()}

    def running_script_ids(self) -> List[str]:
        with self.lock:
            return sorted(self.scripts.keys())

    def trigger_slot(self, slot: int, page: int = 0):
        return runtime.trigger_slot(self.client, slot, page=page)

    def query_state(self) -> dict:
        return runtime.query_client(self.client)

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
        last_slow_log_at = 0.0
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
                    batch_started_at = time.perf_counter()
                    for line in polled["lines"]:
                        with self.lock:
                            current_scripts = [script for script in self.scripts.values() if script.needs_chat_feed()]
                        for script in current_scripts:
                            line_started_at = time.perf_counter()
                            try:
                                script.on_chat_line(line["seq"], line["text"])
                            except Exception as exc:
                                script.set_status(f"Error: {exc}")
                                self.emit(
                                    "error",
                                    f"{self.client.display_name}: {type(exc).__name__}: {exc}",
                                    script_id=getattr(script, "script_id", None),
                                )
                            finally:
                                line_elapsed = time.perf_counter() - line_started_at
                                now_perf = time.perf_counter()
                                if line_elapsed > 0.50 and now_perf - last_slow_log_at > 10.0:
                                    last_slow_log_at = now_perf
                                    self.emit(
                                        "error",
                                        (
                                            f"{self.client.display_name}: slow {getattr(script, 'script_id', 'script')} "
                                            f"chat handler took {line_elapsed:.2f}s at seq {line['seq']}"
                                        ),
                                        script_id=getattr(script, "script_id", None),
                                    )
                    batch_elapsed = time.perf_counter() - batch_started_at
                    if batch_elapsed > 1.0 and time.perf_counter() - last_slow_log_at > 10.0:
                        last_slow_log_at = time.perf_counter()
                        self.emit(
                            "error",
                            f"{self.client.display_name}: slow chat batch processed {len(polled['lines'])} lines in {batch_elapsed:.2f}s",
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
            description="Watch combat activity, sample HP, and drink from the selected quickbar slot when health falls below the configured threshold.",
            fields=[
                ScriptField("slot", "Slot", "int", 2, minimum=1, maximum=12, step=1, width=4),
                ScriptField("page", "Bank", "choice", "None", choices=["None", "Shift", "Control"], width=8),
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
            description="Switch ranged damage modes or learned weapon sets from combat log lines, without relying on in-game toggles or focus.",
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
                        AutoAAScript.MODE_WEAPON_SWAP,
                    ],
                    width=16,
                ),
                ScriptField("current_weapon", "Cur", "choice", WEAPON_CURRENT_UNKNOWN, choices=WEAPON_CURRENT_CHOICES, width=8),
                ScriptField("weapon_slot_1", "W1", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_2", "W2", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_3", "W3", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_4", "W4", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_5", "W5", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_6", "W6", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("swap_cooldown_seconds", "Swap", "float", 6.2, minimum=0.1, maximum=20.0, step=0.1, width=6),
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
            description="Repeatedly issue the selected combat action on its cooldown.",
            fields=[
                ScriptField("mode", "Mode", "choice", "Called Shot", choices=["Called Shot", "Knockdown", "Disarm"], width=12),
                ScriptField("cooldown_seconds", "Cooldown", "float", 6.2, minimum=0.1, maximum=30.0, step=0.1, width=6),
            ],
            factory=AutoActionScript,
        )
        self.registry[auto_action.script_id] = auto_action

        auto_attack = ScriptDefinition(
            script_id="auto_attack",
            name="Auto Attack",
            description="Repeatedly issue `!action attack lead:opponent`, matching the old HGXLE autoAttack.py behavior.",
            fields=[
                ScriptField("cooldown_seconds", "Cooldown", "float", 3.0, minimum=0.1, maximum=30.0, step=0.1, width=6),
            ],
            factory=AutoAttackScript,
        )
        self.registry[auto_attack.script_id] = auto_attack

        auto_rsm = ScriptDefinition(
            script_id="auto_rsm",
            name="Auto RSM",
            description="Trigger `!action rsm self` automatically when you attack and the RSM status byte is not active.",
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
            return {"running": False, "status": "Stopped", "details": {}}
        return host.get_state(script_id)

    def running_script_count(self, client_pid: int) -> int:
        host = self.hosts.get(client_pid)
        if host is None:
            return 0
        return len(host.running_script_ids())

    def stop_all(self):
        for pid in list(self.hosts.keys()):
            self.stop_all_for_client(pid)
