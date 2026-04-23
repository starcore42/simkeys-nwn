# SimKeys for NWN Higher Ground

SimKeys is a Windows control toolkit for **Neverwinter Nights** clients running on the **Higher Ground** server. It was written by **Starcore** to drive in-game functionality without window focus, using direct in-process hooks and a named-pipe control layer instead of foreground key sending.

This public repository contains the SimKeys source, a bundled hook DLL, build files, notes, and packaged `characters.d` support data. It intentionally does not include a game install or unrelated third-party client/script folders.

![SimKeys Control Center](docs/assets/simkeys-control-center.png)

## What it does

- Discovers running `nwmain.exe` clients and injects the next uninjected client.
- Exposes a per-client named pipe for quickbar, chat, state, and automation control.
- Triggers quickbar slots directly through game functions, including Base, Shift, and Control quickbar banks.
- Sends chat through an unfocused client path.
- Provides a desktop GUI for multi-client script control.
- Includes automations for AutoDrink, Stop Hitting, Auto Damage, Auto Attack, Auto Action, and Auto RSM.

## Requirements

- Windows
- Neverwinter Nights Diamond / `nwmain.exe`
- Python for the GUI, controller scripts, and injection. 64-bit Python can inject the 32-bit NWN client.
- Visual Studio 2022 Build Tools with the C++ workload, only if rebuilding the hook DLL yourself

## Repository Layout

- `simkeys_gui.ps1`
  - Main GUI launcher.
- `simkeys_control.ps1`
  - CLI launcher for listing, injecting, querying, slot triggering, and chat send.
- `bin/SimKeysHook2.dll`
  - Bundled prebuilt hook DLL used by default.
- `src/simkeys_app/`
  - Python GUI, controller, pipe client, runtime helpers, and automation host.
- `src/native/SimKeysHook2/`
  - Visual Studio native hook source, solution, and build wrapper.
- `data/characters.d/`
  - Packaged `characters.d` XML data used by Auto Damage scoring.
- `docs/reverse-engineering/notes/`
  - SimKeys-specific reverse-engineering notes.

## Run the GUI

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1
```

If you want to supply an alternate Python path for injection:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1 -InjectPython "C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe"
```

## Run the CLI Controller

List clients:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 list
```

Inject the next uninjected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 inject-next
```

Inject all discovered clients:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 inject-all
```

Query one injected client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 query 1
```

Trigger a quickbar slot:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 slot 1 1
```

Send chat through a client:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_control.ps1 chat-send 1 "!action rsm self"
```

## Bundled DLL

The repository includes `bin\SimKeysHook2.dll`, so users do not need Visual Studio just to run SimKeys. The GUI and CLI prefer this bundled DLL automatically.

Runtime logs are written under `logs\` in the repository root. They are ignored by git.

## Rebuild the DLL

If you want to rebuild the hook from source, install Visual Studio 2022 Build Tools with the C++ workload, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\src\native\SimKeysHook2\build.ps1
```

The build wrapper rebuilds the x86 Release DLL and copies it into `bin\SimKeysHook2.dll`, replacing the bundled copy.

## Data

The repository includes `data\characters.d\` so Auto Damage works out of the box with the packaged dataset.

## Automation Notes

- Auto Damage uses `characters.d` resistances, immunities, healing, and weapon-learning data to choose safe damage.
- Stop Hitting watches your outgoing damage and drinks the configured healing potion if you hit a `characters.d` creature marked `kickback="Area"`.
- Stop Hitting intentionally does not resume attacking after drinking, so the potion acts as an interrupt rather than a heal-and-continue loop.

## License

SimKeys is released under the MIT License. See `LICENSE`.

## Notes

- This project is intended for use on the **Neverwinter Nights Higher Ground** server.
- Written by **Starcore**.
- If you build new scripts, automations, reverse-engineering findings, or quality-of-life features on top of SimKeys, please consider sharing them openly so the Higher Ground community can improve the toolset together.
- This is an unofficial project and is not affiliated with BioWare, Beamdog, or the Higher Ground server team.
