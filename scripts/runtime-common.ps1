Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$RuntimeDirectory = Join-Path $ProjectRoot '.runtime'
$StateFile = Join-Path $RuntimeDirectory 'services.json'
$LifecycleLock = Join-Path $RuntimeDirectory 'lifecycle.lock'
$PythonExecutable = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$ServerScript = Join-Path $ProjectRoot 'scripts/serve.py'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Executable" }
}

function Lock-Lifecycle {
    [IO.Directory]::CreateDirectory($RuntimeDirectory) | Out-Null
    try {
        return [IO.File]::Open($LifecycleLock, 'OpenOrCreate', 'ReadWrite', 'None')
    } catch {
        throw 'Another start/stop operation is in progress. Try again after it finishes.'
    }
}

function Get-OwnedProcess {
    param($State)
    if ([string]$State.ProjectRoot -ne $ProjectRoot) { throw 'Runtime state belongs to another project.' }
    $process = Get-Process -Id ([int]$State.ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    # Creation timestamp defeats PID reuse. Absolute executable and command line prove ownership.
    if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne [string]$State.StartTicks) {
        throw 'Saved PID has been reused; refusing to stop or adopt an unrelated process.'
    }
    if ($process.Path -ne $PythonExecutable) { throw 'Executable mismatch; refusing process action.' }
    $details = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
    if (-not $details.CommandLine.Contains($ServerScript) -or
        -not $details.CommandLine.Contains([string]$State.Instance)) {
        throw 'Command-line ownership could not be verified; refusing process action.'
    }
    return $process
}

function Stop-ConverterChildren {
    param([int]$ServiceProcessId, $State)
    $extractScript = Join-Path $ProjectRoot 'scripts/extract_text.py'
    $originalsRoot = Join-Path $ProjectRoot '.runtime'
    $descendants = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ServiceProcessId"
    foreach ($descendant in $descendants) {
        if ($descendant.ExecutablePath -eq (Join-Path ([Environment]::SystemDirectory) 'conhost.exe')) { continue }
        if (($descendant.ExecutablePath -eq $State.BasePython -or $descendant.ExecutablePath -eq $PythonExecutable) -and
            $descendant.CommandLine.Contains($extractScript) -and
            $descendant.CommandLine.Contains($originalsRoot)) {
            Stop-ConverterChildren -ServiceProcessId $descendant.ProcessId -State $State
            $result = Invoke-CimMethod -InputObject $descendant -MethodName Terminate
            if ($result.ReturnValue -ne 0 -and (Get-Process -Id $descendant.ProcessId -ErrorAction SilentlyContinue)) {
                throw 'Could not stop the verified document converter.'
            }
        } elseif ($descendant.ExecutablePath -eq (Join-Path $ProjectRoot '.venv/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe') -and
            (($descendant.CommandLine.Contains('--no-session-persistence') -and
              $descendant.CommandLine.Contains('--strict-mcp-config') -and
              $descendant.CommandLine.Contains('--tools')) -or
             $descendant.CommandLine.Contains('--version'))) {
            # Only the bundled, text-only SDK process descending from our verified worker.
            Stop-ConverterChildren -ServiceProcessId $descendant.ProcessId -State $State
            $result = Invoke-CimMethod -InputObject $descendant -MethodName Terminate
            if ($result.ReturnValue -ne 0 -and (Get-Process -Id $descendant.ProcessId -ErrorAction SilentlyContinue)) {
                throw 'Could not stop the verified Claude SDK process.'
            }
        } else {
            throw 'Unexpected converter child; refusing to terminate it.' 
        }
    }
}

function Stop-OwnedProcess {
    param($State)
    $owned = Get-OwnedProcess $State
    if ($null -eq $owned) { return }
    # Windows venv launchers may own a base-Python child; verify both before terminating.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($owned.Id)"
    foreach ($child in $children) {
        if ($child.ExecutablePath -eq (Join-Path ([Environment]::SystemDirectory) 'conhost.exe')) { continue }
        if ($child.CommandLine.Contains($ServerScript) -and
            $child.CommandLine.Contains([string]$State.Instance) -and
            ($child.ExecutablePath -eq $State.BasePython -or $child.ExecutablePath -eq $PythonExecutable)) {
            Stop-ConverterChildren -ServiceProcessId $child.ProcessId -State $State
            $result = Invoke-CimMethod -InputObject $child -MethodName Terminate
            if ($result.ReturnValue -ne 0) { throw 'Could not stop the verified Python worker.' }
        } else {
            throw 'Unexpected child process; refusing to terminate it.'
        }
    }
    $parentDetails = Get-CimInstance Win32_Process -Filter "ProcessId=$($owned.Id)"
    if ($null -ne $parentDetails) {
        $result = Invoke-CimMethod -InputObject $parentDetails -MethodName Terminate
        if ($result.ReturnValue -ne 0) { throw 'Could not stop the verified server launcher.' }
    }
}
