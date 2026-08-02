[CmdletBinding()]
param()

$monitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $monitorRoot "data\dashboard.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "Dashboard is not running."
    exit 0
}

$dashboardPid = 0
[int]::TryParse((Get-Content -Raw -LiteralPath $pidFile), [ref]$dashboardPid) | Out-Null
if ($dashboardPid -gt 0) {
    $process = Get-Process -Id $dashboardPid -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $dashboardPid
        Write-Output "Stopped dashboard process $dashboardPid."
    }
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
