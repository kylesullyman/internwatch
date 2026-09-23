from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Query params that identify a posting; everything else (utm_*, ref, source...) is noise.
_KEEP_PARAMS = {"gh_jid", "jobid", "job_id", "id", "jk", "lever-source"}


def canonical_url(url: str) -> str:
    """Normalize a posting URL so the same job from two sources dedupes."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    q = {k: v for k, v in parse_qs(p.query).items() if k.lower() in _KEEP_PARAMS}
    path = re.sub(r"/(apply|application)/?$", "", p.path.rstrip("/"))
    host = p.netloc.lower().removeprefix("www.")
    # Greenhouse has two hosts for the same board.
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    return urlunparse(("https", host, path.lower(), "", urlencode(q, doseq=True), ""))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


@dataclass
class Job:
    source: str                 # e.g. "simplify", "greenhouse:figma"
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    posted_at: float | None = None      # epoch seconds, if the source knows it
    terms: list[str] = field(default_factory=list)   # "Summer 2027", ...
    category: str | None = None
    description: str | None = None      # plain text, if already fetched
    extra: dict = field(default_factory=dict)
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        """Dedupe keys. A job counts as seen if ANY key was seen before.

        The URL key catches the same posting across sources; the company+title+location
        key catches reposts under a new URL and list entries that point at a careers
        page instead of the ATS.
        """
        loc = _norm(self.locations[0]) if self.locations else ""
        return [
            "u:" + canonical_url(self.url),
            f"t:{_norm(self.company)}|{_norm(self.title)}|{loc}",
        ]

    def short(self) -> str:
        loc = ", ".join(self.locations[:2]) + (f" +{len(self.locations) - 2}" if len(self.locations) > 2 else "")
        return f"{self.company} — {self.title} ({loc or 'location n/a'})"
