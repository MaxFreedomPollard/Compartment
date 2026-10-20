"""Talk to `compartment serve` the way an MCP client does, and fail loudly.

    python tools/mcp_smoke.py [--command compartment] [--vault PATH]

Starts the server over stdio, sends `initialize`, the `initialized`
notification and `tools/list`, then calls `memory_status`, and checks every
answer. CI runs it against the INSTALLED package on macOS, Windows and Linux:
the unit suite drives the server in-process, which cannot see the failures a
client sees first - an entry point that does not start, a stray line on
stdout that corrupts the JSON-RPC stream, a handshake that never answers.

Standard library only, so it runs before or without the package's own
dependencies on the interpreter that launches it.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading

#: Tools every build must expose. memory_unlock is off by default, so it is
#: not required here.
REQUIRED_TOOLS = {
    "memory_search", "memory_store", "memory_store_many", "memory_get",
    "memory_recent", "memory_forget", "memory_link", "memory_relations",
    "memory_unlink", "memory_list_namespaces", "memory_status", "memory_lock",
    "memory_selftest",
}
TIMEOUT_S = 120.0       # the first call may load the embedding model


def _reader(stream, q: queue.Queue) -> None:
    for line in iter(stream.readline, b""):
        q.put(line)
    q.put(None)


def _expect(q: queue.Queue, want_id: int, stderr_tail: list[str]) -> dict:
    """The response with this id. Every stdout line must be JSON-RPC: a line
    that is not is exactly the stream corruption clients choke on."""
    while True:
        try:
            line = q.get(timeout=TIMEOUT_S)
        except queue.Empty:
            raise SystemExit(f"no answer to request {want_id} within "
                             f"{TIMEOUT_S:.0f}s\nstderr:\n" + "".join(stderr_tail))
        if line is None:
            raise SystemExit(f"the server exited before answering {want_id}\n"
                             "stderr:\n" + "".join(stderr_tail))
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            raise SystemExit(f"non-JSON line on the server's stdout: {text!r}")
        if msg.get("jsonrpc") != "2.0":
            raise SystemExit(f"not a JSON-RPC 2.0 message: {text!r}")
        if msg.get("id") == want_id:
            if "error" in msg:
                raise SystemExit(f"request {want_id} failed: {msg['error']}")
            return msg["result"]
        # notifications (logging, progress) are fine; keep reading


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--command", default="compartment",
                    help="the executable to start (default: compartment on PATH)")
    ap.add_argument("--vault", help="pass --vault to the server")
    args = ap.parse_args()

    exe = shutil.which(args.command) or args.command
    cmd = [exe]
    if args.vault:
        cmd += ["--vault", args.vault]
    cmd += ["serve"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=os.environ.copy())
    out_q: queue.Queue = queue.Queue()
    err_tail: list[str] = []

    def _drain_err():
        for line in iter(proc.stderr.readline, b""):
            err_tail.append(line.decode("utf-8", "replace"))
            del err_tail[:-40]
    threading.Thread(target=_reader, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=_drain_err, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "compartment-smoke", "version": "1"}}})
        init = _expect(out_q, 1, err_tail)
        name = init.get("serverInfo", {}).get("name", "")
        print(f"initialize: server {name!r} {init.get('serverInfo', {}).get('version', '')}, "
              f"protocol {init.get('protocolVersion')}")
        if not init.get("instructions"):
            raise SystemExit("the server sent no instructions in initialize")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in _expect(out_q, 2, err_tail)["tools"]}
        missing = REQUIRED_TOOLS - set(tools)
        if missing:
            raise SystemExit(f"tools missing from tools/list: {sorted(missing)}")
        for t in tools.values():
            schema = t.get("inputSchema") or {}
            if schema.get("type") != "object":
                raise SystemExit(f"{t['name']}: inputSchema is not an object schema")
        print(f"tools/list: {len(tools)} tools")

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "memory_status", "arguments": {}}})
        res = _expect(out_q, 3, err_tail)
        if res.get("isError"):
            raise SystemExit(f"memory_status reported an error: {res}")
        print("tools/call memory_status: ok")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise SystemExit("the server did not exit after its stdin closed")
    print("MCP smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
