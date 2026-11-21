param([string]$Script, [string]$Scenario, [string]$Staging)

$ErrorActionPreference = 'Stop'
$env:GITHUB_ACTIONS = if ($Scenario -eq 'local') { 'false' } else { 'true' }
$env:RUNNER_OS = 'Windows'
$env:RUNNER_TEMP = $Staging
$global:wingetTestState = @{
    Installed = $false
    Downloads = [System.Collections.Generic.List[string]]::new()
    Deployments = [System.Collections.Generic.List[object]]::new()
}
function Get-Command {
    param($Name, $ErrorAction)
    if ($Scenario -eq 'ready') { [pscustomobject]@{ Source = 'winget' } }
}
function winget {
    $global:LASTEXITCODE = 0
    if ($Scenario -eq 'ready' -or ($global:wingetTestState.Installed -and $Scenario -ne 'wrong-version')) {
        'v1.29.380'
    } else { 'v1.0.0' }
}
function Import-Module { param($Name, [switch]$UseWindowsPowerShell) }
function Install-Module { throw 'Unexpected PowerShell Gallery installation' }
function Repair-WinGetPackageManager { throw 'Broken provisioned AppxBundleManifest registration' }
function Invoke-WebRequest {
    param($Uri, $OutFile)
    if ($Scenario -eq 'download-failure') { throw 'Synthetic download failure' }
    $global:wingetTestState.Downloads.Add($Uri)
    New-Item -ItemType File -Path $OutFile | Out-Null
}
function Expand-Archive {
    param($LiteralPath, $DestinationPath)
    $architecture = New-Item -ItemType Directory -Path (Join-Path $DestinationPath 'x64')
    if ($Scenario -ne 'missing-dependencies') {
        New-Item -ItemType File -Path (Join-Path $architecture 'Microsoft.VCLibs.140.00.UWPDesktop_14.0.33728.0_x64.appx') | Out-Null
    }
}
function Get-AppxPackage {
    param($Name)
    if ($Scenario -eq 'newer-dependency') {
        [pscustomobject]@{ Architecture = 'X64'; Version = '14.0.99999.0' }
    }
}
function Add-AppxPackage {
    param($Path, [switch]$ForceUpdateFromAnyVersion)
    $global:wingetTestState.Deployments.Add([pscustomobject]@{ Path = $Path; Downgrade = $ForceUpdateFromAnyVersion.IsPresent })
    if ($Path.EndsWith('.msixbundle')) { $global:wingetTestState.Installed = $true }
}
try {
    & $Script
    if ($Scenario -in 'local', 'download-failure', 'missing-dependencies', 'wrong-version') {
        throw "Expected preparation failure: $Scenario"
    }
} catch {
    $expected = switch ($Scenario) {
        'local' { 'restricted to disposable' }
        'download-failure' { 'Synthetic download failure' }
        'missing-dependencies' { 'no x64 dependencies' }
        'wrong-version' { 'Expected WinGet 1.29.380' }
        default { throw }
    }
    if ($_.Exception.Message -notmatch $expected) { throw }
}
if ($Scenario -in 'local', 'ready', 'download-failure') {
    if ($global:wingetTestState.Downloads.Count -ne 0 -or $global:wingetTestState.Deployments.Count -ne 0) {
        throw 'Preparation changed packages unexpectedly'
    }
} elseif ($Scenario -eq 'missing-dependencies') {
    if ($global:wingetTestState.Deployments.Count -ne 0) { throw 'Installed bundle without dependencies' }
} else {
    $expectedCount = if ($Scenario -eq 'newer-dependency') { 1 } else { 2 }
    if ($global:wingetTestState.Deployments.Count -ne $expectedCount) { throw 'Incorrect deployment sequence' }
    $last = $global:wingetTestState.Deployments[-1]
    if (-not $last.Path.EndsWith('.msixbundle') -or -not $last.Downgrade) {
        throw 'Pinned bundle was not installed directly with downgrade support'
    }
    if ($global:wingetTestState.Downloads.Count -ne 2 -or @($global:wingetTestState.Downloads | Where-Object {
        -not $_.StartsWith('https://github.com/microsoft/winget-cli/releases/download/v1.29.380/')
    }).Count -ne 0) { throw 'Downloads did not use the pinned official release' }
}
Write-Output "Verified $Scenario"
