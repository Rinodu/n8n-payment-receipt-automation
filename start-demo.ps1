$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:MODE = 'demo'
if (-not $env:PROCESSOR_KEY) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $env:PROCESSOR_KEY = [Convert]::ToBase64String($bytes)
}
# Child demo sender inherits the key; do not print it.
Write-Host 'Starting demo server on port 8787. For a one-command demonstration without credential setup, use python run_demo.py instead.'
python processor.py
