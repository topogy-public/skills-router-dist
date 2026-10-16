#!/usr/bin/env python3
"""PreToolUse on Read: allow reads of the router's own content library without a
permission prompt. The library lives in the plugin cache, outside the project, so
Claude Code would otherwise ask the user to approve reading it. Only paths that
resolve inside the library directory are allowed; everything else is left to the
normal permission flow. The library's own .claude-plugin/ manifest is excluded: it
carries the org's router token. Fails open (no output = no opinion)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def main():
    inp = lib.read_input()
    path = inp.get("file_path") or ""
    manifest = os.path.join(lib.LIBRARY, ".claude-plugin")
    if (path and os.path.isabs(path) and inside(path, lib.LIBRARY)
            and not inside(path, manifest)):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "skills-router library file",
            }
        }))
        lib.trace(inp, "PreToolUse", "read-allowed", tool="Read", path=path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router read_allow: {exc}\n")
    sys.exit(0)
