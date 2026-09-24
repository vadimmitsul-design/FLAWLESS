param(
    [switch]$Stop,
    [int]$BackendPort = 8001,
    [int]$FrontendPort = 3000
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$previewRoot = Join-Path $repoRoot 'dist\cabinet-preview'
$recordsPath = Join-Path $previewRoot 'processes.json'
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$frontendRoot = Join-Path $repoRoot 'frontend'
$nextPath = Join-Path $frontendRoot 'node_modules\next\dist\bin\next'

function Stop-PreviewProcesses {
    if (-not (Test-Path -LiteralPath $recordsPath)) { return }
    $records = @(Get-Content -LiteralPath $recordsPath -Raw | ConvertFrom-Json)
    foreach ($record in $records) {
        $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        if ($process.StartTime.ToUniversalTime().ToString('o') -ne $record.started_at -or
            $process.Path -ne $record.path) {
            throw "PID $($record.id) no longer belongs to this preview; refusing to stop it."
        }
        # Stop only descendants of the verified process, in child-first order.
        $snapshot = @(Get-CimInstance Win32_Process)
        $owned = [Collections.Generic.List[int]]::new()
        $owned.Add([int]$record.id)
        for ($index = 0; $index -lt $owned.Count; $index++) {
            foreach ($child in $snapshot | Where-Object { $_.ParentProcessId -eq $owned[$index] }) {
                if ($child.CreationDate.ToUniversalTime() -ge $process.StartTime.ToUniversalTime()) {
                    $owned.Add([int]$child.ProcessId)
                }
            }
        }
        for ($index = $owned.Count - 1; $index -ge 0; $index--) {
            Stop-Process -Id $owned[$index] -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $recordsPath
    Write-Host 'Local FLAWLESS preview stopped. Demo data is preserved.'
}

if ($Stop) {
    Stop-PreviewProcesses
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements-dev.lock first.' }
if (-not (Test-Path -LiteralPath $nextPath)) { throw 'Run npm ci in frontend first.' }
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
if ($BackendPort -eq $FrontendPort -or $BackendPort -lt 1024 -or $FrontendPort -lt 1024 -or
    $BackendPort -gt 65535 -or $FrontendPort -gt 65535) {
    throw 'Choose two different local ports between 1024 and 65535.'
}
foreach ($port in @($BackendPort, $FrontendPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already occupied. Use -Stop for this preview or choose other ports."
    }
}
if (Test-Path -LiteralPath $recordsPath) {
    foreach ($record in @(Get-Content -LiteralPath $recordsPath -Raw | ConvertFrom-Json)) {
        if (Get-Process -Id $record.id -ErrorAction SilentlyContinue) {
            throw 'Preview processes still exist. Run this script with -Stop first.'
        }
    }
}
New-Item -ItemType Directory -Path $previewRoot -Force | Out-Null
if ((Get-Item -LiteralPath $previewRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'The preview directory must not be a link.'
}
if (Test-Path -LiteralPath (Join-Path $previewRoot '.env')) {
    throw 'Remove the .env file from the isolated preview directory before starting.'
}

# Children receive only OS essentials and explicit local configuration. No inherited
# provider tokens, Telegram credentials, DATABASE_URL or proxy settings are copied.
$savedEnvironment = @{}
Get-ChildItem Env: | ForEach-Object { $savedEnvironment[$_.Name] = $_.Value }
$osVariables = @(
    'PATH', 'PATHEXT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
    'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
    'PROGRAMDATA', 'ProgramFiles', 'ProgramFiles(x86)', 'SYSTEMDRIVE', 'USERNAME'
)
$records = [Collections.Generic.List[object]]::new()
function Save-ProcessRecord($process, $name) {
    $process.Refresh()
    $records.Add([pscustomobject]@{
        name = $name
        id = $process.Id
        path = $process.Path
        started_at = $process.StartTime.ToUniversalTime().ToString('o')
    })
    ConvertTo-Json -InputObject @($records.ToArray()) | Set-Content -LiteralPath $recordsPath -Encoding UTF8
}

try {
    Get-ChildItem Env: | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }
    foreach ($name in $osVariables) {
        if ($savedEnvironment.ContainsKey($name)) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
        }
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
    $env:NEXT_TELEMETRY_DISABLED = '1'
    $env:NEXT_PUBLIC_CABINET_DEMO = 'true'
    & $pythonPath (Join-Path $PSScriptRoot 'seed_cabinet_demo.py')
    if ($LASTEXITCODE -ne 0) { throw 'Demo seeding failed.' }

    $secretPath = Join-Path $previewRoot 'session-secret.txt'
    if (-not (Test-Path -LiteralPath $secretPath)) {
        $secret = & $pythonPath -c 'import secrets; print(secrets.token_urlsafe(48))'
        [IO.File]::WriteAllText($secretPath, $secret.Trim())
    }
    $env:DATABASE_URL = 'sqlite+aiosqlite:///' + (Join-Path $previewRoot 'cabinet.sqlite3').Replace('\', '/')
    $env:SESSION_SECRET = (Get-Content -LiteralPath $secretPath -Raw).Trim()
    $env:MODELS_CONFIG_PATH = Join-Path $previewRoot 'models.yaml'
    $env:ENVIRONMENT = 'development'
    $env:SIGNUP_MODE = 'closed'
    $env:INSTANCE_NAME = 'LOCAL DEMO'
    $env:TELEGRAM_BOT_TOKEN = ''
    $env:LLM_NUM_RETRIES = '0'
    $env:LLM_TIMEOUT_SECONDS = '2'
    $env:ENABLE_PUBLIC_SITE = 'false'
    $env:ENABLE_RESOURCES = 'false'
    $env:ENABLE_SHOP = 'false'
    $env:ENABLE_PROMPTS = 'false'
    $env:ENABLE_CHILDREN = 'false'
    $env:ENABLE_ARCHIVE = 'false'
    $backend = Start-Process -FilePath $pythonPath -ArgumentList @(
        '-m', 'uvicorn', 'app.main:app', '--app-dir', "`"$repoRoot`"",
        '--host', '127.0.0.1', '--port', $BackendPort, '--no-proxy-headers'
    ) -WorkingDirectory $previewRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $previewRoot 'backend.stdout.log') `
        -RedirectStandardError (Join-Path $previewRoot 'backend.stderr.log')
    Save-ProcessRecord $backend 'backend'

    # Next needs only its backend address; do not give it the session secret.
    foreach ($name in @('DATABASE_URL', 'SESSION_SECRET', 'MODELS_CONFIG_PATH')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:FLAWLESS_BACKEND_URL = "http://127.0.0.1:$BackendPort"
    $frontend = Start-Process -FilePath $nodePath -ArgumentList @(
        "`"$nextPath`"", 'dev', '--hostname', '127.0.0.1', '--port', $FrontendPort
    ) -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $previewRoot 'frontend.stdout.log') `
        -RedirectStandardError (Join-Path $previewRoot 'frontend.stderr.log')
    Save-ProcessRecord $frontend 'frontend'
} catch {
    if ($records.Count -gt 0) { Stop-PreviewProcesses }
    throw
} finally {
    Get-ChildItem Env: | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }
    foreach ($entry in $savedEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}

Write-Host "FLAWLESS preview starting: http://127.0.0.1:$FrontendPort"
Write-Host 'Login: demo@flawless.local / Neon-Demo-2026!'
Write-Host "Synthetic data, logs and owned process IDs: $previewRoot"
Write-Host 'Provider calls are disabled. Stop with the same script and -Stop.'
