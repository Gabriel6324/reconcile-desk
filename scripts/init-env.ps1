$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$target = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $target) {
    Write-Host '.env already exists; kept unchanged.'
    exit 0
}
$randomBytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
$secret = -join ($randomBytes | ForEach-Object { $_.ToString('x2') })
$template = [System.IO.File]::ReadAllText((Join-Path $projectRoot '.env.example'))
$content = $template.Replace('replace_with_generated_hex_password', $secret)
[System.IO.File]::WriteAllText($target, $content, (New-Object System.Text.UTF8Encoding($false)))
Write-Host 'Created local .env. The generated database password was not printed.'
