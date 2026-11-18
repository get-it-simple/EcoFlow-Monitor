param(
    [string]$Version,
    [string]$AppExe,
    [string]$OutputDirectory,
    [string]$MakeNsis,
    [switch]$Build,
    [switch]$UseExisting,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    if ($Build -and $UseExisting) { throw 'Choose either -Build or -UseExisting' }
    if (-not $UseExisting) {
        $buildArguments = @((Join-Path $root "build.py"))
        if ($AppExe) { $buildArguments += @("--dist-dir", (Split-Path -Parent $AppExe)) }
        & $Python @buildArguments
        if ($LASTEXITCODE -ne 0) { throw "Application build failed: $LASTEXITCODE" }
    }
    $arguments = @("-m", "scripts.release", "installer")
    if ($Version) { $arguments += @("--version", $Version) }
    if ($AppExe) { $arguments += @("--app-exe", $AppExe) }
    if ($OutputDirectory) { $arguments += @("--installer-directory", $OutputDirectory) }
    if ($MakeNsis) { $arguments += @("--makensis", $MakeNsis) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed: $LASTEXITCODE" }
} finally {
    Pop-Location
}
