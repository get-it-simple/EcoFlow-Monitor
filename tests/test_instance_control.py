import os
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QSignalSpy, QTest
import pytest

from ecoflow_monitor.adapters.instance_windows import InstanceMutex, instance_name
from ecoflow_monitor.instance_control import InstanceController


def wait_until(qapp, condition, timeout=3000):
    deadline = time.monotonic() + timeout / 1000
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(10)
    assert condition()


def worker(mode, name, directory, delay=0):
    process = QProcess()
    environment = QProcessEnvironment.systemEnvironment()
    environment.insert("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    process.setProcessEnvironment(environment)
    process.setWorkingDirectory(str(directory))
    process.start(sys.executable, [str(Path(__file__).with_name("instance_worker.py")), mode, name, "--delay", str(delay)])
    assert process.waitForStarted(3000)
    return process


def next_line(process, timeout=5000):
    deadline = time.monotonic() + timeout / 1000
    while not process.canReadLine() and time.monotonic() < deadline:
        process.waitForReadyRead(max(1, int((deadline - time.monotonic()) * 1000)))
    assert process.canReadLine(), bytes(process.readAllStandardError()).decode(errors="replace")
    return bytes(process.readLine()).strip()


def stop_worker(process):
    if process.state() != QProcess.ProcessState.NotRunning:
        process.kill()
        assert process.waitForFinished(3000)


def test_identity_is_independent_of_working_directory(tmp_path, monkeypatch):
    original = instance_name()
    monkeypatch.chdir(tmp_path)
    assert instance_name() == original
    assert original.startswith("EcoFlowMonitor.")


def test_mutex_excludes_duplicates_and_releases_on_close():
    name = "EcoFlowMonitor-test-" + uuid.uuid4().hex
    first, second = InstanceMutex(name), InstanceMutex(name)
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("delay", [0, 350])
def test_second_process_activates_primary_and_crash_allows_restart(qapp, tmp_path, delay):
    name = "EcoFlowMonitor-test-" + uuid.uuid4().hex
    primary = worker("primary", name, tmp_path, delay)
    secondary = None
    try:
        assert next_line(primary) == b"primary"
        secondary = worker("secondary", name, Path(__file__).resolve().parents[1])
        assert next_line(secondary) == b"activated"
        assert secondary.waitForFinished(3000)
        assert secondary.exitCode() == 0
        assert next_line(primary) == b"received"
        stop_worker(primary)
        replacement = InstanceController(name)
        try:
            deadline = time.monotonic() + 3
            while not replacement.start():
                assert time.monotonic() < deadline, "Terminated process retained its instance lock"
                QTest.qWait(10)
        finally:
            replacement.close()
    finally:
        stop_worker(primary)
        if secondary is not None:
            stop_worker(secondary)


def test_simultaneous_launch_has_one_primary(qapp, tmp_path):
    name = "EcoFlowMonitor-test-" + uuid.uuid4().hex
    processes = [worker("race", name, tmp_path) for _ in range(3)]
    try:
        results = [next_line(process) for process in processes]
        assert results.count(b"primary") == 1
        assert results.count(b"activated") == 2
    finally:
        for process in processes:
            stop_worker(process)


def test_fragmented_command_is_buffered_and_acknowledged(qapp):
    guard = InstanceController("EcoFlowMonitor-test-" + uuid.uuid4().hex)
    socket = QLocalSocket()
    received = bytearray()
    socket.readyRead.connect(lambda: received.extend(bytes(socket.readAll())))
    spy = QSignalSpy(guard.activation_requested)
    try:
        assert guard.start()
        socket.connectToServer(guard.name)
        wait_until(qapp, lambda: b"\n" in received)
        assert received == f"pid {os.getpid()}\n".encode()
        received.clear()
        socket.write(b"acti")
        socket.flush()
        qapp.processEvents()
        assert spy.count() == 0
        socket.write(b"vate\n")
        wait_until(qapp, lambda: received == b"ok\n")
        assert spy.count() == 1
    finally:
        socket.abort()
        guard.close()


@pytest.mark.parametrize("command", [b"unknown\n", b"activate\nextra", b"x" * 129])
def test_invalid_command_does_not_activate(qapp, command):
    guard = InstanceController("EcoFlowMonitor-test-" + uuid.uuid4().hex)
    socket = QLocalSocket()
    spy = QSignalSpy(guard.activation_requested)
    shutdown = QSignalSpy(guard.shutdown_requested)
    try:
        assert guard.start()
        socket.connectToServer(guard.name)
        wait_until(qapp, lambda: bool(guard._clients))
        socket.write(command)
        wait_until(qapp, lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        assert spy.count() == 0
        assert shutdown.count() == 0
    finally:
        socket.abort()
        guard.close()


def test_unavailable_primary_has_bounded_timeout(qapp):
    guard = InstanceController("EcoFlowMonitor-test-" + uuid.uuid4().hex)
    try:
        start = time.monotonic()
        assert not guard.activate_existing(timeout_ms=100)
        assert time.monotonic() - start < 1
    finally:
        guard.close()


@pytest.mark.parametrize("mode,expected", [("shutdown", b"stopped"), ("shutdown-wrong-path", b"failed")])
def test_shutdown_targets_matching_executable_only(qapp, tmp_path, mode, expected):
    name = "EcoFlowMonitor-test-" + uuid.uuid4().hex
    primary = worker("primary", name, tmp_path)
    secondary = None
    try:
        assert next_line(primary) == b"primary"
        secondary = worker(mode, name, tmp_path)
        assert next_line(secondary) == expected
        assert secondary.waitForFinished(3000)
        if mode == "shutdown":
            assert primary.waitForFinished(3000)
            assert primary.exitCode() == 0
        else:
            assert primary.state() == QProcess.ProcessState.Running
    finally:
        stop_worker(primary)
        if secondary is not None:
            stop_worker(secondary)


def test_shutdown_without_primary_does_not_start_one(qapp):
    guard = InstanceController("EcoFlowMonitor-test-" + uuid.uuid4().hex)
    try:
        assert not guard.shutdown_existing(timeout_ms=100)
        assert guard.start()
    finally:
        guard.close()
