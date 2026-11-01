import configparser
import ctypes
import math
from ctypes import wintypes
from pathlib import Path
import struct
import subprocess

import psutil

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import AfterburnerObservation, Availability, ProfileDefinition, normalized_executable


COMPARABLE_FIELDS = {
    "coreclock": (4, 1000), "shaderclock": (20, 1000), "memoryclock": (36, 1000),
    "fanspeed": (52, 1), "corevoltage": (72, 1), "memoryvoltage": (88, 1),
    "auxvoltage": (104, 1), "corevoltageboost": (120, 1), "memoryvoltageboost": (136, 1),
    "auxvoltageboost": (152, 1), "powerlimit": (168, 1), "coreclockboost": (184, 1000),
    "memoryclockboost": (200, 1000), "thermallimit": (216, 1),
}


class MemoryRegion(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
    ]


def read_profiles(path: Path) -> tuple[ProfileDefinition, ...]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(path.read_text(encoding="utf-8-sig"))
    profiles = []
    for slot in range(1, 6):
        section = f"Profile{slot}"
        if not parser.has_section(section):
            continue
        values = tuple(sorted((key, value.strip()) for key, value in parser.items(section)))
        try:
            power = parser.getfloat(section, "PowerLimit", fallback=None)
        except ValueError:
            power = None
        if power is not None and (not math.isfinite(power) or not 1 <= power <= 200):
            power = None
        profiles.append(ProfileDefinition(slot, "", power, values, "discovered"))
    return tuple(profiles)


def current_settings(gpu_id: str) -> dict[str, float] | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenFileMappingW.restype = wintypes.HANDLE
    kernel.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    kernel.MapViewOfFile.restype = ctypes.c_void_p
    kernel.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MemoryRegion), ctypes.c_size_t]
    kernel.VirtualQuery.restype = ctypes.c_size_t
    handle = kernel.OpenFileMappingW(4, False, "MACMSharedMemory")
    if not handle:
        if ctypes.get_last_error() == 5:
            raise PermissionError("msi_access_denied")
        return None
    view = None
    try:
        view = kernel.MapViewOfFile(handle, 4, 0, 0, 0)
        if not view:
            raise PermissionError("msi_access_denied")
        region = MemoryRegion()
        if not kernel.VirtualQuery(view, ctypes.byref(region), ctypes.sizeof(region)) or region.RegionSize < 36:
            return None
        header = ctypes.string_at(view, 36)
        signature, version, header_size, count, entry_size = struct.unpack_from("<5I", header)
        if signature != int.from_bytes(b"MCAM", "little") or version < 0x00020003 or version >> 16 != 2:
            return None
        if not 36 <= header_size <= 4096 or not 1 <= count <= 32 or not 3756 <= entry_size <= 65536:
            return None
        total = header_size + count * entry_size
        if total > region.RegionSize:
            return None
        memory = ctypes.string_at(view, total)
        master, flags = struct.unpack_from("<2I", memory, 20)
        for index in range(count):
            entry = memory[header_size + index * entry_size:header_size + (index + 1) * entry_size]
            identifier = entry[3496:3756].split(b"\0", 1)[0].decode("ascii", errors="replace")
            if identifier.casefold() != gpu_id.casefold():
                continue
            values = {key: struct.unpack_from("<i", entry, offset)[0] / divisor for key, (offset, divisor) in COMPARABLE_FIELDS.items()}
            values["_master"] = float(index == master)
            values["_sync_multiple"] = float(count > 1 and bool(flags & 2))
            return values
        return None
    finally:
        if view:
            kernel.UnmapViewOfFile(view)
        kernel.CloseHandle(handle)


def match_profile(profiles: tuple[ProfileDefinition, ...], settings: dict[str, float] | None) -> int | None:
    if not settings:
        return None
    matches = []
    for profile in profiles:
        values = dict(profile.settings)
        if any(key not in COMPARABLE_FIELDS for key in values):
            continue
        if not {"powerlimit", "coreclock", "memoryclock"} <= values.keys():
            continue
        try:
            if all(key in settings and float(value) == settings[key] for key, value in values.items()):
                matches.append(profile.slot)
        except ValueError:
            continue
    return matches[0] if len(matches) == 1 else None


class AfterburnerAdapter:
    def inspect(self, configured: str, gpu_id: str) -> tuple[AfterburnerObservation, frozenset[str]]:
        processes = []
        running = set()
        access_denied = False
        for process in psutil.process_iter(["name", "exe"]):
            try:
                info = process.info
                executable = info.get("exe")
                if executable:
                    running.add(normalized_executable(executable))
                if (info.get("name") or "").casefold() == "msiafterburner.exe":
                    if executable:
                        processes.append(executable)
                    else:
                        access_denied = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        executable = next((path for path in processes if not configured or normalized_executable(path) == normalized_executable(configured)), "")
        if not executable:
            state = Availability.ACCESS_DENIED if access_denied else Availability.NOT_RUNNING
            return AfterburnerObservation(state), frozenset(running)
        try:
            directory = Path(executable).parent / "Profiles"
            gpu_files = tuple(sorted(directory.glob("VEN_*.cfg")))
            ids = tuple(path.stem for path in gpu_files)
            selected = gpu_id or (ids[0] if len(ids) == 1 else "")
            profiles = read_profiles(directory / (selected + ".cfg")) if selected in ids else ()
            values = current_settings(selected) if selected else None
            detail = "msi_choose_gpu" if len(ids) > 1 and not selected else ""
            if values and (not values["_master"] or values["_sync_multiple"]):
                detail = "msi_select_in_afterburner"
            if len(ids) > 1 and selected and values is None:
                detail = "msi_select_in_afterburner"
            return AfterburnerObservation(Availability.READY, profiles, ids, selected, match_profile(profiles, values), detail), frozenset(running)
        except PermissionError:
            return AfterburnerObservation(Availability.ACCESS_DENIED), frozenset(running)
        except (OSError, ValueError, configparser.Error):
            return AfterburnerObservation(Availability.ERROR), frozenset(running)

    def dispatch(self, configured: str, slot: int, gpu_id: str) -> bool:
        if slot not in range(1, 6):
            return False
        observation, running = self.inspect(configured, gpu_id)
        if observation.availability != Availability.READY or observation.detail:
            return False
        if observation.profiles and slot not in {profile.slot for profile in observation.profiles}:
            return False
        path = configured
        if not path:
            path = next((path for path in running if Path(path).name.casefold() == "msiafterburner.exe"), "")
        if not path:
            return False
        try:
            executable = local_executable(path, allow_shortcut=False)
            if normalized_executable(executable) not in running:
                return False
            result = subprocess.run([executable, f"-profile{slot}"], shell=False, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            return result.returncode == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
