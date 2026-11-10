from dataclasses import replace
import json
import subprocess
import sys
import threading
import time

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QTabBar
import pytest

from ecoflow_monitor.adapters import onepassword
from ecoflow_monitor.adapters.ecoflow import Credentials, Device
from ecoflow_monitor.adapters.onepassword import OnePasswordError, RequestCancelled, SavedItem
from ecoflow_monitor.application import Application
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.credential_refs import normalize_secret_reference
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, SettingsError, SettingsStore
from ecoflow_monitor.ui.settings_window import SettingsWindow


ACCESS_REF = "op://Personal/EcoFlow/username"
SECRET_REF = "op://Personal/EcoFlow/password"


def wait_until(qapp, condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert condition()


@pytest.mark.parametrize("reference", [ACCESS_REF, '  "' + ACCESS_REF + '"  ', "'" + ACCESS_REF + "'", "op://My Vault/My Item/section/access key"])
def test_field_reference_accepts_copied_quotes_and_spaces(reference):
    assert normalize_secret_reference(reference).startswith("op://")
    assert not normalize_secret_reference(reference).startswith(('"', "'"))


@pytest.mark.parametrize("reference", ["", "https://example.com", "op://vault/item", "op://vault//field", "op://vault/item/field/extra/part", '"' + ACCESS_REF, ACCESS_REF + "'", ACCESS_REF + "\nother", ACCESS_REF + "?attribute=otp", ACCESS_REF + "#value", "op://vault/item/\x7f"])
def test_invalid_reference_is_rejected_before_process_launch(reference, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid references must not run commands")
    monkeypatch.setattr(onepassword, "run_cli", forbidden)
    with pytest.raises(OnePasswordError, match="^onepassword_reference_error$"):
        onepassword.read_credentials(ACCESS_REF, reference, threading.Event())


def test_old_settings_default_to_windows_and_new_references_roundtrip(tmp_path):
    payload = AppSettings().to_dict()
    del payload["ecoflow"]["credential_source"]
    del payload["ecoflow"]["onepassword"]
    legacy = AppSettings.from_dict(payload)
    assert legacy.credential_source == "windows"
    store = SettingsStore(tmp_path / "settings.json")
    settings = replace(legacy, credential_source="1password", onepassword_access_ref=ACCESS_REF, onepassword_secret_ref=SECRET_REF)
    store.save(settings)
    assert store.load() == settings
    assert json.loads(store.path.read_text(encoding="utf-8"))["ecoflow"]["onepassword"] == {"access_ref": ACCESS_REF, "secret_ref": SECRET_REF}


@pytest.mark.parametrize("changes", [dict(credential_source="other"), dict(credential_source="1password"), dict(onepassword_access_ref="plaintext"), dict(onepassword_secret_ref=12)])
def test_settings_reject_invalid_provider_or_references(changes):
    with pytest.raises(ValueError):
        replace(AppSettings(), **changes).validate()


def test_desktop_environment_does_not_inherit_other_authentication(monkeypatch):
    for key in ("OP_SESSION_example", "op_service_account_token", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN", "OP_BIOMETRIC_UNLOCK_ENABLED", "OP_DEBUG", "OP_CACHE", "OP_ACCOUNT"):
        monkeypatch.setenv(key, "synthetic")
    monkeypatch.setenv("UNRELATED_SETTING", "keep")
    environment = onepassword.desktop_environment()
    assert {key: value for key, value in environment.items() if key.upper().startswith("OP_")} == {"OP_BIOMETRIC_UNLOCK_ENABLED": "true", "OP_CACHE": "false"}
    assert environment["UNRELATED_SETTING"] == "keep"


def test_cli_transport_delivers_stdin_without_secret_arguments(tmp_path, monkeypatch):
    script = tmp_path / "synthetic_cli.py"
    script.write_text("import json, os, sys\nvalue = json.load(sys.stdin)\nassert os.environ['OP_BIOMETRIC_UNLOCK_ENABLED'] == 'true'\nassert os.environ['OP_CACHE'] == 'false'\nassert 'OP_SERVICE_ACCOUNT_TOKEN' not in os.environ\nassert value['secret'] not in ' '.join(sys.argv)\nsys.stdout.write(value['secret'])\n", encoding="utf-8")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "unused-synthetic-token")
    monkeypatch.setattr(onepassword.shutil, "which", lambda name: sys.executable)
    output = onepassword.run_cli([str(script)], threading.Event(), b'{"secret":"synthetic-private-value"}')
    assert output == b"synthetic-private-value"


@pytest.mark.parametrize("cancel", [False, True])
def test_cli_timeout_and_cancel_reap_the_process(tmp_path, monkeypatch, cancel):
    script = tmp_path / "waiting_cli.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(onepassword.shutil, "which", lambda name: sys.executable)
    original = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(onepassword.subprocess, "Popen", launch)
    cancelled = threading.Event()
    timer = threading.Timer(0.15, cancelled.set) if cancel else None
    if timer:
        timer.start()
    try:
        with pytest.raises(OnePasswordError, match="onepassword_cancelled" if cancel else "onepassword_timeout"):
            onepassword.run_cli([str(script)], cancelled, timeout=5 if cancel else 0.15)
        assert processes[0].poll() is not None
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_cli_error_does_not_expose_stderr_or_secrets(tmp_path, monkeypatch):
    script = tmp_path / "denied_cli.py"
    script.write_text("import sys\nsys.stderr.write('synthetic sensitive diagnostics')\nsys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(onepassword.shutil, "which", lambda name: sys.executable)
    with pytest.raises(OnePasswordError) as failure:
        onepassword.run_cli([str(script)], threading.Event())
    assert str(failure.value) == "onepassword_access_error"


def test_missing_cli_and_pre_cancelled_request_do_not_launch(monkeypatch):
    monkeypatch.setattr(onepassword.shutil, "which", lambda name: None)
    with pytest.raises(OnePasswordError, match="onepassword_missing"):
        onepassword.run_cli(["read", ACCESS_REF], threading.Event())
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RequestCancelled):
        onepassword.run_cli(["read", ACCESS_REF], cancelled)


def test_failed_cli_launch_is_sanitized(monkeypatch):
    monkeypatch.setattr(onepassword.shutil, "which", lambda name: "synthetic-op.exe")
    def reject(*args, **kwargs):
        raise OSError("synthetic sensitive path and diagnostics")
    monkeypatch.setattr(onepassword.subprocess, "Popen", reject)
    with pytest.raises(OnePasswordError) as failure:
        onepassword.run_cli(["read", ACCESS_REF], threading.Event())
    assert str(failure.value) == "onepassword_start_error"


@pytest.mark.parametrize("output", [b"", b" \t", b"\xff", b"key\n", b"key\x00"])
def test_bad_secret_field_is_rejected(monkeypatch, output):
    monkeypatch.setattr(onepassword, "run_cli", lambda *args: output)
    with pytest.raises(OnePasswordError, match="onepassword_value_error"):
        onepassword.read_secret(ACCESS_REF, threading.Event())


def test_create_item_uses_stdin_and_returns_stable_field_references(monkeypatch):
    credentials = Credentials("synthetic-access", "synthetic-secret")
    def cli(arguments, cancelled, payload):
        assert "synthetic-access" not in arguments and "synthetic-secret" not in arguments
        assert arguments == ["item", "create", "-", "--vault", "Test Vault", "--format", "json"]
        template = json.loads(payload)
        assert template["category"] == "LOGIN"
        assert [(field["id"], field["value"]) for field in template["fields"]] == [("username", credentials.access_key), ("password", credentials.secret_key)]
        return json.dumps({"id": "i" * 26, "vault": {"id": "v" * 26}}).encode()
    monkeypatch.setattr(onepassword, "run_cli", cli)
    assert onepassword.create_item("Test Vault", "EcoFlow test", credentials, threading.Event()) == SavedItem(f"op://{'v' * 26}/{'i' * 26}/username", f"op://{'v' * 26}/{'i' * 26}/password")
    assert "synthetic" not in repr(credentials)


@pytest.mark.parametrize("output", [b"{}", b"[]", b"null", b"not json", b'{"id":"bad/path","vault":{"id":"test"}}'])
def test_malformed_create_response_has_a_safe_error(monkeypatch, output):
    monkeypatch.setattr(onepassword, "run_cli", lambda *args: output)
    with pytest.raises(OnePasswordError, match="onepassword_save_result_error"):
        onepassword.create_item("Test", "Test", Credentials("a", "s"), threading.Event())


class NoLocalCredentials:
    def read(self, reference):
        raise AssertionError("1Password must not read Windows credentials")

    def write(self, reference, credentials):
        raise AssertionError("1Password must not write Windows credentials")

    def delete(self, reference):
        raise AssertionError("1Password must not delete Windows credentials")


@pytest.fixture
def controller(tmp_path, qapp):
    instance = Controller(SettingsStore(tmp_path / "settings.json"), NoLocalCredentials())
    yield instance
    instance.stop()
    qapp.processEvents()


def configure_onepassword(controller, **changes):
    controller.apply_settings(replace(controller.settings, credential_source="1password", onepassword_access_ref=ACCESS_REF, onepassword_secret_ref=SECRET_REF, **changes))


def test_monitor_reads_current_keys_each_time_without_persisting_them(controller, qapp, monkeypatch):
    pairs = [Credentials("access-first", "secret-first"), Credentials("access-rotated", "secret-rotated")]
    received = []
    def read(access, secret, cancelled):
        assert (access, secret) == (ACCESS_REF, SECRET_REF)
        assert threading.current_thread() is not threading.main_thread()
        return pairs[len(received)]
    class Client:
        def __init__(self, credentials):
            received.append(credentials)
        def read(self, serial):
            return Telemetry(0, 10, 80, 100, time.monotonic())
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", read)
    controller.client_factory = Client
    configure_onepassword(controller, serial="synthetic-device")
    for count in (1, 2):
        controller.refresh()
        wait_until(qapp, lambda: "api" not in controller._active)
        assert len(received) == count
        assert controller.connection == ConnectionState.LIVE
    assert received == pairs
    payload = controller.store.path.read_text(encoding="utf-8")
    assert all(value not in payload for pair in pairs for value in (pair.access_key, pair.secret_key))


def test_save_and_failed_publication_never_copy_keys_to_windows(controller, monkeypatch):
    configure_onepassword(controller)
    previous = controller.store.path.read_bytes()
    supplied = Credentials("synthetic-access", "synthetic-secret")
    controller.apply_settings(controller.settings, supplied)
    assert controller.store.path.read_bytes() == previous
    def reject(*args, **kwargs):
        raise SettingsError("settings_save_error")
    monkeypatch.setattr(controller.store, "save", reject)
    with pytest.raises(SettingsError):
        controller.apply_settings(replace(controller.settings, serial="changed"), supplied)
    assert controller.settings.serial == ""
    assert controller.store.path.read_bytes() == previous


def test_declined_access_stops_automatic_retries_and_allows_manual_retry(controller, qapp, monkeypatch):
    attempts = []
    def reject(*args):
        attempts.append(True)
        raise OnePasswordError("onepassword_access_error")
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", reject)
    configure_onepassword(controller, serial="synthetic-device")
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.notice == "onepassword_access_error"
    assert controller.connection == ConnectionState.AUTH_ERROR
    controller.tick()
    qapp.processEvents()
    assert len(attempts) == 1
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(attempts) == 2


def test_changing_provider_cancels_pending_lookup_and_ignores_old_result(controller, qapp, monkeypatch):
    entered = threading.Event()
    def read(access, secret, cancelled):
        entered.set()
        assert cancelled.wait(3)
        raise RequestCancelled()
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", read)
    configure_onepassword(controller, serial="synthetic-device")
    controller.refresh()
    wait_until(qapp, entered.is_set)
    controller.apply_settings(replace(controller.settings, serial="", credential_source="windows"))
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.connection == ConnectionState.UNCONFIGURED
    assert controller.telemetry is None
    assert controller.notice == ""


def test_shutdown_cancels_pending_lookup(controller, qapp, monkeypatch):
    entered = threading.Event()
    completed = threading.Event()
    def read(access, secret, cancelled):
        entered.set()
        assert cancelled.wait(3)
        completed.set()
        raise RequestCancelled()
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", read)
    configure_onepassword(controller, serial="synthetic-device")
    controller.refresh()
    wait_until(qapp, entered.is_set)
    controller.stop()
    assert completed.is_set()
    assert controller.telemetry is None


def test_unexpected_worker_failure_logs_only_its_type(qapp, caplog):
    def reject(cancelled):
        raise RuntimeError("synthetic-secret-in-exception")
    request = onepassword.SecretRequest(reject)
    request.start()
    result = None
    def finished():
        nonlocal result
        if result is None:
            result = request.take_result()
        return result is not None
    try:
        wait_until(qapp, finished)
        assert result.error == "onepassword_communication_error"
        assert "RuntimeError" in caplog.text
        assert "synthetic-secret-in-exception" not in caplog.text
        assert "synthetic-secret-in-exception" not in repr(result)
    finally:
        request.close()


@pytest.fixture
def dialog(controller):
    window = SettingsWindow(controller)
    yield window
    window.reject()
    window.deleteLater()


def set_panel_references(dialog):
    panel = dialog.onepassword
    panel.source.setCurrentIndex(1)
    panel.access_ref.setText('"' + ACCESS_REF + '"')
    panel.secret_ref.setText(SECRET_REF)
    return panel


@pytest.mark.parametrize("source", ["windows", "1password"])
def test_credential_tabs_stay_above_mode_fields_and_switch_visibility(controller, qapp, source):
    controller.settings = replace(controller.settings, credential_source=source, onepassword_access_ref=ACCESS_REF, onepassword_secret_ref=SECRET_REF)
    window = SettingsWindow(controller)
    try:
        window.show()
        qapp.processEvents()
        panel = window.onepassword
        assert isinstance(panel.source, QTabBar)
        assert panel.source.count() == 2
        assert panel.source.tabData(panel.source.currentIndex()) == source
        for index in (0, 1, 0):
            panel.source.setCurrentIndex(index)
            qapp.processEvents()
            assert panel.source.isVisibleTo(window)
            assert all(panel.source.isTabVisible(tab) for tab in range(2))
            assert not panel.source.usesScrollButtons()
            assert window.manual_credentials.isVisibleTo(window) == (index == 0)
            assert window.access.isVisibleTo(window) == (index == 0)
            assert window.secret.isVisibleTo(window) == (index == 0)
            assert all(label.isVisibleTo(window) == (index == 0) for label in window.manual_credentials.findChildren(QLabel))
            assert panel.references.isVisibleTo(window) == (index == 1)
            assert panel.load_button.isVisibleTo(window) == (index == 1)
            assert panel.save_section.isVisibleTo(window) == (index == 0)
            fields = window.manual_credentials if index == 0 else panel.references
            assert panel.source.geometry().bottom() < fields.geometry().top()
        panel.source.setCurrentIndex(1 if source == "windows" else 0)
        window.load_draft()
        assert panel.source.tabData(panel.source.currentIndex()) == source
        assert window.manual_credentials.isVisibleTo(window) == (source == "windows")
    finally:
        window.reject()
        window.deleteLater()


def test_fill_masks_keys_and_discovers_devices_with_unsaved_references(dialog, controller, qapp, monkeypatch):
    pair = Credentials("synthetic-draft-access", "synthetic-draft-secret")
    monkeypatch.setattr("ecoflow_monitor.ui.onepassword.read_credentials", lambda *args: pair)
    class Client:
        def __init__(self, supplied):
            assert supplied == pair
        def devices(self):
            return (Device("synthetic-serial", "Test DELTA", True),)
    controller.client_factory = Client
    panel = set_panel_references(dialog)
    panel.load_credentials()
    wait_until(qapp, lambda: panel.credentials is not None)
    assert dialog.access.text() == pair.access_key and dialog.secret.text() == pair.secret_key
    assert dialog.access.echoMode() == dialog.secret.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.access.isReadOnly() and dialog.secret.isReadOnly()
    assert not dialog.manual_credentials.isVisibleTo(dialog)
    assert not controller.store.path.exists()
    dialog.discover()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert dialog.devices.currentData() == "synthetic-serial"
    assert dialog.gather().onepassword_access_ref == ACCESS_REF


def test_reference_edit_and_reset_clear_loaded_keys(dialog, qapp, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.ui.onepassword.read_credentials", lambda *args: Credentials("access", "secret"))
    panel = set_panel_references(dialog)
    panel.load_credentials()
    wait_until(qapp, lambda: panel.credentials is not None)
    panel.access_ref.setText("op://Personal/other/username")
    assert panel.credentials is None
    assert dialog.access.text() == dialog.secret.text() == ""
    dialog.load_draft()
    assert not panel.uses_onepassword
    assert not dialog.access.isReadOnly()


def test_pending_fill_cannot_save_and_cancel_discards_result(dialog, qapp, monkeypatch):
    entered = threading.Event()
    def pending(access, secret, cancelled):
        entered.set()
        assert cancelled.wait(3)
        return Credentials("obsolete", "obsolete")
    monkeypatch.setattr("ecoflow_monitor.ui.onepassword.read_credentials", pending)
    panel = set_panel_references(dialog)
    panel.load_credentials()
    wait_until(qapp, entered.is_set)
    assert not dialog.save_button.isEnabled() and not dialog.discover_button.isEnabled()
    panel.cancel()
    qapp.processEvents()
    assert not panel.busy
    assert panel.credentials is None
    assert dialog.access.text() == dialog.secret.text() == ""
    assert dialog.save_button.isEnabled()


def test_application_exit_cancels_lookup_in_open_settings(controller, qapp, monkeypatch):
    entered = threading.Event()
    cancelled_request = threading.Event()
    def pending(access, secret, cancelled):
        entered.set()
        assert cancelled.wait(3)
        cancelled_request.set()
        raise RequestCancelled()
    monkeypatch.setattr("ecoflow_monitor.ui.onepassword.read_credentials", pending)
    runtime = Application(controller.store.path, native=False, controller=controller)
    dialog = SettingsWindow(controller, runtime.window)
    runtime._dialog = dialog
    try:
        panel = set_panel_references(dialog)
        panel.load_credentials()
        wait_until(qapp, entered.is_set)
        runtime.stop()
        assert cancelled_request.is_set()
        assert not panel.busy
        assert panel.credentials is None
    finally:
        runtime.stop()
        dialog.deleteLater()
        runtime.window.deleteLater()
        runtime.mini.deleteLater()
        runtime.deleteLater()
        qapp.processEvents()


def test_create_from_draft_selects_references_without_publishing_settings(dialog, controller, qapp, monkeypatch):
    pair = Credentials("entered-access", "entered-secret")
    def create(vault, title, credentials, cancelled):
        assert (vault, title, credentials) == ("Test", "EcoFlow Monitor", pair)
        return SavedItem(ACCESS_REF, SECRET_REF)
    monkeypatch.setattr("ecoflow_monitor.ui.onepassword.create_item", create)
    dialog.access.setText(pair.access_key)
    dialog.secret.setText(pair.secret_key)
    panel = dialog.onepassword
    panel.vault.setText("Test")
    panel.save_credentials()
    wait_until(qapp, lambda: panel.uses_onepassword and not panel.busy)
    assert panel.credentials == pair
    assert dialog.gather().onepassword_access_ref == ACCESS_REF
    assert not controller.store.path.exists()
    dialog.save()
    payload = controller.store.path.read_text(encoding="utf-8")
    assert pair.access_key not in payload and pair.secret_key not in payload
    assert controller.settings.credential_source == "1password"
    assert panel.credentials is None
    assert dialog.access.text() == dialog.secret.text() == ""
