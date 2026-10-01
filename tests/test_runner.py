import os
import time

import pytest
from geolayer_fixer.core import clean_quarantine, iter_pngs, quarantine_dir
from geolayer_fixer.runner import VerifiedCache, pick_workers, run_batch
from tests.test_core import bad_crc, bad_filter, make_png, truncated


def setup_root(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "GEOlayers3"
    (root / "tiles").mkdir(parents=True)
    return root


def test_quarantined_tiles_are_not_rescanned(tmp_path, monkeypatch):
    """v2.0 bug: quarantine lived inside the scanned folder -> endless loop."""
    root = setup_root(tmp_path, monkeypatch)
    (root / "tiles" / "a.png").write_bytes(truncated(make_png()))
    # old v1 / gl-patch quarantine folder inside GEOlayers3 must be ignored too
    (root / "corrupt_tiles_quarantine").mkdir()
    (root / "corrupt_tiles_quarantine" / "old.png").write_bytes(truncated(make_png()))

    s1 = run_batch([str(root)], workers=1)
    assert s1["quarantined"] == 1
    s2 = run_batch([str(root)], workers=1)
    assert sum(s2.values()) == 0
    assert list(iter_pngs([str(root)])) == []
    assert (root / "corrupt_tiles_quarantine" / "old.png").exists()  # untouched


def test_cache_skips_verified_and_rechecks_changed(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    for i in range(5):
        (root / "tiles" / f"{i}.png").write_bytes(make_png())
    cache = VerifiedCache()
    assert run_batch([str(root)], cache=cache, workers=1)["ok"] == 5

    cache2 = VerifiedCache()  # reload from disk
    s = run_batch([str(root)], cache=cache2, workers=1)
    assert s["cached"] == 5 and s["ok"] == 0

    # tile rewritten broken by GEOlayers -> must be checked again
    f = root / "tiles" / "3.png"
    time.sleep(0.01)
    f.write_bytes(bad_crc(make_png()))
    s = run_batch([str(root)], cache=cache2, workers=1)
    assert s["cached"] == 4 and s["repaired"] == 1
    # repaired tile is now remembered as clean
    assert run_batch([str(root)], cache=cache2, workers=1)["cached"] == 5


def test_force_ignores_cache(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    (root / "tiles" / "a.png").write_bytes(make_png())
    cache = VerifiedCache()
    run_batch([str(root)], cache=cache, workers=1)
    assert run_batch([str(root)], cache=cache, force=True, workers=1)["reencoded"] == 1


def test_parallel_process_pool(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    for i in range(20):
        data = [make_png(), bad_crc(make_png()), bad_filter(make_png())][i % 3]
        (root / "tiles" / f"{i}.png").write_bytes(data)
    s = run_batch([str(root)], workers=2)  # >=16 files -> process pool
    assert (s["ok"], s["repaired"], s["quarantined"]) == (7, 7, 6), s


def test_clean_quarantine(tmp_path, monkeypatch):
    setup_root(tmp_path, monkeypatch)
    q = quarantine_dir()
    q.mkdir(parents=True)
    old, new = q / "old.png", q / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time() - 10 * 86400
    os.utime(old, (t, t))
    assert clean_quarantine(7) == 1
    assert new.exists() and not old.exists()


def test_misc():
    assert pick_workers() >= 1
    _vtuple = pytest.importorskip("geolayer_fixer.app")._vtuple  # needs tkinter
    assert _vtuple("2.10.0") > _vtuple("2.9.1")


def test_stale_tmp_cleaned(tmp_path, monkeypatch):
    root = setup_root(tmp_path, monkeypatch)
    old = root / "tiles" / "x.png.glfix.tmp"
    fresh = root / "tiles" / "y.png.glfix.tmp"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    t = time.time() - 2 * 3600
    os.utime(old, (t, t))
    list(iter_pngs([str(root)]))
    assert not old.exists() and fresh.exists()

