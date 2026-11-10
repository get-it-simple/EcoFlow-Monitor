import argparse
import json
from pathlib import Path
import subprocess
import struct
import tempfile

import pefile

from ecoflow_monitor import __version__


def verify_version(path: Path) -> None:
    with pefile.PE(str(path)) as executable:
        resources = [entry for group in executable.FileInfo for entry in group if hasattr(entry, "StringTable")]
        tables = [table.entries for resource in resources for table in resource.StringTable]
        if not any(table.get(b"ProductVersion") == __version__.encode() and table.get(b"FileVersion") == (__version__ + ".0").encode() for table in tables):
            raise ValueError("Executable version resources do not match the application")


def verify_icon(path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "ecoflow_monitor/ui/assets/app.ico"
    data = source.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if (reserved, kind, count) != (0, 1, 7):
        raise ValueError("Application icon must contain seven resolutions")
    images = []
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        images.append(data[entry[7]:entry[7] + entry[6]])
    with pefile.PE(str(path)) as executable:
        resources = {entry.id: entry for entry in executable.DIRECTORY_ENTRY_RESOURCE.entries}
        if 3 not in resources or 14 not in resources:
            raise ValueError("Executable is missing Windows icon resources")
        payloads = {}
        for entry in resources[3].directory.entries:
            resource = entry.directory.entries[0].data.struct
            payloads[entry.id] = executable.get_data(resource.OffsetToData, resource.Size)
        group = resources[14].directory.entries[0].directory.entries[0].data.struct
        directory = executable.get_data(group.OffsetToData, group.Size)
        if struct.unpack_from("<HHH", directory) != (0, 1, count):
            raise ValueError("Executable icon group differs from the application icon")
        for index, expected in enumerate(images):
            entry = struct.unpack_from("<BBBBHHIH", directory, 6 + index * 14)
            if payloads.get(entry[7]) != expected:
                raise ValueError("Executable icon image differs from the application icon")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke the real portable executable with isolated synthetic settings")
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    verify_version(executable)
    verify_icon(executable)
    helper = subprocess.run([str(executable), "--elevated-helper"], timeout=90, check=False)
    if helper.returncode != 2:
        raise RuntimeError("Executable helper entry point did not reject invalid arguments")
    with tempfile.TemporaryDirectory(prefix="ecoflow-exe-check-") as directory:
        report = Path(directory) / "smoke.json"
        result = subprocess.run([str(executable), "--smoke-test", "--smoke-output", str(report)], cwd=directory, timeout=90, check=False)
        if result.returncode != 0 or not report.is_file():
            raise RuntimeError("Executable smoke did not finish successfully")
        data = json.loads(report.read_text(encoding="utf-8"))
        expected = {"version": __version__, "state": "not_configured", "main_visible": True, "msi_hidden": True, "ukrainian": True, "mini_visible": True, "integration_example": True, "custom_integration": True, "schema_roundtrip": True, "dashboard_visible": True, "design_tokens": True}
        if any(data.get(key) != value for key, value in expected.items()) or not {"en", "uk"} <= set(data.get("languages", [])):
            raise RuntimeError("Executable smoke report failed acceptance checks")
    print(f"Executable smoke passed: {__version__}, native windows, both catalogs, mini widget, MSI example, custom integration, schema migration, PE metadata and application icon")


if __name__ == "__main__":
    main()
