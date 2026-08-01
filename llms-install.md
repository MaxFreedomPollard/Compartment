# Installing Compartment (instructions for an AI assistant)

This file is for an AI coding agent installing Compartment on the user's machine.
Follow it top to bottom. There is exactly one step you cannot do yourself, and it
is marked.

Compartment is a local, encrypted memory vault. It runs over **stdio only** and
opens no ports. There is no API key, no account and no cloud service to sign up
for.

## 1. Check the prerequisite

Compartment needs **Python 3.11 or newer**.

```bash
python3 --version
```

If that reports anything below 3.11, stop and tell the user to install a newer
Python first. Do not try to work around it.

## 2. Install the package

```bash
pip install compartment
```

`pipx install compartment` and `uv tool install compartment` both work too, and
are better if the user keeps their global Python clean. All three put a
`compartment` executable on the PATH. Confirm it landed:

```bash
compartment --version
```

If the shell cannot find `compartment`, the install directory is not on PATH.
On most systems that directory is `~/.local/bin`.

## 3. Create the vault (the user must do this step, not you)

```bash
compartment init
```

**Ask the user to run this one command themselves in their own terminal.**

It prompts for a passphrase, twice, without echoing it. That passphrase is the
only key to the vault: Compartment never transmits it, never stores it in
plaintext, and cannot recover it. If it is lost, the memories are
cryptographically unrecoverable.

Never invent a passphrase for the user, never type one on their behalf, and
never pass `--passphrase` on the command line, because that would put the secret
into the shell history and the process list. The flag exists for unattended
provisioning, and this is not that.

On macOS the user can add `--keychain` to store a reboot-surviving credential:

```bash
compartment init --keychain
```

Without it, the vault comes up locked after every reboot and the user has to run
`compartment unlock` again before the memory tools will answer.

Wait for the user to confirm this finished before continuing.

## 4. Register Compartment with Cline

```bash
compartment integrate cline
```

This writes the server into Cline's own `cline_mcp_settings.json` and does not
disturb any other MCP server already configured there. It takes a byte-exact
backup first, writes atomically, and refuses to touch a config it cannot parse
(printing the block for manual pasting instead).

**Reload the VS Code window afterwards** so Cline picks up the new server.

`compartment integrate --list` shows every client it knows about and whether it
is installed on this machine. `compartment integrate --all` wires up all of them
at once.

## 5. Verify

```bash
compartment status
```

A healthy vault reports `unlocked`. If it reports locked, the user runs:

```bash
compartment unlock
```

Then confirm the MCP server itself starts:

```bash
compartment serve --help
```

Cline launches it as `compartment serve` over stdio. You do not need to run
`compartment serve` yourself, and you should not leave a copy running.

## Manual configuration (any MCP client)

If a client is not in `--list`, add this block to its MCP config by hand:

```json
{
  "mcpServers": {
    "compartment": {
      "command": "compartment",
      "args": ["serve"]
    }
  }
}
```

VS Code uses the key `servers` and wants `"type": "stdio"`. Zed uses
`context_servers`. Codex uses TOML under `[mcp_servers.compartment]`.

## Troubleshooting

**Tools return a locked error.** The vault is locked. `compartment unlock`, or
`compartment unlock --keychain` on macOS so it survives the next reboot.

**`compartment: command not found` inside the client but not in the shell.**
The client is not inheriting the user's PATH. Put the absolute path to the
executable in the `command` field instead. `which compartment` prints it.

**The client shows the server but lists no tools.** Reload the client window.
Most MCP clients only read their server list at startup.
