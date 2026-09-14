param(
    [ValidateRange(1024,65535)][int]$Port = 8000,
    # First-time setup: create the initial platform administrator. Everything
    # else a fresh clone needs - virtual environment, configuration, database -
    # is created on every run anyway; the administrator is the one step that
    # cannot be, because it needs a password only a person can supply.
    [switch]$Install
)
. (Join-Path $PSScriptRoot 'scripts/runtime-common.ps1')

function Get-AdminState {
    $state = (& $PythonExecutable 'scripts/admin_status.py' 2>$null | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0) { return 'unknown' }
    return "$state".Trim()
}

function Install-Administrator {
    if ((Get-AdminState) -eq 'present') {
        Write-Output 'A platform administrator already exists; nothing to install.'
        Write-Output 'Bootstrap stays disabled once one exists - it cannot elevate or reset an account.'
        return
    }
    $envFile = Join-Path $ProjectRoot '.env'
    $identity = ''
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            $entry = $line.Trim()
            if (-not $entry -or $entry.StartsWith('#')) { continue }
            $parts = $entry -split '=', 2
            if ($parts[0].Trim() -eq 'SITE_ADMIN_USER_ID') {
                $identity = $parts[1].Trim().Trim('"', "'")
            }
        }
    }
    if (-not $identity) {
        $identity = (Read-Host 'Choose the administrator sign-in ID').Trim()
        if (-not $identity) { throw 'An administrator sign-in ID is required for -Install.' }
        # .env may legally hold this one key and nothing else, so writing the
        # whole file is not data loss - but only do it when the key is absent.
        Set-Content -LiteralPath $envFile -Encoding utf8 -Value @(
            '# Optional bootstrap identity only. Set this to your chosen sign-in user ID.',
            '# No passwords, API keys, connection strings, or other settings belong here.',
            "SITE_ADMIN_USER_ID=$identity"
        )
        Write-Output "Wrote .env with SITE_ADMIN_USER_ID=$identity"
    }
    # bootstrap_admin prompts for the password itself and reads it hidden, so it
    # never reaches a file, an argument or this console's history.
    & $PythonExecutable 'manage.py' 'bootstrap_admin'
    if ($LASTEXITCODE -ne 0) {
        throw 'Administrator setup did not complete. Nothing was started.'
    }
}
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
    # Kept in a file: cmd.exe strips the quotes out of an inline -c one-liner.
    Invoke-Checked $PythonExecutable @('scripts/assert_development.py')
    Invoke-Checked $PythonExecutable @('manage.py', 'check', '--fail-level', 'WARNING')
    Invoke-Checked $PythonExecutable @('manage.py', 'migrate', '--noinput')
    # After migrate: the user table has to exist before an administrator can.
    if ($Install) { Install-Administrator }
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
    if (-not $Install -and (Get-AdminState) -eq 'missing') {
        Write-Output ''
        Write-Output 'No administrator account exists yet, so nobody can sign in.'
        Write-Output 'Run ./stop-all.ps1, then ./start-all.ps1 -Install to create one.'
        Write-Output ''
    }
    Write-Output 'Stop with ./stop-all.ps1. Logs: .runtime/server-error.log'
} finally {
    Pop-Location
    $operationLock.Dispose()
}
