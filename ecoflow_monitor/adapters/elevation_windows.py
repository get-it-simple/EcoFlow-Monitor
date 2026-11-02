import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import psutil


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE)]


def kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def close_handle(handle):
    api = kernel32()
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle(handle)


def pipe_peer(connection, server: bool) -> int:
    function = getattr(kernel32(), "GetNamedPipeServerProcessId" if server else "GetNamedPipeClientProcessId")
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not function(connection.fileno(), ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pid.value


def is_helper_process(actual: int, launched: int) -> bool:
    if actual == launched:
        return True
    if not getattr(sys, "frozen", False):
        return False
    try:
        process = psutil.Process(actual)
        return process.ppid() == launched and Path(process.exe()).resolve() == Path(sys.executable).resolve()
    except (psutil.Error, OSError):
        return False


def connect_pipe(address):
    from multiprocessing.connection import PipeConnection
    api = kernel32()
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    handle = api.CreateFileW(address, 0xC0000000, 0, None, 3, 0x40000000 | 0x100000 | 0x10000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    api.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    api.SetNamedPipeHandleState.restype = wintypes.BOOL
    mode = wintypes.DWORD(2)
    if not api.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
        error = ctypes.WinError(ctypes.get_last_error())
        close_handle(handle)
        raise error
    return PipeConnection(handle)


def launch_helper(address: str, parent: int) -> tuple[int, int]:
    executable = sys.executable
    arguments = [] if getattr(sys, "frozen", False) else ["-I", str(Path(__file__).parents[1] / "elevated_helper.py")]
    arguments += ["--elevated-helper", address, str(parent)]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(Path(executable).parent)
    info.nShow = 0
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    api = kernel32()
    api.GetProcessId.argtypes = [wintypes.HANDLE]
    api.GetProcessId.restype = wintypes.DWORD
    return info.hProcess, api.GetProcessId(info.hProcess)


@lru_cache(maxsize=64)
def requires_administrator(path: str, modified: int) -> bool:
    if sys.platform != "win32":
        return False
    api = kernel32()
    api.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    api.LoadLibraryExW.restype = wintypes.HMODULE
    api.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    api.FindResourceW.restype = wintypes.HANDLE
    api.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    api.SizeofResource.restype = wintypes.DWORD
    api.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    api.LoadResource.restype = wintypes.HANDLE
    api.LockResource.argtypes = [wintypes.HANDLE]
    api.LockResource.restype = ctypes.c_void_p
    api.FreeLibrary.argtypes = [wintypes.HMODULE]
    module = api.LoadLibraryExW(path, None, 0x22)
    if not module:
        return False
    try:
        resource = api.FindResourceW(module, 1, 24)
        if not resource:
            return False
        size = api.SizeofResource(module, resource)
        pointer = api.LockResource(api.LoadResource(module, resource))
        if not pointer or not 0 < size <= 1048576:
            return False
        root = ET.fromstring(ctypes.string_at(pointer, size))
        return any(element.tag.endswith("requestedExecutionLevel") and element.get("level") == "requireAdministrator" for element in root.iter())
    except ET.ParseError:
        return False
    finally:
        api.FreeLibrary(module)
