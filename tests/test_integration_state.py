from dataclasses import replace
import subprocess
import os
import sys
import threading

import pytest
from qt_helpers import wait_until

from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integration_state import IntegrationStateStore, ProcessIdentity, command_fingerprint
from ecoflow_monitor.integrations import IntegrationRule, Reaction, afterburner_example
from ecoflow_monitor.settings import AppSettings, SettingsStore
from ecoflow_monitor.polling import TelemetryPatch
from ecoflow_monitor.system_activity import SystemActivity


@pytest.fixture
def module():
    return replace(afterburner_example(), enabled=True, auto_enabled=True, executable=sys.executable,
                   system_rules=(IntegrationRule("powered", "profile_1", input_condition="present"),
                                 IntegrationRule("battery", "profile_5", input_condition="absent")))


@pytest.fixture
def process():
    return ProcessIdentity(normalized_executable(sys.executable), 1234, 1000.0)


def saved_store(path, module, process):
    store = IntegrationStateStore(path)
    store.configure((module,))
    revision = store.begin(module)
    store.finish(module, revision, "profile_1", process)
    return store


def test_restore_requires_same_commands_and_process(tmp_path, module, process):
    path = tmp_path / "state.json"
    saved_store(path, module, process)
    restored = IntegrationStateStore(path)
    restored.configure((module,))
    assert restored.restore(module, process) == "profile_1"
    for other in (None, replace(process, pid=4321), replace(process, created=2000), replace(process, executable="other.exe")):
        assert restored.restore(module, other) is None
    renamed = replace(module, label="Renamed", auto_enabled=False,
                      profiles=tuple(replace(profile, label="Label", technical_details="Notes") for profile in module.profiles))
    assert command_fingerprint(renamed) == command_fingerprint(module)
    restored.configure((renamed,))
    assert restored.restore(renamed, process) == "profile_1"
    changed = replace(module, executable="other.exe")
    restored.configure((changed,))
    assert restored.restore(changed, process) is None


@pytest.mark.parametrize("change", ["commands", "delete", "disable"])
def test_configuration_invalidates_old_completions_and_readded_ids(tmp_path, module, process, change):
    path = tmp_path / "state.json"
    store = saved_store(path, module, process)
    revision = store.begin(module)
    changed = replace(module, profiles=(replace(module.profiles[0], activation=Reaction(True, ("{app} -profile2",))), *module.profiles[1:]))
    store.configure((changed,) if change == "commands" else (replace(module, enabled=False),) if change == "disable" else ())
    store.configure((module,))
    store.finish(module, revision, "profile_1", process)
    store.flush()
    restarted = IntegrationStateStore(path)
    restarted.configure((module,))
    assert restarted.restore(module, process) is None


def test_pending_transition_survives_restart_as_unknown(tmp_path, module, process):
    path = tmp_path / "state.json"
    store = saved_store(path, module, process)
    store.begin(module)
    restarted = IntegrationStateStore(path)
    restarted.configure((module,))
    assert restarted.restore(module, process) is None


@pytest.mark.parametrize("content", ["broken", "[]", '{"version":2,"modules":{}}',
                                     '{"version":1,"modules":{"bad":{"profile":[]}}}',
                                     '{"version":1,"modules":{"bad":{"profile":"p","fingerprint":"x","process":{}}}}'])
def test_corrupt_state_is_unknown(tmp_path, module, process, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    store = IntegrationStateStore(path)
    store.configure((module,))
    assert store.restore(module, process) is None


def test_atomic_failure_preserves_previous_file_and_diagnoses_success(tmp_path, module, process, monkeypatch, caplog):
    path = tmp_path / "state.json"
    store = saved_store(path, module, process)
    original = path.read_bytes()
    def fail(*args):
        raise PermissionError("Synthetic locked file")
    with monkeypatch.context() as patch:
        patch.setattr("ecoflow_monitor.integration_state.os.replace", fail)
        with pytest.raises(PermissionError):
            store.begin(module)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".integration-*.tmp"))
    revision = store.begin(module)
    with monkeypatch.context() as patch:
        patch.setattr("ecoflow_monitor.integration_state.os.replace", fail)
        store.finish(module, revision, "profile_5", process)
    assert "Successful integration state could not be saved" in caplog.text
    restarted = IntegrationStateStore(path)
    restarted.configure((module,))
    assert restarted.restore(module, process) is None


def test_parallel_modules_do_not_overwrite_each_other(tmp_path, module, process):
    second = replace(module, id="second")
    store = IntegrationStateStore(tmp_path / "state.json")
    store.configure((module, second))
    def save(selected):
        revision = store.begin(selected)
        store.finish(selected, revision, "profile_1", process)
    threads = [threading.Thread(target=save, args=(selected,)) for selected in (module, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    restarted = IntegrationStateStore(store.path)
    restarted.configure((module, second))
    assert [restarted.restore(selected, process) for selected in (module, second)] == ["profile_1", "profile_1"]


@pytest.fixture
def automation(tmp_path, qapp, module, process, monkeypatch):
    now = [100.0]
    target = [process]
    available = [True]
    fail = [False]
    commands = []
    instances = []
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(replace(AppSettings(), integrations=(module,)))
    def run(args, **kwargs):
        commands.append(args[-1])
        return subprocess.CompletedProcess(args, 7 if fail[0] else 0)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    def create():
        adapter = IntegrationAdapter()
        monkeypatch.setattr(adapter, "inspect", lambda selected: IntegrationObservation(
            "integration_ready" if available[0] else "integration_not_running", executable=sys.executable, process=target[0]))
        controller = Controller(settings_store, integration_adapter=adapter, clock=lambda: now[0])
        instances.append(controller)
        return controller
    def step(controller, at, watts=100):
        now[0] = at
        values = (("input_power", watts), ("output_power", 20), ("battery_level", 50))
        patch = TelemetryPatch(tuple(key for key, value in values), values)
        controller._completed("api", controller._generation, ("telemetry", patch), None)
        controller.tick()
        wait_until(qapp, lambda: not controller._active)
    yield create, step, now, target, available, fail, commands
    for controller in instances:
        controller.stop()
    qapp.processEvents()


def test_two_input_rules_failure_revalidation_and_no_retry_loop(automation):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    step(controller, 101)
    fail[0] = True
    step(controller, 106)
    assert commands == ["-profile1"]
    step(controller, 120)
    assert commands == ["-profile1"]
    fail[0] = False
    step(controller, 121, 0)
    step(controller, 126, 0)
    assert commands == ["-profile1", "-profile5"]
    step(controller, 140, None)
    assert len(commands) == 2
    step(controller, 141)
    step(controller, 146)
    assert commands == ["-profile1", "-profile5", "-profile1"]


def test_restart_monitor_suppresses_repeat_but_new_target_reapplies(automation):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    assert commands == ["-profile1"]
    controller.stop()
    restarted = create()
    step(restarted, 110)
    step(restarted, 116)
    assert commands == ["-profile1"]
    target[0] = replace(target[0], created=2000)
    step(restarted, 120)
    step(restarted, 121)
    step(restarted, 126)
    assert commands == ["-profile1", "-profile1"]


@pytest.mark.parametrize("watts,expected", [(100, ["-profile1"]), (0, ["-profile1", "-profile5"])])
def test_sleep_preserves_success_and_rechecks_changed_input(automation, qapp, watts, expected):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    controller.set_system_activity(SystemActivity(sleeping=True))
    controller.set_system_activity(SystemActivity())
    qapp.processEvents()
    wait_until(qapp, lambda: not controller._active)
    step(controller, 120, watts)
    assert controller.fresh_for({"input_power"})
    assert not controller._resume_scans
    step(controller, 125, watts)
    assert commands == expected


def test_failed_command_retries_once_after_availability_recovers(automation):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    fail[0] = True
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    available[0] = False
    step(controller, 110)
    step(controller, 111)
    available[0] = True
    fail[0] = False
    step(controller, 120)
    step(controller, 121)
    assert controller.fresh_for({"input_power"})
    assert not controller._resume_scans
    step(controller, 126)
    assert commands == ["-profile1", "-profile1"]


def test_state_write_failure_blocks_command_and_manual_msi_can_resend(automation, monkeypatch, qapp):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    original = os.replace
    def blocked(source, destination):
        if destination == controller.integration_cache.path:
            raise PermissionError("Synthetic write failure")
        return original(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr("ecoflow_monitor.integration_state.os.replace", blocked)
        controller.request_profile("afterburner", "profile_1")
        wait_until(qapp, lambda: not controller._active)
    assert commands == [] and controller.notice == "profile_failed"
    for _ in range(2):
        controller.request_profile("afterburner", "profile_1")
        wait_until(qapp, lambda: not controller._active)
    assert commands == ["-profile1", "-profile1"]


def test_failure_retries_once_after_sleep_with_new_telemetry(automation, qapp):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    fail[0] = True
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    controller.set_system_activity(SystemActivity(sleeping=True))
    controller.set_system_activity(SystemActivity())
    qapp.processEvents()
    wait_until(qapp, lambda: not controller._active)
    now[0] = 120
    controller.tick()
    wait_until(qapp, lambda: not controller._active)
    assert commands == ["-profile1"]
    fail[0] = False
    step(controller, 121)
    step(controller, 126)
    step(controller, 140)
    assert commands == ["-profile1", "-profile1"]


@pytest.mark.parametrize("count", [1, 2])
def test_sleep_during_command_persists_only_complete_transition(automation, qapp, monkeypatch, count):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    module = controller.settings.integrations[0]
    profile = replace(module.profiles[0], activation=Reaction(True, ("{app} -profile1",) * count))
    module = replace(module, profiles=(profile, *module.profiles[1:]))
    controller.apply_settings(replace(controller.settings, integrations=(module,)))
    step(controller, 100)
    started, release = threading.Event(), threading.Event()
    def run(args, **kwargs):
        commands.append(args[-1])
        started.set()
        assert release.wait(4)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    try:
        controller.request_profile(module.id, profile.id, automatic=True)
        wait_until(qapp, started.is_set)
        controller.set_system_activity(SystemActivity(sleeping=True))
        controller.set_system_activity(SystemActivity())
        qapp.processEvents()
        release.set()
        wait_until(qapp, lambda: not controller._active)
        restarted = IntegrationStateStore(controller.integration_cache.path)
        restarted.configure((module,))
        assert commands == ["-profile1"]
        assert restarted.restore(module, target[0]) == (profile.id if count == 1 else None)
    finally:
        release.set()


def test_configuration_does_not_wait_for_disk_io(tmp_path, module, process, monkeypatch):
    store = saved_store(tmp_path / "state.json", module, process)
    started, release = threading.Event(), threading.Event()
    original = os.replace
    def slow(source, destination):
        started.set()
        assert release.wait(4)
        return original(source, destination)
    monkeypatch.setattr("ecoflow_monitor.integration_state.os.replace", slow)
    thread = threading.Thread(target=lambda: store.begin(module))
    try:
        thread.start()
        assert started.wait(3)
        store.configure(())
        assert store.dirty
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive()
    store.flush()
    restarted = IntegrationStateStore(store.path)
    restarted.configure((module,))
    assert restarted.restore(module, process) is None


def test_old_scan_cannot_restore_profile_before_later_success(automation, qapp):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    state = controller.integration_states["afterburner"]
    module = controller.settings.integrations[0]
    old_scan = controller._inspect_integration(module, state.dispatch_revision)
    controller.request_profile(module.id, "profile_5")
    wait_until(qapp, lambda: not controller._active)
    controller._completed("scan:" + module.id, controller._generation, old_scan, None)
    assert state.active_profile == "profile_5"
    assert controller.integration_cache.restore(module, target[0]) == "profile_5"


def test_unknown_process_does_not_suppress_after_monitor_restart(automation):
    create, step, now, target, available, fail, commands = automation
    target[0] = None
    controller = create()
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    step(controller, 120)
    assert commands == ["-profile1"]
    controller.stop()
    restarted = create()
    step(restarted, 130)
    step(restarted, 131)
    step(restarted, 136)
    assert commands == ["-profile1", "-profile1"]


def test_label_rule_and_auto_changes_preserve_success(automation):
    create, step, now, target, available, fail, commands = automation
    controller = create()
    step(controller, 100)
    step(controller, 101)
    step(controller, 106)
    module = controller.settings.integrations[0]
    changed = replace(module, label="Renamed", system_rules=tuple(replace(rule, id="new_" + rule.id) for rule in module.system_rules))
    controller.apply_settings(replace(controller.settings, integrations=(changed,)))
    controller.set_auto(module.id, False)
    controller.set_auto(module.id, True)
    step(controller, 120)
    step(controller, 125)
    assert commands == ["-profile1"]
    assert controller.integration_states[module.id].active_profile == "profile_1"
