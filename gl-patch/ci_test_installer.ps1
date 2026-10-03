# CI test for Install-GlPatchV4.ps1 / Uninstall-GlPatchV4.ps1 on a fake GEOlayers extension
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
$ext = Join-Path $env:RUNNER_TEMP "fakeext\Geolayers 3 test"
New-Item -ItemType Directory -Force -Path "$ext\js", "$ext\CSXS" | Out-Null
$enc = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText("$ext\CSXS\manifest.xml", '<ExtensionManifest ExtensionBundleName="GEOlayers 3"/>', $enc)
$v3 = "/* GL-PATCH v3 2026-09-02: test */`nwindow.glSafeImageFile=(function(){return{}})();`n`n!function o(n){var s='Kölner Straße – ünïcödé ✓'}();`n"
$main = "a();x=glSafeImageFile.writeDataUriAtomic(r,n+`".`"+i).then(f);b('ä');`n"
[IO.File]::WriteAllText("$ext\js\libs.js", $v3, $enc)
[IO.File]::WriteAllText("$ext\js\main.js", $main, $enc)
$h0 = (Get-FileHash "$ext\js\libs.js").Hash, (Get-FileHash "$ext\js\main.js").Hash

function Ann($text) { ($text -replace "%", "%25" -replace "`r", "" -replace "`n", "%0A") }
function Check($cond, $msg) { if (-not $cond) { Write-Host "::error::FAIL: $msg"; exit 1 } else { Write-Host "::notice::ok - $msg" } }
function Run($script, [string[]]$extra) {
  $ErrorActionPreference = "Continue"
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here $script) -ExtensionDir $ext -Yes @extra 2>&1 | Out-String
  $code = $LASTEXITCODE
  Write-Host "::notice::$script exit $code%0A$(Ann $out)"
  return $code
}

$code = Run "Install-GlPatchV4.ps1"
Check ($code -eq 0) "install exit code 0"
$libs = [IO.File]::ReadAllText("$ext\js\libs.js", $enc)
$mainNew = [IO.File]::ReadAllText("$ext\js\main.js", $enc)
Check ($libs.StartsWith("/* GL-PATCH v4")) "libs.js has v4 block"
Check ($libs.EndsWith("`n`n!function o(n){var s='Kölner Straße – ünïcödé ✓'}();`n")) "rest of libs.js preserved byte-for-byte (unicode)"
Check ($mainNew -eq "a();x=glSafeImageFile.writeCanvasAtomic(p,u,m,r,n+`".`"+i).then(f);b('ä');`n") "main.js call site replaced, rest untouched"
Check (Test-Path "$ext\gl-patch-installed.txt") "install note written"
Check ((Get-ChildItem $ext -Directory -Filter "gl-patch-backup-v3-*").Count -eq 1) "backup created"

$code = Run "Install-GlPatchV4.ps1"
Check ($code -eq 0) "second install is a no-op"
Check ((Get-ChildItem $ext -Directory -Filter "gl-patch-backup-v3-*").Count -eq 1) "no second backup"

$code = Run "Uninstall-GlPatchV4.ps1"
Check ((Get-FileHash "$ext\js\libs.js").Hash -eq $h0[0] -and (Get-FileHash "$ext\js\main.js").Hash -eq $h0[1]) "uninstall restores v3 byte-for-byte"

# unknown state (e.g. GEOlayers updated, no patch) -> refuses and changes nothing
[IO.File]::WriteAllText("$ext\js\libs.js", "!function o(n){}();`n", $enc)
$before = (Get-FileHash "$ext\js\libs.js").Hash
$code = Run "Install-GlPatchV4.ps1"
Check ($code -eq 1) "unknown libs.js state -> exit 1"
Check ((Get-FileHash "$ext\js\libs.js").Hash -eq $before) "unknown state left untouched"
Write-Host "gl-patch installer tests passed"
} catch {
  Write-Host ("::error::EXCEPTION at line " + $_.InvocationInfo.ScriptLineNumber + ": " + $_.Exception.Message + " | " + $_.InvocationInfo.Line.Trim())
  exit 1
}
