import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile
from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from ecoflow_monitor import __version__, i18n
from ecoflow_monitor.application import Application
from ecoflow_monitor.settings import default_settings_path
from ecoflow_monitor.integrations import IntegrationDefinition, IntegrationProfile


def main() -> int:
    parser = argparse.ArgumentParser(description="DELTA 3 Max monitoring and saved GPU profile automation")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="ecoflow-smoke-") if args.smoke_test else None
    settings_path = Path(temporary.name) / "settings.json" if temporary else args.settings or default_settings_path()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("EcoFlow Monitor")
    app.setOrganizationName("EcoFlowMonitor")
    if not args.smoke_test:
        try:
            directory = settings_path.parent / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(directory / "ecoflow-monitor.log", maxBytes=1048576, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger("ecoflow_monitor").addHandler(handler)
        except OSError:
            pass
    runtime = Application(settings_path, native=not args.smoke_test)
    if not args.smoke_test and not runtime.ensure_single_instance(settings_path):
        runtime.stop()
        return 0
    runtime.start()
    if args.smoke_test:
        def finish():
            result = {"version": __version__, "state": runtime.controller.connection.value, "languages": list(i18n.get_available_languages()), "main_visible": runtime.window.isVisible(), "msi_hidden": runtime.window.msi_section.isHidden()}
            example = runtime.controller.settings.integrations[0]
            result["integration_example"] = example.driver == "afterburner" and len(example.profiles) == 5
            sample = IntegrationDefinition("smoke", "Synthetic integration", enabled=True, profiles=(IntegrationProfile("sample", "Sample"),))
            runtime.controller.apply_settings(replace(runtime.controller.settings, integrations=(sample,)))
            result["custom_integration"] = runtime.window.integration_selector.currentData() == "smoke" and runtime.window.profile_choice.currentData() == "sample"
            result["schema_roundtrip"] = runtime.controller.store.load() == runtime.controller.settings
            i18n.setup("uk")
            runtime.window.refresh()
            result["ukrainian"] = i18n.t("input_power") != "Input"
            runtime.controller.update_preferences(mini_enabled=True)
            runtime.mini.refresh()
            result["mini_visible"] = runtime.mini.isVisible()
            if args.smoke_output:
                args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
                args.smoke_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            runtime.stop()
            app.exit(0 if all(result[key] for key in ("main_visible", "mini_visible", "msi_hidden", "ukrainian", "integration_example", "custom_integration", "schema_roundtrip")) else 1)
        QTimer.singleShot(750, finish)
    exit_code = app.exec()
    runtime.stop()
    if temporary:
        temporary.cleanup()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
