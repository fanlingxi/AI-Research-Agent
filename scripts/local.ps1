param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $projectRoot 'data/runtime/local'
$statePath = Join-Path $runtimeDirectory 'processes.json'

function Get-ManagedProcess($record) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $record.started) {
        return $process
    }
}

function Stop-ManagedProcesses($records) {
    foreach ($record in $records) {
        if (Get-ManagedProcess $record) {
            # Include Python's Windows venv launcher child; validate PID reuse first.
            & taskkill.exe /PID $record.id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop $($record.name)." }
            Write-Host "Stopped $($record.name)."
        }
    }
}

$records = @()
if (Test-Path -LiteralPath $statePath) {
    $records = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}
if ($Action -eq 'stop') {
    Stop-ManagedProcesses $records
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
    return
}
if ($Action -eq 'status') {
    foreach ($record in $records) {
        $status = if (Get-ManagedProcess $record) { 'running' } else { 'stopped' }
        Write-Host "$($record.name): $status (PID $($record.id))"
    }
    if ($records.Count -eq 0) { Write-Host 'No managed local services.' }
    return
}
if (@($records | Where-Object { Get-ManagedProcess $_ }).Count -gt 0) {
    throw 'Local services are already running. Use scripts/local.ps1 status or stop first.'
}

$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$vite = Join-Path $projectRoot 'frontend/node_modules/vite/bin/vite.js'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $vite)) {
    throw 'Install Python and frontend dependencies first; see docs/LOCAL_DEVELOPMENT.md.'
}
$node = (Get-Command node -ErrorAction Stop).Source
foreach ($port in @(8000, 5173, 8501)) {
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $probe.Start() }
    catch { throw "Port $port is unavailable. Stop the existing listener before starting." }
    finally { $probe.Stop() }
}
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$environmentFile = Join-Path $projectRoot '.env'
if (!(Test-Path -LiteralPath $environmentFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $environmentFile
}

$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:AI_RESEARCH_API_URL = 'http://127.0.0.1:8000'
$env:AI_RESEARCH_REACT_URL = 'http://127.0.0.1:5173'
$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:8000'
$env:VITE_OPERATIONS_URL = 'http://127.0.0.1:8501'
$services = @(
    @{ name = 'api'; executable = $python; arguments = '-m uvicorn app.api.main:create_app --factory --host 127.0.0.1 --port 8000'; directory = $projectRoot },
    @{ name = 'worker'; executable = $python; arguments = '-m app.worker'; directory = $projectRoot },
    @{ name = 'web'; executable = $node; arguments = ('"{0}" --host 127.0.0.1 --port 5173 --strictPort' -f $vite); directory = (Join-Path $projectRoot 'frontend') },
    @{ name = 'operations'; executable = $python; arguments = '-m streamlit run app/ui/streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false'; directory = $projectRoot }
)
$records = @()
try {
    foreach ($service in $services) {
        $process = Start-Process -FilePath $service.executable -ArgumentList $service.arguments `
            -WorkingDirectory $service.directory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $runtimeDirectory "$($service.name).out.log") `
            -RedirectStandardError (Join-Path $runtimeDirectory "$($service.name).err.log")
        $records += [pscustomobject]@{
            name = $service.name
            id = $process.Id
            started = $process.StartTime.ToUniversalTime().ToString('o')
        }
        ConvertTo-Json -InputObject @($records) | Set-Content -LiteralPath $statePath -Encoding UTF8
        # API initializes the schema before the independent worker opens it.
        if ($service.name -eq 'api') {
            $deadline = (Get-Date).AddSeconds(45)
            do {
                if (!(Get-ManagedProcess $records[-1])) { throw 'API exited during startup.' }
                try {
                    $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 3
                    if ($health.status -eq 'ok') { break }
                } catch { }
                Start-Sleep -Milliseconds 500
            } while ((Get-Date) -lt $deadline)
            if (!$health -or $health.status -ne 'ok') { throw 'API startup timed out.' }
        }
    }
    $deadline = (Get-Date).AddSeconds(45)
    do {
        foreach ($record in $records) {
            if (!(Get-ManagedProcess $record)) { throw "$($record.name) exited during startup." }
        }
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 3
            $web = Invoke-WebRequest 'http://127.0.0.1:5173' -UseBasicParsing -TimeoutSec 3
            $operations = Invoke-WebRequest 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 3
            if ($health.services.worker.available -and $web.StatusCode -eq 200 -and $operations.StatusCode -eq 200) {
                Write-Host 'Workspace:  http://127.0.0.1:5173'
                Write-Host 'API docs:   http://127.0.0.1:8000/docs'
                Write-Host 'Operations: http://127.0.0.1:8501'
                Write-Host "Logs: $runtimeDirectory"
                if (!$health.knowledge.live_llm_configured) { Write-Warning 'Live LLM is not configured. Extraction and research reports require .env configuration.' }
                foreach ($name in @('qdrant', 'neo4j')) {
                    if (!$health.services.$name.available) { Write-Warning "$name is offline; external retrieval/projection is unavailable." }
                }
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw 'Services did not become ready before the startup deadline.'
} catch {
    Stop-ManagedProcesses $records
    throw "Local startup failed. See $runtimeDirectory. $($_.Exception.Message)"
}
