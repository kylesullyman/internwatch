"""Markdown logbook of every match, written to the repo and to the Obsidian vault.

One note per month per destination (`internwatch-2026-09.md`), appended to in place so a
month's matches accumulate in a single note instead of scattering one file per run.

Entries are Obsidian-flavoured task checkboxes, so `- [ ]` lines show up in Tasks/Dataview
queries and you can tick one off once you've applied. Within a run's section, entries are
ordered newest posting first. Re-running is safe: every entry carries its canonical URL,
and URLs already present in the note are skipped.

A destination that doesn't exist (e.g. the vault isn't mounted in CI) is skipped with a
warning — a missing vault must never take the poller down.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

from .models import Job, canonical_url

_URL_IN_NOTE = re.compile(r"<!-- iw:(\S+) -->")


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def destinations(cfg: dict, root: Path) -> list[Path]:
    """Resolve the configured logbook directories to absolute paths."""
    lcfg = cfg.get("logbook", {})
    out = []
    for raw in lcfg.get("dirs", []):
        p = _expand(str(raw))
        out.append(p if p.is_absolute() else root / p)
    return out


def _header(month: str) -> str:
    return (f"---\ncreated: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"tags: [career, job-search, applications, internwatch]\n---\n\n"
            f"# Application links — {month}\n\n"
            f"Auto-written by [internwatch](https://github.com/) on every poll. "
            f"Tick a box once you've applied.\n"
            f"Related: [[Positions to Apply For]], [[Applications]], [[biotech-positions-sep2026]]\n")


def _ago(job: Job) -> str:
    if job.extra.get("posted_label"):
        return job.extra["posted_label"]
    if not job.posted_at:
        return "posted time unknown"
    m = int((time.time() - job.posted_at) / 60)
    return f"posted {m} min ago" if m < 120 else f"posted {m // 60} h ago" if m < 2880 else f"posted {m // 1440} d ago"


def _recency_key(job: Job) -> float:
    """Sort key putting the most recently posted job first.

    Negated epoch seconds, so a plain ascending sort is newest-first; a job whose source
    didn't give us a date scores 0 and lands at the end of the batch. The sort is stable,
    so jobs sharing a timestamp (or all lacking one) keep the score order they came in with.
    """
    return -(job.posted_at or 0.0)


def entry(job: Job) -> str:
    """One Obsidian task line plus an indented detail line, tagged with a dedupe marker."""
    loc = ", ".join(job.locations[:3]) or "location n/a"
    kind = job.extra.get("kind", "role")
    why = ", ".join(job.score_reasons[:4]) or "filters only"
    return (f"- [ ] **{job.company} — {job.title}** · [Apply]({job.url}) <!-- iw:{canonical_url(job.url)} -->\n"
            f"\t{kind} · {loc} · score {job.score:g} ({why}) · {_ago(job)} · `{job.source}`\n")


def write(jobs: list[Job], cfg: dict, root: Path, dry_run: bool = False) -> list[Path]:
    """Append `jobs` to this month's note in every configured directory.

    Returns the notes actually written. Safe to call with an empty list.
    """
    if not jobs or not cfg.get("logbook", {}).get("enabled", True):
        return []
    month = datetime.now().strftime("%Y-%m")
    stem = cfg.get("logbook", {}).get("filename", "internwatch-{month}.md").format(month=month)
    written = []
    for d in destinations(cfg, root):
        try:
            if not d.exists():
                # Only create a leaf whose parent is already there. That makes the repo's
                # applications/ dir on first run, but refuses to invent a whole tree for an
                # unmounted vault or a typo'd path — those get a warning instead.
                if cfg.get("logbook", {}).get("create_dirs", True) and d.parent.is_dir():
                    d.mkdir(parents=False, exist_ok=True)
                else:
                    print(f"  ! logbook: {d} does not exist, skipped")
                    continue
            note = d / stem
            existing = note.read_text() if note.exists() else ""
            have = set(_URL_IN_NOTE.findall(existing))
            new = [j for j in jobs if canonical_url(j.url) not in have]
            # A job can appear twice in one batch via two sources; keep the first.
            seen, deduped = set(), []
            for j in new:
                c = canonical_url(j.url)
                if c not in seen:
                    seen.add(c)
                    deduped.append(j)
            if not deduped:
                continue
            deduped.sort(key=_recency_key)
            body = existing or _header(month)
            if not body.endswith("\n"):
                body += "\n"
            body += f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')} — {len(deduped)} new\n\n"
            body += "".join(entry(j) for j in deduped)
            if dry_run:
                print(f"[DRY RUN] logbook → {note} (+{len(deduped)} entries)")
                continue
            tmp = note.with_suffix(".md.tmp")
            tmp.write_text(body)
            os.replace(tmp, note)
            written.append(note)
            print(f"  logbook → {note} (+{len(deduped)})")
        except OSError as e:
            print(f"  ! logbook {d}: {e}")
    return written
