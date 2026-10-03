import io
import os
import struct
import zlib

import pytest
from PIL import Image

from geolayer_fixer.core import (PNG_SIG, _parse_chunks, inspect_bytes,
                                 iter_pngs, process_file)


def make_png(w=64, h=64, mode="RGBA"):
    im = Image.new(mode, (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 4 % 256, y * 4 % 256, 128, 255)[: len(mode)]
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def chunk(t, body):
    return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body))


def with_raw(data, mutate):
    """Rebuild PNG after mutating the decompressed scanline bytes."""
    p = _parse_chunks(data)
    raw = bytearray(zlib.decompress(bytes(p.idat)))
    mutate(raw)
    return PNG_SIG + chunk(b"IHDR", p.ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def bad_filter(data):
    row_len = 64 * 4 + 1

    def m(raw):
        for r in (3, 10, 40):
            raw[r * row_len] = 0x9C
    return with_raw(data, m)


def bad_crc(data):
    """Pixel data intact, only the IDAT CRC field is wrong ("IDAT: CRC error")."""
    b = bytearray(data)
    i = b.find(b"IDAT")
    length = struct.unpack(">I", b[i - 4:i])[0]
    b[i + 4 + length] ^= 0xFF
    return bytes(b)


def corrupt_stream(data):
    b = bytearray(data)
    i = b.find(b"IDAT")
    b[i + 6] ^= 0xFF  # damage compressed data itself
    return bytes(b)


def test_corrupt_stream_quarantined(tmp_path):
    f = tmp_path / "tiles" / "c.png"
    f.parent.mkdir()
    f.write_bytes(corrupt_stream(make_png()))
    assert process_file(str(f)).status == "quarantined"


def truncated(data):
    return data[: len(data) // 2]


def pillow_strict_ok(data):
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return im.size


def test_clean_png_has_no_problems():
    assert inspect_bytes(make_png()) == []


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Quarantine + cache go to APPDATA\\GeoLayerFixer: keep tests isolated."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path / "appdata" / "GeoLayerFixer"


def test_truncated_is_quarantined_not_padded(tmp_path, isolated_appdata):
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    f = tiles / "t.png"
    f.write_bytes(truncated(make_png()))
    r = process_file(str(f))
    assert r.status == "quarantined", r
    assert not f.exists()
    assert len(list((isolated_appdata / "quarantine").iterdir())) == 1


def zero_len_idat(data):
    """What gl-patch logged: a 0-length IDAT with wrong CRC followed by zeros."""
    i = data.find(b"IDAT") - 4
    return data[:i] + b"\x00\x00\x00\x00IDAT" + b"\x00" * 64


def test_geolayers_zero_len_idat_quarantined(tmp_path):
    f = tmp_path / "tiles" / "z.png"
    f.parent.mkdir()
    f.write_bytes(zero_len_idat(make_png()))
    assert process_file(str(f)).status == "quarantined"


def pixels(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.mode, im.size, im.tobytes()


def extra_data(data):
    """Junk after the zlib stream inside IDAT (libpng: 'Extra compressed data')."""
    p = _parse_chunks(data)
    return (PNG_SIG + chunk(b"IHDR", p.ihdr) + chunk(b"IDAT", bytes(p.idat) + b"JUNKJUNK")
            + chunk(b"IEND", b""))


def split_idat(data):
    """IDAT run interrupted by another chunk ('Too many IDATs found')."""
    p = _parse_chunks(data)
    z = bytes(p.idat)
    h = len(z) // 2
    return (PNG_SIG + chunk(b"IHDR", p.ihdr) + chunk(b"IDAT", z[:h]) + chunk(b"tEXt", b"a\x00b")
            + chunk(b"IDAT", z[h:]) + chunk(b"IEND", b""))


@pytest.mark.parametrize("breaker,needle", [
    (bad_crc, "CRC"),
    (extra_data, "extra data"),
    (split_idat, "not consecutive"),
])
def test_lossless_repair_keeps_every_pixel(tmp_path, breaker, needle):
    good = make_png()
    broken = breaker(good)
    probs = inspect_bytes(broken)
    assert needle in " ".join(probs), probs

    f = tmp_path / "12_345_678.png"
    f.write_bytes(broken)
    r = process_file(str(f))
    assert r.status == "repaired", r
    fixed = f.read_bytes()
    assert inspect_bytes(fixed) == []
    assert pillow_strict_ok(fixed) == (64, 64)
    assert pixels(fixed) == pixels(good)          # byte-identical image
    assert r.sig == (f.stat().st_size, f.stat().st_mtime_ns)
    assert not list(tmp_path.glob("*.tmp"))


def test_bad_filter_is_quarantined_not_patched(tmp_path, isolated_appdata):
    """Patching filter bytes leaves visible stripes (errors spread through
    Up/Paeth rows) -> the tile must be re-downloaded instead."""
    f = tmp_path / "tiles" / "b.png"
    f.parent.mkdir()
    f.write_bytes(bad_filter(make_png()))
    r = process_file(str(f))
    assert r.status == "quarantined", r
    assert "bad adaptive filter" in " ".join(r.problems)
    assert not f.exists()


def test_tile_rewritten_during_repair_is_not_overwritten(tmp_path, monkeypatch):
    """GEOlayers re-writes a tile while we repair the old copy: keep the new one."""
    import geolayer_fixer.core as core
    f = tmp_path / "t.png"
    f.write_bytes(bad_crc(make_png()))
    newer = make_png(32, 32)
    real = core._rebuild_lossless

    def slow_rebuild(p):
        out = real(p)
        f.write_bytes(newer)                 # GEOlayers writes a fresh tile
        os.utime(f, ns=(1, 1))               # guarantee a different signature
        return out

    monkeypatch.setattr(core, "_rebuild_lossless", slow_rebuild)
    r = process_file(str(f))
    assert r.status == "skipped" and "rewritten" in r.message
    assert f.read_bytes() == newer


def test_quarantine_respects_concurrent_rewrite(tmp_path, monkeypatch):
    import geolayer_fixer.core as core
    f = tmp_path / "tiles" / "q.png"
    f.parent.mkdir()
    f.write_bytes(truncated(make_png()))
    newer = make_png()
    real = core.inspect_bytes
    calls = {"n": 0}

    def inspect_then_rewrite(data):
        res = real(data)
        calls["n"] += 1
        if calls["n"] == 1:
            f.write_bytes(newer)
            os.utime(f, ns=(1, 1))
        return res

    monkeypatch.setattr(core, "inspect_bytes", inspect_then_rewrite)
    r = process_file(str(f))
    assert r.status == "skipped"
    assert f.read_bytes() == newer


def test_clean_file_untouched(tmp_path):
    f = tmp_path / "a.png"
    data = make_png()
    f.write_bytes(data)
    assert process_file(str(f)).status == "ok"
    assert f.read_bytes() == data


def test_force_reencodes(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(make_png())
    assert process_file(str(f), force=True).status == "reencoded"
    assert inspect_bytes(f.read_bytes()) == []


def test_dry_run_does_not_write(tmp_path):
    f = tmp_path / "a.png"
    broken = bad_filter(make_png())
    f.write_bytes(broken)
    r = process_file(str(f), dry_run=True)
    assert r.problems and f.read_bytes() == broken


def test_finds_extensionless_png(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tile").write_bytes(make_png())
    (tmp_path / "x.json").write_text("{}")
    found = list(iter_pngs([str(tmp_path)]))
    assert len(found) == 1 and found[0].endswith("tile")


def test_palette_png(tmp_path):
    im = Image.new("P", (32, 32))
    im.putpalette([i % 256 for i in range(768)])
    buf = io.BytesIO()
    im.save(buf, "PNG", transparency=0)
    f = tmp_path / "p.png"
    f.write_bytes(bad_crc(buf.getvalue()))
    assert process_file(str(f)).status == "repaired"
    assert inspect_bytes(f.read_bytes()) == []


def test_quarantine_name_never_grows(tmp_path, isolated_appdata):
    long = "_".join(["1790894380"] * 20) + "_esri-muowx25av9p7l_1024_6_12021.png"
    f = tmp_path / "tiles" / long
    f.parent.mkdir()
    f.write_bytes(truncated(make_png()))
    r = process_file(str(f))
    assert r.status == "quarantined", r
    (q,) = (isolated_appdata / "quarantine").iterdir()
    assert q.name.endswith("_esri-muowx25av9p7l_1024_6_12021.png")
    assert len(q.name) == 14 + len("esri-muowx25av9p7l_1024_6_12021.png")


def test_quarantine_retries_while_file_locked(tmp_path, isolated_appdata, monkeypatch):
    """AE keeps a tile open right after failing on it (WinError 32)."""
    import geolayer_fixer.core as core
    f = tmp_path / "tiles" / "locked.png"
    f.parent.mkdir()
    f.write_bytes(truncated(make_png()))
    real, calls = os.replace, {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(32, "being used by another process")
        return real(a, b)

    monkeypatch.setattr(core.os, "replace", flaky_replace)
    r = process_file(str(f))
    assert r.status == "quarantined", r
    assert calls["n"] == 4


def test_glpatch_status(tmp_path, monkeypatch):
    from geolayer_fixer.core import glpatch_status
    ext = tmp_path / "pf" / "Common Files" / "Adobe" / "CEP" / "extensions"
    for name, head in (("GEO v41", "/* GL-PATCH v4.1 x */"), ("GEO v4", "/* GL-PATCH v4 x */"),
                       ("GEO v3", "/* GL-PATCH v3 x */"), ("GEO plain", "!function(){}")):
        (ext / name / "js").mkdir(parents=True)
        (ext / name / "CSXS").mkdir()
        (ext / name / "CSXS" / "manifest.xml").write_text("<X Name='GEOlayers 3'/>")
        (ext / name / "js" / "libs.js").write_text(head)
    (ext / "Other" / "CSXS").mkdir(parents=True)
    (ext / "Other" / "CSXS" / "manifest.xml").write_text("<X Name='Lottie'/>")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf"))
    st = {os.path.basename(d): s for d, s in glpatch_status()}
    assert st == {"GEO v41": "v4.1", "GEO v4": "v4.0", "GEO v3": "v3", "GEO plain": "none"}
