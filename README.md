# GeoLayer Fixer v2

Fixes the **After Effects 2025/2026 PNGIO errors** with GEOlayers 3 map tiles:

```
PNGIO library error: bad adaptive filter value (5027 :: 12)
PNGIO library error: IDAT: CRC error (5027 :: 12)
```

## What it does

| Problem in tile | Action |
|---|---|
| Wrong CRC, pixel data intact | Rebuilt losslessly |
| Bad filter byte (> 4) in some rows | Rebuilt; only those rows change |
| Half-written / cut-off / corrupt stream | **Moved to `corrupt_tiles_quarantine`** so GEOlayers downloads it again (never padded with black rows) |
| Clean tile | **Not touched** (unless *Force* is on) |

* Checks PNGs exactly where AE's libpng fails: chunk CRCs, every scanline's filter byte, truncation.
* Detects PNGs by signature, scans all subfolders.
* Writes atomically (temp file + replace), so After Effects never reads a half-written file.
* Uses every CPU core (process pool) with optional high priority. GPU is not used — PNG decode/encode is CPU work.
* Watcher waits until GEOlayers finishes writing before checking a tile, and never rewrites clean tiles (no fix-loop).

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
