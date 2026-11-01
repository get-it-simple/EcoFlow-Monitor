import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from ecoflow_monitor.adapters.reference_effects import apply_acrylic_effect


def internet_unavailable() -> bool:
    if sys.platform != "win32":
        return False
    try:
        flags = wintypes.DWORD()
        wininet = ctypes.WinDLL("wininet")
        wininet.InternetGetConnectedState.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        wininet.InternetGetConnectedState.restype = wintypes.BOOL
        return not bool(wininet.InternetGetConnectedState(ctypes.byref(flags), 0))
    except OSError:
        return False


def acrylic(window: QWidget, enabled: bool) -> bool:
    if sys.platform != "win32" or sys.getwindowsversion().build < 22621:
        return False
    try:
        registry = __import__("winreg")
        with registry.OpenKey(registry.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            if not registry.QueryValueEx(key, "EnableTransparency")[0]:
                enabled = False
    except OSError:
        enabled = False
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        apply_acrylic_effect(window, enabled)
        hwnd = int(window.winId())
        backdrop = ctypes.c_int()
        result = dwm.DwmGetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        return enabled and result == 0 and backdrop.value == 3
    except OSError:
        return False


def raise_without_focus(window: QWidget) -> None:
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    if sys.platform == "win32":
        user = ctypes.WinDLL("user32")
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
