param(
    [string]$PythonPath = "",
    [string]$ConfigPath = "",
    [string]$DataDirectory = "",
    [ValidateRange(1,65535)][int]$Port = 8787,
    [string]$ShortcutDirectory = ""
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
$resolvedPython = (& $PythonPath -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot find the Python runtime.' }
$pythonw = Join-Path (Split-Path -Parent $resolvedPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is missing. Install Python for Windows first.' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $projectRoot 'config.toml' }
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
if (-not $DataDirectory) { $DataDirectory = Join-Path (Split-Path -Parent $ConfigPath) 'data' }
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
if (-not $ShortcutDirectory) { $ShortcutDirectory = [Environment]::GetFolderPath('DesktopDirectory') }
if (-not (Test-Path -LiteralPath $ShortcutDirectory -PathType Container)) { throw 'Desktop folder does not exist.' }
$bootstrap = Join-Path $projectRoot 'tools\desktop_launcher.pyw'
# Windows paths cannot contain a double quote; validate explicitly before argument composition.
foreach ($value in @($bootstrap,$ConfigPath,$DataDirectory)) {
    if ($value.Contains('"')) { throw 'Invalid quote in a path.' }
}
$linkPath = Join-Path $ShortcutDirectory '雨课堂习题看板.lnk'
$wsh = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $linkPath) {
    $existing = $wsh.CreateShortcut($linkPath)
    if ($existing.Arguments -notlike '*desktop_launcher.pyw*') { throw 'A different shortcut already uses this name; it was not overwritten.' }
}
$shortcut = $wsh.CreateShortcut($linkPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $bootstrap + '" --config "' + $ConfigPath + '" --data-dir "' + $DataDirectory + '" --port ' + $Port
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = '雨课堂习题看板：无黑框启动；最后一个网页关闭 3 分钟后退出'
$shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',14'
$shortcut.Save()
Write-Output "Created: $linkPath"
Write-Output 'Uses pythonw.exe (no console). Re-run this script after moving the project or Python runtime.'
