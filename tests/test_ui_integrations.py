from dataclasses import replace
import json

from PySide6.QtWidgets import QDialog, QPushButton, QWidget
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.integrations import IntegrationRule, afterburner_example
from ecoflow_monitor.ui.integration_creation import IntegrationCreationDialog
from ecoflow_monitor.ui.integrations import IntegrationsEditor, ProfileDialog
from ecoflow_monitor.ui.rules import RuleDialog, RulesEditor


@pytest.fixture
def runtime(tmp_path, qapp):
    instance = Application(tmp_path / "settings.json", native=False)
    yield instance
    instance.stop()
    instance.dashboard.deleteLater()
    instance.mini.deleteLater()
    instance.main.deleteLater()
    qapp.processEvents()
    i18n.setup("en")


@pytest.mark.parametrize("accept", [False, True])
def test_new_integration_is_staged_only_after_dialog_acceptance(runtime, monkeypatch, accept):
    editor = runtime.dashboard.integrations_editor
    original = editor.modules()

    def edit(dialog):
        dialog.label.setText("Custom program")
        if accept:
            dialog.save()
        else:
            dialog.reject()
        return dialog.result()

    monkeypatch.setattr(IntegrationCreationDialog, QDialog.exec.__name__, edit)
    editor.add()
    assert runtime.controller.settings.integrations == original
    assert len(editor.modules()) == len(original) + int(accept)
    if accept:
        created = editor.modules()[-1]
        assert created.label == "Custom program"
        assert not created.enabled and not created.auto_enabled
        editor.label.setText("Renamed")
        assert editor.modules()[-1].id == created.id
        runtime.dashboard.load_draft()
        assert editor.modules() == original


def test_creation_import_preview_and_cancel_preserve_the_existing_draft(qapp, tmp_path, monkeypatch):
    module = replace(afterburner_example(), enabled=True, auto_enabled=True)
    source = tmp_path / "module.json"
    source.write_text(json.dumps(module.to_dict()), encoding="utf-8")
    monkeypatch.setattr("ecoflow_monitor.ui.integration_creation.QFileDialog.getOpenFileName", lambda *args: (str(source), ""))
    monkeypatch.setattr("ecoflow_monitor.ui.integration_creation.confirm", lambda *args: False)
    dialog = IntegrationCreationDialog(())
    try:
        dialog.label.setText("Unfinished")
        original = dialog.draft()
        dialog.import_module()
        assert dialog.draft() == original
        monkeypatch.setattr("ecoflow_monitor.ui.integration_creation.confirm", lambda *args: True)
        dialog.import_module()
        assert dialog.draft() == replace(module, enabled=False, auto_enabled=False)
        source.write_text('{"version":999}', encoding="utf-8")
        dialog.import_module()
        assert dialog.error.text()
        assert dialog.draft().id == module.id
        dialog.reject()
        assert dialog.module is None
    finally:
        dialog.deleteLater()


def test_creation_rejects_duplicate_example_and_invalid_executable(qapp):
    module = afterburner_example()
    dialog = IntegrationCreationDialog((module,))
    try:
        dialog.use_example()
        assert dialog.error.text() == i18n.t("integration_duplicate")
        assert dialog.module is None
        dialog.label.setText("New")
        dialog.path.setText("missing-file.exe")
        dialog.save()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.label.text() == "New"
        assert dialog.error.text()
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("accept", [False, True])
def test_rule_dialog_changes_are_staged_and_keep_identity(qapp, monkeypatch, accept):
    editor = RulesEditor(False)
    profiles = afterburner_example().profiles
    original = IntegrationRule("stable", profiles[0].id)
    editor.configure(profiles, ("input", "soc"))
    editor.set_rules((original,))

    def edit(dialog):
        dialog.minimum.setValue(20)
        dialog.maximum.setValue(90)
        dialog.profile.setCurrentIndex(1)
        if accept:
            dialog.save()
        else:
            dialog.reject()
        return dialog.result()

    monkeypatch.setattr(RuleDialog, QDialog.exec.__name__, edit)
    try:
        editor.edit_rule()
        expected = replace(original, min_soc=20, max_soc=90, profile_id=profiles[1].id) if accept else original
        assert editor.rules() == (expected,)
        editor.add_rule()
        assert len(editor.rules()) == 1 + int(accept)
        if accept:
            assert editor.rules()[1].id != original.id
            editor.move_rule(-1)
            assert editor.rules()[1] == expected
            editor.table.cellWidget(0, 0).setChecked(False)
            assert not editor.rules()[0].enabled
    finally:
        editor.deleteLater()


def test_rule_validation_and_optional_limits_keep_input(qapp):
    profiles = afterburner_example().profiles
    original = IntegrationRule("stable", profiles[0].id)
    dialog = RuleDialog(profiles, ("input", "soc"), False, original)
    try:
        assert not dialog.save_button.isEnabled()
        dialog.minimum.setValue(90)
        dialog.maximum.setValue(20)
        assert not dialog.save_button.isEnabled()
        dialog.save()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.minimum.value() == 90 and dialog.maximum.value() == 20
        dialog.maximum.toggle.setChecked(False)
        assert dialog.draft().max_soc is None
        assert dialog.maximum.spin.value() == 20
        dialog.maximum.toggle.setChecked(True)
        dialog.maximum.slider.setValue(100)
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.rule == replace(original, min_soc=90, max_soc=100)
    finally:
        dialog.deleteLater()


def test_application_rule_uses_shortcut_target_and_does_not_add_on_cancel(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QFile
    executable = tmp_path / "Програма.exe"
    executable.write_bytes(b"synthetic")
    shortcut = tmp_path / "Shortcut.lnk"
    assert QFile(str(executable)).link(str(shortcut))
    editor = RulesEditor(True)
    editor.configure(afterburner_example().profiles, ("application", "soc"))
    monkeypatch.setattr(RuleDialog, QDialog.exec.__name__, lambda dialog: QDialog.DialogCode.Rejected)
    try:
        editor.add_application(str(shortcut))
        assert editor.rules() == ()

        def accept(dialog):
            assert dialog.path.text() == str(executable)
            dialog.save()
            return dialog.result()

        monkeypatch.setattr(RuleDialog, QDialog.exec.__name__, accept)
        editor.add_application(str(shortcut))
        assert len(editor.rules()) == 1
        assert editor.rules()[0].executable == str(executable)
        editor.add_application(str(executable))
        assert len(editor.rules()) == 1
    finally:
        editor.deleteLater()


def test_rules_without_profiles_cannot_create_dangling_references(qapp):
    editor = RulesEditor(False)
    editor.configure((), ("input", "soc"))
    try:
        assert all(not button.isEnabled() and button.toolTip() for button in editor.add_buttons)
        editor.add_rule()
        assert editor.rules() == ()
    finally:
        editor.deleteLater()


@pytest.mark.parametrize("conditions", [(), ("input",), ("soc",)])
def test_rule_dialog_exposes_only_declared_conditions(qapp, conditions):
    module = afterburner_example()
    dialog = RuleDialog(module.profiles, conditions, False, IntegrationRule("example", module.profiles[0].id))
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.path.isHidden()
        assert dialog.input.isHidden() is ("input" not in conditions)
        assert dialog.minimum.isHidden() is ("soc" not in conditions)
        assert dialog.maximum.isHidden() is ("soc" not in conditions)
        assert dialog.draft().input_condition == "any"
        assert dialog.draft().min_soc is None and dialog.draft().max_soc is None
    finally:
        dialog.close()
        dialog.deleteLater()


def test_profile_token_insertion_and_validation_never_execute(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: calls.append(args))
    module = afterburner_example()
    dialog = ProfileDialog(module, module.profiles[0])
    try:
        toggle, field = dialog.reactions["activation"]
        previous = field.toPlainText()
        toggle.setChecked(False)
        assert not field.isEnabled() and field.toPlainText() == previous
        toggle.setChecked(True)
        field.clear()
        chip = next(button for button in dialog.findChildren(QPushButton) if button.text() == "{app}")
        chip.click()
        assert field.toPlainText() == "{app}"
        field.setPlainText("{app} {cli:missing}")
        assert not dialog.save_button.isEnabled()
        assert dialog.error_label.text()
        assert field.toPlainText() == "{app} {cli:missing}"
        field.setPlainText(previous)
        dialog.label.setText("Changed label")
        dialog.save()
        assert dialog.profile.id == module.profiles[0].id
        assert not calls
    finally:
        dialog.deleteLater()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_integration_pages_and_dialogs_fit_reference_and_minimum(runtime, qapp, monkeypatch, language, scale):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QScreen
    from ecoflow_monitor.ui.components import SettingRow
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 2560, 2160))
    runtime.controller.apply_settings(replace(runtime.controller.settings, language=language, scale=scale))
    window = runtime.dashboard
    runtime.open_dashboard(3)
    window.resize(round(1184 * scale), round(900 * scale))
    for width, height in ((1184, 900), (800, 600)):
        window.resize(round(width * scale), round(height * scale))
        for index in range(4):
            window.integrations_editor.body.setCurrentIndex(index)
            qapp.processEvents()
            page = window.tabs.currentWidget()
            assert page.horizontalScrollBar().maximum() == 0
            assert window.footer.isVisible()
    module = runtime.controller.settings.integrations[0]
    for dialog in (ProfileDialog(module, module.profiles[0], window), RuleDialog(module.profiles, module.conditions, False, IntegrationRule("new", module.profiles[0].id), window, creating=True), IntegrationCreationDialog((), window)):
        try:
            dialog.show()
            qapp.processEvents()
            assert dialog.screen().availableGeometry().contains(dialog.geometry())
            assert dialog.content_scroll.horizontalScrollBar().maximum() == 0
            assert not dialog.content_scroll.isAncestorOf(dialog.footer)
            assert dialog.rect().contains(dialog.footer.geometry().translated(dialog.footer.parentWidget().pos()))
            for row in dialog.findChildren(SettingRow):
                if row.isVisible():
                    assert row.rect().contains(row.control.geometry())
        finally:
            dialog.close()
            dialog.deleteLater()


@pytest.mark.design
@pytest.mark.parametrize("scale", [1, 2])
def test_dialog_footer_and_wide_fields_fit_a_small_work_area(qapp, monkeypatch, scale):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QScreen
    from ecoflow_monitor.ui.common import scaled_font
    from ecoflow_monitor.ui.components import SettingRow
    monkeypatch.setattr(QScreen, "availableGeometry", lambda self: QRect(0, 0, 800, 600))
    parent = QWidget()
    scaled_font(parent, scale)
    module = afterburner_example()
    try:
        for dialog in (ProfileDialog(module, module.profiles[0], parent), RuleDialog(module.profiles, module.conditions, False, IntegrationRule("example", module.profiles[0].id), parent, creating=True), IntegrationCreationDialog((), parent)):
            try:
                dialog.show()
                qapp.processEvents()
                assert dialog.screen().availableGeometry().contains(dialog.geometry())
                assert dialog.content_scroll.horizontalScrollBar().maximum() == 0
                assert dialog.footer.parentWidget().rect().contains(dialog.footer.geometry())
                for button in dialog.footer.buttons:
                    assert dialog.footer.body.rect().contains(button.geometry())
                for row in dialog.findChildren(SettingRow):
                    if row.isVisible():
                        assert row.rect().contains(row.control.geometry())
            finally:
                dialog.close()
                dialog.deleteLater()
    finally:
        parent.deleteLater()


@pytest.mark.design
def test_selected_command_retains_its_semantic_id_color(qapp):
    from PySide6.QtGui import QColor
    from ecoflow_monitor.ui.theme import settings_style
    from ecoflow_monitor.ui.tokens import COLORS
    editor = IntegrationsEditor()
    editor.setStyleSheet(settings_style())
    editor.load((afterburner_example(),))
    editor.resize(1000, 700)
    editor.body.setCurrentIndex(1)
    editor.show()
    try:
        editor.mapping.selectRow(0)
        qapp.processEvents()
        cell = editor.mapping.visualItemRect(editor.mapping.item(0, 0))
        pixels = editor.mapping.viewport().grab().toImage()
        color = QColor(COLORS["input"])
        assert any(pixels.pixelColor(x, y) == color for x in range(cell.left(), cell.right() + 1) for y in range(cell.top(), cell.bottom() + 1))
    finally:
        editor.close()
        editor.deleteLater()


@pytest.mark.design
@pytest.mark.parametrize("table_name", ["mapping", "profiles", "application_rules", "system_rules"])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_integration_table_text_stays_readable(qapp, table_name, focused, selected):
    from PySide6.QtGui import QColor, QPalette
    from ecoflow_monitor.ui.theme import settings_style
    from ecoflow_monitor.ui.tokens import COLORS
    module = afterburner_example()
    rule = IntegrationRule("example", module.profiles[0].id, executable="Example.exe", min_soc=0, max_soc=100)
    module = replace(module, application_rules=(rule,), system_rules=(replace(rule, executable=""),))
    editor = IntegrationsEditor()
    palette = QPalette(editor.palette())
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("black"))
    editor.setPalette(palette)
    editor.setStyleSheet(settings_style())
    editor.load((module,))
    editor.resize(1200, 1000)
    editor.body.setCurrentIndex(1 if table_name == "mapping" else 2 if table_name == "profiles" else 3)
    table = getattr(editor, table_name)
    if isinstance(table, RulesEditor):
        table = table.table
    editor.show()
    try:
        if selected:
            table.selectRow(0)
        else:
            table.clearSelection()
        if focused:
            table.setFocus()
        else:
            table.clearFocus()
        qapp.processEvents()
        assert table.hasFocus() == focused
        assert bool(table.selectionModel().selectedRows()) == selected
        pixels = table.viewport().grab().toImage()
        for column in range(table.columnCount()):
            if table.isColumnHidden(column):
                continue
            item = table.item(0, column)
            if item is None:
                continue
            tokens = {0: "input", 2: "muted"} if table_name == "mapping" else {0: "input"} if table_name == "profiles" else {}
            token = tokens.get(column, "text")
            color = QColor(COLORS[token])
            cell = table.visualItemRect(item).intersected(pixels.rect())
            assert any(
                pixels.pixelColor(x, y) == color
                for x in range(cell.left(), cell.right() + 1)
                for y in range(cell.top(), cell.bottom() + 1)
            ), (table_name, column, token)
    finally:
        editor.close()
        editor.deleteLater()
