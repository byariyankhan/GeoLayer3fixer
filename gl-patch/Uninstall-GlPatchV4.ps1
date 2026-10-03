<#
  Restores js\libs.js and js\main.js of GEOlayers 3 from the newest gl-patch-backup-v3-* folder
  (i.e. undoes Install-GlPatchV4.ps1). Run as Administrator.
#>
param([string]$ExtensionDir = "", [switch]$Yes)
$ErrorActionPreference = "Stop"
function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }; exit 1 }

if (-not $ExtensionDir) {
  $roots = @("${env:ProgramFiles(x86)}\Common Files\Adobe\CEP\extensions", "$env:ProgramFiles\Common Files\Adobe\CEP\extensions", "$env:APPDATA\Adobe\CEP\extensions")
  $found = @(foreach ($r in $roots) { if (Test-Path -LiteralPath $r) { Get-ChildItem -LiteralPath $r -Directory | Where-Object { Get-ChildItem -LiteralPath $_.FullName -Directory -Filter "gl-patch-backup-v3-*" -ErrorAction SilentlyContinue } | ForEach-Object { $_.FullName } } })
  if ($found.Count -ne 1) { Fail "Could not find exactly one GEOlayers folder with a v3 backup. Pass -ExtensionDir." }
  $ExtensionDir = $found[0]
}
if (Get-Process -Name "AfterFX" -ErrorAction SilentlyContinue) { Fail "Close After Effects first." }
$backup = Get-ChildItem -LiteralPath $ExtensionDir -Directory -Filter "gl-patch-backup-v3-*" | Sort-Object Name | Select-Object -Last 1
if (-not $backup) { Fail "No gl-patch-backup-v3-* folder found." }
Copy-Item -LiteralPath (Join-Path $backup.FullName "libs.js") -Destination (Join-Path $ExtensionDir "js\libs.js") -Force
Copy-Item -LiteralPath (Join-Path $backup.FullName "main.js") -Destination (Join-Path $ExtensionDir "js\main.js") -Force
Write-Host "Restored GL-PATCH v3 files from $($backup.FullName)" -ForegroundColor Green
if (-not $Yes) { Read-Host "Press Enter to close" | Out-Null }
