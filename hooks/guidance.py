"""Scoped guidance: pick a moment's items from guidance.yaml for the work at hand.

A compiled library splits *when* from *what*. moments.yaml holds ~10 standard
moments, each with a trigger. guidance.yaml holds one item per intent and per
skill, each listing the moments it belongs to and a `scope`. Delivery picks the
items whose scope fits the context, and both delivery paths use this module:
load_guidance (pull) and the gate (push).

Context is a list of units. One unit is the session: its repo plus any declared
areas/platforms. Each path about to be touched adds a unit: the repo of the
path's own checkout, the platform from its extension, the areas whose
scopes.yaml location (for that same repo) has a matching glob, and its
repo-relative path for `module`.

Scope rule: an item matches when one unit answers every key its scope sets.
`general: true` always matches. A key no unit can answer means not matched; such
items in this repo are listed as loadable rather than delivered.
"""
import collections
import os
import re
import subprocess

import lib

PULL_BUDGET = int(lib._setting("SKILLS_ROUTER_PULL_BUDGET", "60000"))
SCOPE_KEYS = ("repo", "platform", "area", "module")


# --- library ---------------------------------------------------------------

def library():
    """Every guidance.yaml item, intents then skills, in file order. The order
    is the index space for the delivery marker, so keep it stable."""
    g = lib._load_yaml("guidance.yaml")
    items = [dict(it, _skill=False) for it in g.get("intents") or []]
    items += [dict(it, _skill=True) for it in g.get("skills") or []]
    return items


def scopes():
    s = lib._load_yaml("scopes.yaml")
    return {"areas": s.get("areas") or {}, "platforms": s.get("platforms") or {}}


def items_for(moment):
    """The items a moment delivers, or [] when the library has none for it (the
    moment then runs on its own runs/context, as before guidance.yaml existed).
    A moment's own runs/context ride along as one general item."""
    items = [it for it in library() if moment["id"] in (it.get("moments") or [])]
    if items:
        legacy = lib.body_for(moment)
        if legacy:
            items.insert(0, {"id": f"moment:{moment['id']}", "scope": {"general": True},
                             "category": "", "text": legacy, "_skill": False})
    return items


# --- context ---------------------------------------------------------------

_CHECKOUTS = {}


def normalize_remote(url):
    """git remote URL -> lowercase owner/name, the form scope.repo uses."""
    url = re.sub(r"\.git/?$", "", (url or "").strip())
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    m = re.match(r"^[^/:]+[:/](?:\d+/)?(.+)$", url)
    parts = [p for p in (m.group(1) if m else url).split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else None


def _git(cwd, *args):
    res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    return res.stdout.strip() if res.returncode == 0 else ""


def checkout(path):
    """(toplevel, repo) of the git checkout holding path, else (None, None).
    The path may not exist yet (a file about to be created)."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    if d not in _CHECKOUTS:
        top, repo = None, None
        try:
            top = _git(d, "rev-parse", "--show-toplevel") or None
            if top:
                url = _git(top, "config", "--get", "remote.origin.url")
                if not url:
                    first = (_git(top, "remote").splitlines() or [""])[0]
                    url = _git(top, "config", "--get", f"remote.{first}.url") if first else ""
                repo = normalize_remote(url)
        except (OSError, subprocess.SubprocessError):
            pass
        _CHECKOUTS[d] = (top, repo)
    return _CHECKOUTS[d]


def _pattern(glob):
    """scopes/module glob -> regex. ** spans directories, * stays in one, and a
    <name> or {name} placeholder is exactly one path segment."""
    out = []
    for tok in re.split(r"(\*\*|\*|<[^>/]+>|\{[^}/]+\})", glob):
        if tok == "**":
            out.append(".*")
        elif tok == "*":
            out.append("[^/]*")
        elif re.fullmatch(r"<[^>/]+>|\{[^}/]+\}", tok):
            out.append("[^/]+")
        else:
            out.append(re.escape(tok))
    return "".join(out)


def glob_match(glob, rel):
    return bool(re.fullmatch(_pattern(glob), rel)) or (glob.endswith("/**") and rel == glob[:-3])


def module_match(module, rel):
    return bool(re.match(_pattern(module.strip("/")) + r"(/|$)", rel))


def platforms_for(path, sc):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return {k for k, p in sc["platforms"].items() if ext and ext in (p.get("extensions") or [])}


def areas_for(repo, rel, sc):
    if not (repo and rel):
        return set()
    found = set()
    for key, area in sc["areas"].items():
        for loc in area.get("locations") or []:
            if (loc.get("repo") or "").lower() == repo and any(glob_match(g, rel) for g in loc.get("paths") or []):
                found.add(key)
    return found


def context(cwd, paths=(), areas=(), platforms=()):
    sc = scopes()
    areas, platforms = set(areas or ()), set(platforms or ())
    cwd = cwd or os.getcwd()
    units = [{"repo": checkout(cwd)[1], "areas": set(areas), "platforms": set(platforms), "rel": None}]
    for p in paths or ():
        ap = os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))
        top, repo = checkout(ap)
        rel = os.path.relpath(ap, top) if top else None
        units.append({
            "repo": repo,
            "areas": areas_for(repo, rel, sc) | areas,
            "platforms": platforms_for(ap, sc) | platforms,
            "rel": rel,
        })
    return units


# --- matching --------------------------------------------------------------

def _values(v):
    return [str(x) for x in ([v] if isinstance(v, str) else (v or []))]


def _repo_ok(scope, unit):
    return "repo" not in scope or (unit["repo"] or "") in {r.lower() for r in _values(scope["repo"])}


def unit_matches(scope, unit):
    if not _repo_ok(scope, unit):
        return False
    if "platform" in scope and not set(_values(scope["platform"])) & unit["platforms"]:
        return False
    if "area" in scope and not set(_values(scope["area"])) & unit["areas"]:
        return False
    if "module" in scope and not (unit["rel"] and any(module_match(m, unit["rel"]) for m in _values(scope["module"]))):
        return False
    return True


def specificity(item):
    scope = item.get("scope") or {}
    return 0 if scope.get("general") else sum(k in scope for k in SCOPE_KEYS)


def select(items, units, exclude=()):
    """(matched items, most specific first; hints) where hints counts the
    unmatched items in this context's repos by the key that would unlock them."""
    exclude = set(exclude)
    matched, hints = [], collections.Counter()
    for it in items:
        if it["id"] in exclude:
            continue
        scope = it.get("scope") or {}
        if scope.get("general") or any(unit_matches(scope, u) for u in units):
            matched.append(it)
        else:
            in_repo = [u for u in units if _repo_ok(scope, u)]
            for key in ("area", "platform", "module"):  # name the key that failed
                if key in scope and in_repo and not any(unit_matches({key: scope[key]}, u) for u in in_repo):
                    hints[(key, ", ".join(_values(scope[key])))] += 1
                    break
    matched.sort(key=lambda it: -specificity(it))  # stable: file order within a tier
    return matched, hints


# --- rendering -------------------------------------------------------------

def describe(units):
    repos = sorted({u["repo"] for u in units if u["repo"]})
    areas = sorted(set().union(*(u["areas"] for u in units)))
    plats = sorted(set().union(*(u["platforms"] for u in units)))
    parts = [f"repo {', '.join(repos) or '(none)'}"]
    if areas:
        parts.append(f"areas {', '.join(areas)}")
    if plats:
        parts.append(f"platforms {', '.join(plats)}")
    return "; ".join(parts)


def _chunk(it, inline_body):
    if not it["_skill"]:
        practice = it.get("practice")
        return f"- {f'({practice}) ' if practice else ''}{(it.get('text') or '').strip()}"
    trigger = (it.get("trigger") or it["id"]).strip()
    path = os.path.join(lib.LIBRARY, it["body"]) if it.get("body") else None
    if inline_body and path:
        body = lib._read_library_file(it["body"])
        if body:
            return f"#### {it['id']}\n{trigger}\n\n{body}"
    return f"- {it['id']}: {trigger}" + (f" Full guidance (Read it): {path}" if path else "")


def render(moment_id, matched, hints, units, budget, inline_bodies):
    """Render matched items into at most `budget` chars, specific tier first,
    grouped by category. Returns (text, delivered items, overflow items)."""
    lines = [f"SKILLS_ROUTER — guidance for '{moment_id}' ({describe(units)}). Follow it for this work.", ""]
    used = sum(len(x) + 1 for x in lines)
    delivered, overflow = [], []
    def grouped(tier):  # categories in order of first (most specific) appearance
        order = {}
        for it in tier:
            order.setdefault(it.get("category", ""), len(order))
        return sorted(tier, key=lambda it: order[it.get("category", "")])

    tiers = [("Specific to this context", grouped([i for i in matched if specificity(i)])),
             ("General", grouped([i for i in matched if not specificity(i)]))]
    for title, tier in tiers:
        heading_done, category = False, None
        for it in tier:
            heads = [] if heading_done else [f"## {title}"]
            if it.get("category", "") != category and it.get("category"):
                heads.append(f"### {it['category']}")
            text = None
            for inline in ([True, False] if (inline_bodies and it["_skill"]) else [False]):
                cand = "\n".join(heads + [_chunk(it, inline)])
                if used + len(cand) + 1 <= budget:
                    text = cand
                    break
            if text is None:
                overflow.append(it)
                continue
            lines.append(text)
            used += len(text) + 1
            heading_done, category = True, it.get("category", category)
            delivered.append(it)
    if not matched:
        lines.append(f"No guidance for '{moment_id}' applies in this context; carry on.")
    if overflow:
        lines += ["", f"{len(overflow)} more matching items didn't fit: "
                  + ", ".join(i["id"] for i in overflow[:30]) + (" …" if len(overflow) > 30 else "")]
    if hints:
        top = ", ".join(f"{n} for {k} {v}" for (k, v), n in hints.most_common(8))
        lines += ["", f"Not loaded (this context doesn't reach their scope): {top}. To load them, "
                  f"call {lib.LOAD_TOOL} again with the paths you'll touch there, or with areas/platforms."]
    return "\n".join(lines), delivered, overflow


# --- delivery marker -------------------------------------------------------
# The MCP server can't see the session, so it appends the indices of the items
# it delivered and the PostToolUse hook records them. Indices keep it short; the
# item count guards against the library changing in between.

def marker(delivered):
    index = {it["id"]: n for n, it in enumerate(library())}
    nums = sorted(index[i["id"]] for i in delivered if i["id"] in index)
    return f"[router-delivered n={len(index)}: {','.join(map(str, nums))}]"


def parse_marker(text):
    m = re.search(r"\[router-delivered n=(\d+): ([\d,]*)\]", text or "")
    items = library()
    if not m or int(m.group(1)) != len(items):
        return []
    return [items[int(n)]["id"] for n in m.group(2).split(",") if n and int(n) < len(items)]
