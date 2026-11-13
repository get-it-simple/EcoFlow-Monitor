import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
AREAS = {
    "core": (
        "tests/test_domain.py",
        "tests/test_settings.py",
        "tests/test_ecoflow.py",
        "tests/test_i18n.py",
    ),
    "connection": (
        "tests/test_connection_session.py",
        "tests/test_controller.py",
        "tests/test_polling.py",
        "tests/test_ecoflow.py",
    ),
    "credentials": (
        "tests/test_credential_cache.py",
        "tests/test_onepassword.py",
        "tests/test_controller.py",
        "tests/test_settings.py",
        "tests/test_i18n.py",
        "tests/test_ui.py::test_every_literal_translation_key_exists_in_english",
    ),
    "logging": (
        "tests/test_diagnostics.py",
        "tests/test_ecoflow.py",
        "tests/test_controller.py",
        "tests/test_settings.py",
        "tests/test_main.py",
        "tests/test_ui.py::test_every_literal_translation_key_exists_in_english",
    ),
    "motion": (
        "tests/test_motion.py",
        "tests/test_settings.py",
        "tests/test_integrations.py::test_module_hotkey_removal_is_saved",
    ),
    "sensors": (
        "tests/test_polling.py",
        "tests/test_ecoflow.py",
        "tests/test_controller.py",
        "tests/test_settings.py",
        "tests/test_domain.py",
        "tests/test_ui.py::test_metric_tooltips_follow_language_changes",
    ),
    "integrations": (
        "tests/test_integrations.py",
        "tests/test_ui_integrations.py",
        "tests/test_design_system.py::test_rule_dependencies_block_condition_disable_and_keep_rules",
        "tests/test_design_system.py::test_command_editor_preview_preserves_id_and_never_executes",
        "tests/test_design_system.py::test_integration_editor_accepts_command_and_profile_dialog_results",
        "tests/test_ui.py::test_friendly_rule_names_and_explanations",
        "tests/test_ui.py::test_profile_details_tooltips_update_on_selection_and_edit",
        "tests/test_appearance.py::test_percent_sliders_save_reset_cancel_and_rules",
        "tests/test_disclosure.py::test_settings_integrations_keep_draft_and_allow_empty_modules",
        "tests/test_ui_interactions.py::test_settings_rebuilds_never_show_temporary_top_level_widgets",
        "tests/test_i18n.py",
        "tests/test_ui.py::test_every_literal_translation_key_exists_in_english",
    ),
    "widgets": (
        "tests/test_adaptive_widget.py",
        "tests/test_settings.py",
        "tests/test_disclosure.py",
        "tests/test_motion.py",
        "tests/test_ui.py",
        "tests/test_ui_regressions.py",
        "tests/test_design_system.py",
        "tests/test_ui_interactions.py",
        "tests/test_appearance.py",
        "tests/test_ui_reference.py",
        "tests/test_installer_smoke_settings.py",
    ),
}
DESIGN_AREAS = {
    "sensors": (
        "tests/test_polling.py",
    ),
    "credentials": (
        "tests/test_credential_cache.py",
    ),
    "logging": (
        "tests/test_diagnostics.py",
    ),
    "widgets": (
        "tests/test_adaptive_widget.py",
        "tests/test_disclosure.py",
        "tests/test_motion.py",
        "tests/test_design_system.py",
        "tests/test_ui.py",
        "tests/test_ui_reference.py",
        "tests/test_ui_regressions.py",
    ),
}


def select_targets(areas=(), targets=(), full=False, design=False):
    if full:
        return ["tests"]
    mapping = {**AREAS, **DESIGN_AREAS} if design else AREAS
    selected = sorted({target for area in areas for target in mapping[area]} | set(targets))
    return [
        target for target in selected
        if not any(
            target.startswith(parent + "::") or target.startswith(parent.rstrip("/") + "/")
            for parent in selected if parent != target
        )
    ]


def build_commands(targets, parallel=False, workers="2", design=False, report=None):
    marker = "design" if design else "not design"
    phases = [("serial", "0", marker)]
    if parallel:
        phases = [("parallel", workers, f"({marker}) and not native"), ("native", "0", f"({marker}) and native")]
    commands = []
    for phase, count, expression in phases:
        command = [
            sys.executable, "-m", "pytest", "-n", count, "--dist", "worksteal",
            "--max-worker-restart=0", "-m", expression, *targets, "-q",
            "--durations=30", "--durations-min=0.01",
        ]
        if report is not None:
            command.append(f"--junitxml={report.with_name(report.stem + '-' + phase + '.xml')}")
        commands.append((phase, command))
    return commands


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a deduplicated union of affected test areas using the current interpreter")
    parser.add_argument("--area", action="append", choices=sorted(AREAS), default=[])
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--design", action="store_true")
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--workers", choices=("2", "auto"), default="2")
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.full and (args.area or args.target):
        parser.error("Choose --full or affected --area/--target selections")
    targets = select_targets(args.area, args.target, args.full, args.design)
    if not targets:
        parser.error("Choose at least one --area/--target, or request --full explicitly")
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_OPENGL="software")
    runs = []
    started = time.perf_counter()
    for repetition in range(1, args.repeat + 1):
        report = args.report
        if report is not None:
            report = (ROOT / report).resolve()
            if args.repeat > 1:
                report = report.with_name(f"{report.stem}-{repetition}{report.suffix}")
            if not args.dry_run:
                report.parent.mkdir(parents=True, exist_ok=True)
        for phase, command in build_commands(targets, args.parallel, args.workers, args.design, report):
            print(f"Run {repetition}, {phase}: {subprocess.list2cmdline(command)}", flush=True)
            if args.dry_run:
                continue
            phase_start = time.perf_counter()
            result = subprocess.run(command, cwd=ROOT, env=environment)
            elapsed = time.perf_counter() - phase_start
            runs.append({"run": repetition, "phase": phase, "seconds": elapsed, "exit_code": result.returncode})
            print(f"{phase}: exit {result.returncode}, {elapsed:.3f}s including process startup/collection", flush=True)
            if result.returncode not in (0, 5):
                break
        if runs and runs[-1]["exit_code"] not in (0, 5):
            break
    if args.dry_run:
        return 0
    summary = {"targets": targets, "design": args.design, "parallel": args.parallel, "runs": runs, "seconds": time.perf_counter() - started}
    if args.report is not None:
        destination = (ROOT / args.report).resolve()
        destination.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    failed = next((run["exit_code"] for run in runs if run["exit_code"] not in (0, 5)), None)
    if failed is not None:
        return failed
    return 0 if any(run["exit_code"] == 0 for run in runs) else 5


if __name__ == "__main__":
    raise SystemExit(main())
