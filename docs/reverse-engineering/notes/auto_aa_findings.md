# Auto-AA Findings

## Legacy HGX behavior

- The legacy HGX Auto-AA implementation listens for HGX-formatted combat log lines containing `" attacks "`.
- It feeds the raw line through `AttackGameEventParser.Parse(...)`, then only reacts when `parsed_event.Attacker` matches the local character name.
- For Arcane Archer mode it computes the best damage type and sends one of these chat commands:
  - `!damac`, `!damco`, `!damel`, `!damfi`, `!damso`, `!damdi`, `!damma`, `!damne`, `!dampo`
- It tracks the currently selected bow type by watching feedback lines such as:
  - `Bow set to <type> damage!`
  - `* Divine Bullets set to <type> damage! *`

## Combat log format

- Captured NWN client logs show attack lines like:
  - `Starcore-Slinger [3.0] attacks Narzugon : *hit* : ...`
  - `Attack Of Opportunity : Guardian of Fire attacks Starcore-Slinger [3.0] : *miss* : ...`
  - `Sneak Attack : Starcore-Lash-Quasi [1.0] attacks Guardian of Fire : *hit* : ...`
- The stable parsing rule is:
  - optional attack-mode prefix ending in `:`
  - attacker name
  - literal `" attacks "`
  - defender name
  - another `:`
- Bracketed text like ` [3.0]` can be part of the actual character name and must be preserved when comparing attack lines against the resolved player identity.

## Creature data model

- The HGX creature dataset is keyed by mob name and includes immunity, resistance, healing, and inheritance data.
- HGCC vendors a local copy of that dataset for runtime lookups.
- Paragon entries commonly inherit from a base mob using `base="..."` and only override healing fields.
- The legacy Auto-AA scoring model uses this rule when scoring:
  - start from the target creature
  - if it has a base creature, use the base creature's immunity table
  - use the live creature's resistance and healing tables
  - for `MiniBoss`, use the `Superior <base>` record for resistance/healing and apply `paragon_ranks = 2`

## HGCC implication

- We do not need the legacy parser implementation to support Auto-AA.
- A lightweight parser over captured chat lines is sufficient, as long as it:
  - strips optional log prefixes and timestamps
  - handles attack-mode prefixes like `Sneak Attack :`
  - preserves the full attacker and defender names exactly as they appear in the log
- The current HGCC Auto-AA implementation mirrors the legacy scoring model and reads from the vendored creature dataset instead of requiring the HGX runtime.

## Divine Slinger notes

- The legacy Divine Slinger logic and the slinger branch of the legacy Auto-AA implementation use the same damage-selection family as bow modes, but only over:
  - acid, cold, electrical, fire, sonic, divine
- The legacy slinger scoring model uses:
  - elemental types: `13 * dice / 2`
  - divine type: `13 * (dice - 2) / 2`
- The important behavioral rule is:
  - select the safe base damage type first
  - only then switch into special secondary modes such as breach or blind
- Archived HGX logs show explicit breach confirmation lines such as:
  - `Rakshasa : Breach Negative Energy Protection`
  - `Pharaoh's Bonded : Breach Freedom of Movement`
- Archived HGX logs did not provide a similarly reliable blind-confirmation line in the sampled data.
- The HGCC slinger loop therefore uses a hybrid approach:
  - real breach confirmation when a `Target : Breach ...` line appears
  - bounded pending windows for breach and blind so the script can advance back to bonus-damage mode instead of getting stuck forever
- A bug in the legacy slinger implementation came from treating breach and blind as sticky secondary states without a reliable completion signal, which could leave the loop stuck in utility mode.

## Weapon-swap notes

- Raw NWN client logs expose outgoing damage lines in this stable form:
  - `Attacker damages Defender: total (component component ...)`
- Sample captures include:
  - `Guardian of Fire damages ~Legendary Dracolich~: 173 (112 Physical 21 Fire 40 Negative Energy)`
  - `Starcore-Slinger [3.0] damages Anymental: 74 (73 Physical 1 Divine)`
- Those breakdowns are useful even when a component is fully resisted because zero-value components still appear, for example:
  - `0 Electrical`
  - `0 Fire`
- The HGCC `Weapon Swap` mode therefore learns weapon typing from raw `damages` lines instead of HGX feedback text.
- Each configured weapon is auto-classified against the supported payload families:
  - `DB`: 3 elemental types at `5d12` each and 3 exotic types at `2d12` each
  - `P1`: 1 elemental type at `9d12` and 2 exotic types at `6d12` each
  - `XR`: 2 elemental types at `11d12` each and 1 exotic type at `8d12`
- Physical damage is intentionally treated as a shared baseline and is not used to rank weapons yet.
- Because triggering the same quickbar weapon twice will unequip it, the HGCC weapon mode keeps explicit state for:
  - the currently equipped configured weapon
  - a pending swap that should not be retriggered until the next combat round window has elapsed
- The GUI exposes that current-weapon selector directly, and the script refuses to arm `Weapon Swap` until it has a known starting weapon.
