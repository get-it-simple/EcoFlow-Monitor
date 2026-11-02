from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ecoflow_monitor.adapters.integrations import IntegrationAdapter, running_processes
from ecoflow_monitor.domain import normalized_executable
from ecoflow_monitor.integrations import IntegrationDefinition, afterburner_example
from ecoflow_monitor.ui.integrations import IntegrationsEditor


@pytest.fixture
def msi_module(tmp_path, monkeypatch):
    executable = tmp_path / "MSIAfterburner.exe"
    executable.write_bytes(b"synthetic")
    process = SimpleNamespace(info={"name": executable.name, "exe": str(executable)})
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", lambda *args: (process,))
    return replace(afterburner_example(), enabled=True, executable=str(executable), gpu_id="obsolete GPU identity")


def test_saved_slots_dispatch_without_gpu_files_or_shared_memory(msi_module, monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        pytest.fail("CLI integration must not inspect GPU files or native shared memory")
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr("ctypes.WinDLL", forbidden)
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    adapter = IntegrationAdapter()
    assert adapter.inspect(msi_module).ready
    for slot in (5, 1, 2):
        result = adapter.transition(msi_module, None, msi_module.profiles[slot - 1], lambda: True)
        assert result.success and result.completed == 1 and result.exit_code == 0
    assert [args for args, kwargs in calls] == [(msi_module.executable, "-profile5"), (msi_module.executable, "-profile1"), (msi_module.executable, "-profile2")]
    assert all(kwargs["shell"] is False and kwargs["timeout"] == 5 and kwargs["cwd"] == str(Path(msi_module.executable).parent) for args, kwargs in calls)


def test_stopped_msi_never_launches(msi_module, monkeypatch):
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", lambda *args: ())
    def unexpected(*args, **kwargs):
        pytest.fail("Stopped Afterburner must not be launched")
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", unexpected)
    result = IntegrationAdapter().transition(msi_module, None, msi_module.profiles[4], lambda: True)
    assert not result.success and result.error == "integration_not_running" and result.completed == 0


def test_msi_exit_between_observation_and_dispatch_is_rechecked(msi_module, monkeypatch):
    adapter = IntegrationAdapter()
    assert adapter.inspect(msi_module).ready
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", lambda *args: ())
    def unexpected(*args, **kwargs):
        pytest.fail("Old process observation must not permit launching a stopped instance")
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", unexpected)
    assert not adapter.transition(msi_module, None, msi_module.profiles[4], lambda: True).success


def test_msi_blank_path_discovers_running_executable(msi_module):
    observation = IntegrationAdapter().inspect(replace(msi_module, executable=""))
    assert observation.ready and normalized_executable(observation.executable) == normalized_executable(msi_module.executable)


def test_configured_path_does_not_target_another_same_named_process(msi_module, tmp_path):
    other = tmp_path / "other" / "MSIAfterburner.exe"
    other.parent.mkdir()
    other.write_bytes(b"synthetic")
    assert IntegrationAdapter().inspect(replace(msi_module, executable=str(other))).availability == "integration_not_running"


def test_ambiguous_discovery_requires_explicit_path(msi_module, tmp_path, monkeypatch):
    other = tmp_path / "other" / "MSIAfterburner.exe"
    processes = tuple(SimpleNamespace(info={"name": "MSIAfterburner.exe", "exe": path}) for path in (msi_module.executable, str(other)))
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", lambda *args: processes)
    adapter = IntegrationAdapter()
    assert adapter.inspect(replace(msi_module, executable="")).availability == "integration_select_executable"
    assert adapter.inspect(msi_module).ready


@pytest.mark.parametrize("blank_path", [True, False])
def test_inaccessible_process_path_is_distinct_from_stopped(msi_module, monkeypatch, blank_path):
    process = SimpleNamespace(info={"name": "MSIAfterburner.exe", "exe": None})
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", lambda *args: (process,))
    module = replace(msi_module, executable="") if blank_path else msi_module
    observation = IntegrationAdapter().inspect(module)
    assert not observation.ready and observation.availability == "integration_access_denied"
    assert "msiafterburner.exe" in running_processes().inaccessible_names


def test_disabled_msi_does_not_scan_processes(msi_module, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Disabled module must not scan processes")
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.psutil.process_iter", unexpected)
    assert IntegrationAdapter().inspect(replace(msi_module, enabled=False)).availability == "integration_disabled"


@pytest.mark.parametrize("failure,reason", [(5, "integration_permission_denied"), (740, "integration_elevation_required"),
                                          (2, "integration_command_failed"), ("timeout", "integration_timeout"),
                                          ("exit", "integration_command_failed")])
def test_cli_errors_preserve_windows_and_exit_codes(msi_module, monkeypatch, failure, reason):
    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        if failure == "exit":
            return subprocess.CompletedProcess(args, 7)
        error = OSError("synthetic Windows command failure")
        error.winerror = failure
        raise error
    monkeypatch.setattr("ecoflow_monitor.adapters.integrations.subprocess.run", run)
    result = IntegrationAdapter().transition(msi_module, None, msi_module.profiles[4], lambda: True)
    assert not result.success and result.error == reason
    assert (result.phase, result.step, result.completed) == ("activation", 1, 0)
    assert result.winerror == (failure if isinstance(failure, int) else None)
    assert result.exit_code == (7 if failure == "exit" else None)


def test_legacy_gpu_identity_survives_roundtrip_and_settings_editor(msi_module, qapp):
    restored = IntegrationDefinition.from_dict(msi_module.to_dict())
    assert restored == msi_module
    editor = IntegrationsEditor()
    try:
        editor.load((restored,))
        assert not hasattr(editor, "gpu")
        assert editor.modules()[0] == restored
    finally:
        editor.deleteLater()
