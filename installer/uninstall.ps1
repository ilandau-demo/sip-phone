# Nextiva Companion App uninstaller - run via Uninstall.bat (double-click)
param([switch]$Quiet, [switch]$KeepSettings)
$ErrorActionPreference = 'Continue'
Write-Host ""
Write-Host "=== Nextiva Companion App uninstaller ===" -ForegroundColor Cyan
Write-Host ""

$check = "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
$PY = $null; $PYA = @()
if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -c $check 2>$null; if ($LASTEXITCODE -eq 0) { $PY = 'py.exe'; $PYA = @('-3') } }
if (-not $PY -and (Get-Command python -ErrorAction SilentlyContinue)) { & python -c $check 2>$null; if ($LASTEXITCODE -eq 0) { $PY = 'python.exe' } }
function Invoke-Py { if ($PY) { & $PY @PYA @args } }

# stop it
foreach ($exe in @('sipphone-app', 'sipphone-native-host')) {
    Get-Process $exe -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

# sign-in autostart + Start-menu shortcut
if ($PY) { Invoke-Py -m sipphone --startup off 2>$null | Out-Null }
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {
    foreach ($name in @('Nextiva Companion App.lnk', 'SIP Phone.lnk')) {   # current and pre-2.5.0 name
        $lnk = Join-Path $folder $name
        if (Test-Path $lnk) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue; Write-Host "Removed $lnk" }
    }
}

# browser bridge
if ($PY) { Invoke-Py -m sipphone --unregister-bridge 2>$null | Out-Null }
foreach ($k in @('HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.sipphone.host',
                 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.sipphone.host',
                 'HKCU:\Software\Chromium\NativeMessagingHosts\com.sipphone.host')) {
    Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path $env:LOCALAPPDATA 'SIPPhone') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:USERPROFILE '.sipphone') -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Browser bridge: removed (remove the extension itself at chrome://extensions)"

# extension folder
$extDir = Join-Path $env:USERPROFILE 'SIP Phone Extension'
if (Test-Path $extDir) { Remove-Item $extDir -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "Removed $extDir" }

# the package
if ($PY) {
    Invoke-Py -m pip uninstall -y sipphone 2>$null
    Write-Host "Package: removed (piper-tts / faster-whisper, if installed, are left in place)"
}

# settings, contacts, history, voice
if (-not $KeepSettings) {
    $cfg = $null
    if ($PY) { $cfg = (Invoke-Py -c "from sipphone.cli import config_dir; print(config_dir())" 2>$null) }
    if (-not $cfg) { $cfg = Join-Path $env:APPDATA 'sipphone' }
    $del = $Quiet
    if (-not $Quiet) { $a = Read-Host "Also delete settings, contacts and call history in $cfg ? [y/N]"; $del = ($a -match '^[yY]') }
    if ($del) {
        Remove-Item $cfg -Recurse -Force -ErrorAction SilentlyContinue
        # the Microsoft Store build of Python keeps its own copy of AppData
        Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter 'PythonSoftwareFoundation.Python*' -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item (Join-Path $_.FullName 'LocalCache\Roaming\sipphone') -Recurse -Force -ErrorAction SilentlyContinue }
        Write-Host "Settings removed"
    }
}
Write-Host "`nNextiva Companion App has been removed." -ForegroundColor Green
if (-not $Quiet) { Read-Host "Press Enter to close" | Out-Null }
