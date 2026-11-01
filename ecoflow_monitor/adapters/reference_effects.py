import ctypes
from ctypes import wintypes
import sys

from PySide6.QtWidgets import QWidget

WM_DWMCOLORIZATIONCOLORCHANGED = 0x320

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

DEFAULT_ACCENT_COLOR = "#3a96dd"


def get_accent_color() -> str:
    if sys.platform != "win32":
        return DEFAULT_ACCENT_COLOR

    try:
        dwmapi = ctypes.windll.dwmapi
        color = ctypes.c_uint32()
        opaque_blend = ctypes.c_int()
        result = dwmapi.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque_blend))
        if result != 0:
            return DEFAULT_ACCENT_COLOR

        argb = color.value
        red = (argb >> 16) & 0xFF
        green = (argb >> 8) & 0xFF
        blue = argb & 0xFF
        return f"#{red:02x}{green:02x}{blue:02x}"
    except OSError:
        return DEFAULT_ACCENT_COLOR


def raise_without_activating(window: QWidget) -> None:
    if sys.platform != "win32":
        return

    try:
        set_window_pos = ctypes.windll.user32.SetWindowPos
        set_window_pos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        set_window_pos.restype = wintypes.BOOL
        set_window_pos(int(window.winId()), HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    except OSError:
        pass
