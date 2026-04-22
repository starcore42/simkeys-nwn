# SimKeys for NWN Higher Ground

SimKeys is a Windows-based injection and control toolkit for **Neverwinter Nights** clients running on the **Higher Ground** server. It was written by **Starcore** to drive in-game functionality without window focus, using direct in-process hooks and a named-pipe control layer instead of foreground key sending.

This public repository contains the **SimKeys source, project files, notes, and packaged `characters.d` support data**. It intentionally does **not** include the NWN game install or the old HGX client/script folders from the working workspace.

## What it does

- Injects a custom DLL into `nwmain.exe`
- Exposes a per-client named pipe for control
- Triggers quickbar slots directly through game functions
- Sends chat through the unfocused client path
- Provides a desktop GUI for:
  - client discovery
  - next-client injection
  - quickbar/chat testing
  - multi-client script control
- Includes scripted automation such as:
  - AutoDrink
  - Auto Damage for Arcane Archer, Zen Ranger, Divine Slinger, and Gnomish Inventor
  - Auto Action
  - Auto RSM

## Requirements

- Windows
- Neverwinter Nights Diamond / `nwmain.exe`
- Visual Studio 2022 Build Tools with the C++ workload
- A 32-bit Python installation for injection into the 32-bit NWN client
- A normal Python installation for the GUI and controller scripts

## Repository layout

- `SimKeysHook2/`
  - Visual Studio solution and DLL hook project
- `simkeys_gui.py`
  - desktop control UI
- `simkeys_control.py`
  - CLI controller for listing, injecting, querying, slot triggering, and chat send
- `simKeys_Client.py`
  - named-pipe client protocol wrapper
- `simkeys_script_host.py`
  - per-client script runtime and GUI script registry
- `simkeys_runtime.py`
  - client discovery, injection helpers, and shared runtime utilities
- `simkeys_hgx_data.py`
  - Higher Ground combat-data scoring helpers
- `simkeys_hgx_combat.py`
  - HGX/NWN combat log parsing helpers
- `docs/reverse-engineering/notes/`
  - SimKeys-specific reverse-engineering notes
- `simkeys_data/characters.d/`
  - packaged `characters.d` XML data used by Auto Damage scoring

## Build

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\SimKeysHook2\build.ps1
```

That builds the hook DLL in `SimKeysHook2\Release\SimKeysHook2.dll`.

## Run the GUI

The PowerShell launcher will pick a normal Python for the GUI and try to locate a 32-bit Python for injection:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1
```

If you want to supply an explicit x86 Python path:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\simkeys_gui.ps1 -InjectPython "C:\Program Files (x86)\Python313-32\python.exe"
```

## Run the CLI controller

List clients:

```powershell
python .\simkeys_control.py list
```

Inject the next uninjected client:

```powershell
python .\simkeys_control.py inject-next
```

Inject all discovered clients:

```powershell
python .\simkeys_control.py inject-all
```

Query one injected client:

```powershell
python .\simkeys_control.py query 1
```

Trigger a quickbar slot:

```powershell
python .\simkeys_control.py slot 1 1
```

Send chat through a client:

```powershell
python .\simkeys_control.py chat-send 1 "!action rsm self"
```

## Live test helper

There is also a scripted live-test entry point:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\live_test_simkeys.ps1 -NoLaunch -Slot 1
```

This rebuilds the DLL, injects the selected/discovered client, and writes detailed diagnostics under `SimKeysHook2\logs\`.

## Higher Ground data

The repository includes the `simkeys_data\characters.d\` XML data used by the Higher Ground damage-selection logic, so Auto Damage works out of the box with the packaged dataset.

## Notes

- This project is intended for use on the **Neverwinter Nights Higher Ground** server.
- Written by **Starcore**.
- If you build new scripts, automations, reverse-engineering findings, or quality-of-life features on top of SimKeys, please consider sharing them openly so the Higher Ground community can improve the toolset together.
- This is an unofficial project and is not affiliated with BioWare, Beamdog, or the Higher Ground server team.
