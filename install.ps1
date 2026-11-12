# tvmi-rc installer for Windows (PowerShell). No admin rights needed.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/install.ps1 | iex"
#
# It checks what is missing and asks before installing anything:
#   1. Python 3 (needed)   2. ffmpeg (optional)   3. tzdata, so daylight saving is right on any PC (recommended)
# then saves tvmi-recorder.py to %LOCALAPPDATA%\tvmi-rc and offers a Start menu shortcut.
# Run it again to update. Uninstall: delete that folder and the shortcut.
$ErrorActionPreference = "Stop"

$Url = if ($env:TVMI_RC_URL) { $env:TVMI_RC_URL } else { "https://raw.githubusercontent.com/onetrickpony1/tvmi-rc/main/tvmi-recorder.py" }
$Dir = Join-Path $env:LOCALAPPDATA "tvmi-rc"

function Say($m) { Write-Host "`n==> $m" }
function Ask($q, $default) {
    if ($env:CI) { return ($default -eq "y") }
    $a = Read-Host "$q"
    if (-not $a) { $a = $default }
    return ($a -match "^[Yy]")
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Try-Py($exe, [string[]]$pyargs) {   # run Python quietly; true if it exited 0 (stderr is not an error here)
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $exe @pyargs *> $null; return ($LASTEXITCODE -eq 0) } finally { $ErrorActionPreference = $old }
}
function Find-Python {
    foreach ($c in @("py", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd -and ($cmd.Source -notmatch "WindowsApps")) {
            if (Try-Py $cmd.Source @("-c", "import tkinter")) { return $cmd.Source }
        }
    }
    return $null
}

Say "Installing tvmi-rc"

# 1. Python (needed)
$py = Find-Python
if (-not $py) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3 (with tkinter) is required. Install it from https://www.python.org/downloads/ and run this again."
    }
    if (-not (Ask "Python 3 is missing and tvmi-rc needs it. Install it now with winget? [Y/n]" "y")) { throw "Python 3 is required" }
    winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) { throw "Python was installed but can't be found yet; open a new PowerShell window and run this again." }
}

# 2. ffmpeg (optional)
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    if (Ask "ffmpeg is not installed. It is optional (only for .mkv conversion and the ffmpeg engine). Install it? [y/N]" "n") {
        winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    }
}

# 3. tzdata (recommended: Windows has no time-zone data for Python)
if (-not (Try-Py $py @("-c", "import zoneinfo; zoneinfo.ZoneInfo('Europe/Malta')"))) {
    if (Ask "Schedule times are Maltese time. Install the small 'tzdata' package so daylight saving is always right? [Y/n]" "y") {
        & $py -m pip install --user --quiet tzdata
    }
}

# 4. the app
Say "Downloading tvmi-recorder.py"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$target = Join-Path $Dir "tvmi-recorder.pyw"
if ($env:TVMI_RC_SOURCE -and (Test-Path $env:TVMI_RC_SOURCE)) { Copy-Item $env:TVMI_RC_SOURCE $target -Force }
else { Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $target }
if (-not (Try-Py $py @("-m", "py_compile", $target))) { throw "the downloaded file is not valid Python" }

# 5. Start menu shortcut
if (-not $env:CI -and (Ask "Add 'TVMi Recorder' to the Start menu? [Y/n]" "y")) {
    $pyw = (& $py -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))").Trim()
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "TVMi Recorder.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = "`"$target`""
    $s.WorkingDirectory = $Dir
    $s.Save()
}

Say "Done. Start it from the Start menu, or run:  pythonw `"$target`""
