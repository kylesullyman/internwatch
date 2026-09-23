from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import typst

TEMPLATE = Path(__file__).parent / "templates" / "resume.typ"

# Progressively tighter layouts, tried in order until the resume fits.
STYLES = [
    dict(font_size=10.5, margin_x=0.6, margin_y=0.5, leading=0.55, section_gap=0.55, entry_gap=0.25, bullet_gap=0.4),
    dict(font_size=10.0, margin_x=0.55, margin_y=0.45, leading=0.5, section_gap=0.45, entry_gap=0.2, bullet_gap=0.35),
    dict(font_size=9.5, margin_x=0.5, margin_y=0.4, leading=0.45, section_gap=0.35, entry_gap=0.15, bullet_gap=0.3),
]


def _page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf))


def render_pdf(doc: dict, style: dict) -> bytes:
    data = dict(doc, style=style)
    return typst.compile(str(TEMPLATE), root=str(TEMPLATE.parent), sys_inputs={"data": json.dumps(data)})


def fit_and_render(doc: dict, max_pages: int = 1, pinned: set[str] = frozenset()) -> tuple[bytes, dict, list[str]]:
    """Render, tightening the layout and then trimming bullets until it fits.

    Returns (pdf, final_doc, notes) where notes describe anything cut for length.
    """
    doc = copy.deepcopy(doc)
    notes: list[str] = []
    for style in STYLES:
        pdf = render_pdf(doc, style)
        if _page_count(pdf) <= max_pages:
            return pdf, doc, notes
    # Still too long at the tightest layout: drop the last bullet of the longest
    # non-pinned entry, one at a time. Never drop an entry's only bullet.
    style = STYLES[-1]
    for _ in range(40):
        cands = [e for s in doc["sections"] for e in s["entries"]
                 if e.get("id") not in pinned and len(e["bullets"]) > 1]
        if not cands:
            break
        e = max(cands, key=lambda e: len(e["bullets"]))
        cut = e["bullets"].pop()
        notes.append(f"Cut for length from {e['heading']}: “{cut}”")
        pdf = render_pdf(doc, style)
        if _page_count(pdf) <= max_pages:
            return pdf, doc, notes
    notes.append(f"Could not fit in {max_pages} page(s) without removing whole entries — review manually.")
    return pdf, doc, notes
