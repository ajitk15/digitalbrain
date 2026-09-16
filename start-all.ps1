param(
    [ValidateRange(1024,65535)][int]$Port = 8000,
    # First-run setup for a machine that has never run this project: install uv
    # (or fall back to pip), build the virtual environment, create the initial
    # platform administrator, and choose an AI provider. An ordinary start keeps
    # an existing environment up to date but never downloads a toolchain, so what
    # a normal run does stays predictable.
    [switch]$Install,
    # Re-run only the AI provider question, without touching anything else.
    [switch]$ConfigureAI
)
. (Join-Path $PSScriptRoot 'scripts/runtime-common.ps1')
. (Join-Path $PSScriptRoot 'scripts/environment.ps1')

function Get-AdminState {
    # Anything this writes to stderr is diagnostic, not a reason to stop: the
    # answer is simply 'unknown'. Windows PowerShell would otherwise turn that
    # output into a terminating error under $ErrorActionPreference = 'Stop'.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $state = (& $PythonExecutable 'scripts/admin_status.py' 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0) { return 'unknown' }
        return "$state".Trim()
    } catch {
        return 'unknown'
    } finally {
        $ErrorActionPreference = $previous
    }
}

#: Offered when the person just presses Enter at the sign-in prompt.
$DefaultAdministrator = 'siteadmin@db.com'

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
        # Offered, not imposed: pressing Enter is the common case and should not
        # require inventing an identifier before the project has ever started.
        $identity = (Read-Host "Administrator sign-in ID [$DefaultAdministrator]").Trim()
        if (-not $identity) { $identity = $DefaultAdministrator }
        # .env may legally hold this one key and nothing else, so writing the
        # whole file is not data loss - but only do it when the key is absent.
        Set-Content -LiteralPath $envFile -Encoding utf8 -Value @(
            '# Optional bootstrap identity only. Set this to your chosen sign-in user ID.',
            '# No passwords, API keys, connection strings, or other settings belong here.',
            "SITE_ADMIN_USER_ID=$identity"
        )
        Write-Output "Wrote .env with SITE_ADMIN_USER_ID=$identity"
    }
    Write-Output ''
    Write-Output "Choose a password for $identity. It is read hidden and never stored"
    Write-Output 'in a file, an argument or this window''s history.'
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
    # A run with no virtual environment is not the "ordinary start" that is meant
    # to stay predictable - it is a first run, and the only thing it can do is
    # fail. Windows ships with LocalMachine set to Restricted, so the advice to
    # "run ./start-all.ps1 -Install" named a command the machine refuses to
    # execute; the person is then stuck with no working path at all. Detecting
    # the state removes the dead end, so double-clicking start-all.cmd on a new
    # laptop sets the project up instead of explaining why it cannot.
    if (-not $Install -and -not (Test-Path -LiteralPath $PythonExecutable)) {
        Write-Output 'No virtual environment here yet, so this is a first run.'
        Write-Output 'Setting up: install uv, build .venv, create the database, then ask you'
        Write-Output 'for an administrator sign-in and which AI provider to use.'
        Write-Output ''
        $Install = [switch]$true
    }
    Initialize-Environment -AllowInstall ($Install.IsPresent)
    # Run on every start, not only when the file is absent: config/local.toml
    # records an absolute secret_directory, so a project folder that was moved or
    # copied would otherwise keep pointing at the old machine's secrets - or fail
    # with an error that names the missing signing key and not the real cause.
    if (-not $env:DIGITAL_BRAIN_CONFIG) {
        Invoke-Checked $PythonExecutable @('scripts/init_local.py')
    }
    # Local convenience scripts must not perform production migrations implicitly.
    # Kept in a file: cmd.exe strips the quotes out of an inline -c one-liner.
    Invoke-Checked $PythonExecutable @('scripts/assert_development.py')
    Invoke-Checked $PythonExecutable @('manage.py', 'check', '--fail-level', 'WARNING')
    Invoke-Checked $PythonExecutable @('manage.py', 'migrate', '--noinput')
    # After migrate: the user table has to exist before an administrator can.
    if ($Install) { Install-Administrator }
    if ($Install -or $ConfigureAI) {
        # Unlike the administrator, an AI provider is optional - the platform runs
        # without one. A person who declines or mistypes should land on a working
        # site with a warning, not on a failed start.
        & $PythonExecutable 'scripts/ai_setup.py'
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'AI provider setup did not complete. Re-run with -ConfigureAI to try again.'
        }
    }
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
        Write-Output 'Run  stop-all.cmd  then  start-all.cmd -Install  to finish setting up.'
        Write-Output ''
    }
    Write-Output 'Stop with ./stop-all.ps1. Logs: .runtime/server-error.log'
} finally {
    Pop-Location
    $operationLock.Dispose()
}
