"""Community-maintained GitHub internship lists.

Two formats are supported:
  * `simplify_json` - the listings.json that SimplifyJobs-style repos generate their README from
    (SimplifyJobs/Summer2027-Internships, vanshb03/Summer2027-Internships, New-Grad-Positions...).
    Reading the JSON is faster and more reliable than scraping the README.
  * `markdown_table` - repos that only publish README tables (speedyapply/2027-SWE-College-Jobs).
"""
from __future__ import annotations

import re
import time

from ..http import get_json, get_text, html_to_text
from ..models import Job


def simplify_json(name: str, url: str) -> list[Job]:
    jobs = []
    for x in get_json(url, timeout=60):
        if not x.get("active", True) or not x.get("is_visible", True):
            continue
        terms = list(x.get("terms") or [])
        if not terms and x.get("season"):
            terms = [x["season"]]
        jobs.append(Job(
            source=name,
            company=x.get("company_name", "").strip(),
            title=x.get("title", "").strip(),
            url=x.get("url", ""),
            locations=list(x.get("locations") or []),
            posted_at=x.get("date_posted"),
            terms=terms,
            category=x.get("category"),
            extra={"sponsorship": x.get("sponsorship"), "degrees": x.get("degrees")},
        ))
    return jobs


_HREF = re.compile(r'href="([^"]+)"')
_AGE = re.compile(r"(\d+)\s*([hdwmo]+)")
_AGE_SECS = {"h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "m": 30 * 86400}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_table(name: str, url: str) -> list[Job]:
    """Parse every markdown table whose header has Company/Position/Posting columns."""
    text = get_text(url, timeout=60)
    jobs, header, section, last_company = [], None, "", ""
    now = time.time()
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            header = None
            continue
        if not line.startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if "Company" in cells and ("Posting" in cells or "Application" in cells or "Link" in cells):
            header = [c.lower() for c in cells]
            continue
        if header is None or set(line) <= set("|-: "):
            continue
        row = dict(zip(header, cells))
        link_cell = row.get("posting") or row.get("application") or row.get("link") or ""
        hrefs = _HREF.findall(link_cell)
        if not hrefs or "🔒" in link_cell:      # closed postings are marked with a lock
            continue
        company = html_to_text(row.get("company", "")).strip("* ")
        if company in ("↳", ""):
            company = last_company
        last_company = company
        age = _AGE.search(row.get("age", "") or row.get("date posted", ""))
        posted = now - int(age.group(1)) * _AGE_SECS.get(age.group(2), 86400) if age else None
        jobs.append(Job(
            source=name,
            company=company,
            title=html_to_text(row.get("position") or row.get("role") or "").strip(),
            url=hrefs[0],
            locations=[html_to_text(row.get("location", ""))],
            posted_at=posted,
            extra={"section": section},
        ))
    return jobs
