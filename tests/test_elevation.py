from dataclasses import replace
import os
from multiprocessing.connection import Listener, Pipe
import subprocess
import sys
import threading
import uuid

import pytest

from ecoflow_monitor.adapters.elevation import ElevatedSession, approved_commands, receive, send, serve
from ecoflow_monitor.adapters.elevation_windows import connect_pipe, is_helper_process, pipe_peer, requires_administrator
from ecoflow_monitor.adapters.integrations import IntegrationAdapter, IntegrationObservation
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile, Reaction, afterburner_example


def test_native_pipe_peers_and_json_protocol():
    address = "\\\\.\\pipe\\EcoFlowMonitor-" + uuid.uuid4().hex
    failures = []
    with Listener(address, family="AF_PIPE") as listener:
        def client():
            try:
                with connect_pipe(address) as connection:
                    assert pipe_peer(connection, server=True) == os.getpid()
                    send(connection, {"text": "Україна"})
                    assert receive(connection) == {"ready": True}
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        with listener.accept() as connection:
            assert pipe_peer(connection, server=False) == os.getpid()
            assert receive(connection) == {"text": "Україна"}
            send(connection, {"ready": True})
        thread.join(5)
    assert not thread.is_alive()
    assert not failures


def test_helper_executes_only_approved_commands_and_exits_on_disconnect(tmp_path):
    marker = tmp_path / "result.txt"
    arguments = ("-I", "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('done')", str(marker))
    from ecoflow_monitor.integrations import CliCommand
    module = IntegrationDefinition("test", "Test", enabled=True, executable=sys.executable, allow_launch=True,
                                   mapping=(CliCommand("write", "Write", arguments),),
                                   profiles=(IntegrationProfile("p", "P", activation=Reaction(True, ("{app} {cli:write}",))),))
    parent, child = Pipe()
    failures = []
    def worker():
        with child:
            try:
                serve(child)
            except EOFError:
                pass
            except Exception as error:
                failures.append(error)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    with parent:
        send(parent, {"module": module.to_dict(), "executable": sys.executable})
        assert receive(parent) == {"ready": True}
        send(parent, {"args": [sys.executable, *arguments]})
        assert receive(parent) == {"exit_code": 0, "winerror": None, "error": ""}
        assert marker.read_text() == "done"
        send(parent, {"args": [sys.executable, "-c", "raise SystemExit(99)"]})
    thread.join(5)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], ValueError)


def test_helper_disconnect_without_commands_exits():
    parent, child = Pipe()
    parent.close()
    with child, pytest.raises(EOFError):
        serve(child)


def test_disabled_reactions_are_not_authorized():
    module = replace(afterburner_example(), profiles=(IntegrationProfile("p", "P", slot=1,
                     activation=Reaction(False, ("{app} -profile1",)),
                     deactivation=Reaction(True, ("{app} -profile2",))),))
    assert approved_commands(module, "example.exe") == frozenset({("example.exe", "-profile2")})


def test_helper_rechecks_stopped_target(monkeypatch):
    module = replace(afterburner_example(), enabled=True, executable=r"C:\Synthetic\MSIAfterburner.exe")
    parent, child = Pipe()
    failures = []
    monkeypatch.setattr(IntegrationAdapter, "inspect", lambda *args: IntegrationObservation("integration_not_running"))
    def forbidden(*args, **kwargs):
        pytest.fail("A stopped target must never be launched")
    monkeypatch.setattr(subprocess, "run", forbidden)
    def worker():
        with child:
            try:
                serve(child)
            except EOFError:
                pass
            except Exception as error:
                failures.append(error)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    with parent:
        send(parent, {"module": module.to_dict(), "executable": module.executable})
        assert receive(parent) == {"ready": True}
        send(parent, {"args": [module.executable, "-profile1"]})
        assert receive(parent)["error"] == "integration_not_running"
    thread.join(5)
    assert not thread.is_alive() and not failures


def test_sessions_survive_auto_toggle_but_close_on_edit_and_disable():
    module = replace(afterburner_example(), enabled=True)
    session = ElevatedSession(module, "example.exe")
    adapter = IntegrationAdapter()
    adapter.sessions[module.id] = session
    adapter.retain_sessions((replace(module, auto_enabled=True),))
    assert adapter.sessions[module.id] is session
    adapter.retain_sessions((replace(module, executable="changed.exe"),))
    assert not adapter.sessions
    with pytest.raises(OSError):
        session.run(("example.exe", "-profile1"))
    adapter.sessions[module.id] = ElevatedSession(module, "example.exe")
    adapter.retain_sessions((replace(module, enabled=False),))
    assert not adapter.sessions


def test_helper_cancel_does_not_install_a_session(monkeypatch):
    module = replace(afterburner_example(), enabled=True)
    adapter = IntegrationAdapter()
    monkeypatch.setattr(adapter, "inspect", lambda *args: IntegrationObservation("integration_ready", executable="example.exe"))
    def cancel(*args):
        error = OSError("UAC cancelled")
        error.winerror = 1223
        raise error
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.launch_helper", cancel)
    with pytest.raises(OSError) as error:
        adapter.start_elevated(module)
    assert error.value.winerror == 1223
    assert not adapter.sessions


@pytest.mark.parametrize("exit_code,error", [(0, ""), (7, "integration_command_failed")])
def test_adapter_routes_approved_session_and_preserves_result(monkeypatch, exit_code, error):
    from types import SimpleNamespace
    module = replace(afterburner_example(), enabled=True, executable=sys.executable)
    adapter = IntegrationAdapter()
    calls = []
    def run(args):
        calls.append(args)
        return {"exit_code": exit_code, "winerror": None, "error": error}
    adapter.sessions[module.id] = SimpleNamespace(matches=lambda value: value == module, run=run)
    monkeypatch.setattr(adapter, "inspect", lambda value: IntegrationObservation("integration_ready", executable=sys.executable))
    def forbidden(*args, **kwargs):
        pytest.fail("Session commands must be dispatched through the helper")
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = adapter.transition(module, None, module.profiles[0], lambda: True)
    assert result.success == (exit_code == 0)
    assert result.exit_code == exit_code
    assert calls == [(sys.executable, "-profile1")]


def test_manifest_inspection_does_not_execute_invalid_binary(tmp_path):
    path = tmp_path / "example.exe"
    path.write_bytes(b"synthetic")
    assert not requires_administrator(str(path), path.stat().st_mtime_ns)


def test_packaged_helper_accepts_only_bootloader_child(monkeypatch):
    from types import SimpleNamespace
    environment = SimpleNamespace(frozen=True, executable=sys.executable)
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation_windows.sys", environment)
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation_windows.psutil.Process",
                        lambda pid: SimpleNamespace(ppid=lambda: 123, exe=lambda: sys.executable))
    assert is_helper_process(456, 123)
    assert not is_helper_process(456, 124)
    environment.frozen = False
    assert not is_helper_process(456, 123)


def test_helper_timeout_and_message_bound():
    parent, child = Pipe()
    with parent, child:
        with pytest.raises(TimeoutError):
            receive(parent, timeout=0)
        with pytest.raises(ValueError):
            send(parent, "x" * 1048577)


@pytest.mark.parametrize("wrong_peer", [False, True])
def test_session_native_handshake_pins_launched_process(monkeypatch, wrong_peer):
    module = replace(afterburner_example(), enabled=True, executable=sys.executable)
    threads = []
    failures = []
    def launch(address, parent):
        assert parent == os.getpid()
        def worker():
            with connect_pipe(address) as connection:
                try:
                    serve(connection)
                except EOFError:
                    pass
                except Exception as error:
                    failures.append(error)
        thread = threading.Thread(target=worker, daemon=True)
        threads.append(thread)
        thread.start()
        return None, os.getpid() + 100000 if wrong_peer else os.getpid()
    monkeypatch.setattr("ecoflow_monitor.adapters.elevation.launch_helper", launch)
    session = ElevatedSession(module, sys.executable)
    if wrong_peer:
        with pytest.raises(PermissionError):
            session.start()
    else:
        session.start()
        session.close()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert not failures
