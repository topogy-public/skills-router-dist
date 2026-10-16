#!/usr/bin/env python3
"""SessionEnd: send whatever the trace still holds before the session closes.

The sender is detached, so it outlives this hook and Claude Code's exit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402


def main():
    lib.start_sender(force=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router session_end: {exc}\n")
    sys.exit(0)
