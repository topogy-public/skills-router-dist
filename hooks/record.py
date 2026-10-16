#!/usr/bin/env python3
"""PostToolUse on Skill and load_guidance: record what Claude did so gates clear.

Skill: the call carries the skill name in tool_input.skill (a project skill like
`quality-checks`) or tool_input.name for some builds. If a gate never clears
after invoking a skill, run with SKILLS_ROUTER_DEBUG=1 and check the id that
actually arrives here against the skill ids in library/skills.yaml.

load_guidance (the router's MCP tool, the pull path): mark the moment delivered
so the gate stays silent, trace it with source=pull, and tell the user.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guidance
import lib


def record_pull(inp, tin):
    mid = tin.get("moment")
    if not lib.find_moment(mid):
        return  # unknown id: the tool already returned an error to Claude
    raw = inp.get("raw", {})
    response = json.dumps(raw.get("tool_response", raw.get("toolResponse", "")))
    if lib.OFF_REPLY in response:
        return  # the server read a newer "off" answer than this session's: nothing was delivered
    items = guidance.parse_marker(response) + [f"moment:{mid}"]
    state = lib.load_state(inp)
    if mid not in state["injected"]:
        state["injected"].append(mid)
    state["delivered"] += [i for i in items if i not in state["delivered"]]
    lib.save_state(inp, state)
    skill_ids = {it["id"] for it in guidance.library() if it["_skill"]}
    lib.trace(inp, "PostToolUse", "inject", moment=mid, source="pull", items=len(items) - 1,
              skills=[i for i in items if i in skill_ids],
              paths=tin.get("paths"), areas=tin.get("areas"), platforms=tin.get("platforms"))
    lib.emit_notice(f"Router: Claude loaded the '{mid}' guidance.")


def main():
    if os.environ.get("SKILLS_ROUTER_JUDGE"):
        return  # inside a judge's model call
    inp = lib.read_input()
    if lib.load_state(inp).get("disabled"):
        return
    tin = inp.get("tool_input", {}) or {}
    if inp.get("tool_name", "").endswith("__" + lib.LOAD_TOOL_SHORT):
        record_pull(inp, tin)
        return
    skill = tin.get("skill") or tin.get("name")
    if not skill:
        return
    # Skills may arrive namespaced (plugin:skill); record both forms.
    lib.mark_invoked(inp, skill)
    if ":" in skill:
        lib.mark_invoked(inp, skill.split(":", 1)[1])
    lib.trace(inp, "PostToolUse", "recorded-invoke", skill=skill)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router record: {exc}\n")
    sys.exit(0)
