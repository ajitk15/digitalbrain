param([ValidateRange(1024,65535)][int]$Port = 8000)
. (Join-Path $PSScriptRoot 'scripts/runtime-common.ps1')
$operationLock = Lock-Lifecycle
Push-Location $ProjectRoot
try {
    if (Test-Path -LiteralPath $StateFile) {
        $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
        $existing = Get-OwnedProcess $state
        if ($null -ne $existing) {
            Write-Output "Digital Brain is already running at http://127.0.0.1:$($state.Port)"
            return
        }
        Remove-Item -LiteralPath $StateFile
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } catch { throw "Port $Port is already occupied; choose -Port." }
    finally { $listener.Stop() }
    if (-not (Test-Path -LiteralPath $PythonExecutable)) {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'Install uv first, then run start-all.ps1 again.'
        }
        Invoke-Checked 'uv' @('--cache-dir', '.runtime/uv-cache', 'sync', '--frozen', '--python', '3.12')
    } else {
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            Invoke-Checked 'uv' @('--cache-dir', '.runtime/uv-cache', 'sync', '--frozen', '--python', $PythonExecutable, '--no-managed-python')
        } else {
            Invoke-Checked $PythonExecutable @('-c', 'import django, waitress, axes, PIL, argon2, pypdf, defusedxml, markitdown')
        }
    }
    if (-not $env:DIGITAL_BRAIN_CONFIG -and -not (Test-Path 'config/local.toml')) {
        Invoke-Checked $PythonExecutable @('scripts/init_local.py')
    }
    # Local convenience scripts must not perform production migrations implicitly.
    Invoke-Checked $PythonExecutable @('-c', 'from digitalbrain.configuration import load_config; assert load_config()["mode"] == "development", "Use the production deployment procedure for production."')
    Invoke-Checked $PythonExecutable @('manage.py', 'check', '--fail-level', 'WARNING')
    Invoke-Checked $PythonExecutable @('manage.py', 'migrate', '--noinput')
    Invoke-Checked $PythonExecutable @('manage.py', 'collectstatic', '--noinput')
    $instance = [guid]::NewGuid().ToString()
    $arguments = @(('"' + $ServerScript + '"'), '--port', "$Port", '--instance', $instance)
    $basePython = (& $PythonExecutable -c 'import sys; print(sys._base_executable)').Trim()
    $started = Start-Process -FilePath $PythonExecutable -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $RuntimeDirectory 'server.log') -RedirectStandardError (Join-Path $RuntimeDirectory 'server-error.log') -PassThru
    $state = [ordered]@{ ProjectRoot=$ProjectRoot; ProcessId=$started.Id; StartTicks=$started.StartTime.ToUniversalTime().Ticks.ToString(); Instance=$instance; Port=$Port; BasePython=$basePython }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    $healthy = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        if ($started.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health/ready" -TimeoutSec 1
            if ($health.status -eq 'ready') { $healthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
        $started.Refresh()
    }
    if (-not $healthy) {
        Stop-OwnedProcess ([pscustomobject]$state)
        Remove-Item -LiteralPath $StateFile
        throw 'Startup failed. See .runtime/server-error.log for diagnostic events.'
    }
    Write-Output "Digital Brain is ready: http://127.0.0.1:$Port"
    Write-Output 'Stop with ./stop-all.ps1. Logs: .runtime/server-error.log'
} finally {
    Pop-Location
    $operationLock.Dispose()
}
