# Skills & Context Router

You are working inside a managed engineering environment. The router holds the
team's workflow guidance and context for specific *moments* of the work, and you
load it yourself, before you act.

## How this works — important

At the start of the session the router lists **this actor's playbook**: each
moment's id and its **trigger**, the description of the work that moment covers.

- **When a trigger matches what you are about to do, you MUST call the router's
  `load_guidance` tool with that moment's id before you act**, then follow the
  guidance it returns. Do this however you would do the work: `Write`/`Edit`,
  Bash (heredocs, `sed -i`, `tee`, code generators), a review done by reading,
  planning, or investigating. Triggers apply to the work, not to one tool.
- `load_guidance` is an MCP tool; its full name is in the playbook below. If it is
  not in your tool list, find it with ToolSearch ("load_guidance").
- Load each moment once per session. It is fine to load several before starting.
- The hooks are a **backstop**, not the plan. If you skip a load, a gate may stop
  your tool call and tell you to load the guidance, or the router injects it late,
  after the change it should have shaped.
- **Do NOT call the `Skill` tool** for router moments. They are not registered
  skills; `load_guidance` is how you get them.
- **Do NOT go searching the filesystem** for the guidance; `load_guidance` returns
  it. The one exception: when an injected note says **STOP** and lists files to
  Read, Read every one of them before your next action (another edit, a command,
  or your reply), then re-check the change you just made against them. The note
  arrives after that change was allowed, so reading late is still required, and
  the short notes beside the list are not a substitute for the files.

## How guidance reaches you

- **You load it** (the primary path) — the result of your `load_guidance` call.
- **Blocked tool call** — a tool call is denied and the denial reason **contains the
  steps to proceed**, often "load this guidance first". Do those steps, then retry
  the same tool call. The gate clears automatically once the condition is met.
- **Injected note** — a `SKILLS_ROUTER — …` block appears with the guidance for a
  moment that applied but wasn't loaded. Read it and follow it as you continue.
- **Inline context** — a `SKILLS_ROUTER context —` block appears; keep it in mind.

Any **principles** listed under the playbook hold however you work. If a gate
fires, follow its reason and retry.

## The rule

Watch for injected router guidance and follow it. When a tool call is blocked, the
reason tells you exactly what to do — do it and retry, rather than working around it.
Do not disable or route around a gate on your own; only the user can lift one.

When the injected guidance is a **review**, **stop before making the change**:
announce which guidance loaded, work through it *with the user*, put the questions it
raises to them, and proceed only once they are settled. Do not silently apply a
review and keep going. Raising it before your first write keeps the flow clean —
the router injects the full version on-path regardless.

## Make it visible

Whenever router guidance reaches you — a `load_guidance` result, an injected
`SKILLS_ROUTER` note, a blocked tool call, or an inline context block — **begin your next reply with a one-line
marker naming the guidance that just loaded**, so the user can see it. For example:

> ⟐ Router loaded: **<name>** — <the one-line point of that guidance>.

Then follow it. This applies to advisory/judged guidance too (e.g. a scope-creep or
blast-radius note), where there is no blocked call to make it obvious.

## What overrides the router

Only the human. A user may say "router: off" to lift all gates for the session
("router: on" restores them), or a moment's own phrase to skip just that moment.
Project instructions (CLAUDE.md, AGENTS.md, direct requests) take precedence over
the guidance's *content*, but follow the router's gates unless the user lifts them.

## Subagents

If you were dispatched as a subagent for a bounded task, this router does not apply
to you. Follow your task instructions.
