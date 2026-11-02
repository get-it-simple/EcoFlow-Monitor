import argparse
import sys

from PySide6.QtCore import QCoreApplication, QTimer

from ecoflow_monitor.instance_control import InstanceController


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("primary", "secondary", "race", "foreground"))
    parser.add_argument("name")
    parser.add_argument("--delay", type=int, default=0)
    args = parser.parse_args()
    if args.mode == "foreground":
        import ctypes
        from PySide6.QtWidgets import QApplication, QWidget
        from ecoflow_monitor.adapters.windows import activate_window
        app = QApplication([])
        window = QWidget()
        window.setWindowTitle("EcoFlow synthetic focus check")
        window.resize(240, 100)
        window.show()
        app.processEvents()
        activate_window(window)
        app.processEvents()
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        print("foreground" if ctypes.windll.user32.GetForegroundWindow() == int(window.winId()) else "focus-denied", flush=True)
    else:
        app = QCoreApplication([])
    guard = InstanceController(args.name)
    try:
        if not guard.start():
            result = guard.activate_existing()
            print("activated" if result else "failed", flush=True)
            return 0 if result else 1
        print("primary", flush=True)
        guard.activation_requested.connect(lambda: print("received", flush=True))
        if args.mode == "secondary":
            return 2
        QTimer.singleShot(10000, app.quit)
        if args.delay:
            import time
            time.sleep(args.delay / 1000)
        return app.exec()
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
