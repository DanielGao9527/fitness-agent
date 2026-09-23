$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $environmentPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $pythonProbe = 'import sys; sys.exit(1) if sys.version_info[:2] != (3, 12) else None; print(sys.executable)'
    if (-not (Test-Path -LiteralPath $environmentPython)) {
        $basePython = $null
        foreach ($commandName in @('py', 'python3', 'python')) {
            $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $command) { continue }
            $launcherArguments = if ($commandName -eq 'py') { @('-3.12') } else { @() }
            try {
                $detectedPython = & $command.Source @launcherArguments -c $pythonProbe 2>$null
                if ($LASTEXITCODE -eq 0 -and $detectedPython) {
                    $basePython = [string]$detectedPython
                    break
                }
            } catch { continue }
        }
        if (-not $basePython) {
            throw 'Python 3.12 was not found. Install Python 3.12 and enable the py launcher or add Python to PATH, then run setup.ps1 again.'
        }
        & $basePython -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $environmentPython -c $pythonProbe | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The existing .venv must use Python 3.12. Create a Python 3.12 virtual environment before running setup.ps1 again.' }
    $requirements = if (Test-Path -LiteralPath 'requirements.lock.txt') { 'requirements.lock.txt' } else { 'requirements.txt' }
    & $environmentPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    Write-Output 'Ready. Run .\scripts\start.ps1'
} finally {
    Pop-Location
}
