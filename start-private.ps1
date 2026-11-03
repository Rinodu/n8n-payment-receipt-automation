param([switch]$Status)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .private/settings.xml)) { throw 'Run configure.ps1 first.' }
$settings = Import-Clixml .private/settings.xml
try {
    foreach ($name in $settings.Keys) {
        $value = [Net.NetworkCredential]::new('', $settings[$name]).Password
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
    New-Item -ItemType Directory -Force data | Out-Null
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls (Join-Path $PSScriptRoot 'data') /inheritance:r /grant:r ('{0}:(OI)(CI)F' -f $identity) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict access to the data folder.' }
    if ($Status) { python admin.py status } else { python processor.py }
    if ($LASTEXITCODE -ne 0) { throw 'The receipt service reported an error. Private configuration remains preserved.' }
} finally {
    foreach ($name in $settings.Keys) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    $value = $null
}
