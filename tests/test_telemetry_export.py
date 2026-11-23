from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
import json
import math
import socket
import subprocess
import sys
import threading

import httpx
import pytest
from qt_helpers import wait_until

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient
from ecoflow_monitor.adapters.ecoflow_mqtt import decode_report
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import ConnectionState
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile, Reaction
from ecoflow_monitor.polling import TelemetryPatch
from ecoflow_monitor.settings import AppSettings, ExportPreferences, SettingsStore
from ecoflow_monitor.system_activity import SystemActivity
from ecoflow_monitor.telemetry_export import TelemetryExport, sanitize_export
from ecoflow_monitor.ui.settings_window import SettingsWindow
from scripts.prometheus_bridge import Bridge


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def request(port, path, **headers):
    with http_connection(port) as connection:
        connection.request("GET", path, headers=headers)
        with connection.getresponse() as response:
            yield response


@contextmanager
def http_connection(port):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        yield connection
    finally:
        connection.close()


def snapshot(port):
    with request(port, "/v1/telemetry") as response:
        assert response.status == 200
        return json.load(response)


def next_event(response):
    line = response.readline()
    assert line.startswith(b"data: ")
    assert response.readline() == b"\n"
    return json.loads(line[6:])


def test_export_settings_default_and_round_trip():
    data = AppSettings().to_dict()
    data.pop("telemetry_export")
    assert AppSettings.from_dict(data).telemetry_export == ExportPreferences()
    data["telemetry_export"] = {
        "enabled": True, "port": 12345, "mode": "interval", "interval_seconds": 12,
        "raw": True, "commands": True, "show_private": True,
    }
    settings = AppSettings.from_dict(data)
    assert AppSettings.from_dict(settings.to_dict()) == settings
    for changes in ({"port": True}, {"port": 0}, {"interval_seconds": 3601}, {"mode": "invalid"}, {"commands": 1}):
        with pytest.raises(ValueError, match="export_invalid"):
            AppSettings.from_dict({**data, "telemetry_export": changes})


@pytest.mark.parametrize("show_private", [False, True])
@pytest.mark.parametrize("show_serial", [False, True])
def test_privacy_options_are_independent_and_keep_unknown_fields(show_private, show_serial):
    prefs = ExportPreferences(show_private=show_private, show_serial=show_serial)
    data = {
        "pd.sn": "SYNTHETIC-SERIAL", "sn_copy": "copy SYNTHETIC-SERIAL", "person.email": "example@example.test",
        "vendor": {"unrecognized": [0, "example@example.test", "synthetic-password", "synthetic-key", "synthetic-broker"]},
        "certificateAccount": "synthetic-broker", "clientSecret": "synthetic-password",
        "deviceName": "Custom name", "unrecognized_name": "Custom name",
    }
    result = sanitize_export(data, prefs, serial="SYNTHETIC-SERIAL", secrets=("synthetic-key",))
    assert result["pd.sn"] == ("SYNTHETIC-SERIAL" if show_serial else "[REDACTED]")
    assert result["sn_copy"] == ("copy SYNTHETIC-SERIAL" if show_serial else "copy [REDACTED]")
    assert result["person.email"] == ("example@example.test" if show_private else "[REDACTED]")
    assert result["unrecognized_name"] == ("Custom name" if show_private else "[REDACTED]")
    assert result["vendor"]["unrecognized"] == [
        0, "example@example.test" if show_private else "[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]",
    ]
    assert result["clientSecret"] == result["certificateAccount"] == "[REDACTED]"


def test_live_feeds_have_no_replay_and_reconfigure_privacy():
    port = free_port()
    prefs = ExportPreferences(enabled=True, port=port, raw=True, commands=True, show_serial=True)
    service = TelemetryExport(prefs)
    service.configure(prefs, active=True, serial="SYNTHETIC-SERIAL", device_name="Synthetic")
    try:
        with request(port, "/v1/telemetry/stream") as response:
            assert response.status == 200
            service.publish("connected", "mqtt", {"input_power": 0}, {"input_power": math.inf}, "Synthetic")
            assert next_event(response)["values"] == {"input_power": 0}
        service.emit("commands", {"event": "past"})
        with request(port, "/v1/commands") as response:
            assert response.status == 200
            service.emit("commands", {"event": "started", "integration_id": "synthetic"})
            assert next_event(response)["event"] == "started"
        with request(port, "/v1/telemetry/raw") as response:
            assert response.status == 200
            decode_report(b'{"unknown":42}', lambda payload: service.emit("raw", {"source": "mqtt", "payload": payload}))
            event = next_event(response)
            assert event["payload"] == {"unknown": 42} and event["serial_number"] == "SYNTHETIC-SERIAL"
            service.configure(replace(prefs, show_serial=False), active=True, serial="SYNTHETIC-SERIAL", device_name="Synthetic")
            assert response.readline() == b""
        with request(port, "/v1/telemetry/raw", Host="example.test") as response:
            assert response.status == 403
        with request(port, "/v1/telemetry/raw", Origin="https://example.test") as response:
            assert response.status == 403
        with request(port, "/v1/telemetry/raw") as response:
            service.emit("raw", {"source": "mqtt", "payload": {"sn": "SYNTHETIC-SERIAL"}})
            event = next_event(response)
            assert event["payload"]["sn"] == "[REDACTED]" and "serial_number" not in event
    finally:
        service.stop()


def test_occupied_port_and_bridge_scrape_drop_expired_values():
    clock = [100.0]
    prefs = ExportPreferences(enabled=True, port=free_port())
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", prefs.port))
        occupied.listen()
        service = TelemetryExport(prefs, lambda: clock[0])
        service.configure(prefs, active=True, serial="", device_name="")
        assert service.error and not service.running
    service.configure(prefs, active=True, serial="", device_name="")
    assert service.running and not service.error
    try:
        service.publish("connected", "https", {"input_power": 0, "usb_c1": -5}, {"input_power": 110, "usb_c1": 105}, "")
        with Bridge(0, prefs.port) as bridge:
            runner = threading.Thread(target=bridge.serve_forever, daemon=True, kwargs={"poll_interval": 0.01})
            runner.start()
            try:
                with request(bridge.server_port, "/metrics") as response:
                    assert response.status == 200
                    assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
                    body = response.read().decode()
                assert "ecoflow_monitor_input_power_watts 0\n" in body
                assert 'ecoflow_monitor_port_watts{port="usb_c1"} -5\n' in body
                clock[0] = 106
                with request(bridge.server_port, "/metrics") as response:
                    assert b"usb_c1" not in response.read()
                service.stop()
                with request(bridge.server_port, "/metrics") as response:
                    assert b"ecoflow_monitor_up 0\n" in response.read()
            finally:
                bridge.shutdown()
                runner.join()
    finally:
        service.stop()


class MemoryCredentials:
    def read(self, reference):
        return Credentials("synthetic-access", "synthetic-secret")


def test_https_raw_and_normalized_export_with_closed_windows(tmp_path, qapp, session_cache):
    prefs = ExportPreferences(enabled=True, port=free_port(), raw=True)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), serial="SYNTHETIC", telemetry_transport="https_only", telemetry_export=prefs,
                       device_profile_assignments=(("SYNTHETIC", "builtin:delta3max"),)))
    quota_requests = []
    release_quota = threading.Event()

    def handle(request):
        if request.url.path.endswith("list"):
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "SYNTHETIC", "deviceName": "DELTA 3 MAX", "online": 1}]})
        quota_requests.append(json.loads(request.content)["params"]["quotas"])
        assert release_quota.wait(4)
        return httpx.Response(200, json={"code": "0", "data": {
            "powInSumW": 0, "powOutSumW": 20, "cmsBattSoc": 66, "unknown": "synthetic-secret",
        }})

    controller = Controller(store, MemoryCredentials(), client_factory=lambda cred: EcoFlowClient(cred, httpx.MockTransport(handle)),
                            credential_cache=session_cache)
    try:
        controller.set_sensor_demand("dashboard", ())
        controller.start()
        assert {"battery_level", "usb_c1", "minimum_discharge_level"} <= controller.polling.required
        with request(prefs.port, "/v1/telemetry/raw") as response:
            release_quota.set()
            event = next_event(response)
            assert event["payload"]["data"]["unknown"] == "[REDACTED]"
            assert event["payload"]["data"]["cmsBattSoc"] == 66
        wait_until(qapp, lambda: controller.connection == ConnectionState.LIVE)
        readings = snapshot(prefs.port)["values"]
        assert readings["battery_level"] == 66 and readings["input_power"] == 0
        assert "cmsBattSoc" not in readings and len(quota_requests) == 1
        controller.set_system_activity(SystemActivity(locked=True))
        assert not controller.telemetry_export.running
        controller.set_system_activity(SystemActivity())
        wait_until(qapp, lambda: controller.telemetry_export.running and len(quota_requests) > 1)
        wait_until(qapp, lambda: "battery_level" in snapshot(prefs.port)["values"])
    finally:
        release_quota.set()
        controller.stop()


def test_periodic_export_keeps_cadence_and_drops_invalid_readings(tmp_path, qapp, session_cache):
    prefs = ExportPreferences(enabled=True, port=free_port(), mode="interval", interval_seconds=15)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), telemetry_export=prefs))
    clock = [100.0]
    controller = Controller(store, MemoryCredentials(), credential_cache=session_cache, clock=lambda: clock[0])
    try:
        controller.start()
        controller._next_api = math.inf

        def sample(battery):
            values = (("battery_level", battery),) if battery is not None else ()
            controller._completed("api", controller._generation, ("telemetry", TelemetryPatch(("battery_level",), values)), None)

        sample(40)
        assert "battery_level" not in snapshot(prefs.port)["values"]
        clock[0] = 115
        controller.tick()
        assert snapshot(prefs.port)["values"]["battery_level"] == 40
        sample(50)
        assert snapshot(prefs.port)["values"]["battery_level"] == 40
        sample(None)
        assert "battery_level" not in snapshot(prefs.port)["values"]
        clock[0] = 130
        sample(60)
        controller.tick()
        assert snapshot(prefs.port)["values"]["battery_level"] == 60
    finally:
        controller.stop()


def test_export_settings_stage_options_until_save_and_reset(tmp_path, qapp, session_cache):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), MemoryCredentials(), credential_cache=session_cache)
    window = SettingsWindow(controller)
    try:
        assert not window.export_controls.isEnabled()
        window.export_enabled.setChecked(True)
        window.export_port.setValue(12345)
        window.export_mode.setCurrentIndex(window.export_mode.findData("interval"))
        assert window.export_interval.isEnabled()
        window.export_switches["show_private"].setChecked(True)
        assert window.gather(False).telemetry_export == ExportPreferences(enabled=True, port=12345, mode="interval", show_private=True)
        assert not controller.settings.telemetry_export.enabled
        window.save()
        assert controller.store.load().telemetry_export.port == 12345
        assert controller.settings.telemetry_export.show_private
        assert not controller.settings.telemetry_export.show_serial
        window.export_switches["show_serial"].setChecked(True)
        window.load_draft()
        assert not window.export_switches["show_serial"].isChecked()
    finally:
        window.close()
        controller.stop()


def test_command_stream_reports_actual_manual_transition_without_arguments(tmp_path, qapp, session_cache, monkeypatch):
    prefs = ExportPreferences(enabled=True, port=free_port(), commands=True)
    profile = IntegrationProfile("active", "Active", Reaction(True, ("{app} --private-argument --watts={value:output_power}",)))
    module = IntegrationDefinition("synthetic", "Synthetic", enabled=True, executable=sys.executable,
                                   allow_launch=True, profiles=(profile,))
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), integrations=(module,), telemetry_export=prefs))
    adapter = IntegrationAdapter()
    ready = IntegrationObservation("integration_ready", executable=sys.executable)
    monkeypatch.setattr(adapter, "inspect", lambda *args: ready)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    controller = Controller(store, MemoryCredentials(), integration_adapter=adapter, credential_cache=session_cache)
    try:
        controller.start()
        with request(prefs.port, "/v1/commands") as response:
            controller.integration_states[module.id].observation = ready
            controller.request_profile(module.id, profile.id)
            assert controller.notice == "integration_values_unavailable" and not calls
            patch = TelemetryPatch(("output_power",), (("output_power", 12.25),))
            controller._completed("api", controller._generation, ("telemetry", patch), None)
            controller.request_profile(module.id, profile.id)
            start, finish = next_event(response), next_event(response)
            assert calls == [(sys.executable, "--private-argument", "--watts=12.25")]
            assert start["event"] == "started" and finish["event"] == "finished" and finish["success"]
            assert start["dispatch_id"] == finish["dispatch_id"]
            assert start["integration_id"] == "synthetic" and start["profile_id"] == "active" and start["trigger"] == "manual"
            assert start.keys() == {"version", "timestamp", "dispatch_id", "integration_id", "profile_id", "phase", "step", "trigger", "event"}
            assert finish.keys() == start.keys() | {"success", "error", "exit_code", "winerror"}
    finally:
        controller.stop()


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
def test_export_controls_fit_in_scrollable_service_at_high_scale(tmp_path, qapp, session_cache, language):
    from PySide6.QtCore import QPoint
    from ecoflow_monitor.i18n import t

    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), language=language, scale=1.5))
    controller = Controller(store, MemoryCredentials(), credential_cache=session_cache)
    window = SettingsWindow(controller)
    try:
        window.resize(880, 600)
        window.show()
        window.tabs.setCurrentIndex(0)
        window.connection_tabs.setCurrentIndex(1)
        window.export_section.set_expanded(True)
        window.export_enabled.setChecked(True)
        qapp.processEvents()
        page = window.tabs.currentWidget()
        page.ensureWidgetVisible(window.export_switches["show_private"], 0, 0)
        qapp.processEvents()
        toggle = window.export_switches["show_private"]
        assert toggle.isVisible() and toggle.accessibleName() == t("export_show_private")
        assert page.horizontalScrollBar().maximum() == 0
        assert page.viewport().rect().contains(toggle.mapTo(page.viewport(), QPoint()))
    finally:
        window.close()
        controller.stop()
