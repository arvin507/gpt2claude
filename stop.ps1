$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'proxy.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No managed proxy process.'; exit 0 }
$proxyProcessId = [int](Get-Content -Raw -LiteralPath $pidFile)
$proxyProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$proxyProcessId"
$proxyFile = Join-Path $PSScriptRoot 'proxy.py'
if ($proxyProcess -and $proxyProcess.CommandLine.Contains($proxyFile)) {
    Stop-Process -Id $proxyProcessId
    Write-Output 'Proxy stopped.'
} elseif ($proxyProcess) {
    throw 'PID belongs to another process; refusing to stop it.'
} else { Write-Output 'Proxy is already stopped.' }
Remove-Item -LiteralPath $pidFile
