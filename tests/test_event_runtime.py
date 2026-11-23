import ctypes
from ctypes import wintypes
from dataclasses import replace
from select import select
import subprocess
import sys
import threading

import pytest

from ecoflow_monitor.adapters.ecoflow_mqtt import MqttCancellation
from ecoflow_monitor.adapters.integrations import ProcessEntry, integration_process_names, process_entry
from ecoflow_monitor.adapters.power_efficiency import EXECUTION_SPEED, IDLE_PRIORITY_CLASS, PROCESS_POWER_THROTTLING, PowerThrottlingState, enable_efficiency_mode
from ecoflow_monitor.adapters.process_events import ProcessExitNotifications, ProcessNotice, open_process, relevant_processes
from ecoflow_monitor.controller import Controller
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationDefinition
from ecoflow_monitor.settings import AppSettings, SettingsStore
from qt_helpers import wait_until as wait_for_condition


def test_idle_controller_does_not_arm_a_heartbeat(tmp_path, qapp, session_cache):
    controller = Controller(SettingsStore(tmp_path / "settings.json"), credential_cache=session_cache)
    try:
        controller.start(authorized=False)
        assert not controller._timer.isActive()
        controller.wake()
        wait_for_condition(qapp, lambda: not controller._timer.isActive())
        assert not controller._active
    finally:
        controller.stop()


def test_target_basename_includes_the_resolved_executable(monkeypatch, tmp_path):
    selected = tmp_path / "selected.exe"
    target = tmp_path / "actual.exe"
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.local_executable", lambda path: str(target))
    module = IntegrationDefinition("tool", "Tool", enabled=True, executable=str(selected))
    assert integration_process_names((module,)) == frozenset({"selected.exe", "actual.exe"})


def test_process_feed_updates_observations_without_scanning_and_recovers(tmp_path, qapp, session_cache, monkeypatch):
    now = [1000.0]
    exe = tmp_path / "profile-tool.exe"
    exe.touch()
    name = exe.name.casefold()
    path = normalized_executable(str(exe))
    module = IntegrationDefinition("one", "One", enabled=True, executable=str(exe))
    other = replace(module, id="two", label="Two")
    store = SettingsStore(tmp_path / "settings.json")
    store.save(replace(AppSettings(), integrations=(module, other)))
    first = ProcessEntry(10001, name, path, 100.0)
    second = ProcessEntry(10002, name, path, 110.0)
    departing = ProcessEntry(10003, name, path, 90.0)
    current = [first, second, departing]
    scans = []
    sampling, release = threading.Event(), threading.Event()

    def scan(names):
        assert names == frozenset({name})
        result = tuple(current)
        scans.append(now[0])
        if len(scans) == 1:
            sampling.set()
            assert release.wait(4)
        return result

    subscriptions = []

    def subscribe(modules, cancelled, publish):
        subscriptions.append((cancelled, publish))
        publish("ready", None)
        cancelled.wait(3)

    monkeypatch.setattr("ecoflow_monitor.controller.relevant_processes", scan)
    monkeypatch.setattr("ecoflow_monitor.adapters.process_events.open_process", lambda entry: None)
    controller = Controller(store, clock=lambda: now[0], credential_cache=session_cache, native_process_events=True)
    tracker = controller._processes
    assert tracker is not None
    try:
        tracker._subscribe = subscribe
        controller.start(authorized=False)
        wait_for_condition(qapp, sampling.is_set)
        cancelled, publish = subscriptions[0]
        publish("process", ProcessNotice(departing.pid, name, 120.0, False))
        wait_for_condition(qapp, lambda: len(tracker._snapshot_events) == 1)
        release.set()

        def both_match(pid):
            return all(state.observation.process is not None and state.observation.process.pid == pid
                       for state in controller.integration_states.values())

        wait_for_condition(qapp, lambda: not controller._timer.isActive()
                           and all(state.observation.ready for state in controller.integration_states.values()))
        assert scans == [1000.0]
        assert all(state.observation.process is None for state in controller.integration_states.values())
        assert not controller._timer.isActive()

        assert len(tracker.snapshot().entries) == 2
        publish("process", ProcessNotice(first.pid, name, 120.0, False))
        wait_for_condition(qapp, lambda: both_match(second.pid))
        restarted = replace(first, created=130.0)
        publish("process", ProcessNotice(restarted.pid, name, 130.0, True, restarted))
        publish("process", ProcessNotice(restarted.pid, name, 120.0, False))
        publish("process", ProcessNotice(second.pid, name, 140.0, False))
        wait_for_condition(qapp, lambda: both_match(restarted.pid))
        assert scans == [1000.0]

        current[:] = [second]
        publish("lost", "synthetic")
        wait_for_condition(qapp, lambda: both_match(second.pid))
        assert scans == [1000.0, 1000.0]
        publish("lost", "synthetic retry denied")
        wait_for_condition(qapp, lambda: tracker._next_retry == 1010.0)
        assert scans == [1000.0, 1000.0]
        tracker._watchers.exited.emit(second)
        wait_for_condition(qapp, lambda: not any(state.observation.ready for state in controller.integration_states.values()))
        assert scans == [1000.0, 1000.0]
        replacement = replace(second, pid=10004, created=150.0)
        current[:] = [replacement]
        now[0] += 2
        controller.wake()
        wait_for_condition(qapp, lambda: len(scans) == 3 and both_match(replacement.pid))

        cancelled.set()
        now[0] += 8
        controller.wake()
        wait_for_condition(qapp, lambda: len(subscriptions) == 2 and len(scans) >= 4
                           and not controller._active and not controller._timer.isActive())
        assert both_match(replacement.pid)
        assert not controller._timer.isActive()
    finally:
        release.set()
        controller.stop()


def test_mqtt_cancellation_wakes_a_blocking_socket_wait():
    cancelled = MqttCancellation()
    with cancelled.wakeup_socket() as reader:
        assert not select([reader], [], [], 0)[0]
        cancelled.set()
        assert select([reader], [], [], 0)[0] == [reader]


@pytest.mark.native
@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles required")
def test_windows_signals_exit_of_known_process(qapp):
    watcher = ProcessExitNotifications()
    exited = []
    watcher.exited.connect(exited.append)
    with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE) as child:
        try:
            assert child.stdin is not None
            entry = process_entry(child.pid)
            assert entry is not None and entry.identity is not None
            assert entry in relevant_processes(frozenset({entry.name}))
            watcher.reconcile([entry], open_process)
            assert not exited
            child.stdin.close()
            wait_for_condition(qapp, lambda: exited == [entry], timeout=4000)
            assert child.wait(timeout=4) == 0
        finally:
            watcher.clear()
            if child.poll() is None:
                child.terminate()


def probe_efficiency():
    assert enable_efficiency_mode()
    windows = ctypes.WinDLL("kernel32", use_last_error=True)
    windows.GetCurrentProcess.restype = wintypes.HANDLE
    process = windows.GetCurrentProcess()
    windows.GetPriorityClass.argtypes = [wintypes.HANDLE]
    windows.GetPriorityClass.restype = wintypes.DWORD
    assert windows.GetPriorityClass(process) == IDLE_PRIORITY_CLASS
    windows.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    windows.GetProcessInformation.restype = wintypes.BOOL
    state = PowerThrottlingState(1, 0, 0)
    assert windows.GetProcessInformation(process, PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)), ctypes.get_last_error()
    assert state.ControlMask & EXECUTION_SPEED and state.StateMask & EXECUTION_SPEED


@pytest.mark.native
@pytest.mark.skipif(sys.platform != "win32" or sys.getwindowsversion().build < 22000, reason="Windows 11 EcoQoS required")
def test_windows_efficiency_state_is_applied_to_the_current_process():
    probe = "import os, runpy, sys; sys.path.insert(0, os.path.dirname(sys.argv[1])); runpy.run_path(sys.argv[1])['probe_efficiency']()"
    result = subprocess.run([sys.executable, "-c", probe, __file__], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
