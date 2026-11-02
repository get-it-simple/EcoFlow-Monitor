import pytest
from PySide6.QtCore import QFile

from ecoflow_monitor.adapters.executables import InvalidExecutable, local_executable
from ecoflow_monitor.adapters.hotkeys import parse_combo, validate_bindings
from ecoflow_monitor.settings import HotkeyBinding


def test_real_shortcut_unicode_and_duplicate_targets(tmp_path, qapp):
    executable = tmp_path / "Застосунок.exe"
    executable.write_bytes(b"synthetic")
    shortcut = tmp_path / "Мій ярлик.lnk"
    assert QFile(str(executable)).link(str(shortcut))
    before = shortcut.read_bytes()
    assert local_executable(str(shortcut)) == local_executable(str(executable))
    assert shortcut.read_bytes() == before


@pytest.mark.parametrize("path", [r"\\server\share\App.exe", r"\\?\C:\App.exe", "relative.exe", "", r"\Device\Harddisk0\App.exe"])
def test_invalid_executable_paths_are_rejected(path):
    with pytest.raises((InvalidExecutable, OSError)):
        local_executable(path)


def test_shortcut_to_nonexecutable_is_rejected(tmp_path, qapp):
    target = tmp_path / "data.txt"
    target.write_text("test", encoding="utf-8")
    shortcut = tmp_path / "bad.lnk"
    assert QFile(str(target)).link(str(shortcut))
    with pytest.raises(InvalidExecutable):
        local_executable(str(shortcut))


def test_hotkey_order_conflict_and_native_combinations():
    assert parse_combo("CTRL+alt+Num5") == (3, 101)
    assert parse_combo("alt+ctrl+num5") == (3, 101)
    with pytest.raises(ValueError):
        validate_bindings((HotkeyBinding("a", "ctrl+alt+num5", True), HotkeyBinding("b", "alt+ctrl+num5", True)))
    with pytest.raises(ValueError):
        parse_combo("ctrl+ctrl+a")


def test_thread_hotkey_dispatcher_event_routes_registered_action(qapp):
    import ctypes
    from ctypes import wintypes
    from ecoflow_monitor.adapters.hotkeys import HotkeyManager
    manager = HotkeyManager()
    received = []
    manager.triggered.connect(received.append)
    manager._bindings[123] = "toggle_monitor"
    message = wintypes.MSG()
    message.message = 0x0312
    message.wParam = 123
    try:
        assert manager.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) == (True, 0)
        assert received == ["toggle_monitor"]
    finally:
        manager._bindings.clear()
        manager.stop()
