#!/usr/bin/env python3
"""PreToolUse: catch deterministic/hybrid moments on this tool call and enact.

Enact modes:
  force  + block  -> deny the tool call until the clear-condition is met; on the
                     first block, the skill body is injected into the deny reason
                     (the how-to for clearing the gate).
  inline / advise -> allow, inject the skill body (dynamic-load) the first time the
                     path is taken, then a terse reminder thereafter.
  nudge           -> allow, attach a short reminder.
  pull            -> Claude should already have loaded this moment's guidance
                     itself (load_guidance) when its trigger matched. If it
                     hasn't, deny the first matching call once and ask it to load;
                     after that, fall back to inline so a missing tool never wedges.

Clear-condition is a precondition predicate (world-state) when present, else the
paired skill being invoked. Shadow (global or per-moment) never denies. Any error
fails open (allow).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guidance
import lib

READ_FIRST_RESERVE = 1200  # room kept for the read-these-files directive


def run_precondition(moment, inp):
    pc = moment.get("precondition") or {}
    cmd = pc.get("check")
    if not cmd:
        return True, ""
    env = dict(os.environ)
    env["SR_PLUGIN_ROOT"] = lib.PLUGIN_ROOT
    env["SR_LIBRARY"] = lib.LIBRARY
    env["SR_FILE_PATH"] = inp.get("file_path", "") or ""
    env["SR_CWD"] = inp.get("cwd", "") or ""
    for key, val in pc.items():
        if key == "check":
            continue
        env["SR_" + key.upper()] = ",".join(map(str, val)) if isinstance(val, list) else str(val)
    try:
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30, env=env)
    except Exception:
        return True, ""  # fail open
    return res.returncode == 0, (res.stdout.strip() or res.stderr.strip())


def main():
    if os.environ.get("SKILLS_ROUTER_JUDGE"):
        return  # inside a judge's model call — do not re-route
    inp = lib.read_input()
    tool = inp.get("tool_name", "") or inp.get("action", "") or "this action"
    state = lib.load_state(inp)
    if state.get("bypass") or state.get("disabled"):
        return

    deny_reasons, ask_reasons, notes = [], [], []
    fresh = []  # inline moments injecting for the first time on this call
    pulls = []  # pull moments whose guidance Claude should load before this call
    scoped = []  # (moment, guidance.yaml items) to push on this call
    units = None  # this call's guidance context, built on first need
    dirty = False

    for m in lib.load_playbook():
        catch = m.get("catch", {}) or {}
        if catch.get("event") != "PreToolUse":
            continue
        if catch.get("regime") not in ("deterministic", "hybrid"):
            continue
        if not lib.tool_call_matches(catch.get("match"), inp):
            continue
        if m["id"] in state.get("skipped", []):
            lib.trace(inp, "PreToolUse", "skipped", moment=m["id"], tool=tool)
            continue

        enact = m.get("enact", "nudge")
        eff_shadow = lib.SHADOW or bool(m.get("shadow"))
        first_time = m["id"] not in state["injected"]
        body = lib.body_for(m)          # full guidance, for inline injection
        summary = lib.summary_for(m)    # concise directive, for a blocked call

        # Scoped guidance (guidance.yaml): what matters is which items this call's
        # file reaches that haven't reached Claude yet. None: silent. Some: a pull
        # moment stops the call once per moment, then pushes; inline pushes.
        items = guidance.items_for(m) if enact in ("pull", "inline") else []
        if items:
            if units is None:
                fp = inp.get("file_path")
                units = guidance.context(inp.get("cwd"), [fp] if fp else [])
            new, _ = guidance.select(items, units, exclude=state["delivered"])
            if not new:
                continue
            # Once per moment per context: moving to another repo or area asks for
            # a fresh pull, as the trigger says, but never twice for the same one.
            m["_deny_key"] = f"{m['id']}@{guidance.describe(units)}"
            if enact == "pull" and m["_deny_key"] not in state["pull_denied"] and not eff_shadow:
                pulls.append(m)
            else:
                if enact == "pull" and eff_shadow:
                    lib.trace(inp, "PreToolUse", "would-block-pull", moment=m["id"], tool=tool)
                scoped.append((m, new))
            continue

        # Pull: once delivered (by either path) the gate stays silent. Not yet
        # delivered: stop this call once; a second miss means the tool is
        # unreachable, so push the guidance in like inline instead.
        if enact == "pull":
            if not first_time or not body:
                continue
            if m["id"] in state["pull_denied"] or eff_shadow:
                if eff_shadow:
                    lib.trace(inp, "PreToolUse", "would-block-pull", moment=m["id"], tool=tool)
                fresh.append(m)
            else:
                pulls.append(m)
            continue

        # Inline: dynamic-load the skill body when the path is taken; never blocks.
        # First-time guidance is collected and sized together after the loop.
        if enact == "inline":
            if first_time and body:
                fresh.append(m)
            else:
                notes.append(f"SKILLS_ROUTER: '{m['id']}' still applies — keep following its guidance.")
                lib.trace(inp, "PreToolUse", "inject-repeat", moment=m["id"], tool=tool)
            continue

        # Determine whether the gate is satisfied.
        if m.get("precondition"):
            met, why = run_precondition(m, inp)
            if met:
                lib.trace(inp, "PreToolUse", "satisfied-precondition", moment=m["id"], tool=tool)
                continue
        else:
            why = None
            invocable = [s["id"] for s in m.get("skill_objs", []) if s.get("invoke")]
            if invocable:
                pending = [s for s in invocable if s not in state["invoked"]]
                if not pending:
                    lib.trace(inp, "PreToolUse", "satisfied", moment=m["id"], tool=tool)
                    continue
            elif not first_time:
                # Body-inject review gate: satisfied once its guidance has been shown.
                lib.trace(inp, "PreToolUse", "satisfied-ack", moment=m["id"], tool=tool)
                continue

        if enact == "force" and m.get("mode") in ("block", "ask"):
            if eff_shadow:
                notes.append(f"SKILLS_ROUTER (shadow): would block this {tool} for '{m['id']}'"
                             + (f" — {why}" if why else "") + ".")
                lib.trace(inp, "PreToolUse", "would-block", moment=m["id"], tool=tool, why=why)
                continue
            override = m.get("override", "router: off")
            reason = f"Moment '{m['id']}' ({lib.trigger_for(m)})"
            reason += f": {why}. " if why else ". "
            if first_time and summary:
                reason += ("What to do (guidance, not a skill to call — do NOT use the Skill tool): "
                           f"{summary} Then retry this {tool}. ")
            else:
                reason += f"Complete the steps for '{m['id']}', then retry this {tool}. "
            reason += f'To bypass once, the user can say "{override}" (or "router: off" for the session).'
            if first_time:
                state["injected"].append(m["id"]); dirty = True
            if m.get("mode") == "ask":
                ask_reasons.append(reason)
                lib.trace(inp, "PreToolUse", "ask", moment=m["id"], tool=tool, why=why)
            else:
                deny_reasons.append(reason)
                lib.trace(inp, "PreToolUse", "block", moment=m["id"], tool=tool, why=why)
            break  # one gate at a time, in priority order — the next retry hits the next
        else:
            notes.append(f"SKILLS_ROUTER: '{m['id']}' applies — follow its guidance before continuing"
                         + (f" ({why})" if why else "") + ".")
            lib.trace(inp, "PreToolUse", "nudge", moment=m["id"], tool=tool, why=why)

    if pulls and not ask_reasons:
        fp = inp.get("file_path")

        def call(m):
            return json.dumps({"moment": m["id"], **({"paths": [fp]} if fp else {})})

        lines = "\n".join(f"  - {call(m)} — {lib.trigger_for(m)}" for m in pulls)
        deny_reasons.append(
            f"SKILLS_ROUTER — load the guidance first. Before this {tool}, call "
            f"{lib.LOAD_TOOL} for each moment below, follow what it returns, then "
            f"retry this {tool}:\n{lines}\n"
            "If that tool is unavailable, just retry and the router will inject the guidance."
        )
        for m in pulls:
            state["pull_denied"].append(m.get("_deny_key", m["id"]))
            lib.trace(inp, "PreToolUse", "block-pull", moment=m["id"], tool=tool)
        dirty = True

    # A denied or asked call carries no context, so first-time guidance waits for
    # the retry rather than being marked injected and lost.
    stopping = bool(deny_reasons or ask_reasons)

    # Several moments can match one call, and they share one injection budget:
    # full text if it all fits, else skill bodies become paths to Read, else the
    # overflow defers to the next matching call (left un-injected so it fires again).
    pushed, to_read = [], []
    if fresh and not stopping:
        header = "SKILLS_ROUTER — guidance for '{}' (read and follow; nothing to invoke):\n\n"
        room = lib.INJECT_BUDGET - sum(len(n) for n in notes)
        full = [header.format(m["id"]) + lib.body_for(m) for m in fresh]
        if sum(map(len, full)) <= room:
            chosen, decision = full, "inject"
        else:
            chosen = [header.format(m["id"]) + lib.pointer_body_for(m) for m in fresh]
            decision = "inject-pointer"
            room -= READ_FIRST_RESERVE
        for m, text in zip(fresh, chosen):
            if len(text) > room:
                notes.append(f"SKILLS_ROUTER: '{m['id']}' also applies — its guidance loads on your next matching call.")
                lib.trace(inp, "PreToolUse", "inject-deferred", moment=m["id"], tool=tool)
                continue
            room -= len(text)
            notes.append(text)
            if decision == "inject-pointer":
                to_read += [lib.skill_body_path(s) for s in m.get("skill_objs", []) if s.get("body")]
            state["injected"].append(m["id"]); dirty = True
            pushed.append(m["id"])
            lib.trace(inp, "PreToolUse", decision, moment=m["id"], tool=tool, source="push",
                      skills=[s["id"] for s in m.get("skill_objs", []) if s.get("body") or s.get("description")])

    # Scoped items share what's left of the budget. Skill bodies go as paths to
    # Read; items that don't fit stay undelivered and come on the next call.
    if scoped and not stopping:
        room = lib.INJECT_BUDGET - sum(len(n) + 1 for n in notes) - READ_FIRST_RESERVE
        for m, new in scoped:
            text, delivered, overflow = guidance.render(m["id"], new, {}, units, room, inline_bodies=False)
            if not delivered:
                notes.append(f"SKILLS_ROUTER: '{m['id']}' also applies — its guidance loads on your next matching call.")
                lib.trace(inp, "PreToolUse", "inject-deferred", moment=m["id"], tool=tool)
                continue
            room -= len(text) + 1
            notes.append(text)
            state["delivered"] += [i["id"] for i in delivered]
            if m["id"] not in state["injected"]:
                state["injected"].append(m["id"])
            dirty = True
            pushed.append(m["id"])
            to_read += [os.path.join(lib.LIBRARY, i["body"]) for i in delivered if i["_skill"] and i.get("body")]
            lib.trace(inp, "PreToolUse", "inject-pointer" if to_read else "inject", moment=m["id"],
                      tool=tool, source="push", items=len(delivered), deferred=len(overflow),
                      skills=[i["id"] for i in delivered if i["_skill"]])

    if to_read:
        # The call this guidance rides on has already been allowed, so ask for
        # the reads before the agent's next action and a re-check of this one.
        files = "\n".join(f"  - {p}" for p in dict.fromkeys(to_read))
        notes.insert(0, (
            "SKILLS_ROUTER — STOP. Before your next action (another edit, a command, "
            "or your reply), Read each of these files. They hold the guidance for the "
            "moments below, and the notes below are not a substitute for them:\n"
            f"{files}\n"
            "Then re-check the change you just made against them and fix anything "
            "that doesn't conform.\n"
        ))

    if dirty:
        lib.save_state(inp, state)
    if deny_reasons:
        notice = None
        if pulls:
            ids = ", ".join(f"'{m['id']}'" for m in pulls)
            notice = f"Router: asked Claude to load the {ids} guidance before this {tool}."
        lib.emit_deny(" ".join(deny_reasons), system_message=notice)
    elif ask_reasons:
        lib.emit_ask(" ".join(ask_reasons))
    elif notes:
        notice = None
        if pushed:
            ids = ", ".join(f"'{i}'" for i in pushed)
            notice = f"Router: injected the {ids} guidance on this {tool}."
        lib.emit_context("PreToolUse", "\n".join(notes), system_message=notice)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router gate: {exc}\n")
    sys.exit(0)
