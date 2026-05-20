<#
.SYNOPSIS
    AI-HUD Config launcher (Windows .bat entry runs this).

.DESCRIPTION
    Mirrors the macOS launch.sh:
      1. Detects USB state via Get-PnpDevice (Rockchip VID 0x2207).
      2. Routes:
           * ADB device -> adb forward + browser open + OTA check
           * MaskROM/Loader -> firmware flash NOT YET IMPLEMENTED on
             Windows; we tell the user to do the firmware step on a
             Mac, then come back. (Customer day-to-day OTA + dashboard
             is fully covered here.)
           * none -> "plug in your AI-HUD device" dialog.

    Stdlib only. Bundled adb.exe lives next to this script.

.NOTES
    Requires PowerShell 5+ (Windows 10 default) for ConvertFrom-Json
    and Invoke-WebRequest.
#>

# Hide the PowerShell console even if -WindowStyle Hidden wasn't passed.
# Some Win10 versions still flash a console window briefly otherwise.
$null = Add-Type -Name w -Namespace c -MemberDefinition '
    [DllImport("Kernel32")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern int ShowWindow(IntPtr hwnd, int n);
' -PassThru -ErrorAction SilentlyContinue
try { [c.w]::ShowWindow([c.w]::GetConsoleWindow(), 0) | Out-Null } catch {}

# Make sure we have the System.Windows.Forms assembly for native dialogs.
# Available on every Windows since Vista; loading is cheap.
Add-Type -AssemblyName System.Windows.Forms | Out-Null

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Adb         = Join-Path $ScriptDir "adb.exe"
$HostPort    = 8080
$DevicePort  = 80
$Url         = "http://localhost:$HostPort"

# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

function Show-Info($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Config",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}

function Show-Warn($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Config",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
}

function Show-Error($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Config",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}

# ---------------------------------------------------------------------------
# USB state detection
# ---------------------------------------------------------------------------

# Returns "adb" | "maskrom" | "loader" | "none" | "unknown:<pid>".
# We hit Get-PnpDevice because it works without admin and reads the
# real PnP enumerator instead of relying on adb (which would fork
# its own server just to tell us "yes, a device is here").
function Get-RockchipState {
    try {
        $devices = Get-PnpDevice -PresentOnly -ErrorAction Stop |
                   Where-Object {
                       $_.InstanceId -match 'VID_2207'
                   }
    } catch {
        # Older Windows / restricted environments may not have the
        # CIM cmdlets. Fall back to assuming no device so the user
        # gets the "plug it in" prompt rather than a stack trace.
        return "none"
    }
    if (-not $devices) { return "none" }

    $pids = @()
    foreach ($d in $devices) {
        if ($d.InstanceId -match 'PID_([0-9A-Fa-f]+)') {
            $pids += [Convert]::ToInt32($Matches[1], 16)
        }
    }
    # MaskROM PID varies by Rockchip SoC generation. RV1106 (our target)
    # uses 0x110c, earlier RK chips report 0x110b. Accept both. Loader
    # stage (post-MaskROM, pre-Linux) is 0x110a across generations.
    # Confirmed on hardware 2026-05-19: RV1106 in MaskROM enumerates as
    # VID_2207&PID_110C with no manufacturer/product strings.
    if (($pids -contains 0x110b) -or ($pids -contains 0x110c)) { return "maskrom" }
    if ($pids -contains 0x110a) { return "loader"  }
    if ($pids -contains 0x0019) { return "adb"     }
    if ($pids) { return "unknown:$($pids[0])" }
    return "none"
}

# ---------------------------------------------------------------------------
# adb shell helpers (mirror updater.py / launcher.sh)
# ---------------------------------------------------------------------------

function Invoke-Adb {
    param([string[]]$ArgsList, [int]$TimeoutSec = 30)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $Adb
    $psi.Arguments              = ($ArgsList | ForEach-Object {
        if ($_ -match '\s') { "`"$_`"" } else { $_ }
    }) -join ' '
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch {}
        return [PSCustomObject]@{
            ExitCode = -1
            StdOut   = ""
            StdErr   = "timeout after $TimeoutSec s"
        }
    }
    return [PSCustomObject]@{
        ExitCode = $proc.ExitCode
        StdOut   = $proc.StandardOutput.ReadToEnd().Trim()
        StdErr   = $proc.StandardError.ReadToEnd().Trim()
    }
}

function Test-AdbDevice {
    $r = Invoke-Adb -ArgsList @("devices") -TimeoutSec 5
    if ($r.ExitCode -ne 0) { return $false }
    foreach ($line in $r.StdOut -split "`n") {
        # Match e.g. "fd673a469f61593a    device"
        if ($line -match "^\S+\s+device\b") { return $true }
    }
    return $false
}

# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

if (-not (Test-Path $Adb)) {
    Show-Error "Bundled adb.exe is missing.`n`nPlease re-extract the AI-HUD Config zip and try again."
    exit 1
}

$state = Get-RockchipState
Write-Output "[launcher] detected USB state: $state"

switch ($state) {
    "none" {
        Show-Warn @"
No AI-HUD device detected.

Please plug the device in via USB, wait a few seconds for it to boot,
then double-click this launcher again.

If the device is in firmware-flash mode (BOOT button held while
plugging in), make sure the cable is a data cable (not power-only).
"@
        exit 2
    }

    { $_ -eq "maskrom" -or $_ -eq "loader" } {
        # Windows firmware flashing requires the Rockchip USB Driver and
        # the upgrade_tool.exe binary, which we don't currently bundle
        # (driver install is itself a several-click affair that breaks
        # the "double-click only" promise). Direct the user to do this
        # one step on a Mac for now -- it's a rare path (kernel upgrade)
        # and macOS handles it cleanly.
        Show-Warn @"
The device is in firmware-flash mode.

Firmware updates from Windows aren't supported yet -- please do this
step on a Mac (drag AI-HUD Config.app out of the AIHUD drive, run it).

If you don't have a Mac available, contact support.
"@
        exit 3
    }

    default {
        if ($state -ne "adb") {
            Show-Error "Unexpected USB state: $state.`nPlease replug the device."
            exit 4
        }
    }
}

# --- ADB path: forward, probe, optional OTA, open browser ------------------

# Start adb server in this directory's tools -- avoids conflicts with a
# user-installed adb on a different protocol version.
Invoke-Adb -ArgsList @("kill-server")  -TimeoutSec 5 | Out-Null
Invoke-Adb -ArgsList @("start-server") -TimeoutSec 5 | Out-Null

if (-not (Test-AdbDevice)) {
    Show-Warn @"
Windows sees the AI-HUD USB device, but adb can't reach it.

Try:
  - Replugging the USB cable
  - Using a different USB port (avoid USB hubs)
  - Waiting 30 seconds for the device to finish booting

Then double-click this launcher again.
"@
    exit 5
}

$forward = Invoke-Adb -ArgsList @("forward", "tcp:$HostPort", "tcp:$DevicePort") -TimeoutSec 5
if ($forward.ExitCode -ne 0) {
    Show-Error "adb forward failed:`n$($forward.StdErr)"
    exit 6
}

# Probe the device-side HTTP server with retries; hud_live.py may still
# be coming up if the customer plugged in <30 s ago.
$probeOk = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/state" `
                    -TimeoutSec 1 -ErrorAction Stop
        $probeOk = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $probeOk) {
    Show-Warn @"
Device is connected but the configuration server is not responding.

Try power-cycling the device (unplug + replug), then double-click
this launcher again.
"@
    exit 7
}

# Best-effort OTA check. updater.ps1 lives alongside us; if it's missing
# we still open the dashboard rather than blocking the user.
$updater = Join-Path $ScriptDir "updater.ps1"
if (Test-Path $updater) {
    try {
        & $updater -Adb $Adb -ScriptDir $ScriptDir
    } catch {
        Write-Output "[launcher] updater failed: $_ -- continuing"
    }
}

# Open the default browser. Start-Process with a URL hits whatever
# Windows is configured to handle http:// -- no need to find the
# user's Chrome / Edge / Firefox path explicitly.
Start-Process $Url
