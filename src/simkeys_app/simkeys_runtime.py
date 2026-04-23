import ctypes as C
import ctypes.wintypes as W
import os
import re
import struct
import subprocess
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from . import inject_simkeys
from . import simKeys_Client as simkeys


TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260
INVALID_HANDLE_VALUE = C.c_void_p(-1).value

k32 = C.WinDLL("kernel32", use_last_error=True)
u32 = C.WinDLL("user32", use_last_error=True)


class FILETIME(C.Structure):
    _fields_ = [
        ("dwLowDateTime", W.DWORD),
        ("dwHighDateTime", W.DWORD),
    ]


class PROCESSENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", C.c_uint32),
        ("cntUsage", C.c_uint32),
        ("th32ProcessID", C.c_uint32),
        ("th32DefaultHeapID", C.c_size_t),
        ("th32ModuleID", C.c_uint32),
        ("cntThreads", C.c_uint32),
        ("th32ParentProcessID", C.c_uint32),
        ("pcPriClassBase", C.c_long),
        ("dwFlags", C.c_uint32),
        ("szExeFile", C.c_wchar * MAX_PATH),
    ]


@dataclass
class ClientRecord:
    ordinal: int
    pid: int
    created_ticks: int
    created_text: str
    hwnd: int
    thread_id: int
    window_title: str
    window_class: str
    visible: bool
    injected: bool
    character_name: str
    player_object: int
    identity_error: int
    query: Optional[dict]
    probe_error: str

    @property
    def display_name(self) -> str:
        if self.character_name:
            return self.character_name
        if self.window_title:
            return self.window_title
        return f"pid {self.pid}"


@dataclass
class PythonInterpreter:
    path: str
    source: str
    is_x86: bool
    bits: int


def filetime_to_ticks(ft: FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def ticks_to_text(ticks: int) -> str:
    if ticks <= 0:
        return "unknown"
    unix_seconds = (ticks - 116444736000000000) / 10000000.0
    return datetime.fromtimestamp(unix_seconds).strftime("%Y-%m-%d %H:%M:%S")


def root_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.abspath(os.path.join(here, os.pardir, os.pardir)),
        os.path.abspath(os.path.join(here, os.pardir)),
    )
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "README.md")):
            return candidate
    return candidates[0]


def package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_dll_path() -> str:
    bundled_dll = os.path.join(root_dir(), "bin", "SimKeysHook2.dll")
    if os.path.isfile(bundled_dll):
        return bundled_dll
    return os.path.join(root_dir(), "src", "native", "SimKeysHook2", "Release", "SimKeysHook2.dll")


def _probe_python_bits(path: str) -> Optional[int]:
    try:
        completed = subprocess.run(
            [path, "-c", "import ctypes; print(ctypes.sizeof(ctypes.c_void_p))"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None

    if completed.returncode != 0:
        return None

    text = (completed.stdout or "").strip()
    if text == "4":
        return 4
    if text == "8":
        return 8
    return None


def resolve_python_interpreter(preferred_path: Optional[str] = None, require_x86: bool = False) -> PythonInterpreter:
    candidates = []
    seen = set()

    def add_candidate(path: Optional[str], source: str):
        if not path:
            return
        normalized = os.path.abspath(path)
        key = normalized.lower()
        if key in seen:
            return
        if not os.path.exists(normalized):
            return
        seen.add(key)
        candidates.append((normalized, source))

    add_candidate(preferred_path, "explicit")
    add_candidate(sys.executable, "current-process")

    py_launcher = shutil.which("py")
    if py_launcher:
        try:
            completed = subprocess.run([py_launcher, "-0p"], capture_output=True, text=True, timeout=5, check=False)
            if completed.returncode == 0:
                for raw_line in (completed.stdout or "").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    match = re.match(r"^\s*-V:\S+\s+\*?\s*(.+python(?:w)?\.exe)\s*$", line, re.IGNORECASE)
                    if match:
                        add_candidate(match.group(1).strip(), "py-launcher")
        except Exception:
            pass

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for candidate in (
        os.path.join(program_files, "Python313", "python.exe"),
        os.path.join(program_files, "Python312", "python.exe"),
        os.path.join(program_files, "Python311", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python313", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python311", "python.exe"),
        os.path.join(program_files_x86, "Python313-32", "python.exe"),
        os.path.join(program_files_x86, "Python312-32", "python.exe"),
        os.path.join(program_files_x86, "Python311-32", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python313-32", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python312-32", "python.exe"),
        os.path.join(local_appdata, "Programs", "Python", "Python311-32", "python.exe"),
    ):
        add_candidate(candidate, "common-python")

    resolved = []
    for path, source in candidates:
        bits = _probe_python_bits(path)
        if bits is None:
            continue
        resolved.append(PythonInterpreter(path=path, source=source, is_x86=(bits == 4), bits=bits * 8))

    if require_x86:
        for interpreter in resolved:
            if interpreter.is_x86:
                return interpreter
        raise RuntimeError("Could not find a Python interpreter matching the requested bitness.")

    if resolved:
        return resolved[0]
    raise RuntimeError("Could not find a usable Python interpreter.")


def enumerate_nwmain_pids(process_name: str = "nwmain.exe") -> List[int]:
    k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    k32.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = W.BOOL
    k32.Process32NextW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = W.BOOL
    k32.CloseHandle.argtypes = [W.HANDLE]
    k32.CloseHandle.restype = W.BOOL

    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, 0, INVALID_HANDLE_VALUE):
        raise OSError(simkeys.winerr("CreateToolhelp32Snapshot failed"))

    target = process_name.lower()
    results = []
    entry = PROCESSENTRY32W()
    entry.dwSize = C.sizeof(entry)
    try:
        ok = k32.Process32FirstW(snapshot, C.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                results.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snapshot, C.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return results


def get_process_creation_ticks(pid: int) -> int:
    k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k32.OpenProcess.restype = W.HANDLE
    k32.GetProcessTimes.argtypes = [
        W.HANDLE,
        C.POINTER(FILETIME),
        C.POINTER(FILETIME),
        C.POINTER(FILETIME),
        C.POINTER(FILETIME),
    ]
    k32.GetProcessTimes.restype = W.BOOL
    k32.CloseHandle.argtypes = [W.HANDLE]
    k32.CloseHandle.restype = W.BOOL

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle in (None, 0, INVALID_HANDLE_VALUE):
        return 0

    created = FILETIME()
    exited = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    try:
        if not k32.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            return 0
        return filetime_to_ticks(created)
    finally:
        k32.CloseHandle(handle)


def get_window_info(pid: int):
    u32.EnumWindows.argtypes = [C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM), W.LPARAM]
    u32.EnumWindows.restype = W.BOOL
    u32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    u32.GetWindowThreadProcessId.restype = W.DWORD
    u32.GetWindow.argtypes = [W.HWND, W.UINT]
    u32.GetWindow.restype = W.HWND
    u32.IsWindowVisible.argtypes = [W.HWND]
    u32.IsWindowVisible.restype = W.BOOL
    u32.GetWindowTextLengthW.argtypes = [W.HWND]
    u32.GetWindowTextLengthW.restype = C.c_int
    u32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    u32.GetWindowTextW.restype = C.c_int
    u32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    u32.GetClassNameW.restype = C.c_int

    best_visible = None
    best_any = None

    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def enum_proc(hwnd, lparam):
        nonlocal best_visible, best_any
        owner = u32.GetWindow(hwnd, 4)
        if owner:
            return True

        proc_id = W.DWORD()
        thread_id = int(u32.GetWindowThreadProcessId(hwnd, C.byref(proc_id)))
        if int(proc_id.value) != pid:
            return True

        title_len = int(u32.GetWindowTextLengthW(hwnd))
        title_buf = C.create_unicode_buffer(max(title_len + 1, 1))
        u32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        class_buf = C.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, class_buf, len(class_buf))

        info = {
            "hwnd": int(hwnd),
            "thread_id": thread_id,
            "title": title_buf.value,
            "class_name": class_buf.value,
            "visible": bool(u32.IsWindowVisible(hwnd)),
        }

        if best_any is None:
            best_any = info
        if info["visible"] and best_visible is None:
            best_visible = info
        return True

    u32.EnumWindows(enum_proc, 0)
    return best_visible or best_any or {
        "hwnd": 0,
        "thread_id": 0,
        "title": "",
        "class_name": "",
        "visible": False,
    }


def probe_client(pid: int, pipe_timeout_ms: int = 125) -> ClientRecord:
    created_ticks = get_process_creation_ticks(pid)
    window = get_window_info(pid)
    injected = False
    character_name = ""
    player_object = 0
    identity_error = 0
    query = None
    probe_error = ""

    try:
        pipe = simkeys.Pipe(pid, timeout_ms=pipe_timeout_ms)
        try:
            query = simkeys.query_state(pipe)
            injected = True
            character_name = query.get("character_name", "")
            player_object = int(query.get("player_object", 0))
            identity_error = int(query.get("identity_error", 0))
        finally:
            pipe.close()
    except Exception as exc:
        probe_error = str(exc)

    return ClientRecord(
        ordinal=0,
        pid=pid,
        created_ticks=created_ticks,
        created_text=ticks_to_text(created_ticks),
        hwnd=window["hwnd"],
        thread_id=window["thread_id"],
        window_title=window["title"],
        window_class=window["class_name"],
        visible=window["visible"],
        injected=injected,
        character_name=character_name,
        player_object=player_object,
        identity_error=identity_error,
        query=query,
        probe_error=probe_error,
    )


def discover_clients(process_name: str = "nwmain.exe", pipe_timeout_ms: int = 125) -> List[ClientRecord]:
    records = [probe_client(pid, pipe_timeout_ms=pipe_timeout_ms) for pid in enumerate_nwmain_pids(process_name)]
    records.sort(key=lambda item: (item.created_ticks or (1 << 63), item.pid))
    for index, record in enumerate(records, start=1):
        record.ordinal = index
    return records


def format_client_line(record: ClientRecord) -> str:
    injected_text = "yes" if record.injected else "no"
    name = record.character_name or "-"
    title = record.window_title or "-"
    return (
        f"[{record.ordinal}] pid={record.pid} injected={injected_text} "
        f"name={name!r} title={title!r} started={record.created_text}"
    )


def find_uninjected_client(records: List[ClientRecord], skip: int = 0) -> Optional[ClientRecord]:
    candidates = [record for record in records if not record.injected]
    if skip < 0 or skip >= len(candidates):
        return None
    return candidates[skip]


def find_injected_clients(records: List[ClientRecord]) -> List[ClientRecord]:
    return [record for record in records if record.injected]


def resolve_client_selector(records: List[ClientRecord], selector: Optional[str], require_injected: bool = True) -> ClientRecord:
    candidates = [record for record in records if record.injected] if require_injected else list(records)
    if not candidates:
        state = "injected" if require_injected else "running"
        raise RuntimeError(f"No {state} NWN clients were found.")

    if selector is None or str(selector).strip() == "":
        if len(candidates) == 1:
            return candidates[0]
        options = ", ".join(str(record.ordinal) for record in candidates)
        raise RuntimeError(f"Multiple candidate clients were found. Choose one of: {options}")

    text = str(selector).strip()
    if text.lower() == "next":
        next_record = find_uninjected_client(records)
        if next_record is None:
            raise RuntimeError("No uninjected NWN clients were found.")
        return next_record

    if text.isdigit():
        value = int(text)
        ordinal_matches = [record for record in candidates if record.ordinal == value]
        if len(ordinal_matches) == 1:
            return ordinal_matches[0]
        pid_matches = [record for record in candidates if record.pid == value]
        if len(pid_matches) == 1:
            return pid_matches[0]

    lower = text.lower()
    exact_matches = [
        record for record in candidates
        if record.character_name.lower() == lower or record.window_title.lower() == lower
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise RuntimeError(f"Selector {selector!r} matched multiple clients exactly.")

    partial_matches = [
        record for record in candidates
        if lower in record.character_name.lower() or lower in record.window_title.lower()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]
    if len(partial_matches) > 1:
        raise RuntimeError(f"Selector {selector!r} matched multiple clients.")

    raise RuntimeError(f"Could not resolve client selector {selector!r}.")


def open_pipe(record_or_pid, timeout_ms: int = 2000):
    pid = record_or_pid.pid if isinstance(record_or_pid, ClientRecord) else int(record_or_pid)
    return simkeys.Pipe(pid, timeout_ms=timeout_ms)


def query_client(record_or_pid):
    pipe = open_pipe(record_or_pid)
    try:
        return simkeys.query_state(pipe)
    finally:
        pipe.close()


def trigger_slot(record_or_pid, slot: int, page: int = 0):
    if slot < 1 or slot > 12:
        raise RuntimeError("slot must be between 1 and 12")
    if page < 0 or page > 2:
        raise RuntimeError("page must be between 0 and 2")
    pipe = open_pipe(record_or_pid)
    try:
        if page == 0:
            _, data = pipe.xfer(simkeys.OP_SLOT, struct.pack("i", slot))
        else:
            _, data = pipe.xfer(simkeys.OP_SLOT_PAGE, struct.pack("ii", slot, page))
        success, vk, rc, aux_rc, err, path = struct.unpack("iiiiii", data)
        return {
            "success": success,
            "vk": vk,
            "rc": rc,
            "aux_rc": aux_rc,
            "err": err,
            "path": path,
            "page": page,
        }
    finally:
        pipe.close()


def send_chat(record_or_pid, text: str, mode: int = 2):
    pipe = open_pipe(record_or_pid)
    try:
        return simkeys.chat_send(pipe, text, mode)
    finally:
        pipe.close()


def inject_client(record: ClientRecord, dll_path: str, export_name: str = "InitSimKeys", python_path: Optional[str] = None):
    dll_path = os.path.abspath(dll_path)
    if python_path is None or os.path.abspath(python_path).lower() == os.path.abspath(sys.executable).lower():
        return inject_simkeys.inject_and_init(record.pid, dll_path, export_name)

    interpreter = resolve_python_interpreter(preferred_path=python_path, require_x86=False)
    script_path = os.path.join(package_dir(), "inject_simkeys.py")
    completed = subprocess.run(
        [
            interpreter.path,
            script_path,
            "--pid", str(record.pid),
            "--dll", dll_path,
            "--export", export_name,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        parts = [f"Injection failed for pid {record.pid} with exit code {completed.returncode}."]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(stderr)
        raise RuntimeError("\n".join(parts))

    base = 0
    func = 0
    match = re.search(r"HMODULE=0x([0-9A-Fa-f]+).+?\((0x[0-9A-Fa-f]+)\)", stdout)
    if match:
        base = int(match.group(1), 16)
        func = int(match.group(2), 16)
    return base, func
