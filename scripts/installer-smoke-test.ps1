param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
if ($env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_OS -ne "Windows") {
    throw "Run this machine installation check only on a disposable GitHub Windows runner."
}
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $env:ProgramW6432 "EcoFlowMonitor"
$registry = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GetItSimple.EcoFlowMonitor"
$startupRegistry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$startupCommand = ""
$installer = Join-Path $root "dist\installers\$Version\EcoFlowMonitor-$Version-x64-machine-setup.exe"
if ((Test-Path -LiteralPath $registry) -or (Test-Path -LiteralPath $target)) {
    throw "Smoke test requires a clean runner without an existing EcoFlow Monitor installation."
}
if ((Get-ItemProperty -LiteralPath $startupRegistry -Name EcoFlowMonitor -ErrorAction SilentlyContinue).EcoFlowMonitor) {
    throw "Smoke test requires a clean runner without EcoFlow Monitor startup registration."
}
$synthetic = Join-Path $env:RUNNER_TEMP "ecoflow-installer-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $synthetic | Out-Null
$settings = Join-Path $synthetic "settings.json"
$settingsBytes = '{"version":2,"integrations":[]}'
[System.IO.File]::WriteAllText($settings, $settingsBytes, [System.Text.UTF8Encoding]::new($false))
$settingsHash = (Get-FileHash -LiteralPath $settings -Algorithm SHA256).Hash
$runningSettings = Join-Path $synthetic "running-settings.json"
& python -m scripts.installer_smoke_settings create $runningSettings
if ($LASTEXITCODE -ne 0) { throw "Synthetic application settings creation failed" }

function Assert-FilePreserved([string]$Path, [string]$ExpectedHash, [string]$Stage) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "${Stage}: settings file was removed: $Path"
    }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $ExpectedHash) {
        throw "${Stage}: settings contents were changed: $Path"
    }
}

function Assert-RunningPreferences([string]$Stage) {
    & python -m scripts.installer_smoke_settings verify $runningSettings --stage $Stage
    if ($LASTEXITCODE -ne 0) { throw "${Stage}: application preferences verification failed" }
}

function Invoke-Setup([string]$Path, [string[]]$Arguments) {
    $process = Start-Process -FilePath $Path -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000)) { throw "Installer timed out: $Path" }
    if ($process.ExitCode -ne 0) { throw "Installer failed: $($process.ExitCode)" }
}

function Assert-Installed {
    if ($startupCommand -and (Get-ItemPropertyValue -LiteralPath $startupRegistry -Name EcoFlowMonitor) -ne $startupCommand) {
        throw "Upgrade changed startup registration"
    }
    $entry = Get-ItemProperty -LiteralPath $registry
    if ($entry.DisplayVersion -ne $Version -or $entry.InstallLocation -ne $target) {
        throw "Installed registry metadata mismatch"
    }
    foreach ($name in @("EcoFlowMonitor.exe", "Uninstall.exe", "LICENSE", ".ecoflow-monitor-installed")) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $name) -PathType Leaf)) { throw "Missing installed file: $name" }
    }
    $shortcut = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "EcoFlow Monitor\EcoFlow Monitor.lnk"
    if (-not (Test-Path -LiteralPath $shortcut)) { throw "Missing shared Start Menu shortcut" }
    if (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GetItSimple.EcoFlowMonitor") {
        throw "Unexpected user installation registration"
    }
}

Invoke-Setup $installer @("/S")
Assert-Installed
Assert-FilePreserved $settings $settingsHash "Initial installation"
$executable = Join-Path $target "EcoFlowMonitor.exe"
$sourceHash = (Get-FileHash -LiteralPath (Join-Path $root "dist\$Version\EcoFlowMonitor.exe") -Algorithm SHA256).Hash
if ((Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash -ne $sourceHash) { throw "Installed payload mismatch" }
& python -m scripts.smoke_executable $executable
if ($LASTEXITCODE -ne 0) { throw "Installed EXE smoke failed" }
$startupCommand = '"{0}" --autostart' -f $executable
New-Item -Path $startupRegistry -Force | Out-Null
New-ItemProperty -LiteralPath $startupRegistry -Name EcoFlowMonitor -Value $startupCommand -PropertyType String -Force | Out-Null

$running = Start-Process -FilePath $executable -ArgumentList @("--settings", "`"$runningSettings`"") -WindowStyle Hidden -PassThru
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 100
    $ready = Get-ChildItem -LiteralPath (Join-Path $synthetic "logs") -ErrorAction SilentlyContinue
} until ($ready -or [DateTime]::UtcNow -gt $deadline)
if (-not $ready -or $running.HasExited) { throw "Synthetic installed application did not start" }
Invoke-Setup $installer @("/SILENTWITHPROGRESS")
if (-not $running.WaitForExit(30000)) { throw "Upgrade did not shut down the running application" }
Assert-Installed
Assert-FilePreserved $settings $settingsHash "Running upgrade"
Assert-RunningPreferences "Running upgrade"
$runningSettingsHash = (Get-FileHash -LiteralPath $runningSettings -Algorithm SHA256).Hash

Set-ItemProperty -LiteralPath $registry -Name DisplayVersion -Value "0.7.3"
Invoke-Setup $installer @("/S")
Assert-Installed
Assert-FilePreserved $settings $settingsHash "Previous-version metadata upgrade"
Assert-FilePreserved $runningSettings $runningSettingsHash "Previous-version metadata upgrade"
Invoke-Setup (Join-Path $target "Uninstall.exe") @("/S")
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (((Test-Path -LiteralPath $registry) -or (Test-Path -LiteralPath $executable)) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 100
}
if (Test-Path -LiteralPath $registry) { throw "Uninstall registry entry remains" }
if (Test-Path -LiteralPath $executable) { throw "Application executable remains" }
if ((Get-ItemProperty -LiteralPath $startupRegistry -Name EcoFlowMonitor -ErrorAction SilentlyContinue).EcoFlowMonitor) {
    throw "Uninstall left startup registration"
}
Assert-FilePreserved $settings $settingsHash "Uninstall"
Assert-FilePreserved $runningSettings $runningSettingsHash "Uninstall"
if (-not (Test-Path -LiteralPath (Join-Path $synthetic "logs"))) { throw "User logs were removed" }
Write-Host "Machine installation, installed EXE smoke, running upgrade, previous-version metadata, uninstall and data preservation passed."
