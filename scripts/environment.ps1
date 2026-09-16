<#
Prerequisite installation for a machine that has never run this project.

Dot-sourced by start-all.ps1, after runtime-common.ps1. Everything here is
user-scoped: nothing is installed machine-wide, no PATH is written to the
registry, and no administrator elevation is requested.

Dependency management is uv + pyproject.toml + uv.lock; there is no
requirements.txt. uv is preferred because `uv sync --frozen` installs the exact
locked versions. The pip path exists only for machines that cannot reach
astral.sh - it resolves fresh, so it can pick up versions the lock file never
pinned, and it says so out loud.
#>

function Find-Uv {
    <#
    .SYNOPSIS
    Locate uv, teaching the current process about a just-installed copy.
    #>
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    # Both installers put uv somewhere this already-running process does not have
    # on PATH, so a fresh install is invisible until the session is told about it.
    $candidates = @()
    if ($env:USERPROFILE) { $candidates += (Join-Path $env:USERPROFILE '.local\bin\uv.exe') }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
        $candidates += (Join-Path $env:LOCALAPPDATA 'uv\bin\uv.exe')
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $env:PATH = (Split-Path $candidate -Parent) + ';' + $env:PATH
            return $candidate
        }
    }
    return $null
}

function Install-Uv {
    # Write-Host, not Write-Output, throughout this function. A PowerShell function
    # returns everything written to the output stream, so a progress message here
    # would be concatenated with the path this returns - and `$uv = Install-Uv`
    # would come back truthy even when no uv was installed, skipping the pip
    # fallback and then trying to run a sentence as a program.
    Write-Host 'Installing uv, the Python package manager this project uses...'
    # Exit codes are not trusted here. winget reports failure for conditions that
    # are fine for us (already installed, no applicable upgrade), and the official
    # installer reports success before its PATH change is visible. Whether uv is
    # actually usable is decided by finding the executable, not by a status code.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id astral-sh.uv -e --source winget `
                --accept-source-agreements --accept-package-agreements 2>&1 | Out-String | Write-Verbose
        } catch {
            Write-Verbose "winget did not complete: $($_.Exception.Message)"
        }
        $found = Find-Uv
        if ($found) { Write-Host "uv installed: $found"; return $found }
    }
    try {
        # Run in a child process: the vendor script is not written against
        # Set-StrictMode -Version Latest and must not inherit this session's rules.
        powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
            'irm https://astral.sh/uv/install.ps1 | iex' 2>&1 | Out-String | Write-Verbose
    } catch {
        Write-Verbose "The uv installer did not complete: $($_.Exception.Message)"
    }
    $found = Find-Uv
    if ($found) { Write-Host "uv installed: $found" } else { Write-Warning 'uv could not be installed automatically.' }
    return $found
}

function Find-HostPython {
    <#
    .SYNOPSIS
    An interpreter matching pyproject.toml's requires-python (>=3.12,<3.15).
    Only needed on the pip path; uv fetches its own interpreter.
    #>
    # `--version`, not `-c`. An inline -c probe has to carry quotes and a percent
    # sign, and PowerShell's native argument passing mangles both - the same
    # quoting trap documented in scripts/assert_development.py, which silently
    # made every candidate look broken. `python --version` prints "Python 3.13.7"
    # and needs no quoting at all.
    $probe = @('--version')
    # Probing is expected to fail for most candidates, and Windows PowerShell
    # turns a native command's stderr into an ErrorRecord. Under this project's
    # $ErrorActionPreference = 'Stop', `py -3.12` printing "No suitable Python
    # runtime found" would end the whole script instead of moving to the next
    # candidate - so failures are made non-terminating just for this loop.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($candidate in @(
                @('py', @('-3.12')), @('py', @('-3.13')), @('py', @('-3.14')),
                @('python', @()), @('python3', @()))) {
            $executable, $prefix = $candidate[0], $candidate[1]
            if (-not (Get-Command $executable -ErrorAction SilentlyContinue)) { continue }
            $global:LASTEXITCODE = 0
            try {
                $reported = (& $executable @($prefix + $probe) 2>$null | Select-Object -Last 1)
            } catch { continue }
            if ($LASTEXITCODE -ne 0) { continue }
            $match = [regex]::Match("$reported", '(\d+)\.(\d+)\.?(\d*)')
            if (-not $match.Success) { continue }
            $major, $minor = [int]$match.Groups[1].Value, [int]$match.Groups[2].Value
            if ($major -eq 3 -and $minor -ge 12 -and $minor -lt 15) {
                return @{ Executable = $executable; Prefix = $prefix; Version = $match.Value }
            }
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $null
}

function Install-WithPip {
    $python = Find-HostPython
    if ($null -eq $python) {
        throw @'
No usable Python found and uv could not be installed.

Install either one, then run this script again:
  winget install --id astral-sh.uv -e
  winget install --id Python.Python.3.12 -e

This project needs Python 3.12, 3.13 or 3.14 (see pyproject.toml).
'@
    }
    Write-Warning 'Installing with pip because uv is unavailable. Versions are resolved fresh rather than taken from uv.lock; install uv for a reproducible environment.'
    Write-Output "Creating the virtual environment with Python $($python.Version)..."
    Invoke-Checked $python.Executable @($python.Prefix + @('-m', 'venv', '.venv'))
    Invoke-Checked $PythonExecutable @('-m', 'pip', 'install', '--upgrade', 'pip')
    Install-Dependencies
}

function Install-Dependencies {
    <#
    .SYNOPSIS
    pip-install the dependencies declared in pyproject.toml.

    Not `pip install -e .`: this project is an application, not a distributable
    package. It declares no [build-system], so setuptools falls back to
    auto-discovery, finds several top-level directories and refuses to guess -
    the install fails before a single dependency is fetched. uv is unaffected
    because it treats the project as non-packaged.
    #>
    Invoke-Checked $PythonExecutable @('scripts/export_requirements.py')
    Invoke-Checked $PythonExecutable @('-m', 'pip', 'install', '-r', '.runtime/requirements.txt')
}

function Initialize-Environment {
    <#
    .SYNOPSIS
    Bring the virtual environment up to date, installing prerequisites when asked.

    .PARAMETER AllowInstall
    Set by -Install. Without it the script never downloads a toolchain; it reports
    what is missing and points at -Install, so an ordinary start stays predictable.
    #>
    param([bool]$AllowInstall)

    $uv = Find-Uv
    if (-not $uv -and $AllowInstall) { $uv = Install-Uv }

    if (Test-Path -LiteralPath $PythonExecutable) {
        if ($uv) {
            Invoke-Checked $uv @('--cache-dir', '.runtime/uv-cache', 'sync', '--frozen',
                '--python', $PythonExecutable, '--no-managed-python')
            return
        }
        if ($AllowInstall) { Install-Dependencies }
    } elseif ($uv) {
        Invoke-Checked $uv @('--cache-dir', '.runtime/uv-cache', 'sync', '--frozen', '--python', '3.12')
        return
    } elseif ($AllowInstall) {
        Install-WithPip
    } else {
        # start-all.ps1 turns a missing .venv into a first run, so this is only
        # reached by a caller that passed -AllowInstall:$false deliberately. It
        # still names the .cmd: Windows blocks .ps1 files out of the box.
        throw 'This project is not set up yet. Run:  start-all.cmd -Install'
    }

    # Reached only when uv did not do the install, so nothing guarantees the
    # environment is complete. Importing every hard dependency is the check: a
    # half-installed venv otherwise passes startup and fails later, at chat time.
    Invoke-Checked $PythonExecutable @('-c',
        'import django, waitress, axes, PIL, argon2, pypdf, defusedxml, markitdown, agents, claude_agent_sdk')
}
