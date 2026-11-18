param([string]$Version = "1.29.380")

$ErrorActionPreference = "Stop"
if ($env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_OS -ne "Windows") {
    throw 'WinGet preparation is restricted to disposable GitHub Windows runners'
}
$command = Get-Command winget -ErrorAction SilentlyContinue
if ($command) {
    $current = & $command.Source --version
    if ($LASTEXITCODE -eq 0 -and $current.TrimStart('v') -eq $Version) { exit 0 }
}
Install-Module -Name Microsoft.WinGet.Client -Scope CurrentUser -Repository PSGallery -Force
Import-Module Microsoft.WinGet.Client
Repair-WinGetPackageManager -Version $Version -AllUsers
$current = winget --version
if ($LASTEXITCODE -ne 0 -or $current.TrimStart('v') -ne $Version) {
    throw "Expected WinGet $Version; got $current"
}
