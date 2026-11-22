import argparse
import json
from pathlib import Path
import sys
import tempfile
from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from ecoflow_monitor import __version__, i18n
from ecoflow_monitor.adapters.power_efficiency import enable_efficiency_mode
from ecoflow_monitor.application import Application
from ecoflow_monitor.settings import SettingsError, default_settings_path
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile
from ecoflow_monitor.instance_control import InstanceController
from ecoflow_monitor.diagnostics import DiagnosticLog


def main() -> int:
    if "--elevated-helper" in sys.argv[1:]:
        from ecoflow_monitor.adapters.elevation import helper_main
        return helper_main(sys.argv[1:])
    parser = argparse.ArgumentParser(description="EcoFlow device monitoring and saved GPU profile automation")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--shutdown-for-update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("EcoFlow Monitor")
    app.setOrganizationName("EcoFlowMonitor")
    guard = None
    try:
        if args.shutdown_for_update:
            guard = InstanceController()
            return 0 if guard.shutdown_existing() else 2
        if not args.smoke_test:
            i18n.setup("system")
            try:
                guard = InstanceController()
                if not guard.start():
                    if args.autostart:
                        return 0
                    if guard.activate_existing():
                        return 0
                    QMessageBox.warning(None, i18n.t("error_title"), i18n.t("instance_activation_failed"))
                    return 1
            except OSError:
                QMessageBox.warning(None, i18n.t("error_title"), i18n.t("instance_start_failed"))
                return 1
        if args.smoke_test:
            with tempfile.TemporaryDirectory(prefix="ecoflow-smoke-") as directory:
                return run_application(app, args, Path(directory) / "settings.json", guard)
        try:
            settings_path = args.settings or default_settings_path()
        except SettingsError as error:
            QMessageBox.warning(None, i18n.t("error_title"), i18n.t(str(error)))
            return 1
        return run_application(app, args, settings_path, guard)
    finally:
        if guard is not None:
            guard.close()


def run_application(app, args, settings_path, guard) -> int:
    with DiagnosticLog(settings_path.parent / "logs" / "ecoflow-monitor.log", disabled=args.smoke_test) as diagnostics:
        return _run_application(app, args, settings_path, guard, diagnostics)


def _run_application(app, args, settings_path, guard, diagnostics) -> int:
    runtime = Application(settings_path, native=not args.smoke_test, diagnostics=diagnostics)
    if not args.smoke_test:
        enable_efficiency_mode()
    if guard is not None:
        guard.activation_requested.connect(runtime.activate_existing_window)
        guard.shutdown_requested.connect(app.quit)
    if args.smoke_test:
        def finish():
            result = {"version": __version__, "state": runtime.controller.connection.value, "languages": list(i18n.get_available_languages()), "main_visible": runtime.dashboard.isVisible(), "msi_hidden": runtime.dashboard.monitoring.msi_section.isHidden()}
            from ecoflow_monitor.ui.tokens import MOTION, TOKENS
            result["dashboard_visible"] = runtime.dashboard is not None and runtime.dashboard.isVisible()
            result["design_tokens"] = TOKENS["version"] == "1.2.0"
            result["motion_tokens"] = MOTION["version"] == "1.2.0" and MOTION["clock"] == "shared-monotonic"
            from PySide6.QtGui import QFontInfo
            result["inter_font"] = QFontInfo(runtime.dashboard.capacity.font()).family() == "Inter"
            result["single_main_window"] = runtime.window is runtime.dashboard
            example = runtime.controller.settings.integrations[0]
            result["integration_example"] = example.driver == "afterburner" and len(example.profiles) == 5
            sample = IntegrationDefinition("smoke", "Synthetic integration", enabled=True, profiles=(IntegrationProfile("sample", "Sample"),))
            runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(sample,)))
            result["custom_integration"] = runtime.dashboard.monitoring.integration_selector.currentData() == "smoke" and runtime.dashboard.monitoring.profile_choice.currentData() == "sample"
            result["schema_roundtrip"] = runtime.controller.store.load() == runtime.controller.settings
            i18n.setup("uk")
            runtime.window.refresh()
            result["ukrainian"] = i18n.t("input_power") != "Input"
            result["main_overlay_default_hidden"] = not runtime.widget.isVisible()
            runtime.controller.update_widget(enabled=True)
            result["mini_visible"] = runtime.widget.isVisible()
            result["main_overlay_visible"] = runtime.widget.isVisible() and runtime.widget.progress.motion is runtime.motion_driver
            result["adaptive_widget"] = runtime.main is runtime.mini is runtime.widget and runtime.controller.settings.to_dict()["version"] == 4
            runtime.dashboard.load_draft()
            runtime.dashboard.animations.setChecked(False)
            runtime.dashboard._draft_changed()
            runtime.dashboard.save()
            result["motion_disabled"] = not runtime.controller.settings.animations and not runtime.motion_driver.enabled and not runtime.motion_driver._timer.isActive()
            if args.smoke_output:
                args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
                args.smoke_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            runtime.stop()
            app.exit(0 if all(result[key] for key in ("main_visible", "mini_visible", "msi_hidden", "ukrainian", "integration_example", "custom_integration", "schema_roundtrip", "dashboard_visible", "design_tokens", "motion_tokens", "main_overlay_default_hidden", "main_overlay_visible", "adaptive_widget", "motion_disabled", "inter_font", "single_main_window")) else 1)
        QTimer.singleShot(750, finish)
    try:
        runtime.start(autostart=getattr(args, "autostart", False))
        return app.exec()
    finally:
        if guard is not None:
            guard.activation_requested.disconnect(runtime.activate_existing_window)
            guard.shutdown_requested.disconnect(app.quit)
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
