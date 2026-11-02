param(
    [string]$Version,
    [string]$AppExe,
    [string]$InstallerDirectory,
    [string]$ReleaseDirectory,
    [string]$Repository = "get-it-simple/EcoFlow-Monitor",
    [string]$MakeNsis,
    [switch]$Build,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $action = if ($Build) { "build" } else { "prepare" }
    $arguments = @("-m", "scripts.release", $action, "--repository", $Repository)
    if ($Version) { $arguments += @("--version", $Version) }
    if ($AppExe) { $arguments += @("--app-exe", $AppExe) }
    if ($InstallerDirectory) { $arguments += @("--installer-directory", $InstallerDirectory) }
    if ($ReleaseDirectory) { $arguments += @("--release-directory", $ReleaseDirectory) }
    if ($MakeNsis) { $arguments += @("--makensis", $MakeNsis) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Release preparation failed: $LASTEXITCODE" }
} finally {
    Pop-Location
}
