param(
    [string]$PythonExe,
    [string]$ShortcutDirectory = [Environment]::GetFolderPath('Desktop')
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonExe) {
    $Candidate = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $Candidate) { $PythonExe = $Candidate }
    elseif ($env:PYTHON_EXE) { $PythonExe = $env:PYTHON_EXE }
    else { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$PythonwExe = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonwExe)) { throw "pythonw.exe not found: $PythonwExe" }
& $PythonExe -c "import sys; assert sys.version_info >= (3,10); import requests, plotly, pystray, PIL"
if ($LASTEXITCODE -ne 0) { throw "Install dependencies first: `"$PythonExe`" -m pip install -r requirements.txt -r requirements-desktop.txt" }
if (-not (Test-Path -LiteralPath $ShortcutDirectory -PathType Container)) { throw 'Shortcut directory does not exist.' }
$Shell = New-Object -ComObject WScript.Shell
$ShortcutPath = Join-Path $ShortcutDirectory 'A-Stock Analysis.lnk'
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonwExe
$Shortcut.Arguments = '"' + (Join-Path $ProjectRoot 'desktop_app.pyw') + '"'
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = (Join-Path $ProjectRoot 'stock_analysis\desktop\assets\dashboard.ico') + ',0'
$Shortcut.Description = 'A-Stock Analysis - browser dashboard and system tray'
$Shortcut.Save()
Write-Output $ShortcutPath
