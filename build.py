import argparse
import importlib.metadata
from pathlib import Path
import os
import struct
import subprocess
import sys

from packaging.requirements import Requirement

from ecoflow_monitor import __version__


ROOT = Path(__file__).resolve().parent


def missing_requirements() -> list[str]:
    missing = []
    for filename in ("requirements.txt", "requirements-build.txt"):
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith(("#", "-r")):
                continue
            requirement = Requirement(line)
            try:
                installed = importlib.metadata.version(requirement.name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            if installed is None or installed not in requirement.specifier:
                missing.append(line)
    return missing


def version_resource() -> Path:
    from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo
    parts = tuple(int(part) for part in __version__.split(".")) + (0,)
    resource = VSVersionInfo(
        ffi=FixedFileInfo(filevers=parts, prodvers=parts, mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [StringStruct(key, value) for key, value in {
                "FileDescription": "EcoFlow Monitor", "FileVersion": __version__ + ".0",
                "InternalName": "EcoFlowMonitor", "OriginalFilename": "EcoFlowMonitor.exe",
                "ProductName": "EcoFlow Monitor", "ProductVersion": __version__,
            }.items()])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    target = ROOT / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(resource), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the portable Windows application")
    parser.add_argument("--install-missing", action="store_true")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist" / __version__)
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Use a 64-bit Windows Python interpreter")
    missing = missing_requirements()
    if missing:
        if not args.install_missing:
            print("Missing dependencies: " + ", ".join(missing))
            if not sys.stdin.isatty() or input("Install these dependencies? [y/N] ").strip().casefold() != "y":
                parser.error("Install requirements-build.txt before building")
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", *missing], check=True)
    environment = dict(os.environ)
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build" / "pyinstaller-cache")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--name", "EcoFlowMonitor", "--paths", str(ROOT),
        "--distpath", str(args.dist_dir.resolve()), "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"), "--version-file", str(version_resource()),
        "--icon", str(ROOT / "ecoflow_monitor" / "ui" / "assets" / "app.ico"),
        "--collect-data", "ecoflow_monitor.i18n", "--collect-data", "ecoflow_monitor.ui",
        "--collect-data", "ecoflow_monitor.integration_examples",
        str(ROOT / "ecoflow_monitor" / "__main__.py"),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    from scripts.release import package_portable
    executable = args.dist_dir.resolve() / "EcoFlowMonitor.exe"
    package_portable(executable, args.dist_dir.resolve() / f"EcoFlowMonitor-{__version__}-portable.zip")
    print(f"Built and verified {executable}")


if __name__ == "__main__":
    main()
