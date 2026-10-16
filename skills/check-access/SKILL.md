---
name: check-access
description: Ask Topogy now whether the Topogy Router is on for this developer, and refresh the cached answer (access.json).
disable-model-invocation: true
allowed-tools: Bash(python3 *)
---

Run this and show the user its output verbatim, with nothing added:

```bash
python3 "${CLAUDE_SKILL_DIR}/../../hooks/access.py" now
```
