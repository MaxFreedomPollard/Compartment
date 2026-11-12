---
title: Support
permalink: /support
---

# Compartment Support

Need help with Compartment on macOS, Linux, or Windows? Reach out directly or browse the answers below.

## Contact

* **Email support:** [support@bellrope.app](mailto:support@bellrope.app)
* **Bug reports and feature requests:** [GitHub Issues](https://github.com/MaxFreedomPollard/Compartment/issues)
* **Documentation:** [Compartment README](https://github.com/MaxFreedomPollard/Compartment#readme)
* **Privacy policy:** [Compartment Privacy Policy](/Compartment/privacy)

## Frequently Asked Questions

### Where is the Compartment window on macOS?

Compartment lives in the macOS menu bar and has no Dock icon by default so it stays out of your way. Click the Compartment icon in the top-right menu bar to open the control panel. If your menu bar is crowded or hidden behind the MacBook notch, open **Compartment** again from `/Applications` or Spotlight and its control panel window will appear in the center of your screen.

### How do I create or unlock my encrypted memory vault?

1. Open Compartment from the menu bar or `/Applications`.
2. Choose a passphrase in the two passphrase fields and click **Create vault**.
3. Everything is encrypted locally with Argon2id and ChaCha20-Poly1305 AEAD. Your passphrase is never transmitted anywhere and cannot be recovered if forgotten, so store it safely (for example in Apple Passwords or a password manager).

### How do I view and search my memories in a browser?

Unlock your vault in the Compartment panel and click **Dashboard**. Compartment starts a local-only server bound to `127.0.0.1` on your Mac and opens the interactive memory graph, hybrid vector + keyword search, and tag browser in your default web browser. No cloud server is contacted.

### How do I connect an AI agent (Claude, Codex, AntiGravity, OpenClaw, or Hermes)?

Open the Compartment panel and click your agent's button under **CONNECT AN AGENT**:

* In the Mac App Store edition (App Sandbox), Compartment opens a standard macOS file dialog pointed at that agent's configuration folder so you can grant write permission to its MCP configuration file, or click **Cancel** in the file dialog to copy the ready-to-paste MCP configuration snippet to your clipboard.
* Restart your AI client after connecting so it loads the `compartment` MCP server tools (`memory_search`, `memory_store`, `memory_store_many`, `memory_link`, `memory_forget`, and related tools).

### How do I back up my vault or move memories to another computer?

* Click **Back up** in the Compartment panel to save a cryptographically verified `.vault` backup file (even while the vault is locked).
* Click **Merge** on another unlocked vault to import memories and relations from a `.vault` backup without ever exporting plaintext to disk.

### How do auto-lock and reboot protection work?

By default, an unlocked vault automatically locks after the inactivity window configured under **Auto-lock** in Settings, and every system restart destroys the per-boot session secret in RAM so a cold disk copy never exposes an unlocked session.
