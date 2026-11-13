from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QFile
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
import pytest

from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation, ProcessSnapshot
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.integrations import CliCommand, IntegrationDefinition, IntegrationProfile, IntegrationRule, Reaction, afterburner_example, template_tokens
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.ui.integrations import IntegrationsEditor, ProfileDialog
from ecoflow_monitor.ui.integration_creation import IntegrationCreationDialog
from ecoflow_monitor.ui.settings_window import SettingsWindow


def module_example(**changes):
    module = IntegrationDefinition(
        "example", "Example", enabled=True, allow_launch=True, executable=sys.executable,
        mapping=(CliCommand("on", "Enable", ("--on",)), CliCommand("off", "Disable", ("--off",))),
        profiles=(
            IntegrationProfile("a", "A", Reaction(True, ("{app} {cli:on}",)), Reaction(True, ("{app} {cli:off}",))),
            IntegrationProfile("b", "B", Reaction(True, ("{app} {cli:on}",)), Reaction(False, ("{app} {cli:off}",))),
        ),
    )
    return replace(module, **changes)


def wait_for(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def controller(tmp_path, qapp):
    instance = Controller(SettingsStore(tmp_path / "settings.json"))
    yield instance
    instance.stop()
    qapp.processEvents()


def test_module_roundtrip_and_stable_mapping_labels():
    module = module_example()
    restored = IntegrationDefinition.from_dict(json.loads(json.dumps(module.to_dict())))
    assert restored == module
    renamed = replace(module, mapping=(replace(module.mapping[0], label="Renamed"), module.mapping[1]))
    assert renamed.expand("{app} {cli:on}", "C:/Test App.exe") == ("C:/Test App.exe", "--on")
    renamed.validate()


@pytest.mark.parametrize("template,expected", [
    ('"{app}" {cli:on} "two words" ""', ("{app}", "{cli:on}", "two words", "")),
    ('{app} "C:\\folder with spaces\\\\"', ("{app}", "C:\\folder with spaces\\")),
    ('{app} "say \\"hello\\""', ("{app}", 'say "hello"')),
    ("{app} Україна & | > %PATH%", ("{app}", "Україна", "&", "|", ">", "%PATH%")),
])
def test_windows_template_tokens(template, expected):
    assert template_tokens(template) == expected


@pytest.mark.parametrize("template", ["", "app.exe --arg", "{app} {unknown}", "{app} x{cli:on}", '{app} "broken', "{app} {app}", "{app}\0"])
def test_invalid_templates_are_rejected(template):
    with pytest.raises(ValueError):
        template_tokens(template)


@pytest.mark.parametrize("changes", [
    {"conditions": ("unknown",)},
    {"conditions": ("soc", "soc")},
    {"mapping": (CliCommand("on", "A", ("x",)), CliCommand("on", "B", ("y",)))},
    {"profiles": (IntegrationProfile("a", "A", Reaction(True, ("{app} {cli:missing}",))),)},
    {"system_rules": (IntegrationRule("r", "missing"),)},
    {"system_rules": (IntegrationRule("r", "a", min_soc=90, max_soc=20),)},
    {"conditions": (), "system_rules": (IntegrationRule("r", "a", min_soc=20),)},
    {"allow_launch": "yes"},
    {"driver": "python"},
])
def test_invalid_module_contracts(changes):
    with pytest.raises((ValueError, TypeError)):
        module_example(**changes).validate()


def test_afterburner_example_preserves_restrictions():
    example = afterburner_example()
    example.validate()
    assert not example.enabled and not example.auto_enabled and not example.allow_launch
    assert [profile.slot for profile in example.profiles] == list(range(1, 6))
    assert all(profile.power_pct is None for profile in example.profiles)
    with pytest.raises(ValueError):
        replace(example, allow_launch=True).validate()
    with pytest.raises(ValueError):
        replace(example, mapping=(replace(example.mapping[0], arguments=("-setpowerlimit", "150")), *example.mapping[1:])).validate()


def test_legacy_settings_migrate_all_msi_values_and_hotkeys():
    payload = {
        "version": 1, "language": "uk", "appearance": {"scale": 1.5},
        "afterburner": {"enabled": True, "auto": True, "executable": "C:/MSI.exe", "gpu_id": "GPU",
                        "profiles": [{"slot": slot, "label": f"Saved {slot}", "power_pct": 80 + slot} for slot in range(5, 0, -1)]},
        "automation": {"applications": [{"id": "app", "slot": 2, "executable": "C:/Game.exe"}],
                       "system": [{"id": "fallback", "slot": 5, "max_soc": 20}]},
        "hotkeys": [{"action": "toggle_auto", "combo": "ctrl+f1", "enabled": True},
                    {"action": "profile_2", "combo": "ctrl+f2", "enabled": True}],
    }
    settings = AppSettings.from_dict(payload)
    module = settings.integrations[0]
    assert module.enabled and module.auto_enabled and module.executable == "C:/MSI.exe" and module.gpu_id == "GPU"
    assert [profile.label for profile in module.profiles] == [f"Saved {slot}" for slot in range(1, 6)]
    assert module.application_rules[0].profile_id == "profile_2"
    assert module.system_rules[0].profile_id == "profile_5"
    assert [binding.action for binding in settings.hotkeys] == ["auto:afterburner", "profile:afterburner:profile_2", "open_dashboard", "recover_widgets"]
    assert settings.scale == 1.5 and settings.language == "uk"
    assert AppSettings.from_dict(settings.to_dict()) == settings


def test_deleted_example_stays_deleted_after_restart(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), integrations=()))
    assert store.load().integrations == ()
    assert store.load().to_dict()["version"] == 4


def test_rules_use_declared_conditions_and_order():
    module = module_example(
        application_rules=(IntegrationRule("app", "a", executable="C:/Game.exe"),),
        system_rules=(IntegrationRule("low", "b", max_soc=20), IntegrationRule("fallback", "a")),
    )
    telemetry = Telemetry(0, 100, 15, 0, 0)
    assert module.select(telemetry, frozenset({"c:/GAME.exe"})).id == "app"
    assert module.select(telemetry, frozenset()).id == "low"
    assert module.select(replace(telemetry, soc_pct=20), frozenset()).id == "fallback"


def test_transition_orders_reactions_and_stops_on_failure(monkeypatch):
    module = module_example()
    adapter = IntegrationAdapter()
    monkeypatch.setattr(adapter, "inspect", lambda selected: IntegrationObservation("integration_ready", executable=sys.executable))
    calls = []
    def run(args, **kwargs):
        calls.append(args[1])
        assert kwargs["shell"] is False and kwargs["timeout"] == 5
        return subprocess.CompletedProcess(args, 0 if len(calls) == 1 else 7)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    target = replace(module.profiles[1], activation=Reaction(True, ("{app} {cli:on}", "{app} {cli:on}")))
    result = adapter.transition(module, module.profiles[0], target, lambda: True)
    assert calls == ["--off", "--on"]
    assert not result.success and result.completed == 1 and result.phase == "activation" and result.step == 1


def test_reaction_switches_and_cancellation(monkeypatch):
    module = module_example()
    adapter = IntegrationAdapter()
    inspected = []
    monkeypatch.setattr(adapter, "inspect", lambda selected: inspected.append(selected) or IntegrationObservation("integration_ready", executable=sys.executable))
    previous = replace(module.profiles[0], deactivation=Reaction(False, ("{app} {cli:off}",)))
    target = replace(module.profiles[1], activation=Reaction(False, ("{app} {cli:on}",)))
    assert adapter.transition(module, previous, target, lambda: True).success
    assert inspected == []
    result = adapter.transition(module, None, module.profiles[0], lambda: False)
    assert not result.success and result.error == "integration_cancelled" and inspected == []


def test_timeout_is_visible(monkeypatch):
    module = module_example()
    adapter = IntegrationAdapter()
    monkeypatch.setattr(adapter, "inspect", lambda selected: IntegrationObservation("integration_ready", executable=sys.executable))
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    result = adapter.transition(module, None, module.profiles[0], lambda: True)
    assert not result.success and result.error == "integration_timeout"


def test_actual_cli_reactions_with_unicode_and_spaces(tmp_path):
    output = tmp_path / "Результат команд.json"
    script = tmp_path / "Synthetic application.py"
    script.write_text("import json, pathlib, sys\np = pathlib.Path(sys.argv[1])\nitems = json.loads(p.read_text(encoding='utf-8')) if p.exists() else []\nitems.append(sys.argv[2:])\np.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')\n", encoding="utf-8")
    module = module_example(mapping=(
        CliCommand("on", "Enable", ("-I", str(script), str(output), "активація", "two words", "&|>")),
        CliCommand("off", "Disable", ("-I", str(script), str(output), "деактивація")),
    ))
    adapter = IntegrationAdapter()
    first = adapter.transition(module, None, module.profiles[0], lambda: True)
    second = adapter.transition(module, module.profiles[0], module.profiles[1], lambda: True)
    assert first.success and second.success
    assert json.loads(output.read_text(encoding="utf-8")) == [["активація", "two words", "&|>"], ["деактивація"], ["активація", "two words", "&|>"]]


def test_launch_policy_and_invalid_path(tmp_path, monkeypatch):
    executable = tmp_path / "App.exe"
    executable.write_bytes(b"synthetic")
    adapter = IntegrationAdapter()
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.running_processes", lambda: ProcessSnapshot())
    module = module_example(executable=str(executable), allow_launch=False)
    assert adapter.inspect(module).availability == "integration_not_running"
    assert adapter.inspect(replace(module, allow_launch=True)).ready
    assert adapter.inspect(replace(module, executable="missing.exe")).availability == "integration_invalid_path"


def test_controller_independent_auto_manual_and_no_repeat(controller: Controller, qapp, monkeypatch):
    first = module_example(auto_enabled=True, system_rules=(IntegrationRule("r", "a"),))
    second = replace(first, id="second", label="Second")
    controller.apply_settings(replace(controller.settings, integrations=(first, second)))
    calls = []
    monkeypatch.setattr(IntegrationAdapter, "inspect", lambda self, module: IntegrationObservation("integration_ready"))
    from ecoflow_monitor.adapters.integrations import TransitionResult
    monkeypatch.setattr(IntegrationAdapter, "transition", lambda self, module, previous, target, current: calls.append((module.id, target.id)) or TransitionResult(True))
    for state in controller.integration_states.values():
        state.observation = IntegrationObservation("integration_ready")
    now = [100.0]
    controller.clock = lambda: now[0]
    controller.telemetry = Telemetry(0, 100, 50, 0, now[0])
    controller.connection = ConnectionState.LIVE
    controller.tick()
    assert calls == []
    now[0] = 105
    controller.tick()
    wait_for(qapp, lambda: not any(state.busy for state in controller.integration_states.values()))
    assert sorted(calls) == [("example", "a"), ("second", "a")]
    controller.request_profile("example", "b")
    wait_for(qapp, lambda: not controller.integration_states["example"].busy)
    assert not controller.module("example").auto_enabled and controller.module("second").auto_enabled
    controller.request_profile("example", "b")
    assert calls.count(("example", "b")) == 1
    controller.apply_settings(replace(controller.settings, integrations=()))
    controller.stop()
    assert len(calls) == 3


def test_settings_change_cancels_later_commands(controller: Controller, qapp, monkeypatch):
    module = module_example()
    module = replace(module, profiles=(replace(module.profiles[0], activation=Reaction(True, ("{app} {cli:on}", "{app} {cli:on}"))),))
    controller.apply_settings(replace(controller.settings, integrations=(module,)))
    state = controller.integration_states[module.id]
    state.observation = IntegrationObservation("integration_ready")
    started = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(IntegrationAdapter, "inspect", lambda self, selected: IntegrationObservation("integration_ready", executable=sys.executable))
    def run(args, **kwargs):
        calls.append(args)
        started.set()
        release.wait(3)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    controller.request_profile(module.id, "a")
    try:
        wait_for(qapp, started.is_set)
        controller.apply_settings(replace(controller.settings, language="uk"))
        release.set()
        wait_for(qapp, lambda: not controller._active)
        state = controller.integration_states[module.id]
        assert len(calls) == 1 and not state.busy and state.active_profile is None
        assert state.result.error == "integration_cancelled"
    finally:
        release.set()


def test_editor_shortcut_and_draft_reset(controller: Controller, tmp_path, qapp):
    executable = tmp_path / "Програма.exe"
    executable.write_bytes(b"synthetic")
    shortcut = tmp_path / "Ярлик.lnk"
    assert QFile(str(executable)).link(str(shortcut))
    dialog = SettingsWindow(controller)
    try:
        editor = dialog.integrations_editor
        editor.path.choose(str(shortcut))
        assert Path(editor.path.text()) == executable
        editor.enabled.setChecked(True)
        assert not controller.settings.integrations[0].enabled
        gathered = dialog.gather()
        assert gathered.integrations[0].enabled and Path(gathered.integrations[0].executable) == executable
        dialog.load_draft()
        assert not editor.enabled.isChecked() and editor.path.text() == ""
    finally:
        dialog.deleteLater()


def test_editor_add_delete_restore_and_duplicate(qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.confirm", lambda *args: True)
    editor = IntegrationsEditor()
    editor.load((afterburner_example(),))
    try:
        editor.remove()
        assert editor.modules() == ()
        creation = IntegrationCreationDialog(editor.modules(), editor)
        creation.use_example()
        creation.save()
        editor.add_module(creation.module)
        creation.deleteLater()
        assert editor.modules()[0] == afterburner_example()
        with pytest.raises(ValueError):
            editor.add_module(afterburner_example())
        editor.add_module(module_example())
        assert len(editor.modules()) == 2
        editor.selector.setCurrentIndex(0)
        assert editor.modules()[1].id == "example"
    finally:
        editor.deleteLater()


def test_profile_editor_reactions_preserve_enabled_states(qapp):
    module = module_example()
    dialog = ProfileDialog(module, module.profiles[0])
    try:
        dialog.reactions["activation"][0].setChecked(False)
        dialog.reactions["deactivation"][0].setChecked(True)
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert not dialog.profile.activation.enabled and dialog.profile.deactivation.enabled
        assert dialog.profile.activation.commands == module.profiles[0].activation.commands
    finally:
        dialog.deleteLater()


def test_profile_technical_details_roundtrip_and_legacy_default():
    module = module_example()
    payload = module.to_dict()
    for profile in payload["profiles"]:
        profile.pop("technical_details")
    restored = IntegrationDefinition.from_dict(payload)
    assert all(profile.technical_details == "" for profile in restored.profiles)
    details = "Battery preset\nGPU limit: 70% <manual> & quiet fans"
    updated = replace(restored, profiles=(replace(restored.profiles[0], technical_details=details),))
    assert IntegrationDefinition.from_dict(json.loads(json.dumps(updated.to_dict()))) == updated
    payload["profiles"][0]["technical_details"] = 12
    with pytest.raises(ValueError, match="technical details"):
        IntegrationDefinition.from_dict(payload)


@pytest.mark.parametrize("driver", ["cli", "afterburner"])
def test_profile_editor_saves_technical_details_for_all_drivers(qapp, driver):
    module = module_example() if driver == "cli" else afterburner_example()
    profile = replace(module.profiles[0], power_pct=85)
    dialog = ProfileDialog(module, profile)
    try:
        dialog.technical_details.setPlainText("Quiet mode\nSaved preset; no GPU observation")
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.profile.technical_details == "Quiet mode\nSaved preset; no GPU observation"
        assert dialog.profile.power_pct == 85
    finally:
        dialog.deleteLater()

@pytest.mark.parametrize("field,value", [
    ("conditions", "soc"),
    ("mapping", {}),
    ("profiles", {}),
    ("application_rules", {}),
])
def test_json_arrays_are_not_silently_coerced(field, value):
    payload = module_example().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        IntegrationDefinition.from_dict(payload)


def test_mapping_arguments_and_reactions_require_json_arrays():
    payload = module_example().to_dict()
    payload["mapping"][0]["arguments"] = "--on"
    with pytest.raises(ValueError):
        IntegrationDefinition.from_dict(payload)
    payload = module_example().to_dict()
    payload["profiles"][0]["activation"]["commands"] = ""
    with pytest.raises(ValueError):
        IntegrationDefinition.from_dict(payload)
    payload = module_example().to_dict()
    payload["profiles"][0]["activation"]["unknown"] = True
    with pytest.raises(ValueError):
        IntegrationDefinition.from_dict(payload)


def test_editor_referenced_items_cannot_be_removed(qapp, monkeypatch):
    messages = []
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.QMessageBox.warning", lambda *args: messages.append(args))
    module = module_example(system_rules=(IntegrationRule("r", "a"),))
    editor = IntegrationsEditor()
    editor.load((module,))
    try:
        editor.mapping.selectRow(0)
        editor.remove_mapping()
        assert len(messages) == 1 and editor.mapping.rowCount() == 2
        editor.profiles.selectRow(0)
        editor.remove_profile()
        assert len(messages) == 2 and len(editor.modules()[0].profiles) == 2
    finally:
        editor.deleteLater()


@pytest.mark.parametrize("table_name", ["mapping", "application_rules", "system_rules"])
@pytest.mark.parametrize("styled", [False, True])
@pytest.mark.design
def test_table_headers_keep_their_appearance_when_rows_are_selected(qapp, table_name, styled):
    from ecoflow_monitor.ui.theme import settings_style

    rules = (IntegrationRule("first", "a"), IntegrationRule("second", "b"))
    editor = IntegrationsEditor()
    editor.load((module_example(application_rules=rules, system_rules=rules),))
    if styled:
        editor.setStyleSheet(settings_style("#3a96dd"))
    table = editor.mapping if table_name == "mapping" else getattr(editor, table_name).table
    editor.body.setCurrentIndex(1 if table_name == "mapping" else 3)
    editor.resize(1200, 1000)
    editor.show()
    try:
        table.setFocus()
        table.clearSelection()
        qapp.processEvents()
        headers = [table.horizontalHeader()]
        if table.verticalHeader().isVisible():
            headers.append(table.verticalHeader())
        assert not table.horizontalHeader().highlightSections()
        assert not table.verticalHeader().highlightSections()
        before = [header.grab().toImage() for header in headers]
        assert all(not image.isNull() for image in before)
        for row in (0, 1):
            table.selectRow(row)
            qapp.processEvents()
            assert [index.row() for index in table.selectionModel().selectedRows()] == [row]
            assert [header.grab().toImage() for header in headers] == before
        table.selectAll()
        qapp.processEvents()
        assert [index.row() for index in table.selectionModel().selectedRows()] == [0, 1]
        assert [header.grab().toImage() for header in headers] == before
        table.clearSelection()
        qapp.processEvents()
        assert not table.selectionModel().hasSelection()
        assert [header.grab().toImage() for header in headers] == before
        if table_name == "mapping":
            table.item(0, 1).setText("Renamed")
            assert editor.modules()[0].mapping[0].label == "Renamed"
        else:
            table.cellWidget(0, 0).setChecked(False)
            assert not getattr(editor.modules()[0], table_name)[0].enabled
    finally:
        editor.close()
        editor.deleteLater()


def test_import_export_and_rejected_import_preserve_modules(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.integration_creation.confirm", lambda *args: True)
    module = module_example(auto_enabled=True)
    source = tmp_path / "import.json"
    destination = tmp_path / "export.json"
    source.write_text(json.dumps(module.to_dict()), encoding="utf-8")
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.QFileDialog.getOpenFileName", lambda *args: (str(source), ""))
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.QFileDialog.getSaveFileName", lambda *args: (str(destination), ""))
    editor = IntegrationsEditor()
    editor.load(())

    def import_module():
        creation = IntegrationCreationDialog(editor.modules(), editor)
        creation.import_module()
        error = creation.error.text()
        if not error:
            creation.save()
            editor.add_module(creation.module)
        creation.deleteLater()
        return error

    try:
        assert not import_module()
        imported = editor.modules()[0]
        assert not imported.enabled and not imported.auto_enabled
        editor.export_module()
        assert IntegrationDefinition.from_dict(json.loads(destination.read_text(encoding="utf-8"))) == imported
        assert import_module()
        assert len(editor.modules()) == 1
        source.write_text('{"version":999}', encoding="utf-8")
        assert import_module()
        assert editor.modules() == (imported,)
    finally:
        editor.deleteLater()


def test_failed_transition_requires_explicit_retry_from_beginning(controller: Controller, qapp, monkeypatch):
    module = module_example(auto_enabled=True, system_rules=(IntegrationRule("r", "b"),))
    controller.apply_settings(replace(controller.settings, integrations=(module,)))
    state = controller.integration_states[module.id]
    state.observation = IntegrationObservation("integration_ready", executable=sys.executable)
    state.active_profile = "a"
    monkeypatch.setattr(IntegrationAdapter, "inspect", lambda self, selected: IntegrationObservation("integration_ready", executable=sys.executable))
    calls = []
    def run(args, **kwargs):
        calls.append(args[1])
        return subprocess.CompletedProcess(args, 7 if len(calls) == 2 else 0)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    now = [100.0]
    controller.clock = lambda: now[0]
    controller.telemetry = Telemetry(0, 100, 50, 0, now[0])
    controller.connection = ConnectionState.LIVE
    controller.tick()
    now[0] = 105
    controller.tick()
    wait_for(qapp, lambda: not state.busy)
    assert calls == ["--off", "--on"] and state.active_profile is None
    assert state.result.completed == 1 and not state.result.success
    now[0] = 125
    controller.tick()
    assert calls == ["--off", "--on"]
    controller.request_profile(module.id, "b")
    wait_for(qapp, lambda: not state.busy)
    assert calls == ["--off", "--on", "--off", "--on"]
    assert state.active_profile == "b" and state.result.success


def test_no_rule_stale_disable_and_shutdown_do_not_deactivate(controller: Controller, qapp, monkeypatch):
    module = module_example(auto_enabled=True)
    controller.apply_settings(replace(controller.settings, integrations=(module,)))
    state = controller.integration_states[module.id]
    state.observation = IntegrationObservation("integration_ready", executable=sys.executable)
    state.active_profile = "a"
    calls = []
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(IntegrationAdapter, "inspect", lambda self, selected: IntegrationObservation("integration_ready", executable=sys.executable))
    controller.telemetry = Telemetry(0, 100, 50, 0, controller.clock())
    controller.connection = ConnectionState.LIVE
    controller.tick()
    controller.connection = ConnectionState.STALE
    controller.tick()
    controller.set_auto(module.id, False)
    controller.apply_settings(replace(controller.settings, integrations=(replace(module, enabled=False),)))
    controller.stop()
    assert calls == []


def test_module_hotkey_removal_is_saved(controller: Controller, qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.integrations.confirm", lambda *args: True)
    dialog = SettingsWindow(controller)
    try:
        assert "profile:afterburner:profile_1" in dialog.hotkey_rows
        dialog.integrations_editor.remove()
        assert tuple(dialog.hotkey_rows) == ("toggle_monitor", "toggle_mini", "open_dashboard", "recover_widgets", "toggle_main")
        dialog.save()
        assert controller.store.load().integrations == ()
        assert {binding.action for binding in controller.store.load().hotkeys} == {"toggle_monitor", "toggle_mini", "open_dashboard", "recover_widgets", "toggle_main"}
    finally:
        dialog.deleteLater()

@pytest.mark.parametrize("profiles", [
    [],
    [{"slot": True}, {"slot": 2}, {"slot": 3}, {"slot": 4}, {"slot": 5}],
    [{"slot": 1}, {"slot": 1}, {"slot": 3}, {"slot": 4}, {"slot": 5}],
])
def test_damaged_legacy_profiles_enter_recovery_without_rewriting(tmp_path, profiles):
    path = tmp_path / "settings.json"
    content = json.dumps({"version": 1, "afterburner": {"profiles": profiles}})
    path.write_text(content, encoding="utf-8")
    from ecoflow_monitor.settings import SettingsError
    store = SettingsStore(path)
    with pytest.raises(SettingsError):
        store.load()
    assert store.recovery and path.read_text(encoding="utf-8") == content
