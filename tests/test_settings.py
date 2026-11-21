from dataclasses import replace
import json

import pytest

from ecoflow_monitor.integrations import IntegrationRule
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore


def test_settings_roundtrip_preserves_order_and_does_not_contain_credentials(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = replace(AppSettings(), language="uk", integrations=(replace(AppSettings().integrations[0], enabled=True, system_rules=(IntegrationRule("b", "profile_5", max_soc=20), IntegrationRule("a", "profile_2", min_soc=20))),), mini_metrics=("output_power", "battery_level"))
    store.save(settings)
    assert store.load() == settings
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 4
    assert payload["integrations"][0]["system_rules"][0]["id"] == "b"
    assert set(payload["ecoflow"]) == {"serial", "credential_ref", "credential_source", "onepassword", "device_support", "capacity_wh", "manual_min", "manual_max", "polling"}


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
    assert store.load().integrations[0].enabled is False


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
    assert settings.to_dict()["version"] == 4


def test_background_opacity_defaults_for_existing_settings():
    payload = AppSettings().to_dict()
    payload["version"] = 2
    del payload["appearance"]["background_opacity"]
    assert AppSettings.from_dict(payload).background_opacity == 140 / 255


@pytest.mark.parametrize("opacity", [0, 0.25, 1])
def test_background_opacity_persists(tmp_path, opacity):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), background_opacity=opacity))
    assert store.load().background_opacity == opacity


@pytest.mark.parametrize("opacity", [-0.01, 1.01, True, None, "0.5", float("nan"), float("inf")])
def test_invalid_background_opacity_is_rejected(opacity):
    payload = AppSettings().to_dict()
    payload["appearance"]["background_opacity"] = opacity
    with pytest.raises(ValueError):
        AppSettings.from_dict(payload)


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
    for profile in payload["integrations"][0]["profiles"]:
        profile["power_pct"] = {1: 100, 2: 95, 3: 90, 4: 85, 5: 51}[profile["slot"]]
    payload["integrations"][0]["profiles"].reverse()
    settings = AppSettings.from_dict(payload)
    assert [(profile.slot, profile.power_pct) for profile in sorted(settings.integrations[0].profiles, key=lambda item: item.slot)] == [(1, 100), (2, 95), (3, 90), (4, 85), (5, 51)]


@pytest.fixture
def packaged_paths(tmp_path, monkeypatch):
    import sys

    binary = tmp_path / "application"
    binary.mkdir()
    local = tmp_path / "user-data"
    monkeypatch.setitem(vars(sys), "frozen", True)
    monkeypatch.setattr(sys, "executable", str(binary / "EcoFlowMonitor.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return binary, local


@pytest.mark.parametrize("installed", [False, True])
def test_installed_settings_use_user_data_even_when_executable_directory_is_writable(packaged_paths, installed):
    from ecoflow_monitor.settings import default_settings_path

    binary, local = packaged_paths
    if installed:
        (binary / ".ecoflow-monitor-installed").write_text("scope=machine\n", encoding="utf-8")
    expected = local / "GetItSimple/EcoFlowMonitor/settings.json" if installed else binary / "settings.json"
    assert default_settings_path() == expected
    assert not local.exists()


def test_portable_unwritable_directory_retains_legacy_fallback(packaged_paths, monkeypatch):
    from ecoflow_monitor.settings import default_settings_path

    _, local = packaged_paths

    def deny_write(**kwargs):
        raise PermissionError("Synthetic write denial")

    monkeypatch.setattr("ecoflow_monitor.settings.tempfile.TemporaryFile", deny_write)
    assert default_settings_path() == local / "EcoFlowMonitor/settings.json"


def test_installed_data_migrates_once_and_keeps_legacy_files(packaged_paths):
    from ecoflow_monitor.settings import default_settings_path

    binary, local = packaged_paths
    (binary / ".ecoflow-monitor-installed").touch()
    legacy = local / "EcoFlowMonitor"
    data = local / "GetItSimple/EcoFlowMonitor"
    files = {
        "settings.json": b'{"version": 2, "language": "uk", "integrations": []}',
        "settings.json.bak": b"previous settings",
        "integration-state.json": b"state",
        "logs/ecoflow-monitor.log": b"log entry",
        "device-profiles/custom.json": b"profile",
    }
    for name, content in files.items():
        path = legacy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert default_settings_path() == data / "settings.json"
    for name, content in files.items():
        assert (data / name).read_bytes() == (legacy / name).read_bytes() == content
    store = SettingsStore(data / "settings.json")
    store.save(replace(store.load(), language="en"))
    assert default_settings_path() == store.path
    assert store.load().language == "en"
    assert (legacy / "settings.json").read_bytes() == files["settings.json"]


def test_failed_installed_data_copy_leaves_no_partial_destination(packaged_paths, monkeypatch):
    from ecoflow_monitor.settings import default_settings_path

    binary, local = packaged_paths
    (binary / ".ecoflow-monitor-installed").touch()
    legacy = local / "EcoFlowMonitor"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_bytes(b"original")
    data = local / "GetItSimple/EcoFlowMonitor"

    def fail_copy(source, destination):
        destination.mkdir()
        (destination / "settings.json").write_bytes(b"partial copy")
        raise PermissionError("Synthetic copy failure")

    with monkeypatch.context() as failed_copy:
        failed_copy.setattr("ecoflow_monitor.settings.shutil.copytree", fail_copy)
        with pytest.raises(SettingsError, match="settings_save_error"):
            default_settings_path()
    assert not list(data.parent.iterdir())
    assert (legacy / "settings.json").read_bytes() == b"original"
    assert default_settings_path().read_bytes() == b"original"
