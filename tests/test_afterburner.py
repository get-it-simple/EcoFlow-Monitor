from pathlib import Path

import pytest
from PySide6.QtCore import QFile

from ecoflow_monitor.adapters.afterburner import match_profile, read_profiles
from ecoflow_monitor.adapters.executables import InvalidExecutable, local_executable
from ecoflow_monitor.adapters.hotkeys import parse_combo, validate_bindings
from ecoflow_monitor.domain import ProfileDefinition
from ecoflow_monitor.settings import HotkeyBinding


def test_profile_discovery_reads_gpu_sections_and_preserves_file(tmp_path):
    path = tmp_path / "VEN_TEST.cfg"
    content = "[Profile1]\nPowerLimit=100\nCoreClock=1500\nMemoryClock=7000\n[Profile5]\nPowerLimit=51\nCoreClock=1400\nMemoryClock=7000\n"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    profiles = read_profiles(path)
    assert [(profile.slot, profile.power_pct) for profile in profiles] == [(1, 100), (5, 51)]
    assert path.read_bytes() == original
    assert match_profile(profiles, {"powerlimit": 51, "coreclock": 1400, "memoryclock": 7000}) == 5


def test_unknown_curve_or_ambiguous_match_cannot_confirm_slot():
    settings = (("powerlimit", "100"), ("coreclock", "1500"), ("memoryclock", "7000"))
    live = {"powerlimit": 100, "coreclock": 1500, "memoryclock": 7000}
    profiles = (ProfileDefinition(1, "", 100, settings), ProfileDefinition(2, "", 100, settings))
    assert match_profile(profiles, live) is None
    assert match_profile((ProfileDefinition(1, "", 100, (*settings, ("vfcurve", "unknown"))),), live) is None
    assert match_profile((ProfileDefinition(1, "", 100),), live) is None


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
