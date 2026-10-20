"""Compartment's MCP server, as the Claude Desktop extension starts it.

Claude Desktop runs this file with `uv run` inside the environment it built
from the bundle's pyproject.toml, so `compartment` here is the pinned
release in that environment, not whatever may or may not be on the PATH a
GUI application was started with. That is the reason the bundle exists:
every other Claude Desktop route needs the `compartment` command findable,
and a Dock-launched app does not inherit the shell's PATH.

The caller is `claude-desktop`, the same name `compartment integrate
claude` registers, so the audit log and any per-caller ACL treat the two
routes as one client. The vault is Compartment's default (COMPARTMENT_HOME
or ~/.compartment, or the ~/.engram an older install left), which is where
`compartment init` creates it.
"""
from compartment.cli import main

if __name__ == "__main__":
    main(["--caller", "claude-desktop", "serve"])
