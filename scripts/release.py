import argparse
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from ecoflow_monitor import __version__
from scripts.smoke_executable import verify_icon, verify_version


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "GetItSimple.EcoFlowMonitor"
REPOSITORY = "get-it-simple/EcoFlow-Monitor"
MANIFEST_VERSION = "1.10.0"
MANIFEST_TYPES = {
    f"{PACKAGE_ID}.yaml": "version",
    f"{PACKAGE_ID}.installer.yaml": "installer",
    f"{PACKAGE_ID}.locale.en-US.yaml": "defaultLocale",
    f"{PACKAGE_ID}.locale.uk-UA.yaml": "locale",
}


def validate_version(version: str = __version__) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Version must have three numeric components without leading zeroes")
    if any(int(part) > 65535 for part in version.split(".")):
        raise ValueError("Version exceeds Windows resource limits")
    if version != __version__:
        raise ValueError("Version must match ecoflow_monitor.__version__")
    return version


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_application(executable: Path) -> None:
    validate_version()
    verify_version(executable)
    verify_icon(executable)


def package_portable(executable: Path, target: Path) -> None:
    verify_application(executable)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        staged = Path(directory) / target.name
        with ZipFile(staged, "w", ZIP_DEFLATED) as archive:
            archive.write(executable, "EcoFlowMonitor.exe")
            archive.write(ROOT / "README.md", "README.md")
        verify_portable(staged, executable)
        staged.replace(target)


def verify_portable(archive_path: Path, executable: Path) -> None:
    with ZipFile(archive_path) as archive:
        if sorted(archive.namelist()) != ["EcoFlowMonitor.exe", "README.md"]:
            raise ValueError("Unexpected portable archive payload")
        if hashlib.sha256(archive.read("EcoFlowMonitor.exe")).hexdigest() != digest(executable):
            raise ValueError("Portable executable hash mismatch")
        if archive.read("README.md") != (ROOT / "README.md").read_bytes():
            raise ValueError("Portable README mismatch")


def sign(paths: list[Path]) -> bool:
    keys = ("WINDOWS_SIGNING_CERTIFICATE_BASE64", "WINDOWS_SIGNING_CERTIFICATE_PASSWORD", "WINDOWS_TIMESTAMP_URL")
    configured = [bool(os.environ.get(key, "").strip()) for key in keys]
    if any(configured) and not all(configured):
        raise ValueError("All three Windows signing variables must be configured together")
    if not any(configured):
        print("Signing is not configured; artifacts remain unsigned")
        return False
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(ROOT / "scripts/sign-artifacts.ps1"),
                    "-Paths", *map(str, paths)], cwd=ROOT, check=True)
    return True


def build_installer(executable: Path, output: Path, compiler: str | None = None) -> Path:
    verify_application(executable)
    if not (ROOT / "LICENSE").is_file():
        raise FileNotFoundError("Installer packaging requires an approved LICENSE at the repository root")
    signed = sign([executable])
    compiler = compiler or shutil.which("makensis")
    if not compiler:
        for key in ("ProgramFiles(x86)", "ProgramFiles"):
            candidate = Path(os.environ.get(key, "")) / "NSIS/makensis.exe"
            if candidate.is_file():
                compiler = str(candidate)
                break
    if not compiler:
        raise FileNotFoundError("Install NSIS or pass --makensis with its executable path")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe"
    with tempfile.TemporaryDirectory(dir=output) as directory:
        staged = Path(directory) / target.name
        command = [compiler, "/INPUTCHARSET", "UTF8", "/WX", f"/DAPP_VERSION={__version__}", f"/DAPP_EXE={executable.resolve()}",
                   f"/DOUTPUT_FILE={staged.resolve()}", f"/DSIGNED_BUILD={int(signed)}"]
        if signed:
            command.append('/DUNINSTALL_SIGN_COMMAND=powershell.exe -NoProfile -File "' +
                           str(ROOT / "scripts/sign-artifacts.ps1") + '" -Paths "%1"')
        subprocess.run([*command, str(ROOT / "installer/EcoFlowMonitor.nsi")], cwd=ROOT, check=True)
        verify_version(staged)
        sign([staged])
        staged.replace(target)
    return target


def manifest_schema_header(manifest_type: str) -> str:
    return f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.{manifest_type}.{MANIFEST_VERSION}.schema.json\n"


def write_manifests(directory: Path, installer: Path, repository: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be owner/name")
    directory.mkdir()
    common = f"PackageIdentifier: {PACKAGE_ID}\nPackageVersion: {__version__}\n"
    suffix = f"ManifestVersion: {MANIFEST_VERSION}\n"
    files = {
        f"{PACKAGE_ID}.yaml": common + "DefaultLocale: en-US\nManifestType: version\n" + suffix,
        f"{PACKAGE_ID}.installer.yaml": common + f"""Platform:
- Windows.Desktop
MinimumOSVersion: 10.0.17763.0
InstallerType: nullsoft
InstallerSwitches:
  Silent: /S
  SilentWithProgress: /SILENTWITHPROGRESS
InstallModes:
- interactive
- silent
- silentWithProgress
UpgradeBehavior: install
ReleaseDate: {date.today().isoformat()}
Installers:
- Architecture: x64
  Scope: machine
  InstallerUrl: https://github.com/{repository}/releases/download/{__version__}/{installer.name}
  InstallerSha256: {digest(installer).upper()}
  AppsAndFeaturesEntries:
  - DisplayName: EcoFlow Monitor
    Publisher: Get it Simple
    ProductCode: {PACKAGE_ID}
ManifestType: installer
""" + suffix,
        f"{PACKAGE_ID}.locale.en-US.yaml": common + f"""PackageLocale: en-US
Publisher: Get it Simple
PublisherUrl: https://github.com/get-it-simple
PublisherSupportUrl: https://github.com/{repository}/issues
PackageName: EcoFlow Monitor
PackageUrl: https://github.com/{repository}
License: MIT
LicenseUrl: https://github.com/{repository}/blob/main/LICENSE
ShortDescription: EcoFlow DELTA 3 Max monitoring and optional CLI profile automation for Windows
ReleaseNotesUrl: https://github.com/{repository}/releases/tag/{__version__}
ManifestType: defaultLocale
""" + suffix,
        f"{PACKAGE_ID}.locale.uk-UA.yaml": common + """PackageLocale: uk-UA
Publisher: Get it Simple
PackageName: EcoFlow Monitor
License: MIT
ShortDescription: Моніторинг EcoFlow DELTA 3 Max та автоматизація CLI-профілів для Windows
ManifestType: locale
""" + suffix,
    }
    for name, content in files.items():
        content = manifest_schema_header(MANIFEST_TYPES[name]) + content
        (directory / name).write_text(content, encoding="utf-8", newline="\r\n")


def asset_names() -> set[str]:
    return {f"EcoFlowMonitor-{__version__}-x64.exe", f"EcoFlowMonitor-{__version__}-portable.zip",
            f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe", f"{PACKAGE_ID}-{__version__}-winget.zip"}


def verify_release(directory: Path) -> None:
    validate_version()
    expected = asset_names()
    if {path.name for path in directory.iterdir()} != expected | {"SHA256SUMS.txt", "winget"}:
        raise ValueError("Unexpected or missing release assets")
    lines = (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    if lines != [f"{digest(directory / name)}  {name}" for name in sorted(expected)]:
        raise ValueError("Release checksum mismatch")
    executable = directory / f"EcoFlowMonitor-{__version__}-x64.exe"
    verify_application(executable)
    verify_portable(directory / f"EcoFlowMonitor-{__version__}-portable.zip", executable)
    installer = directory / f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe"
    verify_version(installer)
    names = set(MANIFEST_TYPES)
    manifests = directory / "winget"
    if {path.name for path in manifests.iterdir()} != names:
        raise ValueError("Unexpected WinGet manifest bundle")
    content = (manifests / f"{PACKAGE_ID}.installer.yaml").read_text(encoding="utf-8")
    if content.count("Scope: machine") != 1 or "Scope: user" in content or f"InstallerSha256: {digest(installer).upper()}" not in content:
        raise ValueError("WinGet scope or installer hash mismatch")
    url = re.search(r"^  InstallerUrl: (\S+)$", content, re.MULTILINE)
    if not url or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/" +
                                  re.escape(__version__) + "/" + re.escape(installer.name), url.group(1)):
        raise ValueError("WinGet installer URL does not match the release")
    for name, manifest_type in MANIFEST_TYPES.items():
        content = (manifests / name).read_text(encoding="utf-8")
        if not content.startswith(manifest_schema_header(manifest_type)):
            raise ValueError(f"WinGet manifest schema header mismatch: {name}")
        lines = content.splitlines()
        if lines.count(f"ManifestType: {manifest_type}") != 1 or lines.count(f"ManifestVersion: {MANIFEST_VERSION}") != 1:
            raise ValueError(f"WinGet manifest type or schema version mismatch: {name}")
        if f"PackageVersion: {__version__}" not in lines:
            raise ValueError("WinGet version mismatch")
    with ZipFile(directory / f"{PACKAGE_ID}-{__version__}-winget.zip") as archive:
        if set(archive.namelist()) != names or len(archive.namelist()) != len(names):
            raise ValueError("Unexpected WinGet archive payload")
        if any(archive.read(name) != (manifests / name).read_bytes() for name in names):
            raise ValueError("WinGet archive mismatch")
    print(f"Release {__version__}: PE versions, archives, manifests and SHA-256 verified")


def prepare_release(executable: Path, installer: Path, target: Path, repository: str = REPOSITORY) -> None:
    verify_application(executable)
    verify_version(installer)
    expected_installer = f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe"
    if installer.name != expected_installer:
        raise ValueError("Unexpected installer filename")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        staged = Path(directory) / "release"
        staged.mkdir()
        shutil.copy2(executable, staged / f"EcoFlowMonitor-{__version__}-x64.exe")
        shutil.copy2(installer, staged / installer.name)
        package_portable(executable, staged / f"EcoFlowMonitor-{__version__}-portable.zip")
        write_manifests(staged / "winget", staged / installer.name, repository)
        with ZipFile(staged / f"{PACKAGE_ID}-{__version__}-winget.zip", "w", ZIP_DEFLATED) as archive:
            for path in sorted((staged / "winget").iterdir()):
                archive.write(path, path.name)
        (staged / "SHA256SUMS.txt").write_text("".join(f"{digest(staged / name)}  {name}\n" for name in sorted(asset_names())), encoding="utf-8")
        verify_release(staged)
        backup = Path(directory) / "previous"
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Build, prepare and verify EcoFlow Monitor releases")
    parser.add_argument("action", choices=("installer", "prepare", "verify", "build"))
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--app-exe", type=Path, default=ROOT / "dist" / __version__ / "EcoFlowMonitor.exe")
    parser.add_argument("--installer-directory", type=Path, default=ROOT / "dist/installers" / __version__)
    parser.add_argument("--release-directory", type=Path, default=ROOT / "dist/release" / __version__)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--makensis")
    args = parser.parse_args()
    validate_version(args.version)
    if args.action == "verify":
        verify_release(args.release_directory)
        return
    if args.action == "build":
        subprocess.run([sys.executable, str(ROOT / "build.py"), "--dist-dir", str(args.app_exe.parent)], cwd=ROOT, check=True)
    if args.action in ("installer", "build"):
        build_installer(args.app_exe, args.installer_directory, args.makensis)
    if args.action in ("prepare", "build"):
        subprocess.run([sys.executable, "-m", "scripts.smoke_executable", str(args.app_exe)], cwd=ROOT, check=True)
        installer = args.installer_directory / f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe"
        prepare_release(args.app_exe, installer, args.release_directory, args.repository)


if __name__ == "__main__":
    main()
