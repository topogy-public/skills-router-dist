"""Shared helpers for the skills-router hooks.

Every hook is a separate process that reads one JSON object on stdin, consults
the moment/skill library + per-session state, and prints at most one JSON object
on stdout. Anything unexpected fails OPEN (exit 0, no output) so the router can
never wedge a session.

Knobs (all env vars, so experiments need no code change):
  SKILLS_ROUTER_ACTOR   which actor's playbook to load (default: the installed
                        library's declared actor, else topogy)
  SKILLS_ROUTER_LIBRARY_DIR  developer override for the content library location
  SKILLS_ROUTER_MODE    enforce | shadow   (shadow never blocks; it only logs)
  SKILLS_ROUTER_DEBUG   1 to append every hook's raw stdin to debug.log
  SKILLS_ROUTER_STATE_DIR  override state/trace location (default:
                        $CLAUDE_CONFIG_DIR/skills-router, else ~/.claude/skills-router)
  TOPOGY_ROUTER_TELEMETRY        0 to never send decisions to ASI (telemetry.py has the rest)

Under `claude plugin eval`, which passes a plugin only EVAL_* variables,
EVAL_<knob> stands in for each knob above (see evals/README.md).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid

PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


def discover_library(installed_plugins_path=None):
    """Find an installed library plugin: one whose .claude-plugin/plugin.json
    carries an explicit `skillsRouterLibrary` marker, e.g.
        "skillsRouterLibrary": {"actor": "rewardz"}
    Returns (library_dir, actor) or (None, None). Fails open on any error."""
    # CLAUDE_CODE_PLUGIN_CACHE_DIR relocates the whole plugins root, install
    # records included; otherwise it sits under the config dir.
    plugins_dir = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or os.path.join(_claude_home(), "plugins")
    path = installed_plugins_path or os.path.join(plugins_dir, "installed_plugins.json")
    try:
        with open(path) as fh:
            plugins = (json.load(fh) or {}).get("plugins", {}) or {}
    except (OSError, ValueError):
        return None, None
    for entries in plugins.values():
        for entry in entries or []:
            root = (entry or {}).get("installPath")
            if not root:
                continue
            try:
                with open(os.path.join(root, ".claude-plugin", "plugin.json")) as fh:
                    marker = (json.load(fh) or {}).get("skillsRouterLibrary")
            except (OSError, ValueError):
                continue
            if marker:
                actor = marker.get("actor") if isinstance(marker, dict) else None
                return root, actor
    return None, None


def _setting(name, default=None):
    """An env knob, or under `claude plugin eval` its EVAL_ form, since an eval
    passes a plugin only EVAL_* variables."""
    return os.environ.get(name) or os.environ.get(f"EVAL_{name}") or default


def _claude_home():
    """Claude Code's config dir: CLAUDE_CONFIG_DIR when set, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _eval_git_dir():
    """The .git dir of the workspace an eval run works in, or None outside an eval.
    An eval run is marked by the EVAL_ form of the library knob alone."""
    if os.environ.get("SKILLS_ROUTER_LIBRARY_DIR") or not os.environ.get("EVAL_SKILLS_ROUTER_LIBRARY_DIR"):
        return None
    git_dir = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), ".git")
    return git_dir if os.path.isdir(git_dir) else None


def _workspace_copy(library, git_dir):
    """An eval run can't read outside its workspace, but inject-pointer tells the
    session to Read the library's skill files. Copy the library into the
    workspace's .git/ once, out of the way of searches and git status, and use the
    copy. Fails open to the original library."""
    copy = os.path.join(git_dir, "router-library")
    if not os.path.isdir(copy):
        tmp = f"{copy}.{os.getpid()}"
        try:
            shutil.copytree(library, tmp)
            os.rename(tmp, copy)  # atomic; a concurrent hook that got there first wins
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
    return copy if os.path.isdir(copy) else library


# The content library (moments + skill bodies) is separate from this engine so a
# customer can host their own. Resolution order: SKILLS_ROUTER_LIBRARY_DIR (dev
# override), an installed plugin carrying the `skillsRouterLibrary` marker, then
# the library bundled with this plugin.
_DISCOVERED_LIBRARY, _DISCOVERED_ACTOR = discover_library()
LIBRARY = (
    _setting("SKILLS_ROUTER_LIBRARY_DIR")
    or _DISCOVERED_LIBRARY
    or os.path.join(PLUGIN_ROOT, "library")
)
# Under an eval, the library and the state/trace live in the workspace, where the
# session can read the library and graders can read the trace.
_EVAL_GIT_DIR = _eval_git_dir()
if _EVAL_GIT_DIR:
    LIBRARY = _workspace_copy(LIBRARY, _EVAL_GIT_DIR)
STATE_DIR = _setting(
    "SKILLS_ROUTER_STATE_DIR",
    os.path.join(_EVAL_GIT_DIR, "router-state") if _EVAL_GIT_DIR
    else os.path.join(_claude_home(), "skills-router"),
)
TRACE_FILE = os.path.join(STATE_DIR, "trace.jsonl")
ACTOR = (
    _setting("SKILLS_ROUTER_ACTOR")
    or (_DISCOVERED_ACTOR if LIBRARY == _DISCOVERED_LIBRARY else None)
    or "topogy"
)
SHADOW = _setting("SKILLS_ROUTER_MODE", "enforce").lower() == "shadow"
# Evals must leave no trace at ASI, and a judge's inner model call is not a developer session:
# neither sends telemetry nor asks ASI whether the router is on.
TELEMETRY = not _EVAL_GIT_DIR and not os.environ.get("SKILLS_ROUTER_JUDGE")
# The MCP tool Claude calls to pull a moment's guidance itself when its trigger
# matches: the primary path, with the hooks as backstop. Claude Code namespaces
# plugin MCP tools as mcp__plugin_<plugin>_<server>__<tool>.
LOAD_TOOL_SHORT = "load_guidance"
LOAD_TOOL = f"mcp__plugin_skills-router_router__{LOAD_TOOL_SHORT}"
# Claude Code moves hook additionalContext over ~10,000 chars to a file and shows
# the model only a 2KB preview, so guidance injected on one tool call stays under this.
INJECT_BUDGET = int(_setting("SKILLS_ROUTER_INJECT_BUDGET", "9000"))

os.makedirs(STATE_DIR, exist_ok=True)

try:
    import yaml
except ImportError:
    # Fall back to the pure-Python PyYAML vendored beside the hooks, so the router
    # runs against a stock python3 with no `pip install` (see _vendor/README.md).
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor"))
    try:
        import yaml
    except ImportError:  # fail open: no YAML at all, router disabled for this call
        sys.stderr.write("skills-router: PyYAML unavailable; hooks disabled\n")
        sys.exit(0)


# --- stdin -----------------------------------------------------------------

def read_input():
    raw = sys.stdin.read()
    if os.environ.get("SKILLS_ROUTER_DEBUG") == "1":
        with open(os.path.join(STATE_DIR, "debug.log"), "a") as fh:
            fh.write(f"--- {time.strftime('%FT%T')} {os.path.basename(sys.argv[0])}\n{raw}\n")
    try:
        return normalize(json.loads(raw) if raw.strip() else {})
    except json.JSONDecodeError:
        return normalize({})


# --- harness normalization -------------------------------------------------
# Claude Code and Codex use near-identical hook contracts, but Claude sends
# snake_case input with tools Write/Edit/Bash, while Codex sends camelCase input
# with tools apply_patch/shell. We normalize both into one canonical view plus a
# logical `action` (write | shell | other) so moments can match either harness.

_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch", "apply-patch", "write_file"}


def detect_harness(raw):
    h = os.environ.get("SKILLS_ROUTER_HARNESS")
    if h:
        return h.lower()
    if any(k in raw for k in ("toolName", "hookEventName", "sessionId", "transcriptPath")):
        return "codex"
    return "claude"


def _tool_view(harness, tool_name, tool_input):
    """Return (action, command, file_path) for either harness's tool call."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    tn = (tool_name or "").lower()
    if tn in _WRITE_TOOLS or tn in ("edit", "multiedit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
        if not fp:  # Codex apply_patch carries the path inside the patch text
            patch = ti.get("input") or ti.get("patch") or ti.get("content") or ""
            m = re.search(r"\*\*\*\s+(?:Add|Update|Delete) File:\s+(.+)", str(patch))
            if m:
                fp = m.group(1).strip()
        return "write", "", fp
    if tn in ("bash", "shell", "exec", "local_shell"):
        cmd = ti.get("command") or ti.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return "shell", str(cmd), ""
    return "other", str(ti.get("command") or ""), str(ti.get("file_path") or ti.get("path") or "")


def normalize(raw):
    """Canonicalize a raw hook payload from either harness."""
    if not isinstance(raw, dict):
        raw = {}
    harness = detect_harness(raw)

    def g(*keys):
        for k in keys:
            if raw.get(k) is not None:
                return raw[k]
        return None

    tool_name = g("tool_name", "toolName") or ""
    tool_input = g("tool_input", "toolInput") or {}
    cwd = g("cwd") or ""
    action, command, file_path = _tool_view(harness, tool_name, tool_input)
    # Codex apply_patch paths are repo-relative; make them absolute so predicates
    # (e.g. the worktree check) resolve against the right repo.
    if file_path and not os.path.isabs(file_path) and cwd:
        file_path = os.path.join(cwd, file_path)
    return {
        "harness": harness,
        "session_id": g("session_id", "sessionId") or "unknown",
        "hook_event_name": g("hook_event_name", "hookEventName") or "",
        "source": g("source") or "",
        "cwd": g("cwd") or "",
        "transcript_path": g("transcript_path", "transcriptPath") or "",
        "prompt": g("prompt") or "",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "action": action,
        "command": command or "",
        "file_path": file_path or "",
        "raw": raw,
    }


# --- config ----------------------------------------------------------------

_YAML_MEMO = {}


def _load_yaml(name):
    """Parse a library file. A compiled guidance.yaml runs to hundreds of KB and
    the vendored pure-Python parser takes ~0.2s on it, on every hook call, so
    parses are cached as JSON keyed by path, mtime and size."""
    path = os.path.join(LIBRARY, name)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    if key in _YAML_MEMO:
        return _YAML_MEMO[key]
    cache = os.path.join(STATE_DIR, "cache", hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")
    try:
        with open(cache) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = f"{cache}.{os.getpid()}"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, cache)
        except (OSError, TypeError, ValueError):
            pass  # not JSON-safe or not writable: just don't cache
    _YAML_MEMO[key] = data
    return data


def load_playbook(actor=ACTOR):
    """Resolve one actor's playbook: the moments it takes, each with its skill
    objects and any inline-context text already attached."""
    moments = _load_yaml("moments.yaml").get("moments", {}) or {}
    skills = _load_yaml("skills.yaml").get("skills", {}) or {}
    contexts = _load_yaml("contexts.yaml").get("contexts", {}) or {}
    actor_cfg = _load_yaml(os.path.join("actors", f"{actor}.yaml")) or {}
    taken = actor_cfg.get("takes", []) or []

    resolved = []
    for mid in taken:
        m = moments.get(mid)
        if not m:
            continue
        m = dict(m)
        m["id"] = mid
        skill_objs = []
        for sid in (m.get("runs", []) or []):
            obj = {"id": sid, **(skills.get(sid, {"state": "unwritten"}))}
            body_file = obj.get("body")
            if body_file:
                obj["body_text"] = _read_library_file(body_file)
            skill_objs.append(obj)
        m["skill_objs"] = skill_objs
        if m.get("context"):
            m["context_text"] = contexts.get(m["context"], "")
        resolved.append(m)
    return resolved


LIBRARY_FORMAT = 1  # highest skillsRouterLibrary.format this engine reads
DEV_VERSION = "0.0.0-dev"  # plugin.json's version in the source repo; releases replace it


def _plugin_json(root):
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json")) as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _version(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def compat_warning():
    """Why this engine can't fully serve the active library, or None. A library
    plugin declares what it needs in its marker, e.g.
        "skillsRouterLibrary": {"actor": "acme", "format": 1, "engine": ">=0.5.1"}
    Without this check a too-old engine shows the triggers but delivers nothing."""
    marker = _plugin_json(LIBRARY).get("skillsRouterLibrary")
    if not isinstance(marker, dict):
        return None
    have = _plugin_json(PLUGIN_ROOT).get("version", "0")
    need = str(marker.get("engine") or "").strip()
    # A source checkout carries DEV_VERSION; release.sh writes the real version
    # into the built copy, so a dev engine has no version to compare.
    if need and have != DEV_VERSION and _version(have) < _version(need):
        return f"this skills pack needs skills-router {need}, installed {have}; update the router plugin"
    fmt = marker.get("format")
    if isinstance(fmt, int) and fmt > LIBRARY_FORMAT:
        return f"this skills pack uses library format {fmt}, router {have} reads up to {LIBRARY_FORMAT}; update the router plugin"
    return None


def trigger_for(moment):
    """The text that tells Claude when a moment applies. `description` is the
    legacy name for `trigger`, kept so older libraries still load. Libraries
    write {LOAD_GUIDANCE} where the tool name goes, so they never hard-code it."""
    text = moment.get("trigger") or moment.get("description") or moment["id"]
    return text.replace("{LOAD_GUIDANCE}", LOAD_TOOL)


def find_moment(moment_id, actor=ACTOR):
    return next((m for m in load_playbook(actor) if m["id"] == moment_id), None)


def actor_display_name(actor=ACTOR):
    """Human-readable name of the customer whose skills pack is loaded. Prefers the
    `name` field in actors/<actor>.yaml; falls back to the actor id, title-cased
    (kebab segments become words)."""
    cfg = _load_yaml(os.path.join("actors", f"{actor}.yaml")) or {}
    name = (cfg.get("name") or "").strip()
    if name:
        return name
    return (actor or "").replace("-", " ").title()


def _read_library_file(relpath):
    path = os.path.join(LIBRARY, relpath)
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def body_for(moment):
    """The full text a moment injects: each skill's body (falling back to its
    description), plus any inline context. Empty when there is nothing to inject."""
    parts = []
    for s in moment.get("skill_objs", []) or []:
        text = s.get("body_text") or s.get("description")
        if text:
            parts.append(f"### {s['id']}\n{text}")
    if moment.get("context_text"):
        parts.append(moment["context_text"])
    return "\n\n".join(parts)


def skill_body_path(skill):
    return os.path.join(LIBRARY, skill["body"])


def pointer_body_for(moment):
    """A compact stand-in for body_for when the full text won't fit this call's
    injection budget: the inline context stays, each skill body becomes a path
    to Read."""
    parts = []
    for s in moment.get("skill_objs", []) or []:
        if s.get("body"):
            parts.append(f"### {s['id']}\nFull guidance (Read it): {skill_body_path(s)}")
        elif s.get("description"):
            parts.append(f"### {s['id']}\n{s['description']}")
    if moment.get("context_text"):
        parts.append(moment["context_text"])
    return "\n\n".join(parts)


def summary_for(moment):
    """A concise directive for a blocked tool call — prefers each skill's short
    `summary`, falling back to its body then description. Kept short so a gate reads
    as a clear instruction to act on, not a wall of text shown as an error."""
    skills = moment.get("skill_objs", []) or []
    multi = len(skills) > 1
    parts = []
    for s in skills:
        text = s.get("summary") or s.get("body_text") or s.get("description")
        if text:
            parts.append(f"### {s['id']}\n{text}" if multi else text)
    if moment.get("context_text"):
        parts.append(moment["context_text"])
    return "\n\n".join(parts)


# --- per-session state -----------------------------------------------------

def _state_path(inp):
    sid = inp.get("session_id") or "unknown"
    return os.path.join(STATE_DIR, f"{sid}.json")


def load_state(inp):
    # injected: moments whose guidance reached Claude (pushed by a hook or pulled
    # via load_guidance). delivered: the same, per guidance.yaml item id.
    # pull_denied: pull moments already stopped once.
    state = {"invoked": [], "bypass": False, "skipped": [], "injected": [],
             "delivered": [], "pull_denied": []}
    path = _state_path(inp)
    if os.path.exists(path):
        try:
            with open(path) as fh:
                state.update(json.load(fh))
        except (json.JSONDecodeError, OSError):
            pass
    return state


def save_state(inp, state):
    with open(_state_path(inp), "w") as fh:
        json.dump(state, fh)


def mark_invoked(inp, skill_id):
    st = load_state(inp)
    if skill_id not in st["invoked"]:
        st["invoked"].append(skill_id)
    save_state(inp, st)


# --- matching --------------------------------------------------------------

def tool_call_matches(match, inp):
    """True when a deterministic/hybrid moment's matchers all hold for this tool
    call. `inp` is the normalized payload. Prefer `action` (logical, cross-harness:
    write | shell); `tool` (regex on the raw tool name) is kept for back-compat.
    `command`/`file_path` match the normalized extracted values. Empty = don't care."""
    match = match or {}
    act = match.get("action")
    if act and (inp.get("action") not in [a.strip() for a in act.split("|")]):
        return False
    tool_re = match.get("tool")
    if tool_re and not re.fullmatch(tool_re, inp.get("tool_name", "") or "", re.I):
        return False
    for field in ("command", "file_path"):
        pat = match.get(field)
        if pat and not re.search(pat, inp.get(field, "") or "", re.I):
            return False
    return True


# --- trace + emit ----------------------------------------------------------

def trace(inp, event, decision, moment=None, **detail):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "id": str(uuid.uuid4()),
        "session": inp.get("session_id", "unknown"),
        "actor": ACTOR,
        "harness": inp.get("harness"),
        "event": event,
        "moment": moment,
        "decision": decision,
        "mode": "shadow" if SHADOW else "enforce",
    }
    rec.update(detail)
    try:
        with open(TRACE_FILE, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        return
    start_sender()


def start_sender(force=False):
    """Start the background telemetry sender, unless telemetry is off for this run."""
    if not TELEMETRY:
        return
    try:
        # Imported here, not at the top: a bug in the sender must never stop a hook loading.
        import telemetry
    except Exception as exc:
        _debug_note(f"telemetry unavailable: {exc!r}")
        return
    telemetry.maybe_spawn(STATE_DIR, PLUGIN_ROOT, LIBRARY, force=force)


def _access():
    try:
        # Imported here for the same reason as the sender: a bug in it must never stop a hook.
        import access
    except Exception as exc:
        _debug_note(f"access check unavailable: {exc!r}")
        return None
    return access


# What load_guidance answers instead of guidance while the router is off.
OFF_REPLY = "The Topogy Router is turned off for this developer; carry on without it."


def router_disabled():
    """True unless ASI's cached answer has the router on for this developer. Evals and a
    judge's inner model call never ask ASI, and keep the router on."""
    if not TELEMETRY:
        return False
    access = _access()
    try:
        return access is None or not access.enabled(STATE_DIR)
    except Exception:
        return True


def check_access():
    """router_disabled() for SessionStart and the MCP server's tool list, which also ask
    ASI: now when this machine has no answer yet, in the background once the cached one
    has expired."""
    if not TELEMETRY:
        return False
    access = _access()
    try:
        return access is None or not access.check(STATE_DIR, LIBRARY)
    except Exception:
        return True


def _debug_note(text):
    try:
        with open(os.path.join(STATE_DIR, "debug.log"), "a") as fh:
            fh.write(f"--- {time.strftime('%FT%T')} {os.path.basename(sys.argv[0])}\n{text}\n")
    except OSError:
        pass


def emit_context(event, text, system_message=None):
    """Emit a single hook JSON object: `additionalContext` reaches the model, and
    the optional top-level `system_message` is the one line the user sees in the
    terminal. Stdout must stay this one object, so the banner rides here, not a
    separate print."""
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if system_message:
        out["systemMessage"] = system_message
    print(json.dumps(out))


def emit_notice(text):
    """A user-visible line only; nothing reaches the model."""
    print(json.dumps({"systemMessage": text}))


def emit_deny(reason, system_message=None):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    if system_message:
        out["systemMessage"] = system_message
    print(json.dumps(out))


def emit_ask(reason):
    # A softer stop: renders as a permission prompt (not a red "hook error"), and
    # the reason text is shown verbatim with no "hook error" prefix. The user
    # approves or declines; declining lets the agent do the workflow instead.
    # An ask's reason is shown to the person, not to Claude, so when they approve,
    # the guidance would never reach Claude. The same text goes in additionalContext.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
            "additionalContext": reason,
        }
    }))
