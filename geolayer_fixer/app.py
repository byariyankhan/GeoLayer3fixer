"""GeoLayer Fixer v2 - Windows GUI."""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
from logging.handlers import RotatingFileHandler
from tkinter import filedialog

import customtkinter as ctk

from . import __version__
from .core import default_folders
from .runner import Watcher, run_batch, set_high_priority

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GeoLayerFixer")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "geolayer_fixer.log")
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_NAME = "GeoLayer3Fixer"

C_BG, C_CARD = "#1A1A2E", "#16213E"
C_GREEN, C_RED, C_ORANGE, C_BLUE, C_GRAY = "#1DB954", "#E74C3C", "#F39C12", "#0078D4", "#888888"

os.makedirs(APP_DIR, exist_ok=True)
log = logging.getLogger("glfix")
log.setLevel(logging.INFO)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_fh)


def load_config() -> dict:
    cfg = {"folders": default_folders(), "force": False, "high_priority": True,
           "watch_on_start": True}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _exe_cmd() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" -m geolayer_fixer --minimized'


def is_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            winreg.QueryValueEx(k, REG_NAME)
        return True
    except Exception:
        return False


def set_autostart(on: bool) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, REG_NAME, 0, winreg.REG_SZ, _exe_cmd())
            else:
                try:
                    winreg.DeleteValue(k, REG_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        log.info(f"autostart error: {e}")


class App(ctk.CTk):
    def __init__(self, minimized=False):
        super().__init__()
        self.cfg = load_config()
        if self.cfg.get("high_priority"):
            set_high_priority()
        self.title(f"GeoLayer Fixer v{__version__}")
        self.geometry("560x760")
        self.minsize(480, 640)
        self.configure(fg_color=C_BG)
        self._ui_q: queue.Queue = queue.Queue()
        self._watcher: Watcher | None = None
        self._cancel = threading.Event()
        self._busy = False
        self._watch_counts = {"repaired": 0, "failed": 0}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._pump)
        self._log(f"GeoLayer Fixer v{__version__} - {os.cpu_count()} CPU threads available")
        for f in self.cfg["folders"]:
            exists = os.path.isdir(os.path.expandvars(f))
            self._log(f"Folder: {f}" + ("" if exists else "  (NOT FOUND!)"))
        if self.cfg.get("watch_on_start"):
            self._toggle_watch()
        if minimized:
            self.iconify()

    # ---------------- UI ----------------
    def _card(self, title, subtitle=None):
        card = ctk.CTkFrame(self, fg_color=C_CARD, corner_radius=12)
        card.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="white", anchor="w").pack(fill="x", padx=16, pady=(12, 0))
        if subtitle:
            ctk.CTkLabel(card, text=subtitle, text_color="#AAAAAA", anchor="w",
                         justify="left", wraplength=480).pack(fill="x", padx=16)
        return card

    def _build(self):
        ctk.CTkLabel(self, text="GeoLayer Fixer", font=ctk.CTkFont(size=24, weight="bold"),
                     text_color="white").pack(pady=(14, 0))
        ctk.CTkLabel(self, text="After Effects 2025/2026 PNG Fix  •  v2", text_color=C_GRAY).pack()

        # Folders
        c = self._card("Tile Folders", "All subfolders are scanned. PNGs are detected by content, not extension.")
        self.folder_box = ctk.CTkTextbox(c, height=60, fg_color="#0F1626", text_color="#CCCCCC")
        self.folder_box.pack(fill="x", padx=16, pady=6)
        self._refresh_folders()
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(row, text="Add folder", width=110, fg_color="#333355",
                      command=self._add_folder).pack(side="left")
        ctk.CTkButton(row, text="Reset", width=80, fg_color="#333355",
                      command=self._reset_folders).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Open folder", width=100, fg_color="#333355",
                      command=self._open_folder).pack(side="left")

        # Watcher
        c = self._card("Auto Watcher", "Checks every tile GEOlayers writes and repairs broken ones instantly.")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=6)
        self.watch_dot = ctk.CTkLabel(row, text="● Inactive", text_color=C_RED)
        self.watch_dot.pack(side="left")
        self.watch_count = ctk.CTkLabel(row, text="Repaired: 0   Failed: 0", text_color=C_GRAY)
        self.watch_count.pack(side="right")
        self.watch_btn = ctk.CTkButton(c, text="Activate Watcher", height=38, fg_color=C_GREEN,
                                       hover_color="#17a045", command=self._toggle_watch)
        self.watch_btn.pack(fill="x", padx=16, pady=(0, 12))

        # Fix
        c = self._card("Fix Before Render", "Scan checks only. Fix repairs broken tiles using all CPU cores.")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=6)
        self.scan_btn = ctk.CTkButton(row, text="Scan Only", height=38, fg_color="#444477",
                                      command=lambda: self._run(dry=True))
        self.scan_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.fix_btn = ctk.CTkButton(row, text="Fix Tiles Now", height=38, fg_color=C_BLUE,
                                     hover_color="#005fa3", command=lambda: self._run(dry=False))
        self.fix_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self.progress = ctk.CTkProgressBar(c, progress_color=C_BLUE)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=4)
        opts = ctk.CTkFrame(c, fg_color="transparent")
        opts.pack(fill="x", padx=16, pady=(0, 12))
        self.force_var = ctk.BooleanVar(value=self.cfg.get("force", False))
        ctk.CTkCheckBox(opts, text="Force: re-encode ALL tiles", variable=self.force_var,
                        command=self._save_opts).pack(anchor="w")
        self.prio_var = ctk.BooleanVar(value=self.cfg.get("high_priority", True))
        ctk.CTkCheckBox(opts, text="High CPU priority", variable=self.prio_var,
                        command=self._save_opts).pack(anchor="w", pady=2)
        self.auto_var = ctk.BooleanVar(value=is_autostart())
        ctk.CTkCheckBox(opts, text="Start with Windows (watcher on)", variable=self.auto_var,
                        command=lambda: set_autostart(self.auto_var.get())).pack(anchor="w")

        # Log
        self.log_box = ctk.CTkTextbox(self, fg_color="#0F1626", text_color="#BBBBBB",
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True, padx=18, pady=(6, 14))

    # ---------------- helpers ----------------
    def _log(self, msg):
        log.info(msg)
        self._ui_q.put(("log", msg))

    def _pump(self):
        try:
            while True:
                kind, val = self._ui_q.get_nowait()
                if kind == "log":
                    self.log_box.insert("end", val + "\n")
                    self.log_box.see("end")
                elif kind == "progress":
                    done, total = val
                    self.progress.set(done / total if total else 1)
                elif kind == "watchcount":
                    self.watch_count.configure(
                        text=f"Repaired: {self._watch_counts['repaired']}   Failed: {self._watch_counts['failed']}")
                elif kind == "done":
                    self._finish(val)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _refresh_folders(self):
        self.folder_box.configure(state="normal")
        self.folder_box.delete("1.0", "end")
        self.folder_box.insert("end", "\n".join(self.cfg["folders"]))
        self.folder_box.configure(state="disabled")

    def _add_folder(self):
        d = filedialog.askdirectory(title="Select a GEOlayers tiles / cache folder")
        if d and d not in self.cfg["folders"]:
            self.cfg["folders"].append(os.path.normpath(d))
            save_config(self.cfg)
            self._refresh_folders()
            self._restart_watch()

    def _reset_folders(self):
        self.cfg["folders"] = default_folders()
        save_config(self.cfg)
        self._refresh_folders()
        self._restart_watch()

    def _open_folder(self):
        for f in self.cfg["folders"]:
            p = os.path.expandvars(f)
            if os.path.isdir(p) and hasattr(os, "startfile"):
                os.startfile(p)
                return

    def _save_opts(self):
        self.cfg["force"] = self.force_var.get()
        self.cfg["high_priority"] = self.prio_var.get()
        save_config(self.cfg)
        if self.cfg["high_priority"]:
            set_high_priority()

    # ---------------- watcher ----------------
    def _on_watch_result(self, r):
        self._watch_counts["repaired" if r.status == "repaired" else "failed"] += 1
        name = os.path.basename(r.path)
        if r.status == "repaired":
            self._log(f"[watch] FIXED {name}: {'; '.join(r.problems)}")
        else:
            self._log(f"[watch] FAILED {name}: {r.message} ({'; '.join(r.problems)})")
        self._ui_q.put(("watchcount", None))

    def _toggle_watch(self):
        if self._watcher:
            self._watcher.stop()
            self._watcher = None
            self.cfg["watch_on_start"] = False
            self.watch_dot.configure(text="● Inactive", text_color=C_RED)
            self.watch_btn.configure(text="Activate Watcher", fg_color=C_GREEN, hover_color="#17a045")
            self._log("Watcher stopped.")
        else:
            try:
                self._watcher = Watcher(self.cfg["folders"], self._on_watch_result)
                self._watcher.start()
            except Exception as e:
                self._watcher = None
                self._log(f"Watcher error: {e}")
                return
            self.cfg["watch_on_start"] = True
            self.watch_dot.configure(text="● Active", text_color=C_GREEN)
            self.watch_btn.configure(text="Deactivate Watcher", fg_color=C_RED, hover_color="#b03030")
            self._log("Watcher running.")
        save_config(self.cfg)

    def _restart_watch(self):
        if self._watcher:
            self._toggle_watch()
            self._toggle_watch()

    # ---------------- batch ----------------
    def _run(self, dry):
        if self._busy:
            self._cancel.set()
            self._log("Cancelling...")
            return
        self._busy = True
        self._cancel.clear()
        force = self.force_var.get() and not dry
        self.progress.set(0)
        (self.scan_btn if dry else self.fix_btn).configure(text="Cancel", fg_color=C_ORANGE)
        self._log(("Scanning" if dry else "Fixing" + (" (FORCE)" if force else "")) + " all tiles...")

        def on_result(r):
            name = os.path.basename(r.path)
            if r.status == "repaired":
                self._log(f"FIXED  {name}: {'; '.join(r.problems)}")
            elif r.status == "failed":
                self._log(f"BROKEN {name}: {'; '.join(r.problems)} {r.message}")

        def work():
            try:
                stats = run_batch(self.cfg["folders"], force=force, dry_run=dry,
                                  high_priority=self.prio_var.get(), on_result=on_result,
                                  on_progress=lambda d, t: self._ui_q.put(("progress", (d, t))),
                                  cancel=self._cancel)
            except Exception as e:
                stats = None
                self._log(f"Error: {e}")
            self._ui_q.put(("done", (dry, stats)))

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, val):
        dry, stats = val
        self._busy = False
        self.scan_btn.configure(text="Scan Only", fg_color="#444477")
        self.fix_btn.configure(text="Fix Tiles Now", fg_color=C_BLUE)
        if stats is None:
            return
        total = sum(stats.values())
        if total == 0:
            self._log("No PNG tiles found! Check the folder list - GEOlayers may store tiles elsewhere.")
            return
        if dry:
            self._log(f"Scan done: {total} tiles, {stats['ok']} clean, {stats['failed']} BROKEN.")
        else:
            self._log(f"Done: {total} tiles | clean {stats['ok']} | repaired {stats['repaired']} | "
                      f"re-encoded {stats['reencoded']} | failed {stats['failed']} | skipped {stats['skipped']}")
            if stats["failed"]:
                self._log("Failed tiles can't be rebuilt: delete them and let GEOlayers re-download.")
            self._log("Now in AE: Edit > Purge > All Memory & Disk Cache, then render.")

    def _close(self):
        self._cancel.set()
        if self._watcher:
            self._watcher.stop()
        self.destroy()


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    # Windowed exe has no console: print() would crash without this
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    args = sys.argv[1:]
    if args and args[0] in ("--scan", "--fix"):
        from .cli import main as cli_main
        sys.exit(cli_main(args))
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App(minimized="--minimized" in args).mainloop()
