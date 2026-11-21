from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runner preparation")
@pytest.mark.parametrize("scenario", [
    "local", "ready", "unregistered", "newer-dependency", "download-failure",
    "missing-dependencies", "wrong-version",
])
def test_winget_preparation_uses_release_bundle_without_registering_broken_image(tmp_path, scenario):
    powershell = shutil.which("pwsh")
    assert powershell, "PowerShell 7 is required for Windows CI preparation checks"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([
        powershell, "-NoProfile", "-NonInteractive", "-File",
        str(root / "tests/fixtures/prepare_winget.ps1"),
        "-Script", str(root / "scripts/prepare-winget.ps1"),
        "-Scenario", scenario, "-Staging", str(tmp_path),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Verified {scenario}" in result.stdout
