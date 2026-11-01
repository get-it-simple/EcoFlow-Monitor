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


def raise_without_focus(window: QWidget) -> None:
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    if sys.platform == "win32":
        user = ctypes.WinDLL("user32")
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos(int(window.winId()), wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
