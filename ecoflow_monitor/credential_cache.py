import json
import secrets
import threading
import time
from typing import Callable

from ecoflow_monitor.adapters.ecoflow import Credentials
from ecoflow_monitor.adapters.onepassword import OnePasswordError, RequestCancelled
from ecoflow_monitor.adapters.session_credentials import SessionCredentialStore, SessionStorageError, WindowsDataProtection


class SessionCredentialCache:
    def __init__(self, storage=None, protection=None, clock=time.monotonic, utc_clock=time.time):
        self._storage = storage if storage is not None else SessionCredentialStore()
        self._protection = protection if protection is not None else WindowsDataProtection()
        self._clock = clock
        self._utc_clock = utc_clock
        self._lock = threading.RLock()
        self._loading = threading.Lock()
        self._entropy = bytearray()
        self._initialized = False
        self._closed = False
        self._epoch = 0
        self._identity = None
        self._obtained = None
        self._expired = False

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._obtained is not None and not self._expired and not self._closed

    def initialize(self) -> None:
        with self._lock:
            if self._closed:
                raise RequestCancelled()
            if not self._initialized:
                self._storage.delete()
                self._entropy = bytearray(secrets.token_bytes(32))
                self._initialized = True

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1
            self._identity = None
            self._obtained = None
            self._expired = False
            if self._initialized:
                self._storage.delete()

    def expire(self, ttl_seconds: int) -> bool:
        with self._lock:
            if self._expired:
                return True
            if self._obtained is None:
                return False
            monotonic, utc = self._obtained
            if self._clock() - monotonic < ttl_seconds and self._utc_clock() - utc < ttl_seconds:
                return False
            self._epoch += 1
            self._identity = None
            self._obtained = None
            self._expired = True
            self._storage.delete()
            return True

    def get(self, identity: tuple[str, str], ttl_seconds: int, cancelled: threading.Event,
            loader: Callable[[], Credentials]) -> Credentials:
        while not self._loading.acquire(timeout=0.1):
            if cancelled.is_set():
                raise RequestCancelled()
        try:
            with self._lock:
                if cancelled.is_set() or self._closed:
                    raise RequestCancelled()
                self.initialize()
                if self.expire(ttl_seconds):
                    raise OnePasswordError("onepassword_cache_expired")
                if self._identity == identity:
                    value = self._storage.read()
                    if value is None:
                        raise SessionStorageError()
                    try:
                        pair = json.loads(self._protection.unprotect(value, self._entropy))
                        if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(key, str) or not key for key in pair):
                            raise ValueError()
                        return Credentials(*pair)
                    except (ValueError, TypeError, UnicodeError):
                        raise SessionStorageError() from None
                epoch = self._epoch
            credentials = loader()
            with self._lock:
                if self._closed or cancelled.is_set() or epoch != self._epoch:
                    raise RequestCancelled()
                value = json.dumps([credentials.access_key, credentials.secret_key]).encode("utf-8")
                self._storage.write(self._protection.protect(value, self._entropy))
                self._identity = identity
                self._obtained = (self._clock(), self._utc_clock())
                return credentials
        finally:
            self._loading.release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.clear()
            finally:
                self._entropy[:] = bytes(len(self._entropy))
