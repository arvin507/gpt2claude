# Start Claude Code with the generated proxy settings. Global Claude settings are untouched.
$ErrorActionPreference = 'Stop'
$claudeArgs = $args
& (Join-Path $PSScriptRoot 'start.ps1')
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'Proxy startup failed.' }
$claudePath = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claudePath) {
    $claudePath = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
}
if (-not (Test-Path -LiteralPath $claudePath)) { throw 'Claude Code executable not found.' }
& $claudePath --settings (Join-Path $PSScriptRoot 'claude-settings.json') @claudeArgs
exit $LASTEXITCODE
