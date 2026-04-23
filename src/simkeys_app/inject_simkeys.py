import argparse, ctypes as C, os, struct, sys, time
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

GetModuleHandleW = k32.GetModuleHandleW
GetModuleHandleW.argtypes = [W.LPCWSTR]
GetModuleHandleW.restype = W.HMODULE
GetProcAddress = k32.GetProcAddress
GetProcAddress.argtypes = [W.HMODULE, W.LPCSTR]
GetProcAddress.restype = W.LPVOID
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
LoadLibraryW = k32.LoadLibraryW
LoadLibraryW.argtypes = [W.LPCWSTR]
LoadLibraryW.restype = W.HMODULE
LoadLibraryA = k32.LoadLibraryA
LoadLibraryA.argtypes = [W.LPCSTR]
LoadLibraryA.restype = W.HMODULE
FreeLibrary  = k32.FreeLibrary
FreeLibrary.argtypes = [W.HMODULE]
FreeLibrary.restype = W.BOOL

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
MEM_COMMIT  = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF

_log_file = None

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
    if os.path.isfile(bundled_dll):
        return bundled_dll
    return os.path.join(repo_root(), "src", "native", "SimKeysHook2", "Release", "SimKeysHook2.dll")

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

def get_export_rva(dll_path, export_name):
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
        return func_rva

    raise OSError(f"Could not find export {export_name} in {dll_path}")

def inject_and_init(pid, path, export_name="InitSimKeys"):
    if C.sizeof(C.c_void_p) != 4:
        raise OSError("This injector must run as 32-bit Python for 32-bit NWN targets.")

    _open_log(pid)
    log(f"opening pid={pid}")
    h = OpenProcess(PROCESS_ACCESS_INJECT, False, pid)
    if not h:
        err = GetLastError()
        if err == 87:
            probe = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not probe:
                raise OSError(f"OpenProcess failed, err=87 (pid {pid} is invalid or the process has already exited)")
            CloseHandle(probe)
            raise OSError(f"OpenProcess failed, err=87 (the pid exists, but the requested injection rights were rejected)")
        raise OSError(f"OpenProcess failed, err={err}")

    use_ansi = all(ord(ch) < 128 for ch in path)
    if use_ansi:
        dll_bytes = (path + "\x00").encode("ascii")
        load_library_name = b"LoadLibraryA"
    else:
        dll_bytes = (path + "\x00").encode("utf-16le")
        load_library_name = b"LoadLibraryW"
    log(f"using {load_library_name.decode('ascii')} with {'ASCII' if use_ansi else 'UTF-16'} DLL path bytes")
    log(f"allocating {len(dll_bytes)} bytes for DLL path")
    addr = VirtualAllocEx(h, None, len(dll_bytes), MEM_RESERVE|MEM_COMMIT, PAGE_READWRITE)
    if not addr: CloseHandle(h); raise OSError(f"VirtualAllocEx failed, err={GetLastError()}")
    nw = C.c_size_t(0)
    log("about to call WriteProcessMemory")
    if not WriteProcessMemory(h, addr, dll_bytes, len(dll_bytes), C.byref(nw)):
        err = GetLastError(); CloseHandle(h); raise OSError(f"WriteProcessMemory failed, err={err}")
    log(f"WriteProcessMemory succeeded, wrote {nw.value} bytes")
    h_kernel32 = GetModuleHandleW("kernel32.dll")
    if not h_kernel32:
        err = GetLastError(); CloseHandle(h); raise OSError(f"GetModuleHandleW(kernel32.dll) failed, err={err}")
    p_loadlib = GetProcAddress(h_kernel32, load_library_name)
    if not p_loadlib:
        err = GetLastError(); CloseHandle(h); raise OSError(f"GetProcAddress({load_library_name.decode('ascii')}) failed, err={err}")
    log(f"{load_library_name.decode('ascii')}={int(p_loadlib):#x}")
    th = CreateRemoteThread(h, None, 0, p_loadlib, addr, 0, None)
    if not th: err = GetLastError(); CloseHandle(h); raise OSError(f"CreateRemoteThread({load_library_name.decode('ascii')}) failed, err={err}")
    WaitForSingleObject(th, INFINITE)
    hmod_remote = W.DWORD(0)
    if not GetExitCodeThread(th, C.byref(hmod_remote)):
        err = GetLastError(); CloseHandle(th); CloseHandle(h); raise OSError(f"GetExitCodeThread({load_library_name.decode('ascii')}) failed, err={err}")
    CloseHandle(th)

    if hmod_remote.value == 0:
        CloseHandle(h)
        raise OSError(f"{load_library_name.decode('ascii')} in remote returned NULL")
    log(f"remote module=0x{hmod_remote.value:08X}")

    # compute RVA of the exported entrypoint without loading the DLL locally.
    rva = get_export_rva(path, export_name)
    remote_func = hmod_remote.value + rva
    log(f"{export_name} rva=0x{rva:08X} remote=0x{remote_func:08X}")

    # call export in remote
    th2 = CreateRemoteThread(h, None, 0, remote_func, None, 0, None)
    if not th2: err = GetLastError(); CloseHandle(h); raise OSError(f"CreateRemoteThread(export) failed, err={err}")
    WaitForSingleObject(th2, INFINITE)
    code = W.DWORD(0)
    if not GetExitCodeThread(th2, C.byref(code)):
        err = GetLastError(); CloseHandle(th2); CloseHandle(h); raise OSError(f"GetExitCodeThread(export) failed, err={err}")
    CloseHandle(th2)
    CloseHandle(h)

    if code.value == 0:
        raise OSError(f"{export_name} returned FALSE in remote")

    log(f"remote init rc=0x{code.value:08X}")
    return hmod_remote.value, remote_func

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--dll", default=default_dll_path())
    ap.add_argument("--export", default="InitSimKeys")
    args = ap.parse_args()
    dll_path = os.path.abspath(args.dll)

    base, func = inject_and_init(args.pid, dll_path, args.export)
    print(f"[+] Injected SimKeysHook2 (HMODULE=0x{base:08X}) and ran {args.export} (0x{func:08X}).")
