# simKeys_Client.py — sidecar client for \\.\pipe\simkeys_<pid>
# Commands:
#   query        -> print installation state plus a full diagnostic snapshot from inside NWN
#   snapshot     -> print only the full diagnostic snapshot
#   slot N       -> trigger quickbar slot 1..12 (mapped to VK_F1..VK_F12) on the game window thread
#   slot-page P N -> trigger quickbar page P slot N directly (page 0=base, 1=shift, 2=ctrl)
#   vk N         -> trigger an arbitrary virtual key on the same internal path
#   replay       -> replay the last successfully dispatched vk
#   setlog N     -> 0=errors, 1=info, 2=debug
#   chat-send T  -> send chat text through the in-game chat path (default mode 2)
#   chat-poll    -> fetch captured chat/log lines from the hook ring buffer

import argparse, struct
import ctypes as C
import ctypes.wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE_VALUE = C.c_void_p(-1).value
CHAR_NAME_CAPACITY = 128

def winerr(prefix):
    err = C.get_last_error()
    message = C.FormatError(err).strip() if err else "no last-error information"
    return f"{prefix} (err={err}: {message})"

class Pipe:
    def __init__(self, pid, timeout_ms=2000):
        self.path = r"\\.\pipe\simkeys_%d" % pid
        self.C, self.W = C, W
        self.CreateFileW = k32.CreateFileW
        self.WaitNamedPipeW = k32.WaitNamedPipeW
        self.CloseHandle = k32.CloseHandle
        self.ReadFile = k32.ReadFile
        self.WriteFile = k32.WriteFile
        self.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
        self.CreateFileW.restype = W.HANDLE
        self.WaitNamedPipeW.argtypes = [W.LPCWSTR, W.DWORD]
        self.WaitNamedPipeW.restype = W.BOOL
        self.CloseHandle.argtypes = [W.HANDLE]
        self.CloseHandle.restype = W.BOOL
        self.ReadFile.argtypes  = [W.HANDLE, W.LPVOID, W.DWORD, W.LPVOID, W.LPVOID]
        self.ReadFile.restype = W.BOOL
        self.WriteFile.argtypes = [W.HANDLE, W.LPCVOID, W.DWORD, W.LPVOID, W.LPVOID]
        self.WriteFile.restype = W.BOOL

        self.WaitNamedPipeW(self.path, timeout_ms)
        self.h = self.CreateFileW(self.path, 0xC0000000, 0, None, 3, 0, None)  # GENERIC_READ|WRITE
        if self.h in (None, 0, INVALID_HANDLE_VALUE):
            raise OSError(winerr(f"Could not open pipe: {self.path}"))

    def _write(self, b):
        n = self.W.DWORD()
        if not self.WriteFile(self.h, b, len(b), self.C.byref(n), None):
            raise OSError(winerr(f"WriteFile failed for {self.path}"))

    def _read(self, nbytes):
        chunks = bytearray()
        while len(chunks) < nbytes:
            want = nbytes - len(chunks)
            buf = (self.C.c_char * want)()
            n = self.W.DWORD()
            if not self.ReadFile(self.h, buf, want, self.C.byref(n), None) or n.value == 0:
                raise OSError(winerr(f"ReadFile failed for {self.path}"))
            chunks.extend(bytes(buf[:n.value]))
        return bytes(chunks)

    def xfer(self, opcode, payload=b""):
        hdr = struct.pack("II", opcode, len(payload))
        self._write(hdr + payload)
        op, sz = struct.unpack("II", self._read(8))
        data = self._read(sz) if sz else b""
        return op, data

    def close(self):
        if getattr(self, "h", None) not in (None, 0, INVALID_HANDLE_VALUE):
            self.CloseHandle(self.h)
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

OP_QUERY=3000; OP_SLOT=3001; OP_VK=3002; OP_SETLOG=3003; OP_REPLAY=3004; OP_SNAPSHOT=3005; OP_CHAT_SEND=3006; OP_CHAT_POLL=3007; OP_SLOT_PAGE=3008
QUERY_STRUCT = struct.Struct("<" + ("I" * 24) + ("i" * 10) + "I" + ("i" * 2) + f"{CHAR_NAME_CAPACITY}s")

def phex(x): return f"0x{x:08X}"
def as_int(x): 
    s = str(x).lower()
    return int(s, 16) if s.startswith("0x") else int(s)

def decode_cstring(b):
    return b.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

def query_state(p):
    _, data = p.xfer(OP_QUERY)
    expected = QUERY_STRUCT.size
    if len(data) != expected:
        raise RuntimeError(f"unexpected query payload size: got {len(data)}, expected {expected}")
    unpacked = QUERY_STRUCT.unpack(data)
    (module_base, hook_proc, hwnd, current_proc, original_proc, main_tid, installed,
     expected_wndproc, expected_pre_dispatch, expected_dispatch_thunk, expected_dispatch_slot0,
     app_global_slot, app_holder, app_object, app_inner, dispatcher_ptr, gate90, gate94, gate98,
     quickbar_exec, quickbar_slot_dispatch, quickbar_panel_vtable, quickbar_slot_ptr, quickbar_this,
     quickbar_page, quickbar_slot, quickbar_slot_type, quickbar_calls, quickbar_scan_attempts, quickbar_scan_hits,
     last_vk, last_rc, last_error, log_level, player_object, identity_refresh_count, identity_error,
     character_name_raw) = unpacked
    return {
        "module_base": module_base,
        "hook_proc": hook_proc,
        "hwnd": hwnd,
        "current_proc": current_proc,
        "original_proc": original_proc,
        "main_tid": main_tid,
        "installed": installed,
        "expected_wndproc": expected_wndproc,
        "expected_pre_dispatch": expected_pre_dispatch,
        "expected_dispatch_thunk": expected_dispatch_thunk,
        "expected_dispatch_slot0": expected_dispatch_slot0,
        "app_global_slot": app_global_slot,
        "app_holder": app_holder,
        "app_object": app_object,
        "app_inner": app_inner,
        "dispatcher_ptr": dispatcher_ptr,
        "gate90": gate90,
        "gate94": gate94,
        "gate98": gate98,
        "quickbar_exec": quickbar_exec,
        "quickbar_slot_dispatch": quickbar_slot_dispatch,
        "quickbar_panel_vtable": quickbar_panel_vtable,
        "quickbar_slot_ptr": quickbar_slot_ptr,
        "quickbar_this": quickbar_this,
        "player_object": player_object,
        "quickbar_page": quickbar_page,
        "quickbar_slot": quickbar_slot,
        "quickbar_slot_type": quickbar_slot_type,
        "quickbar_calls": quickbar_calls,
        "quickbar_scan_attempts": quickbar_scan_attempts,
        "quickbar_scan_hits": quickbar_scan_hits,
        "last_vk": last_vk,
        "last_rc": last_rc,
        "last_error": last_error,
        "log_level": log_level,
        "identity_refresh_count": identity_refresh_count,
        "identity_error": identity_error,
        "character_name": decode_cstring(character_name_raw),
    }

def cmd_query(p):
    result = query_state(p)
    print(f"moduleBase={phex(result['module_base'])} hwnd={phex(result['hwnd'])} mainTid={result['main_tid']} installed={result['installed']} logLevel={result['log_level']}")
    print(f"wndproc: current={phex(result['current_proc'])} hook={phex(result['hook_proc'])} original={phex(result['original_proc'])} expected_nwn={phex(result['expected_wndproc'])}")
    print(f"path: preDispatch={phex(result['expected_pre_dispatch'])} dispatcherThunk={phex(result['expected_dispatch_thunk'])} dispatcherSlot0={phex(result['expected_dispatch_slot0'])}")
    print(f"engine: appGlobalSlot={phex(result['app_global_slot'])} appHolder={phex(result['app_holder'])} appObject={phex(result['app_object'])} appInner={phex(result['app_inner'])} dispatcher={phex(result['dispatcher_ptr'])} gate90={phex(result['gate90'])} gate94={phex(result['gate94'])} gate98={phex(result['gate98'])}")
    print(f"quickbar: exec={phex(result['quickbar_exec'])} slotDispatch={phex(result['quickbar_slot_dispatch'])} panelVtable={phex(result['quickbar_panel_vtable'])} capturedThis={phex(result['quickbar_this'])} page={result['quickbar_page']} slot={result['quickbar_slot']} slotPtr={phex(result['quickbar_slot_ptr'])} slotType={result['quickbar_slot_type']} calls={result['quickbar_calls']} scanAttempts={result['quickbar_scan_attempts']} scanHits={result['quickbar_scan_hits']}")
    print(f"identity: player={phex(result['player_object'])} name={result['character_name'] or '<unknown>'} refreshes={result['identity_refresh_count']} err={result['identity_error']}")
    print(f"last: vk={phex(result['last_vk'])} rc={result['last_rc']} err={result['last_error']}")
    print()
    cmd_snapshot(p)

def cmd_snapshot(p):
    _, data = p.xfer(OP_SNAPSHOT)
    text = data.decode("utf-8", errors="replace")
    print(text.rstrip())

def cmd_replay(p):
    _, data = p.xfer(OP_REPLAY)
    success, vk, rc, aux_rc, err, path = struct.unpack("iiiiii", data)
    print(f"replay: success={success} vk={phex(vk)} rc={rc} aux={aux_rc} path={path} err={err}")

def cmd_slot(p, slot):
    _, data = p.xfer(OP_SLOT, struct.pack("i", slot))
    success, vk, rc, aux_rc, err, path = struct.unpack("iiiiii", data)
    print(f"slot={slot} success={success} vk={phex(vk)} rc={rc} aux={aux_rc} path={path} err={err}")

def cmd_slot_page(p, page, slot):
    _, data = p.xfer(OP_SLOT_PAGE, struct.pack("ii", slot, page))
    success, vk, rc, aux_rc, err, path = struct.unpack("iiiiii", data)
    print(f"page={page} slot={slot} success={success} vk={phex(vk)} rc={rc} aux={aux_rc} path={path} err={err}")

def cmd_vk(p, vk):
    _, data = p.xfer(OP_VK, struct.pack("i", vk))
    success, out_vk, rc, aux_rc, err, path = struct.unpack("iiiiii", data)
    print(f"vk={phex(vk)} success={success} dispatched={phex(out_vk)} rc={rc} aux={aux_rc} path={path} err={err}")

def cmd_setlog(p, level):
    _, data = p.xfer(OP_SETLOG, struct.pack("i", level))
    (actual,) = struct.unpack("i", data)
    print("log level set to", actual)

def chat_send(p, text, mode=2):
    payload = text.encode("utf-8", errors="replace")
    _, data = p.xfer(OP_CHAT_SEND, struct.pack("ii", mode, len(payload)) + payload)
    success, actual_mode, rc, err = struct.unpack("iiii", data)
    return {
        "success": success,
        "mode": actual_mode,
        "rc": rc,
        "err": err,
    }

def chat_poll(p, after=0, max_lines=20):
    _, data = p.xfer(OP_CHAT_POLL, struct.pack("ii", after, max_lines))
    if len(data) < 8:
        raise RuntimeError(f"unexpected chat-poll payload size: got {len(data)}, expected at least 8")

    latest_seq, count = struct.unpack_from("ii", data, 0)
    offset = 8
    lines = []
    for _ in range(count):
        if offset + 8 > len(data):
            raise RuntimeError("chat-poll payload ended before line header")
        seq, text_len = struct.unpack_from("ii", data, offset)
        offset += 8
        if text_len < 0 or offset + text_len > len(data):
            raise RuntimeError("chat-poll payload ended before line text")
        text = data[offset:offset + text_len].decode("utf-8", errors="replace")
        offset += text_len
        lines.append({
            "seq": seq,
            "text": text,
        })
    return {
        "latest_seq": latest_seq,
        "lines": lines,
    }

def cmd_chat_send(p, text, mode):
    result = chat_send(p, text, mode)
    print(f"chat-send: success={result['success']} mode={result['mode']} rc={result['rc']} err={result['err']}")

def cmd_chat_poll(p, after, max_lines):
    result = chat_poll(p, after, max_lines)
    print(f"chat-poll: latest_seq={result['latest_seq']} count={len(result['lines'])}")
    for line in result["lines"]:
        print(f"[{line['seq']}] {line['text']}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("query")
    sub.add_parser("snapshot")
    sub.add_parser("replay")
    s1 = sub.add_parser("slot"); s1.add_argument("slot", type=int, choices=range(1, 13))
    s2 = sub.add_parser("slot-page"); s2.add_argument("page", type=int, choices=[0, 1, 2]); s2.add_argument("slot", type=int, choices=range(1, 13))
    s3 = sub.add_parser("vk"); s3.add_argument("vk")
    s4 = sub.add_parser("setlog"); s4.add_argument("level", type=int, choices=[0,1,2])
    s5 = sub.add_parser("chat-send"); s5.add_argument("text"); s5.add_argument("--mode", type=int, default=2)
    s6 = sub.add_parser("chat-poll"); s6.add_argument("--after", type=int, default=0); s6.add_argument("--max", type=int, default=20)
    a = ap.parse_args()

    p = Pipe(a.pid)
    try:
        if a.cmd == "query":   cmd_query(p)
        elif a.cmd == "snapshot": cmd_snapshot(p)
        elif a.cmd == "replay": cmd_replay(p)
        elif a.cmd == "slot": cmd_slot(p, a.slot)
        elif a.cmd == "slot-page": cmd_slot_page(p, a.page, a.slot)
        elif a.cmd == "vk": cmd_vk(p, as_int(a.vk))
        elif a.cmd == "setlog": cmd_setlog(p, a.level)
        elif a.cmd == "chat-send": cmd_chat_send(p, a.text, a.mode)
        elif a.cmd == "chat-poll": cmd_chat_poll(p, a.after, a.max)
    finally:
        p.close()
