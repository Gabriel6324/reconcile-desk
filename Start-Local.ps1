$ErrorActionPreference = 'Stop'
$workspace = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$runtimePython = Join-Path $workspace 'work\runtime\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    throw 'Local runtime is unavailable here. See README.md for Docker startup.'
}
& $runtimePython (Join-Path $PSScriptRoot 'scripts\local_runner.py')
exit $LASTEXITCODE
