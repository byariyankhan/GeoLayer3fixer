# GL-PATCH v4 for GEOlayers 3

Stops broken map tiles **before they are written to disk**, so After Effects never sees them.

## Why

GEOlayers builds merged tiles (1024/2048/4096 px) with Chromium's `canvas.toDataURL("image/png")`.
Inside After Effects 25/26 this encoder sometimes returns a broken PNG:

* an IDAT chunk header with length 0 (GL-PATCH v3 refused these: 137 times in one log), or
* chunks with **valid CRCs but corrupt compressed data** - v3 only checked CRCs, so these were
  written, and AE failed with `bad adaptive filter value (5027 :: 12)`.

The corruption is random: re-encoding the same canvas usually succeeds.

## What v4 does

1. Fully validates every PNG in memory: CRCs, chunk order, then **inflates the image data** and
   checks every scanline's filter byte and the exact image size (what AE's libpng checks).
2. If Chromium's PNG is broken, encodes the canvas again.
3. If that is broken too, encodes it with its own encoder (canvas pixels + Node zlib), pixel-exact.
4. Cached tiles get the same full check; broken ones are deleted and re-rendered.
5. Keeps v3's atomic write + read-back verification.

Log: `%APPDATA%\aescripts\GEOlayers3\gl-patch.log` (`loaded v4`, `BROKEN PNG from canvas.toDataURL`,
`re-encode OK`, `own encoder used`).

## Install

Requires GL-PATCH v3 already installed (the installer refuses any other state and changes nothing).

1. Close After Effects.
2. Unzip `GL-PATCH-v4.zip`, double-click `install.cmd` (asks for admin rights).
3. Start After Effects.

Undo: run `Uninstall-GlPatchV4.ps1` as administrator (restores the v3 files from the backup).

This repository contains only the patch code - no GEOlayers files.
