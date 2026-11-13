from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from shiboken6 import isValid


def flush_deletions(qapp):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def close_runtime(runtime, qapp):
    widgets = {id(widget): widget for widget in (runtime.dashboard, runtime.widget, runtime.menu, runtime._dialog) if widget is not None}
    if isValid(runtime):
        runtime.stop()
    for widget in widgets.values():
        if isValid(widget):
            widget.close()
            widget.deleteLater()
    if isValid(runtime):
        runtime.deleteLater()
    flush_deletions(qapp)


def wait_until(qapp, condition, timeout=5000):
    if condition():
        return
    loop = QEventLoop()
    poll = QTimer(loop)
    deadline = QTimer(loop)
    deadline.setSingleShot(True)
    errors = []

    def check():
        try:
            if condition():
                loop.quit()
        except BaseException as error:
            errors.append(error)
            loop.quit()

    poll.timeout.connect(check)
    deadline.timeout.connect(loop.quit)
    poll.start(1)
    deadline.start(timeout)
    try:
        loop.exec()
    finally:
        poll.stop()
        deadline.stop()
        loop.deleteLater()
    if errors:
        raise errors[0]
    assert condition(), f"Condition did not become true within {timeout}ms"
