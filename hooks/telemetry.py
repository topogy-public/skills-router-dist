#!/usr/bin/env python3
"""Ship the router's decision trace to ASI as OTLP/JSON logs.

trace.jsonl is the outbox. lib.trace() appends one line per decision and calls
maybe_spawn(), which starts this file detached, at most once per MIN_INTERVAL_S,
so no hook ever waits on the network. The sender posts unsent lines in batches,
remembers how far it got in telemetry.offset, and gives up silently on any error;
unsent lines wait for the next run.

It authenticates with the org's router token (router_token.py), which ships with
the customer's library plugin, so telemetry is on with no setup. A library
without one sends nothing.

Knobs:
  TOPOGY_ROUTER_TELEMETRY=0         opt out
  TOPOGY_ROUTER_TELEMETRY_ENDPOINT  full logs URL, for testing against a local or staging ASI
"""
import calendar
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_token  # noqa: E402

DEFAULT_ENDPOINT = "https://otel.topogy.com/topogy_router/v1/logs"
MIN_INTERVAL_S = 30
BATCH_LINES = 500
HTTP_TIMEOUT_S = 10
LOCK_STALE_S = 120
AUTH_BACKOFF_S = 24 * 3600
ROTATE_BYTES = 5 * 1024 * 1024
# Hooks open the trace, write one line and close it, so a write that began before the
# rename finishes into the old file within microseconds; this waits that out.
ROTATE_GRACE_S = 1.0

TRACE = "trace.jsonl"
OFFSET = "telemetry.offset"
OLD_OFFSET = "telemetry.offset.1"
LOCK = "telemetry.lock"
LAST = "telemetry.last"
AUTH_FAILED = "telemetry.auth-failed"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Only these trace fields leave the machine. Everything else (why, paths, areas,
# platforms, library, ...) can quote commands or reveal a repo's layout.
_FIELDS = {"session": "session.id", "event": "hook.event", "moment": "moment",
           "mode": "mode", "tool": "tool", "skill": "skill", "source": "source"}


def token(library):
    if os.environ.get("TOPOGY_ROUTER_TELEMETRY") == "0":
        return None
    return router_token.read(library)


def endpoint():
    return (os.environ.get("TOPOGY_ROUTER_TELEMETRY_ENDPOINT") or "").strip() or DEFAULT_ENDPOINT


def maybe_spawn(state_dir, plugin_root, library, force=False):
    """Start a detached sender when there's a token and none started recently."""
    try:
        if not token(library):
            return False
        last = os.path.join(state_dir, LAST)
        if not force:
            try:
                if time.time() - os.path.getmtime(last) < MIN_INTERVAL_S:
                    return False
            except OSError:
                pass
        with open(last, "w"):
            pass
        os.utime(last)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "flush", state_dir, plugin_root, library],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        return True
    except Exception:
        return False


def flush(state_dir, plugin_root, library, post=None):
    tok = token(library)
    if not tok or _auth_failed(state_dir, tok):
        return 0
    lock = _lock(state_dir)
    if lock is None:
        return 0
    try:
        return _flush(state_dir, plugin_root, library, tok, post or _post, lock)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _flush(state_dir, plugin_root, library, tok, post, lock):
    trace = os.path.join(state_dir, TRACE)
    old = trace + ".1"
    send = (_version(plugin_root), _version(library), tok, post, lock)
    # The previous rotation's file first: a write that stalled across that rename ends up there.
    sent, done = _send_file(state_dir, old, OLD_OFFSET, *send)
    if not done:
        return sent
    more, done = _send_file(state_dir, trace, OFFSET, *send)
    sent += more
    if done and _read_offset(state_dir, OFFSET) >= ROTATE_BYTES:
        # Everything is sent, so the old file can go: rename the trace over it. Hooks
        # recreate trace.jsonl on their next write.
        try:
            os.replace(trace, old)
        except OSError:
            return sent
        _write(os.path.join(state_dir, OLD_OFFSET), str(_read_offset(state_dir, OFFSET)))
        _write(os.path.join(state_dir, OFFSET), "0")
        time.sleep(ROTATE_GRACE_S)
        sent += _send_file(state_dir, old, OLD_OFFSET, *send)[0]
    return sent


def _send_file(state_dir, path, offset_name, plugin_version, library_version, tok, post, lock):
    """Send `path` from its saved offset. Returns (records accepted, reached the end)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0, True
    offset = _read_offset(state_dir, offset_name)
    if offset > size:  # the file was truncated or replaced since the last send
        offset = 0
    sent = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            lines, end = _read_lines(fh, BATCH_LINES)
            if not lines:
                return sent, True
            records = [r for r in map(to_record, lines) if r]
            if records:
                status = post(endpoint(), tok, _payload(records, plugin_version, library_version))
                if status in (401, 403):
                    _write(os.path.join(state_dir, AUTH_FAILED), _digest(tok))
                    return sent, False
                if 200 <= status < 300:
                    sent += len(records)
                elif not (400 <= status < 500 and status not in (408, 429)):
                    return sent, False  # worth retrying: keep the offset and try again later
                # A 4xx other than throttling means ASI will never accept this batch, and
                # retrying it would wedge everything queued behind it: skip past it.
            _write(os.path.join(state_dir, offset_name), str(end))
            os.utime(lock)  # still alive: keep a long backlog from looking stale


def to_record(line):
    """One trace line as ((actor, harness), OTLP log record), or None if it can't be sent."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not rec.get("id") or not rec.get("decision"):
        return None
    try:
        nanos = calendar.timegm(time.strptime(str(rec.get("ts")), _TS_FORMAT)) * 1_000_000_000
    except ValueError:
        return None
    attrs = [_kv("event.name", f"topogy_router.{rec['decision']}"), _kv("event.id", str(rec["id"]))]
    for field, key in _FIELDS.items():
        value = rec.get(field)
        if isinstance(value, str) and value:
            attrs.append(_kv(key, value))
    count = rec.get("items", rec.get("moments"))
    if isinstance(count, list):
        count = len(count)
    if isinstance(count, int) and not isinstance(count, bool):
        attrs.append(_kv("count", count))
    # Library ids, not user content: which moments a prompt matched, which skills went in.
    for field in ("moments", "skills"):
        ids = rec.get(field)
        if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
            attrs.append(_kv(field, ids))
    key = (str(rec.get("actor") or ""), str(rec.get("harness") or ""))
    return key, {"timeUnixNano": str(nanos), "attributes": attrs}


def _payload(records, plugin_version, library_version):
    groups = {}
    for key, record in records:
        groups.setdefault(key, []).append(record)
    resource_logs = []
    for (actor, harness), recs in groups.items():
        resource = [("service.name", "topogy-router"), ("service.version", plugin_version),
                    ("topogy_router.library_version", library_version),
                    ("topogy_router.actor", actor), ("topogy_router.harness", harness)]
        resource_logs.append({
            "resource": {"attributes": [_kv(k, v) for k, v in resource if v]},
            "scopeLogs": [{"scope": {"name": "topogy-router"}, "logRecords": recs}],
        })
    return {"resourceLogs": resource_logs}


def _kv(key, value):
    if isinstance(value, list):
        return {"key": key, "value": {"arrayValue": {"values": [{"stringValue": v} for v in value]}}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib copies the Authorization header onto a redirect, wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url, tok, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}",
                 "User-Agent": "topogy-router"},
    )
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _read_lines(fh, limit):
    """Up to `limit` complete lines. A last line still being written is left for next time."""
    lines = []
    while len(lines) < limit:
        start = fh.tell()
        line = fh.readline()
        if not line.endswith(b"\n"):
            fh.seek(start)
            break
        lines.append(line)
    return lines, fh.tell()


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


def _auth_failed(state_dir, tok):
    path = os.path.join(state_dir, AUTH_FAILED)
    try:
        with open(path) as fh:
            same_token = fh.read().strip() == _digest(tok)
        return same_token and time.time() - os.path.getmtime(path) < AUTH_BACKOFF_S
    except OSError:
        return False


def _digest(tok):
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def _read_offset(state_dir, name):
    try:
        with open(os.path.join(state_dir, name)) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write(path, text):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _plugin_json(root):
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json")) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _version(root):
    return str(_plugin_json(root).get("version") or "")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "flush":
            flush(*sys.argv[2:])
    except Exception:
        pass
    sys.exit(0)
