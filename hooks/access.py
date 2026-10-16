#!/usr/bin/env python3
"""Whether ASI has switched the router off for this developer.

ASI's answer is cached for the whole machine in access.json, because it is about
the developer, not a session, and the MCP server never learns the session id.
SessionStart reads the cache and, once it has expired, starts this file detached
to ask again, so a change takes effect from the next session. Only a machine
that has never had an answer waits on ASI, briefly, at its first SessionStart.

The developer is identified by their coding agent's account email (Claude Code,
then Codex), else git's user.email. It is self-reported: this is an on/off
switch, not an auth boundary. The router is on only when ASI said so: every
failure counts as "off" (no token, no email, ASI unreachable, a bad answer) and
is retried soon.

It authenticates with the org's router token (router_token.py). Opting out of
telemetry (TOPOGY_ROUTER_TELEMETRY=0) does not stop this check.

Knobs:
  TOPOGY_ROUTER_ACCESS_ENDPOINT  full access URL, for testing against a local or staging ASI
  TOPOGY_ROUTER_TOKEN            override the shipped router token
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_token  # noqa: E402

DEFAULT_ENDPOINT = "https://otel.topogy.com/topogy_router/v1/access"
HTTP_TIMEOUT_S = 10
# How long the first SessionStart on a machine with no answer waits for ASI.
FIRST_CHECK_TIMEOUT_S = 3
AWAIT_POLL_S = 0.1
LOCK_STALE_S = 60
# After a failed check, ask again this soon rather than waiting out a full TTL.
RETRY_S = 300
# Bounds on the TTL ASI sends, so a bad value can neither hammer ASI nor pin an answer for weeks.
MIN_TTL_S, MAX_TTL_S = 60, 7 * 24 * 3600

CACHE = "access.json"
LOCK = "access.lock"


def enabled(state_dir):
    """The cached answer. On only when ASI said so."""
    return _read_cache(state_dir).get("enabled") is True


def check(state_dir, library):
    """Whether the router is on, for SessionStart and the MCP server's tool list. With no
    answer cached yet, ask ASI now (briefly); otherwise answer from the cache and refresh
    it in the background."""
    if "enabled" not in _read_cache(state_dir):
        if refresh(state_dir, library, timeout=FIRST_CHECK_TIMEOUT_S) is None:
            _await_answer(state_dir)
    else:
        maybe_refresh(state_dir, library)
    return enabled(state_dir)


def _await_answer(state_dir):
    """Wait for the first check another process is already making. SessionStart and the
    MCP server start together on a new machine, and whichever loses the lock would
    otherwise find no answer and switch the router off for that session."""
    deadline = time.time() + FIRST_CHECK_TIMEOUT_S + 1
    while "enabled" not in _read_cache(state_dir) and time.time() < deadline:
        time.sleep(AWAIT_POLL_S)


def maybe_refresh(state_dir, library):
    """Start a detached check when the cached answer has expired."""
    try:
        cache = _read_cache(state_dir)
        if time.time() < _number(cache.get("checked_at")) + _number(cache.get("ttl")):
            return False
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "refresh", state_dir, library],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        return True
    except Exception:
        return False


def refresh(state_dir, library, post=None, email=None, timeout=HTTP_TIMEOUT_S):
    """Ask ASI and cache the answer; on any failure cache "off" and retry soon."""
    lock = _lock(state_dir)
    if lock is None:
        return None
    try:
        tok = router_token.read(library)
        email = email or user_email()
        if not tok or not email:
            return _write_cache(state_dir, False, RETRY_S)
        try:
            answer = (post or _post)(endpoint(), tok, {"email": email}, timeout)
        except Exception:
            answer = None
        if (isinstance(answer, dict) and isinstance(answer.get("enabled"), bool)
                and isinstance(answer.get("ttl_seconds"), int)):
            ttl = min(max(answer["ttl_seconds"], MIN_TTL_S), MAX_TTL_S)
            return _write_cache(state_dir, answer["enabled"], ttl)
        return _write_cache(state_dir, False, RETRY_S)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def endpoint():
    return (os.environ.get("TOPOGY_ROUTER_ACCESS_ENDPOINT") or "").strip() or DEFAULT_ENDPOINT


def user_email():
    for find in (_claude_email, _codex_email, _git_email):
        try:
            email = find()
        except Exception:
            email = None
        if isinstance(email, str) and email.strip():
            return email.strip()
    return None


def _claude_email():
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    with open(os.path.join(root, ".claude.json")) as fh:
        return (json.load(fh).get("oauthAccount") or {}).get("emailAddress")


def _codex_email():
    # Only the id_token's email claim is read; the credentials in this file stay put.
    root = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    with open(os.path.join(root, "auth.json")) as fh:
        id_token = (json.load(fh).get("tokens") or {}).get("id_token") or ""
    payload = id_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims.get("email")


def _git_email():
    out = subprocess.run(["git", "config", "--get", "user.email"], capture_output=True,
                         text=True, timeout=5, stdin=subprocess.DEVNULL)
    return out.stdout if out.returncode == 0 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies the Authorization header onto a redirect, wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url, tok, payload, timeout=HTTP_TIMEOUT_S):
    """ASI's JSON answer, or None on any failure."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}",
                 "User-Agent": "topogy-router"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except Exception:  # unreachable, timed out, truncated (IncompleteRead), not JSON
        return None


def _read_cache(state_dir):
    try:
        with open(os.path.join(state_dir, CACHE)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(state_dir, is_enabled, ttl):
    data = {"enabled": is_enabled, "checked_at": int(time.time()), "ttl": ttl}
    path = os.path.join(state_dir, CACHE)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _lock(state_dir):
    path = os.path.join(state_dir, LOCK)
    try:
        if time.time() - os.path.getmtime(path) > LOCK_STALE_S:
            os.unlink(path)
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return None
    return path


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "refresh":
            refresh(*sys.argv[2:])
    except Exception:
        pass
    sys.exit(0)
