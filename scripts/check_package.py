import argparse
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import tomllib
from zipfile import ZipFile

from ecoflow_monitor import __version__


def check_wheel(wheel: Path, root: Path) -> int:
    expected = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for pattern in ("ecoflow_monitor/**/*.py", "ecoflow_monitor/i18n/*/strings.json", "ecoflow_monitor/ui/assets/*.svg", "ecoflow_monitor/ui/assets/*.ico", "ecoflow_monitor/integration_examples/*.json")
        for path in root.glob(pattern)
    }
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Wheel contains duplicate members")
        payload = {name for name in names if name.startswith("ecoflow_monitor/") and not name.endswith("/")}
        if payload != set(expected):
            raise ValueError(f"Wheel payload mismatch: {sorted(payload.symmetric_difference(expected))}")
        for name, content in expected.items():
            if archive.read(name) != content:
                raise ValueError(f"Wheel content differs from source: {name}")
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("Wheel must contain exactly one package metadata file")
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_paths[0]))
        if metadata["Name"] != project["name"] or metadata["Version"] != __version__:
            raise ValueError("Wheel name/version differs from project metadata")
        if metadata["Requires-Python"] != project["requires-python"]:
            raise ValueError("Wheel Python requirement differs from project metadata")
    return len([name for name in expected if name.endswith("/strings.json")])


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the built foundation wheel against its source payload")
    parser.add_argument("wheel_directory", type=Path)
    arguments = parser.parse_args()
    wheels = list(arguments.wheel_directory.glob(f"ecoflow_monitoring_win_support-{__version__}-*.whl"))
    if len(wheels) != 1:
        parser.error("Expected exactly one wheel for the current application version")
    count = check_wheel(wheels[0], Path(__file__).resolve().parents[1])
    print(f"Verified {wheels[0].name}: version {__version__}, source bytes, {count} language catalogs")


if __name__ == "__main__":
    main()
