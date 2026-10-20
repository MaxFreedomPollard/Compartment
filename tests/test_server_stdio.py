"""`compartment serve` driven over real pipes, as a client drives it: the
handshake, a tool list, then the client hanging up. The rest of the suite
calls the server in-process and cannot see what only a client sees: a
stray line on stdout, a traceback on the way out, an exit status that
tells the client the server crashed."""
import json
import os
import subprocess
import sys
from pathlib import Path

from compartment.vault import Vault

from conftest import PASS

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_a_session_over_stdio_ends_cleanly_when_the_client_hangs_up(
        tmp_path):
    vault = str(tmp_path / "v.vault")
    Vault.create(vault, PASS, creator="test").lock()
    env = dict(os.environ, COMPARTMENT_PASSPHRASE=PASS,
               COMPARTMENT_EMBED_DAEMON="0",
               PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "compartment.cli", "--vault", vault, "serve"],
        input="".join(json.dumps(r) + "\n" for r in requests).encode(),
        capture_output=True, env=env, timeout=120)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, err
    assert "Traceback" not in err, err
    lines = proc.stdout.decode("utf-8").splitlines()
    replies = [json.loads(line) for line in lines]     # nothing but frames
    assert [r.get("id") for r in replies] == [1, 2]
    init, listed = replies
    assert init["result"]["serverInfo"]["name"] == "compartment"
    assert "memory_search" in init["result"]["instructions"]
    assert {t["name"] for t in listed["result"]["tools"]} >= {
        "memory_search", "memory_store", "memory_status"}
