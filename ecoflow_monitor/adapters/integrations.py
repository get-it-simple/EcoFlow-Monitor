from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
from typing import Callable

import psutil

from ecoflow_monitor.adapters.executables import local_executable
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationObservation:
    availability: str
    running: frozenset[str] = frozenset()
    executable: str = ""
    detail: str = ""

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


@dataclass(frozen=True)
class ProcessSnapshot:
    running: frozenset[str] = frozenset()
    inaccessible_names: frozenset[str] = frozenset()


def running_processes() -> ProcessSnapshot:
    running = set()
    inaccessible = set()
    for process in psutil.process_iter(["name", "exe"]):
        try:
            path = process.info.get("exe")
            if isinstance(path, str) and path:
                running.add(normalized_executable(path))
            elif isinstance(process.info.get("name"), str):
                inaccessible.add(process.info["name"].casefold())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ProcessSnapshot(frozenset(running), frozenset(inaccessible))


class IntegrationAdapter:
    def inspect(self, module: IntegrationDefinition) -> IntegrationObservation:
        if not module.enabled:
            return IntegrationObservation("integration_disabled")
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
            return IntegrationObservation("integration_ready", running, path)
        state = "integration_access_denied" if Path(path).name.casefold() in snapshot.inaccessible_names else "integration_not_running"
        return IntegrationObservation(state, running, path)

    def transition(self, module: IntegrationDefinition, previous: IntegrationProfile | None,
                   target: IntegrationProfile, current: Callable[[], bool]) -> TransitionResult:
        completed = 0
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
                    args = module.expand(template, observation.executable)
                    if not current():
                        return TransitionResult(False, completed, phase, index, "integration_cancelled")
                    result = subprocess.run(args, shell=False, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
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
                    logger.error("Integration %s %s step %s failed (%s)", module.id, phase, index, type(error).__name__)
                    return TransitionResult(False, completed, phase, index, "integration_command_failed")
                completed += 1
        return TransitionResult(True, completed, exit_code=0 if completed else None)
