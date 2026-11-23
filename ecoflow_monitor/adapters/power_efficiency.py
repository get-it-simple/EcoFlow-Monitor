import ctypes
import logging
import sys
from ctypes import wintypes

from ecoflow_monitor.diagnostics import event


IDLE_PRIORITY_CLASS = 0x40
PROCESS_POWER_THROTTLING = 4
EXECUTION_SPEED = 1


class PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("ControlMask", wintypes.DWORD), ("StateMask", wintypes.DWORD)]


def enable_efficiency_mode() -> bool:
    if sys.platform != "win32":
        return False
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        current_process = kernel.GetCurrentProcess
        current_process.argtypes = []
        current_process.restype = wintypes.HANDLE
        process = current_process()
    except (AttributeError, OSError) as error:
        event("efficiency_mode_failed", level=logging.WARNING, operation="current_process", winerror=getattr(error, "winerror", None))
        return False

    priority = _apply(kernel, "SetPriorityClass", [wintypes.HANDLE, wintypes.DWORD], process, IDLE_PRIORITY_CLASS)
    state = PowerThrottlingState(1, EXECUTION_SPEED, EXECUTION_SPEED)
    eco_qos = _apply(kernel, "SetProcessInformation", [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
                     process, PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state))
    return priority and eco_qos


def _apply(kernel, operation, argument_types, *arguments) -> bool:
    try:
        function = getattr(kernel, operation)
        function.argtypes = argument_types
        function.restype = wintypes.BOOL
        if function(*arguments):
            return True
        winerror = ctypes.get_last_error()
    except (AttributeError, OSError) as error:
        winerror = getattr(error, "winerror", None)
    event("efficiency_mode_failed", level=logging.WARNING, operation=operation, winerror=winerror)
    return False
