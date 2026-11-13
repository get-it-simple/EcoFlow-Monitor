from PySide6.QtCore import QTimer
import pytest
from shiboken6 import isValid

from ecoflow_monitor.application import Application
from qt_helpers import close_runtime, wait_until


def test_cleanup_deletes_aliased_windows_runtime_and_owned_callbacks(tmp_path, qapp):
    callbacks = []
    runtime = Application(tmp_path / "settings.json", native=False)
    dashboard, widget, menu = runtime.dashboard, runtime.widget, runtime.menu
    assert runtime.main is runtime.mini is widget
    timer = QTimer(runtime)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: callbacks.append(True))
    timer.start(0)
    close_runtime(runtime, qapp)
    assert all(not isValid(item) for item in (runtime, dashboard, widget, menu, timer))
    assert not callbacks
    close_runtime(runtime, qapp)


def test_wait_returns_after_queued_completion(qapp):
    completed = []
    QTimer.singleShot(0, lambda: completed.append(True))
    wait_until(qapp, lambda: bool(completed), timeout=1000)
    assert completed == [True]


def test_wait_timeout_does_not_hide_failure(qapp):
    with pytest.raises(AssertionError, match="within 10ms"):
        wait_until(qapp, lambda: False, timeout=10)


def test_wait_propagates_predicate_error_from_event_loop(qapp):
    calls = []
    def condition():
        calls.append(True)
        if len(calls) > 1:
            raise ValueError("Synthetic predicate failure")
        return False
    with pytest.raises(ValueError, match="Synthetic predicate failure"):
        wait_until(qapp, condition, timeout=1000)
