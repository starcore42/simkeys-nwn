# Auto-AA Findings

## Original HGX behavior

- `NWN Diamond/HGXLE-final-beta/scripts/auto_aa_2.4.py` listens for HGX-formatted combat log lines containing `" attacks "`.
- It feeds the raw line through `AttackGameEventParser.Parse(...)`, then only reacts when `parsed_event.Attacker` matches the local character name.
- For Arcane Archer mode it computes the best damage type and sends one of these chat commands:
  - `!damac`, `!damco`, `!damel`, `!damfi`, `!damso`, `!damdi`, `!damma`, `!damne`, `!dampo`
- It tracks the currently selected bow type by watching feedback lines such as:
  - `Bow set to <type> damage!`
  - `* Divine Bullets set to <type> damage! *`

## Combat log format

- Real NWN client logs in `NWN Diamond/logs/nwclientLog*.txt` show attack lines like:
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

## `characters.d` model

- HGX creature data lives under `HGXLE-final-beta/data/characters.d`.
- SimKeys now vendors a local copy at `simkeys_data/characters.d`.
- Paragon entries commonly inherit from a base mob using `base="..."` and only override healing fields.
- The old HGX Auto-AA script uses this rule when scoring:
  - start from the target creature
  - if it has a base creature, use the base creature's immunity table
  - use the live creature's resistance and healing tables
  - for `MiniBoss`, use the `Superior <base>` record for resistance/healing and apply `paragon_ranks = 2`

## SimKeys implication

- We do not need the old HGX parser implementation to support Auto-AA.
- A lightweight parser over our captured chat lines is sufficient, as long as it:
  - strips optional log prefixes and timestamps
  - handles attack-mode prefixes like `Sneak Attack :`
  - strips the player's ` [x.y]` suffix before comparing names
- The current SimKeys Auto-AA implementation mirrors the old HGX scoring model and reads from the vendored `characters.d` copy instead of requiring the HGX runtime.

## Divine Slinger notes

- `NWN Diamond/HGXLE-final-beta/scripts/auto_slinger.py.old` and the slinger branch in `auto_aa_2.4.py` use the same damage-selection family as bow modes, but only over:
  - acid, cold, electrical, fire, sonic, divine
- The old slinger scoring model uses:
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
- The old `auto_slinger.py.old` bug came from treating breach/blind as sticky secondary states without a reliable completion signal, which could leave the loop stuck in utility mode.
