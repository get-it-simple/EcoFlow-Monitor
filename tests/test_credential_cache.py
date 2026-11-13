from dataclasses import replace
import ctypes
from pathlib import Path
import threading
import time
import uuid

from PySide6.QtWidgets import QSystemTrayIcon
import pytest
from qt_helpers import close_runtime, wait_until

from ecoflow_monitor.adapters.ecoflow import Credentials, Device, EcoFlowError
from ecoflow_monitor.adapters.credentials import NativeCredential
from ecoflow_monitor.adapters.onepassword import OnePasswordError, RequestCancelled
from ecoflow_monitor.adapters.session_credentials import SessionCredentialStore, SessionStorageError, WindowsDataProtection
from ecoflow_monitor.credential_cache import SessionCredentialCache
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.application import Application
from ecoflow_monitor import i18n
from ecoflow_monitor.domain import ConnectionState, Telemetry
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.ui.credential_renewal import CredentialRenewalBanner


IDENTITY = ("op://Test/EcoFlow/username", "op://Test/EcoFlow/password")
PAIR = Credentials("synthetic-session-access", "synthetic-session-secret")




@pytest.fixture
def controller(tmp_path, qapp, session_cache):
    instance = Controller(SettingsStore(tmp_path / "settings.json"), credential_cache=session_cache)
    instance.apply_settings(replace(instance.settings, serial="synthetic-device", credential_source="1password",
                                    onepassword_access_ref=IDENTITY[0], onepassword_secret_ref=IDENTITY[1]))
    yield instance
    instance.stop()
    qapp.processEvents()


@pytest.fixture
def api(controller, monkeypatch):
    reads = []
    def load(*args):
        reads.append(True)
        return Credentials(PAIR.access_key, PAIR.secret_key + str(len(reads)))
    class Client:
        def __init__(self, credentials):
            self.credentials = credentials
        def read(self, serial):
            return Telemetry(0, 10, 80, 100, time.monotonic())
        def devices(self):
            return (Device("synthetic-device", "Synthetic station", True),)
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", load)
    controller.client_factory = Client
    return reads


def test_cache_ttl_defaults_and_roundtrips_without_changing_schema():
    payload = AppSettings().to_dict()
    del payload["ecoflow"]["onepassword"]["cache_ttl_seconds"]
    assert AppSettings.from_dict(payload).onepassword_cache_ttl_seconds == 86400
    settings = replace(AppSettings(), onepassword_cache_ttl_seconds=7200)
    assert AppSettings.from_dict(settings.to_dict()) == settings
    assert settings.to_dict()["version"] == 4


@pytest.mark.parametrize("ttl", [True, 0, 3599, 3601, 604801, 3600.0, "86400", None])
def test_invalid_ttl_is_rejected(ttl):
    with pytest.raises(ValueError, match="onepassword_cache_ttl_error"):
        replace(AppSettings(), onepassword_cache_ttl_seconds=ttl).validate()


def test_expiry_blocks_automatic_and_manual_polling_until_explicit_renewal(controller, qapp, api):
    now = [0.0]
    controller.credential_cache._clock = lambda: now[0]
    notices = []
    controller.credentials_expired.connect(lambda: notices.append(True))
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    previous = controller.telemetry
    now[0] = 86400
    controller.tick()
    for _ in range(3):
        controller.tick()
        controller.refresh()
    assert len(api) == 1
    assert notices == [True]
    assert controller.credential_renewal_required
    assert controller.credential_cache._storage.read() is None
    assert controller.telemetry is previous and not controller.fresh
    assert controller.connection == ConnectionState.AUTH_ERROR
    controller.renew_credentials()
    controller.renew_credentials()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(api) == 2
    assert not controller.credential_renewal_required
    assert controller.connection == ConnectionState.LIVE


def test_expiry_during_network_request_does_not_publish_late_telemetry(controller, qapp, api):
    now = [0.0]
    controller.credential_cache._clock = lambda: now[0]
    entered, release = threading.Event(), threading.Event()
    class Client:
        def read(self, serial):
            entered.set()
            assert release.wait(3)
            return Telemetry(0, 10, 80, 100, time.monotonic())
    controller.client_factory = lambda pair: Client()
    try:
        controller.refresh()
        wait_until(qapp, entered.is_set)
        now[0] = 86400
        controller.tick()
        release.set()
        wait_until(qapp, lambda: "api" not in controller._active)
        assert controller.telemetry is None
        assert controller.credential_renewal_required
        assert controller.credential_cache._storage.read() is None
    finally:
        release.set()


def test_failed_renewal_keeps_banner_and_does_not_retry(controller, qapp, api, monkeypatch):
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    controller.credential_cache._clock = lambda: time.monotonic() + 90000
    controller.tick()
    def reject(*args):
        raise OnePasswordError("onepassword_access_error")
    monkeypatch.setattr("ecoflow_monitor.controller.read_credentials", reject)
    banner = CredentialRenewalBanner(controller)
    try:
        banner.renew_button.click()
        assert not banner.renew_button.isEnabled()
        wait_until(qapp, lambda: "api" not in controller._active)
        controller.tick()
        assert controller.credential_renewal_required
        assert banner.renew_button.isEnabled()
        assert not banner.isHidden()
        assert controller.notice == "onepassword_access_error"
        assert controller._next_api == float("inf")
    finally:
        banner.deleteLater()


def test_network_error_after_renewal_keeps_new_cache_and_backoff(controller, qapp, api):
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    controller.credential_cache._clock = lambda: time.monotonic() + 90000
    controller.tick()
    class OfflineClient:
        def read(self, serial):
            raise EcoFlowError(ConnectionState.NETWORK_UNAVAILABLE)
    controller.client_factory = lambda pair: OfflineClient()
    controller.renew_credentials()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(api) == 2
    assert controller.credential_cache.ready
    assert not controller.credential_renewal_required
    assert controller.connection == ConnectionState.NETWORK_UNAVAILABLE
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(api) == 2


def test_storage_failure_blocks_api_without_falling_back_to_cli(controller, qapp, api, monkeypatch):
    def fail(value):
        raise SessionStorageError()
    monkeypatch.setattr(controller.credential_cache._storage, SessionCredentialStore.write.__name__, fail)
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert controller.notice == "onepassword_cache_error"
    assert controller.connection == ConnectionState.AUTH_ERROR
    assert controller.telemetry is None
    controller.tick()
    assert len(api) == 1


def test_new_references_discard_cache_and_retrieve_new_keys(controller, qapp, api):
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    controller.apply_settings(replace(controller.settings, onepassword_access_ref="op://Test/Other/username"))
    assert controller.credential_cache._storage.read() is None
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(api) == 2


def test_ttl_control_is_staged_and_reset_preserves_saved_value(controller, qapp):
    runtime = Application(controller.store.path, native=False, controller=controller)
    try:
        panel = runtime.dashboard.onepassword
        panel.cache_hours.setValue(12)
        runtime.dashboard._draft_changed()
        assert runtime.dashboard.save_button.isEnabled()
        assert controller.settings.onepassword_cache_ttl_seconds == 86400
        assert runtime.dashboard.gather(False).onepassword_cache_ttl_seconds == 43200
        runtime.dashboard.load_draft()
        assert panel.cache_hours.value() == 24
        panel.cache_hours.setValue(6)
        runtime.dashboard.save()
        assert controller.store.load().onepassword_cache_ttl_seconds == 21600
    finally:
        close_runtime(runtime, qapp)


def test_expiry_notifies_once_and_tray_click_preserves_draft(controller, qapp, api, monkeypatch):
    runtime = Application(controller.store.path, native=False, controller=controller)
    messages = []
    monkeypatch.setattr(runtime.tray, QSystemTrayIcon.isVisible.__name__, lambda: True)
    monkeypatch.setattr(runtime.tray, QSystemTrayIcon.showMessage.__name__, lambda *args: messages.append(args))
    monkeypatch.setattr("ecoflow_monitor.application.QSystemTrayIcon.supportsMessages", lambda: True)
    monkeypatch.setattr("ecoflow_monitor.application.activate_window", lambda window: None)
    runtime.native = True
    try:
        runtime.open_dashboard()
        controller.refresh()
        wait_until(qapp, lambda: "api" not in controller._active)
        runtime.dashboard.capacity.setValue(3000)
        controller.credential_cache._clock = lambda: time.monotonic() + 90000
        for _ in range(3):
            controller.tick()
        assert len(messages) == 1
        assert not runtime.dashboard.monitoring.credential_renewal.isHidden()
        assert not runtime.dashboard.credential_renewal.isHidden()
        runtime.tray.messageClicked.emit()
        assert runtime.dashboard.isVisible()
        assert runtime.dashboard.capacity.value() == 3000
    finally:
        runtime.native = False
        close_runtime(runtime, qapp)


def test_saved_discovery_and_unrelated_settings_reuse_cache(controller, qapp, api):
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    controller.apply_settings(replace(controller.settings, capacity_wh=3000))
    controller.discover()
    wait_until(qapp, lambda: "api" not in controller._active)
    assert len(api) == 1


def test_ttl_changes_keep_original_age_and_cannot_revive_expired_cache(controller, qapp, api):
    now = [0.0]
    controller.credential_cache._clock = lambda: now[0]
    controller.refresh()
    wait_until(qapp, lambda: "api" not in controller._active)
    now[0] = 7200
    controller.apply_settings(replace(controller.settings, onepassword_cache_ttl_seconds=3600))
    assert controller.credential_renewal_required
    controller.apply_settings(replace(controller.settings, onepassword_cache_ttl_seconds=604800))
    controller.tick()
    assert controller.credential_renewal_required
    assert len(api) == 1


def test_forward_utc_jump_expires_even_if_monotonic_clock_stands_still(session_cache):
    utc = [0.0]
    session_cache._utc_clock = lambda: utc[0]
    session_cache.get(IDENTITY, 3600, threading.Event(), lambda: PAIR)
    utc[0] = 3600
    assert session_cache.expire(3600)


def test_backwards_utc_jump_does_not_extend_cache(session_cache):
    now, utc = [0.0], [100000.0]
    session_cache._clock = lambda: now[0]
    session_cache._utc_clock = lambda: utc[0]
    session_cache.get(IDENTITY, 3600, threading.Event(), lambda: PAIR)
    now[0], utc[0] = 3600, 0
    assert session_cache.expire(3600)


def test_new_session_purges_crash_remainder_and_uses_fresh_entropy(session_cache):
    session_cache.get(IDENTITY, 86400, threading.Event(), lambda: PAIR)
    storage = session_cache._storage
    previous_blob = storage.read()
    previous_entropy = bytes(session_cache._entropy)
    restarted = SessionCredentialCache(storage, session_cache._protection)
    try:
        restarted.initialize()
        assert storage.read() is None
        assert bytes(restarted._entropy) != previous_entropy
        with pytest.raises(ValueError):
            restarted._protection.unprotect(previous_blob, restarted._entropy)
        rotated = Credentials("new-session-access", "new-session-secret")
        assert restarted.get(IDENTITY, 86400, threading.Event(), lambda: rotated) == rotated
    finally:
        restarted.close()


def test_cancellation_and_shutdown_reject_late_loader_results(session_cache):
    entered, release = threading.Event(), threading.Event()
    errors = []
    def load():
        entered.set()
        assert release.wait(3)
        return PAIR
    def request():
        try:
            session_cache.get(IDENTITY, 86400, threading.Event(), load)
        except RequestCancelled:
            errors.append(True)
    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert entered.wait(3)
        session_cache.close()
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert errors == [True]
        assert session_cache._storage.read() is None
        assert not any(session_cache._entropy)
    finally:
        release.set()
        worker.join(3)


def test_concurrent_requests_share_one_successful_acquisition(session_cache):
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def load():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return PAIR
    def request():
        results.append(session_cache.get(IDENTITY, 86400, threading.Event(), load))
    workers = [threading.Thread(target=request) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert entered.wait(3)
        release.set()
        for worker in workers:
            worker.join(3)
        assert results == [PAIR, PAIR]
        assert calls == [True]
    finally:
        release.set()
        for worker in workers:
            worker.join(3)


@pytest.mark.native
def test_native_dpapi_session_record_roundtrip_and_entropy_isolation():
    storage = SessionCredentialStore("EcoFlowMonitor/1PasswordSession/test-" + uuid.uuid4().hex)
    protection = WindowsDataProtection()
    entropy = bytearray(range(32))
    payload = b'["synthetic-native-access", "synthetic-native-secret"]'
    try:
        encrypted = protection.protect(payload, entropy)
        assert payload not in encrypted
        storage.write(encrypted)
        api = storage._credentials._api()
        pointer = ctypes.POINTER(NativeCredential)()
        assert api.CredReadW(storage.reference, 1, 0, ctypes.byref(pointer))
        try:
            assert pointer.contents.Persist == 1
            assert pointer.contents.UserName == "1Password session"
        finally:
            api.CredFree(pointer)
        assert protection.unprotect(storage.read(), entropy) == payload
        with pytest.raises(SessionStorageError):
            protection.unprotect(storage.read(), bytearray(reversed(entropy)))
    finally:
        storage.delete()
    assert storage.read() is None


@pytest.mark.design
@pytest.mark.parametrize("language", ["en", "uk"])
@pytest.mark.parametrize("scale", [0.5, 1, 2, 3])
def test_expiration_banner_and_ttl_layout(controller, qapp, language, scale):
    controller.apply_settings(replace(controller.settings, language=language, scale=scale, main_enabled=True, mini_enabled=True))
    controller.credential_renewal_required = True
    controller.connection = ConnectionState.AUTH_ERROR
    controller.notice = "onepassword_cache_expired"
    runtime = Application(controller.store.path, native=False, controller=controller)
    try:
        runtime.start()
        runtime.dashboard.resize(round(1184 * scale), round(900 * scale))
        controller.changed.emit()
        qapp.processEvents()
        banner = runtime.dashboard.monitoring.credential_renewal
        assert banner.renew_button.width() >= banner.renew_button.minimumSizeHint().width()
        assert banner.message.wordWrap()
        runtime.dashboard.navigation.setCurrentRow(1)
        qapp.processEvents()
        panel = runtime.dashboard.onepassword
        assert panel.cache_hours.width() >= panel.cache_hours.minimumSizeHint().width()
        assert runtime.main.controller.connection == runtime.mini.controller.connection == ConnectionState.AUTH_ERROR
        folder = Path("build/credential-preview") / f"{language}-{scale:g}"
        folder.mkdir(parents=True, exist_ok=True)
        assert runtime.dashboard.credential_renewal.grab().save(str(folder / "banner.png"))
        assert panel.references.grab().save(str(folder / "references.png"))
        assert runtime.dashboard.grab().save(str(folder / "connection.png"))
        runtime.dashboard.navigation.setCurrentRow(0)
        qapp.processEvents()
        assert runtime.dashboard.grab().save(str(folder / "monitoring.png"))
        assert runtime.main.grab().save(str(folder / "main.png"))
        assert runtime.mini.grab().save(str(folder / "mini.png"))
    finally:
        close_runtime(runtime, qapp)
        i18n.setup("en")
