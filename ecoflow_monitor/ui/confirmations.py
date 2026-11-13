from collections import deque
from dataclasses import dataclass
import threading
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from ecoflow_monitor.adapters.onepassword import RequestCancelled
from ecoflow_monitor.adapters.windows import activate_window


@dataclass
class ConfirmationRequest:
    key: object
    prompt: Callable | None
    accepted: Callable
    valid: Callable = lambda: True
    rejected: Callable = lambda: None


class ConfirmationCoordinator(QObject):
    external_requested = Signal(object)
    released = Signal(object)

    def __init__(self, owner, parent=None):
        super().__init__(parent or owner)
        self.owner = owner
        self.pending = deque()
        self.active = None
        self.dialog = None
        self._closed = False
        self._watching = False
        self._waiting_modal = None
        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)
        self._raise_timer = QTimer(self)
        self._raise_timer.setSingleShot(True)
        self._raise_timer.timeout.connect(self.raise_active)
        self.external_requested.connect(self.enqueue, Qt.ConnectionType.QueuedConnection)
        self.released.connect(self.finish, Qt.ConnectionType.QueuedConnection)

    def enqueue(self, request):
        if self._closed:
            request.rejected()
            return
        retained = deque()
        for item in self.pending:
            if item.valid():
                retained.append(item)
            else:
                item.rejected()
        self.pending = retained
        if (self.active is not None and self.active.key == request.key and self.active.valid()
                or any(item.key == request.key for item in self.pending)):
            return
        self.pending.append(request)
        self._watch(True)
        self._advance_timer.start(0)

    def _watch(self, enabled):
        if enabled == self._watching:
            return
        self._watching = enabled
        app = QApplication.instance()
        if enabled:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)

    def _advance(self):
        if self._closed or self.active is not None:
            return
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            if modal is not self._waiting_modal:
                self._waiting_modal = modal
                modal.finished.connect(self._resume)
            return
        self._waiting_modal = None
        while self.pending:
            request = self.pending.popleft()
            if not request.valid():
                request.rejected()
                continue
            self.active = request
            if request.prompt is None:
                request.accepted(lambda: self.finish(request.key))
                return
            parent = QApplication.activeModalWidget() or self.owner
            self.dialog = request.prompt(parent)
            self.dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            self.dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.dialog.finished.connect(lambda answer, item=request: self._answered(item, answer))
            self.dialog.show()
            self.raise_active()
            return
        self._watch(False)

    def _answered(self, request, answer):
        dialog, self.dialog = self.dialog, None
        if dialog is not None:
            dialog.deleteLater()
        if self.active is not request:
            return
        if not self._closed and request.valid() and answer == QMessageBox.StandardButton.Yes:
            request.accepted(lambda: self.finish(request.key))
        else:
            request.rejected()
            self.finish(request.key)

    def finish(self, key):
        if self.active is not None and self.active.key == key:
            self.active = None
            if not self._closed:
                self._advance_timer.start(0)

    def _resume(self, result):
        if not self._closed:
            self._advance_timer.start(0)

    def invalidate(self):
        if self.active is not None and self.dialog is not None and not self.active.valid():
            self.dialog.reject()

    def raise_active(self):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.setWindowState(self.dialog.windowState() & ~Qt.WindowState.WindowMinimized)
            activate_window(self.dialog)

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.ApplicationActivate, QEvent.Type.WindowActivate, QEvent.Type.Show}:
            if self.dialog is not None and watched is not self.dialog and watched is not self.dialog.windowHandle():
                self._raise_timer.start(0)
        return False

    def run_external(self, operation, cancelled):
        granted = threading.Event()
        refused = threading.Event()
        key = object()
        request = ConfirmationRequest(key, None, lambda done: granted.set(),
                                      lambda: not cancelled.is_set(), refused.set)
        self.external_requested.emit(request)
        try:
            while not granted.wait(0.05):
                if cancelled.is_set() or refused.is_set() or self._closed:
                    raise RequestCancelled()
            if cancelled.is_set() or self._closed:
                raise RequestCancelled()
            return operation()
        finally:
            self.released.emit(key)

    def close(self):
        self._closed = True
        for request in self.pending:
            request.rejected()
        self.pending.clear()
        if self.dialog is not None:
            self.dialog.reject()
        self.active = None
        self._advance_timer.stop()
        self._raise_timer.stop()
        self._watch(False)
