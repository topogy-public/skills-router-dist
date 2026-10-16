#!/usr/bin/env python3
"""SessionStart: inject the router "brain" plus the resolved playbook.

Fires on startup|resume|clear|compact, so the router survives /clear and every
compaction (which is the only way injected context comes back verbatim). The
brain makes Claude a cooperative participant; the hooks remain the enforcement.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guidance
import lib


def main():
    if os.environ.get("SKILLS_ROUTER_JUDGE"):
        return  # inside a judge's model call — stay silent
    inp = lib.read_input()

    # Decided once per session and pinned in its state: a background refresh can land
    # mid-session, and gates must not switch off under a playbook that was already
    # injected. Compaction keeps the session, so it keeps the pinned answer.
    state = lib.load_state(inp)
    if inp.get("source") != "compact" or "disabled" not in state:
        state["disabled"] = lib.check_access()
        lib.save_state(inp, state)
    if state["disabled"]:
        lib.trace(inp, "SessionStart", "disabled", source=inp.get("source"), actor=lib.ACTOR)
        return

    brain_path = os.path.join(lib.PLUGIN_ROOT, "hooks", "brain.md")
    brain = ""
    if os.path.exists(brain_path):
        with open(brain_path) as fh:
            brain = fh.read().strip()

    # With scoped guidance, list only the moments that have something for this
    # repo, and say what the router knows about it.
    units = guidance.context(inp.get("cwd"))
    repo = units[0]["repo"]
    has_guidance = bool(guidance.library())
    repo_line = ""
    if has_guidance:
        sc = guidance.scopes()
        areas = sorted(k for k, a in sc["areas"].items()
                       if any((loc.get("repo") or "").lower() == repo for loc in a.get("locations") or []))
        plats = sorted({p for a in sc["areas"].values() for loc in a.get("locations") or []
                        if (loc.get("repo") or "").lower() == repo for p in loc.get("platforms") or []})
        repo_line = (f"\nThis session's repo: {repo or '(not a git repo with a remote)'}"
                     + (f" — areas: {', '.join(areas)}" if areas else "")
                     + (f"; platforms: {', '.join(plats)}" if plats else "") + ".\n")

    def relevant(m):
        items = guidance.items_for(m)
        if not has_guidance or not items:
            return not has_guidance or bool(lib.body_for(m))
        return any(guidance._repo_ok(it.get("scope") or {}, units[0]) or (it.get("scope") or {}).get("general")
                   for it in items)

    lines = []
    for m in lib.load_playbook():
        if not relevant(m):
            continue
        catch = m.get("catch", {}) or {}
        how = catch.get("regime", "?")
        lines.append(
            f"- `{m['id']}` — {lib.trigger_for(m).strip()} "
            f"[{m.get('enact','nudge')}/{m.get('mode','advise')}, {how}]"
        )
    table = "\n".join(lines) if lines else "- (no moments taken by this actor yet)"

    principles = [
        f"- {m['principle'].strip()}"
        for m in lib.load_playbook()
        if m.get("principle")
    ]
    principles_block = ""
    if principles:
        principles_block = (
            "\n\n## Principles (hold regardless of which tool you use)\n"
            + "\n".join(principles)
        )

    state = lib.load_state(inp)
    if inp.get("source") == "compact":
        # Compaction can drop guidance text that was delivered earlier, so let
        # both paths deliver it again. Gates satisfied by real actions
        # (`invoked`) stay satisfied. /clear starts a new session id anyway.
        state["injected"], state["delivered"], state["pull_denied"] = [], [], []
        lib.save_state(inp, state)
    already = ", ".join(state.get("invoked", []))
    already = f"\nSkills already invoked this session (gates satisfied): {already}." if already else ""

    ctx = (
        f"<SKILLS_ROUTER actor=\"{lib.ACTOR}\">\n"
        f"{brain}\n\n"
        f"## This actor's playbook — moments and their triggers\n"
        f"When a trigger matches what you are about to do, call `{lib.LOAD_TOOL}` "
        f"with {{\"moment\": \"<id>\"}} before you act, and follow what it returns.\n"
        f"{repo_line}"
        f"{table}"
        f"{principles_block}\n"
        f"{already}\n"
        f"</SKILLS_ROUTER>"
    )
    banner = f"Topogy Router loaded with the {lib.actor_display_name()} skills pack."
    warning = lib.compat_warning()
    if warning:
        banner += f" Warning: {warning}."
    lib.emit_context("SessionStart", ctx, system_message=banner)
    lib.trace(inp, "SessionStart", "injected", moments=len(lines), source=inp.get("source"), actor=lib.ACTOR, library=lib.LIBRARY, compat=warning)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail open
        sys.stderr.write(f"skills-router session_start: {exc}\n")
    sys.exit(0)
