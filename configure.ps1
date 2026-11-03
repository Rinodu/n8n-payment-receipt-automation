param([ValidateSet('demo','live')][string]$Mode = 'demo')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function New-PrivateKey {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes)
}
function Protect-Value([string]$Value) {
    $secure = [Security.SecureString]::new()
    foreach ($character in $Value.ToCharArray()) { $secure.AppendChar($character) }
    $secure.MakeReadOnly()
    return $secure
}
New-Item -ItemType Directory -Force .private | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls (Join-Path $PSScriptRoot '.private') /inheritance:r /grant:r ('{0}:(OI)(CI)F' -f $identity) | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the private settings folder.' }
if (Test-Path .private/settings.xml) { throw 'Existing private settings preserved. Follow OPERATIONS.md for key rotation; do not overwrite keys.' }
$settings = @{}
$settings['MODE'] = Protect-Value $Mode
foreach ($name in @('PROCESSOR_KEYS','INDEX_KEY','N8N_ENCRYPTION_KEY')) {
    $settings[$name] = Protect-Value (New-PrivateKey)
}
$fernet = & python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt first.' }
$settings['DATA_KEYS'] = Protect-Value $fernet.Trim()
if ($Mode -eq 'live') {
    Write-Host 'Sandbox payments only. Use a test inbox you own and consent to receiving receipts.'
    foreach ($name in @('AIRTABLE_TOKEN','AIRTABLE_BASE_ID','AIRTABLE_TABLE_ID','RESEND_API_KEY','EMAIL_FROM','TEST_RECIPIENT','MIDTRANS_SERVER_KEY')) {
        $settings[$name] = Read-Host $name -AsSecureString
    }
}
$settings | Export-Clixml .private/settings.xml
Write-Host 'Settings saved with Windows user encryption. Keys are not printed. This file works only for this Windows user on this PC.'
Write-Host 'Use start-private.ps1 to start the service. See OPERATIONS.md for backing up keys separately.'
