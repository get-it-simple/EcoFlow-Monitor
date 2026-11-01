import ctypes
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from ecoflow_monitor.adapters import windows


@pytest.mark.parametrize("enabled,system_enabled,build,failures,expected", [
    (True, True, 22621, (), True),
    (True, True, 22621, (20, 33), True),
    (True, True, 22621, (38,), False),
    (False, True, 22621, (), False),
    (True, False, 22621, (), False),
    (True, True, 19045, (), False),
])
def test_acrylic_result_depends_on_backdrop_and_system_support(qapp, monkeypatch, enabled, system_enabled, build, failures, expected):
    import winreg

    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class SetAttribute:
        def __init__(self):
            self.values = {}

        def __call__(self, hwnd, attribute, pointer, size):
            assert hwnd == 0x100000001
            self.values[attribute] = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value
            return -1 if attribute in failures else 0

    setter = SetAttribute()
    class GetAttribute:
        def __call__(self, hwnd, attribute, pointer, size):
            assert hwnd == 0x100000001
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = setter.values.get(attribute, 1)
            return -1 if attribute in failures else 0
    monkeypatch.setattr("ecoflow_monitor.adapters.windows.sys", SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(build=build)))
    monkeypatch.setattr(winreg, "OpenKey", lambda *args: RegistryKey())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *args: (int(system_enabled), 4))
    monkeypatch.setattr(windows.ctypes, "windll", SimpleNamespace(dwmapi=SimpleNamespace(DwmSetWindowAttribute=setter, DwmGetWindowAttribute=GetAttribute())))
    window = QWidget()
    monkeypatch.setattr(window, "winId", lambda: 0x100000001)
    assert windows.acrylic(window, enabled) is expected
    if build >= 22621:
        assert setter.values[38] == (3 if enabled and system_enabled else 1)
    else:
        assert not setter.values


def test_acrylic_unavailable_library_falls_back(qapp, monkeypatch):
    import winreg
    monkeypatch.setattr("ecoflow_monitor.adapters.windows.sys", SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(build=22621)))
    def unavailable(*args):
        raise OSError("Synthetic unavailable DWM")
    monkeypatch.setattr(winreg, "OpenKey", unavailable)
    monkeypatch.setattr("ecoflow_monitor.adapters.windows.apply_acrylic_effect", unavailable)
    assert windows.acrylic(QWidget(), True) is False
