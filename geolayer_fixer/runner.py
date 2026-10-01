"""Parallel batch runner + filesystem watcher."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from .core import Result, is_incomplete, is_png_file, iter_pngs, process_file

HIGH_PRIORITY_CLASS = 0x00000080


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


def _worker_init(high: bool) -> None:
    if high:
        set_high_priority()


def run_batch(folders, force=False, dry_run=False, since=0.0, workers=None,
              high_priority=True, on_result=None, on_progress=None,
              cancel: threading.Event | None = None) -> Counter:
    """Scan/fix all PNGs using every CPU core. Returns a Counter of statuses."""
    paths = list(iter_pngs(folders, since=since))
    total = len(paths)
    stats: Counter = Counter()
    if on_progress:
        on_progress(0, total)
    if not total:
        return stats

    workers = workers or max(1, os.cpu_count() or 1)
    # Small jobs: threads avoid process start-up cost
    if total < 64:
        pool = ThreadPoolExecutor(max_workers=min(workers, total))
    else:
        pool = ProcessPoolExecutor(max_workers=workers, initializer=_worker_init,
                                   initargs=(high_priority,))
    done = 0
    with pool:
        futs = [pool.submit(process_file, p, force, dry_run) for p in paths]
        for fut in as_completed(futs):
            if cancel and cancel.is_set():
                for f in futs:
                    f.cancel()
                break
            try:
                r: Result = fut.result()
            except Exception as e:  # worker crashed on one file
                r = Result("?", "failed", message=str(e))
            stats[r.status] += 1
            done += 1
            if on_result and r.status != "ok":
                on_result(r)
            if on_progress and (done % 25 == 0 or done == total):
                on_progress(done, total)
    return stats


class Watcher:
    """Watches folders; every new/changed PNG is checked & fixed as soon as
    GEOlayers finishes writing it."""

    def __init__(self, folders, on_result):
        self.folders = [os.path.expandvars(f) for f in folders]
        self.on_result = on_result
        self._q: queue.Queue[str] = queue.Queue()
        self._pending: dict[str, float] = {}
        self._observer = None
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 2) // 2))

    def start(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        q = self._q

        class H(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.is_directory or ev.event_type not in ("created", "modified", "moved"):
                    return
                path = getattr(ev, "dest_path", "") or ev.src_path
                if path.endswith(".glfix.tmp"):
                    return
                q.put(path)

        self._observer = Observer()
        for f in self.folders:
            os.makedirs(f, exist_ok=True)
            self._observer.schedule(H(), f, recursive=True)
        self._observer.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        # Debounce: process a file 0.6s after its last change event
        while not self._stop.is_set():
            try:
                p = self._q.get(timeout=0.2)
                self._pending[p] = time.time()
            except queue.Empty:
                pass
            now = time.time()
            for p, t in list(self._pending.items()):
                if now - t >= 0.6:
                    del self._pending[p]
                    self._pool.submit(self._handle, p)

    def _handle(self, path):
        if not path.lower().endswith(".png") and not is_png_file(path):
            return
        r = process_file(path, dry_run=True, wait_stable=3.0)
        if r.status == "ok" or not r.problems:
            return
        if is_incomplete(r.problems):
            # Maybe GEOlayers is still writing: give it more time before acting
            time.sleep(8)
        r = process_file(path, wait_stable=2.0)
        if r.status in ("repaired", "quarantined", "failed"):
            self.on_result(r)

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
        self._pool.shutdown(wait=False, cancel_futures=True)
