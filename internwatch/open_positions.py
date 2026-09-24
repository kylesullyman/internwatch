"""Refresh a snapshot of open, school-year-friendly jobs from live ATS boards.

Unlike the new-posting alert path, this reconsiders every currently listed job on
*every* poll: a first run, an already-seen posting, and a closed posting all need to
produce the right snapshot. Community summer/new-grad lists aren't used here.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .filters import Filter, is_us
from .models import Job, canonical_url

_ATS = ("greenhouse:", "lever:", "ashby:", "workday:")
_PART_TIME = re.compile(r"\bpart[ -]?time\b|\b(?:[1-2]\d)\s*(?:hours?|hrs?)\s*(?:/|per\s*)\s*(?:week|wk)\b", re.I)
_PART_TIME_DESCRIPTION = re.compile(
    r"\bpart[ -]?time\s+(?:student|worker|role|position|contract|intern|job|opportunity|schedule|program)\b|"
    r"\b(?:this|the)\s+(?:role|position|internship|program).{0,80}?\bpart[ -]?time\b|"
    r"\b(?:work|commit|available for)\s+(?:a\s+)?(?:[1-2]\d)\s*(?:hours?|hrs?)\s*(?:/|per\s*)\s*(?:week|wk)\b",
    re.I,
)
_SEASON = re.compile(r"\b(fall|autumn|winter)\s*[-/]?\s*'?(20\d\d|\d\d)\b", re.I)
_OTHER_SEASON = re.compile(r"\b(spring|summer)\s*[-/]?\s*'?(20\d\d|\d\d)\b", re.I)
_EXCLUDE = re.compile(r"\b(?:senior|sr\.?|staff|principal|director|manager|lead|phd|ph\.d|doctoral|mba|"
                      r"marketing|sales|recruiter|clinical|veterinarian|surgery|surgeon)\b", re.I)
_RELEVANT = re.compile(r"\b(?:software|developer|programmer|data|machine learning|\bml\b|\bai\b|"
                       r"research|bioinformatics|biotech|biology|graphics|rendering|game|robotics|"
                       r"simulation|technical|technology|\bit\b|computing|engineering|engineer|"
                       r"artist|vfx|photonics|lab automation)\b", re.I)
_REMOTE = re.compile(r"\bremote\b", re.I)
_INTERNSHIP = re.compile(r"\b(?:intern|internship|co-?op)\b", re.I)
_NON_US_TITLE = re.compile(r"\b(?:anz|latam|emea|apac|australia|new zealand|malaysia|"
                           r"philippines|thailand|canada|europe|asia)\b", re.I)


def _school_year_season(text: str, now: datetime) -> bool:
    """Only the current school year: fall Sep-Nov, winter Dec-Feb."""
    fall_year = now.year if now.month >= 9 else now.year - 1
    winter_year = fall_year + 1
    for season, year in _SEASON.findall(text):
        year = int("20" + year if len(year) == 2 else year)
        if season.lower() in ("fall", "autumn") and year == fall_year and now.month <= 11:
            return True
        if season.lower() == "winter" and year == winter_year and (year, 2) >= (now.year, now.month):
            return True
    return False


def _accessible(job: Job, locations: list[str]) -> bool:
    """A school-year job must be local or explicitly US-remote, not a relocation."""
    title = job.title
    if _NON_US_TITLE.search(title):
        return False
    for loc in job.locations:
        if _REMOTE.search(loc):
            # Remote alone can still be explicitly non-US (or restricted by title).
            if is_us(loc) is not False:
                return True
        if is_us(loc) is True and any(s.lower() in loc.lower() for s in locations):
            return True
    return False


def select(jobs: list[Job], cfg: dict, now: datetime | None = None) -> tuple[list[Job], list[Job]]:
    """Return (confirmed part-time, seasonal roles with hours unconfirmed)."""
    now = now or datetime.now()
    opts = cfg.get("current_positions", {})
    flt = Filter(cfg)
    locations = opts.get("local_locations", ["Irvine", "Orange County", "Los Angeles"])
    minimum = float(opts.get("min_score", 2))
    seen: set[str] = set()
    part_time, seasonal = [], []
    for job in jobs:
        if not job.source.startswith(_ATS) or not job.title or not job.url:
            continue
        title, label = job.title, " ".join(str(job.extra.get(k) or "") for k in ("employmentType", "commitment"))
        if _EXCLUDE.search(title) or re.search(r"\bfull[ -]?time\b", title + " " + label, re.I):
            continue
        if _OTHER_SEASON.search(title) and not _school_year_season(title, now):
            continue
        if not _accessible(job, locations):
            continue
        # Company reputation alone isn't a role fit. Avoid marketing/operations jobs.
        if not _RELEVANT.search(title):
            continue
        flt.score(job)
        if job.score < minimum and not _PART_TIME.search(title + " " + label):
            continue
        if _PART_TIME.search(title + " " + label):
            bucket = part_time
        elif job.description and _PART_TIME_DESCRIPTION.search(job.description):
            bucket = part_time
        elif _school_year_season(title, now) and _INTERNSHIP.search(title) and not _OTHER_SEASON.search(title):
            bucket = seasonal  # Seasonal internships are NOT necessarily part-time.
        else:
            continue
        key = canonical_url(job.url)
        if key in seen:
            continue
        seen.add(key)
        bucket.append(job)
    for bucket in (part_time, seasonal):
        bucket.sort(key=lambda j: (-j.score, -(j.posted_at or 0), j.company, j.title))
    return part_time, seasonal


def _entry(job: Job) -> str:
    # ATS titles/locations are untrusted text; never let them inject Markdown rows.
    clean = lambda s: re.sub(r"[\r\n*\[\]]", " ", s).strip()
    place = ", ".join(job.locations[:2]) or "location unlisted"
    why = ", ".join(job.score_reasons[:3]) or "technical role"
    return f"- **{clean(job.company)} — {clean(job.title)}** · [Apply]({job.url}) · {clean(place)} · topic score {job.score:g} ({clean(why)})\n"


def write(results: dict[str, list[Job] | Exception], cfg: dict, root: Path, dry_run: bool = False) -> Path | None:
    """Atomically replace the snapshot; never discard it when every ATS is down."""
    opts = cfg.get("current_positions", {})
    if not opts.get("enabled", True):
        return None
    path = root / opts.get("filename", "currently-open-positions.md")
    successes = [v for k, v in results.items() if k.startswith(_ATS) and isinstance(v, list)]
    failed = sorted(k for k, v in results.items() if k.startswith(_ATS) and isinstance(v, Exception))
    if not successes:
        print("  ! current positions: no ATS sources succeeded; kept previous snapshot")
        return None
    jobs = [j for batch in successes for j in batch]
    part_time, seasonal = select(jobs, cfg)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    body = (f"# Currently open positions — {stamp}\n\n"
            "Live snapshot of listed openings on the configured company ATS boards, not a record of new alerts. "
            "Part-time work during school is the priority. Recheck the application page, hours, eligibility and "
            "start date before applying; a live board listing can change at any time. "
            "On-site results are limited to configured local cities; remote roles may have state restrictions.\n\n")
    if failed:
        body += f"Incomplete snapshot: {len(failed)} board(s) failed ({', '.join(failed)}). Their openings are not represented.\n\n"
    body += f"## Part-time / school-year work ({len(part_time)})\n\n"
    body += "".join(_entry(j) for j in part_time) or "No matching part-time openings on the available boards right now.\n"
    body += f"\n## Fall / winter opportunities — hours unconfirmed ({len(seasonal)})\n\n"
    body += "These are not confirmed part-time; check whether their schedule works alongside classes.\n\n"
    body += "".join(_entry(j) for j in seasonal) or "No matching seasonal openings on the available boards right now.\n"
    body += "\nOnly currently listed ATS jobs are included; campus, Handshake and unmonitored employers are not covered.\n"
    if dry_run:
        print(f"[DRY RUN] current positions → {path} ({len(part_time)} part-time, {len(seasonal)} seasonal)")
        return None
    try:
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  ! current positions {path}: {e}")
        return None
    print(f"  current positions → {path} ({len(part_time)} part-time, {len(seasonal)} seasonal)")
    return path
