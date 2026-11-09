# Telvoro installer - run via Install.bat (double-click), or from PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Quiet] [-NoTTS] [-STT] [-NoExtension]
#                                                        [-NoStartup] [-Mode phone|presence]
#                                                        [-UpdateSource <folder, share or https URL>]
# -Quiet answers every question with its default (TTS yes, STT no, extension
# folder yes, start at sign-in yes) - for pushing the app to many PCs.
param(
    [switch]$Quiet,
    [switch]$NoTTS,
    [switch]$STT,
    [switch]$NoExtension,
    [switch]$NoStartup,
    [ValidateSet('', 'phone', 'presence')][string]$Mode = '',
    [string]$UpdateSource = ''
)
$ErrorActionPreference = 'Continue'
# Everything shown here is also written to install.log next to this script.
try { Start-Transcript -Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'install.log') -Force | Out-Null } catch { }
trap {
    Write-Host ""
    Write-Host "The installer hit an error it couldn't recover from:" -ForegroundColor Red
    Write-Host ("  " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ("  at " + $_.InvocationInfo.PositionMessage)
    Write-Host "Send install.log (next to Install.bat) to get this fixed."
    try { Stop-Transcript | Out-Null } catch { }
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}
function Step($t) { Write-Host ""; Write-Host ("--- " + $t) -ForegroundColor DarkCyan }

# Chrome/Edge extension IDs allowed to talk to the desktop app. The first is the
# fixed ID of the "Load unpacked" build; after publishing to the Chrome Web Store
# or Edge Add-ons, add those store IDs here and re-share the bundle.
$ExtensionIds = @('ekooaigkfmobhjmioleeahcanjnhhheh', 'kghndilnhefammdeabhnjlphlmeeomno')
# Once the extension is published on the Chrome Web Store, put its store link
# here. The installer then opens that page (one-click install, auto-updates)
# instead of the drag-the-zip flow, and tells the app the store copy's ID.
$StoreUrl = 'https://chromewebstore.google.com/detail/kghndilnhefammdeabhnjlphlmeeomno'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeKeys = @('HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.sipphone.host',
                'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.sipphone.host')

function Ask($question, $default) {
    # Returns $true/$false. In -Quiet mode the default wins.
    if ($Quiet) { return $default }
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$question $hint"
    if ($a -eq '') { return $default }
    return ($a -match '^[yY]')
}
function Pause-Exit($code) {
    try { Stop-Transcript | Out-Null } catch { }
    if (-not $Quiet) { Read-Host "Press Enter to close" | Out-Null }
    exit $code
}

Write-Host ""
Write-Host "=== Telvoro installer ===" -ForegroundColor Cyan
Write-Host ("PowerShell " + $PSVersionTable.PSVersion + "  -  " + (Get-Date))
Write-Host ("Folder: " + $here)

Step "0. Checking the files"
# 0. Is this the extracted folder? (double-clicking inside the zip gives a lone temp copy)
$whl = Get-ChildItem $here -Filter 'sipphone-*.whl' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if (-not $whl) {
    Write-Host "The Telvoro files aren't next to this installer." -ForegroundColor Red
    if ($here -like '*\Temp\*') {
        Write-Host "It looks like Install.bat was run from inside the .zip. Right-click the zip, choose"
        Write-Host "'Extract All...', open the extracted folder, then double-click Install.bat there."
    } else {
        Write-Host "Keep Install.bat, install.ps1 and sipphone-*.whl together in one folder."
    }
    Pause-Exit 1
}

Step "1. Looking for Python"
# 1. Find Python 3.9+ (py launcher, python, or the usual install folders)
$check = "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
function Find-Python {
    $script:PY = $null; $script:PYA = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c $check 2>$null
        if ($LASTEXITCODE -eq 0) { $script:PY = 'py.exe'; $script:PYA = @('-3'); return $true }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c $check 2>$null
        if ($LASTEXITCODE -eq 0) { $script:PY = 'python.exe'; return $true }
    }
    $cands = @()
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")) {
        if ($root -and (Test-Path $root)) {
            $cands += Get-ChildItem $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                      Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' }
        }
    }
    foreach ($c in $cands) {
        if (Test-Path $c) {
            & $c -c $check 2>$null
            if ($LASTEXITCODE -eq 0) { $script:PY = $c; return $true }
        }
    }
    return $false
}
if (-not (Find-Python)) {
    Write-Host "Python 3.9 or newer is needed and wasn't found." -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and (Ask "Install Python now (from python.org, via winget; about 100 MB)?" $true)) {
        Write-Host "Installing Python 3.12 ..."
        & winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        # the new PATH isn't in this window yet
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
        if (-not (Find-Python)) {
            Write-Host "Python was installed but can't be used from here yet. Close this window and run Install.bat again." -ForegroundColor Yellow
            Pause-Exit 1
        }
    } else {
        Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
        Write-Host "or from the Microsoft Store (search 'Python 3.12'), then run Install.bat again."
        Pause-Exit 1
    }
}
function Invoke-Py { & $PY @PYA @args }
Write-Host ("Using " + (Invoke-Py --version) + " ($PY)")

Step "2. Stopping a running copy"
# 2. Make room: tell a running copy (and the browser bridge) to step aside.
#    The bridge registration is removed first so Chrome doesn't restart the old
#    bridge while pip is replacing it; it is registered again in step 6.
foreach ($k in $BridgeKeys) { Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue }
Invoke-Py -c "import sipphone" 2>$null
$had = ($LASTEXITCODE -eq 0)
if ($had) { Invoke-Py -m sipphone --prepare-update 2>$null }
foreach ($exe in @('sipphone-app', 'sipphone-native-host')) {
    Get-Process $exe -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

Step "3. Installing the app"
# 3. Install the app (a few tries, in case something still holds a file)
Write-Host "`nInstalling $($whl.Name) ..."
$ok = $false
for ($i = 1; $i -le 4 -and -not $ok; $i++) {
    Invoke-Py -m pip install --upgrade --disable-pip-version-check --no-warn-script-location "$($whl.FullName)"
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host "Install attempt $i failed; retrying ..." -ForegroundColor Yellow
    foreach ($exe in @('sipphone-app', 'sipphone-native-host')) {
        Get-Process $exe -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}
if (-not $ok) {
    Write-Host "Install failed (see above). Close Chrome and Edge, then run Install.bat again." -ForegroundColor Red
    Pause-Exit 1
}
Invoke-Py -m sipphone --finish-update 2>$null | Out-Null

Step "4. Optional features"
# 4. Optional features
if (-not $NoTTS -and (Ask "`nInstall text-to-speech (type text, the caller hears it; ~100 MB)?" $true)) {
    Invoke-Py -m pip install --upgrade --disable-pip-version-check --no-warn-script-location piper-tts
}
if ($STT -or (Ask "Install speech-to-text (see what the caller says; ~250 MB)?" $false)) {
    Invoke-Py -m pip install --upgrade --disable-pip-version-check --no-warn-script-location faster-whisper
}

Step "5. Settings, voice, Start-menu shortcut"
# 5. Config, voice, Start-menu shortcut (+ presets for team roll-outs)
Write-Host ""
Invoke-Py -m sipphone --setup
$sets = @()
if ($Mode) { $sets += "--set"; $sets += "app.mode=$Mode" }
if ($UpdateSource) { $sets += "--set"; $sets += "update.source=$UpdateSource" }
if ($sets.Count) { Invoke-Py -m sipphone @sets }

Step "6. Browser bridge"
# 6. Let the Chrome/Edge extension talk to the app (native messaging host).
#    The app does this itself (and again on every start); PowerShell only steps
#    in for a Microsoft Store Python older than 3.11, which can't write the registry.
Invoke-Py -m sipphone --register-bridge
if ($LASTEXITCODE -ne 0) {
    try {
        $info = (Invoke-Py -m sipphone --native-host-info) | ConvertFrom-Json
        if (-not (Test-Path $info.path)) { throw "bridge program not found at $($info.path)" }
        $ids = @($ExtensionIds + $info.extension_ids | Select-Object -Unique)
        $dir = Join-Path $env:LOCALAPPDATA 'SIPPhone'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $manifest = Join-Path $dir 'com.sipphone.host.json'
        $origins = @($ids | ForEach-Object { "chrome-extension://$_/" })
        [ordered]@{ name = $info.name; description = 'Telvoro bridge'; path = $info.path;
                    type = 'stdio'; allowed_origins = $origins } | ConvertTo-Json | Set-Content -Encoding ascii $manifest
        foreach ($k in $BridgeKeys) {
            New-Item -Force -Path $k | Out-Null
            Set-ItemProperty -Path $k -Name '(default)' -Value $manifest
        }
        Write-Host "Chrome/Edge extension bridge: registered (by the installer)"
    } catch {
        Write-Host "Chrome/Edge extension bridge: skipped ($_)" -ForegroundColor Yellow
    }
}

Step "7. Browser extension"
# 7. Browser extension: a zip to drop onto the browser's extensions page (and a
#    folder for "Load unpacked"). Both are kept current by the app from now on.
$extDir = Join-Path $env:USERPROFILE 'SIP Phone Extension'
$extZip = Join-Path $here 'Telvoro Browser Extension.zip'
$storeId = ''
if ($StoreUrl -match '([a-p]{32})') { $storeId = $Matches[1] }
if ($storeId) {
    # the app must accept the store copy's ID (it writes the bridge manifest itself)
    Invoke-Py -m sipphone --set "extension.store_id=$storeId" | Out-Null
    Invoke-Py -m sipphone --register-bridge | Out-Null
}
if (Test-Path (Join-Path $extDir 'manifest.json')) {
    Invoke-Py -c "from sipphone.extension_sync import sync; sync()" | Out-Null
    Write-Host "Chrome/Edge extension: folder updated (a folder install reloads itself)"
} elseif (-not $NoExtension) {
    if ($storeId) {
        if ($Quiet) {
            Write-Host "Chrome/Edge extension: install it from $StoreUrl"
        } elseif (Ask "`nAdd Telvoro to Chrome/Edge from the Chrome Web Store (side panel, click-to-call, call pop-ups)?" $true) {
            Start-Process $StoreUrl
            Write-Host ""
            Write-Host "The store page opened in your browser." -ForegroundColor Cyan
            Write-Host "  1. Click 'Add to Chrome' (Edge: allow extensions from other stores if asked)."
            Write-Host "  2. Pin Telvoro from the puzzle-piece menu."
            Read-Host "`nPress Enter when that's done" | Out-Null
        }
    } elseif ($Quiet) {
        Invoke-Py -c "from sipphone.extension_sync import sync, write_zip; sync(); print(write_zip())"
        Write-Host "Chrome/Edge extension: drop the zip above onto chrome://extensions (Developer mode on), or Load unpacked $extDir"
    } elseif (Ask "`nAdd Telvoro to Chrome/Edge (side panel, click-to-call, call pop-ups)?" $true) {
        Write-Host ""
        Invoke-Py -m sipphone --extension
        Write-Host ""
        Write-Host "Two windows opened: the browser's Extensions page and a folder with the zip selected." -ForegroundColor Cyan
        Write-Host "  1. On the Extensions page turn on 'Developer mode' (top right; left side in Edge)."
        Write-Host "  2. Drag the selected zip onto the Extensions page."
        Write-Host "  3. Pin Telvoro from the puzzle-piece menu."
        Read-Host "`nPress Enter when that's done" | Out-Null
    }
}

Step "8. Start at sign-in"
# 8. Start at sign-in (runs in the background; the extension finds it running)
if (-not $NoStartup -and (Ask "`nStart Telvoro automatically when you sign in to Windows?" $true)) {
    Invoke-Py -m sipphone --startup on | Out-Null
    Write-Host "Start at sign-in: on"
}

Step "9. Starting Telvoro"
# 9. Start the phone. On a new install its Settings screen opens for the details.
try {
    # Start through pythonw (what the Start-menu shortcut does too) - the pip exe
    # launcher breaks when the Python it was installed with changes.
    $pyw = (Invoke-Py -c "from sipphone.app import pythonw; print(pythonw())").Trim()
    if (Test-Path $pyw) {
        $appArgs = @('-m', 'sipphone.app'); if ($Quiet) { $appArgs += '--no-browser' }
        Start-Process -FilePath $pyw -ArgumentList $appArgs
        Write-Host "`nStarting Telvoro..."
    }
} catch { }

$ver = (Invoke-Py -m sipphone --version 2>$null)
Write-Host ""
Write-Host "Done - $ver installed." -ForegroundColor Green
if (-not $Quiet) {
    Write-Host "Telvoro opens in your browser. Choose 'Phone + Team view' or 'Team view only' in its"
    Write-Host "Settings screen and fill in the details it asks for."
}
Write-Host "Next time, start it from 'Telvoro' in the Start menu (or the browser extension)."
Write-Host "Updates: Settings > Updates > 'Install from file', or run the new Install.bat."
Write-Host "Browser extension later: Settings > Browser extension, or run   python -m sipphone --extension"
Pause-Exit 0
