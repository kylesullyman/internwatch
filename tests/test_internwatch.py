import json
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest
import yaml

from internwatch import logbook, open_positions
from internwatch import tailor as T
from internwatch.filters import Filter, is_us
from internwatch.models import Job, canonical_url
from internwatch.notify import Alert, Notifier
from internwatch.render import _page_count, fit_and_render
from internwatch.sources import github_lists
from internwatch.state import State

ROOT = Path(__file__).parent.parent
FX = Path(__file__).parent / "fixtures"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


# ---- sources ------------------------------------------------------------------------
def test_simplify_json_parser():
    with mock.patch.object(github_lists, "get_json", return_value=json.loads((FX / "simplify_sample.json").read_text())):
        jobs = github_lists.simplify_json("simplify", "x")
    assert jobs and all(j.company and j.title and j.url.startswith("http") for j in jobs)
    assert all(j.posted_at for j in jobs)


def test_markdown_table_parser():
    with mock.patch.object(github_lists, "get_text", return_value=(FX / "speedyapply_sample.md").read_text()):
        jobs = github_lists.markdown_table("speedyapply", "x")
    assert len(jobs) > 100
    j = jobs[0]
    assert "<" not in j.company and "<" not in j.title and j.url.startswith("http")
    assert all(j.company for j in jobs)


# ---- dedupe -------------------------------------------------------------------------
def test_canonical_url_dedupes_hosts_and_tracking():
    a = canonical_url("https://boards.greenhouse.io/figma/jobs/123?gh_src=abc&utm_source=x")
    b = canonical_url("https://job-boards.greenhouse.io/figma/jobs/123/")
    assert a == b
    assert canonical_url("https://x.com/job?gh_jid=5") != canonical_url("https://x.com/job?gh_jid=6")


def test_state_first_seen_and_prune(tmp_path):
    s = State(tmp_path / "s.json")
    j = Job("t", "Figma", "SWE Intern", "https://job-boards.greenhouse.io/figma/jobs/1", ["SF"])
    assert s.is_new(j)
    s.mark(j)
    s.save()
    s2 = State(tmp_path / "s.json")
    assert not s2.is_new(j)
    # same job via a different source/URL but same company/title/location → not new
    j2 = Job("list", "Figma", "SWE Intern", "https://figma.com/careers?gh_jid=1", ["SF"])
    assert not s2.is_new(j2)


# ---- filters ------------------------------------------------------------------------
@pytest.mark.parametrize("loc,exp", [
    ("San Francisco, CA", True), ("US, CA, Santa Clara", True), ("Remote in USA", True),
    ("Ottawa, ON, Canada", False), ("London, UK", False), ("China, Shanghai", False),
    ("Indianapolis, IN", True), ("3 Locations", None), ("New York", True)])
def test_is_us(loc, exp):
    assert is_us(loc) is exp


def _job(title, source="greenhouse:x", **kw):
    return Job(source, kw.pop("company", "Acme"), title, "https://x/1", kw.pop("locations", ["Irvine, CA"]),
               kw.pop("posted_at", time.time()), **kw)


def test_filter_rules():
    f = Filter(CFG)
    assert f.reject_reason(_job("Software Engineer Intern")) is None
    assert f.reject_reason(_job("Senior Software Engineer")) == "not an internship/new-grad title"
    assert f.reject_reason(_job("PhD Research Intern")) == "title excluded"
    assert f.reject_reason(_job("Software Intern - Summer 2026")).startswith("season")
    assert f.reject_reason(_job("Software Intern - Spring 2027")).startswith("season")
    assert f.reject_reason(_job("Software Intern - Summer 2027")) is None
    assert f.reject_reason(_job("Software Intern", locations=["Toronto, Canada"])) == "not in US"
    assert f.reject_reason(_job("Software Intern", posted_at=time.time() - 10 * 86400)) == "too old"
    # list sources skip the intern-title check but respect terms
    assert f.reject_reason(_job("Software Engineer", source="simplify", terms=["Summer 2027"])) is None
    assert f.reject_reason(_job("Software Engineer", source="simplify", terms=["Winter 2026"])).startswith("terms")
    assert f.reject_reason(_job("Software Engineer", source="simplify", terms=["N/A"])) is None


def test_scoring_prefers_graphics_at_nvidia():
    f = Filter(CFG)
    a = f.score(_job("Graphics Software Intern", company="NVIDIA", locations=["US, CA, Santa Clara"]))
    b = f.score(_job("Data Intern", company="Acme", locations=["Madison, WI"]))
    assert a.score > b.score and a.score >= CFG["tailoring"]["min_score"]


# ---- tailoring guardrails ----------------------------------------------------------
@pytest.fixture
def resume():
    return T.load_resume(ROOT / "resume.example.yaml")


def test_validate_reverts_fabrications(resume):
    out = json.loads((FX / "mock_tailor_nvidia.json").read_text())
    doc, warnings, _ = T.validate(resume, out)
    lab = next(e for s in doc["sections"] for e in s["entries"] if e["id"] == "lab")
    assert "Began porting the controller to C++ for lower-latency acquisition" in lab["bullets"]
    assert not any("CUDA" in b for s in doc["sections"] for e in s["entries"] for b in e["bullets"])
    assert all("CUDA" not in s["items"] for s in doc["skills"])
    assert any("ghost" in w for w in warnings)


def test_validate_rejects_cross_entry_tech(resume):
    # C++ is a real skill, but the MCP project never used it — claiming it there is fabrication.
    out = {"sections": [{"id": "projects", "entries": [{"id": "mcp", "bullets": [
        {"source_id": "mcp-1", "text": "Built a C++ Model Context Protocol server exposing search tools to an LLM"}]}]}],
        "skills": [], "changes": [], "fit": {}}
    doc, warnings, _ = T.validate(resume, out)
    mcp = next(e for s in doc["sections"] for e in s["entries"] if e["id"] == "mcp")
    assert "C++" not in mcp["bullets"][0] and warnings


def test_pinned_entries_always_included(resume):
    out = {"sections": [{"id": "projects", "entries": [{"id": "fps", "bullets": [
        {"source_id": "fps-1", "text": "x"}]}]}], "skills": [], "changes": [], "fit": {}}
    doc, warnings, _ = T.validate(resume, out)
    assert any(e["id"] == "lab" for s in doc["sections"] for e in s["entries"])
    assert [s["id"] for s in doc["sections"]] == ["experience", "projects"]


def test_end_to_end_tailor_with_mock(resume, tmp_path):
    out = json.loads((FX / "mock_tailor_nvidia.json").read_text())
    job = Job("workday:nvidia", "NVIDIA", "Software Engineering Intern", "https://nvidia.example/job", ["US, CA, Santa Clara"])
    with mock.patch.object(T, "_call_claude", return_value=out):
        res = T.tailor(job, (FX / "nvidia_jd.txt").read_text(), ROOT / "resume.example.yaml", tmp_path)
    assert res.pdf.exists() and _page_count(res.pdf.read_bytes()) == 1
    assert "What changed and why" in res.summary_text and "Warnings" in res.summary_text
    assert res.fit_score == 6


def test_fit_trims_to_one_page(resume):
    doc = T.base_doc(resume)
    for s in doc["sections"]:
        for e in s["entries"]:
            e["bullets"] = e["bullets"] * 5
    pdf, _, notes = fit_and_render(doc, 1, {"lab"})
    assert _page_count(pdf) == 1 and notes


# ---- notifications -----------------------------------------------------------------
def test_email_has_pdf_and_html(tmp_path, monkeypatch):
    for k, v in dict(SMTP_HOST="smtp.test", SMTP_USER="me@test", SMTP_PASS="pw", EMAIL_TO="me@test").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): pass
        def send_message(self, m): sent["m"] = m

    with mock.patch("smtplib.SMTP_SSL", FakeSMTP):
        Notifier().send(Alert(_job("Graphics Intern"), "New!", "## Changes\n- **a** — b", ["x"], [pdf]))
    m = sent["m"]
    assert "Graphics Intern" in m["Subject"]
    assert [p.get_filename() for p in m.iter_attachments()] == ["r.pdf"]
    assert "<h3>Changes</h3>" in m.get_body(("html",)).get_content()


def _smtp_env(monkeypatch):
    for k, v in dict(SMTP_HOST="smtp.test", SMTP_USER="me@test", SMTP_PASS="pw", EMAIL_TO="me@test").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


def test_email_flag_disables_channel(monkeypatch):
    """The email=False switch drops the channel even with every SMTP_* secret present."""
    _smtp_env(monkeypatch)
    assert Notifier().channels == ["email"]
    assert Notifier(email=False).channels == []

    def boom(*a, **k):
        raise AssertionError("SMTP must not be touched when email is disabled")

    with mock.patch("smtplib.SMTP_SSL", boom):
        Notifier(email=False).send(Alert(_job("Graphics Intern"), "New!", "", ["x"]))
        Notifier(email=False).digest([_job("Graphics Intern")])


def test_digest_survives_broken_email(monkeypatch, capsys):
    """A failing channel in digest() is logged, not raised — a dead mail server
    must not fail the whole poll (the 534 App Password outage did exactly that)."""
    _smtp_env(monkeypatch)

    def boom(*a, **k):
        raise OSError("Connection unexpectedly closed")

    with mock.patch("smtplib.SMTP_SSL", boom):
        Notifier().digest([_job("Graphics Intern")])          # must not raise
        Notifier().send(Alert(_job("Graphics Intern"), "New!", "", ["x"]))
    out = capsys.readouterr().out
    assert out.count("! email failed") == 2


# ---- logbook -----------------------------------------------------------------------
def _scored(company, title, url, score=9.0):
    j = Job("greenhouse:x", company, title, url, ["Irvine, CA"], time.time())
    j.score, j.score_reasons, j.extra["kind"] = score, ["bioinformatics +9"], "internship"
    return j


def _logcfg(*dirs):
    return {"logbook": {"enabled": True, "filename": "internwatch-{month}.md",
                        "create_dirs": True, "dirs": [str(d) for d in dirs]}}


def test_logbook_writes_every_destination(tmp_path):
    a, b = tmp_path / "applications", tmp_path / "vault"
    b.mkdir()
    jobs = [_scored("Insitro", "ML Intern, CompBio", "https://jobs.ashbyhq.com/insitro/1"),
            _scored("Recursion", "Bioinformatics Intern", "https://boards.greenhouse.io/recursion/jobs/2")]
    notes = logbook.write(jobs, _logcfg(a, b), tmp_path)
    assert len(notes) == 2
    text = notes[0].read_text()
    assert text == notes[1].read_text()
    assert text.count("- [ ]") == 2
    assert "[Apply](https://jobs.ashbyhq.com/insitro/1)" in text
    assert "tags: [career, job-search, applications, internwatch]" in text


def test_logbook_dedupes_across_runs(tmp_path):
    d = tmp_path / "applications"
    cfg = _logcfg(d)
    first = _scored("Recursion", "Bioinformatics Intern", "https://boards.greenhouse.io/recursion/jobs/2")
    logbook.write([first], cfg, tmp_path)
    # same posting, other Greenhouse host + tracking params, plus one genuinely new job
    again = _scored("Recursion", "Bioinformatics Intern",
                    "https://job-boards.greenhouse.io/recursion/jobs/2?utm_source=x")
    new = _scored("Xaira", "Protein Design Intern", "https://boards.greenhouse.io/xaira/jobs/3")
    logbook.write([again, new], cfg, tmp_path)
    text = (d / f"internwatch-{datetime.now().strftime('%Y-%m')}.md").read_text()
    assert text.count("- [ ]") == 2
    assert text.count("Bioinformatics Intern") == 1
    # a run with nothing new must not append an empty section
    logbook.write([again], cfg, tmp_path)
    assert text == (d / f"internwatch-{datetime.now().strftime('%Y-%m')}.md").read_text()


def test_logbook_orders_entries_newest_first(tmp_path):
    d = tmp_path / "applications"
    now = time.time()
    old = _scored("Insitro", "ML Intern", "https://x/1", score=9.9)
    old.posted_at = now - 3 * 86400
    newest = _scored("Xaira", "Protein Design Intern", "https://x/2", score=1.0)
    newest.posted_at = now
    middle = _scored("Recursion", "Bioinformatics Intern", "https://x/3", score=5.0)
    middle.posted_at = now - 3600
    undated = _scored("Genentech", "Data Intern", "https://x/4", score=7.0)
    undated.posted_at = None
    logbook.write([old, newest, middle, undated], _logcfg(d), tmp_path)
    text = (d / f"internwatch-{datetime.now().strftime('%Y-%m')}.md").read_text()
    order = [line.split("**")[1].split(" — ")[0] for line in text.splitlines() if line.startswith("- [ ]")]
    # newest posting first, regardless of score; unknown posting date sinks to the end
    assert order == ["Xaira", "Recursion", "Insitro", "Genentech"]


def test_logbook_skips_missing_vault_without_inventing_it(tmp_path):
    d, missing = tmp_path / "applications", tmp_path / "not-mounted" / "vault" / "work"
    notes = logbook.write([_scored("Insitro", "ML Intern", "https://x/1")], _logcfg(d, missing), tmp_path)
    assert len(notes) == 1 and d.is_dir()
    assert not (tmp_path / "not-mounted").exists()


def test_logbook_config_paths_resolve():
    """The real config must point at the repo and the Obsidian work folder."""
    dirs = logbook.destinations(CFG, ROOT)
    assert ROOT / "applications" in dirs
    assert any(d.name == "work" and "obsidian" in str(d) and "$" not in str(d) and "~" not in str(d)
               for d in dirs), dirs


# ---- current school-year openings ---------------------------------------------------
def test_open_positions_prioritizes_part_time_without_summer_alert_filters():
    now = datetime(2026, 9, 24)
    jobs = [
        _job("Technical Developer Specialist, USA (Part-Time Contract)", source="greenhouse:roblox",
             company="Roblox", locations=["Remote"]),
        _job("Software Engineer Intern (Winter 2027)", source="greenhouse:figma",
             company="Figma", locations=["Los Angeles, CA"]),
        _job("Applied AI Intern - Fall 2026", source="workday:nvidia", company="NVIDIA"),
        _job("Part-Time Marketing Intern", source="ashby:x"),
        _job("Software Engineer Intern (Summer 2027)", source="ashby:x"),
        _job("Research Intern (Winter 2026)", source="ashby:x"),
        _job("Game Engine Intern (Fall 2027)", source="ashby:x"),
        _job("Software Engineer I (Graduation Date: Fall 2026-Summer 2027)", source="ashby:x"),
        _job("Software Engineer Intern (Winter 2027)", source="ashby:x", locations=["Canada"]),
        _job("Data Analyst (Part-time)", source="lever:zoox", locations=["Foster City, CA"]),
        _job("Data Analyst (Part-time)", source="simplify"),
    ]
    for n, job in enumerate(jobs):
        job.url = f"https://example.com/jobs/{n}"
    part, seasonal = open_positions.select(jobs, CFG, now)
    assert [j.title for j in part] == ["Technical Developer Specialist, USA (Part-Time Contract)"]
    assert {j.title for j in seasonal} == {"Software Engineer Intern (Winter 2027)", "Applied AI Intern - Fall 2026"}


def test_open_positions_metadata_description_and_deduping():
    now = datetime(2026, 9, 24)
    ashby = _job("IT Support Associate", source="ashby:x", extra={"employmentType": "PartTime"})
    lever = _job("Robotics Research Student", source="lever:x",
                 description="This role is part-time and works alongside school.")
    lever.url = "https://example.com/jobs/research"
    duplicate = _job("IT Support Associate", source="ashby:x", extra={"employmentType": "PartTime"})
    foreign = _job("Technical Artist (part-time) - LATAM", source="greenhouse:x", locations=["Remote"])
    part, seasonal = open_positions.select([ashby, lever, duplicate, foreign], CFG, now)
    assert [j.title for j in part] == ["Robotics Research Student", "IT Support Associate"]
    assert seasonal == []


def test_open_positions_refresh_removes_closed_and_respects_dry_run(tmp_path):
    job = _job("Software Developer (part-time)", source="greenhouse:x")
    path = tmp_path / "currently-open-positions.md"
    assert open_positions.write({"greenhouse:x": [job]}, CFG, tmp_path, dry_run=True) is None
    assert not path.exists()
    assert open_positions.write({"greenhouse:x": [job, job]}, CFG, tmp_path) == path
    assert path.read_text().count("[Apply]") == 1
    assert open_positions.write({"greenhouse:x": []}, CFG, tmp_path) == path
    assert "No matching part-time openings" in path.read_text()
    assert "[Apply]" not in path.read_text()
    before = path.read_text()
    assert open_positions.write({"greenhouse:x": RuntimeError("offline")}, CFG, tmp_path) is None
    assert path.read_text() == before


def test_open_positions_reports_partial_failure(tmp_path):
    job = _job("AI Developer (part time)", source="greenhouse:x")
    open_positions.write({"greenhouse:x": [job], "ashby:y": RuntimeError("offline")}, CFG, tmp_path)
    text = (tmp_path / "currently-open-positions.md").read_text()
    assert "Incomplete snapshot" in text and "ashby:y" in text and "AI Developer" in text


def test_run_writes_positions_on_first_run_without_alerts(tmp_path, monkeypatch):
    from internwatch import __main__ as main
    from types import SimpleNamespace

    job = _job("Technical Developer Specialist, USA (Part-Time Contract)", source="greenhouse:roblox",
               company="Roblox", locations=["Remote"])
    cfg = {**CFG, "logbook": {"enabled": False}, "state_file": "state/seen.json"}
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "_load_cfg", lambda _: cfg)
    monkeypatch.setattr(main, "fetch_all", lambda _: {"greenhouse:roblox": [job]})
    assert main.run(SimpleNamespace(config="config.yaml", dry_run=False, save=False, verbose=False)) == 0
    path = tmp_path / "currently-open-positions.md"
    assert "Technical Developer Specialist" in path.read_text()
    assert main.run(SimpleNamespace(config="config.yaml", dry_run=False, save=False, verbose=False)) == 0
    assert path.read_text().count("[Apply]") == 1
