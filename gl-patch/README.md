# GL-PATCH v4.1 for GEOlayers 3

Stops broken map tiles **before they are written to disk**, so After Effects never sees them.

## Why

GEOlayers builds merged tiles (1024/2048/4096 px) with Chromium's `canvas.toDataURL("image/png")`.
Inside After Effects 25/26 this encoder sometimes returns a broken PNG:

* an IDAT chunk header with length 0 (GL-PATCH v3 refused these: 137 times in one log), or
* chunks with **valid CRCs but corrupt compressed data** - v3 only checked CRCs, so these were
  written, and AE failed with `bad adaptive filter value (5027 :: 12)`.

The corruption is random: re-encoding the same canvas usually succeeds.

## What v4.1 does

1. **Guards `canvas.toDataURL` itself** inside the GEOlayers panel. Every PNG it returns is fully
   validated (CRCs, chunk order, inflated image data, every scanline's filter byte, exact size).
   A broken one is replaced on the spot by a PNG encoded from the same canvas pixels with Node zlib
   (2D and WebGL canvases, pixel-exact, ~0.4 s for 4096 px). This covers every GEOlayers tile path:
   tile merger, GL renderer and zoom-level merge.
2. The safe writer validates again before writing (atomic write + read-back verification from v3).
3. Cached tiles get the same full check; broken ones are deleted and re-rendered.

v4.0 only covered the zoom-level merge, so Esri imagery tiles (tile merger -> GL renderer) still
failed with "N tile(s) couldn't be rendered".

Log: `%APPDATA%\aescripts\GEOlayers3\gl-patch.log` (`loaded v4.1 ... canvas guard on`,
`canvas.toDataURL returned a broken PNG ... replaced by own encoder`).

## Install

Requires GL-PATCH v3 or v4.0 already installed (the installer refuses any other state and changes nothing).

1. Close After Effects.
2. Unzip `GL-PATCH-v4.zip`, double-click `install.cmd` (asks for admin rights).
3. Start After Effects.

Undo: run `Uninstall-GlPatchV4.ps1` as administrator (restores the files from the newest backup).

This repository contains only the patch code - no GEOlayers files.
