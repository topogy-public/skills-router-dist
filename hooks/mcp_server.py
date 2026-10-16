#!/usr/bin/env python3
"""MCP server: the router's pull path.

A moment's trigger tells Claude to call load_guidance before it acts. That is the
primary way guidance arrives; the PreToolUse gate is the backstop. This is a
minimal stdio JSON-RPC server (initialize, tools/list, tools/call, ping) on the
standard library alone, so the stock-python3 rule holds.

The server never sees the session id, so the PostToolUse hook (record.py) marks
the moment delivered and traces it. Fails open: if the server cannot start, the
tool is simply absent and the hooks still deliver the guidance.

While ASI has the router off for this developer the server lists no tools, so
Claude is never offered load_guidance. On a machine with no answer yet, listing
waits for the first access check, as SessionStart does. The client keeps the list
for the whole connection, which outlives /clear, so when the cached answer flips
the server tells it to list again.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guidance
import lib

PROTOCOL_VERSION = "2025-06-18"
# How often to look for a flipped access answer. A new answer only applies from the
# next SessionStart, so a few seconds' lag doesn't matter.
WATCH_S = 5

_stdout = threading.Lock()
# Whether the last tools/list offered load_guidance; None until the client first lists.
_listed_on = None
_seen_mtime = None


def plugin_version():
    try:
        with open(os.path.join(lib.PLUGIN_ROOT, ".claude-plugin", "plugin.json")) as fh:
            return json.load(fh).get("version", "0")
    except (OSError, ValueError):
        return "0"


def _enum_prop(entries, what):
    """An optional array argument whose values come from scopes.yaml. Enum values
    can't carry descriptions, so each value's label goes in the description."""
    lines = "; ".join(f"{k}: {v.get('label') or k}" for k, v in entries.items())
    return {
        "type": "array",
        "items": {"type": "string", "enum": list(entries)},
        "description": f"Optional: the {what} the work touches, when paths alone don't show it. {lines}.",
    }


def tool_def():
    ids = [m["id"] for m in lib.load_playbook()]
    moment = {"type": "string", "description": "Moment id from the router playbook."}
    if ids:
        moment["enum"] = ids
    props = {
        "moment": moment,
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The files or directories you are about to create, change or review. "
                           "The router uses them to pick the guidance for that repo and area.",
        },
    }
    sc = guidance.scopes()
    if sc["areas"]:
        props["areas"] = _enum_prop(sc["areas"], "areas")
    if sc["platforms"]:
        props["platforms"] = _enum_prop(sc["platforms"], "platforms")
    return {
        "name": lib.LOAD_TOOL_SHORT,
        "description": (
            "Load the skills router's guidance for one playbook moment. You MUST call "
            "this as soon as a moment's trigger matches what you are about to do, "
            "before you act, then follow the guidance it returns. Moment ids and "
            "their triggers are listed in the router playbook at session start."
        ),
        "inputSchema": {"type": "object", "properties": props, "required": ["moment"]},
    }


def _strings(v):
    return [v] if isinstance(v, str) else [str(x) for x in (v or [])]


def load_guidance(args):
    """Return (text, is_error): the same selection the gate pushes, with skill
    bodies inline while they fit."""
    # The server never sees the session id, so it reads the machine-wide answer.
    if lib.router_disabled():
        return lib.OFF_REPLY, False
    args = args or {}
    mid = args.get("moment")
    m = lib.find_moment(mid)
    if not m:
        ids = ", ".join(x["id"] for x in lib.load_playbook()) or "(none)"
        return f"Unknown moment {mid!r}. Valid moment ids: {ids}.", True
    items = guidance.items_for(m)
    if items:
        units = guidance.context(os.getcwd(), _strings(args.get("paths")),
                                 _strings(args.get("areas")), _strings(args.get("platforms")))
        matched, hints = guidance.select(items, units)
        text, delivered, _ = guidance.render(mid, matched, hints, units, guidance.PULL_BUDGET, inline_bodies=True)
        return f"{text}\n\n{guidance.marker(delivered)}", False
    body = lib.body_for(m)
    if not body:
        return f"Moment '{mid}' has no guidance text yet; carry on.", False
    return f"SKILLS_ROUTER — guidance for '{mid}' (follow it for this work):\n\n{body}", False


def handle(msg):
    global _listed_on
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "skills-router", "version": plugin_version()},
        }
    if method == "tools/list":
        _listed_on = not lib.check_access()
        return {"tools": [tool_def()] if _listed_on else []}
    if method == "tools/call":
        if params.get("name") != lib.LOAD_TOOL_SHORT:
            raise LookupError(f"unknown tool {params.get('name')!r}")
        text, is_error = load_guidance(params.get("arguments"))
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    if method == "ping":
        return {}
    raise NotImplementedError(method)


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    send(out)


def send(msg):
    # The watcher thread writes too, and two messages must never interleave on one line.
    with _stdout:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def notify_if_access_changed():
    """Ask the client to list tools again when the cached answer no longer matches the
    last list. Without this, a session that turns the router on (a /clear, or a refresh
    landing just before SessionStart) gets the playbook but no load_guidance."""
    global _listed_on, _seen_mtime
    try:
        mtime = os.stat(os.path.join(lib.STATE_DIR, "access.json")).st_mtime_ns
    except OSError:
        mtime = None
    if mtime == _seen_mtime:
        return
    _seen_mtime = mtime
    on = not lib.router_disabled()
    if _listed_on is not None and on != _listed_on:
        _listed_on = on
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def _watch_access():
    while True:
        time.sleep(WATCH_S)
        try:
            notify_if_access_changed()
        except Exception:
            pass


def main():
    if lib.TELEMETRY:  # evals and a judge's model call never ask ASI, so nothing can flip
        threading.Thread(target=_watch_access, daemon=True).start()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("id") is None:
            continue  # notifications (e.g. notifications/initialized) get no reply
        try:
            reply(msg["id"], handle(msg))
        except NotImplementedError as exc:
            reply(msg["id"], error={"code": -32601, "message": f"method not found: {exc}"})
        except Exception as exc:
            reply(msg["id"], error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    main()
