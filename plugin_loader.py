"""Auto-discovery for new tools.

Drop a new ``.py`` file into ``plugins/`` (lead server) or ``browser_plugins/``
(browser agent) and push to GitHub. On the next start every file is imported
automatically, nothing else needs editing. A plugin file can provide:

* ``register(mcp)``: add tools / prompts with ``@mcp.tool()`` inside it.
* ``LEAD_SOURCES = {"name": async_fn}``: extra lead sources for
  ``find_freelance_leads`` (signature: ``async def fn(client, keywords) -> list[dict]``).

Files starting with ``_`` are skipped (use ``_template.py`` as a starting point).
A plugin that crashes on import is skipped and reported, it never takes the
whole server down.
"""

from __future__ import annotations

import importlib
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_plugins(mcp, package: str, lead_sources: dict | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {}
    folder = ROOT / package
    if not folder.is_dir():
        return status
    for f in sorted(folder.glob("*.py")):
        if f.name.startswith("_"):
            continue
        t0 = time.perf_counter()
        try:
            mod = importlib.import_module(f"{package}.{f.stem}")
            if hasattr(mod, "register"):
                mod.register(mcp)
            added = []
            if lead_sources is not None:
                for name, fn in (getattr(mod, "LEAD_SOURCES", None) or {}).items():
                    lead_sources[name] = fn
                    added.append(name)
            status[f.stem] = {"ok": True, "load_ms": round((time.perf_counter() - t0) * 1000, 1),
                              **({"lead_sources": added} if added else {})}
        except Exception as e:  # never let one plugin break the server
            status[f.stem] = {"ok": False, "error": f"{type(e).__name__}: {e}",
                              "trace": traceback.format_exc(limit=3)[-600:]}
    return status
