# NWN Chat Path Findings

Date: 2026-04-23

This note records the chat paths that moved from reverse-engineered candidates into real working SimKeys paths.

## Confirmed working outbound chat path

- HGX request type `1005` maps to chat messages.
- That request is dispatched by `sub_100074E0`.
- `sub_100074E0` copies the payload into the server's local string wrapper, reads the HGX chat mode from `[request+0x0C]`, and calls the NWN function pointer resolved at `0x0057C9F0`.

That NWN target is now the live SimKeys send path as well:

- address: `0x0057C9F0`
- call shape observed from HGX and used by SimKeys: pointer-to-string-object plus `mode`
- wrapper used by SimKeys: `{ char* text; int32_t length; }`

The current hook path is:

1. Queue a send request over the SimKeys pipe with op `3006`.
2. Post `kMsgSendChat` to the NWN window thread.
3. On that thread, build the lightweight NWN string wrapper and call `0x0057C9F0` directly.
4. Return `success`, `mode`, `rc`, and `err` to the caller.

This is the path used by:

- the low-level pipe client
- the Python runtime send helper
- script-host helpers such as `send_chat(...)` and `send_console(...)`
- higher-level automation scripts such as Auto-AA, AutoDrink, and Auto RSM

## What `0x0057C9F0` still appears to do

- It parses slash commands and local-console commands.
- The decompile shows explicit handling for:
  - `##` local console messages
  - `/d`, `/p`, `/s`, `/w`, `/dm`, `/o`, `/t` and `/tell`
- For plain non-command text it falls through to a small mode switch at the bottom of the function and dispatches through specialized chat-channel handlers.

That matters because the current hook is not inventing a parallel chat system. It is calling the same in-game function HGX used, so plain text, slash commands, and `##` console output all go through the same parser and channel logic.

## HGX inbound log/chat events

- HGX uses pipe responses:
  - `2001` as log text / game event text
  - `2002` as chat-command style text
- The relevant server-side response writers are at:
  - `0x10007A40` for response type `0x7D1` (`2001`)
  - `0x100076E0` for response type `0x7D2` (`2002`)

Recreating HGX's whole server-side event system is still unnecessary for SimKeys because the local hook now captures the rendered NWN chat/log output directly inside the client.

## Confirmed working inbound capture path

- The NWN function at `0x00493BD0` is not just a likely capture point anymore. It is the live chat-window capture point used by the hook.
- Evidence for the choice is still the same:
  - many game paths call it
  - `0x0057C9F0` also calls it for console / parsed chat output
  - it formats a debug line using `"[CHAT WINDOW TEXT] [%s] %s\n"`

The current capture flow is:

1. A trace hook on `0x00493BD0` receives the NWN string object passed into the chat window.
2. The hook extracts the text buffer and length from that NWN string object.
3. The text is appended to a local ring buffer with a monotonically increasing sequence number.
4. Pipe op `3007` returns deltas from that ring buffer to callers.

So the current live read-side API is:

- pipe op `3007` = poll rendered chat/log lines
- response shape = `latest_seq` plus zero or more `(seq, text)` entries

This is the feed consumed by the script host, which means the automation scripts are now driven by the same rendered text the player sees in the in-game chat window.

## Practical SimKeys result

The reverse-engineering conclusion is now operational:

- outbound chat uses `0x0057C9F0` on the real NWN window thread
- inbound chat/log capture uses `0x00493BD0`
- both are exposed over the per-process SimKeys pipe
- the higher-level Python tooling already treats those as the authoritative chat paths

That makes chat one of the areas where the notes should now be read as "confirmed working hook path" rather than "future integration idea".
