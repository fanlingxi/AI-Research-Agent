param(
    [ValidateSet('start', 'stop', 'status')][string]$Action = 'status',
    [string]$RunDirectory
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot 'data/runtime/resume-preview'
$statePath = Join-Path $runtime 'processes.json'
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$records = @()
if (Test-Path -LiteralPath $statePath) {
    $records = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}
function Get-ManagedProcess($record) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $record.started) {
        return $process
    }
}
if ($Action -eq 'stop') {
    foreach ($record in $records) {
        if (Get-ManagedProcess $record) {
            & taskkill.exe /PID $record.id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop $($record.name)." }
        }
    }
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
    return
}
if ($Action -eq 'status') {
    foreach ($record in $records) {
        $status = if (Get-ManagedProcess $record) { 'running' } else { 'stopped' }
        Write-Host "$($record.name): $status (PID $($record.id))"
    }
    return
}
if (@($records | Where-Object { Get-ManagedProcess $_ }).Count -gt 0) {
    throw 'A preview is already running. Stop it before selecting another archive.'
}
if (!$RunDirectory) { throw 'Provide -RunDirectory pointing to a completed evaluation arm.' }
$source = (Resolve-Path -LiteralPath $RunDirectory).Path
$allowed = [IO.Path]::GetFullPath((Join-Path $projectRoot 'data/evaluation/resume-closeout'))
if (!$source.StartsWith($allowed + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Only the managed closeout evaluation archives may be previewed.'
}
$run = Get-Content -LiteralPath (Join-Path $source 'archive/run.json') -Raw | ConvertFrom-Json
foreach ($port in @(18030, 15196)) {
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    try { $probe.Start() }
    catch { throw "Port $port is already in use; existing listeners are preserved." }
    finally { $probe.Stop() }
}
$copy = Join-Path $runtime ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
& $python -m app.persistence.backup backup (Join-Path $source 'knowledge.db') $copy | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Archive copy failed.' }
$env:PYTHONUTF8 = '1'
$env:KNOWLEDGE_DB_PATH = Join-Path $copy 'database.db'
$env:AGENT_CHECKPOINT_PATH = Join-Path $copy 'unused-checkpoints.db'
$env:KNOWLEDGE_VAULT_PATH = Join-Path $copy 'vault'
$env:LLM_PROVIDER = 'mock'
$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:18030'
$services = @(
    @{ name = 'api'; exe = $python; args = '-m uvicorn app.benchmarking.archive_preview:create --factory --host 127.0.0.1 --port 18030'; cwd = $projectRoot },
    @{ name = 'web'; exe = (Get-Command node -ErrorAction Stop).Source; args = ('"{0}" --host 127.0.0.1 --port 15196 --strictPort' -f (Join-Path $projectRoot 'frontend/node_modules/vite/bin/vite.js')); cwd = (Join-Path $projectRoot 'frontend') }
)
$records = @()
foreach ($service in $services) {
    $process = Start-Process -FilePath $service.exe -ArgumentList $service.args -WorkingDirectory $service.cwd `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "$($service.name).out.log") `
        -RedirectStandardError (Join-Path $runtime "$($service.name).err.log")
    $records += [pscustomobject]@{ name = $service.name; id = $process.Id; started = $process.StartTime.ToUniversalTime().ToString('o') }
    ConvertTo-Json -InputObject @($records) | Set-Content -LiteralPath $statePath -Encoding UTF8
}
$deadline = (Get-Date).AddSeconds(40)
do {
    try {
        Invoke-RestMethod 'http://127.0.0.1:18030/health' -TimeoutSec 2 | Out-Null
        Invoke-WebRequest 'http://127.0.0.1:15196/' -UseBasicParsing -TimeoutSec 2 | Out-Null
        Write-Host "Read-only real report: http://127.0.0.1:15196/agent-runs/$($run.id)"
        return
    } catch { Start-Sleep -Milliseconds 300 }
} while ((Get-Date) -lt $deadline)
throw "Preview startup timed out; inspect $runtime logs."
