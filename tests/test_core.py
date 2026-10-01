import io
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


def test_truncated_is_quarantined_not_padded(tmp_path):
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    f = tiles / "t.png"
    f.write_bytes(truncated(make_png()))
    r = process_file(str(f))
    assert r.status == "quarantined", r
    assert not f.exists()
    assert len(list((tmp_path / "corrupt_tiles_quarantine").iterdir())) == 1


def zero_len_idat(data):
    """What gl-patch logged: a 0-length IDAT with wrong CRC followed by zeros."""
    i = data.find(b"IDAT") - 4
    return data[:i] + b"\x00\x00\x00\x00IDAT" + b"\x00" * 64


def test_geolayers_zero_len_idat_quarantined(tmp_path):
    f = tmp_path / "tiles" / "z.png"
    f.parent.mkdir()
    f.write_bytes(zero_len_idat(make_png()))
    assert process_file(str(f)).status == "quarantined"


@pytest.mark.parametrize("breaker,needle", [
    (bad_filter, "bad adaptive filter"),
    (bad_crc, "CRC"),
])
def test_detect_and_repair(tmp_path, breaker, needle):
    broken = breaker(make_png())
    probs = inspect_bytes(broken)
    assert probs, "corruption should be detected"
    assert needle in " ".join(probs)

    f = tmp_path / "12_345_678.png"
    f.write_bytes(broken)
    r = process_file(str(f))
    assert r.status == "repaired", r
    fixed = f.read_bytes()
    assert inspect_bytes(fixed) == []
    assert pillow_strict_ok(fixed) == (64, 64)
    assert not list(tmp_path.glob("*.tmp"))


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
