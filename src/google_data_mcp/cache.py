"""On-disk result cache.

This matters more here than in a hosted scraper, and for a specific reason: an MCP server runs on
the user's own machine, so every request leaves from **one** IP. Google's per-IP budget accumulates
over hours, so an agent that asks the same question twice in a session must not spend that budget
twice. A cache hit is budget never spent.

Failures are silent by design: a corrupt or unreadable cache entry must degrade into a live fetch,
never into an error the caller sees.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

DEFAULT_TTL = 6 * 3600.0


def _root() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "google-data-mcp")


def key(kind: str, **parts) -> str:
    canon = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "%s-%s" % (kind, hashlib.sha256(canon.encode()).hexdigest()[:24])


def get(k: str, ttl: float = DEFAULT_TTL):
    path = os.path.join(_root(), k + ".json")
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def put(k: str, value) -> None:
    try:
        os.makedirs(_root(), exist_ok=True)
        path = os.path.join(_root(), k + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
