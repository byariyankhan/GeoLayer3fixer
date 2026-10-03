# GeoLayer Fixer v2

Fixes the **After Effects 2025/2026 PNGIO errors** with GEOlayers 3 map tiles:

```
PNGIO library error: bad adaptive filter value (5027 :: 12)
PNGIO library error: IDAT: CRC error (5027 :: 12)
```

## What it does

**Rule: only repairs that keep every pixel identical. Anything else is re-downloaded.**

| Problem in tile | Action |
|---|---|
| Wrong CRC, data after the image, split IDAT chunks | Rebuilt losslessly (pixels verified byte-identical) |
| Bad filter byte, corrupt / half-written / cut-off data | **Moved to quarantine** so GEOlayers downloads a correct copy |
| Clean tile | **Not touched** (unless *Force* is on) |

Why bad filter bytes are not "patched": the error spreads to following rows
through the Up/Average/Paeth filters. On a real 4096px GEOlayers tile, 9 broken
rows became 217 visibly wrong rows after patching — stripes in the map.

* Checks PNGs exactly where AE's libpng fails: chunk CRCs, every scanline's filter byte, truncation.
* Detects PNGs by signature, scans all subfolders.
* Writes atomically (temp file + replace), so After Effects never reads a half-written file.
* If GEOlayers rewrites a tile while it is being fixed, the fix is dropped and the new tile is kept.
* Uses every CPU core (process pool) with optional high priority. GPU is not used — PNG decode/encode is CPU work.
* Watcher waits until GEOlayers finishes writing before checking a tile, and never rewrites clean tiles (no fix-loop).
* Watcher also re-scans recently changed tiles every 45 s, in case Windows drops change notifications.
* Tiles verified clean are remembered (size + modified time), so repeat runs of **Fix Tiles Now** only check new/changed tiles.
* Worker count adapts to free RAM (a 4096px tile needs ~600 MB while being fixed), leaving room for After Effects.
* Quarantine is kept in `%APPDATA%\GeoLayerFixer\quarantine` (outside the watched folders) and auto-cleaned after 7 days.
* Only one copy can run at a time; an old *Start with Windows* entry pointing at v1 is switched to this version.
* Shows a notice when a newer release is available.

## GL-PATCH v4.1 (recommended)

The Fixer can only react after a broken tile is on disk - often After Effects reads it first.
`GL-PATCH-v4.zip` (GL-PATCH v4.1) patches GEOlayers so broken tiles are re-encoded before they are written.
See [gl-patch/README.md](gl-patch/README.md). The Fixer's log shows whether v4.1 is active.

## Use

1. Download `GeoLayer_Fixer.exe` from **Releases** (or the latest Actions artifact).
2. Close the old v1 fixer.
3. Run, keep **Auto Watcher** active, click **Fix Tiles Now** before rendering.
4. In AE: *Edit → Purge → All Memory & Disk Cache*.

CLI: `GeoLayer_Fixer.exe --scan [folder]` / `--fix [--force] [folder]`

Logs: `%APPDATA%\GeoLayerFixer\geolayer_fixer.log`

## Develop

```
pip install -r requirements.txt pytest
python -m pytest tests
python -m geolayer_fixer
```
