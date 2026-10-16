#!/usr/bin/env python3
"""The org's Topogy Router token, which the engine authenticates to ASI with.

It ships with the customer's library plugin, in its plugin.json marker
(skillsRouterLibrary.routerToken), so the engine reaches ASI with no setup. A
library without one (the bundled library/, a development checkout) has none.

Knobs:
  TOPOGY_ROUTER_TOKEN  override the shipped token, for testing against a local or staging ASI
"""
import json
import os


def read(library):
    override = (os.environ.get("TOPOGY_ROUTER_TOKEN") or "").strip()
    if override:
        return override
    try:
        with open(os.path.join(library, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
    except (OSError, ValueError):
        return None
    marker = plugin.get("skillsRouterLibrary") if isinstance(plugin, dict) else None
    shipped = marker.get("routerToken") if isinstance(marker, dict) else None
    if not isinstance(shipped, str):
        return None
    return shipped.strip() or None
