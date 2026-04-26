import argparse, ctypes as C, os, struct, time
from ctypes import wintypes as W

k32 = C.WinDLL("kernel32", use_last_error=True)

OpenProcess = k32.OpenProcess
OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
OpenProcess.restype  = W.HANDLE

VirtualAllocEx = k32.VirtualAllocEx
VirtualAllocEx.argtypes = [W.HANDLE, W.LPVOID, C.c_size_t, W.DWORD, W.DWORD]
VirtualAllocEx.restype  = W.LPVOID

WriteProcessMemory = k32.WriteProcessMemory
WriteProcessMemory.argtypes = [W.HANDLE, W.LPVOID, W.LPCVOID, C.c_size_t, C.POINTER(C.c_size_t)]
WriteProcessMemory.restype  = W.BOOL

CreateRemoteThread = k32.CreateRemoteThread
CreateRemoteThread.argtypes = [W.HANDLE, W.LPVOID, C.c_size_t, W.LPVOID, W.LPVOID, W.DWORD, C.POINTER(W.DWORD)]
CreateRemoteThread.restype = W.HANDLE
WaitForSingleObject = k32.WaitForSingleObject
WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
WaitForSingleObject.restype = W.DWORD
GetExitCodeThread   = k32.GetExitCodeThread
GetExitCodeThread.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
GetExitCodeThread.restype = W.BOOL
CloseHandle = k32.CloseHandle
CloseHandle.argtypes = [W.HANDLE]
CloseHandle.restype = W.BOOL
GetLastError = k32.GetLastError
GetLastError.argtypes = []
GetLastError.restype = W.DWORD
PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_ACCESS_INJECT = (
    PROCESS_CREATE_THREAD
    | PROCESS_VM_OPERATION
    | PROCESS_VM_READ
    | PROCESS_VM_WRITE
    | PROCESS_QUERY_INFORMATION
)
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_MODULE_NAME32 = 255
ERROR_BAD_LENGTH = 24
INVALID_HANDLE_VALUE = C.c_void_p(-1).value
IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64
MEM_COMMIT  = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF

CreateToolhelp32Snapshot = k32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
CreateToolhelp32Snapshot.restype = W.HANDLE
Module32FirstW = k32.Module32FirstW
Module32FirstW.argtypes = [W.HANDLE, W.LPVOID]
Module32FirstW.restype = W.BOOL
Module32NextW = k32.Module32NextW
Module32NextW.argtypes = [W.HANDLE, W.LPVOID]
Module32NextW.restype = W.BOOL
IsWow64Process = k32.IsWow64Process
IsWow64Process.argtypes = [W.HANDLE, C.POINTER(W.BOOL)]
IsWow64Process.restype = W.BOOL
IsWow64Process2 = getattr(k32, "IsWow64Process2", None)
if IsWow64Process2 is not None:
    IsWow64Process2.argtypes = [W.HANDLE, C.POINTER(W.WORD), C.POINTER(W.WORD)]
    IsWow64Process2.restype = W.BOOL

_log_file = None


class MODULEENTRY32W(C.Structure):
    _fields_ = [
        ("dwSize", W.DWORD),
        ("th32ModuleID", W.DWORD),
        ("th32ProcessID", W.DWORD),
        ("GlblcntUsage", W.DWORD),
        ("ProccntUsage", W.DWORD),
        ("modBaseAddr", C.c_void_p),
        ("modBaseSize", W.DWORD),
        ("hModule", W.HMODULE),
        ("szModule", W.WCHAR * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", W.WCHAR * W.MAX_PATH),
    ]


Module32FirstW.argtypes = [W.HANDLE, C.POINTER(MODULEENTRY32W)]
Module32NextW.argtypes = [W.HANDLE, C.POINTER(MODULEENTRY32W)]

def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.abspath(os.path.join(here, os.pardir, os.pardir)),
        os.path.abspath(os.path.join(here, os.pardir)),
    )
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "README.md")):
            return candidate
    return candidates[0]

def default_dll_path():
    bundled_dll = os.path.join(repo_root(), "bin", "SimKeysHook2.dll")
    release_dll = os.path.join(repo_root(), "src", "native", "SimKeysHook2", "Release", "SimKeysHook2.dll")
    candidates = [path for path in (bundled_dll, release_dll) if os.path.isfile(path)]
    if candidates:
        return max(candidates, key=lambda path: os.path.getmtime(path))
    return bundled_dll

def _open_log(pid):
    global _log_file
    if _log_file is not None:
        return
    log_dir = os.path.join(repo_root(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"pyinject_{pid}_{stamp}_{os.getpid()}.log")
    _log_file = open(path, "a", encoding="utf-8", buffering=1)
    print(f"[inject] local python injector log={path}", flush=True)
    _log_file.write(f"[inject] local python injector log={path}\n")

def log(message):
    print(f"[inject] {message}", flush=True)
    if _log_file is not None:
        _log_file.write(f"[inject] {message}\n")

def _read_u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]

def _read_u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]

def _machine_pointer_size(machine):
    if machine == IMAGE_FILE_MACHINE_I386:
        return 4
    if machine in (IMAGE_FILE_MACHINE_AMD64, IMAGE_FILE_MACHINE_ARM64):
        return 8
    return 0

def get_process_pointer_size(process_handle):
    if IsWow64Process2 is not None:
        process_machine = W.WORD(0)
        native_machine = W.WORD(0)
        if IsWow64Process2(process_handle, C.byref(process_machine), C.byref(native_machine)):
            if process_machine.value == IMAGE_FILE_MACHINE_UNKNOWN:
                return _machine_pointer_size(native_machine.value) or C.sizeof(C.c_void_p)
            pointer_size = _machine_pointer_size(process_machine.value)
            if pointer_size:
                return pointer_size

    is_wow64 = W.BOOL(False)
    if not IsWow64Process(process_handle, C.byref(is_wow64)):
        raise OSError(f"IsWow64Process failed, err={GetLastError()}")
    if is_wow64.value:
        return 4
    return C.sizeof(C.c_void_p)

def get_pe_pointer_size(dll_path):
    with open(dll_path, "rb") as f:
        data = f.read(0x1000)

    if data[:2] != b"MZ":
        raise OSError(f"{dll_path} is not a PE file")

    pe_off = _read_u32(data, 0x3C)
    needed = pe_off + 4 + 20 + 2
    if len(data) < needed:
        with open(dll_path, "rb") as f:
            data = f.read(needed)

    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise OSError(f"{dll_path} has an invalid PE header")

    file_header_off = pe_off + 4
    machine = _read_u16(data, file_header_off)
    pointer_size = _machine_pointer_size(machine)
    if pointer_size:
        return pointer_size

    optional_off = file_header_off + 20
    magic = _read_u16(data, optional_off)
    if magic == 0x10B:
        return 4
    if magic == 0x20B:
        return 8
    raise OSError(f"{dll_path} has unsupported PE machine 0x{machine:04X} and optional header magic 0x{magic:04X}")

def _iter_process_modules(pid):
    flags = TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
    last_error = 0
    for _attempt in range(8):
        snapshot = CreateToolhelp32Snapshot(flags, pid)
        if snapshot not in (None, 0, INVALID_HANDLE_VALUE):
            break
        last_error = GetLastError()
        if last_error != ERROR_BAD_LENGTH:
            raise OSError(f"CreateToolhelp32Snapshot(modules) failed, err={last_error}")
        time.sleep(0.05)
    else:
        raise OSError(f"CreateToolhelp32Snapshot(modules) failed, err={last_error}")

    entry = MODULEENTRY32W()
    entry.dwSize = C.sizeof(entry)
    try:
        ok = Module32FirstW(snapshot, C.byref(entry))
        while ok:
            yield {
                "name": entry.szModule,
                "path": entry.szExePath,
                "base": int(entry.modBaseAddr or 0),
                "size": int(entry.modBaseSize),
            }
            entry = MODULEENTRY32W()
            entry.dwSize = C.sizeof(entry)
            ok = Module32NextW(snapshot, C.byref(entry))
    finally:
        CloseHandle(snapshot)

def _normalize_module_name(name):
    base = os.path.basename(str(name)).lower()
    if not base.endswith(".dll"):
        base += ".dll"
    return base

def find_remote_module(pid, module_name):
    wanted = _normalize_module_name(module_name)
    wanted_stem = wanted[:-4]
    for module in _iter_process_modules(pid):
        module_base = os.path.basename(module["name"]).lower()
        module_stem = module_base[:-4] if module_base.endswith(".dll") else module_base
        path_base = os.path.basename(module["path"]).lower()
        path_stem = path_base[:-4] if path_base.endswith(".dll") else path_base
        if module_base == wanted or module_stem == wanted_stem or path_base == wanted or path_stem == wanted_stem:
            return module
    raise OSError(f"Could not find {wanted} in pid {pid}")

def get_export(dll_path, export_name):
    with open(dll_path, "rb") as f:
        data = f.read()

    if data[:2] != b"MZ":
        raise OSError(f"{dll_path} is not a PE file")

    pe_off = _read_u32(data, 0x3C)
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise OSError(f"{dll_path} has an invalid PE header")

    file_header_off = pe_off + 4
    num_sections = _read_u16(data, file_header_off + 2)
    size_of_optional_header = _read_u16(data, file_header_off + 16)
    optional_off = file_header_off + 20
    magic = _read_u16(data, optional_off)

    if magic == 0x10B:
        data_dir_off = optional_off + 96
    elif magic == 0x20B:
        data_dir_off = optional_off + 112
    else:
        raise OSError(f"{dll_path} has unsupported PE optional header magic 0x{magic:04X}")

    export_rva = _read_u32(data, data_dir_off)
    export_size = _read_u32(data, data_dir_off + 4)
    if export_rva == 0 or export_size == 0:
        raise OSError(f"{dll_path} does not expose an export table")

    section_table_off = optional_off + size_of_optional_header
    sections = []
    for i in range(num_sections):
        sec_off = section_table_off + i * 40
        virtual_size = _read_u32(data, sec_off + 8)
        virtual_address = _read_u32(data, sec_off + 12)
        raw_size = _read_u32(data, sec_off + 16)
        raw_ptr = _read_u32(data, sec_off + 20)
        sections.append((virtual_address, virtual_size, raw_ptr, raw_size))

    def rva_to_offset(rva):
        for virtual_address, virtual_size, raw_ptr, raw_size in sections:
            size = max(virtual_size, raw_size)
            if virtual_address <= rva < virtual_address + size:
                return raw_ptr + (rva - virtual_address)
        raise OSError(f"RVA 0x{rva:08X} not mapped in {dll_path}")

    export_off = rva_to_offset(export_rva)
    number_of_functions = _read_u32(data, export_off + 20)
    number_of_names = _read_u32(data, export_off + 24)
    address_of_functions = _read_u32(data, export_off + 28)
    address_of_names = _read_u32(data, export_off + 32)
    address_of_name_ordinals = _read_u32(data, export_off + 36)

    functions_off = rva_to_offset(address_of_functions)
    names_off = rva_to_offset(address_of_names)
    ordinals_off = rva_to_offset(address_of_name_ordinals)

    target = export_name.encode("ascii")
    for i in range(number_of_names):
        name_rva = _read_u32(data, names_off + i * 4)
        name_off = rva_to_offset(name_rva)
        end = data.index(b"\x00", name_off)
        if data[name_off:end] != target:
            continue
        ordinal = _read_u16(data, ordinals_off + i * 2)
        if ordinal >= number_of_functions:
            raise OSError(f"Export ordinal {ordinal} for {export_name} is out of range")
        func_rva = _read_u32(data, functions_off + ordinal * 4)
        if export_rva <= func_rva < export_rva + export_size:
            forwarder_off = rva_to_offset(func_rva)
            end = data.index(b"\x00", forwarder_off)
            return None, data[forwarder_off:end].decode("ascii")
        return func_rva, None

    raise OSError(f"Could not find export {export_name} in {dll_path}")

def get_export_rva(dll_path, export_name):
    rva, forwarder = get_export(dll_path, export_name)
    if forwarder:
        raise OSError(f"Export {export_name} in {dll_path} is forwarded to {forwarder}")
    return rva

def _split_forwarder(forwarder):
    module_name, sep, export_name = str(forwarder).rpartition(".")
    if not sep or not module_name or not export_name:
        raise OSError(f"Unsupported export forwarder {forwarder!r}")
    if not module_name.lower().endswith(".dll"):
        module_name += ".dll"
    if export_name.startswith("#"):
        raise OSError(f"Ordinal export forwarder {forwarder!r} is not supported")
    return module_name, export_name

def resolve_remote_export(pid, module_name, export_name, _depth=0):
    if _depth > 8:
        raise OSError(f"Export forwarder chain for {module_name}!{export_name} is too deep")

    try:
        module = find_remote_module(pid, module_name)
    except OSError:
        normalized = _normalize_module_name(module_name)
        if normalized.startswith("api-ms-win-") or normalized.startswith("ext-ms-win-"):
            module = find_remote_module(pid, "kernelbase.dll")
        else:
            raise

    rva, forwarder = get_export(module["path"], export_name)
    if forwarder:
        next_module, next_export = _split_forwarder(forwarder)
        log(f"{module['name']}!{export_name} forwards to {next_module}!{next_export}")
        return resolve_remote_export(pid, next_module, next_export, _depth + 1)

    return module["base"] + rva, module

def _ensure_remote_pointer(value, target_pointer_size, label):
    address = int(value or 0)
    if not address:
        raise OSError(f"{label} resolved to NULL")
    if target_pointer_size == 4 and address > 0xFFFFFFFF:
        raise OSError(f"{label} address 0x{address:X} does not fit in the 32-bit target")
    return address

def _open_process_for_injection(pid):
    h = OpenProcess(PROCESS_ACCESS_INJECT, False, pid)
    if h:
        return h

    err = GetLastError()
    if err == 87:
        probe = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not probe:
            raise OSError(f"OpenProcess failed, err=87 (pid {pid} is invalid or the process has already exited)")
        CloseHandle(probe)
        raise OSError(f"OpenProcess failed, err=87 (the pid exists, but the requested injection rights were rejected)")
    raise OSError(f"OpenProcess failed, err={err}")

def _create_remote_thread_and_wait(process_handle, start_address, parameter, label, target_pointer_size):
    start_address = _ensure_remote_pointer(start_address, target_pointer_size, label)
    if parameter is not None:
        parameter = _ensure_remote_pointer(parameter, target_pointer_size, f"{label} parameter")
    th = CreateRemoteThread(process_handle, None, 0, start_address, parameter, 0, None)
    if not th:
        raise OSError(f"CreateRemoteThread({label}) failed, err={GetLastError()}")
    try:
        WaitForSingleObject(th, INFINITE)
        code = W.DWORD(0)
        if not GetExitCodeThread(th, C.byref(code)):
            raise OSError(f"GetExitCodeThread({label}) failed, err={GetLastError()}")
        return int(code.value)
    finally:
        CloseHandle(th)

def _load_remote_library(process_handle, pid, path, target_pointer_size):
    use_ansi = all(ord(ch) < 128 for ch in path)
    if use_ansi:
        dll_bytes = (path + "\x00").encode("ascii")
        load_library_name = b"LoadLibraryA"
    else:
        dll_bytes = (path + "\x00").encode("utf-16le")
        load_library_name = b"LoadLibraryW"
    log(f"using {load_library_name.decode('ascii')} with {'ASCII' if use_ansi else 'UTF-16'} DLL path bytes")
    log(f"allocating {len(dll_bytes)} bytes for DLL path")
    addr = VirtualAllocEx(process_handle, None, len(dll_bytes), MEM_RESERVE|MEM_COMMIT, PAGE_READWRITE)
    if not addr:
        raise OSError(f"VirtualAllocEx failed, err={GetLastError()}")
    addr = _ensure_remote_pointer(addr, target_pointer_size, "VirtualAllocEx")
    nw = C.c_size_t(0)
    dll_buffer = C.create_string_buffer(dll_bytes, len(dll_bytes))
    log("about to call WriteProcessMemory")
    if not WriteProcessMemory(process_handle, addr, dll_buffer, len(dll_bytes), C.byref(nw)):
        raise OSError(f"WriteProcessMemory failed, err={GetLastError()}")
    log(f"WriteProcessMemory succeeded, wrote {nw.value} bytes")

    load_library_export = load_library_name.decode("ascii")
    p_loadlib, module = resolve_remote_export(pid, "kernel32.dll", load_library_export)
    p_loadlib = _ensure_remote_pointer(p_loadlib, target_pointer_size, load_library_export)
    log(f"remote {module['name']}!{load_library_export}=0x{p_loadlib:08X}")
    hmod_remote = _create_remote_thread_and_wait(
        process_handle,
        p_loadlib,
        addr,
        load_library_export,
        target_pointer_size,
    )

    if hmod_remote == 0:
        raise OSError(f"{load_library_name.decode('ascii')} in remote returned NULL")
    log(f"remote module=0x{hmod_remote:08X}")
    return hmod_remote

def _validate_target_and_dll(process_handle, path):
    target_pointer_size = get_process_pointer_size(process_handle)
    dll_pointer_size = get_pe_pointer_size(path)
    log(
        "bitness: "
        f"injector={C.sizeof(C.c_void_p) * 8}-bit "
        f"target={target_pointer_size * 8}-bit "
        f"dll={dll_pointer_size * 8}-bit"
    )
    if target_pointer_size != dll_pointer_size:
        raise OSError(
            f"Cannot inject a {dll_pointer_size * 8}-bit DLL into a "
            f"{target_pointer_size * 8}-bit process."
        )
    if target_pointer_size != 4:
        raise OSError("SimKeysHook2.dll is intended for the 32-bit NWN Diamond client.")
    return target_pointer_size

def load_remote_library(pid, path):
    _open_log(pid)
    path = os.path.abspath(path)
    log(f"opening pid={pid}")
    h = _open_process_for_injection(pid)
    try:
        target_pointer_size = _validate_target_and_dll(h, path)
        return _load_remote_library(h, pid, path, target_pointer_size)
    finally:
        CloseHandle(h)

def inject_and_init(pid, path, export_name="InitSimKeys"):
    _open_log(pid)
    path = os.path.abspath(path)
    log(f"opening pid={pid}")
    h = _open_process_for_injection(pid)
    try:
        target_pointer_size = _validate_target_and_dll(h, path)
        hmod_remote = _load_remote_library(h, pid, path, target_pointer_size)

        # compute RVA of the exported entrypoint without loading the DLL locally.
        rva = get_export_rva(path, export_name)
        remote_func = hmod_remote + rva
        remote_func = _ensure_remote_pointer(remote_func, target_pointer_size, export_name)
        log(f"{export_name} rva=0x{rva:08X} remote=0x{remote_func:08X}")

        # call export in remote
        code = _create_remote_thread_and_wait(h, remote_func, None, "export", target_pointer_size)
    finally:
        CloseHandle(h)

    if code == 0:
        raise OSError(f"{export_name} returned FALSE in remote")

    log(f"remote init rc=0x{code:08X}")
    return hmod_remote, remote_func

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--dll", default=default_dll_path())
    ap.add_argument("--export", default="InitSimKeys")
    args = ap.parse_args()
    dll_path = os.path.abspath(args.dll)

    base, func = inject_and_init(args.pid, dll_path, args.export)
    print(f"[+] Injected SimKeysHook2 (HMODULE=0x{base:08X}) and ran {args.export} (0x{func:08X}).")
