from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading

from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationDefinition, identity


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessIdentity:
    executable: str
    pid: int
    created: float

    def valid(self) -> bool:
        return (isinstance(self.executable, str) and bool(self.executable)
                and type(self.pid) is int and self.pid > 0
                and type(self.created) in (int, float) and math.isfinite(self.created) and self.created > 0)


def command_fingerprint(module: IntegrationDefinition) -> str:
    data = {
        "driver": module.driver, "executable": normalized_executable(module.executable),
        "allow_launch": module.allow_launch,
        "mapping": sorted((item.id, item.arguments) for item in module.mapping),
        "profiles": sorted((item.id, item.slot, asdict(item.activation), asdict(item.deactivation)) for item in module.profiles),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DefinitionSnapshot:
    definitions: dict[str, str]
    revisions: dict[str, int]
    invalidated: frozenset[str] = frozenset()
    serial: int = 0


class IntegrationStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._policy = DefinitionSnapshot({}, {})
        self._modules = None
        self._records = {}
        self._record_revisions = {}
        self._loaded = False
        self._saved_serial = 0

    @property
    def dirty(self) -> bool:
        return self._policy.serial != self._saved_serial

    def configure(self, modules: tuple[IntegrationDefinition, ...]) -> None:
        if self._modules == modules:
            return
        self._modules = modules
        policy = self._policy
        definitions = {module.id: command_fingerprint(module) for module in modules if module.enabled}
        revisions = dict(policy.revisions)
        invalidated = set(policy.invalidated)
        for key in policy.definitions.keys() | definitions.keys():
            if policy.definitions.get(key) != definitions.get(key):
                revisions[key] = revisions.get(key, 0) + 1
                if key in policy.definitions:
                    invalidated.add(key)
        serial = policy.serial + (definitions != policy.definitions)
        self._policy = DefinitionSnapshot(definitions, revisions, frozenset(invalidated), serial)

    def _load(self) -> None:
        if self._loaded:
            return
        policy = self._policy
        try:
            if self.path.stat().st_size > 256 * 1024:
                raise ValueError("Integration state exceeds size limit")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("modules"), dict):
                raise ValueError("Invalid integration state")
            records = {}
            for key, value in data["modules"].items():
                identity(key)
                if value is None:
                    continue
                if not isinstance(value, dict) or set(value) != {"profile", "fingerprint", "process"}:
                    raise ValueError("Invalid integration record")
                identity(value["profile"])
                if not isinstance(value["fingerprint"], str) or len(value["fingerprint"]) != 64:
                    raise ValueError("Invalid integration fingerprint")
                process = ProcessIdentity(**value["process"])
                if not process.valid():
                    raise ValueError("Invalid integration process")
                if key not in policy.invalidated and policy.definitions.get(key) == value["fingerprint"]:
                    records[key] = value
            self._records.update(records)
            self._record_revisions.update({key: policy.revisions[key] for key in records})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            logger.warning("Integration state could not be loaded")
        self._loaded = True

    def _write(self) -> None:
        temporary = None
        policy = self._policy
        self._records = {key: value for key, value in self._records.items()
                         if policy.definitions.get(key) == value["fingerprint"]
                         and self._record_revisions.get(key) == policy.revisions.get(key)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".integration-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump({"version": 1, "modules": self._records}, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._saved_serial = policy.serial
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    logger.warning("Temporary integration state could not be removed")

    def flush(self) -> None:
        with self._lock:
            self._load()
            if self.dirty:
                try:
                    self._write()
                except OSError:
                    self._saved_serial = self._policy.serial
                    logger.warning("Integration state could not be saved")

    def restore(self, module: IntegrationDefinition, process: ProcessIdentity | None) -> str | None:
        with self._lock:
            self._load()
            policy = self._policy
            record = self._records.get(module.id)
            if (process is not None and process.valid() and record is not None
                    and policy.definitions.get(module.id) == record["fingerprint"]
                    and self._record_revisions.get(module.id) == policy.revisions.get(module.id)
                    and record["fingerprint"] == command_fingerprint(module)
                    and record["process"] == asdict(process)
                    and any(profile.id == record["profile"] for profile in module.profiles)):
                return record["profile"]
            return None

    def begin(self, module: IntegrationDefinition) -> int | None:
        with self._lock:
            policy = self._policy
            if policy.definitions.get(module.id) != command_fingerprint(module):
                return None
            self._load()
            revision = policy.revisions[module.id]
            self._records.pop(module.id, None)
            self._write()
            return revision if self._policy.revisions.get(module.id) == revision and self._policy.definitions.get(module.id) == command_fingerprint(module) else None

    def finish(self, module: IntegrationDefinition, revision: int, profile_id: str, process: ProcessIdentity | None) -> None:
        with self._lock:
            policy = self._policy
            if (policy.revisions.get(module.id) != revision or policy.definitions.get(module.id) != command_fingerprint(module)
                    or process is None or not process.valid()):
                return
            self._records[module.id] = {"profile": profile_id, "fingerprint": command_fingerprint(module), "process": asdict(process)}
            self._record_revisions[module.id] = revision
            try:
                self._write()
            except OSError:
                logger.warning("Successful integration state could not be saved")
