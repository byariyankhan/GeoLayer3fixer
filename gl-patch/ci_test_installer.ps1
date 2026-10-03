# CI test for Install-GlPatchV4.ps1 / Uninstall-GlPatchV4.ps1 on a fake GEOlayers extension
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ext = Join-Path $env:RUNNER_TEMP "fakeext\Geolayers 3 test"
New-Item -ItemType Directory -Force -Path "$ext\js", "$ext\CSXS" | Out-Null
$enc = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText("$ext\CSXS\manifest.xml", '<ExtensionManifest ExtensionBundleName="GEOlayers 3"/>', $enc)
$v3 = "/* GL-PATCH v3 2026-09-02: test */`nwindow.glSafeImageFile=(function(){return{}})();`n`n!function o(n){var s='Kölner Straße – ünïcödé ✓'}();`n"
$main = "a();x=glSafeImageFile.writeDataUriAtomic(r,n+`".`"+i).then(f);b('ä');`n"
[IO.File]::WriteAllText("$ext\js\libs.js", $v3, $enc)
[IO.File]::WriteAllText("$ext\js\main.js", $main, $enc)
$h0 = (Get-FileHash "$ext\js\libs.js").Hash, (Get-FileHash "$ext\js\main.js").Hash

function Check($cond, $msg) { if (-not $cond) { Write-Host "FAIL: $msg"; exit 1 } else { Write-Host "ok - $msg" } }

& powershell -NoProfile -ExecutionPolicy Bypass -File "$here\Install-GlPatchV4.ps1" -ExtensionDir $ext -Yes
Check ($LASTEXITCODE -eq 0) "install exit code 0"
$libs = [IO.File]::ReadAllText("$ext\js\libs.js", $enc)
$mainNew = [IO.File]::ReadAllText("$ext\js\main.js", $enc)
Check ($libs.StartsWith("/* GL-PATCH v4")) "libs.js has v4 block"
Check ($libs.EndsWith("`n`n!function o(n){var s='Kölner Straße – ünïcödé ✓'}();`n")) "rest of libs.js preserved byte-for-byte (unicode)"
Check ($mainNew -eq "a();x=glSafeImageFile.writeCanvasAtomic(p,u,m,r,n+`".`"+i).then(f);b('ä');`n") "main.js call site replaced, rest untouched"
Check (Test-Path "$ext\gl-patch-installed.txt") "install note written"
Check ((Get-ChildItem $ext -Directory -Filter "gl-patch-backup-v3-*").Count -eq 1) "backup created"

& powershell -NoProfile -ExecutionPolicy Bypass -File "$here\Install-GlPatchV4.ps1" -ExtensionDir $ext -Yes
Check ($LASTEXITCODE -eq 0) "second install is a no-op"
Check ((Get-ChildItem $ext -Directory -Filter "gl-patch-backup-v3-*").Count -eq 1) "no second backup"

& powershell -NoProfile -ExecutionPolicy Bypass -File "$here\Uninstall-GlPatchV4.ps1" -ExtensionDir $ext -Yes
Check ((Get-FileHash "$ext\js\libs.js").Hash -eq $h0[0] -and (Get-FileHash "$ext\js\main.js").Hash -eq $h0[1]) "uninstall restores v3 byte-for-byte"

# unknown state (e.g. GEOlayers updated, no patch) -> refuses and changes nothing
[IO.File]::WriteAllText("$ext\js\libs.js", "!function o(n){}();`n", $enc)
$before = (Get-FileHash "$ext\js\libs.js").Hash
& powershell -NoProfile -ExecutionPolicy Bypass -File "$here\Install-GlPatchV4.ps1" -ExtensionDir $ext -Yes
Check ($LASTEXITCODE -eq 1) "unknown libs.js state -> exit 1"
Check ((Get-FileHash "$ext\js\libs.js").Hash -eq $before) "unknown state left untouched"
Write-Host "gl-patch installer tests passed"
