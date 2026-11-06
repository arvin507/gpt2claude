$ErrorActionPreference = 'Stop'
$proxyRoot = $PSScriptRoot
& python (Join-Path $proxyRoot 'proxy.py') --init
if ($LASTEXITCODE -ne 0) { throw 'Proxy initialization failed.' }
$config = Get-Content -Raw -LiteralPath (Join-Path $proxyRoot 'config.json') | ConvertFrom-Json
$baseUrl = "http://127.0.0.1:$($config.port)"
try {
    $health = Invoke-RestMethod "$baseUrl/health" -TimeoutSec 2
    if ($health.service -ne 'claude-codex-proxy') { throw 'Port is occupied by another service.' }
    Write-Output "Proxy is already running: $baseUrl (model: $($health.upstream_model))"
    exit 0
} catch [System.Net.WebException] {
    # No running proxy: start one below.
}
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$proxyFile = Join-Path $proxyRoot 'proxy.py'
$proxyProcess = Start-Process -FilePath $pythonPath -ArgumentList @('"' + $proxyFile + '"') -WorkingDirectory $proxyRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $proxyRoot 'stdout.log') -RedirectStandardError (Join-Path $proxyRoot 'proxy.log')
$proxyProcess.Id | Set-Content -LiteralPath (Join-Path $proxyRoot 'proxy.pid')
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 200
    try {
        $health = Invoke-RestMethod "$baseUrl/health" -TimeoutSec 2
        if ($health.service -eq 'claude-codex-proxy') {
            Write-Output "Proxy started: $baseUrl (model: $($health.upstream_model))"
            exit 0
        }
    } catch { }
    if ($proxyProcess.HasExited) { break }
}
throw "Proxy did not start. Check $proxyRoot\proxy.log"
