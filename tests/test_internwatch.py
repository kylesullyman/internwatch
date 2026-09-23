import json
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

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
