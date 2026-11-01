from dataclasses import replace
import json

import pytest

from ecoflow_monitor.domain import AutomationRule
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore


def test_settings_roundtrip_preserves_order_and_does_not_contain_credentials(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = replace(AppSettings(), language="uk", msi_enabled=True, system_rules=(AutomationRule("b", 5, max_soc=20), AutomationRule("a", 2, min_soc=20)), mini_metrics=("output_power", "battery_level"))
    store.save(settings)
    assert store.load() == settings
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["automation"]["system"][0]["id"] == "b"
    assert set(payload["ecoflow"]) == {"serial", "credential_ref", "capacity_wh", "manual_min", "manual_max"}


def test_invalid_file_is_preserved_until_explicit_save_with_backup(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.path.write_bytes(b"broken original")
    with pytest.raises(SettingsError):
        store.load()
    with pytest.raises(SettingsError):
        store.save(AppSettings())
    assert store.path.read_bytes() == b"broken original"
    store.save(AppSettings(), explicit=True)
    assert store.path.with_suffix(".json.bak").read_bytes() == b"broken original"
    assert store.load().msi_enabled is False


def test_failed_atomic_publish_preserves_previous_file(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings())
    original = store.path.read_bytes()
    def reject(source, destination):
        raise PermissionError("Synthetic write denial")
    monkeypatch.setattr("ecoflow_monitor.settings.os.replace", reject)
    with pytest.raises(SettingsError):
        store.save(replace(store.load(), language="uk"))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".ecoflow-*.tmp"))


def test_version_zero_migrates_without_erasing_values():
    settings = AppSettings.from_dict({"language": "uk", "ui_scale": 1.5})
    assert settings.language == "uk" and settings.scale == 1.5
    assert settings.to_dict()["version"] == 1


@pytest.mark.parametrize("changes", [{"scale": 0}, {"capacity_wh": float("nan")}, {"manual_min": 90, "manual_max": 80}, {"mini_metrics": ()}, {"mini_metrics": ("battery_level", "battery_level")}, {"mini_dock": "invalid"}])
def test_invalid_preferences_are_rejected(changes):
    with pytest.raises(ValueError):
        replace(AppSettings(), **changes).validate()


def test_future_schema_is_not_silently_overwritten(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.path.write_text('{"version": 999}', encoding="utf-8")
    with pytest.raises(SettingsError):
        store.load()
    assert store.path.read_text(encoding="utf-8") == '{"version": 999}'


def test_reordered_json_profiles_keep_their_slot_identity():
    payload = AppSettings().to_dict()
    payload["afterburner"]["profiles"].reverse()
    settings = AppSettings.from_dict(payload)
    assert [(profile.slot, profile.power_pct) for profile in settings.profiles] == [(1, 100), (2, 95), (3, 90), (4, 85), (5, 51)]
