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
- Player names often carry a level suffix like ` [3.0]`, which should be stripped before comparing against the current character name.

## Creature data model

- The HGX creature dataset is keyed by mob name and includes immunity, resistance, healing, and inheritance data.
- SimKeys vendors a local copy of that dataset for runtime lookups.
- Paragon entries commonly inherit from a base mob using `base="..."` and only override healing fields.
- The legacy Auto-AA scoring model uses this rule when scoring:
  - start from the target creature
  - if it has a base creature, use the base creature's immunity table
  - use the live creature's resistance and healing tables
  - for `MiniBoss`, use the `Superior <base>` record for resistance/healing and apply `paragon_ranks = 2`

## SimKeys implication

- We do not need the legacy parser implementation to support Auto-AA.
- A lightweight parser over captured chat lines is sufficient, as long as it:
  - strips optional log prefixes and timestamps
  - handles attack-mode prefixes like `Sneak Attack :`
  - strips the player's ` [x.y]` suffix before comparing names
- The current SimKeys Auto-AA implementation mirrors the legacy scoring model and reads from the vendored creature dataset instead of requiring the HGX runtime.

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
- The SimKeys slinger loop therefore uses a hybrid approach:
  - real breach confirmation when a `Target : Breach ...` line appears
  - bounded pending windows for breach and blind so the script can advance back to bonus-damage mode instead of getting stuck forever
- A bug in the legacy slinger implementation came from treating breach and blind as sticky secondary states without a reliable completion signal, which could leave the loop stuck in utility mode.
