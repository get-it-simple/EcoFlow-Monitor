from collections.abc import Mapping
from datetime import datetime, timezone
import json
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import re
import subprocess
import threading
from typing import Any


REDACTED = "[REDACTED]"
SENSITIVE_FIELDS = frozenset({
    "accesskey", "secretkey", "apikey", "secret", "password", "passwd", "token",
    "accesstoken", "refreshtoken", "authorization", "cookie", "setcookie", "sign",
    "signature", "nonce", "sn", "serial", "serialnumber", "devicesn", "devicename", "deviceid",
    "deviceidentifier", "id", "username", "account", "accountid", "userid", "uid", "email",
    "emailaddress", "phone", "phonenumber", "address", "firstname", "lastname", "fullname",
    "ip", "ipaddress", "ipaddr", "mac", "macaddress", "ssid", "bssid", "wifi", "wifiname", "latitude",
    "longitude", "location", "credentialref", "accessref", "secretref", "vault",
    "vaultid", "itemid", "path", "executable", "command", "arguments", "name",
})
TEXT_PATTERNS = (
    re.compile(r"op://[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"(?<![\w.+-])[\w.+-]{1,254}@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.IGNORECASE),
    re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"<>]*"),
    re.compile(r"Bearer\s+[^\s\"']+", re.IGNORECASE),
)
ASSIGNMENT = re.compile(
    r"\b(access[_-]?key|secret[_-]?key|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|sign|signature|sn|serial)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)", re.IGNORECASE,
)
IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}(?:%[\w.-]+)?(?![\w:])", re.IGNORECASE)
MAX_EVENT_BYTES = 65536
MAX_NODES = 4096
MAX_DEPTH = 16
LOGGER_NAME = "ecoflow_monitor"


def sensitive_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in SENSITIVE_FIELDS or any(
        normalized.endswith(suffix) for suffix in ("accesskey", "secretkey", "password", "token", "serialnumber", "credentialref", "email", "ipaddress", "macaddress")
    )


def scrub_text(value: str, sensitive: tuple[str, ...] = ()) -> str:
    for secret in sorted(set(sensitive), key=len, reverse=True):
        if secret and (len(secret) >= 4 or value == secret):
            value = value.replace(secret, REDACTED)
    for pattern in TEXT_PATTERNS:
        value = pattern.sub(lambda match: REDACTED, value)
    def hide_ipv6(match):
        try:
            ipaddress.IPv6Address(match.group())
            return REDACTED
        except ValueError:
            return match.group()
    value = IPV6.sub(hide_ipv6, value)
    return ASSIGNMENT.sub(lambda match: match.group(1) + "=" + REDACTED, value)


def sanitize(value: Any, sensitive: tuple[str, ...] = ()) -> Any:
    secrets = list(sensitive)
    budget = MAX_NODES

    def collect(item, depth=0, protected=False) -> bool:
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > MAX_DEPTH:
            return False
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not collect(child, depth + 1, protected or (isinstance(key, str) and sensitive_field(key))):
                    return False
        elif isinstance(item, (list, tuple)):
            for child in item:
                if not collect(child, depth + 1, protected):
                    return False
        elif protected and isinstance(item, (str, int, float)) and not isinstance(item, bool):
            secrets.append(str(item))
        return True

    if not collect(value):
        return {"_truncated": True, "reason": "sanitization_size_limit"}
    hidden = tuple(secrets)
    budget = MAX_NODES

    def clean(item, depth=0):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > MAX_DEPTH:
            return "[TRUNCATED]"
        if isinstance(item, Mapping):
            result = {}
            for key, child in item.items():
                name = scrub_text(str(key)[:2048], hidden)
                result[name] = REDACTED if sensitive_field(str(key)) else clean(child, depth + 1)
                if budget < 0:
                    result["_truncated"] = True
                    break
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for child in item:
                result.append(clean(child, depth + 1))
                if budget < 0:
                    break
            return result
        if isinstance(item, str):
            text = scrub_text(item, hidden)
            return text[:8192] + ("[TRUNCATED]" if len(text) > 8192 else "")
        if item is None or isinstance(item, (int, float, bool)):
            return item
        return "[UNSUPPORTED TYPE]"

    return clean(value)


def event(name: str, *, level: int = logging.INFO, sensitive: tuple[str, ...] = (), **fields) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.isEnabledFor(level):
        logger.log(level, name, extra={"diagnostic_fields": sanitize(fields, sensitive)})


def reveal_log(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        subprocess.Popen(["explorer.exe", "/select," + str(path.resolve())], shell=False)
        return True
    except OSError:
        return False


class DiagnosticLog(logging.Handler):
    def __init__(self, path: Path, *, disabled: bool = False, max_bytes: int = 5 * 1024 * 1024,
                 backup_count: int = 4, queue_size: int = 1024) -> None:
        super().__init__()
        self.path = path
        self.disabled = disabled
        self.mode = "off"
        self.failed = False
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._queue = queue.Queue(queue_size)
        self._state_lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._dropped = 0
        self._sink = None
        self._thread = None
        self._logger = logging.getLogger(LOGGER_NAME)
        self._previous = (self._logger.handlers[:], self._logger.level, self._logger.propagate)
        self._logger.handlers = [self]
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def configure(self, mode: str) -> None:
        if not isinstance(mode, str) or mode not in {"off", "normal", "detailed"}:
            raise ValueError("Invalid logging mode")
        with self._state_lock:
            if self._closed or mode == self.mode:
                return
            self.mode = mode
            self.failed = False
            self._generation += 1
            self._logger.setLevel(logging.DEBUG if mode == "detailed" else logging.INFO)
            if mode != "off" and not self.disabled and self._thread is None:
                self._thread = threading.Thread(target=self._write, name="ecoflow-diagnostics", daemon=True)
                self._thread.start()
        event("logging_mode_changed", mode=mode)

    def emit(self, record: logging.LogRecord) -> None:
        with self._state_lock:
            if self._closed or self.disabled or self.failed or self.mode == "off":
                return
            if record.levelno < (logging.DEBUG if self.mode == "detailed" else logging.INFO):
                return
            data: dict[str, Any] = {
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "event": scrub_text(record.getMessage()),
            }
            fields = getattr(record, "diagnostic_fields", None)
            if fields:
                data["data"] = sanitize(fields)
            exception_type = record.exc_info[0] if isinstance(record.exc_info, tuple) else None
            if isinstance(exception_type, type):
                data["exception_type"] = exception_type.__name__
            line = json.dumps(data)
            if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                data["data"] = {"truncated": True, "reason": "event_size_limit"}
                data["event"] = data["event"][:1024]
                line = json.dumps(data)
            try:
                self._queue.put_nowait((self._generation, line))
            except queue.Full:
                self._dropped += 1

    def _write(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                generation, line = item
                with self._state_lock:
                    if generation != self._generation or self.mode == "off" or self.failed:
                        continue
                    try:
                        if self._sink is None:
                            self.path.parent.mkdir(parents=True, exist_ok=True)
                            self._sink = RotatingFileHandler(self.path, maxBytes=self._max_bytes,
                                                             backupCount=self._backup_count, encoding="utf-8")
                            self._sink.setFormatter(logging.Formatter("%(message)s"))
                        if self._dropped:
                            self._write_line(json.dumps({"event": "log_events_dropped", "count": self._dropped}))
                            self._dropped = 0
                        self._write_line(line)
                    except (OSError, ValueError):
                        self.failed = True
            finally:
                self._queue.task_done()

    def _write_line(self, line: str) -> None:
        record = logging.LogRecord(LOGGER_NAME, logging.INFO, "", 0, line, (), None)
        if self._sink.shouldRollover(record):
            self._sink.doRollover()
        self._sink.stream.write(line + "\n")
        self._sink.flush()

    def flush(self) -> None:
        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._queue.join()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()
        if self._sink is not None:
            try:
                self._sink.close()
            except OSError:
                self.failed = True
        self._logger.handlers, level, self._logger.propagate = self._previous
        self._logger.setLevel(level)
        super().close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
