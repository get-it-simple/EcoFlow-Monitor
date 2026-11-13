from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ecoflow_monitor.settings import SettingsStore
from scripts.installer_smoke_settings import create_settings, verify_settings


@pytest.fixture
def migrated_settings(tmp_path):
    path = tmp_path / "running-settings.json"
    create_settings(path)
    store = SettingsStore(path)
    store.save(store.load())
    return path


def test_installer_fixture_migrates_without_losing_preferences(tmp_path):
    path = tmp_path / "running-settings.json"
    create_settings(path)
    original = path.read_bytes()
    legacy = json.loads(original)
    assert legacy["version"] == 2
    assert legacy["hotkeys"] == []
    store = SettingsStore(path)
    loaded = store.load()
    assert path.read_bytes() == original
    assert loaded.language == "uk"
    assert loaded.credential_ref == "EcoFlowMonitor/installer-smoke"
    assert loaded.integrations == ()
    assert [(binding.action, binding.combo, binding.enabled) for binding in loaded.hotkeys] == [
        ("open_dashboard", "", False), ("recover_widgets", "ctrl+alt+w", True),
    ]
    store.save(loaded)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 4
    assert saved["ecoflow"]["serial"] == ""
    assert saved["ecoflow"]["capacity_wh"] == 3072
    assert saved["ecoflow"]["manual_min"] == 15
    assert saved["ecoflow"]["manual_max"] == 85
    assert saved["appearance"]["scale"] == 0.75
    assert saved["appearance"]["animations"] is False
    assert saved["appearance"]["transparency"] is False
    verify_settings(path)


def test_installer_check_allows_window_geometry_saves(migrated_settings):
    store = SettingsStore(migrated_settings)
    store.save(replace(store.load(), monitor_geometry=(10, 20, 640, 400),
                       dashboard_geometry=(30, 40, 1184, 780), mini_position=(50, 60)))
    verify_settings(migrated_settings)


@pytest.mark.parametrize(("field", "value"), [
    ("version", 2), ("version", 3.0), ("language", "en"), ("integrations", [{}]),
    ("hotkeys", []), ("hotkeys.0.action", "toggle_monitor"),
    ("hotkeys.1.combo", "ctrl+alt+x"), ("hotkeys.1.enabled", False),
    ("ecoflow.serial", "changed"), ("ecoflow.credential_ref", "EcoFlowMonitor/changed"),
    ("ecoflow.capacity_wh", 2048), ("ecoflow.manual_min", 20), ("ecoflow.manual_max", 90),
    ("appearance.scale", 1), ("appearance.animations", True), ("appearance.transparency", True),
    ("appearance.animations", 0), ("appearance.scale", "0.75"), ("ecoflow.manual_min", None),
])
def test_installer_check_rejects_changed_preferences(migrated_settings, field, value):
    saved = json.loads(migrated_settings.read_text(encoding="utf-8"))
    parts = field.split(".")
    group = saved
    for part in parts[:-1]:
        group = group[int(part)] if isinstance(group, list) else group[part]
    group[parts[-1]] = value
    migrated_settings.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match=parts[-1]):
        verify_settings(migrated_settings)


@pytest.mark.parametrize("field", ["language", "ecoflow.manual_min", "appearance.animations"])
def test_installer_check_rejects_missing_preferences(migrated_settings, field):
    saved = json.loads(migrated_settings.read_text(encoding="utf-8"))
    parts = field.split(".")
    group = saved
    for part in parts[:-1]:
        group = group[part]
    del group[parts[-1]]
    migrated_settings.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match=field + ": required preference is missing"):
        verify_settings(migrated_settings)


@pytest.mark.parametrize("content", [b"broken", b"\xff", b"[]", b"null", b'{"ecoflow": []}'])
def test_installer_check_rejects_unreadable_or_invalid_settings(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        verify_settings(path)


def test_installer_check_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="file was removed"):
        verify_settings(tmp_path / "missing.json")


def test_installer_check_cli_reports_stage_and_field(tmp_path):
    path = tmp_path / "settings with spaces.json"
    command = [sys.executable, "-m", "scripts.installer_smoke_settings"]
    root = Path(__file__).resolve().parents[1]
    created = subprocess.run([*command, "create", str(path)], cwd=root, capture_output=True, text=True, timeout=30)
    assert created.returncode == 0, created.stderr
    store = SettingsStore(path)
    store.save(store.load())
    verified = subprocess.run([*command, "verify", str(path)], cwd=root, capture_output=True, text=True, timeout=30)
    assert verified.returncode == 0, verified.stderr
    store.save(replace(store.load(), language="en"))
    failed = subprocess.run([*command, "verify", str(path), "--stage", "Running upgrade"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert failed.returncode == 1
    assert "Running upgrade: settings.language: preference changed" in failed.stderr
    assert "Traceback" not in failed.stderr
