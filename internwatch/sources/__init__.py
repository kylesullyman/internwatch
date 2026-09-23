from __future__ import annotations

from functools import partial
from typing import Callable

from ..models import Job
from . import ats, github_lists

SourceFn = Callable[[], list[Job]]


def build_sources(cfg: dict) -> list[tuple[str, SourceFn]]:
    """Turn the `sources:` block of config.yaml into (source_id, fetch_fn) pairs."""
    out: list[tuple[str, SourceFn]] = []
    src = cfg.get("sources", {})
    for lst in src.get("github_lists", []):
        if not lst.get("enabled", True):
            continue
        fn = github_lists.simplify_json if lst["format"] == "simplify_json" else github_lists.markdown_table
        out.append((f"list:{lst['name']}", partial(fn, lst["name"], lst["url"])))
    for token in src.get("greenhouse", []):
        out.append((f"greenhouse:{token}", partial(ats.greenhouse, token)))
    for co in src.get("lever", []):
        out.append((f"lever:{co}", partial(ats.lever, co)))
    for b in src.get("ashby", []):
        board, name = (b, None) if isinstance(b, str) else (b["board"], b.get("name"))
        out.append((f"ashby:{board}", partial(ats.ashby, board, name)))
    for w in src.get("workday", []):
        w = dict(w)
        name = w.pop("name")
        out.append((f"workday:{name.lower()}", partial(ats.workday, name, **w)))
    return out
