# NWN Quickbar Path Findings

Date: 2026-04-23

This note records the real working quickbar activation path now used by the current SimKeys hook. It keeps the addresses and behavior that matter if another developer wants to verify the same chain in their own decompile.

## Confirmed working quickbar paths

The live quickbar trigger path is now:

1. Capture or rediscover the live quickbar panel pointer (`quickbar_this`).
2. Run the quickbar call on the NWN window thread through the injected window procedure.
3. For base-bank slots, call `sub_51FAA0(panel, slotIndex)` directly.
4. For Shift/Ctrl banks, call `sub_51FD10(panel, pageIndex)`, then `sub_51FAA0(panel, slotIndex)`, then restore the original page.
5. Return the result over the SimKeys pipe so higher-level Python tooling can treat it as a normal action request.

In the current hook implementation that maps to:

- `CallQuickbarExecDirect(...)` -> direct `sub_51FAA0` call
- `CallQuickbarPageSelectDirect(...)` -> direct `sub_51FD10` call
- `kMsgTriggerVk` -> base-bank `F1..F12` dispatch on the window thread
- `kMsgTriggerPageSlot` -> explicit page+slot dispatch on the window thread
- pipe op `3001` -> base-bank slot trigger
- pipe op `3008` -> explicit page+slot trigger

This is no longer just a promising narrow candidate. It is the path SimKeys now uses for quickbar activation.

## Narrow engine chain still matches the live implementation

The quickbar execution chain is still the same:

1. `sub_4269A0` at `0x004269A0` is the large command handler with the quickbar command cases.
2. The contiguous quickbar command block is at `0x00427605` and covers cases `5-16`.
3. That block reduces the command id to a slot index and calls `sub_51FAA0`.
4. `sub_51FAA0` at `0x0051FAA0` reads the current page base from `[this + 0x2BB8]`, resolves the slot record, and calls `sub_5164A0(slotRecord)`.

Addresses worth checking in an independent decompile:

- `0x004269A0`
- `0x00427605`
- `0x0051FAA0`

The difference now is operational confidence: the current hook posts back onto the game window thread and calls this narrow path directly instead of only tracing it.

## Quickbar panel capture and layout

The quickbar panel constructor is `sub_51F6D0` at `0x0051F6D0`.

What it confirms:

- Panel vtable is `off_8AB6D0`
- Panel resource name is `"PNL_QUICK_BAR"`
- Slot button range is built around `"QB_BUT66"`, `"QB_BUT67"`, and `"QB_BUT67END"`
- The current page pointer is stored at `[panel + 0x2BB8]`
- The slot storage begins at `panel + 0x68`
- There are `3` pages
- Each page stride is `0xE70`
- Each slot stride is `0x134`
- `sub_51FD10` switches the active page by rewriting `[panel + 0x2BB8]`

Addresses worth checking:

- `0x0051F6D0`
- `0x0051FD10`

The current hook uses that layout in two ways:

- trace hooks capture `quickbar_this`, current page, slot pointer, and slot type from real in-game executions
- a memory scan can rediscover the panel by matching the quickbar vtable, current-page pointer shape, and slot-dispatch pointer when no panel has been captured yet

That scan logic is what lets the current direct-call path recover even if the panel was not previously traced in the same process lifetime.

## Confirmed page mapping for Shift and Ctrl banks

The extra 24 quickbar slots are not driven by a second executor.
The game still uses the same narrow executor at `sub_51FAA0`, but it selects the active quickbar page first through `sub_51FD10`.

Confirmed mapping:

- page `0` = base quickbar
- page `1` = `Shift+F1` through `Shift+F12`
- page `2` = `Ctrl+F1` through `Ctrl+F12`

Why this is high-confidence:

1. The input/keymap registration block around `0x0040DAB8..0x0040DE73` binds:
   - actions `5..16` to `"F1 Key"` through `"F12 Key"`
   - actions `19` and `20` to `"Left Shift Key"` and `"Right Shift Key"`
   - actions `21` and `22` to `"Left Control"` and `"Right Control"`
2. In `sub_4269A0`, the command handlers for actions `19/20` set `[keyState + 0x8]` and then call `sub_51FD10(panel, 1)`.
3. The command handlers for actions `21/22` set `[keyState + 0xC]` and then call `sub_51FD10(panel, 2)`.
4. If neither modifier-state field is set, the same code falls back to `sub_51FD10(panel, 0)`.
5. The quickbar activation block for actions `5..16` then calls `sub_51FAA0(panel, slotIndex)` using whichever page is currently active.

Addresses worth checking:

- `0x0040DAB8..0x0040DE73`
- `0x004269A0`
- `0x0051FD10`
- `0x0051FAA0`

The current page-slot trigger path in the hook mirrors that behavior exactly:

1. Resolve the original page.
2. Switch to the requested page with `sub_51FD10`.
3. Execute the slot with `sub_51FAA0`.
4. Restore the original page if one was changed.

## Current runtime dispatch behavior

The injected window procedure now has three practical dispatch modes:

- path `1` = fall back to the broader key pre-dispatch / `WM_KEYUP` route
- path `2` = direct base-bank quickbar exec through `sub_51FAA0`
- path `3` = direct page select + quickbar exec + page restore

For quickbar work, the important real path is path `2` or `3`.

The higher-level runtime already treats this as the authoritative quickbar API:

- pipe op `3001` is used for page `0`
- pipe op `3008` is used for page `1` or `2`
- the script host uses that runtime helper for AutoDrink and other quickbar-backed actions
- the GUI presents the three banks as Base / Shift / Ctrl rows

The query snapshot also reports the rebased runtime addresses and latest quickbar capture state:

- quickbar exec address
- quickbar page-select address
- slot-dispatch address
- captured panel pointer
- current page
- last slot
- last raw slot type and jump-table case
- scan attempts / hits

So the reverse-engineering note should now be read as "confirmed working path plus layout details", not "best remaining guess".

## What `sub_5164A0` still tells us about slot semantics

`sub_5164A0` at `0x005164A0` remains the per-slot executor below `sub_51FAA0`.
Its first steps are still important:

- it gates on `[slot + 4]`
- it reads the raw slot kind byte at `[slot + 0x84]`
- it decrements that byte before entering the jump table

That means:

- `raw slot type = 1` becomes jump-table case `0`
- `raw slot type = 10` becomes jump-table case `9`
- `raw slot type = 38` becomes jump-table case `37`

Important known mappings from the raw decompile:

- raw `1` -> `sub_516F10`
- raw `2` -> `sub_517130`
- raw `3` -> `sub_517A30`
- raw `4` -> `sub_517B00`
- raw `6` -> `sub_517830`
- raw `7` -> `sub_5170A0`
- raw `8` -> actor path through `sub_4CFB90`
- raw `10` -> `sub_517B90`
- raw `11-17` -> `sub_517250` with literal selectors `5, 6, 0x0D, 0x0C, 7, 0x0F, 9`
- raw `38` -> `sub_4B1F80`
- raw `39` -> `sub_517C20`
- raw `40` -> `sub_5178C0`
- raw `41` -> `sub_517950`
- raw `42` -> `sub_4D0380`
- raw `43` -> `sub_4D03A0`

Address worth checking:

- `0x005164A0`

This part is still useful for understanding what a given quickbar slot actually does after activation, even though the top-level activation path itself is now settled.

## Important correction: raw `slotType=10` is not the `sub_4B1F80` path

The live trace for a representative manual slot activation showed `slotType=10`.

That does **not** map to `sub_4B1F80`.

Because `sub_5164A0` decrements the raw type before the jump table:

- raw `10` -> case `9` -> `sub_517B90`
- raw `38` -> case `37` -> `sub_4B1F80`

This still matters when interpreting slot traces from the working hook, because the direct quickbar call path proves activation but does not by itself explain the meaning of every slot kind.

## Paths that are still not the activation path

`sub_515690` at `0x00515690` still looks like quickbar drag/drop or assignment handling, not slot activation.

Why it is likely the wrong path for execution:

- it starts by creating `"gui_dm_drop"`
- it switches on `arg0 - 5`
- it fans out to handlers like `sub_4AEB70`, `sub_4AEC50`, `sub_4AED30`, and `sub_4AEDE0`

This remains useful for reverse engineering slot setup and manipulation, but the actual working activation path is now the `sub_51FD10` / `sub_51FAA0` chain above.
