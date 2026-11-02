from dataclasses import replace
import json
from multiprocessing.connection import Listener
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
import threading
import uuid

from ecoflow_monitor.adapters.elevation_windows import close_handle, connect_pipe, is_helper_process, launch_helper, pipe_peer


MAX_MESSAGE = 1048576


def send(connection, value):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("Helper message is too large")
    connection.send_bytes(data)


def receive(connection, timeout=10):
    try:
        if not connection.poll(timeout):
            raise TimeoutError("Elevated helper did not respond")
        return json.loads(connection.recv_bytes(MAX_MESSAGE))
    except OSError as error:
        if getattr(error, "winerror", None) in (109, 232):
            raise EOFError("Helper pipe disconnected") from error
        raise


def approved_commands(module, executable):
    return frozenset(module.expand(command, executable)
                     for profile in module.profiles
                     for reaction in (profile.activation, profile.deactivation) if reaction.enabled
                     for command in reaction.commands)


class ElevatedSession:
    def __init__(self, module, executable):
        self.module = replace(module, auto_enabled=False)
        self.executable = executable
        self.connection = None
        self.handle = None
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def matches(self, module):
        return self.module == replace(module, auto_enabled=False)

    def start(self):
        address = "\\\\.\\pipe\\EcoFlowMonitor-" + uuid.uuid4().hex
        accepted = Queue()
        handover = threading.Lock()
        with Listener(address, family="AF_PIPE") as listener:
            try:
                self.handle, pid = launch_helper(address, os.getpid())
                def accept():
                    try:
                        connection = listener.accept()
                        with handover:
                            if self._closed.is_set():
                                connection.close()
                            else:
                                accepted.put(connection)
                    except OSError as error:
                        accepted.put(error)
                threading.Thread(target=accept, daemon=True).start()
                try:
                    connection = accepted.get(timeout=15)
                except Empty:
                    with handover:
                        self._closed.set()
                        try:
                            abandoned = accepted.get_nowait()
                        except Empty:
                            abandoned = None
                        if abandoned is not None and not isinstance(abandoned, Exception):
                            abandoned.close()
                    raise TimeoutError("Elevated helper did not connect") from None
                if isinstance(connection, Exception):
                    raise connection
                self.connection = connection
                if not is_helper_process(pipe_peer(connection, server=False), pid):
                    raise PermissionError("Unexpected helper process")
                send(connection, {"module": self.module.to_dict(), "executable": self.executable})
                if receive(connection) != {"ready": True}:
                    raise ValueError("Invalid helper handshake")
            except Exception:
                self.close()
                raise

    def run(self, args):
        with self._lock:
            if self._closed.is_set() or self.connection is None:
                raise OSError("Elevated helper is disconnected")
            try:
                send(self.connection, {"args": list(args)})
                response = receive(self.connection)
                if not isinstance(response, dict) or set(response) != {"exit_code", "winerror", "error"}:
                    raise ValueError("Invalid helper response")
                return response
            except (OSError, EOFError, ValueError, TimeoutError):
                self.close()
                raise

    def close(self):
        self._closed.set()
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.handle is not None:
            close_handle(self.handle)
            self.handle = None


def serve(connection):
    from ecoflow_monitor.adapters.integrations import IntegrationAdapter
    from ecoflow_monitor.integrations import IntegrationDefinition
    bootstrap = receive(connection)
    module = IntegrationDefinition.from_dict(bootstrap["module"])
    executable = bootstrap["executable"]
    module = replace(module, executable=executable, enabled=True)
    allowed = approved_commands(module, executable)
    adapter = IntegrationAdapter()
    send(connection, {"ready": True})
    while True:
        request = receive(connection, timeout=None)
        args = request.get("args") if isinstance(request, dict) else None
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args) or tuple(args) not in allowed:
            raise ValueError("Command is outside the approved integration")
        observation = adapter.inspect(module)
        if not observation.ready or observation.executable != executable:
            send(connection, {"exit_code": None, "winerror": None, "error": observation.availability})
            continue
        try:
            result = subprocess.run(args, shell=False, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                                    cwd=str(Path(executable).parent), check=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            response = {"exit_code": result.returncode, "winerror": None,
                        "error": "" if result.returncode == 0 else "integration_command_failed"}
        except subprocess.TimeoutExpired:
            response = {"exit_code": None, "winerror": None, "error": "integration_timeout"}
        except OSError as error:
            code = getattr(error, "winerror", None)
            reason = {5: "integration_permission_denied", 740: "integration_elevation_required"}.get(code, "integration_command_failed")
            response = {"exit_code": None, "winerror": code, "error": reason}
        send(connection, response)


def helper_main(arguments):
    try:
        if len(arguments) != 3 or arguments[0] != "--elevated-helper" or not arguments[1].startswith("\\\\.\\pipe\\EcoFlowMonitor-"):
            return 2
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            return 3
        with connect_pipe(arguments[1]) as connection:
            if pipe_peer(connection, server=True) != int(arguments[2]):
                return 4
            serve(connection)
    except (OSError, EOFError, ValueError, KeyError, TypeError, TimeoutError):
        return 1
    return 0
