"""Fetch a job description as plain text, given any posting URL.

Known ATSs are read through their JSON APIs (reliable, no JS rendering needed).
Anything else falls back to fetching the page HTML, which works for server-rendered
careers sites and returns little for JS-only ones; the caller checks the length.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .http import get_json, get_text, html_to_text
from .models import Job


def fetch_description(job_or_url: Job | str) -> str:
    job = job_or_url if isinstance(job_or_url, Job) else None
    url = job.url if job else job_or_url
    if job and job.description and len(job.description) > 300:
        return job.description
    try:
        text = _by_url(url, job)
    except Exception as e:  # noqa: BLE001 - a JD failure must never kill the run
        text = ""
        print(f"  ! JD fetch failed for {url}: {e}")
    return text


def _by_url(url: str, job: Job | None) -> str:
    p = urlparse(url)
    host, parts = p.netloc.lower(), [x for x in p.path.split("/") if x]
    q = parse_qs(p.query)

    # Greenhouse: job-boards.greenhouse.io/<token>/jobs/<id>, or ?gh_jid= on a company site
    if job and "gh_token" in job.extra:
        return _greenhouse(job.extra["gh_token"], job.extra["gh_id"])
    if "greenhouse.io" in host and "jobs" in parts:
        i = parts.index("jobs")
        return _greenhouse(parts[i - 1], parts[i + 1])
    # Lever: jobs.lever.co/<company>/<uuid>
    if host == "jobs.lever.co" and len(parts) >= 2:
        d = get_json(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
        lists = "\n\n".join(f"{l.get('text')}\n{html_to_text(l.get('content'))}" for l in d.get("lists", []))
        return "\n\n".join(filter(None, [d.get("descriptionPlain"), lists, d.get("additionalPlain")]))
    # Ashby: jobs.ashbyhq.com/<board>/<uuid>
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}")
        for j in d.get("jobs", []):
            if j["id"] == parts[1]:
                return j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
    # Workday: <tenant>.wdN.myworkdayjobs.com/[<lang>/]<site>/job/<...>
    if "myworkdayjobs.com" in host and "job" in parts:
        api = job.extra.get("wd_api") if job else None
        if not api:
            i = parts.index("job")
            site = parts[i - 1]
            api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/" + "/".join(parts[i:])
        d = get_json(api)
        return html_to_text(d.get("jobPostingInfo", {}).get("jobDescription"))
    # Company site embedding Greenhouse (?gh_jid=123): guess the board token from the
    # domain / company name before falling back to scraping the (often JS-only) page.
    if "gh_jid" in q:
        guesses = [host.removeprefix("www.").removeprefix("careers.").split(".")[0]]
        if job:
            guesses += [re.sub(r"[^a-z0-9]", "", job.company.lower())]
        for token in dict.fromkeys(guesses):
            try:
                text = _greenhouse(token, q["gh_jid"][0])
                if len(text) > 300:
                    return text
            except Exception:  # noqa: BLE001
                pass
    html = get_text(url)
    text = html_to_text(re.sub(r"(?is)<head.*?</head>", "", html))
    if "gh_jid" in q and len(text) < 500:
        m = re.search(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board/js\?for=|v1/boards/)?([a-z0-9_-]+)", html)
        if m:
            return _greenhouse(m.group(1), q["gh_jid"][0])
    return text


def _greenhouse(token: str, job_id) -> str:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
    return html_to_text(d.get("content"))
