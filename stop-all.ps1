. (Join-Path $PSScriptRoot 'scripts/runtime-common.ps1')
$operationLock = Lock-Lifecycle
try {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        Write-Output 'No project-managed services are running.'
        return
    }
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    $owned = Get-OwnedProcess $state
    if ($null -ne $owned) {
        # Windows termination is immediate; use a process supervisor for production draining.
        Stop-OwnedProcess $state
    }
    Remove-Item -LiteralPath $StateFile
    Write-Output 'Digital Brain stopped. Database, files, and other services were preserved.'
} finally {
    $operationLock.Dispose()
}
