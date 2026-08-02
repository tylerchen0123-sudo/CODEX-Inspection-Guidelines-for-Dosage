[CmdletBinding()]
param(
    [ValidateSet("User", "Project")]
    [string] $Scope = "User",
    [switch] $DryRun
)

$monitorRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Scope -eq "User") {
    $target = Join-Path $env:USERPROFILE ".codex\hooks.json"
}
else {
    $target = Join-Path (Get-Location) ".codex\hooks.json"
}

$python = if ($env:CODEX_PYTHON) { $env:CODEX_PYTHON } else { "python" }
$args = @((Join-Path $monitorRoot "scripts\install_hooks.py"), "--target", $target)
if ($DryRun) { $args += "--dry-run" }
& $python @args
exit $LASTEXITCODE
