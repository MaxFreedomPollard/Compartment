"""macOS menu bar app: engRAM in the status bar.

Click the icon and a popover shows what memory is doing - whether the vault
is open, the three settings worth changing day to day, and the last handful
of things it remembered. No dock icon, no window to manage.

Design notes:

* The data layer is plain functions with no AppKit in sight, so it is
  testable on every OS in CI. AppKit is imported inside `run()`, which is the
  only part that cannot run headless.
* State is read by shelling out to the `engram` CLI rather than opening the
  vault in-process. A status bar app that idles at 300 MB because it is
  holding an embedding model would be a bad neighbour; a subprocess that
  exits is not.
* Settings live in `<vault>.config.json`, which needs no passphrase, so the
  toggles work whether or not the vault is currently unlocked.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .acl import VaultConfig

AUTO_LOCK_CHOICES = [15, 30, 60, 0]          # 0 = never
POPOVER_WIDTH = 360
POPOVER_MAX_HEIGHT = 640
CONTENT_INSET = 16
CONTENT_WIDTH = POPOVER_WIDTH - CONTENT_INSET * 2
RECENT_COUNT = 5

# NSLayoutAttribute. Named because passing 0 here silently centres everything
# instead of raising - the layout looks "almost right" and nothing tells you.
_LEADING = 5
_CENTER_Y = 10
_HORIZONTAL, _VERTICAL = 0, 1


# --------------------------------------------------------------- data layer

def engram_bin() -> str:
    """The CLI to shell out to - prefer the one from this same install."""
    here = Path(sys.executable).parent / "engram"
    if here.exists():
        return str(here)
    return shutil.which("engram") or "engram"


def default_vault() -> str:
    return os.environ.get("ENGRAM_VAULT",
                          str(Path.home() / ".engram" / "memory.vault"))


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _json_cmd(vault: str, *sub: str) -> dict | None:
    code, out = _run([engram_bin(), "--vault", vault, *sub])
    if code != 0:
        return None
    start = out.find("{")
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def read_settings(vault: str) -> dict:
    """The three adjustable settings, plus whether the capture hook is on.

    Reads the config file directly: no vault unlock, no passphrase, so the
    popover works even when memory is locked.
    """
    cfg = VaultConfig.load(vault)
    from . import claude_hooks
    try:
        hook = claude_hooks.is_installed()
    except Exception:                                   # noqa: BLE001
        hook = False
    return {
        "capture_hook": hook,
        "search_starter_facts": bool(
            cfg.settings.get("include_packs_in_search", True)),
        "auto_lock_minutes": int(cfg.settings.get("auto_lock_minutes", 30)),
    }


def set_setting(vault: str, key: str, value) -> dict:
    """Change one setting. Returns the settings as they now stand."""
    if key == "capture_hook":
        from . import claude_hooks
        if value:
            claude_hooks.install(engram_bin=engram_bin(), vault=vault)
        else:
            claude_hooks.uninstall()
        return read_settings(vault)
    cfg = VaultConfig.load(vault)
    if key == "search_starter_facts":
        cfg.settings["include_packs_in_search"] = bool(value)
    elif key == "auto_lock_minutes":
        cfg.settings["auto_lock_minutes"] = int(value)
    else:
        raise KeyError(f"unknown setting {key!r}")
    cfg.save(vault)
    return read_settings(vault)


def fetch_state(vault: str) -> dict:
    """Everything the popover shows. Never raises - a status bar app that
    dies because a vault is missing is worse than one that says so."""
    state = {"vault": vault, "exists": os.path.exists(vault), "locked": True,
             "records": 0, "organic": 0, "recent": [], "error": None,
             "settings": {"capture_hook": False,
                          "search_starter_facts": True,
                          "auto_lock_minutes": 30}}
    try:
        state["settings"] = read_settings(vault)
    except Exception as exc:                            # noqa: BLE001
        state["error"] = str(exc)
    if not state["exists"]:
        state["error"] = "no vault yet - run: engram init"
        return state

    status = _json_cmd(vault, "status")
    if status is None:
        state["error"] = "could not read vault status"
        return state
    state["locked"] = bool(status.get("locked", True))
    state["records"] = int(status.get("records", 0) or 0)
    state["organic"] = int(status.get("organic_records", 0) or 0)
    if state["locked"]:
        return state

    recent = _json_cmd(vault, "recent", "--limit", str(RECENT_COUNT), "--json")
    if recent:
        counts = recent.get("counts") or {}
        state["organic"] = int(counts.get("organic", state["organic"]))
        state["records"] = int(counts.get("total", state["records"]))
        # newest first reads better in a list you glance at
        state["recent"] = list(reversed(recent.get("results") or []))
    return state


def lock_vault(vault: str) -> bool:
    return _run([engram_bin(), "--vault", vault, "lock"])[0] == 0


def summarise(state: dict) -> str:
    """One line under the title. Also what --self-check prints."""
    if not state["exists"]:
        return "no vault"
    if state["locked"]:
        return "locked - unlock in Terminal: engram unlock"
    return (f"{state['records']:,} memories · "
            f"{state['organic']:,} stored by you")


def auto_lock_label(minutes: int) -> str:
    return "Never" if not minutes else f"{minutes} min"


# ----------------------------------------------------------------- self test

def self_check(vault: str | None = None) -> int:
    """Prove the data layer works without opening a window (CI + debugging)."""
    v = vault or default_vault()
    st = fetch_state(v)
    print(f"vault    : {st['vault']}")
    print(f"status   : {summarise(st)}")
    if st["error"]:
        print(f"note     : {st['error']}")
    s = st["settings"]
    print(f"settings : capture_hook={s['capture_hook']} "
          f"search_starter_facts={s['search_starter_facts']} "
          f"auto_lock={auto_lock_label(s['auto_lock_minutes'])}")
    print(f"recent   : {len(st['recent'])} memories")
    for r in st["recent"]:
        text = " ".join((r.get("text") or "").split())
        print(f"  [{r.get('created_local','')}] {text[:88]}")
    return 0


# ------------------------------------------------------------------- the UI

def run(vault: str | None = None, show: bool = False,
        render_to: str | None = None) -> int:
    """Start the status bar app. Returns only when the user quits.

    `render_to` writes the popover to a PNG and exits instead of running -
    a way to actually look at the UI in a headless/automated check, without
    needing screen-recording permission to photograph a live window.
    """
    if sys.platform != "darwin":
        print("error: the menu bar app is macOS only", file=sys.stderr)
        return 1
    try:
        import objc                                     # noqa: F401
        from AppKit import (NSApp, NSApplication,
                            NSApplicationActivationPolicyAccessory, NSColor,
                            NSFont, NSImage, NSMakeRect, NSMenu, NSMenuItem,
                            NSPopover, NSSegmentedControl, NSStackView,
                            NSStatusBar, NSSwitch, NSTextField, NSButton,
                            NSView, NSViewController)
        from Foundation import NSObject
    except ImportError:
        print("error: the menu bar app needs PyObjC.\n"
              "  pip install 'engram-memory-vault[menubar]'", file=sys.stderr)
        return 1

    vault_path = vault or default_vault()

    # -- small helpers on top of AppKit's verbosity ------------------------
    def label(text, size=13, bold=False, secondary=False, wrap=False,
              truncate=False, width=CONTENT_WIDTH):
        f = NSTextField.labelWithString_(text)
        f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
        if secondary:
            f.setTextColor_(NSColor.secondaryLabelColor())
        f.setAlignment_(0)                  # NSTextAlignmentLeft
        if wrap:
            f.setLineBreakMode_(0)          # NSLineBreakByWordWrapping
            f.setUsesSingleLineMode_(False)
            f.setPreferredMaxLayoutWidth_(width)
            f.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
        elif truncate:
            # one line, ellipsised - a long tag list must not blow the width
            f.setLineBreakMode_(4)          # NSLineBreakByTruncatingTail
            f.widthAnchor().constraintLessThanOrEqualToConstant_(
                width).setActive_(True)
        return f

    def row(*views, spacing=8):
        s = NSStackView.stackViewWithViews_(list(views))
        s.setOrientation_(_HORIZONTAL)
        s.setSpacing_(spacing)
        s.setAlignment_(_CENTER_Y)
        # fill the popover width, otherwise the trailing control floats
        s.widthAnchor().constraintEqualToConstant_(CONTENT_WIDTH).setActive_(True)
        return s

    def divider():
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        v.setWantsLayer_(True)
        v.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
        v.heightAnchor().constraintEqualToConstant_(1).setActive_(True)
        return v

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.state = fetch_state(vault_path)
            self.popover = None
            self.status_item = None
            self.body = None
            return self

        # ---- building the popover contents -----------------------------
        def buildBody(self):
            st = self.state
            views = []
            title = row(label("engRAM", 15, bold=True),
                        label("locked" if st["locked"] else "unlocked", 11,
                              secondary=True))
            views.append(title)
            views.append(label(summarise(st), 11, secondary=True))
            if st["error"]:
                views.append(label(st["error"], 11, secondary=True, wrap=True))
            views.append(divider())

            views.append(label("SETTINGS", 10, bold=True, secondary=True))
            s = st["settings"]

            self.hook_switch = NSSwitch.alloc().init()
            self.hook_switch.setState_(1 if s["capture_hook"] else 0)
            self.hook_switch.setTarget_(self)
            self.hook_switch.setAction_("toggleHook:")
            views.append(row(label("Capture hook"), _spacer(), self.hook_switch))
            views.append(label("Save memories Claude Code writes, automatically",
                               10, secondary=True))

            self.starter_switch = NSSwitch.alloc().init()
            self.starter_switch.setState_(1 if s["search_starter_facts"] else 0)
            self.starter_switch.setTarget_(self)
            self.starter_switch.setAction_("toggleStarter:")
            views.append(row(label("Search starter facts"), _spacer(),
                             self.starter_switch))
            views.append(label("Include the built-in reference knowledge",
                               10, secondary=True))

            seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
                [auto_lock_label(m) for m in AUTO_LOCK_CHOICES], 0, self,
                "changeAutoLock:")
            try:
                seg.setSelectedSegment_(
                    AUTO_LOCK_CHOICES.index(int(s["auto_lock_minutes"])))
            except ValueError:
                seg.setSelectedSegment_(1)
            self.autolock_seg = seg
            views.append(row(label("Auto-lock"), _spacer(), seg))
            views.append(label("Lock the vault after this much idle time",
                               10, secondary=True))
            views.append(divider())

            views.append(label(f"LAST {RECENT_COUNT} MEMORIES", 10, bold=True,
                               secondary=True))
            if st["locked"]:
                views.append(label("unlock the vault to see them", 11,
                                   secondary=True))
            elif not st["recent"]:
                views.append(label("nothing stored yet", 11, secondary=True))
            else:
                for r in st["recent"]:
                    text = " ".join((r.get("text") or "").split())
                    views.append(label("• " + text[:110]
                                       + ("…" if len(text) > 110 else ""),
                                       11, wrap=True))
                    when = r.get("created_local") or ""
                    tags = [t for t in (r.get("tags") or [])
                            if not t.startswith("id:")][:2]
                    meta = when + ("  ·  " + ", ".join(tags) if tags else "")
                    views.append(label(meta, 9, secondary=True, truncate=True))
            views.append(divider())

            refresh = NSButton.buttonWithTitle_target_action_("Refresh", self,
                                                             "refresh:")
            lock = NSButton.buttonWithTitle_target_action_("Lock", self,
                                                           "lockNow:")
            quit_b = NSButton.buttonWithTitle_target_action_("Quit", self,
                                                            "quitApp:")
            views.append(row(refresh, lock, _spacer(), quit_b))

            stack = NSStackView.stackViewWithViews_(views)
            stack.setOrientation_(_VERTICAL)
            stack.setAlignment_(_LEADING)
            stack.setSpacing_(6)
            stack.setEdgeInsets_((CONTENT_INSET, CONTENT_INSET,
                                  CONTENT_INSET, CONTENT_INSET))
            stack.layoutSubtreeIfNeeded()
            fit = stack.fittingSize()
            stack.setFrameSize_((POPOVER_WIDTH,
                                 min(fit.height, POPOVER_MAX_HEIGHT)))
            return stack

        def rebuild(self):
            self.state = fetch_state(vault_path)
            vc = self.popover.contentViewController()
            for sub in list(vc.view().subviews()):
                sub.removeFromSuperview()
            body = self.buildBody()
            h = min(body.frame().size.height, POPOVER_MAX_HEIGHT)
            self.popover.setContentSize_((POPOVER_WIDTH, h))
            vc.view().setFrameSize_((POPOVER_WIDTH, h))
            vc.view().addSubview_(body)
            body.setFrameOrigin_((0, 0))

        # ---- actions ---------------------------------------------------
        def toggleHook_(self, sender):
            try:
                set_setting(vault_path, "capture_hook",
                            bool(sender.state()))
            except Exception:                           # noqa: BLE001
                pass
            self.rebuild()

        def toggleStarter_(self, sender):
            set_setting(vault_path, "search_starter_facts", bool(sender.state()))
            self.rebuild()

        def changeAutoLock_(self, sender):
            idx = int(sender.selectedSegment())
            set_setting(vault_path, "auto_lock_minutes", AUTO_LOCK_CHOICES[idx])
            self.rebuild()

        def refresh_(self, sender):
            self.rebuild()

        def lockNow_(self, sender):
            lock_vault(vault_path)
            self.rebuild()

        def quitApp_(self, sender):
            NSApp.terminate_(self)

        def togglePopover_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
            else:
                self.rebuild()
                btn = self.status_item.button()
                self.popover.showRelativeToRect_ofView_preferredEdge_(
                    btn.bounds(), btn, 1)               # NSRectEdgeMaxY
                NSApp.activateIgnoringOtherApps_(True)

    def _spacer():
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        v.setContentHuggingPriority_forOrientation_(1, 0)
        return v

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    ctrl = Controller.alloc().init()

    if render_to:
        body = ctrl.buildBody()
        body.layoutSubtreeIfNeeded()
        bounds = body.bounds()
        rep = body.bitmapImageRepForCachingDisplayInRect_(bounds)
        body.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
        png = rep.representationUsingType_properties_(4, {})   # 4 = PNG
        ok = png.writeToFile_atomically_(render_to, True)
        print(f"rendered popover -> {render_to}" if ok
              else f"error: could not write {render_to}")
        return 0 if ok else 1
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1.0)
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        "brain.head.profile", "engRAM")
    if img is None:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "memorychip", "engRAM")
    if img is not None:
        img.setTemplate_(True)
        item.button().setImage_(img)
    else:
        item.button().setTitle_("eR")
    item.button().setTarget_(ctrl)
    item.button().setAction_("togglePopover:")

    vc = NSViewController.alloc().init()
    container = NSView.alloc().initWithFrame_(
        NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
    vc.setView_(container)
    pop = NSPopover.alloc().init()
    pop.setContentViewController_(vc)
    pop.setContentSize_((POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
    pop.setBehavior_(1)                                 # transient
    pop.setAnimates_(True)

    ctrl.popover = pop
    ctrl.status_item = item

    if show:
        ctrl.togglePopover_(None)
    app.run()
    return 0


__all__ = ["run", "self_check", "fetch_state", "read_settings", "set_setting",
           "summarise", "auto_lock_label", "default_vault", "engram_bin",
           "AUTO_LOCK_CHOICES"]
