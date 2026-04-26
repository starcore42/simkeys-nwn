# Client Identity Findings

Date: 2026-04-23

This note records the player-identity lookup chain that moved from HGX reverse engineering into the current working HGCC path.

## HGX `DetectCharacter`

- HGX request `1008` is the `DetectCharacter()` path.
- That request dispatches to `sub_10001A50`.
- The reply path writes response type `2004`, labeled `PlayerLogin`.
- The payload is a single character-name string.

## Confirmed working identity chain

`sub_10001A50` ultimately calls these NWN functions through HGX thunk slots:

- `0x00405160`
- `0x00407850`
- `0x004CEF20`
- `0x005BA420`

The meaning of that chain is:

1. Read the global app slot at `0x0092DC50`.
2. Resolve the app object with `0x00405160`.
3. Resolve the current player object from the live client state with `0x00407850`.
4. Build the player name string with `0x004CEF20`.
5. Destroy the temporary NWN string wrapper with `0x005BA420`.

That exact chain is now the live HGCC identity path as well.

## Relevant NWN behavior

- `0x00407850` leads into `0x00410F70`, which uses the live app state and object id to resolve the active player object.
- `0x004CEF20` reads `[this + 0x2B8]` and calls `0x004F7D90`.
- `0x004F7D90` combines two internal string fields with a space when both are present, which is consistent with first-name / surname style character names.
- `0x005BA420` is the matching NWN string destructor used to free the temporary output object.

The extra `0x00405160` hop still matters. Calling `0x00407850` directly on the global slot value skips one dereference and produces bad identity reads.

## Current HGCC path

The current hook refreshes identity by posting a request back onto the NWN window thread and then running the same chain there.

Operationally that means:

1. The HGCC hook posts `kMsgRefreshIdentity` to the injected window procedure.
2. On the window thread it runs:
   - app-holder read
   - app-object resolve
   - current-player resolve
   - player-name build
   - NWN string destroy
3. It stores the results in hook state:
   - `player_object`
   - `character_name`
   - `identity_error`
   - `identity_refresh_count`
4. Pipe query responses expose that cached identity to the higher-level runtime.

So character identity no longer needs to be guessed from chat text or window titles. The current code asks NWN for the same name HGX used to return.

## Practical HGCC result

This identity path is now the authoritative source for higher-level tooling:

- the runtime probe reads the cached name and player pointer from the hook query
- the GUI displays the resolved character name per client
- the script host waits for this identity before parsing player-owned combat events
- Auto-AA and Auto RSM compare combat-log names against that resolved identity verbatim, including bracketed text such as ` [3.0]` when it is part of the actual character name

So this note should now be read as "confirmed working identity resolver" rather than "what we think HGX probably did".
