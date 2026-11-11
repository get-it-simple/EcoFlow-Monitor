from dataclasses import replace
import json
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QRectF
from PySide6.QtGui import QImage, QPainter
import pytest

from ecoflow_monitor import i18n
from ecoflow_monitor.adapters.ecoflow import normalize_telemetry
from ecoflow_monitor.application import Application
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.motion import RoleTransition, ValueTransition, derive_motion
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.ui.tokens import MOTION
from ecoflow_monitor.ui.motion import wave
from ecoflow_monitor.ui.motion import MotionSurface
from ecoflow_monitor.ui.common import status_text


def snapshot(incoming=640, outgoing=185, **changes):
    return replace(Telemetry(incoming, outgoing, 72, 1700000000, 1000), **changes)


def derive(data, previous="idle", fresh=True, connection=ConnectionState.LIVE):
    return derive_motion(data, connection, fresh, previous, MOTION["powerThresholds"])


@pytest.mark.parametrize("incoming,outgoing,previous,mode,balance", [
    (640, 185, "idle", "charging", "input"),
    (0, 185, "idle", "discharging", "output"),
    (185, 185, "charging", "idle", None),
    (0, 0, "discharging", "idle", None),
    (8, 0, "idle", "idle", None),
    (8.01, 0, "idle", "charging", "input"),
    (6, 0, "charging", "charging", "input"),
    (5, 0, "charging", "idle", None),
    (0, 6, "discharging", "discharging", "output"),
    (0, 5, "discharging", "idle", None),
])
def test_direction_and_hysteresis(incoming, outgoing, previous, mode, balance):
    state = derive(snapshot(incoming, outgoing), previous)
    assert (state.mode, state.balance) == (mode, balance)
    assert state.net == incoming - outgoing


@pytest.mark.parametrize("state,expected", [(0, "idle"), (1, "discharging"), (2, "charging"), (3, "charging"), (True, "charging"), (None, "charging")])
def test_battery_api_priority_does_not_change_measured_channels(state, expected):
    result = derive(snapshot(charging_state=state))
    assert result.mode == expected
    assert result.input_active and result.output_active
    assert result.balance == "input"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1, True])
def test_invalid_channel_does_not_become_zero_or_invent_balance(bad):
    result = derive(snapshot(incoming=bad))
    assert result.mode == "unknown" and result.net is None and result.balance is None
    assert not result.input_active and result.output_active


@pytest.mark.parametrize("connection,status", [(ConnectionState.STALE, "stale"), (ConnectionState.CONNECTING, "connecting"), (ConnectionState.DEVICE_OFFLINE, "offline"), (ConnectionState.AUTH_ERROR, "error")])
def test_non_live_states_stop_energy_without_losing_last_reading(connection, status):
    state = derive(snapshot(ac_enabled=True, dc_enabled=True), fresh=False, connection=connection)
    assert state.status == status and state.mode == "unknown"
    assert state.soc == 72 and state.net == 455
    assert not any((state.input_active, state.output_active, state.ac_active, state.dc_active, state.balance))


@pytest.mark.parametrize("soc,expected", [(-1, 0), (0, 0), (100, 100), (105, 100), (None, None), (float("nan"), None)])
def test_charge_geometry_clamps_without_mutating_telemetry(soc, expected):
    telemetry = snapshot(soc_pct=soc)
    assert derive(telemetry).soc == expected
    assert telemetry.soc_pct is soc


@pytest.mark.parametrize("value,expected", [(4, False), (0, True), (14, True), ("4", False), ("12", True), (None, None), (True, None), (-1, None), (1.5, None), ("bad", None)])
def test_official_outlet_state_mapping_is_independent_of_power(value, expected):
    data = normalize_telemetry({"powInSumW": 0, "powOutSumW": 0, "cmsBattSoc": 72, "flowInfoAcOut": value, "flowInfo12v": value}, 0, 0)
    assert data.ac_enabled is expected and data.dc_enabled is expected
    state = derive(data)
    assert state.ac_active is (expected is True)
    assert not state.output_active


def test_value_transition_retargets_current_geometry_and_catches_up():
    transition = ValueTransition()
    transition.set(10, 0, 500)
    transition.set(90, 100, 500)
    middle = transition.value(350)
    assert 10 < middle < 90
    transition.set(20, 350, 500)
    assert transition.value(350) == middle
    assert transition.value(5000) == 20
    assert not transition.active(5000)
    transition.set(30, 5000, 0)
    assert transition.value(5000) == 30


def test_direction_transition_finishes_old_direction_before_new_one():
    transition = RoleTransition()
    transition.set("charging", 0, MOTION["timing"], True)
    assert transition.sample(1000) == ("charging", 1)
    transition.set("discharging", 1000, MOTION["timing"], True)
    assert transition.sample(1075) == ("charging", 0.5)
    assert transition.sample(1150) == ("discharging", 0)
    assert transition.sample(1250) == ("discharging", 0.5)
    assert transition.sample(1350) == ("discharging", 1)


@pytest.fixture
def runtime(tmp_path, qapp, monkeypatch):
    failures = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, traceback: failures.append(error))
    instance = Application(tmp_path / "settings.json", native=False)
    clock = [1000.0]
    instance.controller.clock = lambda: clock[0]
    instance.test_clock = clock
    yield instance
    instance.stop()
    for widget in (instance.dashboard, instance.mini, instance.main):
        widget.deleteLater()
    instance.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    i18n.setup("en")
    assert not failures, failures


def live(runtime, **changes):
    runtime.controller.telemetry = snapshot(**changes)
    runtime.controller.connection = ConnectionState.LIVE
    runtime.controller.changed.emit()


def test_single_clock_is_shared_and_delayed_frames_do_not_slow_it(runtime, qapp):
    live(runtime)
    runtime.controller.update_preferences(main_enabled=True, mini_enabled=True)
    runtime.open_dashboard(0)
    qapp.processEvents()
    driver = runtime.motion_driver
    assert runtime.main.progress.motion is runtime.mini.shell.motion is runtime.dashboard.monitoring.balance.motion is driver
    start = driver.phase("fieldWave")
    for step in (0.016, 0.100, 1.5, 3.0):
        runtime.test_clock[0] = 1000 + step
        driver.tick()
        assert driver.phase("fieldWave") == pytest.approx((start + step * 1000 / 2800) % 1)
    runtime.mini.hide()
    runtime.test_clock[0] += 2
    runtime.mini.show()
    assert runtime.mini.shell.motion.phase("fieldWave") == driver.phase("fieldWave")


def test_animation_frames_never_change_readings_or_save_settings(runtime, qapp):
    live(runtime)
    runtime.open_dashboard(0)
    qapp.processEvents()
    runtime.dashboard._geometry_timer.stop()
    original = runtime.controller.telemetry
    labels = {key: reading.value.text() for key, reading in runtime.dashboard.monitoring.readings.items()}
    runtime.test_clock[0] += 10
    runtime.motion_driver.tick()
    assert runtime.controller.telemetry is original
    assert {key: reading.value.text() for key, reading in runtime.dashboard.monitoring.readings.items()} == labels
    assert not runtime.controller.store.path.exists()


def test_all_hidden_views_release_repaint_timer_and_resume_shared_phase(runtime, qapp):
    live(runtime)
    runtime.controller.update_preferences(main_enabled=True, mini_enabled=True)
    runtime.open_dashboard(0)
    qapp.processEvents()
    assert runtime.motion_driver._timer.isActive()
    runtime.controller.update_preferences(main_enabled=False, mini_enabled=False)
    runtime.dashboard.request_visibility(False)
    qapp.processEvents()
    assert not runtime.motion_driver._timer.isActive(), [(type(view).__name__, view.window().objectName()) for view in runtime.motion_driver._views if runtime.motion_driver.visible(view)]
    runtime.controller.update_preferences(main_enabled=True)
    qapp.processEvents()
    assert runtime.motion_driver._timer.isActive()


def test_destroyed_subscriber_can_be_retained_by_python_without_later_callbacks(runtime, qapp):
    live(runtime)
    surface = MotionSurface(runtime.controller, "input")
    surface.show()
    qapp.processEvents()
    assert runtime.motion_driver._timer.isActive()
    surface.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    runtime.motion_driver.refresh()
    runtime.motion_driver.tick()
    assert not runtime.motion_driver._timer.isActive()


def test_delayed_frame_stops_energy_and_reports_stale_without_new_api_data(runtime):
    live(runtime)
    telemetry = runtime.controller.telemetry
    runtime.test_clock[0] += 91
    runtime.motion_driver.tick()
    assert runtime.motion_driver.state.status == "stale"
    assert not runtime.motion_driver.state.input_active
    assert runtime.motion_driver.roles["battery"].sample(runtime.motion_driver.now())[0] is None
    assert i18n.t("stale_data") in status_text(runtime.controller)
    assert runtime.controller.telemetry is telemetry


def test_global_switch_system_preference_and_cancel_apply_to_every_effect(runtime, qapp):
    live(runtime, ac_enabled=True)
    runtime.open_dashboard(0)
    qapp.processEvents()
    runtime.dashboard.animations.setChecked(False)
    runtime.dashboard._draft_changed()
    assert not runtime.motion_driver.enabled and not runtime.motion_driver._timer.isActive()
    assert runtime.controller.settings.animations
    runtime.dashboard.load_draft()
    assert runtime.motion_driver.enabled
    qapp.setProperty("system_animations", False)
    assert not runtime.motion_driver.enabled and not runtime.motion_driver._timer.isActive()
    qapp.setProperty("system_animations", True)
    runtime.dashboard.capacity.setValue(3456)
    runtime.dashboard.animations.setChecked(False)
    runtime.dashboard._draft_changed()
    runtime.dashboard.save()
    assert not runtime.motion_driver.enabled
    assert not SettingsStore(runtime.controller.store.path).load().animations
    assert runtime.controller.settings.capacity_wh == 3456


def test_same_error_never_replays_entry_and_recovery_allows_new_entry(runtime):
    runtime.controller.connection = ConnectionState.API_ERROR
    runtime.controller.changed.emit()
    started = runtime.motion_driver.error_started
    runtime.test_clock[0] += 5
    runtime.controller.changed.emit()
    assert runtime.motion_driver.error_started == started
    live(runtime)
    runtime.controller.connection = ConnectionState.API_ERROR
    runtime.controller.changed.emit()
    assert runtime.motion_driver.error_started > started
    runtime.controller.update_preferences(animations=False)
    assert runtime.motion_driver.error_started is None


def test_main_action_preserves_dashboard_shortcut_and_drafts(runtime, qapp):
    runtime.start()
    assert not runtime.main.isVisible()
    runtime.dashboard.capacity.setValue(3456)
    runtime.dashboard._draft_changed()
    runtime.hotkey("toggle_main")
    assert runtime.main.isVisible() and runtime.controller.settings.main_enabled
    assert runtime.dashboard.capacity.value() == 3456
    runtime.hotkey("toggle_monitor")
    assert not runtime.dashboard.isVisible() and runtime.main.isVisible()
    runtime.hotkey("toggle_monitor")
    assert runtime.dashboard.capacity.value() == 3456
    runtime.main.close()
    assert not runtime.main.isVisible() and not runtime.controller.settings.main_enabled
    binding = next(binding for binding in runtime.controller.settings.hotkeys if binding.action == "toggle_main")
    assert not binding.enabled and not binding.combo


def test_main_click_through_requires_recovery_and_recovery_clears_both_drafts(runtime):
    settings = replace(runtime.controller.settings, main_overlay=replace(runtime.controller.settings.main_overlay, click_through=True))
    with pytest.raises(ValueError, match="click_through_unavailable"):
        runtime.preflight_settings(settings)
    runtime.dashboard.overlay_fields["main"]["click_through"].setChecked(True)
    runtime.dashboard.capacity.setValue(3456)
    runtime.dashboard._draft_changed()
    runtime.recover_widgets()
    assert not runtime.dashboard.gather(False).main_overlay.click_through
    assert not runtime.dashboard.gather(False).mini_overlay.click_through
    assert runtime.dashboard.gather(False).capacity_wh == 3456


def test_legacy_main_preferences_survive_and_new_main_defaults_are_disabled(tmp_path):
    data = replace(AppSettings(), scale=1.5, background_opacity=0.27, monitor_geometry=(10, 20, 512, 350)).to_dict()
    data["workspace"].pop("main_enabled")
    data["workspace"].pop("main_scale")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    settings = SettingsStore(path).load()
    assert not settings.main_enabled and settings.main_scale == 1.5
    assert settings.background_opacity == 0.27 and settings.monitor_geometry == (10, 20, 512, 350)
    assert path.read_bytes() == before


def test_packaged_motion_tokens_match_reference_when_available():
    reference = Path(__file__).resolve().parents[1] / "design/EcoFlow-Monitor-Design-System/motion-tokens.json"
    if reference.exists():
        assert MOTION == json.loads(reference.read_text(encoding="utf-8"))


@pytest.mark.design
@pytest.mark.parametrize("opacity", [0, 0.82, 1])
@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_main_composition_keeps_readings_and_controls_inside_window(runtime, qapp, opacity, scale):
    live(runtime)
    runtime.controller.update_preferences(main_enabled=True, main_scale=scale, background_opacity=opacity, animations=False)
    runtime.main.resize(round(360 * scale), round(320 * scale))
    qapp.processEvents()
    assert runtime.main.rect().contains(QRect(runtime.main.close_button.mapTo(runtime.main, QPoint()), runtime.main.close_button.size()))
    for button in (runtime.main.close_button, runtime.main.open_button, runtime.main.overlay.pin, runtime.main.overlay.menu_button):
        assert button.width() == button.height() == round(36 * scale)
    for reading in (runtime.main.battery, runtime.main.runtime, *runtime.main.readings.values()):
        assert runtime.main.rect().contains(QRect(reading.mapTo(runtime.main, QPoint()), reading.size()))
        assert reading.value.isVisible()
        assert reading.value.fontMetrics().horizontalAdvance(reading.value.text()) <= reading.value.width()
    assert runtime.main.battery.value.outlined is (opacity == 0)


@pytest.mark.design
@pytest.mark.parametrize("material", ["Panel", "Overlay", "Clear"])
def test_wave_has_token_alpha_direction_and_invisible_cycle_boundary(material):
    def render(role, phase):
        image = QImage(200, 80, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        wave(painter, QRectF(0, 0, 200, 80), role, phase, material)
        painter.end()
        return image

    for phase in (0, 1):
        image = render("charging", phase)
        assert all(image.pixelColor(x, 40).alpha() == 0 for x in range(200))
    peaks = []
    for role, phase in (("charging", 0.25), ("charging", 0.75), ("discharging", 0.25), ("discharging", 0.75)):
        image = render(role, phase)
        alphas = [image.pixelColor(x, 40).alpha() for x in range(2, 198)]
        assert 0 < max(alphas) <= round(MOTION["materials"][material]["waveMaxAlpha"] * 255) + 1
        peaks.append(alphas.index(max(alphas)))
    assert peaks[0] < peaks[1] and peaks[2] > peaks[3]
