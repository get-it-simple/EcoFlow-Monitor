import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class MemorySessionStorage:
    def __init__(self):
        self.value = None

    def read(self):
        return self.value

    def write(self, value):
        self.value = value

    def delete(self):
        self.value = None


class SyntheticProtection:
    def protect(self, value, entropy):
        return bytes(entropy) + value[::-1]

    def unprotect(self, value, entropy):
        if not value.startswith(entropy):
            raise ValueError("Different synthetic session")
        return value[len(entropy):][::-1]


@pytest.fixture
def session_cache():
    from ecoflow_monitor.credential_cache import SessionCredentialCache
    cache = SessionCredentialCache(MemorySessionStorage(), SyntheticProtection())
    yield cache
    cache.close()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.processEvents()
