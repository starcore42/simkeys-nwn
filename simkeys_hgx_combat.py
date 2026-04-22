import re
from dataclasses import dataclass
from typing import Optional

from simkeys_hgx_data import AA_WORD_TO_TYPE, GI_WORD_TO_TYPE


CHAT_WINDOW_PREFIX_RE = re.compile(r"^\[CHAT WINDOW TEXT\]\s*", re.IGNORECASE)
SERVER_PREFIX_RE = re.compile(r"^\[Server\]\s*", re.IGNORECASE)
TIMESTAMP_PREFIX_RE = re.compile(r"^\[[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*")
ATTACK_LINE_RE = re.compile(r"^(?:(?P<attack_mode>[^:]+?)\s*:\s*)?(?P<attacker>.+?) attacks (?P<defender>.+?)\s*:\s*", re.IGNORECASE)
PLAYER_LEVEL_SUFFIX_RE = re.compile(r"\s+\[\d+(?:\.\d+)?\]$")
BOW_SET_RE = re.compile(r"Bow set to (?P<word>[A-Za-z]+) damage!", re.IGNORECASE)
DIVINE_BULLETS_SET_RE = re.compile(r"Divine Bullets set to (?P<word>[A-Za-z]+) damage!", re.IGNORECASE)
GI_BOLT_SET_RE = re.compile(r"You are now using (?P<word>[A-Za-z]+)!?", re.IGNORECASE)
BREACH_LINE_RE = re.compile(r"^(?P<target>.+?)\s*:\s*Breach\s+(?P<effect>.+)$", re.IGNORECASE)
TARGET_BLIND_RE = re.compile(r"\(Target Blind\)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedAttackLine:
    raw_text: str
    normalized_text: str
    attack_mode: str
    attacker: str
    defender: str


@dataclass(frozen=True)
class ParsedBreachLine:
    raw_text: str
    normalized_text: str
    target: str
    effect: str


def normalize_chat_line(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    value = CHAT_WINDOW_PREFIX_RE.sub("", value)
    value = TIMESTAMP_PREFIX_RE.sub("", value)
    value = SERVER_PREFIX_RE.sub("", value)
    return value.strip()


def strip_player_level_suffix(name: str) -> str:
    return PLAYER_LEVEL_SUFFIX_RE.sub("", str(name or "").strip()).strip()


def parse_attack_line(text: str) -> Optional[ParsedAttackLine]:
    normalized = normalize_chat_line(text)
    if " attacks " not in normalized.lower():
        return None

    match = ATTACK_LINE_RE.search(normalized)
    if match is None:
        return None

    attacker = strip_player_level_suffix(match.group("attacker"))
    defender = strip_player_level_suffix(match.group("defender"))
    if not attacker or not defender:
        return None

    attack_mode = (match.group("attack_mode") or "").strip()
    return ParsedAttackLine(
        raw_text=str(text or ""),
        normalized_text=normalized,
        attack_mode=attack_mode,
        attacker=attacker,
        defender=defender,
    )


def parse_damage_feedback_type(text: str) -> Optional[int]:
    normalized = normalize_chat_line(text)
    for pattern in (BOW_SET_RE, DIVINE_BULLETS_SET_RE):
        match = pattern.search(normalized)
        if match is None:
            continue
        word = (match.group("word") or "").strip().lower()
        return AA_WORD_TO_TYPE.get(word)
    return None


def parse_gi_feedback_type(text: str) -> Optional[int]:
    normalized = normalize_chat_line(text)
    match = GI_BOLT_SET_RE.search(normalized)
    if match is None:
        return None

    word = (match.group("word") or "").strip().lower().rstrip("!.,")
    return GI_WORD_TO_TYPE.get(word)


def parse_breach_line(text: str) -> Optional[ParsedBreachLine]:
    normalized = normalize_chat_line(text)
    match = BREACH_LINE_RE.search(normalized)
    if match is None:
        return None

    target = strip_player_level_suffix(match.group("target"))
    effect = str(match.group("effect") or "").strip()
    if not target or not effect:
        return None

    return ParsedBreachLine(
        raw_text=str(text or ""),
        normalized_text=normalized,
        target=target,
        effect=effect,
    )


def has_target_blind_marker(text: str) -> bool:
    normalized = normalize_chat_line(text)
    return TARGET_BLIND_RE.search(normalized) is not None
