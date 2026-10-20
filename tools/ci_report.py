"""Run pytest and, when it fails or stalls, surface why where it can be read.

    python tools/ci_report.py [pytest args...]

A red CI job's log is only readable by someone signed in to GitHub, and
pytest's verdict (which test, which assertion, which import failed during
collection) sits at the end of thousands of lines. This runs pytest with the
given arguments, streams its output unchanged, and under GitHub Actions also
emits error annotations - they appear on the run's summary page, inline on a
pull request, and in the check-run annotations API:

* when pytest fails: the short test summary and the last lines;
* when pytest falls silent for STALL_S seconds (a hung test): the last lines
  as they stand, the unfinished line included - with `-v` that line names
  the test that is running, and with `-o faulthandler_timeout=N` the stacks
  of every thread are already in it.

A job that a hung test makes run into its timeout used to end with no
verdict at all; the stall annotation is written while the job is alive.
Outside GitHub Actions it is plain pytest.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

TAIL_LINES = 60
STALL_S = int(os.environ.get("CI_REPORT_STALL_S", "150"))
MAX_STALL_REPORTS = 4
ON_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def _escape(text: str) -> str:
    # Workflow-command data escaping, so a multi-line message stays one
    # annotation.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _annotate(title: str, text: str) -> None:
    sys.stdout.write(f"::error title={title}::{_escape(text)}\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    proc = subprocess.Popen([sys.executable, "-m", "pytest", "-rfE", *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    buf: list[str] = []              # complete lines so far
    partial = [""]                   # the line being written now
    last = [time.monotonic()]
    lock = threading.Lock()

    def reader() -> None:
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            with lock:
                last[0] = time.monotonic()
                pieces = (partial[0] + text).split("\n")
                partial[0] = pieces.pop()
                buf.extend(pieces)
                del buf[:-2000]

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    reports = 0
    while proc.poll() is None:
        time.sleep(5)
        with lock:
            idle = time.monotonic() - last[0]
            tail = buf[-TAIL_LINES:] + ([partial[0]] if partial[0] else [])
        if ON_ACTIONS and idle >= STALL_S and reports < MAX_STALL_REPORTS:
            reports += 1
            _annotate(f"pytest silent for {int(idle)}s (still running)",
                      "\n".join(tail))
            with lock:
                last[0] = time.monotonic()     # next report after another stall
    t.join(timeout=10)
    rc = proc.returncode
    lines = buf + ([partial[0]] if partial[0] else [])
    if rc != 0 and ON_ACTIONS:
        summary = [ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
        if summary:
            _annotate("pytest failures", "\n".join(summary[:50]))
        _annotate(f"pytest exited {rc} (last lines)", "\n".join(lines[-TAIL_LINES:]))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
