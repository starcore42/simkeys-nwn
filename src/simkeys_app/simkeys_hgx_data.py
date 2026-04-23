import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from xml.etree import ElementTree as ET


DAMAGE_TYPE_NAME_TO_ID = {
    "bludgeoning": 0,
    "piercing": 1,
    "slashing": 2,
    "acid": 3,
    "cold": 4,
    "electrical": 5,
    "fire": 6,
    "sonic": 7,
    "divine": 8,
    "magical": 9,
    "negative": 10,
    "positive": 11,
    "ectoplasmic": 12,
    "internal": 13,
    "psionic": 14,
    "sacred": 15,
    "vile": 16,
    "anarchic": 17,
    "axiomatic": 18,
    "primal": 19,
    "subdual": 20,
    "force": 21,
    "desiccation": 22,
    "venom": 23,
    "rawarcane": 24,
    "rawdivine": 25,
    "rawnature": 26,
    "dragonfire": 27,
    "blight": 28,
    "deception": 29,
    "degeneration": 30,
    "digestion": 31,
    "retribution": 32,
    "antimagic": 33,
}

AA_COMMAND_BY_TYPE = {
    3: "!damac",
    4: "!damco",
    5: "!damel",
    6: "!damfi",
    7: "!damso",
    8: "!damdi",
    9: "!damma",
    10: "!damne",
    11: "!dampo",
    12: "!dambr",
}

SLINGER_COMMAND_BY_TYPE = {
    3: "!damac",
    4: "!damco",
    5: "!damel",
    6: "!damfi",
    7: "!damso",
    8: "!damdi",
}

SLINGER_BREACH_COMMAND = "!dambr"
SLINGER_BLIND_COMMAND = "!dambd"
SLINGER_HEAL_COMMAND = "!damhe"

GI_COMMAND_BY_TYPE = {
    1: "!gi bolt 1",
    5: "!gi bolt 2",
    7: "!gi bolt 3",
    9: "!gi bolt 4",
}

AA_WORD_TO_TYPE = {
    "acid": 3,
    "cold": 4,
    "electrical": 5,
    "fire": 6,
    "sonic": 7,
    "divine": 8,
    "magical": 9,
    "negative": 10,
    "positive": 11,
    "breach": 12,
}

AA_TYPE_TO_WORD = {value: key for key, value in AA_WORD_TO_TYPE.items()}
GI_WORD_TO_TYPE = {
    "shrappers": 1,
    "arcers": 5,
    "boomers": 7,
    "zappers": 9,
}
GI_TYPE_TO_WORD = {value: key for key, value in GI_WORD_TO_TYPE.items()}
ZEN_DAMAGE_LINKS = {
    3: 9,
    4: 11,
    5: 8,
    6: 10,
    7: 8,
}

SLINGER_BREACH_TARGETS = frozenset(
    name.lower()
    for name in (
        "Combat Dummy",
        "Bandit",
        "Rakshasa",
        "Raja",
        "Greater Raja",
        "Superior Raja",
        "Elite Raja",
        "Wastrilith",
        "Greater Wastrilith",
        "Superior Wastrilith",
        "Elite Wastrilith",
        "Artaaglith",
        "Greater Artaaglith",
        "Superior Artaaglith",
        "Elite Artaaglith",
        "Ekolid Builder",
        "Greater Ekolid Builder",
        "Superior Ekolid Builder",
        "Elite Ekolid Builder",
        "Ekolid Chitterer",
        "Greater Ekolid Chitterer",
        "Superior Ekolid Chitterer",
        "Elite Ekolid Chitterer",
        "Sibriex",
        "Varrangoin Arcanist",
        "Greater Varrangoin Arcanist",
        "Superior Varrangoin Arcanist",
        "Elite Varrangoin Arcanist",
        "Tiefling Sorceress",
        "Greater Tiefling Sorceress",
        "Superior Tiefling Sorceress",
        "Elite Tiefling Sorceress",
        "Dustman Necromancer",
        "Greater Dustman Necromancer",
        "Superior Dustman Necromancer",
        "Elite Dustman Necromancer",
        "Bonesinger",
        "Greater Bonesinger",
        "Superior Bonesinger",
        "Elite Bonesinger",
        "Feral Elf Stormwarden",
        "Greater Feral Elf Stormwarden",
        "Superior Feral Elf Stormwarden",
        "Elite Feral Elf Stormwarden",
        "Githzerai Chaosweaver",
        "Greater Githzerai Chaosweaver",
        "Superior Githzerai Chaosweaver",
        "Elite Githzerai Chaosweaver",
        "Green Slaad",
        "Greater Green Slaad",
        "Superior Green Slaad",
        "Elite Green Slaad",
        "Grey Slaad",
        "Greater Grey Slaad",
        "Superior Grey Slaad",
        "Elite Grey Slaad",
        "Lillend",
        "Greater Lillend",
        "Superior Lillend",
        "Elite Lillend",
        "Yuan-Ti Sorceress",
        "Greater Yuan-Ti Sorceress",
        "Superior Yuan-Ti Sorceress",
        "Elite Yuan-Ti Sorceress",
    )
)

SLINGER_BLIND_TARGETS = frozenset(
    name.lower()
    for name in (
        "Combat Dummy",
        "Bandit",
        "Rakshasa",
        "Raja",
        "Greater Raja",
        "Superior Raja",
        "Elite Raja",
        "Ice Fiend",
        "Hyperborian Fiend",
        "Greater Hyperborian Fiend",
        "Superior Hyperborian Fiend",
        "Elite Hyperborian Fiend",
        "Grey Slaad",
        "Greater Grey Slaad",
        "Superior Grey Slaad",
        "Elite Grey Slaad",
        "Green Slaad",
        "Greater Green Slaad",
        "Superior Green Slaad",
        "Elite Green Slaad",
    )
)

CHARACTER_TYPE_NAME_TO_VALUE = {
    "ignore": -1,
    "normal": 0,
    "greater": 1,
    "superior": 2,
    "elite": 3,
    "p4": 4,
    "p5": 5,
    "miniboss": 6,
    "boss": 7,
}

HGX_DAMAGE_TYPE_COUNT = 34


@dataclass(frozen=True)
class CreatureRecord:
    name: str
    base_name: str
    character_type: int
    direct_immunity: Tuple[Optional[int], ...]
    direct_resistance: Tuple[Optional[int], ...]
    direct_healing: Tuple[Optional[int], ...]


@dataclass(frozen=True)
class EffectiveCreatureStats:
    immunity: Tuple[int, ...]
    resistance: Tuple[int, ...]
    healing: Tuple[int, ...]


@dataclass(frozen=True)
class DamageRecommendation:
    requested_name: str
    matched_name: str
    mode: str
    damage_type: int
    selection_name: str
    command: str
    expected_damage: int
    paragon_ranks: int


@dataclass(frozen=True)
class CombatProfile:
    matched_name: str
    immunity: Tuple[int, ...]
    resistance: Tuple[int, ...]
    healing: Tuple[int, ...]
    paragon_ranks: int


@dataclass(frozen=True)
class DamageComponentEstimate:
    requested_name: str
    matched_name: str
    expected_damage: int
    paragon_ranks: int
    healing_types: Tuple[int, ...]


def project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.abspath(os.path.join(here, os.pardir, os.pardir)),
        os.path.abspath(os.path.join(here, os.pardir)),
    )
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "README.md")):
            return candidate
    return candidates[0]


def default_character_data_dir() -> str:
    return os.path.join(project_root(), "data", "characters.d")


def _normalize_damage_type_name(text: str) -> str:
    return "".join(str(text or "").strip().lower().split())


def _parse_optional_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    value = str(text).strip()
    if value == "":
        return None
    return int(value)


def _make_empty_optional_stats() -> list:
    return [None] * HGX_DAMAGE_TYPE_COUNT


class HgxCharacterDatabase:
    def __init__(self, source_dir: str, records: Dict[str, CreatureRecord]):
        self.source_dir = os.path.abspath(source_dir)
        self.records = dict(records)
        self._records_lower = {name.lower(): record for name, record in self.records.items()}
        self._effective_cache: Dict[str, EffectiveCreatureStats] = {}
        self._recommendation_cache: Dict[Tuple[str, str, int], Optional[DamageRecommendation]] = {}
        self._lock = threading.RLock()

    def lookup(self, creature_name: str) -> Optional[CreatureRecord]:
        if not creature_name:
            return None
        return self.records.get(creature_name) or self._records_lower.get(creature_name.lower())

    def effective_stats(self, creature_or_name) -> Optional[EffectiveCreatureStats]:
        if isinstance(creature_or_name, CreatureRecord):
            record = creature_or_name
        else:
            record = self.lookup(str(creature_or_name))
        if record is None:
            return None

        cache_key = record.name.lower()
        with self._lock:
            cached = self._effective_cache.get(cache_key)
            if cached is not None:
                return cached

        stack = set()
        resolved = self._resolve_effective_stats(record, stack)
        with self._lock:
            self._effective_cache[cache_key] = resolved
        return resolved

    def _resolve_effective_stats(self, record: CreatureRecord, stack: set) -> EffectiveCreatureStats:
        cache_key = record.name.lower()
        cached = self._effective_cache.get(cache_key)
        if cached is not None:
            return cached

        if cache_key in stack:
            return EffectiveCreatureStats(
                immunity=tuple([0] * HGX_DAMAGE_TYPE_COUNT),
                resistance=tuple([0] * HGX_DAMAGE_TYPE_COUNT),
                healing=tuple([0] * HGX_DAMAGE_TYPE_COUNT),
            )

        stack.add(cache_key)
        if record.base_name:
            base_record = self.lookup(record.base_name)
            base_stats = self._resolve_effective_stats(base_record, stack) if base_record is not None else None
        else:
            base_stats = None

        immunity = list(base_stats.immunity) if base_stats is not None else [0] * HGX_DAMAGE_TYPE_COUNT
        resistance = list(base_stats.resistance) if base_stats is not None else [0] * HGX_DAMAGE_TYPE_COUNT
        healing = list(base_stats.healing) if base_stats is not None else [0] * HGX_DAMAGE_TYPE_COUNT

        for index, value in enumerate(record.direct_immunity):
            if value is not None:
                immunity[index] = value
        for index, value in enumerate(record.direct_resistance):
            if value is not None:
                resistance[index] = value
        for index, value in enumerate(record.direct_healing):
            if value is not None:
                healing[index] = value

        stack.remove(cache_key)
        resolved = EffectiveCreatureStats(
            immunity=tuple(immunity),
            resistance=tuple(resistance),
            healing=tuple(healing),
        )
        self._effective_cache[cache_key] = resolved
        return resolved

    def recommend_arcane_archer_damage(self, creature_name: str, elemental_dice: int) -> Optional[DamageRecommendation]:
        if not creature_name:
            return None

        elemental_dice = max(int(elemental_dice), 0)
        cache_key = ("arcane_archer", creature_name.lower(), elemental_dice)
        cached_found, cached = self._get_cached_recommendation(cache_key)
        if cached_found:
            return cached

        profile = self._resolve_combat_profile(creature_name)
        if profile is None:
            self._store_cached_recommendation(cache_key, None)
            return None

        candidate_types = [3, 4, 5, 6, 7, 8, 9, 10, 11]
        candidate_types = [damage_type for damage_type in candidate_types if profile.healing[damage_type] == 0]

        if profile.healing[9] != 0 or profile.healing[10] != 0:
            candidate_types = [damage_type for damage_type in candidate_types if damage_type >= 8]

        if not candidate_types:
            self._store_cached_recommendation(cache_key, None)
            return None

        shared_magic = self._apply_immunity_and_resistance(13.0, profile.immunity[9], profile.resistance[9], profile.paragon_ranks)
        shared_negative = self._apply_immunity_and_resistance(13.0, profile.immunity[10], profile.resistance[10], profile.paragon_ranks)

        best_type = candidate_types[0]
        best_damage = -1
        for damage_type in candidate_types:
            if damage_type < 8:
                base_damage = 21.0 * float(elemental_dice) / 2.0
                expected_damage = self._apply_immunity_and_resistance(
                    base_damage,
                    profile.immunity[damage_type],
                    profile.resistance[damage_type],
                    profile.paragon_ranks,
                )
                expected_damage += shared_magic + shared_negative
            else:
                base_damage = 21.0 * float(max(elemental_dice - 2, 0)) / 2.0
                expected_damage = self._apply_immunity_and_resistance(
                    base_damage,
                    profile.immunity[damage_type],
                    profile.resistance[damage_type],
                    profile.paragon_ranks,
                )

            if expected_damage > best_damage:
                best_type = damage_type
                best_damage = expected_damage

        recommendation = DamageRecommendation(
            requested_name=creature_name,
            matched_name=profile.matched_name,
            mode="Arcane Archer",
            damage_type=best_type,
            selection_name=AA_TYPE_TO_WORD[best_type],
            command=AA_COMMAND_BY_TYPE[best_type],
            expected_damage=best_damage,
            paragon_ranks=profile.paragon_ranks,
        )
        self._store_cached_recommendation(cache_key, recommendation)
        return recommendation

    def recommend_zen_ranger_damage(self, creature_name: str, elemental_dice: int) -> Optional[DamageRecommendation]:
        if not creature_name:
            return None

        elemental_dice = max(int(elemental_dice), 0)
        cache_key = ("zen_ranger", creature_name.lower(), elemental_dice)
        cached_found, cached = self._get_cached_recommendation(cache_key)
        if cached_found:
            return cached

        profile = self._resolve_combat_profile(creature_name)
        if profile is None:
            self._store_cached_recommendation(cache_key, None)
            return None

        candidate_types = [3, 4, 5, 6, 7, 8]
        filtered_types = []
        for damage_type in candidate_types:
            if damage_type == 8:
                if profile.healing[8] == 0:
                    filtered_types.append(damage_type)
                continue
            if profile.healing[damage_type] != 0:
                continue
            if profile.healing[ZEN_DAMAGE_LINKS[damage_type]] != 0:
                continue
            filtered_types.append(damage_type)
        candidate_types = filtered_types

        if not candidate_types:
            self._store_cached_recommendation(cache_key, None)
            return None

        best_type = candidate_types[0]
        best_damage = -1
        for damage_type in candidate_types:
            if damage_type in ZEN_DAMAGE_LINKS:
                exo_type = ZEN_DAMAGE_LINKS[damage_type]
                exo_damage = self._apply_immunity_and_resistance(
                    13.0,
                    profile.immunity[exo_type],
                    profile.resistance[exo_type],
                    profile.paragon_ranks,
                )
                base_damage = 13.0 * float(elemental_dice) / 2.0
                direct_damage = self._apply_immunity_and_resistance(
                    base_damage,
                    profile.immunity[damage_type],
                    profile.resistance[damage_type],
                    profile.paragon_ranks,
                )
                expected_damage = exo_damage + direct_damage
            else:
                base_damage = 13.0 * float(max(elemental_dice - 2, 0)) / 2.0
                expected_damage = self._apply_immunity_and_resistance(
                    base_damage,
                    profile.immunity[damage_type],
                    profile.resistance[damage_type],
                    profile.paragon_ranks,
                )

            if expected_damage > best_damage:
                best_type = damage_type
                best_damage = expected_damage

        recommendation = DamageRecommendation(
            requested_name=creature_name,
            matched_name=profile.matched_name,
            mode="Zen Ranger",
            damage_type=best_type,
            selection_name=AA_TYPE_TO_WORD[best_type],
            command=AA_COMMAND_BY_TYPE[best_type],
            expected_damage=best_damage,
            paragon_ranks=profile.paragon_ranks,
        )
        self._store_cached_recommendation(cache_key, recommendation)
        return recommendation

    def recommend_gnomish_inventor_damage(self, creature_name: str, damage_dice: int) -> Optional[DamageRecommendation]:
        if not creature_name:
            return None

        damage_dice = max(int(damage_dice), 0)
        cache_key = ("gnomish_inventor", creature_name.lower(), damage_dice)
        cached_found, cached = self._get_cached_recommendation(cache_key)
        if cached_found:
            return cached

        profile = self._resolve_combat_profile(creature_name)
        if profile is None:
            self._store_cached_recommendation(cache_key, None)
            return None

        candidate_types = [1, 5, 7, 9]
        candidate_types = [damage_type for damage_type in candidate_types if profile.healing[damage_type] == 0]
        if not candidate_types:
            self._store_cached_recommendation(cache_key, None)
            return None

        best_type = candidate_types[0]
        best_damage = -1
        for damage_type in candidate_types:
            if damage_type == 1:
                base_damage = 13.0 * float(max(damage_dice - 5, 0)) / 2.0
            else:
                base_damage = 13.0 * float(damage_dice) / 2.0
            expected_damage = self._apply_immunity_and_resistance(
                base_damage,
                profile.immunity[damage_type],
                profile.resistance[damage_type],
                profile.paragon_ranks,
            )
            if expected_damage > best_damage:
                best_type = damage_type
                best_damage = expected_damage

        recommendation = DamageRecommendation(
            requested_name=creature_name,
            matched_name=profile.matched_name,
            mode="Gnomish Inventor",
            damage_type=best_type,
            selection_name=GI_TYPE_TO_WORD[best_type].title(),
            command=GI_COMMAND_BY_TYPE[best_type],
            expected_damage=best_damage,
            paragon_ranks=profile.paragon_ranks,
        )
        self._store_cached_recommendation(cache_key, recommendation)
        return recommendation

    def recommend_divine_slinger_damage(self, creature_name: str, damage_dice: int) -> Optional[DamageRecommendation]:
        if not creature_name:
            return None

        damage_dice = max(int(damage_dice), 0)
        cache_key = ("divine_slinger", creature_name.lower(), damage_dice)
        cached_found, cached = self._get_cached_recommendation(cache_key)
        if cached_found:
            return cached

        profile = self._resolve_combat_profile(creature_name)
        if profile is None:
            self._store_cached_recommendation(cache_key, None)
            return None

        candidate_types = [3, 4, 5, 6, 7, 8]
        candidate_types = [damage_type for damage_type in candidate_types if profile.healing[damage_type] == 0]
        if not candidate_types:
            self._store_cached_recommendation(cache_key, None)
            return None

        best_type = candidate_types[0]
        best_damage = -1
        for damage_type in candidate_types:
            if damage_type == 8:
                base_damage = 13.0 * float(max(damage_dice - 2, 0)) / 2.0
            else:
                base_damage = 13.0 * float(damage_dice) / 2.0

            expected_damage = self._apply_immunity_and_resistance(
                base_damage,
                profile.immunity[damage_type],
                profile.resistance[damage_type],
                profile.paragon_ranks,
            )
            if expected_damage > best_damage:
                best_type = damage_type
                best_damage = expected_damage

        recommendation = DamageRecommendation(
            requested_name=creature_name,
            matched_name=profile.matched_name,
            mode="Divine Slinger",
            damage_type=best_type,
            selection_name=AA_TYPE_TO_WORD[best_type],
            command=SLINGER_COMMAND_BY_TYPE[best_type],
            expected_damage=best_damage,
            paragon_ranks=profile.paragon_ranks,
        )
        self._store_cached_recommendation(cache_key, recommendation)
        return recommendation

    def estimate_custom_damage(self, creature_name: str, components: Dict[int, float]) -> Optional[DamageComponentEstimate]:
        if not creature_name or not components:
            return None

        profile = self._resolve_combat_profile(creature_name)
        if profile is None:
            return None

        expected_damage = 0
        healing_types = []
        for damage_type, base_damage in sorted(components.items()):
            if not isinstance(damage_type, int):
                continue
            if damage_type < 0 or damage_type >= HGX_DAMAGE_TYPE_COUNT:
                continue

            base_damage = float(base_damage)
            if base_damage <= 0.0:
                continue

            if profile.healing[damage_type] != 0:
                healing_types.append(damage_type)
                continue

            expected_damage += self._apply_immunity_and_resistance(
                base_damage,
                profile.immunity[damage_type],
                profile.resistance[damage_type],
                profile.paragon_ranks,
            )

        return DamageComponentEstimate(
            requested_name=creature_name,
            matched_name=profile.matched_name,
            expected_damage=expected_damage,
            paragon_ranks=profile.paragon_ranks,
            healing_types=tuple(sorted(set(healing_types))),
        )

    def _resolve_combat_profile(self, creature_name: str) -> Optional[CombatProfile]:
        record = self.lookup(creature_name)
        if record is None:
            return None

        working_record = record
        immunity_stats = self.effective_stats(record)
        paragon_ranks = 0
        if record.base_name:
            base_record = self.lookup(record.base_name)
            if base_record is not None:
                immunity_stats = self.effective_stats(base_record)

            if 0 < record.character_type < 6:
                paragon_ranks = record.character_type
            elif record.character_type == 6:
                paragon_ranks = 2
                superior_record = self.lookup(f"Superior {record.base_name}")
                if superior_record is not None:
                    working_record = superior_record

        working_stats = self.effective_stats(working_record)
        if immunity_stats is None or working_stats is None:
            return None

        return CombatProfile(
            matched_name=record.name,
            immunity=immunity_stats.immunity,
            resistance=working_stats.resistance,
            healing=working_stats.healing,
            paragon_ranks=paragon_ranks,
        )

    def _get_cached_recommendation(self, cache_key: Tuple[str, str, int]):
        with self._lock:
            if cache_key in self._recommendation_cache:
                return True, self._recommendation_cache.get(cache_key)
            return False, None

    def _store_cached_recommendation(self, cache_key: Tuple[str, str, int], recommendation: Optional[DamageRecommendation]):
        with self._lock:
            self._recommendation_cache[cache_key] = recommendation

    @staticmethod
    def _apply_immunity_and_resistance(base_damage: float, immunity_percent: int, resistance_value: int, paragon_ranks: int) -> int:
        reduced = (base_damage * (1.0 - ((float(immunity_percent) + 10.0 * float(paragon_ranks)) / 100.0))) - float(resistance_value)
        rounded = int(round(reduced))
        return rounded if rounded > 0 else 0


def load_character_database(source_dir: Optional[str] = None) -> HgxCharacterDatabase:
    directory = os.path.abspath(source_dir or default_character_data_dir())
    if not os.path.isdir(directory):
        raise RuntimeError(f"HGX characters.d data directory was not found: {directory}")

    records: Dict[str, CreatureRecord] = {}
    for entry_name in sorted(os.listdir(directory)):
        if not entry_name.lower().endswith(".xml"):
            continue
        entry_path = os.path.join(directory, entry_name)
        if not os.path.isfile(entry_path):
            continue

        tree = ET.parse(entry_path)
        root = tree.getroot()
        for creature_node in root.findall("creature"):
            name = (creature_node.get("name") or "").strip()
            if not name:
                continue

            base_name = (creature_node.get("base") or "").strip()
            character_type = CHARACTER_TYPE_NAME_TO_VALUE.get((creature_node.get("type") or "Normal").strip().lower(), 0)

            immunity = _make_empty_optional_stats()
            resistance = _make_empty_optional_stats()
            healing = _make_empty_optional_stats()

            damage_root = creature_node.find("damageImmunities")
            if damage_root is not None:
                for damage_node in damage_root.findall("damage"):
                    damage_type_name = _normalize_damage_type_name(damage_node.get("type"))
                    damage_type = DAMAGE_TYPE_NAME_TO_ID.get(damage_type_name)
                    if damage_type is None:
                        continue

                    immunity[damage_type] = _parse_optional_int(damage_node.get("immunity"))
                    resistance[damage_type] = _parse_optional_int(damage_node.get("resistance"))
                    healing[damage_type] = _parse_optional_int(damage_node.get("healing"))

            records[name] = CreatureRecord(
                name=name,
                base_name=base_name,
                character_type=character_type,
                direct_immunity=tuple(immunity),
                direct_resistance=tuple(resistance),
                direct_healing=tuple(healing),
            )

    return HgxCharacterDatabase(directory, records)


def is_slinger_breach_target(creature_name: str) -> bool:
    return str(creature_name or "").strip().lower() in SLINGER_BREACH_TARGETS


def is_slinger_blind_target(creature_name: str) -> bool:
    return str(creature_name or "").strip().lower() in SLINGER_BLIND_TARGETS


_default_database = None
_default_database_lock = threading.Lock()


def load_default_database() -> HgxCharacterDatabase:
    global _default_database
    if _default_database is not None:
        return _default_database

    with _default_database_lock:
        if _default_database is None:
            _default_database = load_character_database()
    return _default_database
