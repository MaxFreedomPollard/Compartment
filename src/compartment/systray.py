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
import shutil
import sys
from pathlib import Path

from .home import env, home

from .menubar import (AUTO_LOCK_CHOICES, RECENT_COUNT, auto_lock_label,
                      change_passphrase, claim_first_run, default_vault,
                      fetch_state, lock_vault, self_check, set_setting,
                      summarise, unlock_vault)

PANEL_WIDTH = 360
PANEL_MAX_HEIGHT = 640
TASKBAR_MARGIN = 56          # room for the taskbar the panel sits above
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Compartment"
SCALE_ENV = "COMPARTMENT_UI_SCALE"


def ui_scale() -> float:
    """How many pixels to a logical unit.

    A process that does not declare DPI awareness gets bitmap-stretched by
    Windows on a high-DPI display: correctly sized and visibly blurry. Asking
    the system for its DPI and scaling the panel to match draws it sharp
    instead, at the same physical size.

    Returns 1.0 off Windows and on ordinary 96 DPI screens, so the common
    case is unchanged. COMPARTMENT_UI_SCALE overrides, for anyone who wants
    the panel bigger or smaller than their display asks for.
    """
    override = os.environ.get(SCALE_ENV)
    if override:
        try:
            v = float(override)
        except ValueError:
            v = 0.0
        if 0.5 <= v <= 4.0:
            return v
    try:
        import ctypes
        # Declaring awareness must happen before the first window exists.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system-DPI aware
        except Exception:                                    # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()        # pre-8.1 fallback
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return max(1.0, round(dpi / 96.0, 2))
    except Exception:                                        # noqa: BLE001
        return 1.0                                           # not Windows


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
    # Locking and unlocking belong in the panel. The vault is the product, and
    # opening it should not mean finding a terminal.
    if state.get("exists"):
        rows.append(("unlock", "Unlock") if state["locked"] else ("lock", "Lock"))
        # Changing the passphrase re-wraps the master key, which only exists
        # in hand while the vault is open - so it is offered only then.
        if not state["locked"]:
            rows.append(("change", "Change password"))
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


def _autostart_command(vault: str | None = None) -> str:
    """What Windows runs at sign-in.

    Two things this has to get right. It launches through the console script
    or pythonw.exe rather than python.exe, because python.exe opens a console
    window behind a tray app that has no window, at every single sign-in. And
    it carries the vault path, because a user running a non-default vault
    would otherwise silently get the default one back after a reboot.
    """
    vault = vault or env("VAULT") or str(home() / "memory.vault")
    exe = shutil.which("compartment")
    if exe:
        return f'"{exe}" --vault "{vault}" tray'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    runner = str(pyw) if pyw.is_file() else sys.executable
    return f'"{runner}" -m compartment.cli --vault "{vault}" tray'


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
                                  _autostart_command())
                return "on"
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
            return "off"
    except Exception as exc:                          # noqa: BLE001
        return f"error: {exc}"


def panel_geometry(content: int, maximum: int) -> tuple[int, bool]:
    """How tall to draw the panel, and whether it needs a scrollbar.

    Split out from the Tk code so the rule is checked on every OS in CI: any
    content taller than the panel scrolls. It is never cut off - the panel
    losing its bottom silently is the whole reason this function exists.
    """
    if content > maximum:
        return maximum, True
    return max(content, 1), False


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

    # Read the DPI (and declare awareness) before the first window exists.
    S = ui_scale()
    PW = int(PANEL_WIDTH * S)
    WRAP = PW - int(40 * S)
    PMH = int(PANEL_MAX_HEIGHT * S)
    TBM = int(TASKBAR_MARGIN * S)

    root = tk.Tk()
    if S != 1.0:
        # Tk sizes fonts in points; this is what turns a point into a pixel.
        # 1.3333 is the 96-DPI baseline Tk already assumes on Windows.
        root.tk.call("tk", "scaling", 1.3333 * S)
    root.withdraw()                                   # no stray empty window
    panel: dict = {"win": None, "note": None}

    def _content(frame, state) -> None:
        """Everything the panel shows. Packs into `frame`, never sizes it -
        deciding how tall the window gets is build()'s job, below."""
        s = state["settings"]

        # Changing the passphrase gets the whole panel to itself, rather than
        # being appended below a panel that is already full. Two boxes, Save
        # and the reason the last attempt failed all belong on screen at once.
        if state["exists"] and not state["locked"] and panel.get("changing"):
            ttk.Label(frame, text="Change password", wraplength=WRAP,
                      font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(frame, text=summarise(state), wraplength=WRAP,
                      foreground="#666").pack(anchor="w", pady=(2, 0))
            ttk.Separator(frame).pack(fill="x", pady=10)
            new = ttk.Entry(frame, show="•", width=34)
            new.pack(anchor="w")
            rep = ttk.Entry(frame, show="•", width=34)
            rep.pack(anchor="w", pady=(6, 0))
            new.focus_set()

            def do_change(*_):
                ok, note = change_passphrase(vault_path, new.get(), rep.get())
                new.delete(0, "end")          # never leave one on screen
                rep.delete(0, "end")
                panel["changing"] = not ok
                panel["change_note"] = note
                refresh()

            new.bind("<Return>", do_change)
            rep.bind("<Return>", do_change)
            if panel.get("change_note"):
                ttk.Label(frame, text=panel["change_note"], wraplength=WRAP,
                          foreground="#b00020").pack(anchor="w", pady=(8, 0))
            ttk.Label(frame, text="Both boxes must match. There is no recovery "
                                  "phrase - if you forget this, the memories "
                                  "are unrecoverable.",
                      foreground="#666", wraplength=WRAP,
                      justify="left").pack(anchor="w", pady=(8, 0))
            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(10, 0))
            ttk.Button(bar, text="Save", command=do_change).pack(side="left")
            ttk.Button(bar, text="Cancel",
                       command=lambda: (panel.update(changing=False,
                                                     change_note=None),
                                        refresh())).pack(side="left", padx=6)
            return

        ttk.Label(frame, text=summarise(state), wraplength=WRAP,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        if state.get("error"):
            ttk.Label(frame, text=str(state["error"]), foreground="#b00020",
                      wraplength=WRAP).pack(anchor="w", pady=(4, 0))

        if state["exists"] and state["locked"]:
            unlock_row = ttk.Frame(frame)
            unlock_row.pack(fill="x", pady=(8, 0))
            entry = ttk.Entry(unlock_row, show="\u2022", width=26)
            entry.pack(side="left")
            entry.focus_set()

            def do_unlock(*_):
                ok, note = unlock_vault(vault_path, entry.get())
                entry.delete(0, "end")        # never leave it on screen
                panel["note"] = None if ok else note
                refresh()

            entry.bind("<Return>", do_unlock)
            ttk.Button(unlock_row, text="Unlock",
                       command=do_unlock).pack(side="right")
            if panel.get("note"):
                ttk.Label(frame, text=panel["note"], foreground="#b00020",
                          wraplength=WRAP).pack(anchor="w",
                                                            pady=(4, 0))
            ttk.Label(frame, text="Stays unlocked until restart or Lock",
                      foreground="#666").pack(anchor="w")

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
                      wraplength=WRAP,
                      justify="left").pack(anchor="w", pady=(4, 0))

        if panel.get("change_note"):        # result of the last attempt
            ttk.Label(frame, text=panel["change_note"],
                      wraplength=WRAP).pack(anchor="w", pady=(8, 0))

        ttk.Separator(frame).pack(fill="x", pady=10)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="left")
        if state["exists"] and not state["locked"]:
            ttk.Button(buttons, text="Lock now",
                       command=lambda: (lock_vault(vault_path),
                                        panel.update(note=None,
                                                     changing=False), refresh())
                       ).pack(side="left", padx=6)
            if not panel.get("changing"):
                ttk.Button(buttons, text="Change password",
                           command=lambda: (panel.update(changing=True,
                                                         change_note=None),
                                            refresh())).pack(side="left", padx=6)
        ttk.Button(buttons, text="Quit", command=quit_app).pack(side="right")

    def build(win) -> None:
        """Fill the window, and give it a scrollbar if the content overflows.

        The content goes inside a canvas rather than straight into the window.
        A fixed-height window cut its own bottom off and said nothing about
        it, which is how the passphrase form came to show one box out of two:
        the second box and the Save button were not merely out of reach, there
        was no sign on screen that they existed at all.
        """
        for child in win.winfo_children():
            child.destroy()
        state = fetch_state(vault_path)

        canvas = tk.Canvas(win, highlightthickness=0, borderwidth=0, width=PW)
        try:                                  # match the themed background
            canvas.configure(background=ttk.Style().lookup("TFrame",
                                                           "background"))
        except tk.TclError:                   # a theme without one: leave it
            pass
        vbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=14)
        canvas.create_window((0, 0), window=frame, anchor="nw", width=PW)

        _content(frame, state)

        frame.update_idletasks()
        need = frame.winfo_reqheight()
        height, scrolling = panel_geometry(need, PMH)
        canvas.configure(height=height, scrollregion=(0, 0, PW, need))
        if scrolling:
            vbar.pack(side="right", fill="y")
            # Bound on the window, not the canvas: the content covers the
            # canvas, so the wheel event never reaches it. Windows reports
            # the delta in multiples of 120.
            win.bind("<MouseWheel>",
                     lambda e: canvas.yview_scroll(-(e.delta // 120), "units"))
            vbar.update_idletasks()
            panel["scroll_w"] = vbar.winfo_reqwidth()
        else:
            win.unbind("<MouseWheel>")
            panel["scroll_w"] = 0

    def place(win) -> None:
        """Bottom right, above the taskbar, where the tray icon is."""
        win.update_idletasks()
        # The scrollbar sits beside the content, so widen the window by it
        # rather than letting it eat a strip off the right of every line.
        w = PW + panel.get("scroll_w", 0)
        h = min(win.winfo_reqheight(), PMH)
        x = win.winfo_screenwidth() - w - 12
        y = win.winfo_screenheight() - h - TBM
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
                                                panel.update(note=None),
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
