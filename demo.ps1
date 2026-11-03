$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$url = if ($env:PAYMENT_URL) { $env:PAYMENT_URL } else { 'http://127.0.0.1:8787/payments' }
$headerName = if ($env:PAYMENT_URL) { 'X-Webhook-Key' } else { 'X-Processor-Key' }
$key = if ($env:PAYMENT_URL) { $env:WEBHOOK_KEY } elseif ($env:PROCESSOR_KEYS) { $env:PROCESSOR_KEYS.Split(',')[0] } else { $env:PROCESSOR_KEY }
if (-not $key) { throw 'Missing authentication key' }
$headers = @{ $headerName = $key }
$payload = Get-Content -Raw sample-payment.json
Invoke-RestMethod -Method Post -Uri $url -Headers $headers -ContentType 'application/json' -Body $payload
Invoke-RestMethod -Method Post -Uri $url -Headers $headers -ContentType 'application/json' -Body $payload
