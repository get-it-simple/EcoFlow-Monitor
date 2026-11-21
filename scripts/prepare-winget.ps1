param([ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = "1.29.380")

$ErrorActionPreference = "Stop"
if ($env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_OS -ne "Windows") {
    throw 'WinGet preparation is restricted to disposable GitHub Windows runners'
}
$command = Get-Command winget -ErrorAction SilentlyContinue
if ($command) {
    $current = & $command.Source --version
    if ($LASTEXITCODE -eq 0 -and $current.TrimStart('v') -eq $Version) { exit 0 }
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'WinGet preparation requires an x64 runner'
}
$staging = Join-Path $env:RUNNER_TEMP ("winget-$Version-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $staging | Out-Null
$release = "https://github.com/microsoft/winget-cli/releases/download/v$Version"
$bundle = Join-Path $staging 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle'
$archive = Join-Path $staging 'DesktopAppInstaller_Dependencies.zip'
Invoke-WebRequest -Uri "$release/$(Split-Path $bundle -Leaf)" -OutFile $bundle
Invoke-WebRequest -Uri "$release/$(Split-Path $archive -Leaf)" -OutFile $archive
$dependencies = Join-Path $staging 'dependencies'
Expand-Archive -LiteralPath $archive -DestinationPath $dependencies
Import-Module Appx -UseWindowsPowerShell
$packages = @(Get-ChildItem -LiteralPath (Join-Path $dependencies 'x64') -File |
    Where-Object { $_.Extension -in '.appx', '.msix' })
if ($packages.Count -eq 0) { throw 'WinGet release has no x64 dependencies' }
foreach ($package in $packages) {
    if ($package.BaseName -notmatch '^(?<name>.+)_(?<version>\d+\.\d+\.\d+\.\d+)_x64$') {
        throw "Unexpected WinGet dependency: $($package.Name)"
    }
    $name = $Matches.name
    $required = [version]$Matches.version
    $installed = @(Get-AppxPackage -Name $name | Where-Object {
        $_.Architecture -eq 'X64' -and [version]$_.Version -ge $required
    })
    if ($installed.Count -eq 0) { Add-AppxPackage -Path $package.FullName }
}
Add-AppxPackage -Path $bundle -ForceUpdateFromAnyVersion
$env:PATH = "$(Join-Path $env:LOCALAPPDATA 'Microsoft/WindowsApps');$env:PATH"
$current = winget --version
if ($LASTEXITCODE -ne 0 -or $current.TrimStart('v') -ne $Version) {
    throw "Expected WinGet $Version; got $current"
}
