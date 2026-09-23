"""Company applicant-tracking systems, polled directly.

This is what makes "within the hour" real: the GitHub lists depend on someone (or
Simplify's crawler) noticing a posting, while these endpoints show it the moment the
company publishes. All four are the public, unauthenticated APIs the companies' own
careers pages use.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..http import get_json, html_to_text, post_json
from ..models import Job


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def greenhouse(token: str) -> list[Job]:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return [Job(
        source=f"greenhouse:{token}",
        company=j.get("company_name") or token,
        title=j["title"],
        url=j["absolute_url"],
        locations=[(j.get("location") or {}).get("name", "")],
        posted_at=_iso(j.get("first_published")) or _iso(j.get("updated_at")),
        extra={"gh_token": token, "gh_id": j["id"]},
    ) for j in d.get("jobs", [])]


def lever(company: str) -> list[Job]:
    d = get_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
    jobs = []
    for j in d:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        desc = "\n\n".join(filter(None, [j.get("openingPlain"), j.get("descriptionBodyPlain") or j.get("descriptionPlain")]
                                  + [f"{l.get('text')}\n{html_to_text(l.get('content'))}" for l in j.get("lists", [])]
                                  + [j.get("additionalPlain")]))
        jobs.append(Job(
            source=f"lever:{company}", company=company.title(), title=j["text"], url=j["hostedUrl"],
            locations=[l for l in locs if l], posted_at=(j.get("createdAt") or 0) / 1000 or None,
            description=desc, extra={"commitment": cat.get("commitment")},
        ))
    return jobs


def ashby(board: str, company: str | None = None) -> list[Job]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    jobs = []
    for j in d.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        jobs.append(Job(
            source=f"ashby:{board}", company=company or board.split(".")[0].title(), title=j["title"],
            url=j["jobUrl"], locations=[l for l in locs if l], posted_at=_iso(j.get("publishedAt")),
            description=j.get("descriptionPlain"), extra={"employmentType": j.get("employmentType")},
        ))
    return jobs


def _workday_posted(text: str | None) -> float | None:
    """Workday only gives 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    t = text.lower()
    now = time.time()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - 86400
    digits = "".join(c for c in t if c.isdigit())
    return now - int(digits) * 86400 if digits else None


def workday(company: str, host: str, site: str, search: str = "intern", max_results: int = 1000,
            tenant: str | None = None) -> list[Job]:
    """Workday's search API is relevance-sorted, not date-sorted, so we have to page
    through every result for the query; `max_results` caps the cost."""
    tenant = tenant or host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    def page(offset: int) -> dict:
        return post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search})

    first = page(0)
    total = min(first.get("total", 0), max_results)
    pages = [first]
    with ThreadPoolExecutor(6) as ex:
        pages += list(ex.map(page, range(20, total, 20)))
    jobs, seen = [], set()
    for p in pages:
        for j in p.get("jobPostings", []):
            path = j.get("externalPath")
            if not path or path in seen:
                continue
            seen.add(path)
            jobs.append(Job(
                source=f"workday:{tenant}", company=company, title=j.get("title", ""),
                url=f"https://{host}/en-US/{site}{path}", locations=[j.get("locationsText", "")],
                posted_at=_workday_posted(j.get("postedOn")),
                extra={"wd_api": f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                       "posted_label": (j.get("postedOn") or "").replace("Posted ", "posted ").lower()},
            ))
    return jobs
