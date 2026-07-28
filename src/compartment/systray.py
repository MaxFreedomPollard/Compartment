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
import subprocess
import sys
from pathlib import Path

from .home import env, home

from .menubar import (AUTO_LOCK_CHOICES, INTEGRATION_TARGETS, RECENT_COUNT,
                      acquire_instance_lock, auto_lock_label,
                      change_passphrase, claim_first_run, default_vault,
                      fetch_state, integrate, lock_vault, self_check,
                      set_setting, starter_note, summarise, unlock_vault)

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
    # Installing leaves you with a vault that nothing is using until an agent
    # is wired to it. One button per agent, so that step is not a terminal
    # command someone has to know about.
    rows.append(("heading", "CONNECT AN AGENT"))
    wired = state.get("integrations") or {}
    for target, name in INTEGRATION_TARGETS:
        rows.append((f"connect:{target}",
                     f"{name} ✓" if wired.get(target) else name))
    rows.append(("note", state.get("connect_note") or
                 "Registers memory with the agent, writes its instructions "
                 "and turns on capture. The same as running "
                 "`compartment integrate claude` in a terminal."))
    rows.append(("heading", f"LAST {RECENT_COUNT} MEMORIES"))
    recent = state.get("recent") or []
    if not recent:
        rows.append(("empty", starter_note(state)))
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


# --- the Linux application entry --------------------------------------------
# A Linux desktop has no tray to leave an icon in, so being findable means
# being in the applications menu. The entry is per-user, needs no root, and
# names an absolute icon path so nothing has to be installed into an icon
# theme or a cache rebuilt.

DESKTOP_FILE = "compartment.desktop"


def desktop_entry_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "applications" / DESKTOP_FILE


def app_icon_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "app.png"


def _panel_command(vault: str | None = None) -> str:
    exe = shutil.which("compartment")
    vault = vault or env("VAULT") or str(home() / "memory.vault")
    if exe:
        return f'"{exe}" --vault "{vault}" panel'
    return f'"{sys.executable}" -m compartment.cli --vault "{vault}" panel'


def install_desktop_entry(vault: str | None = None) -> str:
    """Put Compartment in the applications menu. Returns what happened."""
    path = desktop_entry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        icon = app_icon_path()
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Compartment\n"
            "Comment=Encrypted memory for AI agents\n"
            f"Exec={_panel_command(vault)}\n"
            + (f"Icon={icon}\n" if icon.is_file() else "")
            + "Terminal=false\n"
            "Categories=Utility;Security;\n"
            "Keywords=memory;vault;mcp;agent;\n",
            encoding="utf-8")
        path.chmod(0o644)
    except OSError as exc:
        return f"error: {exc}"
    # Best effort: most desktops notice a new file by themselves, and the
    # ones that want telling are not worth failing an install over.
    try:
        subprocess.run(["update-desktop-database", str(path.parent)],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    return str(path)


def remove_desktop_entry() -> bool:
    try:
        desktop_entry_path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _is_linux() -> bool:
    """This module draws the panel on Windows and on everything that is not
    macOS. Only the latter uses desktop entries, and macOS must never get
    one: it has its own front end, and writing into an XDG directory there
    would litter a machine that will never read it."""
    return sys.platform not in ("win32", "darwin")


def login_status() -> str:
    if _is_linux():
        # No tray to sit in, so nothing worth starting at login: the panel is
        # opened when it is wanted, from the applications menu.
        return "on" if desktop_entry_path().is_file() else "off"
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
    if _is_linux():
        if not enabled:
            return "off" if remove_desktop_entry() else "error"
        out = install_desktop_entry()
        return "on" if not out.startswith("error") else out
    if sys.platform != "win32":
        return "error: start at login is handled by the menu bar app here"
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

def has_tray() -> bool:
    """Is there a notification area to put an icon in?

    Windows has one everywhere. Linux does not: whether a tray icon appears
    depends on the desktop, and on GNOME or Wayland it can simply never show
    up, with nothing said. Silent absence is the worst possible failure for
    the control that unlocks your memories, so Linux gets the same panel as
    an ordinary window instead of an icon that may or may not exist.
    """
    return sys.platform == "win32"


def run(vault: str | None = None, show: bool = False,
        render_to: str | None = None) -> int:
    vault_path = vault or default_vault()
    tray = has_tray()
    where = "notification area" if tray else "window"
    if render_to:                                     # parity with --render
        print("error: --render is macOS only", file=sys.stderr)
        return 2

    # One copy per vault, however each was started: the Run key at sign-in,
    # `compartment init`, a launcher entry, or a person running it again.
    _lock, only = acquire_instance_lock(vault_path)
    if not only:
        if tray:
            print("Compartment is already running - open it from the "
                  "notification area")
        else:
            print("Compartment is already open - look for its window")
        return 0

    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("error: this Python has no tkinter, which the "
              f"{'tray panel' if tray else 'panel'} needs.\n"
              "  The quickest fix is to install Compartment with uv, which\n"
              "  brings its own Python with tkinter already in it:\n"
              "    uv tool install compartment\n"
              "  Otherwise install your distribution's Python Tk package\n"
              "  (Debian/Ubuntu: python3-tk, Fedora: python3-tkinter).",
              file=sys.stderr)
        return 3
    pystray = Image = None
    if tray:
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

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # A machine with no desktop: SSH, a container, a CI runner. Say so
        # in one line rather than showing a Tk stack trace.
        print(f"error: no graphical display to draw the panel on ({exc}).\n"
              "  On a headless machine use the CLI, or `compartment dash` to\n"
              "  read the vault in a browser.", file=sys.stderr)
        return 4
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
        ttk.Label(frame, text="CONNECT AN AGENT",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w")

        def connect(target, name):
            """Say it is working, then work, so the click is visibly seen.

            Tk repaints between events and not during one, so doing the
            wiring straight from the handler freezes the panel for a second
            and then changes one line: indistinguishable from a dead button.
            """
            if panel.get("connect_busy"):
                return                          # one at a time
            panel["connect_busy"] = target
            panel["connect_note"] = f"Connecting {name}…"
            refresh()

            def work():
                try:
                    _ok, note = integrate(vault_path, target)
                except Exception as exc:        # noqa: BLE001
                    note = f"could not connect: {exc}"
                panel["connect_busy"] = None
                panel["connect_note"] = note
                refresh()
            (panel.get("win") or root).after(50, work)

        wired = state.get("integrations") or {}
        connect_row = ttk.Frame(frame)
        connect_row.pack(fill="x", pady=(6, 0))
        for _target, _name in INTEGRATION_TARGETS:
            ttk.Button(connect_row,
                       text=f"{_name} ✓" if wired.get(_target) else _name,
                       state=("disabled" if panel.get("connect_busy")
                              else "normal"),
                       command=lambda t=_target, n=_name: connect(t, n)
                       ).pack(side="left", padx=(0, 6))
        ttk.Label(frame, text=panel.get("connect_note") or
                  "Registers memory with the agent, writes its instructions "
                  "and turns on capture. The same as running "
                  "`compartment integrate claude` in a terminal.",
                  foreground=("#000" if panel.get("connect_note") else "#666"),
                  wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Separator(frame).pack(fill="x", pady=10)
        ttk.Label(frame, text=f"LAST {RECENT_COUNT} MEMORIES",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w")
        recent = state.get("recent") or []
        if not recent:
            ttk.Label(frame, text=starter_note(state), foreground="#666",
                      wraplength=WRAP,
                      justify="left").pack(anchor="w", pady=(4, 0))
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
        """Where the panel sits: by the tray icon, or centred without one."""
        win.update_idletasks()
        # The scrollbar sits beside the content, so widen the window by it
        # rather than letting it eat a strip off the right of every line.
        w = PW + panel.get("scroll_w", 0)
        h = min(win.winfo_reqheight(), PMH)
        if tray:
            x = win.winfo_screenwidth() - w - 12      # above the taskbar
            y = win.winfo_screenheight() - h - TBM
        else:
            # No icon to point at, so the bottom right corner would be an
            # odd place for the only window the app has.
            x = (win.winfo_screenwidth() - w) // 2
            y = (win.winfo_screenheight() - h) // 3
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")

    def show_panel() -> None:
        win = panel["win"]
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(root)
            win.title("Compartment")
            win.resizable(False, False)
            if tray:
                win.attributes("-topmost", True)
            if icon_path().is_file():
                try:
                    win.iconbitmap(str(icon_path()))
                except Exception:                     # noqa: BLE001
                    pass                              # an icon is a nicety
            # With an icon in the tray, closing the window hides it and the
            # app lives on. Without one, a hidden window is an app with no
            # way back to it, so closing means quitting.
            win.protocol("WM_DELETE_WINDOW",
                         win.withdraw if tray else quit_app)
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
        icon = panel.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:                         # noqa: BLE001
                pass
        root.quit()

    # Tray callbacks arrive on pystray's thread; hand them to Tk's.
    def from_tray(fn):
        return lambda *_: root.after(0, fn)

    if tray:
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
        panel["icon"] = icon
        icon.run_detached()

    # First launch opens the panel by itself. A tray icon nobody has seen
    # before is indistinguishable from an app that failed to start, which is
    # the single most expensive failure this app can have. Without an icon
    # there is nothing else to look at, so the window always opens.
    if show or not tray or claim_first_run(vault_path):
        root.after(200, show_panel)

    root.mainloop()
    return 0


def quit_running() -> bool:
    """Stop a running panel before an update or uninstall replaces it."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "compartment.exe"],
                           capture_output=True, timeout=30)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    import signal
    killed = False
    try:
        out = subprocess.run(
            ["pgrep", "-f", "compartment.*(panel|tray|menubar)"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            pass
    return killed


__all__ = ["run", "self_check", "login_status", "set_login", "quit_running",
           "panel_rows",
           "icon_path"]
