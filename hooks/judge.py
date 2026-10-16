#!/usr/bin/env python3
"""Stop / PostToolUse: evaluate judged and hybrid moments with a cheap model.

Some moments have no deterministic signal ("the work outgrew the request", "this
change has real blast radius"). For those, a small model reads the recent session
and decides whether the condition holds, then the moment enacts (inject the skill
body as advice, or block a Stop until it is addressed).

- judged  moments: run at their declared event (usually Stop).
- hybrid  moments: a cheap tool/path matcher gates WHEN the model runs (usually
                   PostToolUse), so the model is not called on every action.

The model is called via the NATIVE harness CLI — `codex exec` under Codex,
`claude -p` under Claude — so the router does not depend on any single agent.
Override with SKILLS_ROUTER_JUDGE_BACKEND (claude|codex) / SKILLS_ROUTER_JUDGE_MODEL.
The call is guarded with SKILLS_ROUTER_JUDGE=1 so the nested agent does not
re-trigger the router. Fires at most once per moment per session; errors fail open.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

BACKEND = os.environ.get("SKILLS_ROUTER_JUDGE_BACKEND", "auto").lower()  # auto|claude|codex
JUDGE_MODEL = os.environ.get("SKILLS_ROUTER_JUDGE_MODEL")  # None -> each backend's default
# After a judged/hybrid moment evaluates to "does not fire", do not re-run its
# model call for this many seconds (per session). Bounds cost when a condition
# keeps not holding. A moment that fires is recorded permanently (fires-once).
COOLDOWN = int(os.environ.get("SKILLS_ROUTER_JUDGE_COOLDOWN", "600"))
EDIT_MARKERS = ("[tool Write", "[tool Edit", "[tool MultiEdit", "[tool NotebookEdit")


def recent_transcript(path, max_chars=4000):
    if not path or not os.path.exists(path):
        return ""
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return ""
    out = []
    for line in lines[-40:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        role = msg.get("role") or obj.get("type") or "?"
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for b in content:
                t = b.get("type")
                if t == "text":
                    parts.append(b.get("text", ""))
                elif t == "tool_use":
                    parts.append(f"[tool {b.get('name')} {json.dumps(b.get('input', {}))[:160]}]")
                elif t == "tool_result":
                    parts.append(f"[result {str(b.get('content'))[:120]}]")
            text = " ".join(parts)
        if text.strip():
            out.append(f"{role}: {text.strip()}")
    return "\n".join(out)[-max_chars:]


def _run_claude(prompt):
    cmd = ["claude", "-p", prompt, "--model", JUDGE_MODEL or "claude-haiku-4-5-20251001",
           "--output-format", "text", "--strict-mcp-config",
           "--disallowedTools", "Bash Edit Write Read"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                         env=dict(os.environ, SKILLS_ROUTER_JUDGE="1"))
    return res.stdout


def _run_codex(prompt):
    # Run in a neutral dir so the project's own .codex/hooks.json is not loaded;
    # read-only + bypass-hook-trust keep the judge from doing or being gated by work.
    cmd = ["codex", "exec", prompt, "-s", "read-only", "--skip-git-repo-check",
           "--dangerously-bypass-hook-trust", "-C", tempfile.gettempdir()]
    if JUDGE_MODEL:
        cmd += ["-m", JUDGE_MODEL]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, SKILLS_ROUTER_JUDGE="1"))
    return res.stdout


def _extract_verdict(text):
    if not text:
        return None
    # Prefer a flat object that mentions "fires"; take the last one (the final answer).
    cands = re.findall(r'\{[^{}]*?"fires"[^{}]*?\}', text, re.S)
    for c in reversed(cands):
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
    return None


def ask_model(judge_prompt, context, harness="claude"):
    prompt = (
        "You are a workflow judge for a coding session. Decide whether the CONDITION "
        "clearly holds, based on the RECENT SESSION.\n\n"
        f"CONDITION:\n{judge_prompt}\n\n"
        f"RECENT SESSION:\n{context}\n\n"
        'Respond with ONLY JSON: {"fires": true|false, "reason": "<one sentence>"}. '
        "Set fires=true only if the condition clearly and materially holds."
    )
    backend = BACKEND if BACKEND in ("claude", "codex") else ("codex" if harness == "codex" else "claude")
    try:
        raw = _run_codex(prompt) if backend == "codex" else _run_claude(prompt)
    except Exception:
        return None
    return _extract_verdict(raw)


def main():
    if os.environ.get("SKILLS_ROUTER_JUDGE"):
        return  # recursion guard (inside a judge's own model call)
    if os.environ.get("SKILLS_ROUTER_NO_JUDGE"):
        return  # judge disabled: keep deterministic gates, skip all model calls
    inp = lib.read_input()
    event = inp.get("hook_event_name") or ("PostToolUse" if inp.get("tool_name") else "Stop")
    state = lib.load_state(inp)
    if state.get("bypass") or state.get("disabled"):
        return
    now = time.time()
    cooldown = state.get("cooldown", {})

    candidates = []
    for m in lib.load_playbook():
        catch = m.get("catch", {}) or {}
        if catch.get("regime") not in ("judged", "hybrid"):
            continue
        if catch.get("event") != event:
            continue
        if m["id"] in state.get("skipped", []) or m["id"] in state.get("judged_fired", []):
            continue
        if now - cooldown.get(m["id"], 0) < COOLDOWN:
            continue  # recently judged and did not fire — hold off
        if not catch.get("judge_prompt"):
            continue
        if catch.get("regime") == "hybrid" and not lib.tool_call_matches(catch.get("match"), inp):
            continue
        candidates.append(m)
    if not candidates:
        return

    context = recent_transcript(inp.get("transcript_path"))
    # At Stop, only judge if real work happened this session (avoid judging pure Q&A).
    if event == "Stop" and not any(mk in context for mk in EDIT_MARKERS):
        return

    notes, dirty = [], False
    for m in candidates:
        verdict = ask_model(m["catch"]["judge_prompt"], context, inp.get("harness", "claude"))
        if not verdict:
            lib.trace(inp, event, "judge-error", moment=m["id"])
            continue
        if not verdict.get("fires"):
            cooldown[m["id"]] = now
            state["cooldown"] = cooldown
            dirty = True
            lib.trace(inp, event, "judge-pass", moment=m["id"], why=verdict.get("reason"))
            continue
        why = verdict.get("reason", "")
        body = lib.body_for(m)
        state.setdefault("judged_fired", []).append(m["id"])
        dirty = True
        lib.trace(inp, event, "judge-fire", moment=m["id"], why=why)
        if m.get("enact") == "force" and m.get("mode") == "block" and event == "Stop":
            lib.save_state(inp, state)
            print(json.dumps({"decision": "block",
                              "reason": f"SKILLS_ROUTER — '{m['id']}' ({why}). Address this before finishing:\n\n{body}"}))
            return
        notes.append(f"SKILLS_ROUTER — '{m['id']}' fired (judged): {why}\n\n{body}")

    if dirty:
        lib.save_state(inp, state)
    if notes:
        lib.emit_context(event, "\n\n".join(notes))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router judge: {exc}\n")
    sys.exit(0)
