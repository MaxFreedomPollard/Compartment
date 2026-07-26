"""Windows tray app: Compartment in the notification area.

The Windows counterpart of `menubar.py`, and deliberately the same product:
click the icon and a panel shows whether the vault is open, the three settings
worth changing day to day, and the last handful of things it remembered.

Design notes:

* Everything above the widgets is shared. State, settings, locking and the
  first-run marker all come from `menubar`, which keeps no AppKit at module
  level for exactly this reason. One data layer, one set of tests, two
  front ends - the platforms differ only in how a window is drawn.
* State is read by shelling out to the `compartment` CLI rather than opening
  the vault in-process, so an idle tray app is not sitting on an embedding
  model. Same trade as macOS.
* Tk owns the main thread and pystray runs detached. Tk is not thread-safe and
  its mainloop must be on the main thread; pystray's Windows backend is a
  message loop that is happy anywhere. Tray callbacks therefore never touch a
  widget directly - they hand work back with `after(0, ...)`.
* `tkinter` is in the standard library and `pystray` is pure Python, so the
  tray app adds no compiled dependency to a Windows install.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .menubar import (AUTO_LOCK_CHOICES, RECENT_COUNT, auto_lock_label,
                      claim_first_run, default_vault, fetch_state, lock_vault,
                      self_check, set_setting, summarise)

PANEL_WIDTH = 360
PANEL_MAX_HEIGHT = 640
TASKBAR_MARGIN = 56          # room for the taskbar the panel sits above
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Compartment"


def icon_path() -> Path:
    """The tray icon, drawn by tools/make_icon.py and shipped as package data."""
    return Path(__file__).resolve().parent / "data" / "tray.ico"


def panel_rows(state: dict) -> list[tuple[str, str]]:
    """The panel as (kind, text) rows.

    Pure, so the layout is testable on any OS without a display: the Windows
    CI runner checks what the panel *says* without ever drawing it.
    """
    rows: list[tuple[str, str]] = [("state", summarise(state))]
    if state.get("error"):
        rows.append(("error", str(state["error"])))
    s = state["settings"]
    rows.append(("heading", "SETTINGS"))
    rows.append(("toggle:capture_hook",
                 f"Capture hook: {'on' if s['capture_hook'] else 'off'}"))
    rows.append(("toggle:search_starter_facts",
                 f"Search starter facts: "
                 f"{'on' if s['search_starter_facts'] else 'off'}"))
    rows.append(("choice:auto_lock_minutes",
                 f"Auto-lock: {auto_lock_label(s['auto_lock_minutes'])}"))
    rows.append(("heading", f"LAST {RECENT_COUNT} MEMORIES"))
    recent = state.get("recent") or []
    if not recent:
        rows.append(("empty", "Nothing yet."))
    for r in recent:
        rows.append(("memory", (r.get("text") or "").strip()))
    return rows


# --- start at login ---------------------------------------------------------
# The Run key is per-user, needs no elevation and no COM, and is what the
# Startup folder ends up writing anyway. Failure is reported, never raised: a
# refusal to autostart must not take the app down with it.

def _winreg():
    import winreg                                    # Windows-only stdlib
    return winreg


def login_status() -> str:
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return "on"
    except FileNotFoundError:
        return "off"
    except Exception as exc:                          # noqa: BLE001
        return f"unknown ({exc})"


def set_login(enabled: bool) -> str:
    """Register or drop the Run entry. Returns what actually happened."""
    try:
        winreg = _winreg()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  f'"{sys.executable}" -m compartment.cli tray')
                return "on"
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
            return "off"
    except Exception as exc:                          # noqa: BLE001
        return f"error: {exc}"


# --- the app ---------------------------------------------------------------

def run(vault: str | None = None, show: bool = False,
        render_to: str | None = None) -> int:
    vault_path = vault or default_vault()
    if render_to:                                     # parity with --render
        print("error: --render is macOS only", file=sys.stderr)
        return 2

    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("error: this Python has no tkinter, which the tray panel needs.\n"
              "  The python.org installer ships it; some minimal builds do not.",
              file=sys.stderr)
        return 3
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("error: the tray app needs pystray and Pillow.\n"
              "  pip install 'compartment[tray]'", file=sys.stderr)
        return 3

    root = tk.Tk()
    root.withdraw()                                   # no stray empty window
    panel: dict = {"win": None}

    def build(win) -> None:
        for child in win.winfo_children():
            child.destroy()
        state = fetch_state(vault_path)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        s = state["settings"]

        ttk.Label(frame, text=summarise(state), wraplength=PANEL_WIDTH - 40,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        if state.get("error"):
            ttk.Label(frame, text=str(state["error"]), foreground="#b00020",
                      wraplength=PANEL_WIDTH - 40).pack(anchor="w", pady=(4, 0))

        ttk.Separator(frame).pack(fill="x", pady=10)
        ttk.Label(frame, text="SETTINGS",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w")

        hook = tk.BooleanVar(value=s["capture_hook"])
        starter = tk.BooleanVar(value=s["search_starter_facts"])

        def toggle(key, var):
            set_setting(vault_path, key, bool(var.get()))
            refresh()

        ttk.Checkbutton(frame, text="Capture hook", variable=hook,
                        command=lambda: toggle("capture_hook", hook)
                        ).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(frame, text="Search starter facts", variable=starter,
                        command=lambda: toggle("search_starter_facts", starter)
                        ).pack(anchor="w")

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Auto-lock").pack(side="left")
        choice = tk.StringVar(value=auto_lock_label(s["auto_lock_minutes"]))

        def set_lock(label):
            for m in AUTO_LOCK_CHOICES:
                if auto_lock_label(m) == label:
                    set_setting(vault_path, "auto_lock_minutes", m)
                    break
            refresh()

        ttk.OptionMenu(row, choice, choice.get(),
                       *[auto_lock_label(m) for m in AUTO_LOCK_CHOICES],
                       command=set_lock).pack(side="right")

        ttk.Separator(frame).pack(fill="x", pady=10)
        ttk.Label(frame, text=f"LAST {RECENT_COUNT} MEMORIES",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w")
        recent = state.get("recent") or []
        if not recent:
            ttk.Label(frame, text="Nothing yet.",
                      foreground="#666").pack(anchor="w", pady=(4, 0))
        for r in recent:
            ttk.Label(frame, text=(r.get("text") or "").strip(),
                      wraplength=PANEL_WIDTH - 40,
                      justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Separator(frame).pack(fill="x", pady=10)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        ttk.Button(buttons, text="Lock now",
                   command=lambda: (lock_vault(vault_path), refresh())
                   ).pack(side="left", padx=6)
        ttk.Button(buttons, text="Quit", command=quit_app).pack(side="right")

    def place(win) -> None:
        """Bottom right, above the taskbar, where the tray icon is."""
        win.update_idletasks()
        w = PANEL_WIDTH
        h = min(win.winfo_reqheight(), PANEL_MAX_HEIGHT)
        x = win.winfo_screenwidth() - w - 12
        y = win.winfo_screenheight() - h - TASKBAR_MARGIN
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")

    def show_panel() -> None:
        win = panel["win"]
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(root)
            win.title("Compartment")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            if icon_path().is_file():
                try:
                    win.iconbitmap(str(icon_path()))
                except Exception:                     # noqa: BLE001
                    pass                              # an icon is a nicety
            win.protocol("WM_DELETE_WINDOW", win.withdraw)
            panel["win"] = win
        build(win)
        place(win)
        win.deiconify()
        win.lift()
        win.focus_force()

    def refresh() -> None:
        win = panel["win"]
        if win is not None and win.winfo_exists():
            build(win)
            place(win)

    def quit_app() -> None:
        try:
            icon.stop()
        except Exception:                             # noqa: BLE001
            pass
        root.quit()

    # Tray callbacks arrive on pystray's thread; hand them to Tk's.
    def from_tray(fn):
        return lambda *_: root.after(0, fn)

    image = (Image.open(icon_path()) if icon_path().is_file()
             else Image.new("RGBA", (32, 32), (240, 234, 224, 255)))
    icon = pystray.Icon(
        "compartment", image, "Compartment",
        menu=pystray.Menu(
            pystray.MenuItem("Open Compartment", from_tray(show_panel),
                             default=True),
            pystray.MenuItem("Lock now",
                             from_tray(lambda: (lock_vault(vault_path),
                                                refresh()))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", from_tray(quit_app)),
        ))
    icon.run_detached()

    # First launch opens the panel by itself. A tray icon nobody has seen
    # before is indistinguishable from an app that failed to start, which is
    # the single most expensive failure this app can have.
    if show or claim_first_run(vault_path):
        root.after(200, show_panel)

    root.mainloop()
    return 0


__all__ = ["run", "self_check", "login_status", "set_login", "panel_rows",
           "icon_path"]
