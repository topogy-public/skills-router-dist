#!/usr/bin/env python3
"""Predicate: is the write target isolated from the base repo?

Exit 0 = safe (work is in an isolated worktree on a non-protected branch, or the
target is not a git repo at all). Non-zero = unsafe; a one-line reason is printed
on stdout for the block message. Fails open (exit 0) on any error.

Inputs (set by the gate as env vars):
  SR_FILE_PATH  the file about to be written (its directory is the target)
  SR_CWD        the session working directory (fallback target)
  SR_PROTECTED  comma-separated protected branches (default: staging,main,master)
"""
import os
import subprocess
import sys


def git(dirp, *args):
    return subprocess.run(["git", "-C", dirp, *args], capture_output=True, text=True, timeout=5)


def real(dirp, path):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(dirp, path))


def main():
    fp = os.environ.get("SR_FILE_PATH") or ""
    cwd = os.environ.get("SR_CWD") or os.getcwd()

    # Resolve the target: the file's nearest EXISTING ancestor directory (the
    # file or its subdir may not exist yet). Only fall back to the session cwd
    # when there is no file path at all — never let a not-yet-created subdir make
    # us judge the session cwd instead of where the file will actually live.
    target = None
    if fp:
        d = os.path.dirname(os.path.abspath(fp))
        while d and d != os.path.dirname(d):
            if os.path.isdir(d):
                target = d
                break
            d = os.path.dirname(d)
    if not target:
        target = cwd if os.path.isdir(cwd) else os.getcwd()

    inside = git(target, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return 0  # not a git repo — nothing to protect

    branch = git(target, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git_dir = git(target, "rev-parse", "--git-dir").stdout.strip()
    common_dir = git(target, "rev-parse", "--git-common-dir").stdout.strip()
    is_worktree = bool(git_dir and common_dir and real(target, git_dir) != real(target, common_dir))

    protected = [b.strip() for b in os.environ.get("SR_PROTECTED", "staging,main,master").split(",") if b.strip()]

    if branch in protected:
        print(f"you are on protected branch '{branch}' in the base checkout")
        return 1
    if not is_worktree:
        print(f"you are on '{branch}' but in the primary checkout, not an isolated worktree")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(f"isolated check error: {exc}\n")
        sys.exit(0)  # fail open
