import os
import threading
import time
import ctypes as C
import ctypes.wintypes as W
import re
from xml.etree import ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Pattern, Set, Tuple

from . import simKeys_Client as simkeys
from . import simkeys_damage_meter as damage_meter
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
    "mammons tears",
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


def _build_base_quickbar_slot_choices() -> List[str]:
    values = [WEAPON_SLOT_NONE]
    values.extend(f"F{slot}" for slot in range(1, 13))
    return values


def _normalize_creature_name_key(text: str) -> str:
    cleaned = str(text or "").strip().lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


WEAPON_SLOT_CHOICES = _build_quickbar_slot_choices()
WEAPON_BASE_SLOT_CHOICES = _build_base_quickbar_slot_choices()
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


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _default_status_rules_dir() -> str:
    workspace_rules = os.path.join(_repo_root(), "data", "statusrules.d")
    if os.path.isdir(workspace_rules):
        return workspace_rules

    hgx_rules = r"C:\NWN\HGXLE-final-beta\data\statusrules.d"
    if os.path.isdir(hgx_rules):
        return hgx_rules
    return workspace_rules


def _default_follow_cues_dir() -> str:
    return os.path.join(_repo_root(), "data", "followcues.d")


def _parse_duration_seconds(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if ":" not in text:
        try:
            return max(float(text), 0.0)
        except ValueError:
            return 0.0

    parts = [part.strip() for part in text.split(":")]
    try:
        numbers = [int(part or "0") for part in parts]
    except ValueError:
        return 0.0
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours = 0
        minutes, seconds = numbers
    else:
        return 0.0
    return float(max((hours * 3600) + (minutes * 60) + seconds, 0))


def _parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _timer_color_rgb(value, default: int = 0xFFFFFF) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFF
    text = str(value or "").strip()
    if not text:
        return default
    named = {
        "white": 0xFFFFFF,
        "green": 0x66FF66,
        "yellow": 0xFFFF66,
        "red": 0xFF6666,
        "cyan": 0x66FFFF,
        "blue": 0x6699FF,
        "orange": 0xFFAA55,
    }
    key = text.lower()
    if key in named:
        return named[key]
    if key.startswith("#"):
        key = key[1:]
    if key.lower().startswith("0x"):
        key = key[2:]
    try:
        return int(key, 16) & 0xFFFFFF
    except ValueError:
        return default


OVERLAY_LINE_COLOR_MARKER = "\x1f"
OVERLAY_CONTROL_MARKER = "\x1d"
OVERLAY_TOGGLE_EVENT_PREFIX = "\x1eSIMKEYS_OVERLAY_TOGGLE:"
OVERLAY_CONTROLS_ID = 7099
OVERLAY_SCRIPT_CONTROLS: Tuple[Tuple[str, str], ...] = (
    ("autodrink", "Dr"),
    ("stop_hitting", "St"),
    ("auto_aa", "Dg"),
    ("auto_action", "Ac"),
    ("auto_attack", "At"),
    ("always_on", "On"),
    ("auto_rsm", "Md"),
    ("ingame_timers", "Tm"),
)


def _overlay_line_color_prefix(color_rgb: int) -> str:
    return f"{OVERLAY_LINE_COLOR_MARKER}{int(color_rgb) & 0xFFFFFF:06X};"


def _overlay_controls_line(running_script_ids: Set[str]) -> str:
    parts = []
    for script_id, label in OVERLAY_SCRIPT_CONTROLS:
        state = "1" if script_id in running_script_ids else "0"
        parts.append(f"{script_id}|{label}|{state}")
    return f"{OVERLAY_CONTROL_MARKER}controls;{';'.join(parts)}"


def _build_var_timer_regex(rule: str) -> Pattern:
    counter = {"minutes": 0, "seconds": 0}

    def replace(match):
        token = match.group(1).lower()
        counter[token] += 1
        name = "MINUTES" if token == "minutes" else "SECONDS"
        return rf"(?P<{name}_{counter[token]}>\d+)"

    pattern = re.sub(r"\{(MINUTES|SECONDS)\}", replace, str(rule or ""), flags=re.IGNORECASE)
    return re.compile(pattern)


SPELL_CAST_LINE_RE = re.compile(r"^(?P<caster>.+?) casts (?P<spell>.+?)\s*$", re.IGNORECASE)
EFFECT_TIMER_LINE_RE = re.compile(r"^\s*#\d+\s+(?P<effect>.+?)\s+\[(?P<remaining>[^\]]+)\]\s*$", re.IGNORECASE | re.MULTILINE)
AVERTED_DEATH_LINE_RE = re.compile(
    r"^(?P<player>.+?)\s+(?P<action>respawn|averts death)\s+:\s+(?P<method>.+?)\s+:\s+\*success\*\s*$",
    re.IGNORECASE,
)
SHIFTER_SHIFT_LINE_RE = re.compile(
    r"^(?P<actor>.+?)\s+(?:shapeshifts|shifts into .+? form)\.\s*$",
    re.IGNORECASE,
)
SHIFTER_ESSENCE_LINE_RE = re.compile(
    r"^You have (?P<current>\d+)\s*/\s*(?P<maximum>\d+) essence points remaining\.\s*$",
    re.IGNORECASE,
)
PLAYER_HIDE_LINE_RE = re.compile(r"^Acquired Item:\s*Player Hide\s*$", re.IGNORECASE)
SPELL_EFFECT_ALIASES = {
    "storm of vengeance": "Divine Power",
}


def _looks_like_duration(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if ":" in text:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return True
    return bool(re.search(r"\d+\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b", text))


def _parse_effect_remaining_seconds(value: object) -> float:
    text = str(value or "").strip().lower()
    if not text:
        return 0.0
    text = text.replace(",", " ")
    text = re.sub(r"\b(left|remaining)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    total = 0
    matched = False
    for match in re.finditer(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?=\b|\d|$)", text):
        matched = True
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("h"):
            total += amount * 3600
        elif unit.startswith("m"):
            total += amount * 60
        else:
            total += amount
    if matched:
        return float(total)
    return _parse_duration_seconds(text)


def _timer_literal_from_regex(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\\(.)", r"\1", value)
    return value.strip()


def _extract_cast_spell_from_timer_rule(rule: str) -> str:
    text = str(rule or "").strip()
    if not text.startswith("^") or not text.endswith("$") or " casts " not in text:
        return ""
    body = text[1:-1]
    _caster, spell = body.split(" casts ", 1)
    spell = _timer_literal_from_regex(spell)
    if not spell or re.search(r"[\^\$\|\(\)\[\]\{\}\*\+\?]", spell):
        return ""
    return spell


def _spell_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


@dataclass(frozen=True)
class OverlayTimerRule:
    key: str
    text: str
    description: str
    kind: str
    pattern: Pattern
    duration_seconds: float
    disable_on_death: bool
    disable_on_rest: bool
    color_rgb: int
    source: str


@dataclass(frozen=True)
class SpellTimerSpec:
    key: str
    spell: str
    effect: str
    label: str
    duration_seconds: float
    color_rgb: int
    source: str


@dataclass
class PendingSpellEffectQuery:
    spec: SpellTimerSpec
    cast_at: float
    next_request_at: float
    deadline_at: float
    attempts: int = 0


@dataclass
class ActiveOverlayTimer:
    label: str
    description: str
    expires_at: float
    duration_seconds: float
    color_rgb: int
    disable_on_death: bool
    disable_on_rest: bool
    source: str
    state: str = ""


@dataclass(frozen=True)
class ChatLineEvent:
    sequence: int
    raw_text: str
    normalized: str
    kinds: Tuple[str, ...]
    attack: object = None
    damage: object = None
    weapon_feedback: str = ""
    aa_feedback_type: Optional[int] = None
    gi_feedback_type: Optional[int] = None
    breach: object = None
    target_blind: bool = False
    spell_cast: object = None
    effect_timer: object = None
    shifter_shift_actor: str = ""
    shifter_essence_current: int = 0
    shifter_essence_maximum: int = 0
    player_hide: bool = False
    averted_death_player: str = ""
    kill_killer: str = ""
    kill_victim: str = ""
    overlay_script_id: str = ""
    password_prompt: bool = False

    def has_kind(self, kind: str) -> bool:
        return str(kind or "") in self.kinds


def parse_chat_line_event(sequence: int, text: str, password_prompt_text: str = "") -> ChatLineEvent:
    raw_text = str(text or "")
    normalized = hgx_combat.normalize_chat_line(raw_text)
    kinds: Set[str] = set()

    attack = None
    damage = None
    weapon_feedback = ""
    aa_feedback_type = None
    gi_feedback_type = None
    breach = None
    target_blind = False
    spell_cast = None
    effect_timer = None
    shifter_shift_actor = ""
    shifter_essence_current = 0
    shifter_essence_maximum = 0
    player_hide = False
    averted_death_player = ""
    kill_killer = ""
    kill_victim = ""
    overlay_script_id = ""
    password_prompt = False

    lowered = normalized.lower()
    if raw_text.startswith(OVERLAY_TOGGLE_EVENT_PREFIX):
        overlay_script_id = raw_text[len(OVERLAY_TOGGLE_EVENT_PREFIX):].strip()
        kinds.add("overlay")

    if password_prompt_text and lowered == password_prompt_text.strip().lower():
        password_prompt = True
        kinds.add("password_prompt")

    if " attacks " in lowered:
        attack = hgx_combat.parse_attack_line(raw_text)
        if attack is not None:
            kinds.add("attack")

    if " damages " in lowered:
        kinds.add("damage_candidate")
        if "(" in normalized and ")" in normalized:
            damage = hgx_combat.parse_damage_line(raw_text)
            if damage is not None:
                kinds.add("damage")

    if "equipped" in lowered or "weapon" in lowered:
        weapon_feedback = hgx_combat.parse_weapon_swap_feedback(raw_text)
        if weapon_feedback:
            kinds.add("weapon_feedback")

    if "bow set to" in lowered or "divine bullets set to" in lowered:
        aa_feedback_type = hgx_combat.parse_damage_feedback_type(raw_text)
        if aa_feedback_type is not None:
            kinds.add("aa_feedback")

    if "you are now using" in lowered:
        gi_feedback_type = hgx_combat.parse_gi_feedback_type(raw_text)
        if gi_feedback_type is not None:
            kinds.add("gi_feedback")

    if "breach" in lowered:
        breach = hgx_combat.parse_breach_line(raw_text)
        if breach is not None:
            kinds.add("breach")

    if "(target blind)" in lowered:
        target_blind = hgx_combat.has_target_blind_marker(raw_text)
        if target_blind:
            kinds.add("target_blind")

    spell_cast = SPELL_CAST_LINE_RE.match(normalized)
    if spell_cast is not None:
        kinds.add("spell_cast")

    effect_timer = EFFECT_TIMER_LINE_RE.match(normalized)
    if effect_timer is not None:
        kinds.add("effect_timer")

    if PLAYER_HIDE_LINE_RE.match(normalized):
        player_hide = True
        kinds.add("player_hide")
        kinds.add("shifter_state")

    shifter_essence = SHIFTER_ESSENCE_LINE_RE.match(normalized)
    if shifter_essence is not None:
        shifter_essence_current = int(shifter_essence.group("current") or "0")
        shifter_essence_maximum = int(shifter_essence.group("maximum") or "0")
        kinds.add("shifter_state")

    shifter_shift = SHIFTER_SHIFT_LINE_RE.match(normalized)
    if shifter_shift is not None:
        shifter_shift_actor = hgx_combat.normalize_actor_name(shifter_shift.group("actor"))
        kinds.add("shifter_state")

    averted = AVERTED_DEATH_LINE_RE.match(normalized)
    if averted is not None:
        averted_death_player = hgx_combat.normalize_actor_name(averted.group("player"))
        kinds.add("averted_death")
        kinds.add("death")

    marker = " killed "
    marker_at = lowered.find(marker)
    if marker_at > 0 and not normalized.startswith("You have the following accomplishments"):
        kill_killer = hgx_combat.normalize_actor_name(normalized[:marker_at])
        kill_victim = hgx_combat.normalize_actor_name(normalized[marker_at + len(marker):])
        if kill_killer and kill_victim:
            kinds.add("kill")
            kinds.add("death")

    if ":" in normalized:
        kinds.add("speech")

    return ChatLineEvent(
        sequence=int(sequence),
        raw_text=raw_text,
        normalized=normalized,
        kinds=tuple(sorted(kinds)),
        attack=attack,
        damage=damage,
        weapon_feedback=weapon_feedback,
        aa_feedback_type=aa_feedback_type,
        gi_feedback_type=gi_feedback_type,
        breach=breach,
        target_blind=target_blind,
        spell_cast=spell_cast,
        effect_timer=effect_timer,
        shifter_shift_actor=shifter_shift_actor,
        shifter_essence_current=shifter_essence_current,
        shifter_essence_maximum=shifter_essence_maximum,
        player_hide=player_hide,
        averted_death_player=averted_death_player,
        kill_killer=kill_killer,
        kill_victim=kill_victim,
        overlay_script_id=overlay_script_id,
        password_prompt=password_prompt,
    )


def _load_hgx_spell_timer_specs(source_dir: str) -> Tuple[SpellTimerSpec, ...]:
    by_spell: Dict[str, SpellTimerSpec] = {}
    directory = os.path.abspath(os.path.expanduser(os.path.expandvars(str(source_dir or ""))))
    if not os.path.isdir(directory):
        return ()

    for file_name in sorted(os.listdir(directory)):
        if not file_name.lower().endswith(".xml"):
            continue
        path = os.path.join(directory, file_name)
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue

        for index, element in enumerate(list(root)):
            tag = str(element.tag or "").strip().lower()
            spell = ""
            duration = _parse_duration_seconds(element.get("duration"))
            color_rgb = _timer_color_rgb(element.get("color"))

            if tag == "spelltimer":
                spell = str(element.get("spell") or element.get("text") or "").strip()
            elif tag == "timer":
                spell = _extract_cast_spell_from_timer_rule(str(element.get("rule") or ""))
            if not spell:
                continue
            effect = str(element.get("effect") or "").strip()
            if not effect:
                effect = SPELL_EFFECT_ALIASES.get(_spell_key(spell), spell)

            key = _spell_key(spell)
            current = by_spell.get(key)
            if current is not None and current.duration_seconds >= duration:
                continue

            by_spell[key] = SpellTimerSpec(
                key=key,
                spell=spell,
                effect=effect,
                label=spell,
                duration_seconds=duration,
                color_rgb=color_rgb,
                source=f"{file_name}:{index}",
            )

    return tuple(sorted(by_spell.values(), key=lambda spec: spec.spell.lower()))


def _format_spell_timer_config(specs: Tuple[SpellTimerSpec, ...]) -> str:
    parts = []
    for spec in specs:
        parts.append(f"{spec.spell}={spec.effect or spec.spell}")
    return "; ".join(parts)


def _parse_spell_timer_config(value: object, defaults: Tuple[SpellTimerSpec, ...]) -> Tuple[SpellTimerSpec, ...]:
    default_by_key = {spec.key: spec for spec in defaults}
    text = str(value or "").strip()
    if not text:
        text = _format_spell_timer_config(defaults)

    specs: List[SpellTimerSpec] = []
    seen: Set[str] = set()
    for raw_entry in re.split(r"[;\r\n]+", text):
        entry = raw_entry.strip()
        if not entry:
            continue

        label = ""
        effect = ""
        duration_text = ""
        if "|" in entry:
            parts = [part.strip() for part in entry.split("|")]
            if len(parts) >= 4:
                label, spell, effect, duration_text = parts[0], parts[1], parts[2], parts[3]
            elif len(parts) == 3:
                if _looks_like_duration(parts[2]):
                    spell, effect, duration_text = parts[0], parts[1], parts[2]
                else:
                    label, spell, effect = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                spell = parts[0]
                if _looks_like_duration(parts[1]):
                    duration_text = parts[1]
                else:
                    effect = parts[1]
            else:
                spell = parts[0]
        elif "=" in entry:
            spell, right = [part.strip() for part in entry.split("=", 1)]
            if "|" in right:
                right_parts = [part.strip() for part in right.split("|") if part.strip()]
                if right_parts and _looks_like_duration(right_parts[-1]):
                    duration_text = right_parts[-1]
                    effect = "|".join(right_parts[:-1]).strip()
                else:
                    effect = right
            elif _looks_like_duration(right):
                duration_text = right
            else:
                effect = right
        else:
            spell = entry

        key = _spell_key(spell)
        if not key or key in seen:
            continue

        default = default_by_key.get(key)
        has_duration = bool(str(duration_text or "").strip())
        duration = _parse_duration_seconds(duration_text)
        if not has_duration and default is not None:
            duration = default.duration_seconds
        if not effect:
            effect = default.effect if default is not None else SPELL_EFFECT_ALIASES.get(key, spell)
        specs.append(SpellTimerSpec(
            key=key,
            spell=spell,
            effect=effect,
            label=label or spell,
            duration_seconds=duration,
            color_rgb=default.color_rgb if default is not None else 0xFFFFFF,
            source="config",
        ))
        seen.add(key)
    return tuple(specs)


def _load_status_timer_rules(source_dir: str) -> Tuple[OverlayTimerRule, ...]:
    rules: List[OverlayTimerRule] = []
    directory = os.path.abspath(os.path.expanduser(os.path.expandvars(str(source_dir or ""))))
    if not os.path.isdir(directory):
        return ()

    for file_name in sorted(os.listdir(directory)):
        if not file_name.lower().endswith(".xml"):
            continue
        path = os.path.join(directory, file_name)
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue

        for index, element in enumerate(list(root)):
            tag = str(element.tag or "").strip().lower()
            text = str(element.get("text") or element.get("description") or "").strip()
            description = str(element.get("description") or text).strip()
            duration = _parse_duration_seconds(element.get("duration"))
            disable_on_death = _parse_bool(element.get("disableOnDeath"))
            disable_on_rest = _parse_bool(element.get("disableOnRest"))
            color_rgb = _timer_color_rgb(element.get("color"))
            source = file_name
            key = f"{file_name}:{index}:{tag}:{text}"

            if not text:
                continue
            if tag == "timer":
                raw_rule = str(element.get("rule") or "").strip()
                if not raw_rule:
                    continue
                if _extract_cast_spell_from_timer_rule(raw_rule):
                    continue
                try:
                    pattern = re.compile(raw_rule)
                except re.error:
                    continue
            elif tag == "vartimer":
                raw_rule = str(element.get("rule") or "").strip()
                if not raw_rule:
                    continue
                try:
                    pattern = _build_var_timer_regex(raw_rule)
                except re.error:
                    continue
            elif tag == "spelltimer":
                continue
            else:
                continue

            rules.append(OverlayTimerRule(
                key=key,
                text=text,
                description=description,
                kind=tag,
                pattern=pattern,
                duration_seconds=duration,
                disable_on_death=disable_on_death,
                disable_on_rest=disable_on_rest,
                color_rgb=color_rgb,
                source=source,
            ))
    return tuple(rules)


def _duration_from_var_match(match) -> float:
    if match is None:
        return 0.0
    minutes = 0
    seconds = 0
    for name, value in match.groupdict().items():
        if value is None:
            continue
        if name.startswith("MINUTES"):
            minutes = max(minutes, int(value))
        elif name.startswith("SECONDS"):
            seconds = max(seconds, int(value))
    return float((minutes * 60) + seconds)


def _load_follow_cues(source_dir: str) -> Tuple[str, ...]:
    cues: List[str] = []
    seen: Set[str] = set()
    directory = os.path.abspath(os.path.expanduser(os.path.expandvars(str(source_dir or ""))))
    if not os.path.isdir(directory):
        return ()

    for file_name in sorted(os.listdir(directory)):
        if not file_name.lower().endswith(".xml"):
            continue
        path = os.path.join(directory, file_name)
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue

        for element in root.iter():
            tag = str(element.tag or "").strip().lower()
            if tag not in ("cue", "followcue", "follow-cue"):
                continue
            text = str(element.get("text") or element.get("phrase") or element.text or "").strip()
            text = re.sub(r"\s+", " ", text).strip()
            key = text.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            cues.append(text)
    return tuple(cues)


def _format_remaining(seconds: float) -> str:
    remaining = max(int(seconds + 0.999), 0)
    hours = remaining // 3600
    minutes = (remaining % 3600) // 60
    secs = remaining % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


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
    normal_average_total: float = 0.0
    normal_observations: int = 0
    critical_average_total: float = 0.0
    critical_observations: int = 0
    normal_recent_totals: List[int] = field(default_factory=list)
    critical_recent_totals: List[int] = field(default_factory=list)


@dataclass(frozen=True)
class PendingWeaponAttackResult:
    sequence: int
    defender: str
    is_critical: bool


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

    def on_tick(self):
        pass

    def on_chat_line(self, sequence: int, text: str):
        raise NotImplementedError

    def on_chat_event(self, event: ChatLineEvent):
        self.on_chat_line(event.sequence, event.raw_text)

    def needs_chat_feed(self) -> bool:
        return True

    def chat_event_types(self) -> Tuple[str, ...]:
        return ("raw",)

    def wants_chat_event(self, event: ChatLineEvent) -> bool:
        event_types = tuple(self.chat_event_types() or ())
        if "raw" in event_types:
            return True
        return bool(set(event_types).intersection(event.kinds))

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
        self.lock_saved_for_recovery = False
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
        self.lock_saved_for_recovery = False
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
        self.lock_saved_for_recovery = False
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
            self.lock_saved_for_recovery = False
            return

        slot = int(self.config.get("slot", 2))
        page = _parse_quickbar_bank_page(self.config.get("page", 0))
        trigger_name = self.host.format_slot(page, slot)
        if bool(self.config.get("lock_target", True)) and not self.lock_saved_for_recovery:
            self.lock_saved_for_recovery = True
            try:
                self.host.send_chat("!lock opponent", 2)
            except Exception as exc:
                self.lock_saved_for_recovery = False
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

    def chat_event_types(self) -> Tuple[str, ...]:
        return ("damage",)

    def on_chat_event(self, event: ChatLineEvent):
        if not self.should_process(event.sequence) or not self.enabled:
            return
        damage_line = event.damage
        if damage_line is None:
            return
        self._handle_damage_line(damage_line)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return
        if " damages " not in str(text or "").lower():
            return

        damage_line = hgx_combat.parse_damage_line(text)
        if damage_line is None:
            return
        self._handle_damage_line(damage_line)

    def _handle_damage_line(self, damage_line):
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
        target_key = _normalize_creature_name_key(target_name)
        if target_key not in MAMMONS_TEAR_TARGETS:
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
        matched_name = _normalize_creature_name_key(analysis.get("matched_name"))
        target = _normalize_creature_name_key(analysis.get("target"))
        if target_key in {matched_name, target} and bool(analysis.get("recommended_is_mammon_wrath")):
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
    MODE_SHIFTER_WEAPON_SWAP = "Shifter Weapon Swap"
    MAX_WEAPON_BINDINGS = len(WEAPON_BINDING_KEYS)
    WEAPON_SIGNATURE_CONFIRM_THRESHOLD = 2
    WEAPON_LEARNING_ATTACKS_BEFORE_ROTATE = 3
    WEAPON_REDISCOVERY_MISMATCH_THRESHOLD = 8
    WEAPON_ACTUAL_DAMAGE_WINDOW = 9
    WEAPON_PENDING_MAX_RETRIES = 2
    WEAPON_EQUIPPED_PROBE_INTERVAL_SECONDS = 0.50
    SHIFTER_UNSHIFT_WAIT_SECONDS = 1.50
    SHIFTER_WEAPON_CONFIRM_RETRY_SECONDS = 2.00
    SHIFTER_SHIFT_FIRST_RETRY_SECONDS = 2.00
    SHIFTER_SHIFT_RETRY_SECONDS = 1.00
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
        self.pending_weapon_retry_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_external_unknown = False
        self.weapon_external_unknown_feedback = ""
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
        self.weapon_pending_attack_results: List[PendingWeaponAttackResult] = []
        self.shifter_shift_choice = WEAPON_SLOT_NONE
        self.shifter_shift_page = 0
        self.shifter_shift_slot = 0
        self.shifter_shift_state = "unknown"
        self.shifter_swap_stage = ""
        self.shifter_sequence_started_at = 0.0
        self.shifter_pending_target = ""
        self.shifter_pending_reason = ""
        self.shifter_pending_unarm = False
        self.shifter_pending_source_key = ""
        self.shifter_unshift_deadline_at = 0.0
        self.shifter_next_weapon_retry_at = 0.0
        self.shifter_next_shift_attempt_at = 0.0
        self.shifter_shift_attempts = 0
        self.shifter_last_shift_line = ""
        self.shifter_last_essence_line = ""
        self.shifter_last_player_hide_at = 0.0
        self.shifter_last_shift_at = 0.0
        self.shifter_resume_pending = False
        self.shifter_lock_sent = False
        self.shifter_last_error = ""
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
        self.pending_weapon_retry_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_external_unknown = False
        self.weapon_external_unknown_feedback = ""
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
        self.weapon_pending_attack_results = []
        self._reset_shifter_runtime(clear_observed=True)

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
        self.pending_weapon_retry_count = 0
        self.weapon_last_swap_feedback = ""
        self.weapon_external_unknown = False
        self.weapon_external_unknown_feedback = ""
        self.weapon_last_equipped_mask = 0
        self.weapon_equipped_key = ""
        self.weapon_equipped_keys = ()
        self.weapon_equipped_probe_at = 0.0
        self.weapon_equipped_probe_error = ""
        self.weapon_equipped_probe_error_logged = False
        self.weapon_unarmed_observations = 0
        self.weapon_pending_attack_results = []
        self._reset_shifter_runtime(clear_observed=True)
        self.canister_stop.set()
        self.host.emit("info", f"{self.host.client.display_name}: {self._mode_label()} stopped", script_id=self.script_id)

    def on_tick(self):
        if self.enabled and self._is_shifter_weapon_mode():
            self._tick_shifter_sequence()

    def chat_event_types(self) -> Tuple[str, ...]:
        return (
            "aa_feedback",
            "attack",
            "averted_death",
            "breach",
            "damage",
            "damage_candidate",
            "death",
            "gi_feedback",
            "kill",
            "player_hide",
            "shifter_state",
            "target_blind",
            "weapon_feedback",
        )

    def on_chat_line(self, sequence: int, text: str):
        self.on_chat_event(parse_chat_line_event(sequence, text))

    def on_chat_event(self, event: ChatLineEvent):
        sequence = int(event.sequence)
        if not self.should_process(sequence) or not self.enabled:
            return
        self.current_chat_sequence = sequence

        if self._is_weapon_mode():
            if self._is_shifter_weapon_mode():
                self._observe_shifter_event(event)
            if event.weapon_feedback:
                self._handle_weapon_swap_feedback(event.weapon_feedback)
            if event.damage is not None:
                self._observe_weapon_damage_event(event.damage)
            elif event.has_kind("damage_candidate"):
                self.weapon_damage_parse_miss_count += 1
                self.host.notify_state_changed()

        if self._mode_label() == self.MODE_DIVINE_SLINGER:
            self._observe_slinger_event(event)

        feedback_type = self._parse_feedback_type_from_event(event)
        if feedback_type is not None:
            self.current_damage_type = feedback_type
            if self._mode_label() == self.MODE_DIVINE_SLINGER:
                self.current_secondary_mode = "damage"
            selection_name = self._selection_name_for_type(feedback_type)
            self.set_status(f"Current {selection_name}")
            return

        attack = event.attack
        if attack is None:
            return

        self._handle_attack_event(attack)

    def _handle_attack_event(self, attack):
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
            self._observe_weapon_attack_result(attack)
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

    def _actor_compare_keys(self, value: object) -> Set[str]:
        text = hgx_combat.normalize_actor_name(str(value or "")).lower()
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return set()
        keys = {text}
        without_suffix = re.sub(r"\s*\[[^\]]+\]\s*$", "", text).strip()
        if without_suffix:
            keys.add(without_suffix)
        return keys

    def _actor_is_self(self, actor_name: object) -> bool:
        actor_keys = self._actor_compare_keys(actor_name)
        if not actor_keys:
            return False
        own_keys: Set[str] = set()
        for value in (
            self._character_name(),
            getattr(self.client, "character_name", ""),
            getattr(self.host.client, "character_name", ""),
            getattr(self.client, "display_name", ""),
            getattr(self.host.client, "display_name", ""),
        ):
            own_keys.update(self._actor_compare_keys(value))
        return bool(actor_keys.intersection(own_keys))

    def _reset_shifter_runtime(self, clear_observed: bool = False):
        self.shifter_swap_stage = ""
        self.shifter_sequence_started_at = 0.0
        self.shifter_pending_target = ""
        self.shifter_pending_reason = ""
        self.shifter_pending_unarm = False
        self.shifter_pending_source_key = ""
        self.shifter_unshift_deadline_at = 0.0
        self.shifter_next_weapon_retry_at = 0.0
        self.shifter_next_shift_attempt_at = 0.0
        self.shifter_shift_attempts = 0
        self.shifter_resume_pending = False
        self.shifter_lock_sent = False
        self.shifter_last_error = ""
        if clear_observed:
            self.shifter_shift_state = "unknown"
            self.shifter_last_shift_line = ""
            self.shifter_last_essence_line = ""
            self.shifter_last_player_hide_at = 0.0
            self.shifter_last_shift_at = 0.0

    def _mode_label(self) -> str:
        mode = str(self.config.get("mode", self.MODE_ARCANE_ARCHER)).strip()
        if mode in (
            self.MODE_ARCANE_ARCHER,
            self.MODE_ZEN_RANGER,
            self.MODE_DIVINE_SLINGER,
            self.MODE_GNOMISH_INVENTOR,
            self.MODE_WEAPON_SWAP,
            self.MODE_SHIFTER_WEAPON_SWAP,
        ):
            return mode
        return self.MODE_ARCANE_ARCHER

    def _is_weapon_mode(self) -> bool:
        return self._mode_label() in (self.MODE_WEAPON_SWAP, self.MODE_SHIFTER_WEAPON_SWAP)

    def _is_shifter_weapon_mode(self) -> bool:
        return self._mode_label() == self.MODE_SHIFTER_WEAPON_SWAP

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
            if parsed[0] != 0:
                raise RuntimeError(
                    f"{binding_key} uses {choice}, but weapon swap slots must be on the base F1-F12 quickbar. "
                    "Move the weapon to a base slot and update Auto Damage."
                )
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

        self.shifter_shift_choice = WEAPON_SLOT_NONE
        self.shifter_shift_page = 0
        self.shifter_shift_slot = 0
        if self._is_shifter_weapon_mode():
            shift_choice = str(self.config.get("shift_slot", WEAPON_SLOT_NONE)).strip() or WEAPON_SLOT_NONE
            parsed_shift_slot = _parse_quickbar_slot_choice(shift_choice)
            if parsed_shift_slot is None:
                raise RuntimeError(f"{self.MODE_SHIFTER_WEAPON_SWAP} requires a shift ability quickbar slot.")
            self.shifter_shift_choice = shift_choice
            self.shifter_shift_page, self.shifter_shift_slot = parsed_shift_slot

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

    def _shifter_shift_display(self) -> str:
        if not self.shifter_shift_slot:
            return WEAPON_SLOT_NONE
        return self.host.format_slot(self.shifter_shift_page, self.shifter_shift_slot)

    def _mark_shifter_unshifted(self, reason: str):
        now = time.monotonic()
        previous = self.shifter_shift_state
        self.shifter_shift_state = "unshifted"
        if "Player Hide" in reason:
            self.shifter_last_player_hide_at = now
        if previous != "unshifted":
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: {self._mode_label()} saw unshifted state ({reason})",
                script_id=self.script_id,
            )
        self.host.notify_state_changed()

    def _mark_shifter_shifted(self, reason: str):
        now = time.monotonic()
        previous = self.shifter_shift_state
        self.shifter_shift_state = "shifted"
        self.shifter_last_shift_at = now
        if previous != "shifted":
            self.host.emit(
                "info",
                f"{self.host.client.display_name}: {self._mode_label()} saw shifted state ({reason})",
                script_id=self.script_id,
            )
        if self.shifter_swap_stage == "reshifting":
            self._finish_shifter_sequence("shift confirmed")
        else:
            self.host.notify_state_changed()

    def _observe_shifter_state_line(self, text: str):
        self._observe_shifter_event(parse_chat_line_event(self.current_chat_sequence, text))

    def _observe_shifter_event(self, event: ChatLineEvent):
        if not event.normalized:
            return

        if event.player_hide:
            self._mark_shifter_unshifted("Player Hide")
            return

        if event.shifter_essence_maximum > 0:
            self.shifter_last_essence_line = event.normalized
            self._mark_shifter_shifted(event.normalized)
            return

        if event.shifter_shift_actor and self._actor_is_self(event.shifter_shift_actor):
            self.shifter_last_shift_line = event.normalized
            self._mark_shifter_shifted(event.normalized)
            return

        if event.averted_death_player and self._actor_is_self(event.averted_death_player):
            self._mark_shifter_unshifted("death averted")
            return

        if event.kill_victim and self._actor_is_self(event.kill_victim):
            self._mark_shifter_unshifted("death")

    def _shifter_send_lock(self):
        if self.shifter_lock_sent or not bool(self.config.get("shifter_lock_target", True)):
            return
        try:
            result = self.host.send_chat("!lock opponent", 2)
            if not result.get("success"):
                self.host.emit(
                    "error",
                    (
                        f"{self.host.client.display_name}: {self._mode_label()} !lock opponent failed "
                        f"rc={result.get('rc')} err={result.get('err')}"
                    ),
                    script_id=self.script_id,
                )
            else:
                self.shifter_lock_sent = True
        except Exception as exc:
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} !lock opponent failed: {exc}",
                script_id=self.script_id,
            )

    def _shifter_send_resume_attack(self):
        if not self.shifter_resume_pending or not bool(self.config.get("shifter_resume_attack", True)):
            self.shifter_resume_pending = False
            return
        self.shifter_resume_pending = False
        try:
            result = self.host.send_chat("!action attack locked", 2)
            if not result.get("success"):
                self.host.emit(
                    "error",
                    (
                        f"{self.host.client.display_name}: {self._mode_label()} !action attack locked failed "
                        f"rc={result.get('rc')} err={result.get('err')}"
                    ),
                    script_id=self.script_id,
                )
        except Exception as exc:
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} !action attack locked failed: {exc}",
                script_id=self.script_id,
            )

    def _begin_shifter_sequence(self, binding: WeaponBinding, target_name: str, reason: str, unarm: bool = False) -> bool:
        if self.shifter_swap_stage:
            self.set_status(f"{target_name}: shifter busy {self.shifter_swap_stage}")
            self.host.notify_state_changed()
            return True

        self.shifter_pending_source_key = binding.key
        self.shifter_pending_target = str(target_name or "").strip()
        self.shifter_pending_reason = str(reason or "").strip()
        self.shifter_pending_unarm = bool(unarm)
        self.shifter_sequence_started_at = time.monotonic()
        self.shifter_resume_pending = True
        self.shifter_lock_sent = False
        self.shifter_last_error = ""
        self._shifter_send_lock()

        if self.shifter_shift_state == "unshifted":
            return self._trigger_shifter_weapon_slot("already unshifted")

        try:
            result = self.host.send_chat("!cancel poly", 2)
        except Exception as exc:
            self.shifter_last_error = str(exc)
            self._reset_shifter_runtime(clear_observed=False)
            self.set_status(f"{target_name}: unshift failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} !cancel poly failed: {exc}",
                script_id=self.script_id,
            )
            return False

        if not result.get("success"):
            self.shifter_last_error = f"rc={result.get('rc')} err={result.get('err')}"
            self._reset_shifter_runtime(clear_observed=False)
            self.set_status(f"{target_name}: unshift failed")
            self.host.emit(
                "error",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} !cancel poly failed "
                    f"rc={result.get('rc')} err={result.get('err')}"
                ),
                script_id=self.script_id,
            )
            return False

        self.shifter_swap_stage = "unshifting"
        self.shifter_unshift_deadline_at = time.monotonic() + self.SHIFTER_UNSHIFT_WAIT_SECONDS
        action = "unarm" if unarm else self._binding_display(binding.key)
        self.set_status(f"{target_name}: unshifting for {reason} {action}")
        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} locked target and sent !cancel poly "
                f"for '{target_name}' reason={reason} weapon={action}"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _trigger_shifter_weapon_slot(self, reason: str, preserve_retry_count: bool = False) -> bool:
        binding = self.weapon_bindings.get(self.shifter_pending_source_key)
        target_name = self.shifter_pending_target or "target"
        if binding is None:
            self.set_status(f"{target_name}: shifter weapon source missing")
            self._begin_shifter_reshift("weapon source missing")
            return False

        try:
            result = self.host.trigger_slot(binding.slot, page=binding.page)
        except Exception as exc:
            self.shifter_last_error = str(exc)
            self.set_status(f"{target_name}: shifter swap failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self._mode_label()} trigger failed for {self._binding_display(binding.key)}: {exc}",
                script_id=self.script_id,
            )
            self._begin_shifter_reshift("weapon trigger failed")
            return False

        if not result.get("success"):
            self.shifter_last_error = f"rc={result.get('rc')} err={result.get('err')}"
            self.set_status(f"{target_name}: shifter swap failed")
            self.host.emit(
                "error",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} trigger failed for "
                    f"{self._binding_display(binding.key)} rc={result.get('rc')} aux={result.get('aux_rc')} "
                    f"path={result.get('path')} err={result.get('err')}"
                ),
                script_id=self.script_id,
            )
            self._begin_shifter_reshift("weapon trigger failed")
            return False

        now = time.monotonic()
        self.pending_weapon_key = binding.key
        self.pending_weapon_unarm = bool(self.shifter_pending_unarm)
        self.pending_weapon_ready_at = now + self._weapon_swap_cooldown_seconds()
        self.pending_weapon_requested_at = now
        self.pending_weapon_retry_count = self.pending_weapon_retry_count if preserve_retry_count else 0
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
        self.shifter_swap_stage = "weapon_pending"
        self.shifter_next_weapon_retry_at = now + self.SHIFTER_WEAPON_CONFIRM_RETRY_SECONDS

        pending_display = self._pending_weapon_display()
        self.set_status(f"{target_name}: {self.shifter_pending_reason} {pending_display}; re-shift pending")
        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} triggered {pending_display} "
                f"after unshift ({reason}) success={result.get('success')} rc={result.get('rc')} "
                f"aux={result.get('aux_rc')} path={result.get('path')} err={result.get('err')}"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return True

    def _confirm_shifter_weapon_ready(self, reason: str) -> bool:
        if self.shifter_swap_stage != "weapon_pending" or not self.pending_weapon_key:
            return False
        pending_display = self._pending_weapon_display()
        if not self._confirm_pending_weapon(reason):
            return False
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} confirmed {pending_display}; shifting back",
            script_id=self.script_id,
        )
        self._begin_shifter_reshift(reason)
        return True

    def _begin_shifter_reshift(self, reason: str):
        self.shifter_swap_stage = "reshifting"
        self.shifter_shift_attempts = 0
        self.shifter_next_shift_attempt_at = 0.0
        self._trigger_shifter_shift(reason)

    def _trigger_shifter_shift(self, reason: str) -> bool:
        target_name = self.shifter_pending_target or "target"
        if not self.shifter_shift_slot:
            self.shifter_last_error = "no shift slot configured"
            self.set_status(f"{target_name}: no shifter slot")
            self.host.notify_state_changed()
            return False

        try:
            result = self.host.trigger_slot(self.shifter_shift_slot, page=self.shifter_shift_page)
        except Exception as exc:
            self.shifter_last_error = str(exc)
            result = {"success": False, "rc": 0, "aux_rc": 0, "path": 0, "err": str(exc)}

        now = time.monotonic()
        self.shifter_shift_attempts += 1
        first_retry = self.SHIFTER_SHIFT_FIRST_RETRY_SECONDS
        retry = self.SHIFTER_SHIFT_RETRY_SECONDS
        self.shifter_next_shift_attempt_at = now + (first_retry if self.shifter_shift_attempts == 1 else retry)

        if result.get("success"):
            self.set_status(
                f"{target_name}: shifting back via {self._shifter_shift_display()} "
                f"(try {self.shifter_shift_attempts})"
            )
        else:
            self.shifter_last_error = f"rc={result.get('rc')} err={result.get('err')}"
            self.set_status(
                f"{target_name}: shift retry pending via {self._shifter_shift_display()} "
                f"(try {self.shifter_shift_attempts})"
            )
            self.host.emit(
                "error",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} shift trigger failed "
                    f"slot={self._shifter_shift_display()} rc={result.get('rc')} aux={result.get('aux_rc')} "
                    f"path={result.get('path')} err={result.get('err')}"
                ),
                script_id=self.script_id,
            )
        self.host.notify_state_changed()
        return bool(result.get("success"))

    def _finish_shifter_sequence(self, reason: str):
        target_name = self.shifter_pending_target or "target"
        self._shifter_send_resume_attack()
        self._reset_shifter_runtime(clear_observed=False)
        self.set_status(f"{target_name}: shifted; attack resumed")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: {self._mode_label()} finished ({reason}) and resumed locked target",
            script_id=self.script_id,
        )
        self.host.notify_state_changed()

    def _tick_shifter_sequence(self):
        if not self._is_shifter_weapon_mode() or not self.shifter_swap_stage:
            return

        now = time.monotonic()
        if self.shifter_swap_stage == "unshifting":
            if self.shifter_shift_state == "unshifted" or now >= self.shifter_unshift_deadline_at:
                reason = "Player Hide" if self.shifter_shift_state == "unshifted" else "unshift wait elapsed"
                self._trigger_shifter_weapon_slot(reason)
            return

        if self.shifter_swap_stage == "weapon_pending":
            if self.pending_weapon_key:
                matches = self._query_equipped_binding_keys(force=True)
                if self.pending_weapon_unarm:
                    if self.weapon_equipped_probe_at > 0.0 and self.pending_weapon_key not in matches:
                        self._confirm_shifter_weapon_ready("equipped mask shows unarmed")
                        return
                elif self.pending_weapon_key in matches:
                    self._confirm_shifter_weapon_ready("equipped quickbar mask")
                    return

                if now >= self.shifter_next_weapon_retry_at and self.pending_weapon_retry_count < self.WEAPON_PENDING_MAX_RETRIES:
                    binding = self.weapon_bindings.get(self.pending_weapon_key)
                    if binding is not None:
                        self.pending_weapon_retry_count += 1
                        self.shifter_next_weapon_retry_at = now + self.SHIFTER_WEAPON_CONFIRM_RETRY_SECONDS
                        self.host.emit(
                            "info",
                            (
                                f"{self.host.client.display_name}: {self._mode_label()} retrying "
                                f"{self._pending_weapon_display()} before re-shift "
                                f"({self.pending_weapon_retry_count}/{self.WEAPON_PENDING_MAX_RETRIES})"
                            ),
                            script_id=self.script_id,
                        )
                        self._trigger_shifter_weapon_slot("weapon confirmation retry", preserve_retry_count=True)
                        return

                self.set_status(f"{self.shifter_pending_target}: waiting for {self._pending_weapon_display()} before re-shift")
                return

            self._begin_shifter_reshift("weapon pending cleared")
            return

        if self.shifter_swap_stage == "reshifting":
            if self.shifter_shift_state == "shifted":
                self._finish_shifter_sequence("already shifted")
                return
            if now >= self.shifter_next_shift_attempt_at:
                self._trigger_shifter_shift("retry")

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
        self.pending_weapon_retry_count = 0

    def _mark_external_weapon_unknown(self, feedback: str):
        self._clear_pending_weapon_state()
        previous = self._binding_display(self.current_weapon_key)
        self.current_weapon_key = WEAPON_CURRENT_UNKNOWN
        feedback_text = str(feedback or "").strip()
        already_marked = self.weapon_external_unknown and self.weapon_external_unknown_feedback == feedback_text
        self.weapon_external_unknown = True
        self.weapon_external_unknown_feedback = feedback_text
        self.set_status("Weapon state unknown after external swap")
        if not already_marked:
            detail = feedback_text or "weapon swap feedback"
            self.host.emit(
                "info",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} detected external weapon swap "
                    f"({detail}); current weapon changed from {previous} to {WEAPON_CURRENT_UNKNOWN} until SimKeys re-establishes control"
                ),
                script_id=self.script_id,
            )
        self.host.notify_state_changed()

    def _clear_external_weapon_unknown(self, reason: str):
        if not self.weapon_external_unknown:
            return
        feedback = self.weapon_external_unknown_feedback
        self.weapon_external_unknown = False
        self.weapon_external_unknown_feedback = ""
        detail = f" after {feedback}" if feedback else ""
        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} resumed weapon learning "
                f"({reason}{detail})"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()

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

    def _shifter_equipped_mask_is_authoritative(self) -> bool:
        return not (self._is_shifter_weapon_mode() and self.shifter_shift_state == "shifted")

    def _set_current_weapon_from_equipped_key(self, binding_key: str, reason: str) -> bool:
        if binding_key not in self.weapon_profiles:
            return False
        if self.current_weapon_key == binding_key:
            if self.weapon_external_unknown:
                self._clear_external_weapon_unknown(reason)
            return False

        previous = self._binding_display(self.current_weapon_key)
        self.current_weapon_key = binding_key
        self._clear_external_weapon_unknown(reason)
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
        if self.pending_weapon_key or self.weapon_external_unknown:
            return ""
        if not self._shifter_equipped_mask_is_authoritative():
            return ""

        matches = self._query_equipped_binding_keys(force=force)
        if len(matches) != 1:
            return ""

        binding_key = matches[0]
        self._set_current_weapon_from_equipped_key(binding_key, "equipped quickbar mask")
        return binding_key

    def _equipped_mask_confirms_binding(self, binding_key: str, force: bool = False) -> Optional[bool]:
        matches = self._query_equipped_binding_keys(force=force)
        if not self._shifter_equipped_mask_is_authoritative():
            return True if binding_key in matches else None
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

        if self._is_shifter_weapon_mode():
            return None

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
        if not self._profile_is_p2(profile):
            if profile.stable_signature:
                return set(profile.stable_signature)
            if profile.candidate_signature:
                return set(profile.candidate_signature)
            return set(profile.current_signature)
        known_types = set(profile.stable_signature) | set(profile.current_signature)
        for signature in profile.signature_counts.keys():
            known_types.update(signature)
        for signature in profile.target_signatures.values():
            known_types.update(signature)
        known_types.update(profile.type_counts.keys())
        known_types.update(profile.type_estimates.keys())
        return known_types

    def _expanded_p2_component_estimates(self, profile: Optional[WeaponLearningProfile], components: Dict[int, float]) -> Dict[int, float]:
        expanded = dict(components)
        if profile is None or not self._profile_is_p2(profile):
            return expanded

        elemental_values = [
            float(expanded[damage_type])
            for damage_type in sorted(WEAPON_ELEMENTAL_TYPES)
            if damage_type in expanded and float(expanded[damage_type]) > 0.0
        ]
        exotic_values = [
            float(expanded[damage_type])
            for damage_type in sorted(WEAPON_EXOTIC_TYPES)
            if damage_type in expanded and float(expanded[damage_type]) > 0.0
        ]

        if elemental_values:
            elemental_average = sum(elemental_values) / float(len(elemental_values))
            for damage_type in sorted(WEAPON_ELEMENTAL_TYPES):
                expanded.setdefault(damage_type, elemental_average)

        if exotic_values:
            exotic_average = sum(exotic_values) / float(len(exotic_values))
            for damage_type in sorted(WEAPON_EXOTIC_TYPES):
                expanded.setdefault(damage_type, exotic_average)

        return expanded

    def _profile_predicted_damage_types(self, profile: Optional[WeaponLearningProfile]) -> Set[int]:
        predicted_types = set(self._profile_known_damage_types(profile))
        if profile is not None and self._profile_is_p2(profile):
            predicted_types.update(WEAPON_SIGNATURE_TYPES)
        return predicted_types

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
        if not self._profile_is_p2(profile):
            committed_types = self._profile_known_damage_types(profile)
            components = {
                damage_type: base_damage
                for damage_type, base_damage in components.items()
                if damage_type in committed_types
            }
        return self._expanded_p2_component_estimates(profile, components)

    def _profile_signature_for_target(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
    ) -> Tuple[int, ...]:
        if profile is None:
            return ()
        if not self._profile_is_p2(profile):
            if profile.stable_signature:
                return tuple(profile.stable_signature)
            if profile.candidate_signature:
                return tuple(profile.candidate_signature)
            return tuple(profile.current_signature)
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
        if self._is_shifter_weapon_mode():
            return False
        if profile is None or self._profile_is_p2(profile):
            return False
        signature = tuple(profile.stable_signature)
        return self._is_p2_signature(signature)

    def _profile_p2_verification_complete(self, profile: Optional[WeaponLearningProfile]) -> bool:
        if not self._profile_requires_p2_verification(profile):
            return True
        return len(getattr(profile, "p2_verification_targets", set()) or set()) >= 2

    def _profile_has_target_sample(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
    ) -> bool:
        if profile is None:
            return False
        target_key = self._profile_target_key(creature_name)
        if not target_key:
            return False
        if target_key in profile.target_signatures:
            return True
        if target_key in profile.target_type_estimates:
            return True
        observed_map = profile.target_damage_observations.get(target_key) or {}
        return any(
            observed is not None and int(getattr(observed, "observations", 0)) > 0
            for observed in observed_map.values()
        )

    def _profile_can_advance_learning_on_target(
        self,
        profile: Optional[WeaponLearningProfile],
        creature_name: str,
    ) -> bool:
        if profile is None or self._profile_learning_complete(profile):
            return False
        if self._profile_has_target_sample(profile, creature_name):
            return False
        if not self._profile_requires_p2_verification(profile):
            return True

        target_key = self._profile_target_key(creature_name)
        if not target_key or target_key in profile.p2_verification_targets:
            return False

        generic_signature = self._generic_p2_signature_for_target(creature_name)
        if not generic_signature:
            return False
        return tuple(generic_signature) != tuple(profile.stable_signature)

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
        is_critical: bool,
    ):
        observed = observed_map.get(signature)
        if observed is None:
            observed = WeaponObservedDamage()
            observed_map[signature] = observed

        if is_critical:
            observed.critical_average_total = self._update_observed_damage_bucket(
                observed.critical_recent_totals,
                int(actual_total),
            )
            observed.critical_observations += 1
        else:
            observed.normal_average_total = self._update_observed_damage_bucket(
                observed.normal_recent_totals,
                int(actual_total),
            )
            observed.normal_observations += 1

        observed.observations += 1
        observed.last_total = int(actual_total)
        observed.max_total = max(int(observed.max_total), int(actual_total))
        observed.average_total = self._combined_observed_damage_average(observed)

    def _update_observed_damage_bucket(self, totals: List[int], actual_total: int) -> float:
        totals.append(max(int(actual_total), 0))
        max_window = max(int(self.WEAPON_ACTUAL_DAMAGE_WINDOW), 3)
        if len(totals) > max_window:
            del totals[:-max_window]
        values = [max(int(total), 0) for total in totals]
        if not values:
            return 0.0
        return float(sum(values)) / float(len(values))

    def _combined_observed_damage_average(self, observed: WeaponObservedDamage) -> float:
        if observed.observations <= 0:
            return 0.0
        if observed.normal_observations <= 0:
            return float(observed.critical_average_total)
        if observed.critical_observations <= 0:
            return float(observed.normal_average_total)

        crit_rate = float(observed.critical_observations) / float(max(int(observed.observations), 6))
        crit_rate = min(max(crit_rate, 0.0), 1.0)
        return (
            (float(observed.normal_average_total) * (1.0 - crit_rate))
            + (float(observed.critical_average_total) * crit_rate)
        )

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
        is_critical: bool = False,
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
        self._apply_observed_damage_map(observed_map, tuple(signature), actual_total, bool(is_critical))

    def _selection_damage_score(self, expected_damage: int, actual_damage: Optional[int], actual_observations: int) -> int:
        if actual_damage is None or actual_observations <= 0:
            return int(expected_damage)
        actual_weight = min(max(float(actual_observations), 0.0) / 3.0, 2.0)
        if actual_weight <= 0.0:
            return int(expected_damage)
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
        if not self._profile_is_p2(profile):
            committed_types = self._profile_known_damage_types(profile)
            components = {
                damage_type: base_damage
                for damage_type, base_damage in components.items()
                if damage_type in committed_types
            }
        return self._expanded_p2_component_estimates(profile, components)

    def _is_mammons_tear_target_name(self, creature_name: str) -> bool:
        return _normalize_creature_name_key(creature_name) in MAMMONS_TEAR_TARGETS

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
        if self._is_shifter_weapon_mode():
            return ""
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
            predicted_types = self._profile_predicted_damage_types(profile)
            if predicted_types:
                parts.append("Predicted " + self._format_weapon_type_set(predicted_types))
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
        if self._profile_learning_complete(profile):
            parts.append("Learning Complete")
        elif self._profile_requires_p2_verification(profile):
            verified = min(len(profile.p2_verification_targets), 2)
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

    def _weapon_swap_min_gain_percent(self) -> float:
        return max(float(self.config.get("min_swap_gain_percent", 6.0)), 0.0)

    def _shifter_swap_min_gain_percent(self) -> float:
        return max(float(self.config.get("shifter_min_swap_gain_percent", 300.0)), 0.0)

    def _shifter_healing_only(self) -> bool:
        return _parse_bool(self.config.get("shifter_healing_only", False), False)

    def _pending_weapon_retry_seconds(self) -> float:
        return max(self._weapon_swap_cooldown_seconds() * 2.0, 12.0)

    def _retry_pending_weapon(self, target_name: str, reason: str) -> bool:
        if not self.pending_weapon_key:
            return False
        if self.pending_weapon_retry_count >= self.WEAPON_PENDING_MAX_RETRIES:
            return False

        binding = self.weapon_bindings.get(self.pending_weapon_key)
        if binding is None:
            return False

        try:
            result = self.host.trigger_slot(binding.slot, page=binding.page)
        except Exception as exc:
            self.host.emit(
                "error",
                (
                    f"{self.host.client.display_name}: {self._mode_label()} retry failed for "
                    f"{self._binding_display(binding.key)}: {exc}"
                ),
                script_id=self.script_id,
            )
            return False

        self.pending_weapon_retry_count += 1
        if result["success"]:
            now = time.monotonic()
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
            self.set_status(
                f"{target_name}: retry {self._pending_weapon_display()} "
                f"({self.pending_weapon_retry_count}/{self.WEAPON_PENDING_MAX_RETRIES})"
            )
        else:
            self.set_status(f"{target_name}: retry failed {self._pending_weapon_display()}")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self._mode_label()} retried pending "
                f"{self._binding_display(binding.key)} on '{target_name}' reason={reason} "
                f"attempt={self.pending_weapon_retry_count}/{self.WEAPON_PENDING_MAX_RETRIES} "
                f"success={result['success']} rc={result['rc']} aux={result['aux_rc']} "
                f"path={result['path']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        self.host.notify_state_changed()
        return bool(result["success"])

    def _confirm_pending_weapon(self, reason: str) -> bool:
        if not self.pending_weapon_key:
            return False
        if self.pending_weapon_unarm:
            return self._confirm_unarmed_state(reason)

        pending_key = self.pending_weapon_key
        self.current_weapon_key = pending_key
        self._clear_pending_weapon_state()
        self._clear_external_weapon_unknown(reason)
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
        self._clear_external_weapon_unknown(reason)
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
        self._handle_weapon_swap_feedback(feedback)

    def _handle_weapon_swap_feedback(self, feedback: str):
        self.weapon_last_swap_feedback = feedback
        if not self.pending_weapon_key:
            self._mark_external_weapon_unknown(feedback)
            return

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
            if self._is_shifter_weapon_mode() and self.shifter_swap_stage == "weapon_pending":
                self._confirm_shifter_weapon_ready("weapon equipped feedback")
                return
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

    def _prune_pending_weapon_attack_results(self):
        minimum_sequence = max(int(self.current_chat_sequence) - 40, 0)
        self.weapon_pending_attack_results = [
            result
            for result in self.weapon_pending_attack_results
            if int(result.sequence) >= minimum_sequence
        ][-24:]

    def _observe_weapon_attack_result(self, attack):
        if not getattr(attack, "is_hit", False):
            return
        self._prune_pending_weapon_attack_results()
        self.weapon_pending_attack_results.append(
            PendingWeaponAttackResult(
                sequence=int(self.current_chat_sequence),
                defender=str(attack.defender or ""),
                is_critical=bool(getattr(attack, "is_critical", False)),
            )
        )

    def _consume_pending_weapon_attack_result(self, defender_name: str) -> Optional[PendingWeaponAttackResult]:
        self._prune_pending_weapon_attack_results()
        defender_key = hgx_combat.normalize_actor_name(defender_name).lower()
        if not defender_key:
            return None

        for index, result in enumerate(self.weapon_pending_attack_results):
            if hgx_combat.normalize_actor_name(result.defender).lower() != defender_key:
                continue
            return self.weapon_pending_attack_results.pop(index)
        return None

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
            retry_due = (
                self.pending_weapon_requested_at > 0.0
                and now >= (self.pending_weapon_requested_at + self._pending_weapon_retry_seconds())
            )
            retry_safe = (
                not equipped_key
                or equipped_key == self.current_weapon_key
                or equipped_key == current_profile.binding.key
            )
            if retry_due and retry_safe:
                self._retry_pending_weapon(
                    defender_name,
                    f"stalled pending swap; still seeing {self._binding_display(current_profile.binding.key)} damage",
                )
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
        if self._is_shifter_weapon_mode():
            return False
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
        if self._is_shifter_weapon_mode():
            return False
        if self._profile_is_p2(profile):
            return False
        distinct_p2_signatures = [
            signature
            for signature in profile.signature_counts.keys()
            if self._is_p2_signature(signature)
        ]
        if len(distinct_p2_signatures) < 2:
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

        profile.signature_counts[signature] = profile.signature_counts.get(signature, 0) + 1

        if self._profile_is_p2(profile):
            profile.current_signature = signature
            if target_key:
                profile.target_signatures[target_key] = signature
            profile.candidate_signature = ()
            profile.candidate_signature_streak = 0
            profile.mismatch_streak = 0
            return profile.signature_counts[signature] == 1

        if self._update_profile_dynamic_kind(profile):
            profile.current_signature = signature
            if target_key:
                profile.target_signatures[target_key] = signature
            profile.candidate_signature = ()
            profile.candidate_signature_streak = 0
            profile.mismatch_streak = 0
            return True

        if profile.stable_signature:
            if tuple(profile.stable_signature) == signature:
                profile.current_signature = signature
                if target_key:
                    profile.target_signatures[target_key] = signature
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

        profile.current_signature = signature
        if target_key:
            profile.target_signatures[target_key] = signature
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
        is_critical: bool = False,
    ) -> Tuple[bool, bool, Set[int], Set[int]]:
        before_known_types = set(self._profile_known_damage_types(profile))
        before_estimated_types = set(profile.type_estimates.keys())
        target_key = self._profile_target_key(damage_line.defender)
        variation_candidate = False

        if profile.stable_signature and observed_types != set(profile.stable_signature):
            if self._profile_accepts_signature_variation(profile, observed_types):
                profile.mismatch_streak = 0
                variation_candidate = True
            elif not self._record_profile_signature_mismatch(profile, observed_types, damage_line.defender, now):
                return False, False, set(), set()
        else:
            profile.mismatch_streak = 0

        profile.observations += 1
        profile.last_seen_at = now
        signature_changed = self._observe_profile_signature(profile, observed_types, target_key)
        commit_components = not variation_candidate or self._profile_is_p2(profile)
        combat_profile = self.db._resolve_combat_profile(damage_line.defender)
        if commit_components:
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
                self._record_profile_target_actual_damage(
                    profile,
                    target_key,
                    self._damage_signature(observed_types),
                    damage_line,
                    is_critical=is_critical,
                )

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
        self._observe_weapon_damage_event(damage_line, counted_candidate=True)

    def _observe_weapon_damage_event(self, damage_line, counted_candidate: bool = False):
        if counted_candidate:
            self.weapon_damage_parse_miss_count = max(self.weapon_damage_parse_miss_count - 1, 0)
        self.weapon_damage_seen_count += 1
        character_name = self._character_name()
        character_key = hgx_combat.normalize_actor_name(character_name).lower() if character_name else ""
        if not character_key or damage_line.attacker.lower() != character_key:
            self.weapon_last_ignored_damage_actor = damage_line.attacker
            self.host.notify_state_changed()
            return
        self.weapon_damage_matched_count += 1
        attack_result = self._consume_pending_weapon_attack_result(damage_line.defender)

        now = time.monotonic()
        if self.weapon_external_unknown and not self.pending_weapon_key:
            self.set_status("Weapon state unknown after external swap")
            return

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
            is_critical=bool(attack_result.is_critical) if attack_result is not None else False,
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
                learned_types=tuple(sorted(self._profile_predicted_damage_types(profile))),
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
            learned_types=tuple(sorted(self._profile_predicted_damage_types(profile))),
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

    def _current_weapon_candidate(self, candidates: List[WeaponRecommendation]) -> Optional[WeaponRecommendation]:
        current_key = str(self.current_weapon_key or "").strip()
        if not current_key:
            return None
        for candidate in candidates:
            if candidate.binding.key == current_key:
                return candidate
        return None

    def _weapon_swap_gain_percent(
        self,
        current_candidate: Optional[WeaponRecommendation],
        best_candidate: Optional[WeaponRecommendation],
    ) -> Optional[float]:
        if current_candidate is None or best_candidate is None:
            return None

        current_score = max(int(current_candidate.selection_damage), 0)
        best_score = max(int(best_candidate.selection_damage), 0)
        if best_score <= current_score:
            return 0.0
        if current_score <= 0:
            return None
        return ((float(best_score) - float(current_score)) * 100.0) / float(current_score)

    def _should_hold_current_weapon_for_margin(
        self,
        current_candidate: Optional[WeaponRecommendation],
        best_candidate: Optional[WeaponRecommendation],
        protected_target: bool = False,
    ) -> bool:
        if protected_target:
            return False
        if current_candidate is None or best_candidate is None:
            return False
        if current_candidate.binding.key == best_candidate.binding.key:
            return False
        if current_candidate.healing_types:
            return False

        current_score = max(int(current_candidate.selection_damage), 0)
        best_score = max(int(best_candidate.selection_damage), 0)
        if best_score <= current_score:
            return True
        if current_score <= 0:
            return False

        gain_percent = self._weapon_swap_gain_percent(current_candidate, best_candidate)
        if gain_percent is None:
            return False
        return gain_percent < self._weapon_swap_min_gain_percent()

    def _recommend_weapon_for_target(
        self,
        safe_candidates: List[WeaponRecommendation],
        protected_target: bool = False,
    ) -> Tuple[Optional[WeaponRecommendation], Optional[WeaponRecommendation], Optional[WeaponRecommendation]]:
        if protected_target:
            best_candidate = self._mammon_wrath_candidate(safe_candidates)
        else:
            best_candidate = self._choose_best_weapon(safe_candidates) if safe_candidates else None
        current_candidate = self._current_weapon_candidate(safe_candidates)
        if self._should_hold_current_weapon_for_margin(current_candidate, best_candidate, protected_target=protected_target):
            return current_candidate, best_candidate, current_candidate
        return best_candidate, best_candidate, current_candidate

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

    def _next_weapon_to_learn(self, target_name: str) -> Optional[WeaponLearningProfile]:
        if not self.weapon_profiles or not target_name:
            return None

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if (
            current_profile is not None
            and not self._profile_learning_complete(current_profile)
            and self._profile_can_advance_learning_on_target(current_profile, target_name)
            and current_profile.attack_attempts < self.WEAPON_LEARNING_ATTACKS_BEFORE_ROTATE
        ):
            return current_profile

        candidates = [
            profile
            for profile in self.weapon_profiles.values()
            if (
                profile.binding.key != self.current_weapon_key
                and not self._profile_learning_complete(profile)
                and self._profile_can_advance_learning_on_target(profile, target_name)
            )
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
        if self._is_shifter_weapon_mode():
            return self._request_shifter_weapon_swap(binding, target_name, reason)

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
            self.pending_weapon_retry_count = 0
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

        if self._is_shifter_weapon_mode():
            return self._begin_shifter_sequence(binding, target_name, reason, unarm=True)

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
            self.pending_weapon_retry_count = 0
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

    def _request_shifter_weapon_swap(self, binding: WeaponBinding, target_name: str, reason: str) -> bool:
        equipped_keys = self._query_equipped_binding_keys(force=True)
        if binding.key in equipped_keys:
            self._cancel_pending_weapon(f"{self._binding_display(binding.key)} is already equipped")
            self._set_current_weapon_from_equipped_key(binding.key, "already equipped before shifter swap")
            self.set_status(f"{target_name}: already using {self._binding_display(binding.key)}")
            self.host.notify_state_changed()
            return True
        return self._begin_shifter_sequence(binding, target_name, reason, unarm=False)

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
        if (not self._is_shifter_weapon_mode()) and self._is_mammons_tear_target(profile.matched_name):
            analysis["special_target_rule"] = "Mammon's Wrath is the only allowed weapon for this target."

        candidates = self._weapon_candidates_for_target(target_name)
        safe_candidates = [candidate for candidate in candidates if not candidate.healing_types]
        protected_target = (not self._is_shifter_weapon_mode()) and self._is_mammons_tear_target(profile.matched_name)
        recommendation = None
        best_candidate = None
        current_candidate = None
        if self._is_shifter_weapon_mode():
            current_candidate = self._current_weapon_candidate(candidates)
            if current_candidate is None:
                analysis["special_target_rule"] = "Shifter mode is holding because the current weapon is unknown."
            elif not safe_candidates:
                analysis["special_target_rule"] = "Current shifter weapon heals this target and no configured weapon is safe."
            else:
                best_candidate = self._choose_best_weapon(safe_candidates)
                if current_candidate.healing_types:
                    recommendation = best_candidate
                    analysis["special_target_rule"] = "Current shifter weapon heals this target; swapping is allowed."
                elif self._shifter_healing_only():
                    recommendation = current_candidate
                    best_candidate = current_candidate
                    analysis["special_target_rule"] = "Shifter mode is set to only swap for healing targets."
                elif best_candidate is None or best_candidate.binding.key == current_candidate.binding.key:
                    recommendation = current_candidate
                    analysis["special_target_rule"] = (
                        f"Shifter mode holds unless a safe alternate exceeds +{self._shifter_swap_min_gain_percent():.1f}%."
                    )
                else:
                    gain_percent = self._weapon_swap_gain_percent(current_candidate, best_candidate)
                    threshold = self._shifter_swap_min_gain_percent()
                    if gain_percent is not None and gain_percent >= threshold:
                        recommendation = best_candidate
                        analysis["special_target_rule"] = (
                            f"Safe alternate exceeds shifter threshold (+{gain_percent:.1f}% >= {threshold:.1f}%)."
                        )
                    else:
                        recommendation = current_candidate
                        if gain_percent is None:
                            analysis["special_target_rule"] = (
                                f"Shifter mode holds unless a safe alternate exceeds +{threshold:.1f}%."
                            )
                        else:
                            analysis["special_target_rule"] = (
                                f"Shifter mode holding: best alternate is +{gain_percent:.1f}% under the +{threshold:.1f}% threshold."
                            )
        elif protected_target:
            recommendation, best_candidate, current_candidate = self._recommend_weapon_for_target(
                safe_candidates,
                protected_target=True,
            )
        else:
            if not safe_candidates:
                analysis["special_target_rule"] = "No configured weapon is safe here; Auto Damage will prefer an unarmed fallback."
            recommendation, best_candidate, current_candidate = self._recommend_weapon_for_target(
                safe_candidates,
                protected_target=False,
            )
        if recommendation is not None:
            analysis["recommended_weapon"] = recommendation.binding.key
            analysis["recommended_is_mammon_wrath"] = recommendation.special_name == "Mammon's Wrath"
        if (
            recommendation is not None
            and best_candidate is not None
            and current_candidate is not None
            and recommendation.binding.key == current_candidate.binding.key
            and best_candidate.binding.key != current_candidate.binding.key
        ):
            gain_percent = self._weapon_swap_gain_percent(current_candidate, best_candidate)
            if gain_percent is not None:
                threshold = self._weapon_swap_min_gain_percent()
                hold_text = (
                    f"Hold current unless gain exceeds {threshold:.1f}% "
                    f"(best alternate is only +{gain_percent:.1f}%)."
                )
                if analysis["special_target_rule"]:
                    analysis["special_target_rule"] = f"{analysis['special_target_rule']} {hold_text}"
                else:
                    analysis["special_target_rule"] = hold_text

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
            retry_due = (
                not self.pending_weapon_unarm
                and self.pending_weapon_requested_at > 0.0
                and now >= (self.pending_weapon_requested_at + self._pending_weapon_retry_seconds())
            )
            if retry_due:
                equipped_keys = self._query_equipped_binding_keys(force=True)
                equipped_key = equipped_keys[0] if len(equipped_keys) == 1 else ""
                retry_safe = not equipped_key or equipped_key == self.current_weapon_key
                if retry_safe and self._retry_pending_weapon(
                    attack.defender,
                    "attack loop observed stale pending swap with no confirming damage",
                ):
                    return
            self.set_status(f"{attack.defender}: awaiting {self._pending_weapon_display()} damage")
            return

        self._reconcile_current_weapon_from_equipped_mask()

        current_profile = self.weapon_profiles.get(self.current_weapon_key)
        if (
            current_profile is not None
            and not self._profile_learning_complete(current_profile)
            and self._profile_can_advance_learning_on_target(current_profile, attack.defender)
        ):
            current_profile.attack_attempts += 1
            current_profile.last_attack_at = now

        learning_profile = self._next_weapon_to_learn(attack.defender)
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

        current_any_candidate = self._current_weapon_candidate(candidates)
        safe_candidates = [candidate for candidate in candidates if not candidate.healing_types]
        protected_target = (not self._is_shifter_weapon_mode()) and self._is_mammons_tear_target(attack.defender)
        if protected_target:
            recommendation, best_candidate, current_candidate = self._recommend_weapon_for_target(
                safe_candidates,
                protected_target=True,
            )
            if recommendation is None:
                self.set_status(f"{attack.defender}: Mammon's Wrath required")
                return
        else:
            recommendation = None
            best_candidate = None
            current_candidate = None

        if self._is_shifter_weapon_mode():
            if current_any_candidate is None:
                self.set_status(f"{attack.defender}: current weapon unknown; shifter hold")
                self.host.notify_state_changed()
                return
            if not current_any_candidate.healing_types:
                if self._shifter_healing_only():
                    summary = self._weapon_recommendation_summary(current_any_candidate)
                    self.set_status(
                        f"{attack.defender}: keep {self._binding_display(current_any_candidate.binding.key)} "
                        f"{summary} (healing-only)"
                    )
                    self.host.notify_state_changed()
                    return

                best_candidate = self._choose_best_weapon(safe_candidates)
                if best_candidate is None or best_candidate.binding.key == current_any_candidate.binding.key:
                    summary = self._weapon_recommendation_summary(current_any_candidate)
                    self.set_status(f"{attack.defender}: keep {self._binding_display(current_any_candidate.binding.key)} {summary}")
                    self.host.notify_state_changed()
                    return

                gain_percent = self._weapon_swap_gain_percent(current_any_candidate, best_candidate)
                threshold = self._shifter_swap_min_gain_percent()
                if gain_percent is None or gain_percent < threshold:
                    if gain_percent is None:
                        self.set_status(
                            f"{attack.defender}: keep {self._binding_display(current_any_candidate.binding.key)} "
                            f"(shifter threshold {threshold:.1f}%)"
                        )
                    else:
                        self.set_status(
                            f"{attack.defender}: keep {self._binding_display(current_any_candidate.binding.key)} "
                            f"(+{gain_percent:.1f}% < {threshold:.1f}%)"
                        )
                    self.host.notify_state_changed()
                    return

                recommendation = best_candidate
                current_candidate = current_any_candidate

        if not safe_candidates:
            unsafe_candidate = self._choose_best_weapon(candidates)
            healing_text = ", ".join(_format_damage_type_label(value) for value in unsafe_candidate.healing_types) if unsafe_candidate else "unknown"
            if self._request_unarmed_fallback(attack.defender, f"unarm unsafe ({healing_text})"):
                return
            self.set_status(f"{attack.defender}: unsafe ({healing_text})")
            return

        if recommendation is None:
            recommendation, best_candidate, current_candidate = self._recommend_weapon_for_target(
                safe_candidates,
                protected_target=False,
            )
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

        if (
            current_candidate is not None
            and best_candidate is not None
            and recommendation.binding.key == current_candidate.binding.key
            and best_candidate.binding.key != current_candidate.binding.key
        ):
            gain_percent = self._weapon_swap_gain_percent(current_candidate, best_candidate)
            threshold = self._weapon_swap_min_gain_percent()
            if gain_percent is not None:
                self.set_status(
                    f"{attack.defender}: keep {self._binding_display(current_candidate.binding.key)} "
                    f"(+{gain_percent:.1f}% < {threshold:.1f}%)"
                )
            else:
                self.set_status(f"{attack.defender}: keep {self._binding_display(current_candidate.binding.key)}")
            return

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
            "current_display": (
                f"{self._binding_display(self.current_weapon_key)} (external swap)"
                if self.weapon_external_unknown
                else self._binding_display(self.current_weapon_key)
            ),
            "pending_weapon": self.pending_weapon_key,
            "pending_display": self._pending_weapon_display(),
            "external_unknown": self.weapon_external_unknown,
            "external_unknown_feedback": self.weapon_external_unknown_feedback,
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
            "shifter_mode": self._is_shifter_weapon_mode(),
            "shifter_state": self.shifter_shift_state,
            "shifter_stage": self.shifter_swap_stage,
            "shifter_shift_slot": self._shifter_shift_display(),
            "shifter_shift_attempts": self.shifter_shift_attempts,
            "shifter_last_shift": self.shifter_last_shift_line,
            "shifter_last_essence": self.shifter_last_essence_line,
            "shifter_last_error": self.shifter_last_error,
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

    def _parse_feedback_type_from_event(self, event: ChatLineEvent) -> Optional[int]:
        if self._is_weapon_mode():
            return None
        if self._mode_label() == self.MODE_GNOMISH_INVENTOR:
            return event.gi_feedback_type
        feedback_type = event.aa_feedback_type
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
        self._observe_slinger_event(parse_chat_line_event(self.current_chat_sequence, text))

    def _observe_slinger_event(self, event: ChatLineEvent):
        now = time.monotonic()
        self._cleanup_slinger_states(now)
        self._refresh_slinger_state_timeouts(now)

        breach = event.breach
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

        if self.current_secondary_mode == "blind" and event.target_blind:
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
    script_label = "Auto Follow"
    DEFAULT_FOLLOW_CUES = ("fall in", "follow me", "follow my")
    ASO_COMMAND = "!action aso target"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.follow_cues: Tuple[str, ...] = ()
        self.cooldown_until = 0.0
        self.follow_count = 0
        self.last_speaker = ""
        self.last_message = ""
        self.last_error_key = ""

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.follow_cues = _load_follow_cues(self._follow_cues_dir()) or self.DEFAULT_FOLLOW_CUES
        self.cooldown_until = 0.0
        self.follow_count = 0
        self.last_speaker = ""
        self.last_message = ""
        self.last_error_key = ""
        self.set_status(f"Listening for {len(self.follow_cues)} follow cues")
        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: {self.script_label} started "
                f"with {len(self.follow_cues)} follow cues from {self._follow_cues_dir()}"
            ),
            script_id=self.script_id,
        )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self.follow_cues = ()
        self.host.emit("info", f"{self.host.client.display_name}: {self.script_label} stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return
        self._handle_follow_line(text)

    def _handle_follow_line(self, text: str):
        parsed = self._parse_follow_cue(text)
        if parsed is None:
            return False

        speaker, message = parsed
        if self._speaker_matches_character(speaker):
            self.set_status(f"Ignored own cue ({speaker})")
            return True

        now = time.monotonic()
        if now < self.cooldown_until:
            self.set_status(f"{speaker}: cooldown")
            return True

        tell_command = f'/tell "{speaker}" !target'
        try:
            aso_result = self.host.send_chat(self.ASO_COMMAND, 2)
            target_result = self.host.send_chat(tell_command, 2)
        except Exception as exc:
            self.cooldown_until = now + 1.0
            self.set_status(f"{speaker}: follow failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: {self.script_label} chat send failed for '{speaker}': {exc}",
                script_id=self.script_id,
            )
            return True

        self.cooldown_until = now + self._cooldown_seconds()
        self.follow_count += 1
        self.last_speaker = speaker
        self.last_message = message
        success = bool(aso_result["success"] and target_result["success"])
        if success:
            self.set_status(f"{speaker}: followed")
            if self.last_error_key:
                self.host.emit("info", f"{self.host.client.display_name}: {self.script_label} recovered", script_id=self.script_id)
                self.last_error_key = ""
        else:
            self.set_status(f"{speaker}: follow failed")
            self.last_error_key = f"{aso_result['rc']}:{aso_result['err']}:{target_result['rc']}:{target_result['err']}"

        self.host.emit(
            "info" if success else "error",
            (
                f"{self.host.client.display_name}: {self.script_label} cue from '{speaker}' message='{message}' "
                f"aso success={aso_result['success']} rc={aso_result['rc']} err={aso_result['err']} "
                f"target success={target_result['success']} rc={target_result['rc']} err={target_result['err']}"
            ),
            script_id=self.script_id,
        )
        if success and bool(self.config.get("echo_console", False)):
            self.host.send_console(f"SimKeys {self.script_label} -> {speaker}")
        self.host.notify_state_changed()
        return True

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
        cues = self.follow_cues or self.DEFAULT_FOLLOW_CUES
        if not any(cue.lower() in lowered_message for cue in cues):
            return None
        return speaker, message

    def _follow_cues_dir(self) -> str:
        value = str(self.config.get("follow_cues_dir", "") or "").strip()
        if value:
            return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))
        return _default_follow_cues_dir()

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
            "follow_cues": len(self.follow_cues),
            "follow_cues_dir": self._follow_cues_dir(),
            "follow_count": self.follow_count,
            "last_speaker": self.last_speaker,
            "last_message": self.last_message,
        }


class AlwaysOnScript(AutoFollowScript):
    script_id = "always_on"
    script_label = "Always On"

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.wallet_count = 0
        self.spellbook_fill_count = 0
        self.fog_off_count = 0
        self.last_wallet_action = ""

    def on_start(self):
        super().on_start()
        self.wallet_count = 0
        self.spellbook_fill_count = 0
        self.fog_off_count = 0
        self.last_wallet_action = ""
        self.set_status("Listening for utility cues")

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return

        line = str(text or "")
        self._handle_auto_wallet_line(line)
        if not self._disabled("disable_follow"):
            self._handle_follow_line(line)

    def _handle_auto_wallet_line(self, line: str):
        if not line:
            return

        if "You are now in Zerial's Workshop" in line and not self._disabled("disable_wallet"):
            deposit_ok = self._send_utility_command("!wallet deposit all", "wallet deposit")
            withdraw_ok = self._send_utility_command("!wallet withdraw 100000", "wallet withdraw")
            self.wallet_count += 1
            self.last_wallet_action = "Zerial's Workshop"
            if deposit_ok and withdraw_ok:
                self.set_status("Wallet refreshed")
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Always On refreshed wallet at Zerial's Workshop",
                    script_id=self.script_id,
                )
            self.host.notify_state_changed()

        if "Resting." in line and not self._disabled("disable_spellbook_fill"):
            if self._send_utility_command("!sb fill", "spellbook fill"):
                self.spellbook_fill_count += 1
                self.last_wallet_action = "Resting"
                self.set_status("Spellbook filled")
                self.host.notify_state_changed()

        if "You are now in" in line and not self._disabled("disable_fog_off"):
            if self._send_utility_command("##mainscene.fog 0", "fog off"):
                self.fog_off_count += 1
                self.last_wallet_action = "Fog off"
                self.set_status("Fog disabled")
                self.host.notify_state_changed()

    def _disabled(self, key: str) -> bool:
        return bool(self.config.get(key, False))

    def _send_utility_command(self, command: str, label: str) -> bool:
        try:
            result = self.host.send_chat(command, 2)
        except Exception as exc:
            self.set_status(f"{label} failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: Always On {label} failed: {exc}",
                script_id=self.script_id,
            )
            return False

        if result["success"]:
            return True

        self.set_status(f"{label} failed")
        self.host.emit(
            "error",
            (
                f"{self.host.client.display_name}: Always On {label} failed command={command} "
                f"rc={result['rc']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        return False

    def get_state_details(self) -> dict:
        details = super().get_state_details()
        details.update({
            "wallet_count": self.wallet_count,
            "spellbook_fill_count": self.spellbook_fill_count,
            "fog_off_count": self.fog_off_count,
            "last_wallet_action": self.last_wallet_action,
            "disable_follow": self._disabled("disable_follow"),
            "disable_wallet": self._disabled("disable_wallet"),
            "disable_spellbook_fill": self._disabled("disable_spellbook_fill"),
            "disable_fog_off": self._disabled("disable_fog_off"),
        })
        return details


class AutoCombatModeScript(ClientScriptBase):
    script_id = "auto_rsm"
    MODE_RAPID_SHOT = "Rapid Shot"
    MODE_FLURRY_OF_BLOWS = "Flurry of Blows"
    MODE_EXPERTISE = "Expertise"
    MODE_IMPROVED_EXPERTISE = "Improved Expertise"
    MODE_POWER_ATTACK = "Power Attack"
    MODE_IMPROVED_POWER_ATTACK = "Improved Power Attack"
    MODE_CONFIG = {
        MODE_RAPID_SHOT: ("!action rsm self", "memory"),
        MODE_FLURRY_OF_BLOWS: ("!action fbm self", "combat log"),
        MODE_EXPERTISE: ("!action exm self", "combat log"),
        MODE_IMPROVED_EXPERTISE: ("!action iem self", "combat log"),
        MODE_POWER_ATTACK: ("!action pam self", "combat log"),
        MODE_IMPROVED_POWER_ATTACK: ("!action ipm self", "combat log"),
    }
    MODE_CHOICES = tuple(MODE_CONFIG.keys())

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.process_handle = None
        self.rsm_address = 0
        self.cooldown_until = 0.0
        self.identity_wait_logged = False
        self.last_probe_error = ""
        self.trigger_count = 0
        self.last_defender = ""
        self.last_active_modes: Tuple[str, ...] = ()

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.rsm_address = 0
        self.cooldown_until = 0.0
        self.identity_wait_logged = False
        self.last_probe_error = ""
        self.trigger_count = 0
        self.last_defender = ""
        self.last_active_modes = ()
        mode = self._mode_label()
        if mode == self.MODE_RAPID_SHOT:
            try:
                address = self._resolve_rsm_address()
                self.set_status(f"{mode} armed 0x{address:08X}")
            except Exception:
                self.set_status(f"{mode} armed (probe pending)")
        else:
            self.set_status(f"{mode} armed")
        self.host.emit(
            "info",
            f"{self.host.client.display_name}: Auto Combat Mode started ({mode})",
            script_id=self.script_id,
        )

    def on_stop(self):
        super().on_stop()
        self.enabled = False
        self._close_process_handle()
        self.host.emit("info", f"{self.host.client.display_name}: Auto Combat Mode stopped", script_id=self.script_id)

    def chat_event_types(self) -> Tuple[str, ...]:
        return ("attack",)

    def on_chat_event(self, event: ChatLineEvent):
        if not self.should_process(event.sequence) or not self.enabled:
            return
        attack = event.attack
        if attack is None:
            return
        self._handle_attack_event(attack)

    def on_chat_line(self, sequence: int, text: str):
        if not self.should_process(sequence) or not self.enabled:
            return

        attack = hgx_combat.parse_attack_line(text)
        if attack is None:
            return
        self._handle_attack_event(attack)

    def _handle_attack_event(self, attack):
        character_name = self._character_name()
        if not character_name:
            self.set_status("Waiting for character name")
            if not self.identity_wait_logged:
                self.identity_wait_logged = True
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Auto Combat Mode is waiting for character identity before parsing attack lines",
                    script_id=self.script_id,
                )
            return

        character_key = hgx_combat.normalize_actor_name(character_name).lower()
        if attack.attacker.lower() != character_key:
            return

        now = time.monotonic()
        if now < self.cooldown_until:
            return

        mode = self._mode_label()
        command = self._mode_command()
        if self._mode_is_active(mode, attack):
            return

        try:
            result = self.host.send_chat(command, 2)
        except Exception as exc:
            self.set_status("Trigger failed")
            self.host.emit(
                "error",
                f"{self.host.client.display_name}: Auto Combat Mode {mode} chat send failed: {exc}",
                script_id=self.script_id,
            )
            self.cooldown_until = now + 1.0
            return

        self.cooldown_until = now + self._cooldown_seconds()
        self.last_defender = attack.defender
        self.trigger_count += 1
        if result["success"]:
            self.set_status(f"{mode} triggered")
        else:
            self.set_status("Trigger failed")

        self.host.emit(
            "info",
            (
                f"{self.host.client.display_name}: Auto Combat Mode triggered {mode} on '{attack.defender}' "
                f"command={command} success={result['success']} rc={result['rc']} err={result['err']}"
            ),
            script_id=self.script_id,
        )
        if bool(self.config.get("echo_console", False)):
            self.host.send_console(
                f"SimKeys Auto Combat Mode {mode} -> {command} rc={result['rc']} err={result['err']}"
            )

    def _mode_label(self) -> str:
        mode = str(self.config.get("mode", self.MODE_RAPID_SHOT)).strip()
        if mode in self.MODE_CONFIG:
            return mode
        return self.MODE_RAPID_SHOT

    def _mode_command(self) -> str:
        return self.MODE_CONFIG[self._mode_label()][0]

    def _mode_is_active(self, mode: str, attack) -> bool:
        if mode == self.MODE_RAPID_SHOT:
            try:
                rsm_status = self._read_rsm_status()
            except Exception as exc:
                error_text = str(exc)
                self.set_status("Probe failed")
                if error_text != self.last_probe_error:
                    self.last_probe_error = error_text
                    self.host.emit(
                        "error",
                        f"{self.host.client.display_name}: Auto Combat Mode Rapid Shot memory probe failed: {error_text}",
                        script_id=self.script_id,
                    )
                return True

            if self.last_probe_error:
                self.host.emit(
                    "info",
                    f"{self.host.client.display_name}: Auto Combat Mode Rapid Shot memory probe recovered",
                    script_id=self.script_id,
                )
                self.last_probe_error = ""

            if rsm_status != 0:
                self.set_status(f"{mode} active ({rsm_status})")
                return True
            return False

        active_modes = hgx_combat.parse_attack_mode_names(attack.attack_mode)
        self.last_active_modes = active_modes
        if mode in active_modes:
            self.set_status(f"{mode} active")
            return True
        return False

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
        return max(float(self.config.get("cooldown_seconds", 6.0)), 0.1)

    def get_state_details(self) -> dict:
        return {
            "mode": self._mode_label(),
            "trigger_count": self.trigger_count,
            "last_defender": self.last_defender,
            "last_active_modes": self.last_active_modes,
        }


class InGameTimersScript(ClientScriptBase):
    script_id = "ingame_timers"
    OVERLAY_ID = 7100
    LIMBO_SOURCE = "limbo"
    LIMBO_NORMAL_COLOR = 0xFFFFFF
    LIMBO_WARNING_COLOR = 0xFFFF66
    LIMBO_DANGER_COLOR = 0xFF6666
    LIMBO_RECOVERED_COLOR = 0x808080
    EFFECT_REQUEST_DELAY_SECONDS = 0.50
    EFFECT_REQUEST_RETRY_SECONDS = 5.0
    EFFECT_REQUEST_TIMEOUT_SECONDS = 14.0
    REST_RE = re.compile(r"\b(resting|rested|rests)\b", re.IGNORECASE)
    DEATH_RE = re.compile(r"\b(you are dead|you died|you have been killed)\b", re.IGNORECASE)

    def __init__(self, client, config: Dict[str, object], host):
        super().__init__(client, config, host)
        self.enabled = False
        self.rules: Tuple[OverlayTimerRule, ...] = ()
        self.spell_defaults: Tuple[SpellTimerSpec, ...] = ()
        self.spell_specs: Tuple[SpellTimerSpec, ...] = ()
        self.spell_specs_by_key: Dict[str, SpellTimerSpec] = {}
        self.spell_specs_by_effect_key: Dict[str, SpellTimerSpec] = {}
        self.pending_effect_queries: Dict[str, PendingSpellEffectQuery] = {}
        self.limbo_actor_keys: Set[str] = set()
        self.character_db = None
        self.active: Dict[str, ActiveOverlayTimer] = {}
        self.last_render_text = ""
        self.last_overlay_error = ""
        self.last_effect_request_error = ""
        self.last_limbo_db_error = ""
        self.next_render_at = 0.0
        self.matched_count = 0
        self.cleared_count = 0
        self.limbo_count = 0
        self.limbo_recovered_count = 0

    def on_start(self):
        super().on_start()
        self.enabled = True
        self.active.clear()
        self.pending_effect_queries.clear()
        self.limbo_actor_keys = self._configured_limbo_actor_keys()
        self.last_limbo_db_error = ""
        self.character_db = self._load_limbo_character_database() if self._limbo_enabled() else None
        self.last_render_text = ""
        self.last_overlay_error = ""
        self.last_effect_request_error = ""
        self.next_render_at = 0.0
        self.matched_count = 0
        self.cleared_count = 0
        self.limbo_count = 0
        self.limbo_recovered_count = 0
        rules_dir = self._rules_dir()
        self.rules = _load_status_timer_rules(rules_dir)
        self.spell_defaults = _load_hgx_spell_timer_specs(rules_dir)
        self.spell_specs = _parse_spell_timer_config(self.config.get("spell_timers", ""), self.spell_defaults)
        self.spell_specs_by_key = {spec.key: spec for spec in self.spell_specs}
        self.spell_specs_by_effect_key = {
            _spell_key(spec.effect): spec
            for spec in self.spell_specs
            if _spell_key(spec.effect)
        }
        self.set_status(f"Loaded {len(self.rules)} rules, {len(self.spell_specs)} spells")
        self.host.emit(
            "info",
            (
                f"{self.client.display_name}: In-Game Timers loaded {len(self.rules)} rules and "
                f"{len(self.spell_specs)} self-cast spell timers from {rules_dir}; "
                f"limbo enemy records={len(self.character_db.records) if self.character_db is not None else 0}, "
                f"allowlist={len(self.limbo_actor_keys)}"
            ),
            script_id=self.script_id,
        )
        self._render_overlay(force=True)

    def on_stop(self):
        self.enabled = False
        self.active.clear()
        self.pending_effect_queries.clear()
        self.limbo_actor_keys.clear()
        self.character_db = None
        self._clear_overlay()
        super().on_stop()
        self.host.emit("info", f"{self.client.display_name}: In-Game Timers stopped", script_id=self.script_id)

    def on_chat_line(self, sequence: int, text: str):
        if not self.enabled or not self.should_process(sequence):
            return

        line = hgx_combat.normalize_chat_line(text)
        if not line:
            return

        now = time.monotonic()
        changed = False
        if self.REST_RE.search(line):
            self._clear_flagged_timers(rest=True)
        if self.DEATH_RE.search(line):
            changed = self._clear_buff_timers() or changed

        if self._handle_effect_timer_line(line, now):
            changed = True
        if self._handle_spell_cast_line(line, now):
            changed = True
        if self._handle_limbo_line(line, now):
            changed = True
        for rule in self.rules:
            match = rule.pattern.search(line)
            if not match:
                continue

            if rule.kind == "vartimer":
                duration = _duration_from_var_match(match)
            else:
                duration = rule.duration_seconds

            if duration <= 0:
                changed = self._clear_timer_label(rule.text) or changed
                continue

            self.active[rule.key] = ActiveOverlayTimer(
                label=rule.text,
                description=rule.description,
                expires_at=now + duration,
                duration_seconds=duration,
                color_rgb=rule.color_rgb,
                disable_on_death=rule.disable_on_death,
                disable_on_rest=rule.disable_on_rest,
                source=rule.source,
            )
            self.matched_count += 1
            changed = True

        if changed:
            self._render_overlay(force=True)

    def on_tick(self):
        if not self.enabled:
            return
        now = time.monotonic()
        pending_changed = self._service_pending_effect_queries(now)
        expired = [
            key
            for key, timer in self.active.items()
            if timer.expires_at <= now
        ]
        for key in expired:
            self.active.pop(key, None)
        if expired:
            self.cleared_count += len(expired)
        if now >= self.next_render_at or expired or pending_changed:
            self._render_overlay(force=bool(expired or pending_changed))

    def _rules_dir(self) -> str:
        value = str(self.config.get("rules_dir", "") or "").strip()
        if value:
            return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))
        return _default_status_rules_dir()

    def _clear_flagged_timers(self, rest: bool = False, death: bool = False):
        removed = []
        for key, timer in self.active.items():
            if (rest and timer.disable_on_rest) or (death and timer.disable_on_death):
                removed.append(key)
        for key in removed:
            self.active.pop(key, None)
        if removed:
            self.cleared_count += len(removed)
            self._render_overlay(force=True)

    def _clear_buff_timers(self) -> bool:
        removed = [
            key
            for key, timer in self.active.items()
            if timer.source != self.LIMBO_SOURCE
        ]
        for key in removed:
            self.active.pop(key, None)

        pending_count = len(self.pending_effect_queries)
        if pending_count:
            self.pending_effect_queries.clear()

        if removed:
            self.cleared_count += len(removed)
        return bool(removed or pending_count)

    def _clear_timer_label(self, label: str) -> bool:
        label_key = str(label or "").strip().lower()
        if not label_key:
            return False
        removed = [
            key
            for key, timer in self.active.items()
            if timer.label.strip().lower() == label_key
        ]
        for key in removed:
            self.active.pop(key, None)
        if removed:
            self.cleared_count += len(removed)
        return bool(removed)

    def _actor_keys(self, value: object) -> Set[str]:
        text = hgx_combat.normalize_actor_name(str(value or ""))
        if not text:
            return set()
        values = {text}
        values.add(re.sub(r"\s+\[[^\]]+\]\s*$", "", text).strip())
        return {
            re.sub(r"\s+", " ", item).strip().lower()
            for item in values
            if item.strip()
        }

    def _actor_primary_key(self, value: object) -> str:
        text = hgx_combat.normalize_actor_name(str(value or ""))
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip().lower()

    def _actor_name_variants(self, value: object) -> Tuple[str, ...]:
        text = hgx_combat.normalize_actor_name(str(value or ""))
        if not text:
            return ()
        values = [text, re.sub(r"\s+\[[^\]]+\]\s*$", "", text).strip()]
        seen = set()
        variants = []
        for item in values:
            key = re.sub(r"\s+", " ", item).strip()
            lowered = key.lower()
            if key and lowered not in seen:
                seen.add(lowered)
                variants.append(key)
        return tuple(variants)

    def _self_actor_keys(self) -> Set[str]:
        keys: Set[str] = set()
        for source in (
            getattr(self.host.client, "display_name", ""),
            getattr(self.host.client, "character_name", ""),
            getattr(self.client, "display_name", ""),
            getattr(self.client, "character_name", ""),
        ):
            keys.update(self._actor_keys(source))
        return keys

    def _caster_matches_self(self, caster: str) -> bool:
        caster_keys = self._actor_keys(caster)
        return bool(caster_keys and caster_keys.intersection(self._self_actor_keys()))

    def _actor_matches_self(self, actor: str) -> bool:
        actor_keys = self._actor_keys(actor)
        return bool(actor_keys and actor_keys.intersection(self._self_actor_keys()))

    def _self_target_name(self) -> str:
        for source in (
            getattr(self.host.client, "character_name", ""),
            getattr(self.client, "character_name", ""),
            getattr(self.host.client, "display_name", ""),
            getattr(self.client, "display_name", ""),
        ):
            name = str(source or "").strip()
            if name:
                return name
        return ""

    def _iter_limbo_names(self, value: object) -> Tuple[str, ...]:
        if isinstance(value, (list, tuple, set)):
            names: List[str] = []
            for item in value:
                names.extend(self._iter_limbo_names(item))
            return tuple(names)

        text = str(value or "").strip()
        if not text:
            return ()
        return tuple(part.strip() for part in re.split(r"[;\r\n]+", text) if part.strip())

    def _configured_limbo_actor_keys(self) -> Set[str]:
        keys: Set[str] = set()
        for config_key in ("limbo_auto_names", "limbo_names"):
            for name in self._iter_limbo_names(self.config.get(config_key, "")):
                keys.update(self._actor_keys(name))

        for source in (
            getattr(self.host.client, "display_name", ""),
            getattr(self.host.client, "character_name", ""),
            getattr(self.client, "display_name", ""),
            getattr(self.client, "character_name", ""),
        ):
            keys.update(self._actor_keys(source))
        return keys

    def _limbo_enabled(self) -> bool:
        value = self.config.get("enable_limbo", True)
        if isinstance(value, str):
            return _parse_bool(value, True)
        return bool(value)

    def _limbo_duration_seconds(self) -> float:
        try:
            return max(float(self.config.get("limbo_duration_seconds", 300.0)), 1.0)
        except (TypeError, ValueError):
            return 300.0

    def _load_limbo_character_database(self):
        try:
            return hgx_data.load_character_database(hgx_data.default_character_data_dir())
        except Exception as exc:
            self.last_limbo_db_error = str(exc)
            self.host.emit(
                "error",
                (
                    f"{self.client.display_name}: In-Game Timers could not load characters.d "
                    f"for Limbo filtering: {exc}"
                ),
                script_id=self.script_id,
            )
            return None

    def _limbo_actor_is_known_enemy(self, value: object) -> bool:
        if self.character_db is None:
            return False
        for name in self._actor_name_variants(value):
            if self.character_db.lookup(name) is not None:
                return True
        return False

    def _is_limbo_actor(self, value: object) -> bool:
        if not self._limbo_enabled():
            return False
        actor_keys = self._actor_keys(value)
        if not actor_keys:
            return False
        if actor_keys.intersection(self.limbo_actor_keys):
            return True
        if self.character_db is None:
            return False
        return not self._limbo_actor_is_known_enemy(value)

    def _handle_spell_cast_line(self, line: str, now: float) -> bool:
        match = SPELL_CAST_LINE_RE.match(line)
        if match is None:
            return False
        if not self._caster_matches_self(match.group("caster")):
            return False

        spell_name = match.group("spell").strip()
        spec = self.spell_specs_by_key.get(_spell_key(spell_name))
        if spec is None:
            return False

        effect_key = _spell_key(spec.effect or spec.spell)
        self.pending_effect_queries[effect_key] = PendingSpellEffectQuery(
            spec=spec,
            cast_at=now,
            next_request_at=now + self.EFFECT_REQUEST_DELAY_SECONDS,
            deadline_at=now + self.EFFECT_REQUEST_TIMEOUT_SECONDS,
        )
        self.set_status(f"Checking {spec.label}")
        return True

    def _handle_effect_timer_line(self, line: str, now: float) -> bool:
        changed = False
        last_status = ""
        for match in EFFECT_TIMER_LINE_RE.finditer(line):
            effect_name = match.group("effect").strip()
            remaining = _parse_effect_remaining_seconds(match.group("remaining"))
            if remaining <= 0:
                continue

            effect_key = _spell_key(effect_name)
            pending = self.pending_effect_queries.pop(effect_key, None)
            spec = pending.spec if pending is not None else self.spell_specs_by_effect_key.get(effect_key)
            if spec is None:
                continue

            self.active[f"spell:{spec.key}"] = ActiveOverlayTimer(
                label=spec.label,
                description=effect_name,
                expires_at=now + remaining,
                duration_seconds=remaining,
                color_rgb=spec.color_rgb,
                disable_on_death=True,
                disable_on_rest=True,
                source=spec.source,
            )
            self.matched_count += 1
            last_status = f"{spec.label}: {_format_remaining(remaining)}"
            changed = True

        if last_status:
            self.set_status(last_status)
        return changed

    def _service_pending_effect_queries(self, now: float) -> bool:
        changed = False
        for effect_key, pending in list(self.pending_effect_queries.items()):
            if now >= pending.deadline_at:
                self.pending_effect_queries.pop(effect_key, None)
                self._fallback_pending_spell_timer(pending, now)
                changed = True
                continue

            if now < pending.next_request_at:
                continue

            target_name = self._self_target_name()
            if not target_name:
                pending.next_request_at = now + 1.0
                self.set_status(f"Waiting for character name ({pending.spec.label})")
                changed = True
                continue

            try:
                effects_result = self.host.send_chat("!effects", 2)
                target_result = self.host.send_chat(f'/tell "{target_name}" !target', 2)
            except Exception as exc:
                error_key = f"{type(exc).__name__}:{exc}"
                if error_key != self.last_effect_request_error:
                    self.last_effect_request_error = error_key
                    self.host.emit(
                        "error",
                        f"{self.client.display_name}: In-Game Timers effect request failed: {exc}",
                        script_id=self.script_id,
                    )
                pending.next_request_at = now + self.EFFECT_REQUEST_RETRY_SECONDS
                changed = True
                continue

            pending.attempts += 1
            pending.next_request_at = now + self.EFFECT_REQUEST_RETRY_SECONDS
            success = bool(effects_result.get("success") and target_result.get("success"))
            if success:
                if self.last_effect_request_error:
                    self.host.emit(
                        "info",
                        f"{self.client.display_name}: In-Game Timers effect requests recovered",
                        script_id=self.script_id,
                    )
                    self.last_effect_request_error = ""
                self.set_status(f"Requested {pending.spec.label}")
            else:
                error_key = (
                    f"{effects_result.get('rc')}:{effects_result.get('err')}:"
                    f"{target_result.get('rc')}:{target_result.get('err')}"
                )
                if error_key != self.last_effect_request_error:
                    self.last_effect_request_error = error_key
                    self.host.emit(
                        "error",
                        (
                            f"{self.client.display_name}: In-Game Timers effect request failed "
                            f"!effects success={effects_result.get('success')} rc={effects_result.get('rc')} "
                            f"err={effects_result.get('err')} target success={target_result.get('success')} "
                            f"rc={target_result.get('rc')} err={target_result.get('err')}"
                        ),
                        script_id=self.script_id,
                    )
                self.set_status(f"{pending.spec.label}: request failed")
            changed = True
        return changed

    def _fallback_pending_spell_timer(self, pending: PendingSpellEffectQuery, now: float) -> bool:
        spec = pending.spec
        if spec.duration_seconds <= 0:
            self.set_status(f"{spec.label}: effect not found")
            return False

        remaining = max(spec.duration_seconds - max(now - pending.cast_at, 0.0), 1.0)
        self.active[f"spell:{spec.key}"] = ActiveOverlayTimer(
            label=spec.label,
            description=spec.effect,
            expires_at=now + remaining,
            duration_seconds=remaining,
            color_rgb=spec.color_rgb,
            disable_on_death=True,
            disable_on_rest=True,
            source=spec.source,
        )
        self.matched_count += 1
        self.set_status(f"{spec.label}: fallback")
        return True

    def _handle_limbo_line(self, line: str, now: float) -> bool:
        limbo_enabled = self._limbo_enabled()
        changed = False
        for raw_line in str(line or "").splitlines():
            event_line = raw_line.strip()
            if not event_line:
                continue

            averted = AVERTED_DEATH_LINE_RE.match(event_line)
            if averted is not None:
                player = hgx_combat.normalize_actor_name(averted.group("player"))
                method = hgx_combat.normalize_actor_name(averted.group("method"))
                if self._actor_matches_self(player):
                    changed = self._clear_buff_timers() or changed
                if limbo_enabled:
                    changed = self._mark_limbo_recovered(player, method) or changed
                continue

            parsed_kill = self._parse_limbo_kill_line(event_line)
            if parsed_kill is None:
                continue
            killer, victim = parsed_kill
            if self._actor_matches_self(victim):
                changed = self._clear_buff_timers() or changed
            if not limbo_enabled:
                continue
            if not self._is_limbo_actor(victim):
                continue
            self._start_limbo_timer(victim, killer, now)
            changed = True

        return changed

    def _parse_limbo_kill_line(self, line: str) -> Optional[Tuple[str, str]]:
        text = str(line or "").strip()
        if not text:
            return None
        if text.startswith("You have the following accomplishments"):
            return None
        if "You cannot gain experience, tags, or random loot from monsters killed in a different area." in text:
            return None

        marker = " killed "
        marker_at = text.lower().find(marker)
        if marker_at <= 0:
            return None

        killer = hgx_combat.normalize_actor_name(text[:marker_at])
        victim = hgx_combat.normalize_actor_name(text[marker_at + len(marker):])
        if not killer or not victim:
            return None
        return killer, victim

    def _start_limbo_timer(self, victim: str, killer: str, now: float):
        key = f"{self.LIMBO_SOURCE}:{self._actor_primary_key(victim)}"
        duration = self._limbo_duration_seconds()
        self.active[key] = ActiveOverlayTimer(
            label=victim,
            description=f"killed by {killer}",
            expires_at=now + duration,
            duration_seconds=duration,
            color_rgb=0xFFFFFF,
            disable_on_death=False,
            disable_on_rest=False,
            source=self.LIMBO_SOURCE,
            state="limbo",
        )
        self.limbo_count += 1
        self.matched_count += 1
        self.set_status(f"{victim}: limbo")

    def _mark_limbo_recovered(self, player: str, method: str) -> bool:
        if not self._is_limbo_actor(player):
            return False

        player_keys = self._actor_keys(player)
        matches = [
            timer
            for timer in self.active.values()
            if timer.source == self.LIMBO_SOURCE and self._actor_keys(timer.label).intersection(player_keys)
        ]
        if not matches:
            return False

        timer = max(matches, key=lambda item: item.expires_at)
        timer.state = "recovered"
        timer.description = method or "recovered"
        timer.color_rgb = 0x808080
        self.limbo_recovered_count += 1
        self.set_status(f"{timer.label}: recovered")
        return True

    def _limbo_line_color(self, timer: ActiveOverlayTimer, remaining: float) -> int:
        if timer.state == "recovered":
            return self.LIMBO_RECOVERED_COLOR
        if remaining <= 30.0:
            return self.LIMBO_DANGER_COLOR
        if remaining <= 60.0:
            return self.LIMBO_WARNING_COLOR
        return self.LIMBO_NORMAL_COLOR

    def _format_lines(self) -> Tuple[str, ...]:
        now = time.monotonic()
        max_timers = max(int(self.config.get("max_timers", 8)), 1)
        timers = sorted(self.active.values(), key=lambda timer: (timer.expires_at, timer.label.lower()))
        lines = []
        for timer in timers[:max_timers]:
            remaining = timer.expires_at - now
            if timer.source == self.LIMBO_SOURCE:
                state = "safe" if timer.state == "recovered" else "limbo"
                line = f"{timer.label} {state} {_format_remaining(remaining)}"
                lines.append(f"{_overlay_line_color_prefix(self._limbo_line_color(timer, remaining))}{line}")
            else:
                lines.append(f"{timer.label} {_format_remaining(remaining)}")
        return tuple(lines)

    def _render_overlay(self, force: bool = False):
        lines = self._format_lines()
        if lines:
            text = "Timers\n" + "\n".join(lines)
        elif self.pending_effect_queries:
            text = "Timers\nChecking..."
        else:
            text = "Timers\nReady"
        now = time.monotonic()
        self.next_render_at = now + 0.50

        if not force and text == self.last_render_text:
            return

        try:
            result = self.host.show_overlay_text(
                text,
                overlay_id=self.OVERLAY_ID,
                position=str(self.config.get("position", "TR")),
                offset_x=int(self.config.get("offset_x", 0)),
                offset_y=int(self.config.get("offset_y", 80)),
                font_size=int(self.config.get("font_size", 16)),
                color=_timer_color_rgb(self.config.get("color", "White")),
            )
        except Exception as exc:
            error_text = str(exc)
            self.set_status("Overlay failed")
            if error_text != self.last_overlay_error:
                self.last_overlay_error = error_text
                self.host.emit(
                    "error",
                    f"{self.client.display_name}: In-Game Timers overlay update failed: {error_text}",
                    script_id=self.script_id,
                )
            return

        if self.last_overlay_error:
            self.host.emit("info", f"{self.client.display_name}: In-Game Timers overlay recovered", script_id=self.script_id)
            self.last_overlay_error = ""
        self.last_render_text = text
        pending_count = len(self.pending_effect_queries)
        if pending_count:
            self.set_status(f"{len(lines)} active, {pending_count} pending")
        else:
            self.set_status(f"{len(lines)} active")
        if not result.get("success"):
            self.set_status("Overlay failed")

    def _clear_overlay(self):
        try:
            self.host.clear_overlay(self.OVERLAY_ID)
        except Exception as exc:
            error_text = str(exc)
            if error_text != self.last_overlay_error:
                self.last_overlay_error = error_text
                self.host.emit(
                    "error",
                    f"{self.client.display_name}: In-Game Timers overlay clear failed: {error_text}",
                    script_id=self.script_id,
                )

    def get_state_details(self) -> dict:
        return {
            "rules": len(self.rules),
            "spell_timers": len(self.spell_specs),
            "active": len(self.active),
            "pending_effects": len(self.pending_effect_queries),
            "matched_count": self.matched_count,
            "cleared_count": self.cleared_count,
            "limbo_count": self.limbo_count,
            "limbo_recovered_count": self.limbo_recovered_count,
            "limbo_enemy_records": len(self.character_db.records) if self.character_db is not None else 0,
            "limbo_allowlist": len(self.limbo_actor_keys),
            "rules_dir": self._rules_dir(),
        }


class ClientScriptHost:
    PASSWORD_PROMPT_TEXT = "you must speak your password before you can continue."
    PASSWORD_CHAT_BLOCK_SECONDS = 5.0
    PASSWORD_PROMPT_POLL_INTERVAL = 0.25
    PASSWORD_PROMPT_MAX_LINES = 20
    DAMAGE_METER_POLL_INTERVAL = 0.10
    DAMAGE_METER_MAX_LINES = 200

    def __init__(self, client, event_callback: Callable[[dict], None]):
        self.client = client
        self.event_callback = event_callback
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.scripts: Dict[str, ClientScriptBase] = {}
        self.latest_sequence = 0
        self.password_chat_blocked_until = 0.0
        self.run_id = 0
        self.overlay_controls_enabled = False
        self.overlay_controls_dirty = False
        self.last_overlay_controls_text = ""
        self.last_slow_event_log_at = 0.0
        self.damage_meter_recorder = damage_meter.DamageMeterRecorder(self.client.pid)
        self.last_damage_meter_error = ""

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
            if self._chat_is_locked_out():
                raise RuntimeError(
                    f"{self.client.display_name} just showed a password prompt; wait a few seconds before restarting scripts."
                )
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
            self._ensure_thread_locked()
            on_start_started_at = time.perf_counter()
            try:
                script.on_start()
            except Exception as exc:
                self.scripts.pop(definition.script_id, None)
                if not self.scripts and not self.overlay_controls_enabled:
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
            self.overlay_controls_dirty = True
            self.emit(
                "info",
                (
                    f"{self.client.display_name}: {definition.name} ready in {elapsed:.2f}s "
                    f"(setup {factory_elapsed:.2f}s, arm {on_start_elapsed:.2f}s)"
                ),
                script_id=definition.script_id,
            )
        self.notify_state_changed()

    def _ensure_thread_locked(self):
        if self.thread is None or not self.thread.is_alive() or self.stop_event.is_set():
            self.stop_event = threading.Event()
            self.run_id += 1
            run_id = self.run_id
            stop_event = self.stop_event
            self.thread = threading.Thread(
                target=self._run,
                args=(run_id, stop_event),
                name=f"SimKeysHost-{self.client.pid}",
                daemon=True,
            )
            self.thread.start()

    def start_overlay_controls(self):
        with self.lock:
            self.overlay_controls_enabled = True
            self.overlay_controls_dirty = True
            self._ensure_thread_locked()

    def stop_overlay_controls(self):
        with self.lock:
            self.overlay_controls_enabled = False
            self.overlay_controls_dirty = False
            if not self.scripts:
                self.stop_event.set()
        self._clear_overlay_controls()

    def stop_script(self, script_id: str):
        with self.lock:
            script = self.scripts.pop(script_id, None)
            if script is None:
                return
            script.on_stop()
            self.overlay_controls_dirty = True
            if not self.scripts and not self.overlay_controls_enabled:
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
        if self._chat_is_locked_out():
            raise RuntimeError("chat is locked out because the client is at the password prompt")
        return runtime.send_chat(self.client, f"##{text}", 2)

    def send_chat(self, text: str, mode: int = 2):
        if self._chat_is_locked_out():
            raise RuntimeError("chat is locked out because the client is at the password prompt")
        return runtime.send_chat(self.client, text, mode)

    def show_overlay_text(
        self,
        text: str,
        overlay_id: int = 1000,
        position: str = "TR",
        offset_x: int = 0,
        offset_y: int = 0,
        font_size: int = 16,
        color: int = 0xFFFFFF,
    ):
        return runtime.show_overlay_text(
            self.client,
            text,
            overlay_id=overlay_id,
            position=position,
            offset_x=offset_x,
            offset_y=offset_y,
            font_size=font_size,
            color=color,
        )

    def clear_overlay(self, overlay_id: int = 1000):
        return runtime.clear_overlay(self.client, overlay_id=overlay_id)

    def _render_overlay_controls(self, force: bool = False):
        with self.lock:
            enabled = self.overlay_controls_enabled
            text = _overlay_controls_line(set(self.scripts.keys())) if enabled else ""
            dirty = self.overlay_controls_dirty

        if not enabled:
            if self.last_overlay_controls_text:
                self._clear_overlay_controls()
            return
        if not force and not dirty and text == self.last_overlay_controls_text:
            return

        try:
            result = self.show_overlay_text(
                text,
                overlay_id=OVERLAY_CONTROLS_ID,
                position="TR",
                offset_x=0,
                offset_y=52,
                font_size=14,
                color=0xFFFFFF,
            )
        except Exception as exc:
            self.emit("error", f"{self.client.display_name}: overlay controls update failed: {exc}")
            return

        if result.get("success"):
            self.last_overlay_controls_text = text
            with self.lock:
                self.overlay_controls_dirty = False
        else:
            self.emit("error", f"{self.client.display_name}: overlay controls update failed err={result.get('err')}")

    def _clear_overlay_controls(self):
        try:
            self.clear_overlay(OVERLAY_CONTROLS_ID)
        except Exception as exc:
            self.emit("error", f"{self.client.display_name}: overlay controls clear failed: {exc}")
        self.last_overlay_controls_text = ""

    def _chat_poll_once(self, after: int, max_lines: int):
        pipe = runtime.open_pipe(self.client.pid, timeout_ms=750)
        try:
            return simkeys.chat_poll(pipe, after=after, max_lines=max_lines)
        finally:
            pipe.close()

    def _tick_scripts(self):
        with self.lock:
            current_scripts = list(self.scripts.values())
        for script in current_scripts:
            try:
                script.on_tick()
            except Exception as exc:
                script.set_status(f"Error: {exc}")
                self.emit(
                    "error",
                    f"{self.client.display_name}: {type(exc).__name__}: {exc}",
                    script_id=getattr(script, "script_id", None),
                )
        self._render_overlay_controls()

    def _parse_chat_event(self, sequence: int, text: str) -> ChatLineEvent:
        return parse_chat_line_event(sequence, text, password_prompt_text=self.PASSWORD_PROMPT_TEXT)

    def _record_damage_meter_event(self, event: ChatLineEvent):
        try:
            self.damage_meter_recorder.record_event(event.sequence, event.raw_text, self.client.display_name)
            self.last_damage_meter_error = ""
        except Exception as exc:
            error_text = str(exc)
            if error_text != self.last_damage_meter_error:
                self.last_damage_meter_error = error_text
                self.emit("error", f"{self.client.display_name}: damage meter log write failed: {error_text}")

    def _dispatch_chat_event(self, event: ChatLineEvent):
        with self.lock:
            current_scripts = [
                script
                for script in self.scripts.values()
                if script.needs_chat_feed() and script.wants_chat_event(event)
            ]
        for script in current_scripts:
            line_started_at = time.perf_counter()
            try:
                script.on_chat_event(event)
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
                if line_elapsed > 0.50 and now_perf - self.last_slow_event_log_at > 10.0:
                    self.last_slow_event_log_at = now_perf
                    self.emit(
                        "error",
                        (
                            f"{self.client.display_name}: slow {getattr(script, 'script_id', 'script')} "
                            f"chat event handler took {line_elapsed:.2f}s at seq {event.sequence}"
                        ),
                        script_id=getattr(script, "script_id", None),
                    )

    def _process_chat_event(self, event: ChatLineEvent, dispatch: bool = True) -> bool:
        if event.overlay_script_id:
            self.event_callback({
                "type": "overlay-script-toggle",
                "client_pid": self.client.pid,
                "client_name": self.client.display_name,
                "script_id": event.overlay_script_id,
                "sequence": event.sequence,
            })
            return False
        if event.password_prompt:
            self._stop_for_password_prompt(event.sequence)
            return True
        if dispatch:
            self._dispatch_chat_event(event)
        return False

    def _is_password_prompt(self, text: str) -> bool:
        line = hgx_combat.normalize_chat_line(text).strip().lower()
        return line == self.PASSWORD_PROMPT_TEXT

    def _handle_overlay_event(self, sequence: int, text: str) -> bool:
        value = str(text or "")
        if not value.startswith(OVERLAY_TOGGLE_EVENT_PREFIX):
            return False

        script_id = value[len(OVERLAY_TOGGLE_EVENT_PREFIX):].strip()
        if script_id:
            self.event_callback({
                "type": "overlay-script-toggle",
                "client_pid": self.client.pid,
                "client_name": self.client.display_name,
                "script_id": script_id,
                "sequence": sequence,
            })
        return True

    def _chat_is_locked_out(self) -> bool:
        return time.monotonic() < self.password_chat_blocked_until

    def _stop_for_password_prompt(self, sequence: int):
        with self.lock:
            if self._chat_is_locked_out() and not self.scripts:
                return
            self.password_chat_blocked_until = time.monotonic() + self.PASSWORD_CHAT_BLOCK_SECONDS
            scripts = list(self.scripts.values())
            self.scripts.clear()
            self.stop_event.set()

        self.emit(
            "error",
            (
                f"{self.client.display_name}: password prompt seen at seq {sequence}; "
                f"stopped all scripts and blocked script chat sends for {self.PASSWORD_CHAT_BLOCK_SECONDS:.0f}s"
            ),
        )
        for script in scripts:
            try:
                script.on_stop()
            except Exception as exc:
                self.emit(
                    "error",
                    f"{self.client.display_name}: {type(exc).__name__} while stopping {getattr(script, 'script_id', 'script')}: {exc}",
                    script_id=getattr(script, "script_id", None),
                )
        self.notify_state_changed()

    def _run(self, run_id: int, stop_event: threading.Event):
        after = 0
        initialized = False
        last_poll_error = ""
        last_slow_log_at = 0.0
        last_backlog_log_at = 0.0
        try:
            while not stop_event.is_set():
                with self.lock:
                    scripts = list(self.scripts.values())
                    overlay_controls_enabled = self.overlay_controls_enabled
                    if not scripts and not overlay_controls_enabled:
                        break
                    chat_scripts = [script for script in scripts if script.needs_chat_feed()]

                if chat_scripts:
                    poll_interval = min(
                        self.DAMAGE_METER_POLL_INTERVAL,
                        *(script.get_poll_interval() for script in chat_scripts),
                    )
                    max_lines = max(
                        self.DAMAGE_METER_MAX_LINES,
                        *(script.get_max_lines() for script in chat_scripts),
                    )
                else:
                    poll_interval = min(self.PASSWORD_PROMPT_POLL_INTERVAL, self.DAMAGE_METER_POLL_INTERVAL)
                    max_lines = max(self.PASSWORD_PROMPT_MAX_LINES, self.DAMAGE_METER_MAX_LINES)

                try:
                    request_after = 0 if not initialized else after
                    request_max_lines = 1 if not initialized else max_lines
                    polled = self._chat_poll_once(request_after, request_max_lines)
                except Exception as exc:
                    error_text = str(exc)
                    if error_text != last_poll_error:
                        self.emit("error", f"{self.client.display_name}: chat poll failed: {error_text}")
                        last_poll_error = error_text
                    stop_event.wait(max(poll_interval, 0.25))
                    continue

                if last_poll_error:
                    self.emit("info", f"{self.client.display_name}: chat poll recovered")
                    last_poll_error = ""

                polled_latest = int(polled.get("latest_seq", after) or 0)
                polled_lines = list(polled.get("lines") or [])
                self.latest_sequence = polled_latest
                if not initialized:
                    for line in polled_lines:
                        line_sequence = int(line.get("seq", polled_latest) or polled_latest)
                        event = self._parse_chat_event(line_sequence, line.get("text", ""))
                        if self._process_chat_event(event, dispatch=False):
                            break
                    if stop_event.is_set():
                        break
                    initialized = True
                    after = polled_latest
                    self.emit("info", f"{self.client.display_name}: host connected at seq {after}")
                    self._tick_scripts()
                    stop_event.wait(poll_interval)
                    continue

                if polled_lines:
                    batch_started_at = time.perf_counter()
                    returned_latest = after
                    for line in polled_lines:
                        line_sequence = int(line["seq"])
                        if line_sequence > returned_latest:
                            returned_latest = line_sequence
                        event = self._parse_chat_event(line_sequence, line["text"])
                        self._record_damage_meter_event(event)
                        if self._process_chat_event(event, dispatch=True):
                            break
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
                    self._tick_scripts()
                    continue

                self._tick_scripts()
                stop_event.wait(poll_interval)
        except Exception as exc:
            self.emit("error", f"{self.client.display_name}: host stopped after error: {exc}")
        finally:
            should_notify = False
            with self.lock:
                if run_id == self.run_id:
                    self.scripts.clear()
                    self.thread = None
                    should_notify = True
            self.damage_meter_recorder.close()
            if should_notify:
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
                        AutoAAScript.MODE_SHIFTER_WEAPON_SWAP,
                    ],
                    width=20,
                ),
                ScriptField("current_weapon", "Cur", "choice", WEAPON_CURRENT_UNKNOWN, choices=WEAPON_CURRENT_CHOICES, width=8),
                ScriptField("weapon_slot_1", "W1", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_2", "W2", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_3", "W3", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_4", "W4", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_5", "W5", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("weapon_slot_6", "W6", "choice", WEAPON_SLOT_NONE, choices=WEAPON_BASE_SLOT_CHOICES, width=6),
                ScriptField("shift_slot", "Shift", "choice", WEAPON_SLOT_NONE, choices=WEAPON_SLOT_CHOICES, width=6),
                ScriptField("swap_cooldown_seconds", "Swap", "float", 6.2, minimum=0.1, maximum=20.0, step=0.1, width=6),
                ScriptField("min_swap_gain_percent", "Gain %", "float", 6.0, minimum=0.0, maximum=100.0, step=0.5, width=6),
                ScriptField("shifter_min_swap_gain_percent", "Shift Gain %", "float", 300.0, minimum=0.0, maximum=10000.0, step=10.0, width=7),
                ScriptField("shifter_healing_only", "Heal Only", "bool", False),
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

        always_on = ScriptDefinition(
            script_id="always_on",
            name="Always On",
            description=(
                "Bundle the usual background helpers: Auto Follow cues, Zerial wallet refresh, spellbook fill on rest, "
                "and fog disable on area transitions."
            ),
            fields=[
                ScriptField("cooldown_seconds", "Follow CD", "float", 1.0, minimum=0.1, maximum=30.0, step=0.1, width=6),
                ScriptField("disable_follow", "Disable Follow", "bool", False),
                ScriptField("disable_wallet", "Disable Wallet", "bool", False),
                ScriptField("disable_spellbook_fill", "Disable SB Fill", "bool", False),
                ScriptField("disable_fog_off", "Disable Fog Off", "bool", False),
                ScriptField("follow_cues_dir", "Follow Cues", "text", "", width=36),
                ScriptField("poll_interval", "Poll", "float", 0.05, minimum=0.01, maximum=2.0, step=0.01, width=6),
                ScriptField("max_lines", "Batch", "int", 200, minimum=1, maximum=500, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", False),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AlwaysOnScript,
        )
        self.registry[always_on.script_id] = always_on

        auto_rsm = ScriptDefinition(
            script_id="auto_rsm",
            name="Auto Combat Mode",
            description=(
                "Keep one selected combat mode active while attacking. Rapid Shot uses the RSM memory byte; "
                "the other modes read active mode prefixes from combat log attack lines."
            ),
            fields=[
                ScriptField(
                    "mode",
                    "Mode",
                    "choice",
                    AutoCombatModeScript.MODE_RAPID_SHOT,
                    choices=list(AutoCombatModeScript.MODE_CHOICES),
                    width=20,
                ),
                ScriptField("cooldown_seconds", "Retry CD", "float", 6.0, minimum=0.1, maximum=30.0, step=0.1, width=6),
                ScriptField("poll_interval", "Poll", "float", 0.10, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 60, minimum=1, maximum=200, step=1, width=5),
                ScriptField("echo_console", "Echo", "bool", False),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=AutoCombatModeScript,
        )
        self.registry[auto_rsm.script_id] = auto_rsm

        default_spell_timers = _format_spell_timer_config(_load_hgx_spell_timer_specs(_default_status_rules_dir()))
        ingame_timers = ScriptDefinition(
            script_id="ingame_timers",
            name="In-Game Timers",
            description="Display HGX-style status timers and self-cast spell timers inside the NWN client.",
            fields=[
                ScriptField("position", "Pos", "choice", "TR", choices=["TL", "T", "TR", "CL", "C", "CR", "BL", "B", "BR", "A"], width=5),
                ScriptField("offset_x", "X", "int", 0, minimum=-2000, maximum=2000, step=1, width=6),
                ScriptField("offset_y", "Y", "int", 0, minimum=-2000, maximum=2000, step=1, width=6),
                ScriptField("font_size", "Font", "int", 16, minimum=8, maximum=72, step=1, width=5),
                ScriptField("color", "Color", "choice", "White", choices=["White", "Green", "Yellow", "Red", "Cyan", "Blue", "Orange"], width=8),
                ScriptField("max_timers", "Max", "int", 8, minimum=1, maximum=32, step=1, width=5),
                ScriptField("spell_timers", "Spells", "text", default_spell_timers, width=72),
                ScriptField("enable_limbo", "Limbo", "bool", True),
                ScriptField("limbo_duration_seconds", "Duration", "float", 300.0, minimum=1.0, maximum=1800.0, step=1.0, width=6),
                ScriptField("limbo_names", "Always Party", "text", "", width=72),
                ScriptField("rules_dir", "Rules", "text", "", width=36),
                ScriptField("poll_interval", "Poll", "float", 0.20, minimum=0.05, maximum=2.0, step=0.05, width=6),
                ScriptField("max_lines", "Batch", "int", 80, minimum=1, maximum=500, step=1, width=5),
                ScriptField("include_backlog", "Backlog", "bool", False),
            ],
            factory=InGameTimersScript,
        )
        self.registry[ingame_timers.script_id] = ingame_timers

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
        if host.running_script_ids() or host.overlay_controls_enabled:
            return
        self.hosts.pop(client_pid, None)

    def enable_overlay_controls(self, client):
        host = self._get_or_create_host(client)
        host.start_overlay_controls()

    def disable_overlay_controls(self, client_pid: int):
        host = self.hosts.get(client_pid)
        if host is None:
            return
        host.stop_overlay_controls()
        if not host.running_script_ids():
            self.hosts.pop(client_pid, None)

    def stop_all_for_client(self, client_pid: int):
        host = self.hosts.get(client_pid)
        if host is None:
            return
        for script_id in host.running_script_ids():
            host.stop_script(script_id)
        host.stop_overlay_controls()
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
