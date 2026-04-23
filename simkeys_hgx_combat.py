import re
from dataclasses import dataclass
from typing import Optional, Tuple

from simkeys_hgx_data import AA_WORD_TO_TYPE, DAMAGE_TYPE_NAME_TO_ID, GI_WORD_TO_TYPE


CHAT_WINDOW_PREFIX_RE = re.compile(r"^\[CHAT WINDOW TEXT\]\s*", re.IGNORECASE)
SERVER_PREFIX_RE = re.compile(r"^\[Server\]\s*", re.IGNORECASE)
TIMESTAMP_PREFIX_RE = re.compile(r"^\[[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*")
INLINE_MARKUP_RE = re.compile(r"</?c[^>\r\n]{0,128}>|<[^>\r\n]{0,128}>", re.IGNORECASE)
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")
WHITESPACE_RE = re.compile(r"\s+")
ATTACK_LINE_RE = re.compile(r"^(?:(?P<attack_mode>[^:]+?)\s*:\s*)?(?P<attacker>.+?) attacks (?P<defender>.+?)\s*:\s*", re.IGNORECASE)
DAMAGE_LINE_RE = re.compile(
    r"^(?P<attacker>.+?) damages (?P<defender>.+?)\s*:\s*(?P<total>-?\d+)\s*\((?P<breakdown>.+)\)\s*$",
    re.IGNORECASE,
)
BOW_SET_RE = re.compile(r"Bow set to (?P<word>[A-Za-z]+) damage!", re.IGNORECASE)
DIVINE_BULLETS_SET_RE = re.compile(r"Divine Bullets set to (?P<word>[A-Za-z]+) damage!", re.IGNORECASE)
GI_BOLT_SET_RE = re.compile(r"You are now using (?P<word>[A-Za-z]+)!?", re.IGNORECASE)
BREACH_LINE_RE = re.compile(r"^(?P<target>.+?)\s*:\s*Breach\s+(?P<effect>.+)$", re.IGNORECASE)
TARGET_BLIND_RE = re.compile(r"\(Target Blind\)", re.IGNORECASE)
EQUIPPED_ITEM_SWAPPED_RE = re.compile(r"^Equipped item swapped out\.\s*$", re.IGNORECASE)
WEAPON_EQUIPPED_RE = re.compile(r"^Weapon equipped as .* weapon\.\s*$", re.IGNORECASE)

COMBAT_LOG_DAMAGE_ALIASES = {
    "physical": (None, "Physical"),
    "bludgeoning": ("bludgeoning", "Bludgeoning"),
    "piercing": ("piercing", "Piercing"),
    "slashing": ("slashing", "Slashing"),
    "acid": ("acid", "Acid"),
    "cold": ("cold", "Cold"),
    "electric": ("electrical", "Electrical"),
    "electrical": ("electrical", "Electrical"),
    "fire": ("fire", "Fire"),
    "sonic": ("sonic", "Sonic"),
    "divine": ("divine", "Divine"),
    "magic": ("magical", "Magical"),
    "magical": ("magical", "Magical"),
    "negative": ("negative", "Negative"),
    "negative energy": ("negative", "Negative Energy"),
    "positive": ("positive", "Positive"),
    "positive energy": ("positive", "Positive Energy"),
    "ectoplasmic": ("ectoplasmic", "Ectoplasmic"),
    "internal": ("internal", "Internal"),
    "psionic": ("psionic", "Psionic"),
    "sacred": ("sacred", "Sacred"),
    "vile": ("vile", "Vile"),
    "anarchic": ("anarchic", "Anarchic"),
    "axiomatic": ("axiomatic", "Axiomatic"),
    "primal": ("primal", "Primal"),
    "subdual": ("subdual", "Subdual"),
    "force": ("force", "Force"),
    "desiccation": ("desiccation", "Desiccation"),
    "venom": ("venom", "Venom"),
    "raw arcane": ("rawarcane", "Raw Arcane"),
    "raw divine": ("rawdivine", "Raw Divine"),
    "raw nature": ("rawnature", "Raw Nature"),
    "dragonfire": ("dragonfire", "Dragonfire"),
    "blight": ("blight", "Blight"),
    "deception": ("deception", "Deception"),
    "degeneration": ("degeneration", "Degeneration"),
    "digestion": ("digestion", "Digestion"),
    "retribution": ("retribution", "Retribution"),
    "antimagic": ("antimagic", "Antimagic"),
}

_COMPONENT_NAME_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(COMBAT_LOG_DAMAGE_ALIASES.keys(), key=len, reverse=True)
)
DAMAGE_COMPONENT_RE = re.compile(
    rf"(?P<amount>-?\d+)\s+(?P<type>{_COMPONENT_NAME_PATTERN})(?=(?:\s+-?\d+\s+(?:{_COMPONENT_NAME_PATTERN}))|\s*$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedAttackLine:
    raw_text: str
    normalized_text: str
    attack_mode: str
    attacker: str
    defender: str


@dataclass(frozen=True)
class ParsedDamageComponent:
    amount: int
    type_name: str
    damage_type: Optional[int]


@dataclass(frozen=True)
class ParsedDamageLine:
    raw_text: str
    normalized_text: str
    attacker: str
    defender: str
    total: int
    components: Tuple[ParsedDamageComponent, ...]


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
    value = strip_inline_markup(value)
    return value.strip()


def strip_inline_markup(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""

    # HGX/NWN chat often wraps speaker and combat names in color tags. The
    # encoded color bytes can render as replacement characters, so strip any
    # short inline angle-bracket tag before actor-name comparisons.
    value = INLINE_MARKUP_RE.sub("", value)
    value = CONTROL_CHAR_RE.sub("", value)
    return value.replace("\ufffd", "")


def normalize_actor_name(text: str) -> str:
    value = strip_inline_markup(text)
    value = WHITESPACE_RE.sub(" ", value)
    return value.strip()


def parse_attack_line(text: str) -> Optional[ParsedAttackLine]:
    normalized = normalize_chat_line(text)
    if " attacks " not in normalized.lower():
        return None

    match = ATTACK_LINE_RE.search(normalized)
    if match is None:
        return None

    attacker = normalize_actor_name(match.group("attacker"))
    defender = normalize_actor_name(match.group("defender"))
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


def parse_damage_line(text: str) -> Optional[ParsedDamageLine]:
    normalized = normalize_chat_line(text)
    if " damages " not in normalized.lower() or "(" not in normalized or ")" not in normalized:
        return None

    match = DAMAGE_LINE_RE.search(normalized)
    if match is None:
        return None

    attacker = normalize_actor_name(match.group("attacker"))
    defender = normalize_actor_name(match.group("defender"))
    if not attacker or not defender:
        return None

    breakdown = str(match.group("breakdown") or "").strip()
    components = []
    position = 0
    for component_match in DAMAGE_COMPONENT_RE.finditer(breakdown):
        if breakdown[position:component_match.start()].strip():
            return None

        alias = str(component_match.group("type") or "").strip().lower()
        canonical_name, display_name = COMBAT_LOG_DAMAGE_ALIASES.get(alias, (None, ""))
        damage_type = DAMAGE_TYPE_NAME_TO_ID.get(canonical_name) if canonical_name else None
        components.append(
            ParsedDamageComponent(
                amount=int(component_match.group("amount") or "0"),
                type_name=display_name or alias.title(),
                damage_type=damage_type,
            )
        )
        position = component_match.end()

    if not components or breakdown[position:].strip():
        return None

    return ParsedDamageLine(
        raw_text=str(text or ""),
        normalized_text=normalized,
        attacker=attacker,
        defender=defender,
        total=int(match.group("total") or "0"),
        components=tuple(components),
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

    target = normalize_actor_name(match.group("target"))
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


def parse_weapon_swap_feedback(text: str) -> str:
    normalized = normalize_chat_line(text)
    if WEAPON_EQUIPPED_RE.search(normalized):
        return "weapon_equipped"
    if EQUIPPED_ITEM_SWAPPED_RE.search(normalized):
        return "item_swapped"
    return ""
