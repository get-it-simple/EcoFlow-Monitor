import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import pefile

from ecoflow_monitor import __version__


def verify_version(path: Path) -> None:
    with pefile.PE(str(path)) as executable:
        resources = [entry for group in executable.FileInfo for entry in group if hasattr(entry, "StringTable")]
        tables = [table.entries for resource in resources for table in resource.StringTable]
        if not any(table.get(b"ProductVersion") == __version__.encode() and table.get(b"FileVersion") == (__version__ + ".0").encode() for table in tables):
            raise ValueError("Executable version resources do not match the application")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke the real portable executable with isolated synthetic settings")
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    verify_version(executable)
    with tempfile.TemporaryDirectory(prefix="ecoflow-exe-check-") as directory:
        report = Path(directory) / "smoke.json"
        result = subprocess.run([str(executable), "--smoke-test", "--smoke-output", str(report)], cwd=directory, timeout=90, check=False)
        if result.returncode != 0 or not report.is_file():
            raise RuntimeError("Executable smoke did not finish successfully")
        data = json.loads(report.read_text(encoding="utf-8"))
        expected = {"version": __version__, "state": "not_configured", "main_visible": True, "msi_hidden": True, "ukrainian": True, "mini_visible": True, "integration_example": True, "custom_integration": True, "schema_roundtrip": True}
        if any(data.get(key) != value for key, value in expected.items()) or not {"en", "uk"} <= set(data.get("languages", [])):
            raise RuntimeError("Executable smoke report failed acceptance checks")
    print(f"Executable smoke passed: {__version__}, native windows, both catalogs, mini widget, MSI example, custom integration, schema migration, PE metadata")


if __name__ == "__main__":
    main()
