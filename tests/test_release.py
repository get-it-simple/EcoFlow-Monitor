from zipfile import ZipFile

import pytest
import pefile

from ecoflow_monitor import __version__
from scripts import release


@pytest.mark.parametrize("version", ["v0.8.0", "0.8", "00.8.0", "65536.0.0", "0.7.3"])
def test_invalid_or_mismatched_versions_are_rejected(version):
    with pytest.raises(ValueError):
        release.validate_version(version)


def test_partial_signing_configuration_is_rejected(monkeypatch):
    for name in ("WINDOWS_SIGNING_CERTIFICATE_BASE64", "WINDOWS_SIGNING_CERTIFICATE_PASSWORD", "WINDOWS_TIMESTAMP_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WINDOWS_TIMESTAMP_URL", "https://example.invalid")
    with pytest.raises(ValueError):
        release.sign([])


def release_inputs(tmp_path, monkeypatch):
    executable = tmp_path / "EcoFlowMonitor.exe"
    executable.write_bytes(b"synthetic application")
    installer = tmp_path / f"EcoFlowMonitor-{__version__}-x64-machine-setup.exe"
    installer.write_bytes(b"synthetic installer")
    monkeypatch.setattr(release, "verify_application", lambda path: None)
    monkeypatch.setattr(release, "verify_version", lambda path: None)
    return executable, installer


def test_release_payload_checksums_and_machine_manifests(tmp_path, monkeypatch):
    executable, installer = release_inputs(tmp_path, monkeypatch)
    target = tmp_path / "release"
    release.prepare_release(executable, installer, target)
    release.verify_release(target)
    manifest = (target / "winget" / f"{release.PACKAGE_ID}.installer.yaml").read_text(encoding="utf-8")
    assert "Scope: machine" in manifest and "Scope: user" not in manifest
    assert f"/releases/download/{__version__}/{installer.name}" in manifest
    assert release.digest(installer).upper() in manifest
    with ZipFile(target / f"EcoFlowMonitor-{__version__}-portable.zip") as archive:
        assert archive.read("EcoFlowMonitor.exe") == executable.read_bytes()


@pytest.mark.parametrize("corruption", ["asset", "extra", "checksum", "manifest", "url", "bundle"])
def test_release_verification_rejects_corruption(tmp_path, monkeypatch, corruption):
    executable, installer = release_inputs(tmp_path, monkeypatch)
    target = tmp_path / "release"
    release.prepare_release(executable, installer, target)
    if corruption == "asset":
        (target / installer.name).write_bytes(b"corrupt")
    elif corruption == "extra":
        (target / "old-release.exe").write_bytes(b"old")
    elif corruption == "checksum":
        (target / "SHA256SUMS.txt").write_text("", encoding="utf-8")
    elif corruption == "manifest":
        (target / "winget" / f"{release.PACKAGE_ID}.installer.yaml").write_text("Scope: user\n", encoding="utf-8")
    elif corruption == "url":
        path = target / "winget" / f"{release.PACKAGE_ID}.installer.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace(f"/download/{__version__}/", "/download/old/"), encoding="utf-8")
    else:
        with ZipFile(target / f"{release.PACKAGE_ID}-{__version__}-winget.zip", "w") as archive:
            archive.writestr("wrong.yaml", "wrong")
        (target / "SHA256SUMS.txt").write_text("".join(f"{release.digest(target / name)}  {name}\n" for name in sorted(release.asset_names())), encoding="utf-8")
    with pytest.raises(ValueError):
        release.verify_release(target)


def test_failed_preparation_preserves_previous_release(tmp_path, monkeypatch):
    executable, installer = release_inputs(tmp_path, monkeypatch)
    target = tmp_path / "release"
    release.prepare_release(executable, installer, target)
    old = (target / "SHA256SUMS.txt").read_bytes()
    def fail(*args):
        raise ValueError("Synthetic packaging failure")
    monkeypatch.setattr(release, "write_manifests", fail)
    with pytest.raises(ValueError):
        release.prepare_release(executable, installer, target)
    assert (target / "SHA256SUMS.txt").read_bytes() == old
    release.verify_release(target)


def test_stale_executable_is_rejected_before_replacing_archive(tmp_path):
    executable = tmp_path / "old.exe"
    executable.write_bytes(b"invalid PE")
    archive = tmp_path / "portable.zip"
    archive.write_bytes(b"previous archive")
    with pytest.raises(pefile.PEFormatError):
        release.package_portable(executable, archive)
    assert archive.read_bytes() == b"previous archive"
