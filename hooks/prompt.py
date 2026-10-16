#!/usr/bin/env python3
"""UserPromptSubmit: handle human overrides and cheap prompt-intent nudges.

Overrides (the human is the only thing that overrides the router):
  "router: off" / "router: on"   lift or restore all gates for the session
  a moment's own override phrase  skip just that moment for the session

Intent nudge: if a taken moment declares `intent` regexes and one matches the
prompt, surface a note so Claude loads the skill before acting. This is only a
nudge; the deterministic gate is what actually enforces.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib


def main():
    if os.environ.get("SKILLS_ROUTER_JUDGE"):
        return  # inside a judge's model call
    inp = lib.read_input()
    prompt = inp.get("prompt", "") or ""
    if not prompt:
        return
    state = lib.load_state(inp)
    if state.get("disabled"):
        return  # switched off by ASI: "router: on" must not bring it back

    if re.search(r"\brouter[: ]+(off|skip|bypass)\b", prompt, re.I):
        state["bypass"] = True
        lib.save_state(inp, state)
        lib.trace(inp, "UserPromptSubmit", "bypass-on")
        lib.emit_context("UserPromptSubmit", "SKILLS_ROUTER: gates lifted for this session by the user.")
        return
    if re.search(r"\brouter[: ]+on\b", prompt, re.I):
        state["bypass"] = False
        lib.save_state(inp, state)
        lib.trace(inp, "UserPromptSubmit", "bypass-off")
        lib.emit_context("UserPromptSubmit", "SKILLS_ROUTER: gates active again.")
        return

    playbook = lib.load_playbook()

    # Per-moment override phrases skip just that moment.
    for m in playbook:
        phrase = m.get("override")
        if phrase and phrase.lower() in prompt.lower():
            if m["id"] not in state["skipped"]:
                state["skipped"].append(m["id"])
            lib.save_state(inp, state)
            lib.trace(inp, "UserPromptSubmit", "skip-moment", moment=m["id"])
            lib.emit_context("UserPromptSubmit", f"SKILLS_ROUTER: moment '{m['id']}' skipped for this session.")
            return

    if state.get("bypass"):
        return

    # Cheap prompt-intent nudges. This fires before any work starts, which is
    # the moment for Claude to pull the guidance itself.
    hits = []
    for m in playbook:
        if m["id"] in state["injected"]:
            continue  # already delivered this session
        for pat in m.get("intent", []) or []:
            if re.search(pat, prompt, re.I):
                hits.append(m["id"])
                break
    if hits:
        body = "\n".join(f'- {{"moment": "{h}"}}' for h in hits)
        lib.emit_context(
            "UserPromptSubmit",
            f"SKILLS_ROUTER: this request matches these moments. Before you start the "
            f"work, call {lib.LOAD_TOOL} for each one and follow what it returns:\n"
            + body + "\n(These are router moments, not skills: do not use the Skill tool.)",
        )
        lib.trace(inp, "UserPromptSubmit", "nudge", moments=hits)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router prompt: {exc}\n")
    sys.exit(0)
