"""
Core PNG inspection and repair engine.

After Effects 25/26 rejects PNG files that older AE versions (and browsers)
silently tolerated. The two errors seen with GEOlayers 3 tiles are:

  * "bad adaptive filter value"  -> a scanline starts with a filter byte > 4
  * "IDAT: CRC error"            -> a chunk's CRC32 does not match its data

Policy (v2.2): a tile is only repaired when the repair is LOSSLESS - every
pixel stays exactly as GEOlayers intended (wrong CRCs, data after the image,
split IDAT runs). Anything that would change pixels (bad filter bytes,
corrupt/missing image data) is moved to quarantine instead, so GEOlayers
downloads a correct copy. Patching a bad filter byte looks harmless but the
error spreads to following rows through the Up/Average/Paeth filters and
shows up as visible stripes in the map.
"""

from __future__ import annotations

import io
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"

# Channels per PNG colour type
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


@dataclass
class Result:
    path: str
    status: str  # ok | repaired | reencoded | quarantined | failed | skipped
    problems: list[str] = field(default_factory=list)
    message: str = ""
    # (size, mtime_ns) of the file content this result is about; used by the
    # verified cache so it never marks a tile clean that it did not check.
    sig: tuple[int, int] | None = None


# --------------------------------------------------------------------------
# Problem classification
# --------------------------------------------------------------------------

# Problems that can be fixed without touching a single pixel
LOSSLESS = ("CRC error", "extra data after image", "too much image data",
            "IDAT chunks not consecutive")


def needs_redownload(problems: list[str]) -> bool:
    """True if fixing the tile would alter pixels -> quarantine + re-download."""
    return any(not pr.startswith(LOSSLESS) for pr in problems)




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
    pre_idat: list[tuple[bytes, bytes]] = field(default_factory=list)   # PLTE, tRNS, sRGB...
    idat: bytearray = field(default_factory=bytearray)
    problems: list[str] = field(default_factory=list)


_KEEP_CHUNKS = (b"PLTE", b"tRNS", b"gAMA", b"sRGB", b"cHRM", b"iCCP", b"pHYs", b"sBIT", b"bKGD")


def _parse_chunks(data: bytes) -> _Parsed:
    p = _Parsed()
    if not data.startswith(PNG_SIG):
        p.problems.append("not a PNG signature")
        return p

    pos = len(PNG_SIG)
    seen_iend = False
    crc_bad = 0
    idat_runs = 0
    prev = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        body_start = pos + 8
        body_end = body_start + length
        if length > 0x7FFFFFFF or not ctype.isalpha():
            p.problems.append(f"garbage chunk header at byte {pos}")
            break
        if body_end + 4 > len(data):
            p.problems.append(f"truncated {ctype.decode('latin1')} chunk")
            if ctype == b"IDAT":
                p.idat += data[body_start:min(body_end, len(data))]
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
            if prev != b"IDAT":
                idat_runs += 1
            p.idat += body
        elif ctype == b"IEND":
            seen_iend = True
            break
        elif ctype in _KEEP_CHUNKS and not p.idat:
            p.pre_idat.append((ctype, body))
        prev = ctype
        pos = body_end + 4

    if crc_bad:
        p.problems.append(f"CRC error in {crc_bad} chunk(s)")
    if idat_runs > 1:
        p.problems.append("IDAT chunks not consecutive")
    if not seen_iend and not any(x.startswith(("truncated", "garbage")) for x in p.problems):
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


def _inflate(idat: bytes) -> tuple[bytes, list[str]]:
    """Decompress IDAT data. Returns (raw, problems)."""
    d = zlib.decompressobj()
    try:
        raw = d.decompress(bytes(idat)) + d.flush()
    except zlib.error as e:
        # Salvage what we can (only used for diagnostics)
        d = zlib.decompressobj()
        out = bytearray()
        for i in range(0, len(idat), 4096):
            try:
                out += d.decompress(bytes(idat[i:i + 4096]))
            except zlib.error:
                break
        return bytes(out), [f"zlib error ({e})"]
    probs = []
    if not d.eof:
        probs.append("zlib stream incomplete")
    elif d.unused_data:
        probs.append("extra data after image stream")
    return raw, probs


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

    raw, zprobs = _inflate(p.idat)
    problems += zprobs

    if p.interlace == 0:
        _bpp, stride = _row_geometry(p)
        row_len = stride + 1
        expected = row_len * p.height
        rows_present = min(len(raw) // row_len, p.height)
        bad = sum(1 for r in range(rows_present) if raw[r * row_len] > 4)
        if bad:
            problems.append(f"bad adaptive filter value in {bad} row(s)")
        if len(raw) < expected:
            problems.append(f"missing {p.height - rows_present} of {p.height} rows")
        elif len(raw) > expected:
            problems.append("too much image data")
    return problems


def _chunk(t: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body) & 0xFFFFFFFF)


def _rebuild_lossless(p: _Parsed) -> bytes:
    """Re-pack the original filtered scanlines into a clean PNG: fresh CRCs,
    one IDAT run, no trailing junk. Pixel data is byte-for-byte unchanged."""
    raw, zprobs = _inflate(p.idat)
    if any(x.startswith(("zlib error", "zlib stream incomplete")) for x in zprobs):
        raise ValueError("image data damaged")
    if p.interlace == 0:
        _bpp, stride = _row_geometry(p)
        expected = (stride + 1) * p.height
        if len(raw) < expected:
            raise ValueError("image data incomplete")
        raw = raw[:expected]
    out = bytearray(PNG_SIG)
    out += _chunk(b"IHDR", p.ihdr[:13])
    for t, body in p.pre_idat:
        out += _chunk(t, body)
    # 1 MB IDAT chunks; level 3 is fast enough for 4096px tiles
    z = zlib.compress(raw, 3)
    for i in range(0, len(z), 1 << 20):
        out += _chunk(b"IDAT", z[i:i + (1 << 20)])
    out += _chunk(b"IEND", b"")
    return bytes(out)


def _reencode(data: bytes) -> bytes:
    """Decode with Pillow (strict) and write a brand-new standard PNG.
    Only used for 'Force' mode on files that are already clean."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        info = {}
        if "transparency" in im.info:
            info["transparency"] = im.info["transparency"]
        buf = io.BytesIO()
        im.save(buf, format="PNG", compress_level=3, **info)
        return buf.getvalue()


def _same_pixels(a: bytes, b: bytes) -> bool:
    """True if both PNGs carry exactly the same image.
    Compares the decompressed scanlines (works even when `a` has CRC errors,
    which Pillow refuses to open); falls back to a Pillow pixel comparison
    when the encodings differ (force/re-encode mode)."""
    pa, pb = _parse_chunks(a), _parse_chunks(b)
    if pa.ihdr is None or pb.ihdr is None or pa.ihdr[:13] != pb.ihdr[:13]:
        return False
    ra, _ = _inflate(pa.idat)
    rb, _ = _inflate(pb.idat)
    if pa.interlace == 0:
        _bpp, stride = _row_geometry(pa)
        n = (stride + 1) * pa.height
        if ra[:n] == rb[:n] and len(rb) == n:
            return True
    elif ra == rb:
        return True
    from PIL import Image
    try:
        with Image.open(io.BytesIO(a)) as x, Image.open(io.BytesIO(b)) as y:
            return x.size == y.size and x.mode == y.mode and x.tobytes() == y.tobytes()
    except Exception:
        return False


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


def _sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


class ChangedError(Exception):
    """The tile was rewritten (by GEOlayers) while we were working on it."""


def _atomic_write(path: Path, data: bytes, expect_sig=None, retries: int = 20) -> None:
    tmp = path.with_name(path.name + ".glfix.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        for i in range(retries):
            if expect_sig is not None and _sig(path) != expect_sig:
                raise ChangedError()
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # File locked (AE or GEOlayers reading it) -> retry
                time.sleep(0.15 * (i + 1))
        raise PermissionError("file is locked by another program")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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


def _quarantine_name(name: str) -> str:
    """'<ms>_<original name>' - strips timestamp prefixes added by earlier
    versions and caps the length (Windows paths max out at 260 chars)."""
    import re
    base = re.sub(r"^(\d{10,13}_)+", "", name)
    if len(base) > 120:
        stem, ext = os.path.splitext(base)
        base = stem[-(120 - len(ext)):] + ext
    return f"{int(time.time() * 1000)}_{base}"


def quarantine(path: Path, qdir: Path, retries: int = 12) -> str:
    """Move a broken tile out of GEOlayers' folders. After Effects often still
    has the file open right after failing on it (WinError 32), so keep trying
    for ~10 s before giving up."""
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / _quarantine_name(path.name)
    for i in range(retries):
        try:
            os.replace(path, dest)
            break
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(0.15 * (i + 1))
        except OSError:
            import shutil  # different drive -> copy + delete
            shutil.move(str(path), str(dest))
            break
    os.utime(dest)  # age counts from quarantine time
    return str(dest)


def glpatch_status() -> list[tuple[str, str]]:
    """[(extension folder, 'v4' | 'v3' | 'none' | 'unknown')] for every GEOlayers
    CEP install found. GL-PATCH v4 stops broken tiles before they reach disk."""
    roots = [os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Common Files", "Adobe", "CEP", "extensions"),
             os.path.join(os.environ.get("ProgramFiles", ""), "Common Files", "Adobe", "CEP", "extensions"),
             os.path.join(os.environ.get("APPDATA", ""), "Adobe", "CEP", "extensions")]
    out = []
    for r in roots:
        try:
            dirs = [os.path.join(r, d) for d in os.listdir(r)]
        except OSError:
            continue
        for d in dirs:
            libs = os.path.join(d, "js", "libs.js")
            man = os.path.join(d, "CSXS", "manifest.xml")
            try:
                with open(man, encoding="utf-8", errors="replace") as f:
                    if "geolayers" not in f.read().lower():
                        continue
                with open(libs, "rb") as f:
                    head = f.read(20)
            except OSError:
                continue
            state = ("v4.1" if head.startswith(b"/* GL-PATCH v4.1") else
                     "v4.0" if head.startswith(b"/* GL-PATCH v4") else
                     "v3" if head.startswith(b"/* GL-PATCH v3") else
                     "none" if not head.startswith(b"/* GL-PATCH") else "unknown")
            out.append((d, state))
    return out


def process_file(path: str, force: bool = False, dry_run: bool = False,
                 wait_stable: float = 0.0, quarantine_to: str | None = None) -> Result:
    """Inspect one PNG and fix it if needed. Safe to run in a worker process.

    status: ok | repaired | reencoded | quarantined | failed | skipped
    """
    p = Path(path)
    try:
        if wait_stable and not _wait_stable(p, wait_stable):
            return Result(path, "skipped", message="file vanished or empty")
        sig0 = _sig(p)
        data = p.read_bytes()
        if _sig(p) != sig0 or len(data) != sig0[0]:
            return Result(path, "skipped", message="file is still being written")
    except OSError as e:
        return Result(path, "skipped", message=str(e))

    if not data.startswith(PNG_SIG):
        return Result(path, "skipped", message="not a PNG")

    problems = inspect_bytes(data)
    if not problems and not force:
        return Result(path, "ok", sig=sig0)
    if dry_run:
        return Result(path, "failed" if problems else "ok", problems, "(scan only)",
                      sig=None if problems else sig0)

    changed = Result(path, "skipped", problems, "tile was rewritten while fixing; will re-check")

    if needs_redownload(problems):
        try:
            if _sig(p) != sig0:
                return changed
            dest = quarantine(p, Path(quarantine_to) if quarantine_to else quarantine_dir())
        except OSError as e:
            return Result(path, "failed", problems, f"could not move to quarantine: {e}")
        return Result(path, "quarantined", problems,
                      f"moved to {dest} - GEOlayers will download it again")

    try:
        if problems:
            out = _rebuild_lossless(_parse_chunks(data))
        else:  # force mode on a clean tile
            out = _reencode(data)
        if inspect_bytes(out) or not _same_pixels(data, out):
            raise ValueError("rebuilt tile did not verify")
    except Exception as e:
        return Result(path, "failed", problems, f"could not rebuild ({e})")

    try:
        _atomic_write(p, out, expect_sig=sig0)
        new_sig = _sig(p)
    except ChangedError:
        return changed
    except Exception as e:
        return Result(path, "failed", problems, f"write failed: {e}")
    return Result(path, "repaired" if problems else "reencoded", problems, sig=new_sig)


_NOT_PNG_EXT = (".json", ".txt", ".log", ".jpg", ".jpeg", ".webp", ".pbf", ".mvt",
                ".tif", ".tiff", ".zip", ".jsx", ".aep", ".aet", ".lic", ".shp", ".dbf")


def iter_pngs(folders: list[str], since: float = 0.0, clean_tmp: bool = True):
    """Yield every PNG under the given folders (by signature, any extension).
    Quarantine folders and the app's own folder are skipped. Leftover temp
    files from an interrupted repair (older than 1 hour) are deleted."""
    seen = set()
    stale = time.time() - 3600
    for folder in folders:
        root = Path(os.path.expandvars(folder))
        if not root.is_dir() or is_excluded(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIR_NAMES]
            for name in files:
                low = name.lower()
                fp = os.path.join(dirpath, name)
                if low.endswith(".glfix.tmp"):
                    if clean_tmp:
                        try:
                            if os.path.getmtime(fp) < stale:
                                os.unlink(fp)
                        except OSError:
                            pass
                    continue
                if low.endswith(_NOT_PNG_EXT):
                    continue
                key = os.path.normcase(fp)
                if key in seen:
                    continue
                if since:
                    try:
                        if os.path.getmtime(fp) < since:
                            continue
                    except OSError:
                        continue
                if low.endswith(".png") or is_png_file(fp):
                    seen.add(key)
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
