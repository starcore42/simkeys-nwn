# HG Control Console

HG Control Console (HGCC), formerly SimKeys, is a Windows control and automation toolkit for **Neverwinter Nights Diamond** clients running on the **Higher Ground** server. It controls NWN clients without requiring foreground focus by injecting a small native hook into each `nwmain.exe` process and exposing a per-client named pipe to the Python GUI and CLI tools.

The project is aimed at multi-client play, combat automation, damage analysis, and Higher Ground quality-of-life workflows. It is not a key-sender wrapper: quickbar activation, chat send, chat capture, overlays, player identity, and quickbar state are handled through in-process client paths.

![HG Control Console](docs/assets/simkeys-control-center.png)

## Scope

HGCC can:

- Discover running `nwmain.exe` clients and inject one or all uninjected clients.
- Query injected clients for player identity, window details, quickbar state, and hook diagnostics.
- Trigger Base, Shift, and Ctrl quickbar slots through NWN quickbar functions.
- Send chat and HG commands through an injected client, even when that client is not focused.
- Capture rendered chat/log lines from each injected client and route them to automation scripts.
- Display a clickable in-game script control strip for injected clients.
- Display in-game timer overlays for status rules, self-cast effects, and Limbo tracking.
- Run per-character automation scripts from the desktop GUI.
- Save per-character script settings and start the saved set across all injected clients.
- Record per-session chat logs and calculate an out-of-game multi-client damage meter.
- Learn and operate weapon-swap profiles for tank-style weapon sets and shifter weapon sets.

## Requirements

- Windows.
- Neverwinter Nights Diamond, using the 32-bit `nwmain.exe` client.
- Python for the GUI, CLI, and injector scripts. The launchers look for `python`, the `py` launcher, and common Python 3.11-3.13 install locations. The runtime code uses the Python standard library and Tkinter.
- Administrator rights when NWN is running elevated. The GUI launcher requests elevation automatically; the CLI should be run from an elevated PowerShell if the target clients require it.
- Visual Studio 2022 Build Tools with the C++ workload only if rebuilding `SimKeysHook2.dll`.

The repository includes a prebuilt 32-bit hook DLL at `bin\SimKeysHook2.dll`, so Visual Studio is not required for normal use.

## Repository Layout

- `simkeys_gui.ps1`
  - Main desktop GUI launcher.
- `simkeys_control.ps1`
  - CLI launcher for discovery, injection, quickbar, chat, and chat-watch commands.
- `bin/SimKeysHook2.dll`
  - Bundled native hook DLL used by default.
- `src/simkeys_app/`
  - Python GUI, CLI controller, injector, pipe client, runtime helpers, damage meter, and automation host.
- `src/native/SimKeysHook2/`
  - Native hook source, Visual Studio solution, and build wrapper.
- `data/characters.d/`
  - Packaged Higher Ground creature data used by Auto Damage, Stop Hitting, weapon analysis, Limbo filtering, and the damage meter.
- `data/followcues.d/`
  - XML follow cue phrases used by Always On follow behavior.
- `data/statusrules.d/`
  - XML timer/status rules used by In-Game Timers.
- `docs/reverse-engineering/notes/`
  - Notes for the confirmed NWN chat, identity, quickbar, and Auto Damage paths.

## Legacy Internal Names

The project used to be named SimKeys. Several names intentionally remain unchanged because they are part of the local compatibility surface:

- `simkeys_gui.ps1` and `simkeys_control.ps1` are still the launcher filenames.
- `src/simkeys_app/` is still the Python package name.
- `bin/SimKeysHook2.dll`, `src/native/SimKeysHook2/`, and `InitSimKeys` are still the native hook DLL, project, and export names.
- Injected clients still expose `\\.\pipe\simkeys_<pid>`.
- Some internal class, thread, and C++ symbol names still include `SimKeys`.

Use **HG Control Console** or **HGCC** for user-facing documentation and discussion. Use the legacy names only when referring to actual filenames, modules, exports, symbols, or pipe names.

## Quick Start

1. Start one or more NWN Diamond clients.
2. Open PowerShell in the repository root.
3. Start the GUI:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1
```

4. Press `Refresh Clients`.
5. Press `Inject Next` or `Inject All`.
6. Select an injected client in the left pane.
7. Configure and start scripts from the `Automation` panel.

The GUI auto-refreshes by default. Each injected client gets an in-game script control strip, and the GUI keeps script state synchronized with the client list.

## GUI Launcher

The GUI launcher resolves Python, sets `PYTHONPATH` to `src`, and runs `simkeys_app.simkeys_gui`. If the current PowerShell is not elevated, it restarts itself with administrator rights before launching the GUI.

Basic launch:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1
```

Use an explicit Python interpreter for the GUI:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1 -PythonExe "C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe"
```

Use a different Python interpreter for injection:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1 -InjectPython "C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe"
```

Additional GUI arguments are passed through to the Python GUI. The current Python options are:

- `--process-name`
  - Process image name to discover. Default: `nwmain.exe`.
- `--dll`
  - Hook DLL path. Default: newest available `bin\SimKeysHook2.dll` or local native build output.
- `--export`
  - Hook initialization export. Default: `InitSimKeys`.
- `--inject-python`
  - Alternate Python interpreter used by the injection subprocess.
- `--refresh-ms`
  - GUI auto-refresh interval in milliseconds. Default: `2500`.

Example:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1 --refresh-ms 1000
```

## GUI Workflow

### Client List

The left pane lists discovered clients in launch order. Each row shows ordinal, PID, injected state, resolved character name, window title, start time, and running script count. HGCC resolves character identity from NWN client memory after injection rather than relying on the window title.

The selected client controls the details panel, manual controls, target analysis, and automation rows.

### Injection

`Inject Next` injects the first uninjected client in launch order. `Inject All` injects every uninjected discovered client. Injection loads `SimKeysHook2.dll` into the target process, calls `InitSimKeys`, installs the hook, and starts the per-client pipe server.

The injector validates that the target process and DLL are both 32-bit. `SimKeysHook2.dll` is intended for the 32-bit NWN Diamond client.

### Manual Test Controls

Manual controls are intended for testing and reverse engineering. They expose:

- Base quickbar slots, equivalent to `F1..F12`.
- Shift quickbar slots, equivalent to `Shift+F1..Shift+F12`.
- Ctrl quickbar slots, equivalent to `Ctrl+F1..Ctrl+F12`.
- Raw chat send through the injected client chat path.

These controls use the same runtime helpers as the scripts.

### Saved Per-Character Settings

Each automation row has a `Saved` checkbox. HGCC stores per-character script settings and saved-script selections in:

```text
data\character_defaults.user.json
```

That file is ignored by git. When a character is seen again, HGCC loads the saved settings for that character. `Start Saved` starts the saved scripts across all injected clients. `Stop All Scripts` stops currently running scripts while leaving injected clients and overlay controls in place.

### In-Game Controls

Injected clients receive a small clickable script control strip rendered inside the NWN frame. Clicking a script control toggles that script for the client. HGCC blocks automation chat sends briefly if it detects the password prompt text, to avoid scripts sending commands while a client is at login/password entry.

## CLI Controller

The CLI launcher resolves Python, sets `PYTHONPATH` to `src`, and runs `simkeys_app.simkeys_control`.

List discovered clients:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 list
```

Inject the next uninjected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 inject-next
```

Skip one uninjected client and inject the next:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 inject-next --skip 1
```

Inject all discovered clients:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 inject-all
```

Query an injected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 query --client 1
```

Trigger a quickbar slot:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 slot 1 --client 1
```

Trigger a Shift or Ctrl quickbar slot:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 slot 5 --page 1 --client 1
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 slot 5 --page 2 --client 1
```

Send chat or an HG command through an injected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 chat-send "!action rsm self" --client 1
```

Watch chat from one injected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 watch-chat --client 1
```

Watch chat from all injected clients, with a filter:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 watch-chat --all --filter damages
```

Client selectors accept the list ordinal, PID, exact character/window name, or a unique name fragment. If exactly one injected client is available, `--client` can be omitted for commands that operate on a single injected client.

## Automation Reference

The GUI starts scripts per injected client. Chat-driven scripts process only new lines by default. Enable `Backlog` only when a script should inspect older buffered combat lines when it starts.

### AutoDrink

AutoDrink watches combat activity involving the client, reads current and maximum HP directly from NWN memory, and triggers a configured quickbar slot when HP is at or below the configured percentage.

Important settings:

- `Slot` and `Bank`
  - Potion quickbar location. Bank can be Base, Shift, or Control.
- `HP %`
  - Trigger threshold. Default: `80`.
- `Cooldown`
  - Delay after drinking before another drink attempt.
- `Lock`
  - Sends `!lock opponent` before drinking.
- `Resume`
  - Sends `!action attack locked` after the drink cooldown.
- `Echo`
  - Prints compact feedback through the in-game console.

AutoDrink is meant for survival automation where the client should resume fighting after the potion action.

### Stop Hitting

Stop Hitting watches outgoing damage by the selected character and checks the defender against `characters.d`. If the target has `kickback="Area"`, the script triggers the configured potion slot and enters a short interrupt cooldown.

Unlike AutoDrink, Stop Hitting does not resume attacking. The potion press is used as an interrupt so the client stops feeding area kickback or similar punishment mechanics.

### Auto Damage

Auto Damage watches attack and damage lines for the selected character, tracks the current target, consults `characters.d`, and changes damage mode or weapon state based on target defenses.

Available modes:

- `Arcane Archer`
  - Selects the best `!dam*` type for AA-style damage.
- `Zen Ranger`
  - Scores linked elemental/exotic damage pairs.
- `Divine Slinger`
  - Chooses elemental/divine damage and can sequence breach (`!dambr`) or blind (`!dambd`) for configured target families.
- `Gnomish Inventor`
  - Selects the best `!gi bolt` type, resets to zappers when needed, and can maintain a canister loop for the current target.
- `Weapon Swap`
  - Learns and swaps configured quickbar weapon sets.
- `Shifter Weapon Swap`
  - Performs weapon learning and swapping around polymorph state, including unshift and re-shift handling.

Auto Damage depends on accurate client identity. It compares combat-log actor names against the resolved character name, including bracket suffixes such as `[3.0]` when they are part of the displayed name.

## Weapon Swap And Shifter Weapon Swap

Weapon swapping is one of HGCC's major automation features. It is designed for tank-style weapon sets and shifter forms where the best weapon is target-specific and where ordinary client-side helpers cannot reliably read enough context or control unfocused clients.

### Weapon Swap

`Weapon Swap` mode supports up to six configured base quickbar weapon slots, labeled `W1` through `W6`. The script learns each configured weapon from observed outgoing damage components, tracks the current weapon, scores known weapons against the current target, and triggers the best safe option through the injected quickbar path.

The learning and scoring loop uses:

- Outgoing attack lines to identify the current target.
- Outgoing damage lines to learn each weapon's elemental/exotic damage signature.
- `characters.d` immunity, resistance, healing, paragon, and special-target data.
- The injected quickbar equipped-item mask to reconcile the current weapon when possible.
- Pending-swap state so the same weapon slot is not pressed repeatedly while HGCC waits for confirming damage.

Weapon Swap avoids weapons that would heal the target. If no configured weapon is safe, it can fall back to unarmed by pressing the currently equipped weapon slot to unequip it, provided HGCC can identify a safe source slot.

The Target Analysis panel is focused on this mode. It shows the matched `characters.d` record, paragon rank, immunity, resistance, healing, learned weapon summaries, expected damage, actual observed damage when available, healing warnings, and the recommended weapon. It also explains why HGCC is holding the current weapon when the best alternate does not beat the configured gain threshold.

### Shifter Weapon Swap

`Shifter Weapon Swap` extends the same target-defense scoring to shifted characters. It requires:

- At least one configured weapon slot.
- A configured `Shift` quickbar slot used to return to form.
- A known `Cur` value unless the script can reconcile the current weapon from later evidence.

When a shifter swap is needed, HGCC:

1. Locks the current target.
2. Sends `!cancel poly`.
3. Waits for the unshift state, including Player Hide feedback.
4. Triggers the selected weapon slot or unarmed fallback.
5. Confirms the weapon state using damage or equipped-slot evidence where possible.
6. Retries the shift slot until the shifted state is confirmed.
7. Resumes attacking the locked target.

The default shifter policy is conservative. If the current weapon is safe, HGCC holds it unless a safe alternate exceeds the configured `Shift Gain %` threshold. If `Heal Only` is enabled, shifter mode keeps the older behavior and swaps only when the current weapon would heal the target.

Important weapon settings:

- `Cur`
  - Current configured weapon (`W1` through `W6`) or `Unknown`.
- `W1` through `W6`
  - Base quickbar weapon slots. Base slots are used for learned weapon bindings.
- `Shift`
  - Quickbar slot used to return to the desired shifted form.
- `Swap`
  - Cooldown after a weapon slot press.
- `Gain %`
  - Normal Weapon Swap threshold for holding the current weapon.
- `Shift Gain %`
  - Shifter-specific threshold for swapping away from a safe current weapon.
- `Heal Only`
  - Restricts shifter swaps to cases where the current weapon heals the target.

Shift and Ctrl weapon slots are available to some script settings, but learned weapon bindings intentionally use base quickbar slots because NWN's equipped-slot mask is not reliable enough for shifted weapon tracking on modifier banks.

### Special Target Handling

HGCC has special handling for targets where only a specific known weapon is allowed. For example, Mammons Tear targets are protected so that `Mammon's Wrath` is the only allowed non-shifter weapon recommendation when HGCC identifies that weapon profile.

## Damage Meter

The GUI records new chat/log lines from injected clients into `logs\damage-meter\` for the current GUI session. The directory is reset when the GUI starts.

Press `Calculate` in the Damage Meter panel to analyze the saved session. The calculation:

- Counts party damage against enemies known in `characters.d`.
- Treats actors not present in `characters.d` as party members.
- Merges duplicate views of the same damage event seen by multiple clients.
- Keeps hits that are visible to only one client.
- Resolves some `someone` lines when another client saw the same event with real names.
- Splits enemy healing from raw damage when the defender has a healing multiplier for the damage type.
- Reports raw damage, enemy healing, net damage, per-actor totals, and element breakdowns.

The progress bar reports the counting, reading, merging, and classifying phases. The report buttons post net, raw, healing, or element summaries through the selected injected client.

## Other Automation Scripts

### Auto Action

Auto Action repeatedly sends one selected HG action command on a cooldown:

- Called Shot: `!action cs opponent`
- Knockdown: `!action kd opponent`
- Disarm: `!action dis opponent`

It does not need combat-log parsing.

### Auto Attack

Auto Attack repeatedly sends:

```text
!action attack lead:opponent
```

Use `Set Selected as Lead` on the Auto Attack row to assign the selected injected client as the lead. HGCC stops Auto Attack on the lead, then sends `!role lead` and `/tell "<lead>" !target` through each follower so they target the lead's opponent.

### Always On

Always On bundles background utility helpers:

- Follow cues loaded from `data\followcues.d\`.
- Zerial wallet refresh on `You are now in Zerial's Workshop`.
- Spellbook fill on `Resting.`.
- Fog disable on area transitions using `##mainscene.fog 0`.

Follow cues default to phrases such as `fall in`, `follow me`, and `follow my`. When another player says a configured cue, HGCC sends `!action aso target` and then `/tell "<speaker>" !target`. Cues from the current character are ignored.

Each utility can be disabled independently from the Always On settings.

### Auto Combat Mode

Auto Combat Mode keeps one selected combat mode active while the character is attacking.

Supported modes:

- Rapid Shot: `!action rsm self`
- Flurry of Blows: `!action fbm self`
- Expertise: `!action exm self`
- Improved Expertise: `!action iem self`
- Power Attack: `!action pam self`
- Improved Power Attack: `!action ipm self`

Rapid Shot uses an NWN memory byte to determine whether RSM is already active. The other modes read attack-mode prefixes from combat-log attack lines. The script uses a retry cooldown and can echo trigger feedback to the in-game console.

### In-Game Timers

In-Game Timers renders an overlay inside the NWN client. It loads status and cooldown rules from `data\statusrules.d\statusrules.xml` by default and can use a custom rules directory.

It tracks:

- Fixed-duration timer rules.
- Variable-duration timer rules using `{MINUTES}` and `{SECONDS}` captures.
- Self-cast spell timers. After a configured self-cast, HGCC requests `!effects` and targets the current character to confirm remaining duration. If confirmation times out, it can use configured fallback duration data.
- Limbo timers for party-style actors, using `characters.d` to avoid treating known enemies as party members.
- Rest/death cleanup for timers marked to clear on rest or death.

Timer overlay position, offset, font size, color, maximum displayed timers, Limbo duration, and spell timer list are configurable from the script settings.

## Data Files

### `characters.d`

`data\characters.d\` contains the packaged creature dataset used by:

- Auto Damage scoring.
- Weapon Swap and Shifter Weapon Swap target analysis.
- Stop Hitting kickback checks.
- Damage Meter enemy/party classification.
- In-Game Timers Limbo filtering.

The XML records contain creature names, inheritance, immunity, resistance, healing, paragon classification, and special flags such as `kickback`.

### `followcues.d`

`data\followcues.d\default.xml` contains follow cue phrases for Always On. Add or edit XML files in this directory to change the phrases HGCC recognizes.

### `statusrules.d`

`data\statusrules.d\statusrules.xml` contains timer, variable timer, state, and spell-timer rules for In-Game Timers.

### User Defaults

`data\character_defaults.user.json` is written by the GUI when per-character script settings are saved. It is intentionally ignored by git.

## Logs

Runtime logs are written under `logs\` in the repository root. The damage meter uses `logs\damage-meter\` for the current GUI session and resets that directory on GUI start.

The native hook also writes process-specific diagnostic logs under the runtime log directory using legacy `simkeys_<pid>.log` filenames.

## Rebuilding The Native Hook

Normal users do not need to rebuild the hook. To rebuild it, install Visual Studio 2022 Build Tools with the C++ workload, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\src\native\SimKeysHook2\build.ps1
```

The build wrapper:

1. Locates Visual Studio with `vswhere.exe`.
2. Rebuilds the `SimKeysHook2` project as `Release|x86`.
3. Writes build output under `src\native\SimKeysHook2\Release\`.
4. Copies the rebuilt DLL to `bin\SimKeysHook2.dll`.

## Low-Level Pipe Client

`src\simkeys_app\simKeys_Client.py` is the lower-level pipe client used by the runtime helpers. It can query state, trigger slots, send chat, poll chat, and show or clear overlays when given a PID. Most users should use `simkeys_gui.ps1` or `simkeys_control.ps1`; the low-level client is mainly useful for debugging hook behavior.

The compatibility pipe name is:

```text
\\.\pipe\simkeys_<pid>
```

## Notes

- HGCC is intended for Neverwinter Nights Diamond on the Higher Ground server.
- It is written by Starcore.
- It is unofficial and is not affiliated with BioWare, Beamdog, or the Higher Ground server team.
- If you build useful scripts, rules, reverse-engineering notes, or data improvements on top of HGCC, consider sharing them with the Higher Ground community.

## License

HG Control Console is released under the MIT License. See `LICENSE`.
