"""Run pytest and, when it fails, surface the reason where it can be read.

    python tools/ci_report.py [pytest args...]

A red CI job's log is only readable by someone signed in to GitHub, and
pytest's verdict (which test, which assertion, which import failed during
collection) sits at the end of thousands of lines. This runs pytest with the
given arguments, streams its output unchanged, and on failure also emits the
short test summary and the last lines as GitHub Actions error annotations:
they appear on the run's summary page, inline on a pull request, and in the
check-run annotations API. Outside GitHub Actions it is plain pytest.
"""
from __future__ import annotations

import os
import subprocess
import sys

TAIL_LINES = 60


def _escape(text: str) -> str:
    # Workflow-command data escaping, so a multi-line message stays one
    # annotation.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str]) -> int:
    proc = subprocess.Popen([sys.executable, "-m", "pytest", "-rfE", *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace")
        sys.stdout.write(line)
        lines.append(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0 and os.environ.get("GITHUB_ACTIONS") == "true":
        summary = [ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
        if summary:
            print("::error title=pytest failures::"
                  + _escape("\n".join(summary[:50])))
        print(f"::error title=pytest exited {rc} (last lines)::"
              + _escape("\n".join(lines[-TAIL_LINES:])))
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
