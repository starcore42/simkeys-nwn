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
WEAPON_PHYSICAL_TYPES = frozenset({0, 1, 2})
WEAPON_ELEMENTAL_TYPES = frozenset({3, 4, 5, 6, 7})
WEAPON_EXOTIC_TYPES = frozenset({8, 9, 10, 11})
WEAPON_SIGNATURE_TYPES = frozenset(set(WEAPON_ELEMENTAL_TYPES) | set(WEAPON_EXOTIC_TYPES))
WEAPON_ESTIMATE_TYPES = frozenset(set(WEAPON_PHYSICAL_TYPES) | set(WEAPON_SIGNATURE_TYPES))
P2_SPECIAL_NAME = "P2"
MAMMONS_WRATH_SIGNATURE = tuple(sorted((
    hgx_data.DAMAGE_TYPE_NAME_TO_ID["cold"],
    hgx_data.DAMAGE_TYPE_NAME_TO_ID["fire"],
    hgx_data.DAMAGE_TYPE_NAME_TO_ID["negative"],
)))
MAMMONS_TEAR_TARGETS = frozenset({
    "mammons tear",
    "dolorous tear",
    "superior dolorous tear",
    "elite dolorous tear",
})
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
class WeaponBinding:
    key: str
    choice: str
    page: int
    slot: int
    label: str


@dataclass
class WeaponDamageEstimate:
    base_estimate: float = 0.0
    observations: int = 0
    last_base_sample: float = 0.0
    last_observed_amount: int = 0
    max_observed_amount: int = 0


@dataclass
class WeaponObservedDamage:
    average_total: float = 0.0
    observations: int = 0
    last_total: int = 0
    max_total: int = 0


@dataclass
class WeaponLearningProfile:
    binding: WeaponBinding
    observations: int = 0
    attack_attempts: int = 0
    last_seen_at: float = 0.0
    last_attack_at: float = 0.0
    stable_signature: Tuple[int, ...] = ()
    stable_signature_observations: int = 0
    candidate_signature: Tuple[int, ...] = ()
    candidate_signature_streak: int = 0
    current_signature: Tuple[int, ...] = ()
    mismatch_streak: int = 0
    mismatch_total: int = 0
    last_mismatch_at: float = 0.0
    rediscoveries: int = 0
    dynamic_kind: str = ""
    signature_counts: Dict[Tuple[int, ...], int] = field(default_factory=dict)
    target_signatures: Dict[str, Tuple[int, ...]] = field(default_factory=dict)
    p2_verification_targets: Set[str] = field(default_factory=set)
    type_counts: Dict[int, int] = field(default_factory=dict)
    type_estimates: Dict[int, WeaponDamageEstimate] = field(default_factory=dict)
    target_type_estimates: Dict[str, Dict[int, WeaponDamageEstimate]] = field(default_factory=dict)
    target_damage_observations: Dict[str, Dict[Tuple[int, ...], WeaponObservedDamage]] = field(default_factory=dict)


@dataclass(frozen=True)
class WeaponRecommendation:
    binding: WeaponBinding
    expected_damage: int
    selection_damage: int
    actual_damage: Optional[int]
    actual_observations: int
    matched_name: str
    paragon_ranks: int
    learned_types: Tuple[int, ...]
    estimated_components: Tuple[Tuple[int, int], ...]
    healing_types: Tuple[int, ...]
    ignored_types: Tuple[int, ...]
    special_name: str
    signature_observations: int
    estimate_observations: int


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
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.last_poll_error = ""

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.hp_address = 0
        self.hp_owner_address = 0
        self.max_hp_address = 0
        self.max_hp_observed = 0
        self.drink_generation = 0
        self.drinking = False
        self.last_poll_error = ""
        self.monitor_stop = threading.Event()
        try:
            current_hp, max_hp, percent, source = self._read_health_snapshot()
            self.set_status(f"Armed {current_hp}/{max_hp} ({percent:.1f}%) [{source}]")
        except Exception:
            self.set_status("Armed")
        self.monitor_thread = threading.Thread(
            target=self._run_health_monitor,
            name=f"AutoDrinkMonitor-{self.client.pid}",
            daemon=True,
        )
        self.monitor_thread.start()
        self.host.emit("info", f"{self.client.display_name}: AutoDrink started", script_id=self.script_id)

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.drinking = False
        self.drink_generation += 1
        self.monitor_stop.set()
        self._close_process_handle()
        self.host.emit("info", f"{self.client.display_name}: AutoDrink stopped", script_id=self.script_id)

    def needs_chat_feed(self) -> bool:
        return False

    def on_chat_line(self, sequence: int, text: str):
        return

    def _clear_hp_path(self):
        self.hp_address = 0
        self.hp_owner_address = 0
        self.max_hp_address = 0

    def _resolve_hp_address(self):
        module_base = int((self.client.query or {}).get("module_base", 0)) or kLegacyImageBase
        pointer1_address = module_base + kLegacyHpPointerOffset
        pointer2_holder = self._read_u32(pointer1_address)
        if pointer2_holder == 0:
            raise RuntimeError(f"hp pointer1 at 0x{pointer1_address:08X} was null")
        hp_owner = self._read_u32(pointer2_holder + kLegacyHpOwnerOffset)
        if hp_owner == 0:
            raise RuntimeError(f"hp owner at 0x{pointer2_holder + kLegacyHpOwnerOffset:08X} was null")

        hp_address = hp_owner + kLegacyCurrentHpOffset
        if hp_owner != self.hp_owner_address or hp_address != self.hp_address:
            previous_address = self.hp_address
            self.hp_owner_address = hp_owner
            self.hp_address = hp_address
            self.max_hp_address = 0
            if previous_address != hp_address:
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

    def _plausible_health_value(self, value: int) -> bool:
        return 0 < int(value) <= 20000

    def _plausible_max_health_value(self, value: int, current_hp: int) -> bool:
        return int(value) >= int(current_hp) and 0 < int(value) <= 20000

    def _health_poll_interval_seconds(self) -> float:
        return max(float(self.config.get("poll_interval", 1.0)), 0.10)

    def _run_health_monitor(self):
        while not self.monitor_stop.is_set():
            if not self.enabled:
                break
            try:
                self._poll_health_once()
                if self.last_poll_error:
                    self.host.emit(
                        "info",
                        f"{self.client.display_name}: AutoDrink HP polling recovered",
                        script_id=self.script_id,
                    )
                    self.last_poll_error = ""
            except Exception as exc:
                error_text = str(exc)
                if error_text != self.last_poll_error:
                    self.last_poll_error = error_text
                    self.host.emit(
                        "error",
                        f"{self.client.display_name}: AutoDrink HP poll failed: {error_text}",
                        script_id=self.script_id,
                    )
                self.set_status("HP poll failed")
            self.monitor_stop.wait(self._health_poll_interval_seconds())

    def _poll_health_once(self):
        if not self.enabled or self.drinking:
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
        last_error = None
        for attempt in range(3):
            try:
                self._resolve_hp_address()
                current_hp = self._read_u16(self.hp_address)
                if not self._plausible_health_value(current_hp):
                    raise RuntimeError(f"current HP at 0x{self.hp_address:08X} was implausible ({current_hp})")

                max_hp = 0
                source = "observed"
                if self.max_hp_address:
                    try:
                        candidate = self._read_u16(self.max_hp_address)
                        if self._plausible_max_health_value(candidate, current_hp):
                            max_hp = candidate
                            source = f"probe+0x{self.max_hp_address - self.hp_address:X}"
                        else:
                            self.max_hp_address = 0
                    except Exception:
                        self.max_hp_address = 0

                if max_hp == 0:
                    candidate = self._guess_max_hp(current_hp)
                    if self._plausible_max_health_value(candidate, current_hp):
                        max_hp = candidate
                        source = f"probe+0x{self.max_hp_address - self.hp_address:X}"

                self.max_hp_observed = max(self.max_hp_observed, current_hp, max_hp)
                if max_hp == 0:
                    max_hp = self.max_hp_observed
                if not self._plausible_max_health_value(max_hp, current_hp):
                    raise RuntimeError(f"max HP was implausible ({max_hp}) for current HP {current_hp}")

                percent = (float(current_hp) * 100.0 / float(max_hp)) if max_hp > 0 else 100.0
                return current_hp, max_hp, percent, source
            except Exception as exc:
                last_error = exc
                self._clear_hp_path()
                if attempt < 2:
                    continue
        raise last_error if last_error is not None else RuntimeError("health snapshot failed")

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


class StopHittingScript(ClientScriptBase):
    script_id = "stop_hitting"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.db = hgx_data.load_default_database()
        self.enabled = False
        self.interrupting = False
        self.interrupt_generation = 0
        self.identity_wait_logged = False
        self.unknown_targets: Set[str] = set()
        self.protected_hits = 0
        self.last_target = ""
        self.last_damage_summary = ""
        self.last_ignored_actor = ""

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.interrupting = False
        self.interrupt_generation = 0
        self.identity_wait_logged = False
        self.unknown_targets = set()
        self.protected_hits = 0
        self.last_target = ""
        self.last_damage_summary = ""
        self.last_ignored_actor = ""
        self.set_status("Armed")
        self.host.emit("info", f"{self.client.display_name}: Stop Hitting started", script_id=self.script_id)

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.interrupting = False
        self.interrupt_generation += 1
        self.host.emit("info", f"{self.client.display_name}: Stop Hitting stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return
        if " damages " not in str(text or "").lower():
            return

        damage_line = hgx_combat.parse_damage_line(text)
        if damage_line is None:
            return

        character_name = self._character_name()
        if not character_name:
            self.set_status("Waiting for character name")
            if not self.identity_wait_logged:
                self.identity_wait_logged = True
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Stop Hitting is waiting for character identity before parsing damage lines",
                    script_id=self.script_id,
                )
            return

        character_key = hgx_combat.normalize_actor_name(character_name).lower()
        if not self._damage_attacker_matches_character(damage_line.attacker, character_key):
            self.last_ignored_actor = damage_line.attacker
            return

        record = self.db.lookup(damage_line.defender)
        if record is None:
            defender_key = damage_line.defender.lower()
            if defender_key not in self.unknown_targets:
                self.unknown_targets.add(defender_key)
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Stop Hitting has no characters.d entry for '{damage_line.defender}'",
                    script_id=self.script_id,
                )
            return

        if not self.db.is_area_kickback(record.name):
            if not self.interrupting:
                self.set_status("Armed")
            return

        self.protected_hits += 1
        self.last_target = record.name
        self.last_damage_summary = self._damage_summary(damage_line)

        if self._protected_target_allows_mammon_wrath(record.name, damage_line):
            self.set_status(f"{record.name}: Mammon's Wrath allowed")
            self.host.notify_state_changed()
            return

        if self.interrupting:
            self.set_status(f"{record.name}: interrupting")
            self.host.notify_state_changed()
            return

        slot = int(self.config.get("slot", 2))
        page = _parse_quickbar_bank_page(self.config.get("page", 0))
        trigger_name = self.host.format_slot(page, slot)
        result = self.host.trigger_slot(slot, page=page)
        self._begin_interrupt_cooldown()

        self.set_status(f"{record.name}: drinking {trigger_name}")
        self.host.emit(
            "info",
            (
                f"{self.client.display_name}: Stop Hitting triggered on area kickback target='{record.name}' "
                f"damage={self.last_damage_summary} potion={trigger_name} "
                f"success={result['success']} rc={result['rc']} aux={result['aux_rc']} "
                f"path={result['path']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", True)):
            self.host.send_console(
                f"SimKeys stop-hitting {record.name}: drank {trigger_name} after {self.last_damage_summary}"
            )

    def _character_name(self) -> str:
        live_name = (self.host.client.character_name or "").strip()
        if live_name:
            return live_name
        cached_name = (self.client.character_name or "").strip()
        if cached_name:
            return cached_name
        return ""

    def _damage_summary(self, damage_line) -> str:
        parts = [f"{component.amount} {component.type_name}" for component in damage_line.components]
        typed = ", ".join(parts) if parts else "no typed components"
        return f"{damage_line.total} ({typed})"

    def _damage_attacker_matches_character(self, attacker: str, character_key: str) -> bool:
        attacker_key = hgx_combat.normalize_actor_name(attacker).lower()
        if not attacker_key or not character_key:
            return False
        if attacker_key == character_key:
            return True

        # HGX damage lines can prefix the actor with sources such as
        # "Sneak Attack: Character". Only the final actor segment should be
        # compared against the current player.
        parts = [
            hgx_combat.normalize_actor_name(part).lower()
            for part in str(attacker or "").split(":")
            if hgx_combat.normalize_actor_name(part)
        ]
        return bool(parts and parts[-1] == character_key)

    def _damage_line_looks_like_mammon_wrath(self, damage_line) -> bool:
        observed_types = set()
        has_physical = False
        for component in damage_line.components:
            damage_type = getattr(component, "damage_type", None)
            if damage_type in WEAPON_SIGNATURE_TYPES:
                observed_types.add(int(damage_type))
            type_name = str(getattr(component, "type_name", "") or "").strip().lower()
            if damage_type in WEAPON_PHYSICAL_TYPES or type_name in {"physical", "bludgeoning", "piercing", "slashing"}:
                has_physical = True
        return has_physical and tuple(sorted(observed_types)) == MAMMONS_WRATH_SIGNATURE

    def _protected_target_allows_mammon_wrath(self, target_name: str, damage_line) -> bool:
        if str(target_name or "").strip().lower() not in MAMMONS_TEAR_TARGETS:
            return False

        try:
            state = self.host.get_state("auto_aa")
        except Exception:
            return False
        if not state.get("running"):
            return False

        details = dict(state.get("details", {}))
        if not details.get("weapon_mode"):
            return False

        if bool(details.get("current_is_mammon_wrath")) or bool(details.get("pending_is_mammon_wrath")):
            return True

        analysis = dict(details.get("target_analysis", {}))
        matched_name = str(analysis.get("matched_name") or "").strip().lower()
        target = str(analysis.get("target") or "").strip().lower()
        if target_name.lower() in {matched_name, target} and bool(analysis.get("recommended_is_mammon_wrath")):
            return True

        return False

    def _begin_interrupt_cooldown(self):
        self.interrupting = True
        self.interrupt_generation += 1
        generation = self.interrupt_generation
        delay = max(float(self.config.get("cooldown_seconds", 3.0)), 0.1)

        def clear_after_delay():
            time.sleep(delay)
            if generation != self.interrupt_generation:
                return
            self.interrupting = False
            self.set_status("Armed")

        threading.Thread(target=clear_after_delay, name=f"StopHittingCooldown-{self.client.pid}", daemon=True).start()

    def get_state_details(self) -> dict:
        return {
            "protected_hits": self.protected_hits,
            "last_target": self.last_target,
            "last_damage_summary": self.last_damage_summary,
            "interrupting": self.interrupting,
            "last_ignored_actor": self.last_ignored_actor,
        }


class AutoAAScript(ClientScriptBase):
    script_id = "auto_aa"
    MODE_ARCANE_ARCHER = "Arcane Archer"
    MODE_ZEN_RANGER = "Zen Ranger"
    MODE_DIVINE_SLINGER = "Divine Slinger"
    MODE_GNOMISH_INVENTOR = "Gnomish Inventor"
    MODE_WEAPON_SWAP = "Weapon Swap"
    MAX_WEAPON_BINDINGS = len(WEAPON_BINDING_KEYS)
    WEAPON_SIGNATURE_CONFIRM_THRESHOLD = 2
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
        self.pending_weapon_unarm = False
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
        self.pending_weapon_unarm = False
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
        self.pending_weapon_unarm = False
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
        self.pending_weapon_unarm = False
        self.pending_weapon_ready_at = 0.0
        self.pending_weapon_requested_at = 0.0
        self.pending_weapon_request_sequence = 0
        self.pending_weapon_feedback_seen = False
        self.pending_weapon_equipped_feedback_seen = False
        self.pending_weapon_feedback_sequence = 0
        self.pending_weapon_conceal_seen = False
        self.pending_weapon_conceal_sequence = 0
        self.pending_weapon_ignored_damage_count = 0

    def _pending_weapon_display(self) -> str:
        if not self.pending_weapon_key:
            return ""
        if self.pending_weapon_unarm:
            source = self._binding_display(self.pending_weapon_key)
            return f"{WEAPON_CURRENT_UNARMED} via {source}"
        return self._binding_display(self.pending_weapon_key)

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

        pending_display = self._pending_weapon_display()
        self._clear_pending_weapon_state()
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} canceled pending {pending_display} ({reason})",
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _damage_signature(self, observed_types: Set[int]) -> Tuple[int, ...]:
        if not observed_types:
            return ()
        return tuple(sorted(damage_type for damage_type in observed_types if damage_type in WEAPON_SIGNATURE_TYPES))

    def _is_p2_signature(self, signature: Tuple[int, ...]) -> bool:
        signature_types = {int(damage_type) for damage_type in signature if isinstance(damage_type, int)}
        if len(signature_types) != 3:
            return False
        elemental_count = len(signature_types.intersection(WEAPON_ELEMENTAL_TYPES))
        exotic_count = len(signature_types.intersection(WEAPON_EXOTIC_TYPES))
        return elemental_count == 2 and exotic_count == 1

    def _profile_is_p2(self, profile: Optional[WeaponLearningProfile]) -> bool:
        return profile is not None and str(profile.dynamic_kind or "").strip() == P2_SPECIAL_NAME

    def _profile_target_key(self, creature_name: str) -> str:
        combat_profile = self.db._resolve_combat_profile(creature_name)
        if combat_profile is not None and combat_profile.matched_name:
            return str(combat_profile.matched_name).strip().lower()
        return str(creature_name or "").strip().lower()

    def _matching_profile_for_observed_types(
        self,
        observed_types: Set[int],
        exclude_key: str = "",
    ) -> Optional[WeaponLearningProfile]:
        signature = self._damage_signature(observed_types)
        if not signature:
            return None

        for profile in self.weapon_profiles.values():
            if profile.binding.key == exclude_key:
                continue
            if tuple(profile.stable_signature) == signature:
                return profile

        dynamic_matches = [
            profile
            for profile in self.weapon_profiles.values()
            if profile.binding.key != exclude_key
            and self._profile_is_p2(profile)
            and signature in profile.signature_counts
        ]
        if len(dynamic_matches) == 1:
            return dynamic_matches[0]
        return None

    def _reset_weapon_profile_for_rediscovery(self, profile: WeaponLearningProfile):
        profile.observations = 0
        profile.attack_attempts = 0
        profile.last_seen_at = 0.0
        profile.last_attack_at = 0.0
        profile.stable_signature = ()
        profile.stable_signature_observations = 0
        profile.candidate_signature = ()
        profile.candidate_signature_streak = 0
        profile.current_signature = ()
        profile.mismatch_streak = 0
        profile.mismatch_total = 0
        profile.last_mismatch_at = 0.0
        profile.rediscoveries += 1
        profile.dynamic_kind = ""
        profile.signature_counts.clear()
        profile.target_signatures.clear()
        profile.p2_verification_targets.clear()
        profile.type_counts.clear()
        profile.type_estimates.clear()
        profile.target_type_estimates.clear()
        profile.target_damage_observations.clear()

    def _profile_signature_types(self, profile: Optional[WeaponLearningProfile]) -> Set[int]:
        if profile is None:
            return set()
        if self._profile_is_p2(profile) and profile.current_signature:
            return set(profile.current_signature)
        return set(profile.stable_signature)

    def _profile_known_damage_types(self, profile: Optional[WeaponLearningProfile]) -> Set[int]:
        if profile is None:
            return set()
        known_types = set(profile.stable_signature) | set(profile.current_signature)
        for signature in profile.signature_counts.keys():
            known_types.update(signature)
        for signature in profile.target_signatures.values():
            known_types.update(signature)
        known_types.update(profile.type_counts.keys())
        known_types.update(profile.type_estimates.keys())
        return known_types

    def _profile_target_component_estimates(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
    ) -> Dict[int, float]:
        if profile is None:
            return {}

        target_key = self._profile_target_key(creature_name)
        if not target_key:
            return {}

        estimates = profile.target_type_estimates.get(target_key) or {}
        components: Dict[int, float] = {}
        for damage_type, estimate in estimates.items():
            if damage_type not in WEAPON_ESTIMATE_TYPES:
                continue
            if estimate.observations <= 0:
                continue
            components[damage_type] = float(estimate.base_estimate)
        return components

    def _profile_signature_for_target(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
    ) -> Tuple[int, ...]:
        if profile is None:
            return ()
        target_key = self._profile_target_key(creature_name)
        if not target_key:
            return ()
        signature = profile.target_signatures.get(target_key)
        if signature:
            return tuple(signature)
        return ()

    def _generic_p2_signature_for_target(self, creature_name: str) -> Tuple[int, ...]:
        combat_profile = self.db._resolve_combat_profile(creature_name)
        if combat_profile is None:
            return ()

        def rank_type(damage_type: int) -> Tuple[int, int, int]:
            healing_value = int(combat_profile.healing[damage_type] or 0)
            if healing_value != 0:
                return (1, healing_value, -int(damage_type))
            expected = self._expected_component_damage_for_target(combat_profile, damage_type, 100.0)
            return (0, expected, -int(damage_type))

        elemental = sorted(
            ((rank_type(damage_type), int(damage_type)) for damage_type in WEAPON_ELEMENTAL_TYPES),
            reverse=True,
        )
        exotic = sorted(
            ((rank_type(damage_type), int(damage_type)) for damage_type in WEAPON_EXOTIC_TYPES),
            reverse=True,
        )
        if len(elemental) < 2 or not exotic:
            return ()
        return tuple(sorted((elemental[0][1], elemental[1][1], exotic[0][1])))

    def _profile_requires_p2_verification(self, profile: Optional[WeaponLearningProfile]) -> bool:
        if profile is None or self._profile_is_p2(profile):
            return False
        signature = tuple(profile.stable_signature)
        return self._is_p2_signature(signature)

    def _profile_p2_verification_complete(self, profile: Optional[WeaponLearningProfile]) -> bool:
        if not self._profile_requires_p2_verification(profile):
            return True
        return len(getattr(profile, "p2_verification_targets", set()) or set()) >= 2

    def _record_p2_verification_target(
        self,
        profile: WeaponLearningProfile,
        creature_name: str,
        observed_signature: Tuple[int, ...],
    ):
        if not self._profile_requires_p2_verification(profile):
            return
        if tuple(profile.stable_signature) != tuple(observed_signature):
            return
        target_key = self._profile_target_key(creature_name)
        if not target_key:
            return
        generic_signature = self._generic_p2_signature_for_target(creature_name)
        if generic_signature and generic_signature != tuple(profile.stable_signature):
            profile.p2_verification_targets.add(target_key)

    def _apply_observed_damage_map(
        self,
        observed_map: Dict[Tuple[int, ...], WeaponObservedDamage],
        signature: Tuple[int, ...],
        actual_total: int,
    ):
        observed = observed_map.get(signature)
        if observed is None:
            observed = WeaponObservedDamage()
            observed_map[signature] = observed

        prior_weight = min(int(observed.observations), 8)
        if observed.observations <= 0:
            observed.average_total = float(actual_total)
        else:
            observed.average_total = ((observed.average_total * float(prior_weight)) + float(actual_total)) / float(prior_weight + 1)
        observed.observations += 1
        observed.last_total = int(actual_total)
        observed.max_total = max(int(observed.max_total), int(actual_total))

    def _profile_target_actual_damage(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
        signature: Tuple[int, ...],
    ) -> Tuple[Optional[int], int]:
        if profile is None or not signature:
            return None, 0

        target_key = self._profile_target_key(creature_name)
        if not target_key:
            return None, 0

        observed_map = profile.target_damage_observations.get(target_key) or {}
        observed = observed_map.get(tuple(signature))
        if observed is None or observed.observations <= 0:
            return None, 0
        return int(round(observed.average_total)), int(observed.observations)

    def _record_profile_target_actual_damage(
        self,
        profile: WeaponLearningProfile,
        target_key: str,
        signature: Tuple[int, ...],
        damage_line,
    ):
        if not target_key or not signature:
            return

        ignored_types = set(self._weapon_ignored_damage_types(profile))
        relevant_types = set(signature) | set(WEAPON_PHYSICAL_TYPES)
        actual_total = 0
        saw_relevant = False
        for component in damage_line.components:
            damage_type = getattr(component, "damage_type", None)
            if damage_type in ignored_types:
                continue
            if damage_type in relevant_types:
                actual_total += int(component.amount)
                saw_relevant = True

        if not saw_relevant:
            return

        observed_map = profile.target_damage_observations.setdefault(target_key, {})
        self._apply_observed_damage_map(observed_map, tuple(signature), actual_total)

    def _selection_damage_score(self, expected_damage: int, actual_damage: Optional[int], actual_observations: int) -> int:
        if actual_damage is None or actual_observations <= 0:
            return int(expected_damage)
        actual_weight = min(max(int(actual_observations), 0) + 1, 4)
        blended = ((float(expected_damage) * 1.0) + (float(actual_damage) * float(actual_weight))) / float(1 + actual_weight)
        return int(round(blended))

    def _profile_component_estimates(self, profile: Optional[WeaponLearningProfile]) -> Dict[int, float]:
        if profile is None:
            return {}

        components: Dict[int, float] = {}
        for damage_type, estimate in profile.type_estimates.items():
            if damage_type not in WEAPON_ESTIMATE_TYPES:
                continue
            if estimate.observations <= 0 or estimate.base_estimate <= 0.0:
                continue
            components[damage_type] = float(estimate.base_estimate)
        return components

    def _is_mammons_tear_target_name(self, creature_name: str) -> bool:
        return str(creature_name or "").strip().lower() in MAMMONS_TEAR_TARGETS

    def _resolve_target_record(self, creature_name: str):
        if not creature_name:
            return None
        return self.db.lookup(creature_name)

    def _is_mammons_tear_target(self, creature_name: str) -> bool:
        record = self._resolve_target_record(creature_name)
        if record is None:
            return self._is_mammons_tear_target_name(creature_name)
        return self._is_mammons_tear_target_name(record.name)

    def _is_mammon_wrath_profile(self, profile: Optional[WeaponLearningProfile]) -> bool:
        if profile is None:
            return False
        if self._profile_is_p2(profile):
            return False
        if len([signature for signature in profile.signature_counts.keys() if signature]) > 1:
            return False
        signature = tuple(sorted(damage_type for damage_type in self._profile_known_damage_types(profile) if damage_type in WEAPON_SIGNATURE_TYPES))
        if signature != MAMMONS_WRATH_SIGNATURE:
            return False
        has_physical = any(
            profile.type_counts.get(damage_type, 0) > 0 or damage_type in profile.type_estimates
            for damage_type in WEAPON_PHYSICAL_TYPES
        )
        return has_physical or profile.observations > 0

    def _special_weapon_name_for_profile(self, profile: Optional[WeaponLearningProfile]) -> str:
        if self._profile_is_p2(profile):
            return P2_SPECIAL_NAME
        if self._is_mammon_wrath_profile(profile):
            return "Mammon's Wrath"
        return ""

    def _mammon_wrath_profile_key(self) -> str:
        for binding_key, profile in self.weapon_profiles.items():
            if self._is_mammon_wrath_profile(profile):
                return binding_key
        return ""

    def _mammon_wrath_candidate(self, candidates: List[WeaponRecommendation]) -> Optional[WeaponRecommendation]:
        for candidate in candidates:
            if candidate.special_name == "Mammon's Wrath":
                return candidate
        return None

    def _weapon_ignored_damage_types(
        self,
        profile: Optional[WeaponLearningProfile],
        components: Optional[Dict[int, float]] = None,
    ) -> Tuple[int, ...]:
        if profile is None:
            return ()

        negative_estimate = profile.type_estimates.get(10)
        if negative_estimate is None or negative_estimate.observations <= 0:
            return ()
        if int(negative_estimate.max_observed_amount) > 1:
            return ()

        effective_components = components if components is not None else self._profile_component_estimates(profile)
        has_other_damage = any(
            damage_type != 10 and float(base_damage) > 0.0
            for damage_type, base_damage in effective_components.items()
        )
        if not has_other_damage:
            return ()

        return (10,)

    def _effective_weapon_components(
        self,
        profile: Optional[WeaponLearningProfile],
        components: Optional[Dict[int, float]] = None,
    ) -> Tuple[Dict[int, float], Tuple[int, ...]]:
        components = dict(components if components is not None else self._profile_component_estimates(profile))
        ignored_types = self._weapon_ignored_damage_types(profile, components)
        if ignored_types:
            ignored_set = set(ignored_types)
            components = {
                damage_type: base_damage
                for damage_type, base_damage in components.items()
                if damage_type not in ignored_set
            }
        return components, ignored_types

    def _profile_estimate_observations(self, profile: Optional[WeaponLearningProfile]) -> int:
        if profile is None:
            return 0
        return sum(int(estimate.observations) for estimate in profile.type_estimates.values())

    def _profile_learning_complete(self, profile: Optional[WeaponLearningProfile]) -> bool:
        if profile is None:
            return False
        has_signature_state = bool(profile.stable_signature) or self._profile_is_p2(profile)
        return has_signature_state and bool(profile.type_estimates) and self._profile_p2_verification_complete(profile)

    def _format_estimated_components(self, components: Dict[int, float]) -> str:
        if not components:
            return ""
        parts = []
        for damage_type in sorted(components):
            parts.append(f"{_format_damage_type_label(damage_type)}~{int(round(components[damage_type]))}")
        return "/".join(parts)

    def _format_ignored_damage_types(self, profile: Optional[WeaponLearningProfile], ignored_types: Tuple[int, ...]) -> str:
        if profile is None or not ignored_types:
            return ""
        parts = []
        for damage_type in ignored_types:
            estimate = profile.type_estimates.get(damage_type)
            if estimate is not None and int(estimate.max_observed_amount) > 0:
                parts.append(f"{_format_damage_type_label(damage_type)}~{int(estimate.max_observed_amount)}")
            else:
                parts.append(_format_damage_type_label(damage_type))
        return "/".join(parts)

    def _weapon_profile_summary(self, profile: WeaponLearningProfile) -> str:
        parts = []
        special_name = self._special_weapon_name_for_profile(profile)
        if special_name:
            parts.append(special_name)
        if self._profile_is_p2(profile):
            if profile.current_signature:
                parts.append("Current " + self._format_weapon_type_set(set(profile.current_signature)))
            else:
                parts.append("Adaptive")
            distinct_signatures = [
                signature
                for signature in profile.signature_counts.keys()
                if self._is_p2_signature(signature)
            ]
            if distinct_signatures:
                parts.append(f"Seen {len(distinct_signatures)} sigs")
        else:
            known_types = self._profile_known_damage_types(profile)
            if profile.stable_signature:
                parts.append("Types " + self._format_weapon_type_set(known_types))
            elif profile.candidate_signature:
                parts.append(
                    "Seen "
                    + self._format_weapon_type_set(set(profile.candidate_signature))
                    + f"? ({profile.candidate_signature_streak}/{self.WEAPON_SIGNATURE_CONFIRM_THRESHOLD})"
                )
            elif known_types:
                parts.append("Seen " + self._format_weapon_type_set(known_types))
            else:
                parts.append("Unknown")

        components = self._profile_component_estimates(profile)
        if components:
            parts.append("Base " + self._format_estimated_components(components))
        ignored_types = self._weapon_ignored_damage_types(profile, components)
        if ignored_types:
            parts.append(f"Ignore {self._format_ignored_damage_types(profile, ignored_types)} rider")
        if self._profile_requires_p2_verification(profile):
            verified = len(profile.p2_verification_targets)
            parts.append(f"P2 check {verified}/2")

        suffix = f"obs {profile.observations}, attacks {profile.attack_attempts}"
        if self._profile_is_p2(profile):
            suffix += ", adaptive"
        if profile.stable_signature_observations:
            suffix += f", stable {profile.stable_signature_observations}"
        if profile.mismatch_streak:
            suffix += f", mismatch {profile.mismatch_streak}/{self.WEAPON_REDISCOVERY_MISMATCH_THRESHOLD}"
        if profile.rediscoveries:
            suffix += f", rediscovered {profile.rediscoveries}"
        parts.append(suffix)
        return ", ".join(part for part in parts if part)

    def _weapon_swap_cooldown_seconds(self) -> float:
        return max(float(self.config.get("swap_cooldown_seconds", 6.2)), 0.1)

    def _confirm_pending_weapon(self, reason: str) -> bool:
        if not self.pending_weapon_key:
            return False
        if self.pending_weapon_unarm:
            return self._confirm_unarmed_state(reason)

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
                    self.set_status(f"awaiting {self._pending_weapon_display()} damage")
                else:
                    self.set_status(f"waiting {self._pending_weapon_display()} damage")
                self.host.notify_state_changed()
            else:
                self.host.notify_state_changed()

    def _observed_types_fit_profile(self, profile: Optional[WeaponLearningProfile], observed_types: Set[int]) -> bool:
        if profile is None or not observed_types:
            return False
        if self._profile_is_p2(profile):
            return self._damage_signature(observed_types) in profile.signature_counts
        known_types = self._profile_signature_types(profile)
        return bool(known_types) and observed_types == known_types

    def _observed_types_exactly_match_profile(self, profile: Optional[WeaponLearningProfile], observed_types: Set[int]) -> bool:
        if profile is None or not observed_types:
            return False
        if self._profile_is_p2(profile):
            return self._damage_signature(observed_types) in profile.signature_counts
        known_types = self._profile_signature_types(profile)
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
            f"{defender_name}: waiting round boundary for {self._pending_weapon_display()}"
        )
        if self.pending_weapon_ignored_damage_count in (1, 4):
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} ignored pre-boundary "
                    f"damage while waiting for {self._pending_weapon_display()}; "
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
        self.set_status(f"{attack.defender}: round boundary for {self._pending_weapon_display()}")
        self.host.notify_state_changed()

    def _damage_line_is_physical_only(self, damage_line) -> bool:
        has_physical = False
        for component in damage_line.components:
            if component.damage_type in WEAPON_SIGNATURE_TYPES:
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
            if component.damage_type in WEAPON_SIGNATURE_TYPES:
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

            matching_profile = self._matching_profile_for_observed_types(
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

        current_types = self._profile_signature_types(current_profile)
        pending_types = self._profile_signature_types(pending_profile)

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

        if self.pending_weapon_unarm:
            if current_types and self._observed_types_fit_profile(current_profile, observed_types):
                self.set_status(f"{defender_name}: awaiting {self._pending_weapon_display()} damage")
                return current_profile

            matching_profile = self._matching_profile_for_observed_types(
                observed_types,
                exclude_key="",
            )
            if matching_profile is not None and matching_profile.binding.key != self.pending_weapon_key:
                matching_display = self._binding_display(matching_profile.binding.key)
                self._cancel_pending_weapon(f"damage matched {matching_display}")
                self._set_current_weapon_from_equipped_key(matching_profile.binding.key, "damage matched a learned slot")
                return matching_profile

            self.set_status(f"{defender_name}: awaiting {self._pending_weapon_display()} damage")
            return None

        if pending_types and self._observed_types_fit_profile(pending_profile, observed_types):
            self._confirm_pending_weapon("matched pending damage")
            return pending_profile

        if current_types and self._observed_types_fit_profile(current_profile, observed_types):
            self.set_status(f"{defender_name}: awaiting {self._pending_weapon_display()} damage")
            return current_profile

        matching_profile = self._matching_profile_for_observed_types(
            observed_types,
            exclude_key="",
        )
        if matching_profile is not None:
            matching_key = matching_profile.binding.key
            if matching_key == self.pending_weapon_key:
                self._confirm_pending_weapon("damage matched pending learned slot")
            elif equipped_key and equipped_key != matching_key:
                self.set_status(f"{defender_name}: awaiting {self._pending_weapon_display()} damage")
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
        pending_has_no_stable_signature = not pending_profile.stable_signature
        if (
            pending_has_no_stable_signature
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

        self.set_status(f"{defender_name}: awaiting {self._pending_weapon_display()} damage")
        return None

    def _format_weapon_type_set(self, damage_types: Set[int]) -> str:
        if not damage_types:
            return "physical-only"
        return "/".join(_format_damage_type_label(value) for value in sorted(damage_types))

    def _record_profile_signature_mismatch(
        self,
        profile: WeaponLearningProfile,
        observed_types: Set[int],
        defender_name: str,
        now: float,
    ) -> bool:
        expected_types = self._profile_signature_types(profile)
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

        equipped_key = str(self.weapon_equipped_key or "").strip()
        if equipped_key and equipped_key != profile.binding.key and equipped_key in self.weapon_profiles:
            self._set_current_weapon_from_equipped_key(
                equipped_key,
                "mismatch belonged to a different equipped slot",
            )
            profile.mismatch_streak = 0
            self.host.notify_state_changed()
            return False

        if self.current_weapon_key not in (profile.binding.key, WEAPON_CURRENT_UNKNOWN):
            if self.current_weapon_key in self.weapon_profiles:
                self._set_current_weapon_from_equipped_key(
                    self.current_weapon_key,
                    "mismatch belonged to the current tracked slot",
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

    def _profile_accepts_signature_variation(
        self,
        profile: WeaponLearningProfile,
        observed_types: Set[int],
    ) -> bool:
        signature = self._damage_signature(observed_types)
        if not signature:
            return False
        if self._profile_is_p2(profile):
            return self._is_p2_signature(signature)
        stable_signature = tuple(profile.stable_signature)
        if not stable_signature:
            return False
        return self._is_p2_signature(stable_signature) and self._is_p2_signature(signature)

    def _update_profile_dynamic_kind(self, profile: WeaponLearningProfile) -> bool:
        if self._profile_is_p2(profile):
            return False
        confirmed_signatures = [
            signature
            for signature, count in profile.signature_counts.items()
            if count >= self.WEAPON_SIGNATURE_CONFIRM_THRESHOLD and self._is_p2_signature(signature)
        ]
        if len(confirmed_signatures) < 2:
            return False
        profile.dynamic_kind = P2_SPECIAL_NAME
        profile.p2_verification_targets.clear()
        profile.candidate_signature = ()
        profile.candidate_signature_streak = 0
        profile.mismatch_streak = 0
        return True

    def _observe_profile_signature(
        self,
        profile: WeaponLearningProfile,
        observed_types: Set[int],
        target_key: str = "",
    ) -> bool:
        signature = self._damage_signature(observed_types)
        if not signature:
            return False

        profile.current_signature = signature
        profile.signature_counts[signature] = profile.signature_counts.get(signature, 0) + 1
        if target_key:
            profile.target_signatures[target_key] = signature

        if self._profile_is_p2(profile):
            profile.candidate_signature = ()
            profile.candidate_signature_streak = 0
            profile.mismatch_streak = 0
            return profile.signature_counts[signature] == 1

        if profile.stable_signature:
            if tuple(profile.stable_signature) == signature:
                profile.stable_signature_observations += 1
                profile.candidate_signature = ()
                profile.candidate_signature_streak = 0
                return False
            if self._profile_accepts_signature_variation(profile, observed_types):
                if tuple(profile.candidate_signature) != signature:
                    profile.candidate_signature = signature
                    profile.candidate_signature_streak = 1
                else:
                    profile.candidate_signature_streak += 1
                return self._update_profile_dynamic_kind(profile)
            return False

        if tuple(profile.candidate_signature) != signature:
            profile.candidate_signature = signature
            profile.candidate_signature_streak = 1
            return False

        profile.candidate_signature_streak += 1
        if profile.candidate_signature_streak < self.WEAPON_SIGNATURE_CONFIRM_THRESHOLD:
            return False

        profile.stable_signature = signature
        profile.stable_signature_observations = profile.candidate_signature_streak
        profile.candidate_signature = ()
        profile.candidate_signature_streak = 0
        profile.mismatch_streak = 0
        return True

    def _estimate_component_base_damage(self, combat_profile, component) -> Optional[float]:
        damage_type = component.damage_type
        if not isinstance(damage_type, int) or damage_type not in WEAPON_ESTIMATE_TYPES:
            return None
        if combat_profile is None:
            return None
        if combat_profile.healing[damage_type] != 0:
            return None

        immunity = int(combat_profile.immunity[damage_type] or 0)
        resistance = int(combat_profile.resistance[damage_type] or 0)
        reduction_factor = 1.0 - ((float(immunity) + 10.0 * float(combat_profile.paragon_ranks)) / 100.0)
        if reduction_factor <= 0.0:
            return None

        estimated = (float(component.amount) + float(resistance)) / reduction_factor
        return max(estimated, 0.0)

    def _apply_component_estimate_map(
        self,
        estimate_map: Dict[int, WeaponDamageEstimate],
        damage_type: int,
        observed_amount: int,
        sample: float,
    ):
        estimate = estimate_map.get(damage_type)
        if estimate is None:
            estimate = WeaponDamageEstimate()
            estimate_map[damage_type] = estimate

        prior_weight = min(int(estimate.observations), 12)
        if estimate.observations <= 0:
            estimate.base_estimate = float(sample)
        else:
            estimate.base_estimate = ((estimate.base_estimate * float(prior_weight)) + float(sample)) / float(prior_weight + 1)
        estimate.observations += 1
        estimate.last_base_sample = float(sample)
        estimate.last_observed_amount = int(observed_amount)
        estimate.max_observed_amount = max(int(estimate.max_observed_amount), int(observed_amount))

    def _apply_component_estimate(self, profile: WeaponLearningProfile, damage_type: int, observed_amount: int, sample: float):
        self._apply_component_estimate_map(profile.type_estimates, damage_type, observed_amount, sample)

    def _apply_weapon_profile_observation(
        self,
        profile: WeaponLearningProfile,
        damage_line,
        observed_types: Set[int],
        now: float,
    ) -> Tuple[bool, bool, Set[int], Set[int]]:
        before_known_types = set(self._profile_known_damage_types(profile))
        before_estimated_types = set(profile.type_estimates.keys())
        target_key = self._profile_target_key(damage_line.defender)

        if profile.stable_signature and observed_types != set(profile.stable_signature):
            if self._profile_accepts_signature_variation(profile, observed_types):
                profile.mismatch_streak = 0
            elif not self._record_profile_signature_mismatch(profile, observed_types, damage_line.defender, now):
                return False, False, set(), set()
        else:
            profile.mismatch_streak = 0

        profile.observations += 1
        profile.last_seen_at = now
        signature_changed = self._observe_profile_signature(profile, observed_types, target_key)
        combat_profile = self.db._resolve_combat_profile(damage_line.defender)
        for component in damage_line.components:
            damage_type = component.damage_type
            if not isinstance(damage_type, int) or damage_type not in WEAPON_ESTIMATE_TYPES:
                continue
            profile.type_counts[damage_type] = profile.type_counts.get(damage_type, 0) + 1
            sample = self._estimate_component_base_damage(combat_profile, component)
            if sample is None:
                continue
            self._apply_component_estimate(profile, damage_type, int(component.amount), sample)
            if target_key:
                target_estimates = profile.target_type_estimates.setdefault(target_key, {})
                self._apply_component_estimate_map(target_estimates, damage_type, int(component.amount), sample)
        self._record_p2_verification_target(profile, damage_line.defender, self._damage_signature(observed_types))
        if target_key:
            self._record_profile_target_actual_damage(profile, target_key, self._damage_signature(observed_types), damage_line)

        after_known_types = set(self._profile_known_damage_types(profile))
        after_estimated_types = set(profile.type_estimates.keys())
        return (
            True,
            signature_changed,
            after_known_types.difference(before_known_types),
            after_estimated_types.difference(before_estimated_types),
        )

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

        applied, signature_changed, new_types, new_estimates = self._apply_weapon_profile_observation(
            profile,
            damage_line,
            observed_types,
            now,
        )
        if not applied:
            return

        if signature_changed or new_types or new_estimates:
            learned_summary = self._weapon_profile_summary(profile)
            detail_parts = []
            if new_types:
                detail_parts.append("types " + self._format_weapon_type_set(new_types))
            if new_estimates:
                detail_parts.append("base " + self._format_weapon_type_set(new_estimates))
            detail_suffix = f" ({'; '.join(detail_parts)})" if detail_parts else ""
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} learned {self._binding_display(profile.binding.key)} "
                    f"from '{damage_line.defender}' -> "
                    f"{learned_summary}{detail_suffix}"
                ),
                script_id=self.script_id,
            )

    def _expected_component_damage_for_target(self, combat_profile, damage_type: int, base_damage: float) -> int:
        if combat_profile is None:
            return 0
        if not isinstance(damage_type, int):
            return 0
        if damage_type < 0 or damage_type >= len(combat_profile.immunity):
            return 0
        if float(base_damage) <= 0.0:
            return 0
        if combat_profile.healing[damage_type] != 0:
            return 0
        return self.db._apply_immunity_and_resistance(
            float(base_damage),
            combat_profile.immunity[damage_type],
            combat_profile.resistance[damage_type],
            combat_profile.paragon_ranks,
        )

    def _components_for_signature(
        self,
        profile: WeaponLearningProfile,
        creature_name: str,
        signature: Tuple[int, ...],
    ) -> Dict[int, float]:
        allowed_types = set(signature) | set(WEAPON_PHYSICAL_TYPES)
        target_components = self._profile_target_component_estimates(profile, creature_name)
        global_components = self._profile_component_estimates(profile)
        components: Dict[int, float] = {}
        for damage_type in sorted(allowed_types):
            if damage_type in target_components:
                components[damage_type] = float(target_components[damage_type])
            elif damage_type in global_components:
                components[damage_type] = float(global_components[damage_type])
        return components

    def _predict_p2_signature_components(
        self,
        profile: WeaponLearningProfile,
        creature_name: str,
    ) -> Tuple[Tuple[int, ...], Dict[int, float]]:
        combat_profile = self.db._resolve_combat_profile(creature_name)
        if combat_profile is None:
            return (), {}

        target_components = self._profile_target_component_estimates(profile, creature_name)
        global_components = self._profile_component_estimates(profile)

        def base_for_type(damage_type: int) -> Optional[float]:
            if damage_type in target_components:
                return float(target_components[damage_type])
            if damage_type in global_components:
                return float(global_components[damage_type])
            return None

        elemental_scores = []
        for damage_type in sorted(WEAPON_ELEMENTAL_TYPES):
            base_damage = base_for_type(damage_type)
            if base_damage is None:
                continue
            elemental_scores.append((
                self._expected_component_damage_for_target(combat_profile, damage_type, base_damage),
                damage_type,
            ))

        exotic_scores = []
        for damage_type in sorted(WEAPON_EXOTIC_TYPES):
            base_damage = base_for_type(damage_type)
            if base_damage is None:
                continue
            exotic_scores.append((
                self._expected_component_damage_for_target(combat_profile, damage_type, base_damage),
                damage_type,
            ))

        if len(elemental_scores) >= 2 and exotic_scores:
            elemental_scores.sort(key=lambda item: (item[0], item[1]), reverse=True)
            exotic_scores.sort(key=lambda item: (item[0], item[1]), reverse=True)
            signature = tuple(sorted((
                elemental_scores[0][1],
                elemental_scores[1][1],
                exotic_scores[0][1],
            )))
            return signature, self._components_for_signature(profile, creature_name, signature)

        best_signature: Tuple[int, ...] = ()
        best_components: Dict[int, float] = {}
        best_expected = -1
        for signature in sorted(profile.signature_counts.keys()):
            if not self._is_p2_signature(signature):
                continue
            components = self._components_for_signature(profile, creature_name, signature)
            if not components:
                continue
            estimate = self.db.estimate_custom_damage(creature_name, components)
            if estimate is None:
                continue
            if estimate.expected_damage > best_expected:
                best_signature = signature
                best_components = components
                best_expected = estimate.expected_damage
        return best_signature, best_components

    def _p2_target_healing_types(self, combat_profile) -> Tuple[int, ...]:
        if combat_profile is None:
            return ()
        return tuple(sorted(
            damage_type
            for damage_type in WEAPON_SIGNATURE_TYPES
            if combat_profile.healing[damage_type] != 0
        ))

    def _p2_candidate_for_target(
        self,
        profile: WeaponLearningProfile,
        creature_name: str,
    ) -> Optional[WeaponRecommendation]:
        combat_profile = self.db._resolve_combat_profile(creature_name)
        if combat_profile is None:
            return None

        healing_types = self._p2_target_healing_types(combat_profile)
        target_signature = self._profile_signature_for_target(profile, creature_name)
        raw_components = self._components_for_signature(profile, creature_name, target_signature) if target_signature else {}
        if not raw_components:
            predicted_signature, predicted_components = self._predict_p2_signature_components(profile, creature_name)
            if predicted_signature:
                target_signature = predicted_signature
                raw_components = predicted_components

        effective_components, ignored_types = self._effective_weapon_components(profile, raw_components)
        actual_damage, actual_observations = self._profile_target_actual_damage(profile, creature_name, target_signature)
        if healing_types:
            return WeaponRecommendation(
                binding=profile.binding,
                expected_damage=0,
                selection_damage=self._selection_damage_score(0, actual_damage, actual_observations),
                actual_damage=actual_damage,
                actual_observations=actual_observations,
                matched_name=combat_profile.matched_name,
                paragon_ranks=combat_profile.paragon_ranks,
                learned_types=tuple(sorted(target_signature)),
                estimated_components=tuple(
                    sorted(
                        (damage_type, int(round(base_damage)))
                        for damage_type, base_damage in raw_components.items()
                    )
                ),
                healing_types=healing_types,
                ignored_types=ignored_types,
                special_name=self._special_weapon_name_for_profile(profile),
                signature_observations=profile.stable_signature_observations,
                estimate_observations=self._profile_estimate_observations(profile),
            )

        if not effective_components:
            return None

        estimate = self.db.estimate_custom_damage(creature_name, effective_components)
        if estimate is None:
            return None

        return WeaponRecommendation(
            binding=profile.binding,
            expected_damage=estimate.expected_damage,
            selection_damage=self._selection_damage_score(estimate.expected_damage, actual_damage, actual_observations),
            actual_damage=actual_damage,
            actual_observations=actual_observations,
            matched_name=estimate.matched_name,
            paragon_ranks=estimate.paragon_ranks,
            learned_types=tuple(sorted(target_signature)),
            estimated_components=tuple(
                sorted(
                    (damage_type, int(round(base_damage)))
                    for damage_type, base_damage in raw_components.items()
                )
            ),
            healing_types=estimate.healing_types,
            ignored_types=ignored_types,
            special_name=self._special_weapon_name_for_profile(profile),
            signature_observations=profile.stable_signature_observations,
            estimate_observations=self._profile_estimate_observations(profile),
        )

    def _weapon_candidates_for_target(self, creature_name: str) -> List[WeaponRecommendation]:
        candidates: List[WeaponRecommendation] = []
        for profile in self.weapon_profiles.values():
            if self._profile_is_p2(profile):
                candidate = self._p2_candidate_for_target(profile, creature_name)
                if candidate is not None:
                    candidates.append(candidate)
                continue

            raw_components = self._profile_component_estimates(profile)
            if not raw_components:
                continue
            effective_components, ignored_types = self._effective_weapon_components(profile, raw_components)
            if not effective_components:
                continue

            estimate = self.db.estimate_custom_damage(creature_name, effective_components)
            if estimate is None:
                continue
            candidate_signature = self._profile_signature_for_target(profile, creature_name)
            if not candidate_signature:
                candidate_signature = tuple(sorted(self._profile_signature_types(profile)))
            actual_damage, actual_observations = self._profile_target_actual_damage(profile, creature_name, candidate_signature)

            candidates.append(
                WeaponRecommendation(
                    binding=profile.binding,
                    expected_damage=estimate.expected_damage,
                    selection_damage=self._selection_damage_score(estimate.expected_damage, actual_damage, actual_observations),
                    actual_damage=actual_damage,
                    actual_observations=actual_observations,
                    matched_name=estimate.matched_name,
                    paragon_ranks=estimate.paragon_ranks,
                    learned_types=tuple(sorted(self._profile_known_damage_types(profile))),
                    estimated_components=tuple(
                        sorted(
                            (
                                damage_type,
                                int(round(base_damage)),
                            )
                            for damage_type, base_damage in raw_components.items()
                        )
                    ),
                    healing_types=estimate.healing_types,
                    ignored_types=ignored_types,
                    special_name=self._special_weapon_name_for_profile(profile),
                    signature_observations=profile.stable_signature_observations,
                    estimate_observations=self._profile_estimate_observations(profile),
                )
            )
        return candidates

    def _choose_best_weapon(self, candidates: List[WeaponRecommendation]) -> Optional[WeaponRecommendation]:
        if not candidates:
            return None

        return max(
            candidates,
            key=lambda candidate: (
                candidate.selection_damage,
                candidate.actual_damage is not None,
                candidate.actual_observations,
                candidate.expected_damage,
                1 if candidate.binding.key == self.current_weapon_key else 0,
                candidate.signature_observations,
                len(candidate.estimated_components),
                candidate.estimate_observations,
                candidate.binding.key,
            ),
        )

    def _weapon_learning_status(self, target_name: str) -> str:
        ready_count = 0
        estimated_count = 0
        adaptive_count = 0
        for profile in self.weapon_profiles.values():
            if self._profile_learning_complete(profile):
                ready_count += 1
            if profile.type_estimates:
                estimated_count += 1
            if self._profile_is_p2(profile):
                adaptive_count += 1
        adaptive_suffix = f", {adaptive_count} adaptive" if adaptive_count else ""
        return (
            f"{target_name}: learning weapons "
            f"({ready_count}/{len(self.weapon_profiles)} ready, {estimated_count} with estimates{adaptive_suffix})"
        )

    def _weapon_runtime_summary(self, profile: WeaponLearningProfile) -> str:
        return self._weapon_profile_summary(profile)

    def _next_weapon_to_learn(self) -> Optional[WeaponLearningProfile]:
        if not self.weapon_profiles:
            return None

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if (
            current_profile is not None
            and not self._profile_learning_complete(current_profile)
            and current_profile.attack_attempts < self.WEAPON_LEARNING_ATTACKS_BEFORE_ROTATE
        ):
            return current_profile

        candidates = [
            profile
            for profile in self.weapon_profiles.values()
            if profile.binding.key != self.current_weapon_key and not self._profile_learning_complete(profile)
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda profile: (
                1 if profile.stable_signature else 0,
                1 if profile.type_estimates else 0,
                profile.observations,
                profile.attack_attempts,
                profile.last_seen_at,
                profile.binding.key,
            ),
        )

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
            self.pending_weapon_unarm = False
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

    def _request_unarmed_fallback(self, target_name: str, reason: str) -> bool:
        if self.pending_weapon_key and self.pending_weapon_unarm:
            self.set_status(f"{target_name}: awaiting {self._pending_weapon_display()} damage")
            self.host.notify_state_changed()
            return True

        if self.current_weapon_key == WEAPON_CURRENT_UNARMED:
            self.set_status(f"{target_name}: already unarmed")
            self.host.notify_state_changed()
            return True

        equipped_keys = self._query_equipped_binding_keys(force=True)
        source_key = ""
        if len(equipped_keys) == 1:
            source_key = equipped_keys[0]
            self._set_current_weapon_from_equipped_key(source_key, "equipped quickbar mask")
        elif self.current_weapon_key in self.weapon_bindings:
            source_key = self.current_weapon_key
        elif len(equipped_keys) > 1 and self.current_weapon_key in equipped_keys:
            source_key = self.current_weapon_key

        binding = self.weapon_bindings.get(source_key)
        if binding is None:
            self.set_status(f"{target_name}: no safe weapon; unarmed source unknown")
            self.host.notify_state_changed()
            return False

        try:
            result = self.host.trigger_slot(binding.slot, page=binding.page)
        except Exception as exc:
            self.set_status(f"{target_name}: unarm failed")
            self.host.emit(
                "error",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} trigger failed for "
                    f"{WEAPON_CURRENT_UNARMED} via {self._binding_display(binding.key)}: {exc}"
                ),
                script_id=self.script_id,
            )
            return False

        if result["success"]:
            now = time.monotonic()
            self.pending_weapon_key = binding.key
            self.pending_weapon_unarm = True
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
            self.set_status(f"{target_name}: {reason} {WEAPON_CURRENT_UNARMED} ({self._weapon_swap_cooldown_seconds():.1f}s)")
        else:
            self.set_status(f"{target_name}: unarm failed")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} target='{target_name}' "
                f"weapon={WEAPON_CURRENT_UNARMED} source={self._binding_display(binding.key)} reason={reason} "
                f"success={result['success']} rc={result['rc']} aux={result['aux_rc']} "
                f"path={result['path']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return bool(result["success"])

    def _weapon_recommendation_summary(self, recommendation: WeaponRecommendation) -> str:
        parts = []
        if recommendation.special_name:
            parts.append(recommendation.special_name)
        parts.append(f"Score {recommendation.selection_damage}")
        parts.append(f"Expected {recommendation.expected_damage}")
        if recommendation.actual_damage is not None and recommendation.actual_observations > 0:
            parts.append(f"Actual {recommendation.actual_damage} ({recommendation.actual_observations})")
        if recommendation.learned_types:
            parts.append("Types " + self._format_weapon_type_set(set(recommendation.learned_types)))
        if recommendation.estimated_components:
            parts.append(
                "Base "
                + "/".join(
                    f"{_format_damage_type_label(damage_type)}~{amount}"
                    for damage_type, amount in recommendation.estimated_components
                )
            )
        if recommendation.ignored_types:
            parts.append(
                "Ignore "
                + "/".join(_format_damage_type_label(damage_type) for damage_type in recommendation.ignored_types)
                + " rider"
            )
        return ", ".join(parts) if parts else "Unknown"

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
            "recommended_is_mammon_wrath": False,
            "special_target_rule": "",
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
        if self._is_mammons_tear_target(profile.matched_name):
            analysis["special_target_rule"] = "Mammon's Wrath is the only allowed weapon for this target."

        candidates = self._weapon_candidates_for_target(target_name)
        safe_candidates = [candidate for candidate in candidates if not candidate.healing_types]
        if self._is_mammons_tear_target(profile.matched_name):
            mammon_candidate = self._mammon_wrath_candidate(safe_candidates)
            recommendation = mammon_candidate
        else:
            recommendation = self._choose_best_weapon(safe_candidates) if safe_candidates else None
            if not safe_candidates:
                analysis["special_target_rule"] = "No configured weapon is safe here; Auto Damage will prefer an unarmed fallback."
        if recommendation is not None:
            analysis["recommended_weapon"] = recommendation.binding.key
            analysis["recommended_is_mammon_wrath"] = recommendation.special_name == "Mammon's Wrath"

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
                "selection_damage": None,
                "expected_damage": None,
                "actual_damage": None,
                "actual_observations": 0,
                "matched_name": "",
                "paragon_ranks": 0,
                "healing_types": [],
                "ignored_types": [],
                "special_name": self._special_weapon_name_for_profile(weapon_profile),
            }
            if estimate is not None:
                weapon.update({
                    "selection_damage": estimate.selection_damage,
                    "expected_damage": estimate.expected_damage,
                    "actual_damage": estimate.actual_damage,
                    "actual_observations": estimate.actual_observations,
                    "matched_name": estimate.matched_name,
                    "paragon_ranks": estimate.paragon_ranks,
                    "healing_types": [
                        _format_damage_type_label(damage_type)
                        for damage_type in estimate.healing_types
                    ],
                    "ignored_types": [
                        _format_damage_type_label(damage_type)
                        for damage_type in estimate.ignored_types
                    ],
                    "special_name": estimate.special_name,
                })
            weapons.append(weapon)

        analysis["weapons"] = weapons
        return analysis

    def _handle_weapon_attack(self, attack):
        self.current_target = attack.defender

        now = time.monotonic()
        if self.pending_weapon_key and now < self.pending_weapon_ready_at:
            remaining = self.pending_weapon_ready_at - now
            self.set_status(f"{attack.defender}: waiting {self._pending_weapon_display()} {remaining:.1f}s")
            return

        if self.pending_weapon_key:
            self.set_status(f"{attack.defender}: awaiting {self._pending_weapon_display()} damage")
            return

        self._reconcile_current_weapon_from_equipped_mask()

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if current_profile is not None and not self._profile_learning_complete(current_profile):
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
        protected_target = self._is_mammons_tear_target(attack.defender)
        if protected_target:
            recommendation = self._mammon_wrath_candidate(safe_candidates)
            if recommendation is None:
                self.set_status(f"{attack.defender}: Mammon's Wrath required")
                return
        else:
            recommendation = None

        if not safe_candidates:
            unsafe_candidate = self._choose_best_weapon(candidates)
            healing_text = ", ".join(_format_damage_type_label(value) for value in unsafe_candidate.healing_types) if unsafe_candidate else "unknown"
            if self._request_unarmed_fallback(attack.defender, f"unarm unsafe ({healing_text})"):
                return
            self.set_status(f"{attack.defender}: unsafe ({healing_text})")
            return

        if recommendation is None:
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
                f"score={recommendation.selection_damage} expected={recommendation.expected_damage} "
                f"actual={recommendation.actual_damage if recommendation.actual_damage is not None else 'n/a'} "
                f"obs={recommendation.actual_observations} paragon={recommendation.paragon_ranks} "
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
        mammon_wrath_key = self._mammon_wrath_profile_key()
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
                "stable": bool(profile.stable_signature) or self._profile_is_p2(profile),
                "p2": self._profile_is_p2(profile),
                "mammon_wrath": binding.key == mammon_wrath_key,
                "mismatch_streak": profile.mismatch_streak,
                "rediscoveries": profile.rediscoveries,
                "summary": self._weapon_runtime_summary(profile),
            })
        return {
            "weapon_mode": True,
            "current_weapon": self.current_weapon_key,
            "current_display": self._binding_display(self.current_weapon_key),
            "pending_weapon": self.pending_weapon_key,
            "pending_display": self._pending_weapon_display(),
            "mammon_wrath_key": mammon_wrath_key,
            "current_is_mammon_wrath": bool(mammon_wrath_key and self.current_weapon_key == mammon_wrath_key),
            "pending_is_mammon_wrath": bool(mammon_wrath_key and self.pending_weapon_key == mammon_wrath_key),
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


class AutoFollowScript(ClientScriptBase):
    script_id = "auto_follow"
    FOLLOW_CUES = ("fall in", "follow me", "follow my")
    ASO_COMMAND = "!action aso target"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.cooldown_until = 0.0
        self.follow_count = 0
        self.last_speaker = ""
        self.last_message = ""
        self.last_error_key = ""

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.cooldown_until = 0.0
        self.follow_count = 0
        self.last_speaker = ""
        self.last_message = ""
        self.last_error_key = ""
        self.set_status("Listening for follow cues")
        self.host.emit("info", f"{self.host.client.display_name}: Auto Follow started", script_id=self.script_id)

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.host.emit("info", f"{self.host.client.display_name}: Auto Follow stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return

        parsed = self._parse_follow_cue(text)
        if parsed is None:
            return

        speaker, message = parsed
        if self._speaker_matches_character(speaker):
            self.set_status(f"Ignored own cue ({speaker})")
            return

        now = time.monotonic()
        if now < self.cooldown_until:
            self.set_status(f"{speaker}: cooldown")
            return

        tell_command = f'/tell "{speaker}" !target'
        try:
            aso_result = self.host.send_chat(self.ASO_COMMAND, 2)
            target_result = self.host.send_chat(tell_command, 2)
        except Exception as exc:
            self.cooldown_until = now + 1.0
            self.set_status(f"{speaker}: follow failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: Auto Follow chat send failed for '{speaker}': {exc}",
                script_id=self.script_id,
            )
            return

        self.cooldown_until = now + self._cooldown_seconds()
        self.follow_count += 1
        self.last_speaker = speaker
        self.last_message = message
        success = bool(aso_result["success"] and target_result["success"])
        if success:
            self.set_status(f"{speaker}: followed")
            if self.last_error_key:
                self.host.emit("info", f"{self.host.client.display_name}: Auto Follow recovered", script_id=self.script_id)
                self.last_error_key = ""
        else:
            self.set_status(f"{speaker}: follow failed")
            self.last_error_key = f"{aso_result['rc']}:{aso_result['err']}:{target_result['rc']}:{target_result['err']}"

        self.host.emit(
            "info" if success else "error",
            (
                f"{self.host.client.display_name}: Auto Follow cue from '{speaker}' message='{message}' "
                f"aso success={aso_result['success']} rc={aso_result['rc']} err={aso_result['err']} "
                f"target success={target_result['success']} rc={target_result['rc']} err={target_result['err']}"
            ),
            script_id=self.script_id,
        )
        if success and bool(self.config.get("echo_console", False)):
            self.host.send_console(f"SimKeys Auto Follow -> {speaker}")
        self.host.notify_state_changed()

    def _parse_follow_cue(self, text: str) -> Optional[Tuple[str, str]]:
        normalized = hgx_combat.normalize_chat_line(text)
        if ":" not in normalized:
            return None

        speaker_text, message_text = normalized.split(":", 1)
        speaker = hgx_combat.normalize_actor_name(speaker_text)
        message = hgx_combat.normalize_actor_name(message_text)
        if not speaker or not message:
            return None

        lowered_message = message.lower()
        if not any(cue in lowered_message for cue in self.FOLLOW_CUES):
            return None
        return speaker, message

    def _speaker_matches_character(self, speaker: str) -> bool:
        character_name = (self.host.client.character_name or self.client.character_name or "").strip()
        if not character_name:
            return False

        speaker_key = self._strip_level_suffix(hgx_combat.normalize_actor_name(speaker).lower())
        character_key = self._strip_level_suffix(hgx_combat.normalize_actor_name(character_name).lower())
        return bool(speaker_key and character_key and speaker_key == character_key)

    def _strip_level_suffix(self, value: str) -> str:
        text = str(value or "").strip()
        if " [" in text and text.endswith("]"):
            return text.split(" [", 1)[0].strip()
        return text

    def _cooldown_seconds(self) -> float:
        return max(float(self.config.get("cooldown_seconds", 1.0)), 0.1)

    def get_state_details(self) -> dict:
        return {
            "follow_count": self.follow_count,
            "last_speaker": self.last_speaker,
            "last_message": self.last_message,
        }


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
        last_backlog_log_at = 0.0
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

                polled_latest = int(polled.get("latest_seq", after) or 0)
                polled_lines = list(polled.get("lines") or [])
                self.latest_sequence = polled_latest
                if not initialized:
                    initialized = True
                    after = polled_latest
                    self.emit("info", f"{self.client.display_name}: host connected at seq {after}")
                    self.stop_event.wait(poll_interval)
                    continue

                if polled_lines:
                    batch_started_at = time.perf_counter()
                    returned_latest = after
                    for line in polled_lines:
                        line_sequence = int(line["seq"])
                        if line_sequence > returned_latest:
                            returned_latest = line_sequence
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
                                            f"chat handler took {line_elapsed:.2f}s at seq {line_sequence}"
                                        ),
                                        script_id=getattr(script, "script_id", None),
                                    )
                    after = returned_latest
                    batch_elapsed = time.perf_counter() - batch_started_at
                    if batch_elapsed > 1.0 and time.perf_counter() - last_slow_log_at > 10.0:
                        last_slow_log_at = time.perf_counter()
                        self.emit(
                            "error",
                            f"{self.client.display_name}: slow chat batch processed {len(polled_lines)} lines in {batch_elapsed:.2f}s",
                        )
                elif polled_latest < after:
                    after = polled_latest

                backlog_remaining = polled_latest > after
                if backlog_remaining:
                    now_perf = time.perf_counter()
                    if now_perf - last_backlog_log_at > 10.0:
                        last_backlog_log_at = now_perf
                        self.emit(
                            "info",
                            (
                                f"{self.client.display_name}: draining chat backlog "
                                f"(processed up to seq {after}, queue at {polled_latest})"
                            ),
                        )
                    continue

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
            description="Poll HP from memory and drink from the selected quickbar slot when health falls below the configured threshold.",
            fields=[
                ScriptField("slot", "Slot", "int", 2, minimum=1, maximum=12, step=1, width=4),
                ScriptField("page", "Bank", "choice", "None", choices=["None", "Shift", "Control"], width=8),
                ScriptField("threshold_percent", "HP %", "float", 80.0, minimum=1.0, maximum=100.0, step=1.0, width=6),
                ScriptField("cooldown_seconds", "Cooldown", "float", 3.0, minimum=0.1, maximum=10.0, step=0.1, width=6),
                ScriptField("lock_target", "Lock", "bool", True),
                ScriptField("resume_attack", "Resume", "bool", True),
                ScriptField("poll_interval", "Poll", "float", 1.0, minimum=0.10, maximum=5.0, step=0.10, width=6),
                ScriptField("echo_console", "Echo", "bool", True),
            ],
            factory=AutoDrinkScript,
        )
        self.registry[autodrink.script_id] = autodrink

        stop_hitting = ScriptDefinition(
            script_id="stop_hitting",
            name="Stop Hitting",
            description=(
                "Watch your outgoing damage and drink a healing potion if you hit a characters.d target marked "
                'kickback="Area", interrupting further attacks without resuming them.'
            ),
            fields=[
                ScriptField("slot", "Slot", "int", 2, minimum=1, maximum=12, step=1, width=4),
                ScriptField("page", "Bank", "choice", "None", choices=["None", "Shift", "Control"], width=8),
                ScriptField("cooldown_seconds", "Drink Time", "float", 3.0, minimum=0.1, maximum=10.0, step=0.1, width=6),
                ScriptField("poll_interval", "Poll", "float", 0.10, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 60, minimum=1, maximum=200, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", True),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=StopHittingScript,
        )
        self.registry[stop_hitting.script_id] = stop_hitting

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

        auto_follow = ScriptDefinition(
            script_id="auto_follow",
            name="Auto Follow",
            description=(
                "Listen for follow voice cues such as 'fall in' or 'follow me', then target the speaker "
                "using the old HGXLE autoFollow.py command sequence."
            ),
            fields=[
                ScriptField("cooldown_seconds", "Cooldown", "float", 1.0, minimum=0.1, maximum=30.0, step=0.1, width=6),
                ScriptField("poll_interval", "Poll", "float", 0.05, minimum=0.01, maximum=2.0, step=0.01, width=6),
                ScriptField("max_lines", "Batch", "int", 200, minimum=1, maximum=500, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", False),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AutoFollowScript,
        )
        self.registry[auto_follow.script_id] = auto_follow

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
