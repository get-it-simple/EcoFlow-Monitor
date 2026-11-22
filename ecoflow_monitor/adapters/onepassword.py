from dataclasses import dataclass, field
import json
import logging
import os
from queue import Empty, Queue
import re
import shutil
import subprocess
import threading
import time
from typing import Callable

from ecoflow_monitor.adapters.credentials import CredentialError
from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.credential_refs import normalize_secret_reference


logger = logging.getLogger(__name__)


class OnePasswordError(CredentialError):
    pass


class RequestCancelled(OnePasswordError):
    def __init__(self):
        super().__init__("onepassword_cancelled")


def desktop_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("OP_")}
    environment["OP_BIOMETRIC_UNLOCK_ENABLED"] = "true"
    environment["OP_CACHE"] = "false"
    return environment


def run_cli(arguments: list[str], cancelled: threading.Event, payload: bytes | None = None,
            timeout: float = 120) -> bytes:
    if cancelled.is_set():
        raise RequestCancelled()
    executable = shutil.which("op.exe" if os.name == "nt" else "op")
    if not executable:
        raise OnePasswordError("onepassword_missing")
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [executable, *arguments], stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=desktop_environment(), shell=False, text=False,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0,
        )
    except OSError:
        raise OnePasswordError("onepassword_start_error") from None
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled.is_set():
                raise RequestCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OnePasswordError("onepassword_timeout")
            try:
                output, _ = process.communicate(input=payload, timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        if cancelled.is_set():
            raise RequestCancelled()
        if process.returncode:
            raise OnePasswordError("onepassword_access_error")
        return output
    except OSError:
        raise OnePasswordError("onepassword_communication_error") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def read_secret(reference: str, cancelled: threading.Event) -> str:
    try:
        reference = normalize_secret_reference(reference)
    except ValueError:
        raise OnePasswordError("onepassword_reference_error") from None
    output = run_cli(["read", "--no-newline", reference], cancelled)
    try:
        value = output.decode("utf-8")
    except UnicodeDecodeError:
        raise OnePasswordError("onepassword_value_error") from None
    if not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise OnePasswordError("onepassword_value_error")
    return value


def read_credentials(access_ref: str, secret_ref: str, cancelled: threading.Event) -> Credentials:
    try:
        access_ref = normalize_secret_reference(access_ref)
        secret_ref = normalize_secret_reference(secret_ref)
    except ValueError:
        raise OnePasswordError("onepassword_reference_error") from None
    return Credentials(read_secret(access_ref, cancelled), read_secret(secret_ref, cancelled))


@dataclass(frozen=True)
class SavedItem:
    access_ref: str
    secret_ref: str


def create_item(vault: str, title: str, credentials: Credentials, cancelled: threading.Event) -> SavedItem:
    if not vault.strip() or not title.strip() or not credentials.access_key or not credentials.secret_key:
        raise OnePasswordError("onepassword_save_required")
    template = {
        "title": title.strip(), "category": "LOGIN",
        "fields": [
            {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": credentials.access_key},
            {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": credentials.secret_key},
        ],
    }
    output = run_cli(["item", "create", "-", "--vault", vault.strip(), "--format", "json"], cancelled,
                     json.dumps(template).encode("utf-8"))
    try:
        item = json.loads(output)
        vault_id, item_id = item["vault"]["id"], item["id"]
        if not all(isinstance(value, str) and re.fullmatch(r"[a-z0-9]{26}", value) for value in (vault_id, item_id)):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise OnePasswordError("onepassword_save_result_error") from None
    return SavedItem(f"op://{vault_id}/{item_id}/username", f"op://{vault_id}/{item_id}/password")


@dataclass(frozen=True)
class RequestResult:
    value: Credentials | SavedItem | None = field(default=None, repr=False)
    error: str = ""


class SecretRequest:
    def __init__(self, operation: Callable[[threading.Event], Credentials | SavedItem],
                 completed: Callable[["SecretRequest"], None] | None = None):
        self._operation = operation
        self._completed = completed
        self._cancelled = threading.Event()
        self._results: Queue[RequestResult] = Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        self.cancel()
        self._thread.join(timeout=2)

    def take_result(self) -> RequestResult | None:
        try:
            result = self._results.get_nowait()
        except Empty:
            return None
        return None if self._cancelled.is_set() else result

    def _run(self) -> None:
        try:
            result = RequestResult(value=self._operation(self._cancelled))
        except RequestCancelled:
            return
        except OnePasswordError as error:
            result = RequestResult(error=str(error))
        except Exception as error:
            logger.error("1Password background request failed (%s)", type(error).__name__)
            result = RequestResult(error="onepassword_communication_error")
        finally:
            self._operation = None
        if not self._cancelled.is_set():
            self._results.put_nowait(result)
            if self._completed is not None:
                self._completed(self)
