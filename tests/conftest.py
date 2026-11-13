import os
import sys

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


@pytest.fixture(autouse=True)
def qt_cleanup(request):
    if "qapp" not in request.fixturenames:
        yield
        return
    from PySide6.QtGui import QFont, QPalette
    from shiboken6 import isValid
    from ecoflow_monitor import i18n
    from qt_helpers import flush_deletions
    app = request.getfixturevalue("qapp")
    existing = {id(widget) for widget in app.topLevelWidgets()}
    language = i18n.get_current_language()
    font, palette = QFont(app.font()), QPalette(app.palette())
    exception_hook = sys.excepthook
    try:
        yield
    finally:
        for widget in app.topLevelWidgets():
            if id(widget) not in existing and isValid(widget):
                widget.close()
                widget.deleteLater()
        flush_deletions(app)
        i18n.setup(language)
        app.setFont(font)
        app.setPalette(palette)
        sys.excepthook = exception_hook
