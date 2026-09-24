"""internwatch — poll internship sources, alert on new matches, attach a tailored resume.

  python -m internwatch run [--dry-run]        one polling pass (what the scheduler calls)
  python -m internwatch loop --every 300       keep polling (for running on your own machine)
  python -m internwatch tailor URL [--company X --title Y]   tailor for one posting on demand
  python -m internwatch test-notify            send a test alert to every configured channel
  python -m internwatch sources                list configured sources and how many jobs each returns
  python -m internwatch positions              refresh currently-open-positions.md without alerts
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .filters import Filter
from .jd import fetch_description
from .models import Job
from .notify import Alert, Notifier
from .sources import build_sources
from .state import State
from . import logbook, open_positions

ROOT = Path(os.environ.get("INTERNWATCH_HOME", Path.cwd()))

# Master switch for the email channel (ntfy/discord are unaffected).
# Off right now: the Gmail account has 2FA, so SMTP_PASS must be a 16-char App
# Password (https://myaccount.google.com/apppasswords) — a normal account
# password gets rejected with "534 5.7.9 Application-specific password required".
# After rotating the secret, flip this to True (or set INTERNWATCH_EMAIL=1) and
# confirm with `python -m internwatch test-notify`.
EMAIL_NOTIFICATIONS = os.environ.get("INTERNWATCH_EMAIL", "").lower() in ("1", "true", "yes")


def _load_cfg(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def _ago(ts: float | None, job: Job | None = None) -> str:
    if job is not None and job.extra.get("posted_label"):
        return job.extra["posted_label"]      # Workday only has day granularity
    if not ts:
        return "posted time unknown"
    m = int((time.time() - ts) / 60)
    return f"posted {m} min ago" if m < 120 else f"posted {m // 60} h ago" if m < 2880 else f"posted {m // 1440} d ago"


def fetch_all(cfg: dict) -> dict[str, list[Job] | Exception]:
    sources = build_sources(cfg)
    results: dict[str, list[Job] | Exception] = {}
    with ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(fn): sid for sid, fn in sources}
        for f in as_completed(futs):
            sid = futs[f]
            try:
                results[sid] = f.result()
            except Exception as e:  # noqa: BLE001
                results[sid] = e
    return results


def _resume_path(cfg: dict) -> Path | None:
    p = ROOT / cfg.get("tailoring", {}).get("resume", "resume.yaml")
    return p if p.exists() else None


def _tailor_and_alert(job: Job, cfg: dict, notifier: Notifier, out_dir: Path, can_tailor: bool) -> None:
    tcfg = cfg.get("tailoring", {})
    head = f"New {job.extra.get('kind', 'role')}: {job.company} — {job.title}"
    lines = [_ago(job.posted_at, job), f"score {job.score:g} ({', '.join(job.score_reasons[:4]) or 'filters only'})"]
    if not can_tailor:
        notifier.send(Alert(job, head, "", lines, priority=4))
        return
    from .tailor import tailor  # imported lazily so the poller works without anthropic/typst installed
    jd = fetch_description(job)
    try:
        res = tailor(job, jd, _resume_path(cfg), out_dir, tcfg.get("model"))
    except Exception as e:  # noqa: BLE001 - still alert, just without the resume
        traceback.print_exc()
        notifier.send(Alert(job, head, f"Tailoring failed: {e}", lines + [f"⚠ tailoring failed: {e}"[:200]], priority=4))
        return
    prio = 5 if res.fit_score >= tcfg.get("urgent_fit", 8) else 4
    notifier.send(Alert(job, f"{head} · fit {res.fit_score}/10", res.summary_text, lines + res.push_lines,
                        [res.pdf, res.summary_md], prio))
    print(f"    tailored → {res.pdf.name} (fit {res.fit_score}/10, {len(res.warnings)} warnings)")


def run(args) -> int:
    cfg = _load_cfg(args.config)
    state = State(ROOT / cfg.get("state_file", "state/seen.json"))
    flt = Filter(cfg)
    notifier = Notifier(dry_run=args.dry_run, email=EMAIL_NOTIFICATIONS)
    out_dir = ROOT / "out"
    ncfg, tcfg = cfg.get("notify", {}), cfg.get("tailoring", {})

    t0 = time.time()
    results = fetch_all(cfg)
    # This snapshot is independent of seen-job state, including on the first run.
    open_positions.write(results, cfg, ROOT, dry_run=args.dry_run)
    new_matches: list[Job] = []
    for sid, res in sorted(results.items()):
        if isinstance(res, Exception):
            print(f"  ! {sid}: {type(res).__name__}: {res}")
            continue
        first = not state.initialized(sid)
        n_new = 0
        for job in res:
            if not state.is_new(job):
                continue
            n_new += 1
            if first:
                state.mark(job)
                continue
            why = flt.reject_reason(job)
            if why:
                state.mark(job)
                if args.verbose:
                    print(f"    skip [{why}] {job.short()}")
                continue
            new_matches.append(flt.score(job))
            state.mark(job)      # mark now so the same job from a second source in this run dedupes
        if first:
            state.set_initialized(sid)
            print(f"  {sid}: {len(res)} jobs — first run, recorded silently")
        elif n_new or args.verbose:
            print(f"  {sid}: {len(res)} jobs, {n_new} new")
    print(f"fetched {len(results)} sources in {time.time() - t0:.1f}s; {len(new_matches)} new matches")

    new_matches.sort(key=lambda j: j.score, reverse=True)
    can_tailor = bool(_resume_path(cfg)) and bool(os.environ.get("ANTHROPIC_API_KEY")) and tcfg.get("enabled", True)
    if not can_tailor and new_matches:
        print("  (tailoring off: needs resume.yaml + ANTHROPIC_API_KEY)")
    instant_min = ncfg.get("instant_min_score", 3)
    tailor_min = tcfg.get("min_score", 5)
    budget = tcfg.get("max_per_run", 5)
    digest = []
    try:
        # Write the logbook before alerting: if a tailoring call or a push fails,
        # the links are already on disk.
        logbook.write(new_matches, cfg, ROOT, dry_run=args.dry_run)
        for job in new_matches:
            if job.score >= instant_min:
                do_tailor = can_tailor and job.score >= tailor_min and budget > 0
                budget -= do_tailor
                print(f"  → {job.short()}  [score {job.score:g}, {_ago(job.posted_at, job)}]")
                _tailor_and_alert(job, cfg, notifier, out_dir, do_tailor)
            else:
                digest.append(job)
        if ncfg.get("digest", True):
            notifier.digest(digest)
    finally:
        if not args.dry_run or args.save:
            state.save()
    return 0


def loop(args) -> int:
    while True:
        try:
            run(args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(args.every)


def tailor_cmd(args) -> int:
    cfg = _load_cfg(args.config)
    rp = _resume_path(cfg)
    if not rp:
        sys.exit("resume.yaml not found — see README")
    host = urlparse(args.url).netloc
    parts = [p for p in urlparse(args.url).path.split("/") if p]
    company = args.company or (parts[0] if any(h in host for h in ("greenhouse", "lever", "ashby")) and parts else host.split(".")[-2])
    job = Job(source="manual", company=company.title() if not args.company else company,
              title=args.title or "Internship", url=args.url)
    jd = fetch_description(job)
    print(f"JD: {len(jd)} chars")
    from .tailor import tailor
    res = tailor(job, jd, rp, ROOT / "out", cfg.get("tailoring", {}).get("model"))
    if args.print:     # off by default: Actions logs are public in a public repo
        print(res.summary_text)
    print(f"PDF: {res.pdf}\nChanges: {res.summary_md}  (fit {res.fit_score}/10, {len(res.warnings)} warnings)")
    if args.notify:
        Notifier(email=EMAIL_NOTIFICATIONS).send(
            Alert(job, f"Tailored: {job.company} — {job.title} · fit {res.fit_score}/10",
                  res.summary_text, res.push_lines, [res.pdf, res.summary_md]))
    return 0


def test_notify(args) -> int:
    n = Notifier(email=EMAIL_NOTIFICATIONS)
    print("channels:", n.channels or "NONE — set NTFY_TOPIC and/or SMTP_* / DISCORD_WEBHOOK_URL")
    if not EMAIL_NOTIFICATIONS:
        print("  (email disabled by EMAIL_NOTIFICATIONS — set INTERNWATCH_EMAIL=1 to include it)")
    job = Job("test", "Example Co", "Software Engineering Intern (test alert)", "https://example.com/job",
              ["Irvine, CA"], time.time())
    n.send(Alert(job, "internwatch test alert", "If you got this, notifications work.", ["It works."]))
    return 0


def positions_cmd(args) -> int:
    cfg = _load_cfg(args.config)
    return 0 if open_positions.write(fetch_all(cfg), cfg, ROOT) else 1


def sources_cmd(args) -> int:
    cfg = _load_cfg(args.config)
    for sid, res in sorted(fetch_all(cfg).items()):
        print(f"{sid:40s} {'ERROR ' + str(res)[:60] if isinstance(res, Exception) else len(res)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="internwatch")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "loop"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; don't save state")
        p.add_argument("--save", action="store_true", help="with --dry-run, still save state")
        p.add_argument("-v", "--verbose", action="store_true")
        if name == "loop":
            p.add_argument("--every", type=int, default=300, help="seconds between polls")
    p = sub.add_parser("tailor")
    p.add_argument("url")
    p.add_argument("--company")
    p.add_argument("--title")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--print", action="store_true", help="print the full change summary")
    sub.add_parser("test-notify")
    sub.add_parser("sources")
    sub.add_parser("positions", help="refresh the open positions snapshot without alerts")
    args = ap.parse_args()
    return {"run": run, "loop": loop, "tailor": tailor_cmd, "test-notify": test_notify,
            "sources": sources_cmd, "positions": positions_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
