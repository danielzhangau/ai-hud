<#
.SYNOPSIS
    AI-HUD OTA updater (Windows). Mirrors mac-launcher/src/updater.py.

.PARAMETER Adb
    Path to the bundled adb.exe.

.PARAMETER ScriptDir
    Directory holding the launcher and (optionally) mirrors.conf.

.DESCRIPTION
    Reads /root/version.txt from the device, compares against the latest
    GitHub Release tag, and -- if outdated and the user accepts -- pulls
    the matching update-bundle-vX.Y.Z.zip, sha256-verifies every entry
    in its manifest.json, adb-pushes everything, runs post_deploy, and
    waits for the resulting device reboot.
#>

param(
    [Parameter(Mandatory = $true)] [string] $Adb,
    [Parameter(Mandatory = $true)] [string] $ScriptDir
)

# Dialogs (same idea as launcher.ps1; copied here so updater.ps1 can be
# invoked standalone for testing).
Add-Type -AssemblyName System.Windows.Forms | Out-Null

$GitHubRepo = "danielzhangau/ai-hud"
$GitHubApi  = "https://api.github.com/repos/$GitHubRepo/releases/latest"

function Show-Info($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Updater",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
}
function Show-Warn($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Updater",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
}
function Show-Error($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "AI-HUD Updater",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}
function Confirm-Update($current, $latest) {
    $r = [System.Windows.Forms.MessageBox]::Show(
        "A new AI-HUD version is available.`n`nCurrent: $current`nNew: $latest`n`nUpdate now? (about 30 seconds)",
        "AI-HUD Updater",
        [System.Windows.Forms.MessageBoxButtons]::OKCancel,
        [System.Windows.Forms.MessageBoxIcon]::Question,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button1)
    return $r -eq 'OK'
}

# ---------------------------------------------------------------------------
# Mirror config (optional)
# ---------------------------------------------------------------------------

function Get-MirrorUrls {
    $cfg = Join-Path $ScriptDir "mirrors.conf"
    $urls = @()
    if (Test-Path $cfg) {
        Get-Content $cfg | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#")) {
                $urls += ($line.TrimEnd('/') + '/')
            }
        }
    }
    return $urls
}

# ---------------------------------------------------------------------------
# adb wrappers
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
        return [PSCustomObject]@{ ExitCode = -1; StdOut = ""; StdErr = "timeout" }
    }
    return [PSCustomObject]@{
        ExitCode = $proc.ExitCode
        StdOut   = $proc.StandardOutput.ReadToEnd().Trim()
        StdErr   = $proc.StandardError.ReadToEnd().Trim()
    }
}

function Get-DeviceVersion {
    $r = Invoke-Adb -ArgsList @("shell", "cat /root/version.txt 2>/dev/null") -TimeoutSec 5
    if ($r.ExitCode -eq 0 -and $r.StdOut) {
        $v = ($r.StdOut -split "`n")[0].Trim()
        if ($v) { return $v }
    }
    return "0.0.0"
}

function Wait-ForDevice {
    param([int]$MaxWaitSec = 60)
    $deadline = (Get-Date).AddSeconds($MaxWaitSec)
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-Adb -ArgsList @("devices") -TimeoutSec 4
        if ($r.StdOut) {
            foreach ($line in $r.StdOut -split "`n") {
                if ($line -match "\bdevice$") {
                    Start-Sleep -Seconds 2
                    return $true
                }
            }
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

# ---------------------------------------------------------------------------
# Semver compare (returns negative / 0 / positive)
# ---------------------------------------------------------------------------

function Compare-Semver($a, $b) {
    $partsA = ($a -replace '^v','').Split('-')[0].Split('.') | ForEach-Object { [int]$_ }
    $partsB = ($b -replace '^v','').Split('-')[0].Split('.') | ForEach-Object { [int]$_ }
    for ($i = 0; $i -lt 3; $i++) {
        $x = if ($i -lt $partsA.Count) { $partsA[$i] } else { 0 }
        $y = if ($i -lt $partsB.Count) { $partsB[$i] } else { 0 }
        if ($x -ne $y) { return $x - $y }
    }
    return 0
}

# ---------------------------------------------------------------------------
# Bundle download + deploy
# ---------------------------------------------------------------------------

function Get-FileSha256($path) {
    return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

function Invoke-HttpDownload {
    param([string]$Url, [string]$Dest)
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Dest -TimeoutSec 60
        return $true
    } catch {
        Write-Output "[updater]   download from $Url failed: $_"
        return $false
    }
}

function Invoke-Update($bundlePath) {
    $workdir = Join-Path $env:TEMP "ai_hud_bundle_$(Get-Random)"
    New-Item -ItemType Directory -Path $workdir -Force | Out-Null
    try {
        Expand-Archive -Path $bundlePath -DestinationPath $workdir -Force
        $manifestPath = Join-Path $workdir "manifest.json"
        if (-not (Test-Path $manifestPath)) {
            throw "manifest.json missing from bundle"
        }
        $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
        if ($manifest.schema -ne 1) {
            throw "unknown manifest schema: $($manifest.schema)"
        }

        $files = $manifest.files
        $total = $files.Count
        $i = 0
        foreach ($entry in $files) {
            $i++
            $local = Join-Path $workdir $entry.src
            if (-not (Test-Path $local)) {
                throw "missing file in bundle: $($entry.src)"
            }
            $actual = Get-FileSha256 $local
            if ($actual -ne $entry.sha256.ToLower()) {
                throw "checksum mismatch for $($entry.src)"
            }

            $destDir = Split-Path -Parent $entry.dest
            if ($destDir -and $destDir -ne "/") {
                Invoke-Adb -ArgsList @("shell", "mkdir -p $destDir") | Out-Null
            }
            Write-Output "  [$i/$total] $($entry.dest)"
            $push = Invoke-Adb -ArgsList @("push", $local, $entry.dest) -TimeoutSec 60
            if ($push.ExitCode -ne 0) {
                throw "adb push failed for $($entry.dest): $($push.StdErr)"
            }
            $mode = if ($entry.mode) { $entry.mode } else { "0644" }
            Invoke-Adb -ArgsList @("shell", "chmod $mode $($entry.dest)") | Out-Null
        }

        $triggersReboot = $false
        foreach ($cmd in $manifest.post_deploy) {
            Write-Output "  post-deploy: $cmd"
            if ($cmd.Trim() -eq "reboot") { $triggersReboot = $true }
            $timeout = if ($cmd.Trim() -eq "reboot") { 4 } else { 10 }
            $r = Invoke-Adb -ArgsList @("shell", $cmd) -TimeoutSec $timeout
            # Ignore non-zero exits; reboot returns non-zero, other
            # commands sometimes do too on busybox.
        }

        if ($triggersReboot) {
            Write-Output "  waiting for device to come back from reboot..."
            Wait-ForDevice -MaxWaitSec 60 | Out-Null
        }

        return $manifest.version
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $workdir
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$current = Get-DeviceVersion
Write-Output "[updater] device version: $current"

try {
    $release = Invoke-WebRequest -UseBasicParsing -Uri $GitHubApi -TimeoutSec 8 -ErrorAction Stop
    $release = $release.Content | ConvertFrom-Json
} catch {
    Write-Output "[updater] GitHub probe failed: $_"
    exit 0
}

$latest = ($release.tag_name -replace '^v','')
if (-not $latest) { exit 0 }
Write-Output "[updater] latest release: $latest"

if ((Compare-Semver $current $latest) -ge 0) {
    Write-Output "[updater] device is up to date"
    exit 0
}

if (-not (Confirm-Update $current $latest)) {
    Write-Output "[updater] user skipped"
    exit 0
}

# Find the update-bundle asset
$asset = $release.assets | Where-Object {
    $_.name -like "update-bundle-*.zip"
} | Select-Object -First 1
if (-not $asset) {
    Show-Error "Update available but the bundle asset is missing on GitHub. Try again later."
    exit 2
}

# Build URL fallback list (GitHub first, then any configured mirrors)
$urls = @($asset.browser_download_url)
foreach ($base in Get-MirrorUrls) {
    $urls += ($base + $asset.name)
}

$bundlePath = Join-Path $env:TEMP "ai-hud-bundle-$(Get-Random).zip"
$downloaded = $false
foreach ($url in $urls) {
    Write-Output "[updater] downloading $url"
    if (Invoke-HttpDownload $url $bundlePath) {
        $downloaded = $true
        break
    }
}
if (-not $downloaded) {
    Show-Error "Download failed from all sources.`nCheck your internet connection."
    exit 2
}

try {
    $applied = Invoke-Update $bundlePath
} catch {
    Show-Error @"
Update failed mid-deploy:

$_

The device may be in an inconsistent state. Try running the launcher again.
"@
    exit 2
} finally {
    Remove-Item -ErrorAction SilentlyContinue $bundlePath
}

Show-Info "Update to v$applied complete.`n`nThe dashboard will reload in a moment."
Write-Output "[updater] update applied: v$applied"
