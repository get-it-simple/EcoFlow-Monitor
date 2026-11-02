param(
    [string]$Version,
    [string]$ReleaseDirectory,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $arguments = @("-m", "scripts.release", "verify")
    if ($Version) { $arguments += @("--version", $Version) }
    if ($ReleaseDirectory) { $arguments += @("--release-directory", $ReleaseDirectory) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Release verification failed: $LASTEXITCODE" }
} finally {
    Pop-Location
}
