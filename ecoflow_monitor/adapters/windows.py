import ctypes
from ctypes import wintypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


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
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        succeeded = True
        for attribute, value in ((20, 1), (33, 2), (38, 3 if enabled else 1)):
            native = ctypes.c_int(value)
            result = dwm.DwmSetWindowAttribute(int(window.winId()), attribute, ctypes.byref(native), ctypes.sizeof(native))
            succeeded = succeeded and result == 0
        return enabled and succeeded
    except OSError:
        return False


def raise_without_focus(window: QWidget) -> None:
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    if sys.platform == "win32":
        user = ctypes.WinDLL("user32")
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
