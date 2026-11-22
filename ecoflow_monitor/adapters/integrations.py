from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
from typing import Callable

import psutil

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile
from ecoflow_monitor.integration_state import ProcessIdentity
from ecoflow_monitor.system_activity import ActivityCancelled


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationObservation:
    availability: str
    running: frozenset[str] = frozenset()
    executable: str = ""
    detail: str = ""
    process: ProcessIdentity | None = None

    @property
    def ready(self) -> bool:
        return self.availability == "integration_ready" and not self.detail


@dataclass(frozen=True)
class TransitionResult:
    success: bool
    completed: int = 0
    phase: str = ""
    step: int = 0
    error: str = ""
    exit_code: int | None = None
    winerror: int | None = None
    process: ProcessIdentity | None = None


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    name: str
    executable: str
    created: float | None

    @property
    def identity(self) -> ProcessIdentity | None:
        if self.created is None:
            return None
        identity = ProcessIdentity(self.executable, self.pid, self.created)
        return identity if identity.valid() else None


@dataclass(frozen=True)
class ProcessSnapshot:
    running: frozenset[str] = frozenset()
    inaccessible_names: frozenset[str] = frozenset()
    identities: tuple[ProcessIdentity, ...] = ()
    uncertain_paths: frozenset[str] = frozenset()
    entries: tuple[ProcessEntry, ...] = ()

    @classmethod
    def from_entries(cls, entries):
        entries = tuple(entries)
        return cls(
            frozenset(item.executable for item in entries if item.executable),
            frozenset(item.name for item in entries if item.name and not item.executable),
            tuple(identity for item in entries if (identity := item.identity) is not None),
            frozenset(item.executable for item in entries if item.executable and item.identity is None),
            entries,
        )


def _process_entry(process, info) -> ProcessEntry:
    name = info.get("name") or ""
    path = normalized_executable(info.get("exe") or "")
    created = None
    if path:
        try:
            created = process.create_time()
        except (psutil.Error, OSError, AttributeError):
            pass
    return ProcessEntry(getattr(process, "pid", 0), name.casefold() or Path(path).name.casefold(), path, created)


def running_processes() -> ProcessSnapshot:
    iterator = psutil.process_iter
    if hasattr(iterator, "cache_clear"):
        iterator.cache_clear()
    return ProcessSnapshot.from_entries(_process_entry(process, process.info) for process in iterator(["name", "exe"]))


def process_entry(pid: int) -> ProcessEntry | None:
    try:
        process = psutil.Process(pid)
        return _process_entry(process, process.as_dict(attrs=["name", "exe"]))
    except (psutil.Error, OSError, ValueError):
        return None


def integration_process_names(modules) -> frozenset[str]:
    names = {"msiafterburner.exe"} if any(module.driver == "afterburner" and not module.executable for module in modules) else set()
    targets = {module.executable for module in modules if module.executable}
    names.update(Path(rule.executable).name.casefold() for module in modules for rule in module.application_rules)
    for path in targets:
        names.add(Path(path).name.casefold())
        try:
            names.add(Path(local_executable(path)).name.casefold())
        except (ValueError, OSError):
            pass
    names.discard("")
    return frozenset(names)


class IntegrationAdapter:
    def __init__(self):
        self.sessions = {}

    def start_elevated(self, module, current=None):
        from ecoflow_monitor.adapters.elevation import ElevatedSession
        if current is not None and not current():
            raise ActivityCancelled()
        observation = self.inspect(module)
        if current is not None and not current():
            raise ActivityCancelled()
        if not observation.ready:
            raise ValueError(observation.availability)
        session = ElevatedSession(module, observation.executable)
        if current is None:
            session.start()
        else:
            session.start(current)
        previous = self.sessions.pop(module.id, None)
        if previous:
            previous.close()
        self.sessions[module.id] = session

    def helper_ready(self, module: IntegrationDefinition) -> bool:
        session = self.sessions.get(module.id)
        return bool(module.enabled and session is not None and session.matches(module) and session.ready)

    def helper_handle(self, module: IntegrationDefinition):
        session = self.sessions.get(module.id)
        return session.handle if session is not None and session.matches(module) and session.ready else None

    def retain_sessions(self, modules):
        definitions = {module.id: module for module in modules if module.enabled}
        for identity, session in tuple(self.sessions.items()):
            if identity not in definitions or not session.matches(definitions[identity]):
                self.sessions.pop(identity, None)
                session.close()

    def close(self):
        self.retain_sessions(())

    def inspect(self, module: IntegrationDefinition, snapshot: ProcessSnapshot | None = None) -> IntegrationObservation:
        if not module.enabled:
            return IntegrationObservation("integration_disabled")
        if snapshot is None:
            snapshot = running_processes()
        running = snapshot.running
        selected = module.executable
        if module.driver == "afterburner" and not selected:
            candidates = tuple(path for path in running if Path(path).name.casefold() == "msiafterburner.exe")
            if len(candidates) > 1:
                return IntegrationObservation("integration_select_executable", running)
            if not candidates:
                state = "integration_access_denied" if "msiafterburner.exe" in snapshot.inaccessible_names else "integration_not_running"
                return IntegrationObservation(state, running)
            selected = candidates[0]
        try:
            path = local_executable(selected)
        except PermissionError:
            return IntegrationObservation("integration_access_denied", running)
        except (OSError, ValueError):
            return IntegrationObservation("integration_invalid_path", running)
        if normalized_executable(path) in running or (module.driver != "afterburner" and module.allow_launch):
            matches = tuple(item for item in snapshot.identities if item.executable == normalized_executable(path))
            process = matches[0] if len(matches) == 1 and normalized_executable(path) not in snapshot.uncertain_paths else None
            return IntegrationObservation("integration_ready", running, path, process=process)
        state = "integration_access_denied" if Path(path).name.casefold() in snapshot.inaccessible_names else "integration_not_running"
        return IntegrationObservation(state, running, path)

    def transition(self, module: IntegrationDefinition, previous: IntegrationProfile | None,
                   target: IntegrationProfile, current: Callable[[], bool]) -> TransitionResult:
        completed = 0
        process = None
        last_process = None
        observed_target = False
        for phase, reaction in (("deactivation", previous.deactivation if previous else None), ("activation", target.activation)):
            if reaction is None or not reaction.enabled:
                continue
            for index, template in enumerate(reaction.commands, 1):
                if not current():
                    return TransitionResult(False, completed, phase, index, "integration_cancelled")
                try:
                    observation = self.inspect(module)
                    if not observation.ready:
                        return TransitionResult(False, completed, phase, index, observation.detail or observation.availability)
                    if observed_target and last_process is not None and last_process != observation.process:
                        return TransitionResult(False, completed, phase, index, "integration_cancelled")
                    if not observed_target:
                        process = observation.process
                    elif process != observation.process:
                        process = None
                    last_process = observation.process
                    observed_target = True
                    args = module.expand(template, observation.executable)
                    if not current():
                        return TransitionResult(False, completed, phase, index, "integration_cancelled")
                    session = self.sessions.get(module.id)
                    if session is not None:
                        if not session.matches(module):
                            return TransitionResult(False, completed, phase, index, "integration_helper_failed")
                        response = session.run(args)
                        if response["error"]:
                            return TransitionResult(False, completed, phase, index, response["error"],
                                                    exit_code=response["exit_code"], winerror=response["winerror"])
                        completed += 1
                        continue
                    result = subprocess.run(args, shell=False, timeout=5,
                                            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS,
                                            cwd=str(Path(observation.executable).parent), check=False,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if result.returncode != 0:
                        return TransitionResult(False, completed, phase, index, "integration_command_failed", exit_code=result.returncode)
                except subprocess.TimeoutExpired:
                    return TransitionResult(False, completed, phase, index, "integration_timeout")
                except OSError as error:
                    reason = {5: "integration_permission_denied", 740: "integration_elevation_required"}.get(getattr(error, "winerror", None), "integration_command_failed")
                    return TransitionResult(False, completed, phase, index, reason, winerror=getattr(error, "winerror", None))
                except ValueError:
                    return TransitionResult(False, completed, phase, index, "integration_command_failed")
                except Exception as error:
                    logger.error("Integration %s step %s failed (%s)", phase, index, type(error).__name__)
                    return TransitionResult(False, completed, phase, index, "integration_command_failed")
                completed += 1
        return TransitionResult(True, completed, exit_code=0 if completed else None, process=process)
