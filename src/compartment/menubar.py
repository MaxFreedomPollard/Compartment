"""macOS menu bar app: Compartment in the status bar.

Click the icon and a popover shows what memory is doing - whether the vault
is open, the three settings worth changing day to day, and the last handful
of things it remembered. No dock icon, no window to manage.

Design notes:

* The data layer is plain functions with no AppKit in sight, so it is
  testable on every OS in CI. AppKit is imported inside `run()`, which is the
  only part that cannot run headless.
* State is read by shelling out to the `compartment` CLI rather than opening the
  vault in-process. A status bar app that idles at 300 MB because it is
  holding an embedding model would be a bad neighbour; a subprocess that
  exits is not.
* Settings live in `<vault>.config.json`, which needs no passphrase, so the
  toggles work whether or not the vault is currently unlocked.
"""
from __future__ import annotations

from .home import env, home
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

# Written beside the vault the first time the app runs, so the very first
# launch can open the panel by itself instead of leaving the user hunting a
# menu bar icon they have never seen before.
FIRST_RUN_MARKER = ".menubar-introduced"

# Menu bar icons are 18pt tall by convention; the asset is drawn at @2x.
MENUBAR_POINTS = 18

# A second launch asks the copy already running to show itself, over this.
SHOW_NOTIFICATION = "io.github.maxfreedompollard.compartment.show"


def claim_first_run(vault: str) -> bool:
    """True exactly once, on the first launch. Never raises: a read-only home
    directory should cost the user a nicety, not the app."""
    try:
        marker = Path(vault).expanduser().parent / FIRST_RUN_MARKER
        if marker.exists():
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        return True
    except OSError:
        return False


# --------------------------------------------------------------- data layer

def compartment_bin() -> str:
    """A real `compartment` CLI path, for commands handed to other programs.

    Inside Compartment.app the executable sits next to a launcher called `Compartment`,
    and macOS filesystems are case-insensitive by default - so looking for
    "compartment" beside the interpreter finds the launcher and the app ends up
    invoking *itself* instead of the CLI. Prefer the console script in the
    environment, and never accept anything inside a bundle's MacOS folder.
    """
    candidates = [Path(sys.prefix) / "bin" / "compartment",
                  Path(sys.executable).parent / "compartment"]
    for c in candidates:
        if c.is_file() and c.parent.name != "MacOS":
            return str(c)
    return shutil.which("compartment") or "compartment"


def _cli_argv() -> list[str]:
    """How this process runs the CLI.

    NOT sys.executable. Inside the .app that is the bundle launcher, a small
    binary that always starts `compartment.cli menubar` and appends whatever
    else it was given - so every CLI call the panel made came back as an
    argparse usage error. The panel then read the word "keyfile" out of that
    usage line and told the user their vault needed a 2FA keyfile it had
    never had.

    sys.prefix is the real interpreter's home in every case that matters:
    PYTHONHOME points it at Contents/Resources/runtime inside the bundle, and
    at the venv or system prefix everywhere else."""
    exe = Path(sys.prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    if exe.exists():
        return [str(exe), "-m", "compartment.cli"]
    return [sys.executable, "-m", "compartment.cli"]


def default_vault() -> str:
    return env("VAULT", str(home() / "memory.vault"))


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _json_cmd(vault: str, *sub: str) -> dict | None:
    code, out = _run([*_cli_argv(), "--vault", vault, *sub])
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
            claude_hooks.install(compartment_bin=compartment_bin(), vault=vault)
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
        state["error"] = "no vault yet - run: compartment init"
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
    return _run([*_cli_argv(), "--vault", vault, "lock"])[0] == 0


def unlock_vault(vault: str, passphrase: str) -> tuple[bool, str]:
    """Unlock from the panel. Returns (ok, what to show the user).

    The passphrase goes down the child's stdin, never into argv: a command
    line is readable by every process on the machine. Argon2id deliberately
    takes its time, hence the generous timeout - a slow unlock is the point.
    """
    if not passphrase:
        return False, "enter your passphrase"
    try:
        p = subprocess.run(
            [*_cli_argv(), "--vault", vault, "unlock", "--passphrase-stdin"],
            input=passphrase + "\n", capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = " ".join(((p.stdout or "") + (p.stderr or "")).split())
    if p.returncode == 0:
        return True, "unlocked"
    low = out.lower()
    # Check this FIRST. If the CLI never ran, nothing in its output describes
    # the vault - and argparse prints "[--keyfile KEYFILE]" in its usage line,
    # which the keyfile test below happily mistook for a vault that needed a
    # 2FA keyfile it had never been given. Never diagnose the vault from a
    # message that is really about our own command line.
    if "unrecognized arguments" in low or low.startswith("usage:"):
        return False, "internal error: could not run the CLI"
    if "wrong passphrase" in low or "no keyslot" in low:
        return False, "wrong passphrase"
    if "keyfile" in low:
        return False, "needs its 2FA keyfile - plug it in and try again"
    return False, (out[:120] or "could not unlock")


def change_passphrase(vault: str, new: str, repeat: str) -> tuple[bool, str]:
    """Replace the passphrase from the panel. Returns (ok, what to show).

    The vault has to be open already - `rekey` re-wraps the master key, and
    the master key is only in hand while unlocked. So this asks for the new
    passphrase and not the old one: whoever is looking at an unlocked vault
    can already read every memory in it, which is strictly more than they
    gain by changing the credential.

    Both fields are compared here, where the user can see them, rather than
    making them type it twice into a terminal they cannot review.
    """
    if not new:
        return False, "enter a new passphrase"
    if new != repeat:
        return False, "the two entries do not match"
    try:
        p = subprocess.run(
            [*_cli_argv(), "--vault", vault, "rekey", "--new-passphrase-stdin"],
            input=new + "\n", capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = " ".join(((p.stdout or "") + (p.stderr or "")).split())
    if p.returncode == 0:
        return True, "passphrase changed"
    low = out.lower()
    if "unrecognized arguments" in low or low.startswith("usage:"):
        return False, "internal error: could not run the CLI"
    if "locked" in low:
        return False, "unlock the vault first"
    return False, (out[:120] or "could not change the passphrase")


def summarise(state: dict) -> str:
    """One line under the title. Also what --self-check prints."""
    if not state["exists"]:
        return "no vault"
    if state["locked"]:
        return "locked - unlock in Terminal: compartment unlock"
    return (f"{state['records']:,} memories · "
            f"{state['organic']:,} stored by you")


def auto_lock_label(minutes: int) -> str:
    return "Never" if not minutes else f"{minutes} min"


# ---------------------------------------------------------------- login item

LOGIN_STATUS = {0: "not registered", 1: "enabled", 2: "requires approval",
                3: "not found"}


def _app_service():
    """SMAppService for this bundle, or None when we are not running from
    Compartment.app (a loose `compartment menubar` has no bundle to register)."""
    try:
        import objc
        from Foundation import NSBundle
    except ImportError:
        return None
    if not (NSBundle.mainBundle().bundleIdentifier() or "").strip():
        return None
    try:
        objc.loadBundle(
            "ServiceManagement", globals(),
            bundle_path="/System/Library/Frameworks/ServiceManagement.framework")
        return objc.lookUpClass("SMAppService").mainAppService()
    except Exception:                                   # noqa: BLE001
        return None


def login_status() -> str:
    svc = _app_service()
    if svc is None:
        return "unavailable (not running from Compartment.app)"
    return LOGIN_STATUS.get(svc.status(), str(svc.status()))


def set_login(enabled: bool) -> str:
    """Start at login, via the modern API so System Settings lists Compartment by
    name with its icon instead of an anonymous "legacy agent" entry."""
    svc = _app_service()
    if svc is None:
        return "unavailable (not running from Compartment.app)"
    try:
        res = (svc.registerAndReturnError_(None) if enabled
               else svc.unregisterAndReturnError_(None))
    except Exception as exc:                            # noqa: BLE001
        return f"failed: {exc}"
    # PyObjC turns the NSError** out-parameter into a second return value.
    # The old code dropped it, so a refusal - "Could not connect to system
    # service", which is what registering from a root installer script gets
    # you - was reported as success.
    ok, err = res if isinstance(res, tuple) else (bool(res), None)
    if not ok:
        detail = ""
        try:
            detail = str(err.localizedDescription()) if err is not None else ""
        except Exception:                               # noqa: BLE001
            pass
        return f"failed: {detail or 'the login item was refused'}"
    return login_status()


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
    a way to look at the UI without screen-recording permission. Run it with
    a normal (framework) Python during development: snapshotting from the
    interpreter embedded in Compartment.app draws the controls but not the text,
    an artefact of the offscreen text system there. The live popover is
    unaffected, because app.run() performs a full launch.
    """
    if sys.platform != "darwin":
        print("error: the menu bar app is macOS only", file=sys.stderr)
        return 1
    try:
        import objc                                     # noqa: F401
        from AppKit import (NSApp, NSApplication,
                            NSApplicationActivationPolicyAccessory,
                            NSBackingStoreBuffered, NSBox, NSColor,
                            NSRunningApplication,
                            NSFont, NSImage, NSMakeRect, NSMenu, NSMenuItem,
                            NSPanel, NSPopover, NSScreen, NSSegmentedControl,
                            NSClipView, NSScrollView,
                            NSBitmapImageRep, NSGraphicsContext,
                            NSSecureTextField, NSStackView, NSStatusBar,
                            NSSwitch, NSTextField,
                            NSButton, NSView, NSViewController,
                            NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
                            NSWindowStyleMaskUtilityWindow)
        from Foundation import (NSBundle, NSDistributedNotificationCenter,
                                NSObject)
    except ImportError:
        print("error: the menu bar app needs PyObjC.\n"
              "  pip install 'compartment[menubar]'", file=sys.stderr)
        return 1

    vault_path = vault or default_vault()

    # One running copy, one icon. LaunchServices normally turns a second
    # launch into a reopen event for the copy already running, but a copy
    # started straight from the executable is invisible to it - and then
    # opening Compartment.app quietly adds a second status item next to the first
    # instead of showing the panel. Handing off over a distributed
    # notification works whichever way each copy was started.
    if not render_to and (NSBundle.mainBundle().bundleIdentifier() or ""):
        bundle_id = NSBundle.mainBundle().bundleIdentifier()
        mine = NSRunningApplication.currentApplication().processIdentifier()
        peers = [a for a in NSRunningApplication
                 .runningApplicationsWithBundleIdentifier_(bundle_id)
                 if a.processIdentifier() != mine]
        if peers:
            NSDistributedNotificationCenter.defaultCenter(
            ).postNotificationName_object_userInfo_deliverImmediately_(
                SHOW_NOTIFICATION, None, None, True)
            print("Compartment is already running - asked it to show its panel")
            return 0

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
        # NSBox's own separator, not a layer-backed view: reaching through to
        # CGColor logs an ObjCPointerWarning on every single rebuild.
        b = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 1))
        b.setBoxType_(2)                                # NSBoxSeparator
        b.widthAnchor().constraintEqualToConstant_(
            CONTENT_WIDTH).setActive_(True)
        return b

    class TopClip(NSClipView):
        """A clip view whose origin is its top left corner.

        AppKit's default is the bottom left, which inside a scroll view means
        the panel opens showing its last line, with the title somewhere above
        the fold. Flipping it makes "scrolled to the top" the resting state.
        """

        def isFlipped(self):
            return True

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.state = fetch_state(vault_path)
            self.popover = None
            self.status_item = None
            self.body = None
            self.window = None
            self.pw_field = None      # only while the vault is locked
            self.unlock_note = None   # "wrong passphrase", and the like
            self.changing_pw = False  # the change-passphrase fields are open
            self.pw_new = None
            self.pw_repeat = None
            self.change_note = None
            return self

        # ---- building the popover contents -----------------------------
        @objc.python_method
        def _finish(self, views):
            stack = NSStackView.stackViewWithViews_(views)
            stack.setOrientation_(_VERTICAL)
            stack.setAlignment_(_LEADING)
            stack.setSpacing_(6)
            stack.setEdgeInsets_((CONTENT_INSET, CONTENT_INSET,
                                  CONTENT_INSET, CONTENT_INSET))
            stack.layoutSubtreeIfNeeded()
            fit = stack.fittingSize()
            stack.setFrameSize_((POPOVER_WIDTH, fit.height))
            if fit.height <= POPOVER_MAX_HEIGHT:
                return stack

            # Taller than a popover is allowed to be. Scroll it. What this
            # did before was cut the overflow off and say nothing, so the
            # buttons below the fold were not merely out of reach - there
            # was no sign they existed. That is how the passphrase form
            # came to show one box out of two.
            stack.setTranslatesAutoresizingMaskIntoConstraints_(True)
            box = NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT)
            scroll = NSScrollView.alloc().initWithFrame_(box)
            scroll.setContentView_(TopClip.alloc().initWithFrame_(box))
            scroll.setHasVerticalScroller_(True)
            scroll.setDrawsBackground_(False)
            scroll.setDocumentView_(stack)
            return scroll

        @objc.python_method
        def buildChangeBody(self, st):
            """Changing the passphrase gets the whole panel to itself.

            The ordinary panel already fills POPOVER_MAX_HEIGHT, and this
            popover clips instead of scrolling. Appending the fields to the
            bottom of it put the second box, Save, and the error note below
            the visible edge: you saw one box, pressed Return, and the save
            failed its repeat check against an empty field you could neither
            fill nor read the complaint about. A focused view cannot overflow.
            """
            def secure(placeholder):
                f = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(0, 0, CONTENT_WIDTH, 24))
                f.setPlaceholderString_(placeholder)
                f.setTarget_(self)
                f.setAction_("saveChangePw:")     # Return saves
                return f

            self.pw_new = secure("New passphrase")
            self.pw_repeat = secure("Repeat it")
            views = [row(label("Change password", 15, bold=True), _spacer()),
                     label(summarise(st), 11, secondary=True),
                     divider(),
                     row(self.pw_new),
                     row(self.pw_repeat)]
            if self.change_note:
                views.append(label(self.change_note, 11, secondary=True,
                                   wrap=True))
            views.append(label("Both boxes must match. There is no recovery "
                               "phrase - if you forget this, the memories are "
                               "unrecoverable.", 10, secondary=True, wrap=True))
            save_b = NSButton.buttonWithTitle_target_action_("Save", self,
                                                             "saveChangePw:")
            save_b.setKeyEquivalent_("\r")
            cancel_b = NSButton.buttonWithTitle_target_action_(
                "Cancel", self, "cancelChangePw:")
            views.append(row(save_b, cancel_b, _spacer()))
            return self._finish(views)

        def buildBody(self):
            st = self.state
            if self.changing_pw and st["exists"] and not st["locked"]:
                return self.buildChangeBody(st)
            views = []
            title = row(label("Compartment", 15, bold=True),
                        label("locked" if st["locked"] else "unlocked", 11,
                              secondary=True))
            views.append(title)
            views.append(label(summarise(st), 11, secondary=True))
            if st["error"]:
                views.append(label(st["error"], 11, secondary=True, wrap=True))

            # Locking and unlocking belong here. The vault is the product, and
            # opening it should not mean finding a terminal.
            self.pw_field = None
            if st["exists"] and st["locked"]:
                field = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(0, 0, CONTENT_WIDTH - 92, 24))
                field.setPlaceholderString_("Passphrase")
                field.setTarget_(self)
                field.setAction_("unlockNow:")        # Return unlocks too
                self.pw_field = field
                unlock_b = NSButton.buttonWithTitle_target_action_(
                    "Unlock", self, "unlockNow:")
                unlock_b.setKeyEquivalent_("\r")
                views.append(row(field, unlock_b))
                if self.unlock_note:
                    views.append(label(self.unlock_note, 11, secondary=True,
                                       wrap=True))
                views.append(label("Stays unlocked until restart or Lock",
                                   10, secondary=True))

            # Changing the passphrase is a separate view: see buildChangeBody.
            self.pw_new = self.pw_repeat = None
            if self.change_note:
                views.append(label(self.change_note, 11, secondary=True,
                                   wrap=True))
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
            quit_b = NSButton.buttonWithTitle_target_action_("Quit", self,
                                                            "quitApp:")
            if st["exists"] and not st["locked"]:
                lock = NSButton.buttonWithTitle_target_action_("Lock", self,
                                                               "lockNow:")
                change = NSButton.buttonWithTitle_target_action_(
                    "Change password", self, "startChangePw:")
                views.append(row(refresh, lock, _spacer(), quit_b))
                views.append(row(change, _spacer()))
            else:
                views.append(row(refresh, _spacer(), quit_b))

            return self._finish(views)

        # Not an action: PyObjC infers a selector from the method name, and
        # a name with no trailing underscores means "takes no arguments".
        @objc.python_method
        def _mount(self, host, body, h):
            for sub in list(host.subviews()):
                sub.removeFromSuperview()
            host.setFrameSize_((POPOVER_WIDTH, h))
            host.addSubview_(body)
            body.setFrameOrigin_((0, 0))

        def rebuild(self):
            """Refresh whichever surface is on screen - popover or window."""
            self.state = fetch_state(vault_path)
            body = self.buildBody()
            h = min(body.frame().size.height, POPOVER_MAX_HEIGHT)
            if self.window is not None and self.window.isVisible():
                self._mount(self.window.contentView(), body, h)
                self.window.setContentSize_((POPOVER_WIDTH, h))
            else:
                self.popover.setContentSize_((POPOVER_WIDTH, h))
                self._mount(self.popover.contentViewController().view(),
                            body, h)

        # ---- getting the thing on screen -------------------------------
        # Two surfaces, and which one to use is never in doubt:
        #
        #   clicked the status item -> popover. Reaching that handler means
        #       the icon was clicked, so the icon is on screen and there is
        #       something to anchor to. No geometry to guess at.
        #   everything else -> window. First launch, reopening the app, a
        #       second launch handing off: in all three the user has no
        #       reachable icon to click, which is how this app came to look
        #       broken in the first place.
        @objc.python_method
        def _makeWindow(self):
            win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskUtilityWindow,
                NSBackingStoreBuffered, False)
            win.setTitle_("Compartment")
            win.setReleasedWhenClosed_(False)
            win.setHidesOnDeactivate_(False)
            win.setLevel_(3)                            # NSFloatingWindowLevel
            return win

        @objc.python_method
        def _placeWindow(self):
            scr = NSScreen.mainScreen()
            if scr is None:
                self.window.center()
                return
            vf = scr.visibleFrame()
            f = self.window.frame()
            self.window.setFrameOrigin_((
                vf.origin.x + vf.size.width - f.size.width - 24,
                vf.origin.y + vf.size.height - f.size.height - 12))

        @objc.python_method
        def showWindow(self):
            _d("showing the window")
            if self.window is None:
                self.window = self._makeWindow()
            self.window.makeKeyAndOrderFront_(None)   # visible before rebuild,
            self.rebuild()                            # so rebuild targets it
            self._placeWindow()
            NSApp.activateIgnoringOtherApps_(True)

        @objc.python_method
        def showPopover(self):
            _d("showing the popover")
            if self.window is not None and self.window.isVisible():
                self.window.orderOut_(None)           # one surface at a time
            self.rebuild()
            btn = self.status_item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                btn.bounds(), btn, 1)                  # NSRectEdgeMaxY
            NSApp.activateIgnoringOtherApps_(True)

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

        def showFirst_(self, _):
            """First launch, reopen, and second-launch handoff all land here.

            Deliberately the window and never the popover. A popover is
            transient: it closes the instant anything takes focus, so on a
            first launch - when the user has never seen the icon, and the
            installer or a browser may still be grabbing focus - it can be
            gone before they ever notice it appeared. That is the exact
            failure this is meant to end. A window stays until dismissed.
            """
            self.showWindow()

        def lockNow_(self, sender):
            lock_vault(vault_path)
            self.unlock_note = None
            self.rebuild()

        def unlockNow_(self, sender):
            if self.pw_field is None:
                return
            pw = str(self.pw_field.stringValue() or "")
            ok, note = unlock_vault(vault_path, pw)
            self.pw_field.setStringValue_("")        # never leave it on screen
            self.unlock_note = None if ok else note
            self.rebuild()

        def startChangePw_(self, sender):
            self.changing_pw = True
            self.change_note = None
            self.rebuild()

        def cancelChangePw_(self, sender):
            self.changing_pw = False
            self.change_note = None
            self.rebuild()

        def saveChangePw_(self, sender):
            if self.pw_new is None or self.pw_repeat is None:
                return
            new = str(self.pw_new.stringValue() or "")
            rep = str(self.pw_repeat.stringValue() or "")
            ok, note = change_passphrase(vault_path, new, rep)
            # Clear both regardless: a passphrase must not sit in a field on
            # screen after the attempt, successful or not.
            self.pw_new.setStringValue_("")
            self.pw_repeat.setStringValue_("")
            self.changing_pw = not ok
            self.change_note = note
            self.rebuild()

        def quitApp_(self, sender):
            NSApp.terminate_(self)

        def togglePopover_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
            elif self.window is not None and self.window.isVisible():
                self.window.orderOut_(None)
            else:
                self.showPopover()

    def _spacer():
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        v.setContentHuggingPriority_forOrientation_(1, 0)
        return v

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    _dbg = env("MENUBAR_DEBUG")
    def _d(*a):
        if _dbg: print("[menubar]", *a, file=sys.stderr, flush=True)
    _d("building controller (fetches state)…")
    ctrl = Controller.alloc().init()
    _d("controller ready")

    if render_to:
        # app.run() normally does this; without it the text system is not up.
        app.finishLaunching()
        body = ctrl.buildBody()
        body.layoutSubtreeIfNeeded()
        bounds = body.bounds()
        # Render through PDF, not cacheDisplayInRect. Caching a view that
        # belongs to no window gives you the switches and the segmented
        # control and not one glyph of text, because there is no window
        # backing store for the text system to lay glyphs into. Ordering an
        # offscreen window in does not help either: it draws as an inactive
        # window, so even the controls lose their tint. The PDF path records
        # glyphs vectorially with their fonts embedded and needs no window at
        # all, so it captures the panel exactly as designed. Rasterise at 2x
        # for a Retina-sharp asset.
        pdf = body.dataWithPDFInsideRect_(bounds)
        w, h = bounds.size.width, bounds.size.height
        image = NSImage.alloc().initWithData_(pdf)
        rep = (NSBitmapImageRep.alloc()
               .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                   None, int(w * 2), int(h * 2), 8, 4, True, False,
                   "NSCalibratedRGBColorSpace", 0, 0))
        rep.setSize_((w, h))
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        image.drawInRect_(NSMakeRect(0, 0, w, h))
        NSGraphicsContext.restoreGraphicsState()
        png = rep.representationUsingType_properties_(4, {})   # 4 = PNG
        ok = png.writeToFile_atomically_(render_to, True)
        print(f"rendered popover -> {render_to}" if ok
              else f"error: could not write {render_to}")
        return 0 if ok else 1
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1.0)
    item.setAutosaveName_("Compartment")          # remember where the user puts it
    # …but never remember it as hidden. An autosave name persists `visible`
    # as well as position, and a status item can be hidden by a stray
    # command-drag off the menu bar. Once that is saved the icon is gone for
    # good - every later launch restores it as hidden, the app looks like it
    # failed to start, and the only way back is to open the app. The icon
    # belongs in the menu bar for as long as Compartment is running; Quit is what
    # removes it.
    item.setVisible_(True)
    # Compartment's own mark - three nested squares, each turned further than
    # the one outside it - drawn by tools/make_icon.py and shipped as package
    # data. Marked as a template so macOS tints it for the light or dark bar
    # and inverts it on click, the way every other status item behaves.
    img = None
    mark = Path(__file__).resolve().parent / "data" / "menubar@2x.png"
    if mark.is_file():
        img = NSImage.alloc().initWithContentsOfFile_(str(mark))
        if img is not None:
            img.setSize_((MENUBAR_POINTS, MENUBAR_POINTS))
    if img is None:                       # a source checkout without the asset
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "square.split.2x2", "Compartment")
    if img is not None:
        img.setTemplate_(True)
        item.button().setImage_(img)
        item.button().setToolTip_("Compartment")
    else:
        item.button().setTitle_("▣")   # nested squares, if the asset is missing
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
    _d("status item created; visible=", item.isVisible(),
       "image=", item.button().image() is not None,
       "window=", item.button().window() is not None)
    if _dbg:
        from AppKit import NSScreen
        import time as _t
        for _ in range(20):
            app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                0xFFFFFFFF, None, "kCFRunLoopDefaultMode", True)
            _t.sleep(0.05)
        w = item.button().window()
        _d("screen:", NSScreen.mainScreen().frame().size.width,
           "| item at:", w.frame().origin.x if w else "no window",
           "| visible:", item.isVisible())

    # Double-clicking Compartment.app while it is already running arrives here.
    # With no delegate macOS does nothing whatsoever, and for an app whose
    # only other surface is a status item that may be hidden behind the notch
    # that is indistinguishable from being broken. This is the way in that
    # cannot fail: the icon can be invisible, the bar can be full, and
    # opening the app again still shows the panel.
    # Conforming to the protocol is what makes this work, not decoration.
    # `hasVisibleWindows:` is a BOOL and the method returns a BOOL; with no
    # protocol to read the signature from, PyObjC types both as object
    # pointers, AppKit sees a method it cannot call, and the reopen event is
    # silently dropped - which is precisely the do-nothing behaviour above.
    try:
        _proto = [objc.protocolNamed("NSApplicationDelegate")]
    except Exception:                                   # noqa: BLE001
        _proto = []

    class CompartmentAppDelegate(NSObject, protocols=_proto):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, vis):
            _d("reopen event -> showing the panel")
            ctrl.showFirst_(None)
            return True

        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            # Closing the panel must never quit Compartment and take the menu bar
            # icon with it. AppKit already defaults to NO, but this app has
            # exactly one window and losing the icon by closing it would be
            # indistinguishable from the app crashing.
            return False

    delegate = CompartmentAppDelegate.alloc().init()
    app.setDelegate_(delegate)
    NSDistributedNotificationCenter.defaultCenter(
    ).addObserver_selector_name_object_(ctrl, "showFirst:",
                                        SHOW_NOTIFICATION, None)

    if show or claim_first_run(vault_path):
        ctrl.performSelector_withObject_afterDelay_("showFirst:", None, 0.35)
    _d("entering run loop")
    app.run()
    _d("run loop exited")
    return 0


__all__ = ["run", "self_check", "fetch_state", "read_settings", "set_setting",
           "summarise", "auto_lock_label", "default_vault", "compartment_bin",
           "AUTO_LOCK_CHOICES"]
