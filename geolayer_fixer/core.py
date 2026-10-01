"""
Core PNG inspection and repair engine.

After Effects 25/26 rejects PNG files that older AE versions (and browsers)
silently tolerated. The two errors seen with GEOlayers 3 tiles are:

  * "bad adaptive filter value"  -> a scanline starts with a filter byte > 4
  * "IDAT: CRC error"            -> a chunk's CRC32 does not match its data

This module checks every PNG for exactly those problems (plus truncation and
broken zlib streams), and repairs them by rebuilding a clean, spec-compliant
PNG. Files that are already valid are left untouched unless `force=True`.
"""

from __future__ import annotations

import io
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFile

PNG_SIG = b"\x89PNG\r\n\x1a\n"

# Channels per PNG colour type
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


@dataclass
class Result:
    path: str
    status: str  # "ok" | "repaired" | "reencoded" | "failed" | "skipped"
    problems: list[str] = field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------------
# Low-level parsing
# --------------------------------------------------------------------------

@dataclass
class _Parsed:
    ihdr: bytes | None = None
    width: int = 0
    height: int = 0
    bit_depth: int = 0
    color_type: int = 0
    interlace: int = 0
    ancillary: list[tuple[bytes, bytes]] = field(default_factory=list)  # PLTE, tRNS, gAMA...
    idat: bytearray = field(default_factory=bytearray)
    problems: list[str] = field(default_factory=list)


def _parse_chunks(data: bytes) -> _Parsed:
    p = _Parsed()
    if not data.startswith(PNG_SIG):
        p.problems.append("not a PNG signature")
        return p

    pos = len(PNG_SIG)
    seen_iend = False
    crc_bad = 0
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        body_start = pos + 8
        body_end = body_start + length
        if length > 0x7FFFFFFF or not ctype.isalpha():
            p.problems.append(f"garbage chunk header at byte {pos}")
            break
        if body_end + 4 > len(data):
            # Truncated chunk: keep whatever body bytes exist
            p.problems.append(f"truncated {ctype.decode('latin1')} chunk")
            body = data[body_start:min(body_end, len(data))]
            if ctype == b"IDAT":
                p.idat += body
            break
        body = data[body_start:body_end]
        crc = struct.unpack(">I", data[body_end:body_end + 4])[0]
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            crc_bad += 1

        if ctype == b"IHDR":
            p.ihdr = body
            if len(body) >= 13:
                (p.width, p.height, p.bit_depth, p.color_type,
                 _comp, _filt, p.interlace) = struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"IDAT":
            p.idat += body
        elif ctype == b"IEND":
            seen_iend = True
            break
        elif ctype in (b"PLTE", b"tRNS", b"gAMA", b"sRGB", b"cHRM", b"iCCP", b"pHYs"):
            p.ancillary.append((ctype, body))
        pos = body_end + 4

    if crc_bad:
        p.problems.append(f"CRC error in {crc_bad} chunk(s)")
    if not seen_iend and "truncated" not in " ".join(p.problems):
        p.problems.append("missing IEND (file cut off)")
    if p.ihdr is None:
        p.problems.append("missing IHDR")
    return p


def _row_geometry(p: _Parsed) -> tuple[int, int]:
    """Return (bytes_per_pixel_for_filter, stride_without_filter_byte)."""
    ch = _CHANNELS.get(p.color_type, 4)
    bits_pp = ch * p.bit_depth
    bpp = max(1, bits_pp // 8)
    stride = (p.width * bits_pp + 7) // 8
    return bpp, stride


def _inflate_lenient(idat: bytes) -> tuple[bytes, str | None]:
    d = zlib.decompressobj()
    try:
        raw = d.decompress(bytes(idat))
        raw += d.flush()
        if not d.eof:
            return raw, "zlib stream incomplete"
        return raw, None
    except zlib.error as e:
        # Salvage as much as possible: decompress in small pieces
        d = zlib.decompressobj()
        out = bytearray()
        for i in range(0, len(idat), 256):
            try:
                out += d.decompress(bytes(idat[i:i + 256]))
            except zlib.error:
                break
        return bytes(out), f"zlib error ({e})"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def is_png_file(path: str | os.PathLike) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == PNG_SIG
    except OSError:
        return False


def inspect_bytes(data: bytes) -> list[str]:
    """Return a list of problems that would make After Effects reject the PNG.
    Empty list == file is clean."""
    p = _parse_chunks(data)
    problems = list(p.problems)
    if p.ihdr is None or p.width == 0 or p.height == 0:
        return problems or ["invalid header"]

    raw, zerr = _inflate_lenient(p.idat)
    if zerr:
        problems.append(zerr)

    if p.interlace == 0:
        _bpp, stride = _row_geometry(p)
        row_len = stride + 1
        rows_present = len(raw) // row_len
        bad = sum(1 for r in range(min(rows_present, p.height)) if raw[r * row_len] > 4)
        if bad:
            problems.append(f"bad adaptive filter value in {bad} row(s)")
        if rows_present < p.height:
            problems.append(f"missing {p.height - rows_present} of {p.height} rows")
    return problems


def _rebuild_png(p: _Parsed) -> bytes:
    """Build a clean PNG: valid CRCs, filter bytes 0-4, all rows present."""
    raw, _ = _inflate_lenient(p.idat)
    if p.interlace == 0:
        _bpp, stride = _row_geometry(p)
        row_len = stride + 1
        fixed = bytearray()
        for r in range(p.height):
            row = raw[r * row_len:(r + 1) * row_len]
            if len(row) < row_len or row[0] > 4:
                # Bad filter byte or missing/short row -> "None" filter, zero-pad
                row = (b"\x00" + row[1:]).ljust(row_len, b"\x00")
            fixed += row
        raw = bytes(fixed)

    def chunk(t: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body) & 0xFFFFFFFF)

    out = bytearray(PNG_SIG)
    out += chunk(b"IHDR", p.ihdr[:13])
    for t, body in p.ancillary:
        out += chunk(t, body)
    # Split into 1 MB IDAT chunks (some decoders dislike one giant chunk);
    # level 3 = fast enough for 4096px tiles, AE doesn't care about size.
    z = zlib.compress(raw, 3)
    for i in range(0, len(z), 1 << 20):
        out += chunk(b"IDAT", z[i:i + (1 << 20)])
    out += chunk(b"IEND", b"")
    return bytes(out)


def _reencode(data: bytes) -> bytes:
    """Decode with Pillow and write a brand-new standard PNG."""
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P", "I;16"):
            im = im.convert("RGBA")
        buf = io.BytesIO()
        info = {}
        if "transparency" in im.info:
            info["transparency"] = im.info["transparency"]
        im.save(buf, format="PNG", compress_level=3, **info)
        return buf.getvalue()


def _wait_stable(path: Path, timeout: float) -> bool:
    """Wait until the file stops growing (GEOlayers may still be writing)."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(0.25)
    return last > 0


def _atomic_write(path: Path, data: bytes, retries: int = 20) -> None:
    tmp = path.with_name(path.name + ".glfix.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # File locked (AE or GEOlayers reading it) -> retry
            time.sleep(0.15 * (i + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise PermissionError("file is locked by another program")


_INCOMPLETE = ("truncated", "missing", "incomplete", "cut off", "zlib error", "garbage")


def is_incomplete(problems: list[str]) -> bool:
    """True if image data is physically missing (half-written / cut-off download).
    Such tiles must NOT be 'repaired' - that would bake black bands into the
    map. They are moved to quarantine so GEOlayers downloads them again."""
    return any(k in pr for pr in problems for k in _INCOMPLETE)


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "GeoLayerFixer"


def quarantine_dir() -> Path:
    """Quarantine lives OUTSIDE the watched GEOlayers folders, so moved tiles
    are never picked up and re-processed (that caused an endless loop)."""
    return app_dir() / "quarantine"


# Folder names never scanned/watched (old v1/gl-patch quarantine included)
SKIP_DIR_NAMES = {"corrupt_tiles_quarantine", "quarantine", "__pycache__"}


def is_excluded(path: str | os.PathLike) -> bool:
    parts = {x.lower() for x in Path(path).parts}
    if parts & SKIP_DIR_NAMES:
        return True
    try:
        Path(path).resolve().relative_to(app_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def clean_quarantine(days: float = 7) -> int:
    """Delete quarantined tiles older than `days`. Returns number removed."""
    q = quarantine_dir()
    if not q.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in q.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def quarantine(path: Path, qdir: Path) -> str:
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / f"{int(time.time() * 1000)}_{path.name}"
    try:
        os.replace(path, dest)
    except OSError:
        # Different drive -> copy + delete
        import shutil
        shutil.move(str(path), str(dest))
    os.utime(dest)  # age counts from quarantine time
    return str(dest)


def process_file(path: str, force: bool = False, dry_run: bool = False,
                 wait_stable: float = 0.0, quarantine_to: str | None = None) -> Result:
    """Inspect one PNG and repair it if needed. Safe to run in a worker process.

    status: ok | repaired | reencoded | quarantined | failed | skipped
    """
    p = Path(path)
    try:
        if wait_stable and not _wait_stable(p, wait_stable):
            return Result(path, "skipped", message="file vanished or empty")
        data = p.read_bytes()
    except OSError as e:
        return Result(path, "skipped", message=str(e))

    if not data.startswith(PNG_SIG):
        return Result(path, "skipped", message="not a PNG")

    problems = inspect_bytes(data)
    if not problems and not force:
        return Result(path, "ok")
    if dry_run:
        return Result(path, "failed" if problems else "ok", problems, "(scan only)")

    if is_incomplete(problems):
        qdir = Path(quarantine_to) if quarantine_to else quarantine_dir()
        try:
            dest = quarantine(p, qdir)
        except OSError as e:
            return Result(path, "failed", problems, f"incomplete tile, could not move: {e}")
        return Result(path, "quarantined", problems,
                      f"incomplete tile moved to {dest} - GEOlayers will re-download it")

    # Data is all there; only CRCs / filter bytes / chunk layout are wrong.
    candidates = []
    try:
        candidates.append(_rebuild_png(_parse_chunks(data)))  # fast, keeps pixels
    except Exception:
        pass
    try:
        candidates.append(_reencode(data))  # fallback (e.g. interlaced PNG)
    except Exception:
        pass

    for out in candidates:
        if out and not inspect_bytes(out):
            try:
                _atomic_write(p, out)
            except Exception as e:
                return Result(path, "failed", problems, f"write failed: {e}")
            return Result(path, "repaired" if problems else "reencoded", problems)

    return Result(path, "failed", problems, "could not rebuild - delete this tile so GEOlayers re-downloads it")


_NOT_PNG_EXT = (".json", ".txt", ".log", ".jpg", ".jpeg", ".webp", ".pbf", ".mvt",
                ".tif", ".tiff", ".zip", ".jsx", ".aep", ".aet", ".lic", ".shp", ".dbf")


def iter_pngs(folders: list[str], since: float = 0.0):
    """Yield every PNG under the given folders (by signature, any extension).
    Quarantine folders and the app's own folder are skipped."""
    seen = set()
    for folder in folders:
        root = Path(os.path.expandvars(folder))
        if not root.is_dir() or is_excluded(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIR_NAMES]
            for name in files:
                low = name.lower()
                if low.endswith(".glfix.tmp") or low.endswith(_NOT_PNG_EXT):
                    continue
                fp = os.path.join(dirpath, name)
                if fp in seen:
                    continue
                if since:
                    try:
                        if os.path.getmtime(fp) < since:
                            continue
                    except OSError:
                        continue
                if low.endswith(".png") or is_png_file(fp):
                    seen.add(fp)
                    yield fp


def default_folders() -> list[str]:
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(appdata, "Aescripts", "GEOlayers3"),
        os.path.join(local, "Aescripts", "GEOlayers3"),
        os.path.join(appdata, "GEOlayers3"),
    ]
    found = [c for c in candidates if c and os.path.isdir(c)]
    return found or [os.path.join(appdata, "Aescripts", "GEOlayers3")]
