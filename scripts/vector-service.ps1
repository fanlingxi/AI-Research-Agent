param(
    [ValidateSet('install', 'start', 'stop', 'restart', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$version = '1.18.0'
$sha256 = 'b69196d0aa1d73ae5488a099360fc958fc2a4d82920a75e1d0975952c0441f6f'
$runtime = Join-Path $projectRoot 'data/runtime/qdrant'
$distribution = Join-Path $projectRoot "data/tools/qdrant/$version"
$executable = Join-Path $distribution 'qdrant.exe'
$statePath = Join-Path $runtime 'process.json'
$configPath = Join-Path $runtime 'config.yaml'
$url = 'http://127.0.0.1:6333'

function Get-ManagedProcess {
    if (!(Test-Path -LiteralPath $statePath)) { return $null }
    $record = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $executable -and
        $process.StartTime.ToUniversalTime().ToString('o') -eq $record.started) {
        return $process
    }
    return $null
}

function Stop-VectorService {
    $process = Get-ManagedProcess
    if ($process) {
        Stop-Process -Id $process.Id -ErrorAction Stop
        $process.WaitForExit(10000) | Out-Null
        if (!$process.HasExited) { throw 'Qdrant did not exit.' }
        Write-Host 'Stopped managed Qdrant. WAL recovery is checked on next start.'
    }
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
}

if ($Action -eq 'install') {
    if (Get-ManagedProcess) { throw 'Stop the managed Qdrant before installing.' }
    if (Test-Path -LiteralPath $executable) {
        Write-Host "Qdrant $version is already installed: $executable"
        return
    }
    New-Item -ItemType Directory -Path $distribution -Force | Out-Null
    $archive = Join-Path $distribution 'qdrant.zip'
    $download = "https://github.com/qdrant/qdrant/releases/download/v$version/qdrant-x86_64-pc-windows-msvc.zip"
    if (!(Test-Path -LiteralPath $archive) -or
        (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sha256) {
        Invoke-WebRequest -Uri $download -OutFile $archive -TimeoutSec 180
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sha256) {
        throw 'Official release SHA256 mismatch; refusing to extract or execute.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $distribution -Force
    if (!(Test-Path -LiteralPath $executable)) { throw 'Release did not contain qdrant.exe.' }
    [pscustomobject]@{
        version = $version; source = $download; sha256 = $sha256
        executable_sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $distribution 'release.json') -Encoding UTF8
    Write-Host "Installed verified Qdrant $version inside the project."
    return
}

if ($Action -eq 'stop' -or $Action -eq 'restart') {
    Stop-VectorService
    if ($Action -eq 'stop') { return }
}
if ($Action -eq 'status') {
    $process = Get-ManagedProcess
    if (!$process) { Write-Host 'No managed Qdrant process.'; return }
    $info = Invoke-RestMethod "$url/" -TimeoutSec 3
    Invoke-RestMethod "$url/healthz" -TimeoutSec 3 | Out-Null
    Write-Host "Qdrant $($info.version): healthy (PID $($process.Id)); $url"
    return
}
if (Get-ManagedProcess) { throw 'Managed Qdrant is already running.' }
if (!(Test-Path -LiteralPath $executable)) {
    throw 'Run scripts/vector-service.ps1 install first, or use the existing Docker Compose service.'
}
$release = Get-Content -LiteralPath (Join-Path $distribution 'release.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.executable_sha256) {
    throw 'Installed executable changed since verification.'
}
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 6333)
try { $probe.Start() }
catch { throw 'Port 6333 is in use. Existing listeners will not be stopped.' }
finally { $probe.Stop() }
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
@'
log_level: INFO
telemetry_disabled: true
storage:
  storage_path: ./storage
  snapshots_path: ./snapshots
service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: null
'@ | Set-Content -LiteralPath $configPath -Encoding UTF8
$process = Start-Process -FilePath $executable -ArgumentList @('--config-path', ('"{0}"' -f $configPath)) `
    -WorkingDirectory $runtime -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $runtime 'stdout.log') `
    -RedirectStandardError (Join-Path $runtime 'stderr.log')
[pscustomobject]@{
    id = $process.Id; started = $process.StartTime.ToUniversalTime().ToString('o'); version = $version
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
$deadline = (Get-Date).AddSeconds(40)
do {
    if (!(Get-ManagedProcess)) { throw "Qdrant exited; inspect $runtime/stderr.log and stdout.log." }
    try {
        Invoke-RestMethod "$url/healthz" -TimeoutSec 2 | Out-Null
        $info = Invoke-RestMethod "$url/" -TimeoutSec 2
        if ($info.version -ne $version) { throw 'Unexpected server version.' }
        Write-Host "Qdrant $version started at $url; storage: $runtime/storage"
        return
    } catch { Start-Sleep -Milliseconds 300 }
} while ((Get-Date) -lt $deadline)
throw 'Qdrant health check timed out; process/logs retained for diagnosis.'
