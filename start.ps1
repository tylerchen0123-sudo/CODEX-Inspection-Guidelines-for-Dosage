[CmdletBinding()]
param(
    [int] $Port = 8765,
    [switch] $NoBrowser
)

$monitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $monitorRoot "data"
$pidFile = Join-Path $dataDir "dashboard.pid"
$stdoutLog = Join-Path $dataDir "dashboard.stdout.log"
$stderrLog = Join-Path $dataDir "dashboard.stderr.log"
$url = "http://127.0.0.1:$Port/"

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

if ($env:CODEX_PYTHON) {
    $python = $env:CODEX_PYTHON
}
else {
    $bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { "python" }
}

try {
    $null = Invoke-WebRequest -Uri ($url + "api/summary") -UseBasicParsing -TimeoutSec 5
    if (-not $NoBrowser) { Start-Process $url }
    Write-Output "Dashboard already running: $url"
    exit 0
}
catch {
    # No listener yet; start one below.
}

& $python (Join-Path $monitorRoot "cost_monitor.py") init | Out-Host
$arguments = @(
    (Join-Path $monitorRoot "cost_monitor.py"),
    "dashboard",
    "--host", "127.0.0.1",
    "--port", $Port
)
$server = Start-Process -FilePath $python -ArgumentList $arguments `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
Set-Content -LiteralPath $pidFile -Value $server.Id -Encoding ascii

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 150
    try {
        $null = Invoke-WebRequest -Uri ($url + "api/summary") -UseBasicParsing -TimeoutSec 5
        $ready = $true
        break
    }
    catch {
        # Keep polling until the local server is ready.
    }
}

if (-not $ready) {
    Write-Error "Dashboard did not start. See $stderrLog"
    exit 1
}

if (-not $NoBrowser) { Start-Process $url }
Write-Output "Dashboard started: $url"
Write-Output "Stop it with: $monitorRoot\stop.ps1"
