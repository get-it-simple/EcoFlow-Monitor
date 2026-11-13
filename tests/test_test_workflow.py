import json
import subprocess

import pytest

from scripts import ci_changes, run_tests


@pytest.mark.parametrize("paths,event,ref,expected", [
    (["README.md"], "push", "refs/heads/main", (False, False, False)),
    (["docs/architecture.md"], "pull_request", "refs/pull/1/merge", (False, False, False)),
    (["ecoflow_monitor/controller.py", "README.md"], "push", "refs/heads/main", (True, False, False)),
    (["requirements.txt"], "pull_request", "refs/pull/1/merge", (True, True, False)),
    (["pyproject.toml"], "push", "refs/heads/main", (True, True, False)),
    (["ecoflow_monitor/ui/assets/app.ico"], "push", "refs/heads/main", (True, False, False)),
    ([".github/workflows/checks.yml"], "push", "refs/heads/main", (True, False, False)),
    (["README.md"], "workflow_dispatch", "refs/heads/main", (True, False, True)),
    (["README.md"], "push", "refs/tags/0.16.0", (True, False, True)),
])
def test_ci_runs_code_checks_and_packages_only_for_requested_events(paths, event, ref, expected):
    assert tuple(ci_changes.classify(paths, event, ref).values()) == expected


def test_unknown_diff_requires_tests_and_audit_without_enabling_branch_packaging():
    assert ci_changes.classify([], unknown=True) == {"tests": True, "dependencies": True, "packages": False}


def test_ci_publishes_decisions_for_documentation_only_diff(tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("PR_BASE", "synthetic-base")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1/merge")
    monkeypatch.setattr(ci_changes.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=b"README.md\0"))
    ci_changes.main()
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {"tests": "false", "dependencies": "false", "packages": "false"}


def test_combined_areas_run_shared_files_and_nodes_once():
    targets = run_tests.select_targets(["logging", "credentials"], ["tests/test_ui.py", "tests/test_controller.py::test_disabled_integration_has_no_process_watcher"])
    assert targets.count("tests/test_controller.py") == 1
    assert targets.count("tests/test_settings.py") == 1
    assert "tests/test_ui.py" in targets
    assert not any(target.startswith("tests/test_ui.py::") or target.startswith("tests/test_controller.py::") for target in targets)
    assert "tests/test_onepassword.py" in targets and "tests/test_diagnostics.py" in targets


def test_sensor_design_selection_does_not_expand_to_unrelated_design_matrices():
    assert run_tests.select_targets(["sensors"], design=True) == ["tests/test_polling.py"]


def test_full_scope_is_explicit_and_conflicting_arguments_fail():
    with pytest.raises(SystemExit) as missing:
        run_tests.main([])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as conflict:
        run_tests.main(["--full", "--area", "logging"])
    assert conflict.value.code == 2


def test_dry_run_does_not_launch_tests_or_create_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_tests.subprocess, "run", lambda *args, **kwargs: pytest.fail("Dry run launched a process"))
    report = tmp_path / "report.json"
    assert run_tests.main(["--area", "logging", "--parallel", "--report", str(report), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "and not native" in output and "and native" in output
    assert not report.exists()


def test_native_phase_failure_is_reported_and_stops_repetitions(tmp_path, monkeypatch):
    results = iter([0, 1])
    monkeypatch.setattr(run_tests.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, next(results)))
    report = tmp_path / "report.json"
    assert run_tests.main(["--full", "--parallel", "--repeat", "3", "--report", str(report)]) == 1
    summary = json.loads(report.read_text())
    assert [(run["phase"], run["exit_code"]) for run in summary["runs"]] == [("parallel", 0), ("native", 1)]


@pytest.mark.parametrize("codes,expected", [([0, 5], 0), ([5, 0], 0), ([5, 5], 5), ([2], 2)])
def test_empty_phase_is_allowed_but_empty_suite_or_collection_failure_is_not(monkeypatch, codes, expected):
    results = iter(codes)
    monkeypatch.setattr(run_tests.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, next(results)))
    assert run_tests.main(["--full", "--parallel"]) == expected
