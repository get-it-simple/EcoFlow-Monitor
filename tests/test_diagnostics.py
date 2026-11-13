from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import logging
from pathlib import Path
import threading
from typing import cast

import httpx
import pytest
from qt_helpers import close_runtime

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.diagnostics import DiagnosticLog, REDACTED, event, reveal_log, sanitize
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.ui.settings_window import SettingsWindow


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_off_creates_no_files_and_preserves_existing_logs(tmp_path):
    path = tmp_path / "logs" / "ecoflow-monitor.log"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("off")
        event("application_started")
        logging.getLogger("ecoflow_monitor.controller").error("Synthetic failure")
        assert not path.parent.exists()
    path.parent.mkdir()
    path.write_text("existing log", encoding="utf-8")
    with DiagnosticLog(path) as diagnostics:
        event("ignored")
    assert path.read_text(encoding="utf-8") == "existing log"


def test_modes_change_live_and_off_stops_pending_writes(tmp_path):
    path = tmp_path / "logs" / "ecoflow-monitor.log"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("normal")
        event("normal_action")
        event("detail_hidden", level=logging.DEBUG, value=123)
        diagnostics.flush()
        assert "normal_action" in path.read_text(encoding="utf-8")
        assert "detail_hidden" not in path.read_text(encoding="utf-8")
        diagnostics.configure("detailed")
        event("detail_visible", level=logging.DEBUG, value=456)
        diagnostics.flush()
        assert records(path)[-1]["data"]["value"] == 456
        with diagnostics._state_lock:
            event("pending_detail", level=logging.DEBUG)
            diagnostics.configure("normal")
        diagnostics.flush()
        assert "pending_detail" not in path.read_text(encoding="utf-8")
        diagnostics.configure("off")
        before = path.read_bytes()
        event("disabled_error", level=logging.ERROR)
        diagnostics.flush()
        assert path.read_bytes() == before


def test_recursive_masking_preserves_fields_and_unknown_values():
    source = {"code": "0", "msg": "device PRIVATE-SERIAL for Private User with KEY-ACCESS",
              "data": [{"SN": "PRIVATE-SERIAL", "device_name": "Private User", "access-key": "KEY-ACCESS",
                        "newMetric": 123, "newText": "unrecognized text", "powInSumW": 0,
                        "nested": {"secret_key": "KEY-SECRET", "online": 1}}]}
    clean = sanitize(source)
    serialized = json.dumps(clean)
    for secret in ("PRIVATE-SERIAL", "Private User", "KEY-ACCESS", "KEY-SECRET"):
        assert secret not in serialized
    assert clean["data"][0]["SN"] == REDACTED
    assert clean["data"][0]["access-key"] == REDACTED
    assert clean["data"][0]["newText"] == "unrecognized text"
    assert clean["data"][0]["newMetric"] == 123
    assert clean["data"][0]["powInSumW"] == 0
    assert source["data"][0]["SN"] == "PRIVATE-SERIAL"


def test_sensitive_nested_values_and_free_text_are_hidden():
    result = sanitize({"account": {"custom": ["private-account"]}, "message": "private-account reflected",
                       "details": "person@example.test 192.168.1.42 AA:BB:CC:DD:EE:FF op://vault/item/field 2001:db8::1234",
                       "file": "C:\\Users\\private-user\\settings.json",
                       "exception": "accessKey=private-key",
                       "embedded": '{"access_key":"private-json-key"}'})
    serialized = json.dumps(result)
    for value in ("private-account", "person@example.test", "192.168.1.42", "AA:BB:CC:DD:EE:FF", "vault/item", "private-user", "private-key", "private-json-key", "2001:db8::1234"):
        assert value not in serialized
    assert result["account"] == REDACTED


@pytest.mark.parametrize("mode", ["normal", "detailed"])
def test_actual_api_logs_hide_secrets_including_error_bodies(tmp_path, mode):
    path = tmp_path / "ecoflow-monitor.log"
    def handle(request):
        return httpx.Response(401, json={"code": "AUTH_FAILURE", "accessKey": "private-access",
                                       "secretKey": "private-secret", "sn": "PRIVATE-SERIAL",
                                       "sign": request.headers["sign"], "nonce": request.headers["nonce"],
                                       "msg": "private-access private-secret PRIVATE-SERIAL",
                                       "unknownNewValue": 123})
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure(mode)
        client = EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(handle))
        with pytest.raises(EcoFlowError) as error:
            client.devices()
        assert error.value.state == ConnectionState.AUTH_ERROR
    content = path.read_text(encoding="utf-8")
    for secret in ("private-access", "private-secret", "PRIVATE-SERIAL"):
        assert secret not in content
    responses = [item for item in records(path) if item["event"] == "api_response"]
    if mode == "normal":
        assert not responses
        assert "unknownNewValue" not in content
    else:
        payload = responses[0]["data"]["payload"]
        assert payload["sign"] == REDACTED and payload["nonce"] == REDACTED
        assert payload["unknownNewValue"] == 123


def test_live_read_path_records_raw_and_normalized_telemetry(tmp_path):
    path = tmp_path / "ecoflow-monitor.log"
    def handle(request):
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "PRIVATE-SERIAL", "deviceName": "Private station", "online": 1}]})
        return httpx.Response(200, json={"code": "0", "data": {"powInSumW": 0, "powOutSumW": "280", "cmsBattSoc": 80,
                                                             "flowInfoAcOut": 4, "flowInfo12v": 2, "cmsMinDsgSoc": -1,
                                                             "cmsChgDsgState": 1.5, "newTelemetry": "new value"}})
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("detailed")
        snapshot = EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(handle)).read("PRIVATE-SERIAL")
    assert snapshot.input_w == 0 and snapshot.output_w == 280
    assert snapshot.ac_enabled is False and snapshot.dc_enabled is True
    assert snapshot.min_soc is None and snapshot.charging_state is None
    items = records(path)
    quota = next(item for item in items if item["event"] == "api_response" and isinstance(item["data"]["payload"]["data"], dict))
    assert quota["data"]["payload"]["data"]["powOutSumW"] == "280"
    normalized = next(item["data"] for item in items if item["event"] == "telemetry_normalized")
    assert normalized["telemetry"]["output_w"] == 280
    assert normalized["telemetry"]["input_w"] == 0
    assert "cmsMinDsgSoc" in normalized["invalid_fields"]
    assert "cmsChgDsgState" in normalized["invalid_fields"]
    assert "cmsMaxChgSoc" in normalized["missing_fields"]
    assert "PRIVATE-SERIAL" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("response", [
    httpx.Response(429, headers={"Retry-After": "60"}, json={"code": "RATE_LIMIT", "email": "private@example.test"}),
    httpx.Response(500, text="private non-JSON body"),
    httpx.Response(200, text="{ broken private json"),
    httpx.Response(200, json={"code": "0", "data": {"unexpected": 7}}),
])
def test_api_failures_keep_safe_diagnostics(tmp_path, response):
    path = tmp_path / "ecoflow-monitor.log"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("detailed")
        client = EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(lambda request: response))
        with pytest.raises(EcoFlowError):
            client.devices()
    content = path.read_text(encoding="utf-8")
    assert "api_response" in content
    assert "private" not in content
    if response.status_code == 429:
        assert any(item.get("data", {}).get("retry_after_seconds") == 60 for item in records(path))


def test_transport_exception_does_not_log_exception_message(tmp_path):
    path = tmp_path / "ecoflow-monitor.log"
    def fail(request):
        raise httpx.ReadTimeout("private-access private-secret PRIVATE-SERIAL", request=request)
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("detailed")
        with pytest.raises(EcoFlowError):
            EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(fail)).devices()
    content = path.read_text(encoding="utf-8")
    assert "ReadTimeout" in content and "elapsed_ms" in content
    assert "private-access" not in content and "PRIVATE-SERIAL" not in content


@pytest.mark.parametrize("status,state", [(401, ConnectionState.AUTH_ERROR), (429, ConnectionState.API_ERROR)])
def test_deep_error_json_keeps_authentication_and_retry_behavior(tmp_path, status, state):
    path = tmp_path / "ecoflow-monitor.log"
    body = b'{"data":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("detailed")
        client = EcoFlowClient(Credentials("private-access", "private-secret"),
                               httpx.MockTransport(lambda request: httpx.Response(status, content=body, headers={"Retry-After": "60"})))
        with pytest.raises(EcoFlowError) as error:
            client.devices()
        assert error.value.state == state
        assert error.value.retry_after == (60 if status == 429 else None)
    assert "api_response" in path.read_text(encoding="utf-8")


def test_rotated_files_are_sanitized_and_writer_is_not_gui_thread(tmp_path, monkeypatch):
    path = tmp_path / "ecoflow-monitor.log"
    writer_threads = []
    original = DiagnosticLog._write_line
    def write(self, line):
        writer_threads.append(threading.get_ident())
        original(self, line)
    monkeypatch.setattr(DiagnosticLog, "_write_line", write)
    with DiagnosticLog(path, max_bytes=800, backup_count=2) as diagnostics:
        diagnostics.configure("detailed")
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda index: event("parallel_event", level=logging.DEBUG, index=index, sn="private-serial", metric=index), range(40)))
    paths = list(tmp_path.glob("ecoflow-monitor*.log"))
    assert len(paths) == 3
    for file in paths:
        assert "private-serial" not in file.read_text(encoding="utf-8")
        assert all(isinstance(item, dict) for item in records(file))
    assert writer_threads and threading.get_ident() not in writer_threads


def test_disk_error_is_visible_and_does_not_escape_event(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "ecoflow-monitor.log"
    original = Path.mkdir
    def denied(self, *args, **kwargs):
        if self == path.parent:
            raise PermissionError("private path")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", denied)
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("normal")
        event("action")
        diagnostics.flush()
        assert diagnostics.failed
        assert not path.exists()
        monkeypatch.setattr(Path, "mkdir", original)
        diagnostics.configure("detailed")
        event("recovered")
        diagnostics.flush()
        assert not diagnostics.failed
        assert "recovered" in path.read_text(encoding="utf-8")


def test_bounded_queue_and_event_size_report_omitted_data(tmp_path):
    path = tmp_path / "ecoflow-monitor.log"
    with DiagnosticLog(path, queue_size=2) as diagnostics:
        diagnostics.configure("detailed")
        diagnostics.flush()
        with diagnostics._state_lock:
            for index in range(20):
                event("queued", index=index)
        diagnostics.flush()
        event("large", level=logging.DEBUG, values=["x" * 8000] * 30)
        diagnostics.flush()
    items = records(path)
    assert any(item["event"] == "log_events_dropped" and item["count"] > 0 for item in items)
    assert any(item.get("data", {}).get("reason") == "event_size_limit" for item in items)
    assert all(len(line.encode("utf-8")) <= 65536 for line in path.read_text(encoding="utf-8").splitlines())


def test_mode_settings_are_backward_compatible_and_validated():
    old = AppSettings().to_dict()
    old.pop("logging")
    assert AppSettings.from_dict(old).logging_mode == "normal"
    for mode in ("off", "normal", "detailed"):
        settings = replace(AppSettings(), logging_mode=mode)
        assert AppSettings.from_dict(settings.to_dict()) == settings
    for value in ("DEBUG", None, 123, []):
        with pytest.raises((ValueError, TypeError)):
            AppSettings.from_dict(dict(old, logging={"mode": value}))


def test_logging_only_save_preserves_monitoring_operation(tmp_path, qapp):
    store = SettingsStore(tmp_path / "settings.json")
    controller = Controller(store)
    try:
        telemetry = Telemetry(0, 280, 80, 1, 2)
        controller.telemetry = telemetry
        controller.connection = ConnectionState.LIVE
        cancelled = controller._credential_cancelled
        generation = controller._generation
        controller._next_api = 1234
        controller.apply_settings(replace(controller.settings, logging_mode="detailed"))
        assert controller.telemetry is telemetry
        assert controller.connection == ConnectionState.LIVE
        assert controller._generation == generation
        assert controller._next_api == 1234
        assert controller._credential_cancelled is cancelled and not cancelled.is_set()
        assert store.load().logging_mode == "detailed"
    finally:
        controller.stop()


def test_settings_controls_save_reset_cancel_and_reveal(tmp_path, qapp, monkeypatch):
    path = tmp_path / "settings.json"
    with DiagnosticLog(tmp_path / "logs" / "ecoflow-monitor.log") as diagnostics:
        controller = Controller(SettingsStore(path), diagnostics=diagnostics)
        window = SettingsWindow(controller)
        try:
            window.logging_mode.setCurrentIndex(window.logging_mode.findData("detailed"))
            window._draft_changed()
            assert window.save_button.isEnabled()
            assert diagnostics.mode == "normal"
            window.load_draft()
            assert window.logging_mode.currentData() == "normal"
            window.logging_mode.setCurrentIndex(window.logging_mode.findData("detailed"))
            window.save()
            assert diagnostics.mode == "detailed" and controller.store.load().logging_mode == "detailed"
            diagnostics.flush()
            window._refresh_log_status()
            assert window.log_link.isEnabled()
            calls = []
            monkeypatch.setattr("ecoflow_monitor.ui.settings_window.reveal_log", lambda target: calls.append(target) or True)
            window.log_link.click()
            assert calls == [diagnostics.path]
            window.logging_mode.setCurrentIndex(window.logging_mode.findData("off"))
            window.reject()
            assert diagnostics.mode == "detailed"
            window.logging_mode.setCurrentIndex(window.logging_mode.findData("off"))
            window.save()
            assert diagnostics.mode == "off"
            window._refresh_log_status()
            assert window.log_link.isEnabled()
            diagnostics.failed = True
            window._refresh_log_status()
            assert window.log_status.text()
        finally:
            window._log_status_timer.stop()
            window._draft_timer.stop()
            window.close()
            controller.stop()


def test_missing_file_does_not_open_explorer_and_spaces_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / "folder with spaces" / "ecoflow-monitor.log"
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert not reveal_log(path)
    assert not calls
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    assert reveal_log(path)
    assert calls == [((["explorer.exe", "/select," + str(path.resolve())],), {"shell": False})]


def test_failed_settings_save_keeps_mode_and_user_input(tmp_path, qapp, monkeypatch):
    from ecoflow_monitor.settings import SettingsError
    warnings = []
    monkeypatch.setattr("ecoflow_monitor.ui.settings_window.QMessageBox.warning", lambda *args: warnings.append(args))
    with DiagnosticLog(tmp_path / "logs" / "ecoflow-monitor.log") as diagnostics:
        controller = Controller(SettingsStore(tmp_path / "settings.json"), diagnostics=diagnostics)
        window = SettingsWindow(controller)
        try:
            window.logging_mode.setCurrentIndex(window.logging_mode.findData("detailed"))
            def fail(*args, **kwargs):
                raise SettingsError("settings_save_error")
            monkeypatch.setattr(controller.store, "save", fail)
            window.save()
            assert diagnostics.mode == "normal"
            assert controller.settings.logging_mode == "normal"
            assert window.logging_mode.currentData() == "detailed"
            assert warnings
        finally:
            window._log_status_timer.stop()
            window._draft_timer.stop()
            window.close()
            controller.stop()


def test_disabled_smoke_writer_and_missing_file_status(tmp_path, qapp):
    path = tmp_path / "logs" / "ecoflow-monitor.log"
    with DiagnosticLog(path, disabled=True) as diagnostics:
        controller = Controller(SettingsStore(tmp_path / "settings.json"), diagnostics=diagnostics)
        window = SettingsWindow(controller)
        try:
            controller.apply_settings(replace(controller.settings, logging_mode="detailed"))
            event("smoke_event")
            window._refresh_log_status()
            assert not window.log_link.isEnabled()
            assert window.log_link.toolTip()
            assert not path.parent.exists()
        finally:
            window._log_status_timer.stop()
            window._draft_timer.stop()
            window.close()
            controller.stop()


def test_legacy_log_exception_never_formats_exception_text(tmp_path):
    path = tmp_path / "ecoflow-monitor.log"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("normal")
        try:
            raise RuntimeError("private-exception-message")
        except RuntimeError:
            logging.getLogger("ecoflow_monitor.controller").exception("Background failed")
    content = path.read_text(encoding="utf-8")
    assert "RuntimeError" in content
    assert "private-exception-message" not in content
    assert "test_diagnostics.py" not in content


def test_large_and_deep_payloads_fail_closed_without_mutating_input():
    data = {"token": "private-deep-token"}
    for _ in range(20):
        data = {"nested": data}
    payload = {"msg": "private-deep-token", "data": data}
    assert "private-deep-token" not in json.dumps(sanitize(payload))
    assert payload["msg"] == "private-deep-token"
    assert sanitize({"many": [0] * 5000})["_truncated"]


def test_unicode_and_invalid_surrogate_values_do_not_break_logging(tmp_path):
    path = tmp_path / "ecoflow-monitor.log"
    with DiagnosticLog(path) as diagnostics:
        diagnostics.configure("detailed")
        event("unicode_payload", level=logging.DEBUG, new_field="Станція \ud800")
    assert records(path)[-1]["data"]["new_field"] == "Станція \ud800"


def test_inaccessible_log_path_does_not_break_settings(tmp_path, qapp, monkeypatch):
    controller = Controller(SettingsStore(tmp_path / "settings.json"))
    window = SettingsWindow(controller)
    try:
        original = Path.is_file
        def denied(self):
            if self.name == "ecoflow-monitor.log":
                raise PermissionError("private path")
            return original(self)
        monkeypatch.setattr(Path, "is_file", denied)
        window._refresh_log_status()
        assert not window.log_link.isEnabled()
        assert window.log_status.text()
        assert "private path" not in window.log_status.text()
        assert not reveal_log(tmp_path / "logs" / "ecoflow-monitor.log")
    finally:
        window._log_status_timer.stop()
        window._draft_timer.stop()
        window.close()
        controller.stop()


def test_explorer_failure_message_survives_status_refresh(tmp_path, qapp, monkeypatch):
    controller = Controller(SettingsStore(tmp_path / "settings.json"))
    window = SettingsWindow(controller)
    try:
        monkeypatch.setattr("ecoflow_monitor.ui.settings_window.reveal_log", lambda path: False)
        window._reveal_log()
        message = window.log_status.text()
        window._refresh_log_status()
        assert message and window.log_status.text() == message
        monkeypatch.setattr("ecoflow_monitor.ui.settings_window.reveal_log", lambda path: True)
        window._reveal_log()
        assert not window.log_status.text()
    finally:
        window._log_status_timer.stop()
        window._draft_timer.stop()
        window.close()
        controller.stop()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_logging_settings_layout_and_preview(tmp_path, qapp, session_cache, monkeypatch, language, scale):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QScreen
    from ecoflow_monitor import i18n
    from ecoflow_monitor.application import Application
    from ecoflow_monitor.ui.tokens import COLORS
    previous_language = i18n.get_current_language()
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), language=language, scale=scale, logging_mode="detailed"))
    controller = Controller(store, credential_cache=session_cache)
    runtime = Application(store.path, native=False, controller=controller)
    output = Path("build/logging-preview")
    output.mkdir(parents=True, exist_ok=True)
    try:
        window = runtime.dashboard
        screen = cast(QScreen, window.screen())
        monkeypatch.setattr(screen, "availableGeometry", lambda: QRect(0, 0, 1920, 1080))
        window.resize(1440, 960)
        window.show()
        window.tabs.setCurrentIndex(1)
        window.load_draft()
        qapp.processEvents()
        page = window.tabs.currentWidget()
        page.ensureWidgetVisible(window.log_link, 0, 0)
        qapp.processEvents()
        assert page.horizontalScrollBar().maximum() == 0
        assert window.logging_mode.width() >= window.logging_mode.minimumSizeHint().width()
        assert window.log_link.height() >= window.log_link.wrapped_height(window.log_link.width())
        top_left = window.log_link.mapTo(page.viewport(), QPoint(0, 0))
        assert page.viewport().rect().contains(top_left)
        assert window.log_link.property("link") is True
        assert COLORS["accent"] in window.styleSheet()
        suffix = f"{language}-{round(scale * 100)}"
        assert window.grab().save(str(output / f"connection-{suffix}.png"))
        window.tabs.setCurrentIndex(0)
        qapp.processEvents()
        assert window.grab().save(str(output / f"monitoring-{suffix}.png"))
        runtime.main.show()
        runtime.mini.show()
        qapp.processEvents()
        assert runtime.main.grab().save(str(output / f"main-{suffix}.png"))
        assert runtime.mini.grab().save(str(output / f"mini-{suffix}.png"))
    finally:
        close_runtime(runtime, qapp)
        i18n.setup(previous_language)


@pytest.mark.design
def test_long_log_link_wraps_at_narrow_width(qapp):
    from PySide6.QtCore import Qt
    from ecoflow_monitor import i18n
    from ecoflow_monitor.ui.components import LinkButton
    from ecoflow_monitor.ui.common import scaled_font
    from ecoflow_monitor.ui.theme import settings_style
    previous_language = i18n.get_current_language()
    i18n.setup("uk")
    link = LinkButton(i18n.t("logging_reveal"))
    try:
        scaled_font(link, 2)
        link.setStyleSheet(settings_style(scale=2))
        link.ensurePolished()
        link.resize(320, link.heightForWidth(320))
        text_rect = link.rect().adjusted(40, 0, -40, 0)
        needed = link.fontMetrics().boundingRect(text_rect, Qt.TextFlag.TextWordWrap, link.text())
        assert needed.height() <= link.height()
        assert link.minimumSizeHint().width() == 0
        assert link.height() > link.sizeHint().height()
    finally:
        link.close()
        link.deleteLater()
        i18n.setup(previous_language)
