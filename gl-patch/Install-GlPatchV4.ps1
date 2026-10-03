<#
  GL-PATCH v4 installer for GEOlayers 3 (After Effects CEP extension)

  Upgrades an installed GL-PATCH v3 to v4:
    * js\libs.js : the glSafeImageFile block is replaced with v4 (full PNG validation,
                   re-encode on corruption, own Node-zlib encoder as fallback)
    * js\main.js : merged tiles are written through writeCanvasAtomic (one call site)

  Safe by design:
    * only patches files in the exact v3 state (anything else -> stops, changes nothing)
    * After Effects must be closed
    * current files are backed up next to the extension; Uninstall-GlPatchV4.ps1 restores them
    * result is verified after writing

  Usage (run as Administrator - install.cmd does this for you):
    powershell -ExecutionPolicy Bypass -File Install-GlPatchV4.ps1 [-ExtensionDir <path>] [-Yes]
#>
param(
  [string]$ExtensionDir = "",
  [switch]$Yes
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$blockFile = Join-Path $here "glsafe_v4.js"
$OldCall = 'glSafeImageFile.writeDataUriAtomic(r,n+"."+i)'
$NewCall = 'glSafeImageFile.writeCanvasAtomic(p,u,m,r,n+"."+i)'
$BlockEnd = "})();`n`n!function o("

function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; Write-Host "Nothing was changed."; if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }; exit 1 }
function Sha($path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash }
function ReadText($path) { [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($path)) }
function WriteText($path, $text) { [System.IO.File]::WriteAllBytes($path, (New-Object System.Text.UTF8Encoding($false)).GetBytes($text)) }

Write-Host "GL-PATCH v4 installer for GEOlayers 3" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $blockFile)) { Fail "glsafe_v4.js not found next to this script." }
$block = (ReadText $blockFile).TrimEnd()
if (-not $block.StartsWith("/* GL-PATCH v4") -or -not $block.EndsWith("})();")) { Fail "glsafe_v4.js looks damaged." }

# ---- find the GEOlayers extension -------------------------------------------------
if (-not $ExtensionDir) {
  $roots = @("${env:ProgramFiles(x86)}\Common Files\Adobe\CEP\extensions",
             "$env:ProgramFiles\Common Files\Adobe\CEP\extensions",
             "$env:APPDATA\Adobe\CEP\extensions")
  $found = @()
  foreach ($r in $roots) {
    if (-not (Test-Path -LiteralPath $r)) { continue }
    foreach ($d in Get-ChildItem -LiteralPath $r -Directory) {
      $m = Join-Path $d.FullName "CSXS\manifest.xml"
      if ((Test-Path -LiteralPath (Join-Path $d.FullName "js\libs.js")) -and (Test-Path -LiteralPath $m) -and ((ReadText $m) -match "(?i)geolayers")) { $found += $d.FullName }
    }
  }
  if ($found.Count -eq 0) { Fail "GEOlayers 3 extension folder not found. Pass it with -ExtensionDir." }
  if ($found.Count -gt 1) { Fail ("More than one GEOlayers install found, pass one with -ExtensionDir:`n  " + ($found -join "`n  ")) }
  $ExtensionDir = $found[0]
}
$libs = Join-Path $ExtensionDir "js\libs.js"
$main = Join-Path $ExtensionDir "js\main.js"
Write-Host "Extension: $ExtensionDir"

# ---- admin + AE closed ------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and $ExtensionDir -like "*Program Files*") { Fail "Run as Administrator (use install.cmd)." }
if (Get-Process -Name "AfterFX" -ErrorAction SilentlyContinue) { Fail "After Effects is running. Save your work, close After Effects, then run this again." }

# ---- check current state ----------------------------------------------------------
$libsText = ReadText $libs
$mainText = ReadText $main
if ($libsText.StartsWith("/* GL-PATCH v4")) {
  Write-Host "`nGL-PATCH v4 is already installed. Nothing to do." -ForegroundColor Green
  if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }; exit 0
}
if (-not $libsText.StartsWith("/* GL-PATCH v3")) { Fail "js\libs.js is not in the GL-PATCH v3 state this installer expects (maybe GEOlayers was updated)." }
$cut = $libsText.IndexOf($BlockEnd)
if ($cut -lt 0) { Fail "Could not find the end of the v3 block in js\libs.js." }
$calls = ([regex]::Matches($mainText, [regex]::Escape($OldCall))).Count
if ($calls -ne 1) { Fail "js\main.js: expected exactly 1 tile-write call site, found $calls." }

if (-not $Yes) {
  Write-Host "`nThis will upgrade GL-PATCH v3 -> v4 (backup is made first)."
  if ((Read-Host "Continue? (y/n)") -notmatch "^[yY]") { Write-Host "Cancelled."; exit 0 }
}

# ---- backup -----------------------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $ExtensionDir "gl-patch-backup-v3-$stamp"
New-Item -ItemType Directory -Path $backup | Out-Null
Copy-Item -LiteralPath $libs -Destination $backup
Copy-Item -LiteralPath $main -Destination $backup
if ((Sha $libs) -ne (Sha (Join-Path $backup "libs.js")) -or (Sha $main) -ne (Sha (Join-Path $backup "main.js"))) { Fail "Backup verification failed." }
Write-Host "Backup: $backup"

# ---- patch ------------------------------------------------------------------------
$newLibs = $block + $libsText.Substring($cut + 5)
$newMain = $mainText.Replace($OldCall, $NewCall)
try {
  WriteText $libs $newLibs
  WriteText $main $newMain
} catch {
  Copy-Item -LiteralPath (Join-Path $backup "libs.js") -Destination $libs -Force
  Copy-Item -LiteralPath (Join-Path $backup "main.js") -Destination $main -Force
  Fail "Writing failed ($($_.Exception.Message)); original files restored."
}

# ---- verify -----------------------------------------------------------------------
$chkLibs = ReadText $libs; $chkMain = ReadText $main
$okLibs = $chkLibs.StartsWith("/* GL-PATCH v4") -and $chkLibs.Contains("writeCanvasAtomic:writeCanvasAtomic") -and $chkLibs.EndsWith($libsText.Substring($cut + 5))
$okMain = $chkMain.Contains($NewCall) -and -not $chkMain.Contains($OldCall) -and $chkMain.Length -eq ($mainText.Length + $NewCall.Length - $OldCall.Length)
if (-not ($okLibs -and $okMain)) {
  Copy-Item -LiteralPath (Join-Path $backup "libs.js") -Destination $libs -Force
  Copy-Item -LiteralPath (Join-Path $backup "main.js") -Destination $main -Force
  Fail "Verification failed; original files restored."
}

$note = Join-Path $ExtensionDir "gl-patch-installed.txt"
$info = @"
GL-PATCH v4 (full PNG validation + re-encode on corruption) installed $(Get-Date -Format s)
js/main.js SHA256 $(Sha $main)
js/libs.js SHA256 $(Sha $libs)
previous (v3) files backed up in: $backup
rollback: run Uninstall-GlPatchV4.ps1 (as administrator)
"@
WriteText $note $info
Write-Host "`nGL-PATCH v4 installed and verified." -ForegroundColor Green
Write-Host "Start After Effects again. Log: %APPDATA%\aescripts\GEOlayers3\gl-patch.log (look for 'loaded v4')."
if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }
exit 0
