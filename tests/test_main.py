from importlib import import_module
import sys

from PySide6.QtWidgets import QMessageBox
import pytest


entry = import_module("ecoflow_monitor.__main__")


@pytest.mark.parametrize("activated,expected", [(True, 0), (False, 1)])
def test_duplicate_launch_never_initializes_runtime_or_settings(qapp, tmp_path, monkeypatch, activated, expected):
    class Duplicate:
        closed = False

        def start(self):
            return False

        def activate_existing(self):
            return activated

        def close(self):
            self.closed = True

    duplicate = Duplicate()
    warnings = []
    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate launch initialized runtime or accessed default settings")
    settings = tmp_path / "other-folder" / "settings.json"
    monkeypatch.setattr(sys, "argv", ["ecoflow-monitor", "--settings", str(settings)])
    monkeypatch.setitem(vars(entry), "InstanceController", lambda: duplicate)
    monkeypatch.setitem(vars(entry), "Application", forbidden)
    monkeypatch.setitem(vars(entry), "default_settings_path", forbidden)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    assert entry.main() == expected
    assert duplicate.closed
    assert bool(warnings) is not activated
    assert not settings.parent.exists()


def test_lock_error_prevents_runtime_start(qapp, monkeypatch):
    def fail():
        raise OSError("Synthetic mutex failure")
    monkeypatch.setattr(sys, "argv", ["ecoflow-monitor"])
    monkeypatch.setitem(vars(entry), "InstanceController", fail)
    monkeypatch.setitem(vars(entry), "Application", lambda *args, **kwargs: pytest.fail("Runtime started without a lock"))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    assert entry.main() == 1


def test_helper_entry_bypasses_main_instance_guard(monkeypatch):
    from ecoflow_monitor.adapters import elevation
    monkeypatch.setattr(sys, "argv", ["ecoflow-monitor", "--elevated-helper"])
    monkeypatch.setitem(vars(entry), "InstanceController", lambda: pytest.fail("Helper acquired main instance lock"))
    monkeypatch.setattr(elevation, "helper_main", lambda args: 23)
    assert entry.main() == 23


def test_smoke_entry_uses_temporary_settings_without_main_instance_guard(qapp, monkeypatch):
    paths = []
    def run(app, args, settings_path, guard):
        assert guard is None
        assert settings_path.parent.is_dir()
        paths.append(settings_path)
        return 0
    monkeypatch.setattr(sys, "argv", ["ecoflow-monitor", "--smoke-test"])
    monkeypatch.setitem(vars(entry), "InstanceController", lambda: pytest.fail("Smoke acquired main instance lock"))
    monkeypatch.setitem(vars(entry), "run_application", run)
    assert entry.main() == 0
    assert len(paths) == 1
    assert not paths[0].parent.exists()


@pytest.mark.parametrize("stopped,expected", [(True, 0), (False, 2)])
def test_shutdown_entry_never_starts_runtime_or_acquires_primary(qapp, monkeypatch, stopped, expected):
    class Shutdown:
        closed = False

        def shutdown_existing(self):
            return stopped

        def close(self):
            self.closed = True

    guard = Shutdown()
    monkeypatch.setattr(sys, "argv", ["ecoflow-monitor", "--shutdown-for-update"])
    monkeypatch.setitem(vars(entry), "InstanceController", lambda: guard)
    monkeypatch.setitem(vars(entry), "Application", lambda *args, **kwargs: pytest.fail("Shutdown started runtime"))
    monkeypatch.setitem(vars(entry), "default_settings_path", lambda: pytest.fail("Shutdown accessed settings"))
    assert entry.main() == expected
    assert guard.closed
