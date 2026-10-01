"""Parallel batch runner + filesystem watcher."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from .core import (Result, app_dir, is_excluded, is_incomplete, is_png_file,
                   iter_pngs, process_file)

HIGH_PRIORITY_CLASS = 0x00000080
# Peak RAM one worker can need for a 4096x4096 RGBA tile (raw + rebuilt + zlib)
_RAM_PER_WORKER = 600 * 1024 * 1024


def set_high_priority() -> None:
    """Give this process high CPU priority (Windows only, no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), HIGH_PRIORITY_CLASS)
    except Exception:
        pass


def available_ram() -> int:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullAvailPhys)
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 8 * 1024 ** 3


def pick_workers() -> int:
    """All CPU threads, but never more than free RAM can hold (AE needs RAM too:
    we only use half of what is free)."""
    cpu = os.cpu_count() or 1
    by_ram = max(1, (available_ram() // 2) // _RAM_PER_WORKER)
    return max(1, min(cpu, by_ram))


def _worker_init(high: bool) -> None:
    if high:
        set_high_priority()


# ---------------------------------------------------------------------------
# Verified cache: tiles already proven clean are skipped until they change.
# Makes "Fix Tiles Now" near-instant on repeat runs.
# ---------------------------------------------------------------------------

class VerifiedCache:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), "verified.json")
        self.data: dict[str, list] = {}
        self._lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    @staticmethod
    def _sig(path):
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]

    def is_clean(self, path) -> bool:
        try:
            return self.data.get(path) == self._sig(path)
        except OSError:
            return False

    def mark_clean(self, path):
        try:
            sig = self._sig(path)
        except OSError:
            return
        with self._lock:
            self.data[path] = sig

    def forget(self, path):
        with self._lock:
            self.data.pop(path, None)

    def save(self):
        with self._lock:
            self.data = {k: v for k, v in self.data.items() if os.path.exists(k)}
            snapshot = dict(self.data)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


def run_batch(folders, force=False, dry_run=False, since=0.0, workers=None,
              high_priority=True, on_result=None, on_progress=None,
              cancel: threading.Event | None = None,
              cache: VerifiedCache | None = None) -> Counter:
    """Scan/fix all PNGs using every CPU core. Returns a Counter of statuses
    ('cached' = skipped because already verified clean and unchanged)."""
    stats: Counter = Counter()
    paths = []
    for p in iter_pngs(folders, since=since):
        if cache is not None and not force and cache.is_clean(p):
            stats["cached"] += 1
        else:
            paths.append(p)
    total = len(paths)
    if on_progress:
        on_progress(0, total)
    if not total:
        return stats

    workers = workers or pick_workers()
    # Small jobs: threads avoid process start-up cost
    if total < 16:
        pool = ThreadPoolExecutor(max_workers=min(workers, total))
    else:
        pool = ProcessPoolExecutor(max_workers=min(workers, total), initializer=_worker_init,
                                   initargs=(high_priority,))
    done = 0
    with pool:
        futs = {pool.submit(process_file, p, force, dry_run): p for p in paths}
        for fut in as_completed(futs):
            if cancel and cancel.is_set():
                for f in futs:
                    f.cancel()
                break
            try:
                r: Result = fut.result()
            except Exception as e:  # worker crashed on one file
                r = Result(futs[fut], "failed", message=str(e))
            stats[r.status] += 1
            if cache is not None:
                if r.status in ("ok", "repaired", "reencoded"):
                    cache.mark_clean(r.path)
                else:
                    cache.forget(r.path)
            done += 1
            if on_result and r.status not in ("ok",):
                on_result(r)
            if on_progress and (done % 10 == 0 or done == total):
                on_progress(done, total)
    if cache is not None:
        cache.save()
    return stats


class Watcher:
    """Watches folders; every new/changed PNG is checked & fixed as soon as
    GEOlayers finishes writing it. A periodic rescan catches anything the
    OS change notifications missed (Windows drops events under heavy load)."""

    RESCAN_EVERY = 45.0

    def __init__(self, folders, on_result, cache: VerifiedCache | None = None):
        self.folders = [os.path.expandvars(f) for f in folders]
        self.on_result = on_result
        self.cache = cache
        self._q: queue.Queue[str] = queue.Queue()
        self._pending: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._observer = None
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(2, min(8, pick_workers())))
        self._last_scan = time.time()

    def start(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        q = self._q

        class H(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.is_directory or ev.event_type not in ("created", "modified", "moved"):
                    return
                path = getattr(ev, "dest_path", "") or ev.src_path
                if path.endswith(".glfix.tmp") or is_excluded(path):
                    return
                q.put(path)

        self._observer = Observer()
        for f in self.folders:
            os.makedirs(f, exist_ok=True)
            self._observer.schedule(H(), f, recursive=True)
        self._observer.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                p = self._q.get(timeout=0.2)
                self._pending[p] = time.time()
            except queue.Empty:
                pass
            now = time.time()
            # Debounce: process a file 0.6s after its last change event
            for p, t in list(self._pending.items()):
                if now - t >= 0.6:
                    del self._pending[p]
                    self._submit(p)
            # Safety net: rescan files changed since last pass
            if now - self._last_scan >= self.RESCAN_EVERY:
                since = self._last_scan - 5
                self._last_scan = now
                if self.cache is not None:
                    self.cache.save()
                try:
                    for p in iter_pngs(self.folders, since=since):
                        if p not in self._pending:
                            self._submit(p)
                except Exception:
                    pass

    def _submit(self, path):
        with self._lock:
            if path in self._inflight:
                return
            self._inflight.add(path)
        self._pool.submit(self._handle, path)

    def _handle(self, path):
        try:
            if not path.lower().endswith(".png") and not is_png_file(path):
                return
            if self.cache is not None and self.cache.is_clean(path):
                return
            r = process_file(path, dry_run=True, wait_stable=3.0)
            if r.status == "ok" or not r.problems:
                if r.status == "ok" and self.cache is not None:
                    self.cache.mark_clean(path)
                return
            if is_incomplete(r.problems):
                # Maybe GEOlayers is still writing: give it more time before acting
                time.sleep(8)
            r = process_file(path, wait_stable=2.0)
            if self.cache is not None:
                if r.status in ("ok", "repaired"):
                    self.cache.mark_clean(path)
                else:
                    self.cache.forget(path)
            if r.status in ("repaired", "quarantined", "failed"):
                self.on_result(r)
        except Exception:
            pass
        finally:
            with self._lock:
                self._inflight.discard(path)

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.cache is not None:
            self.cache.save()
