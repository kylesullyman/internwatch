"""Tailor the base resume to one job description, then prove it didn't make anything up.

Claude proposes: which entries/bullets to show and in what order, reworded bullets,
skill ordering, an optional summary line, and an explanation of each change.
Code then enforces the rules deterministically — the model's promise not to fabricate
is not trusted on its own:

  * every bullet must cite a `source_id` from the base resume, and only its own entry
  * a rewritten bullet may not introduce a number or a technology that the entry never mentioned
  * skills must come from `skills` or `extra_skills` in resume.yaml
  * entry headers (title, org, dates, location) are copied from the base, never from the model

Anything that fails a check is reverted to the original wording and listed under Warnings.
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import vocab
from .models import Job
from .render import fit_and_render

DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM = """You are an expert technical recruiter and resume editor helping a computer science \
student tailor their resume for one specific internship posting.

Hard rules (these are checked by code; violations are automatically reverted):
1. Never invent anything. Every bullet you output must be a rewrite of exactly one base bullet \
(cite its source_id) from the same entry. Do not add technologies, tools, languages, metrics, \
numbers, team sizes, users, outcomes, or responsibilities that the base resume does not state \
for that entry.
2. You MAY: choose which entries and bullets to include; reorder entries and bullets by \
relevance; reword a bullet to lead with the aspect the job cares about; use the job's \
terminology for something the bullet already describes (e.g. "serial protocol" → "device \
communication protocol"); tighten wording; select and reorder skills; write a one-sentence \
summary grounded only in the resume.
3. Skills must be chosen only from the provided skills and extra_skills lists.
4. Entries listed as pinned must be included.
5. Keep it to one page: roughly {bullet_budget} bullets in total across all sections.
6. Be candid in `fit`: list real gaps (requirements in the posting the resume does not show) \
so the student knows what to prepare for. Do not pad strengths.
Prefer strong verbs and concrete scope; avoid buzzwords and filler like "passionate" or "leveraged"."""

TOOL = {
    "name": "submit_tailored_resume",
    "description": "Submit the tailored resume and an explanation of every change.",
    "input_schema": {
        "type": "object",
        "required": ["sections", "skills", "changes", "fit"],
        "properties": {
            "summary": {"type": "string", "description": "One sentence, or empty string for no summary."},
            "coursework": {"type": "array", "items": {"type": "string"},
                           "description": "Relevant coursework to list, chosen from the base list, most relevant first."},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object", "required": ["id", "entries"],
                    "properties": {
                        "id": {"type": "string"},
                        "entries": {"type": "array", "items": {
                            "type": "object", "required": ["id", "bullets"],
                            "properties": {
                                "id": {"type": "string"},
                                "bullets": {"type": "array", "items": {
                                    "type": "object", "required": ["source_id", "text"],
                                    "properties": {"source_id": {"type": "string"}, "text": {"type": "string"}}}},
                            }}},
                    }},
            },
            "skills": {"type": "array", "items": {
                "type": "object", "required": ["category", "items"],
                "properties": {"category": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}}}},
            "changes": {"type": "array", "items": {
                "type": "object", "required": ["what", "why"],
                "properties": {"what": {"type": "string"}, "why": {"type": "string"}}},
                "description": "Every meaningful change and the reason, tied to the posting."},
            "fit": {"type": "object", "required": ["score", "one_liner", "strengths", "gaps"], "properties": {
                "score": {"type": "integer", "minimum": 1, "maximum": 10},
                "one_liner": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "interview_prep": {"type": "array", "items": {"type": "string"},
                                   "description": "2-4 specific things to review before an interview."}}},
        },
    },
}


# ---------------------------------------------------------------------------------------
@dataclass
class Result:
    pdf: Path
    summary_md: Path
    fit_score: int
    one_liner: str
    push_lines: list[str]
    summary_text: str
    warnings: list[str] = field(default_factory=list)


def load_resume(path: str | Path) -> dict:
    r = yaml.safe_load(Path(path).read_text())
    for s in r["sections"]:
        for e in s["entries"]:
            for i, b in enumerate(e.get("bullets", [])):
                if isinstance(b, str):      # allow plain strings; give them ids
                    e["bullets"][i] = {"id": f"{e['id']}-{i + 1}", "text": b}
    return r


def _entry_text(e: dict) -> str:
    return " ".join([e.get("heading", ""), e.get("org", "") or "", e.get("subline", "") or "",
                     " ".join(e.get("tags", []))] + [b["text"] for b in e.get("bullets", [])])


def _all_skills(r: dict) -> dict[str, str]:
    """lowercased -> canonical spelling"""
    out = {}
    for items in (r.get("skills") or {}).values():
        out.update({s.lower(): s for s in items})
    out.update({s.lower(): s for s in r.get("extra_skills") or []})
    return out


def resume_plaintext(doc: dict) -> str:
    parts = [doc.get("summary") or ""]
    for e in doc.get("education", []):
        parts += [e.get("heading", ""), e.get("org", "") or "", e.get("subline", "") or ""] + e.get("bullets", [])
    for s in doc["sections"]:
        for e in s["entries"]:
            parts += [e["heading"], e.get("org") or "", e.get("subline") or ""] + e["bullets"]
    for s in doc["skills"]:
        parts += s["items"]
    return "\n".join(p if isinstance(p, str) else p.get("text", "") for p in parts)


def base_doc(r: dict) -> dict:
    """The untailored resume in render format (used for 'before' coverage and as a fallback)."""
    return {
        "name": r["name"], "contact": r["contact"], "summary": r.get("summary") or "",
        "education": [_edu(e, e.get("coursework")) for e in r.get("education", [])],
        "sections": [{"id": s["id"], "title": s["title"], "entries": [
            {**_hdr(e), "bullets": [b["text"] for b in e.get("bullets", [])]} for e in s["entries"]
            if not e.get("hidden")]} for s in r["sections"]],
        "skills": [{"category": k, "items": v} for k, v in (r.get("skills") or {}).items()],
        "skills_first": bool((r.get("layout") or {}).get("skills_first")),
    }


def _hdr(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "heading", "org", "subline", "location", "dates")}


def _edu(e: dict, coursework: list[str] | None) -> dict:
    bullets = list(e.get("bullets") or [])
    if coursework:
        bullets.append("Relevant coursework: " + ", ".join(coursework))
    return {**_hdr(e), "bullets": bullets}


# ---------------------------------------------------------------------------------------
def _call_claude(r: dict, job: Job, jd: str, model: str, bullet_budget: int) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    base_for_model = {k: r.get(k) for k in ("summary", "education", "sections", "skills", "extra_skills")}
    pinned = (r.get("tailoring") or {}).get("pinned", [])
    user = (
        f"## Posting\nCompany: {job.company}\nTitle: {job.title}\nType: {job.extra.get('kind', 'unknown')}\n"
        f"Location: {', '.join(job.locations)}\n\n"
        f"{jd[:14000] if jd else '(Job description could not be retrieved — tailor from the title and company only, conservatively.)'}\n\n"
        f"## Base resume (YAML-derived JSON)\n```json\n{json.dumps(base_for_model, indent=1)}\n```\n\n"
        f"Pinned entries (must include): {pinned}\n"
        f"Candidate notes: {(r.get('tailoring') or {}).get('notes', '')}\n\n"
        "Call submit_tailored_resume."
    )
    msg = client.messages.create(
        model=model, max_tokens=8000,
        system=SYSTEM.format(bullet_budget=bullet_budget),
        tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no tool call")


def validate(r: dict, out: dict) -> tuple[dict, list[str], list[dict]]:
    """Build the render doc from model output, enforcing the no-fabrication rules.

    Returns (doc, warnings, bullet_changes) where bullet_changes lists every rewrite that survived.
    """
    warnings: list[str] = []
    rewrites: list[dict] = []
    skills_ok = _all_skills(r)
    user_terms = set(skills_ok)
    sections = {s["id"]: s for s in r["sections"]}
    pinned = set((r.get("tailoring") or {}).get("pinned", []))

    doc_sections, used_entries = [], set()
    for so in out.get("sections", []):
        base_s = sections.get(so.get("id"))
        if not base_s:
            warnings.append(f"Model referenced unknown section '{so.get('id')}' — ignored.")
            continue
        entries = {e["id"]: e for e in base_s["entries"]}
        new_entries = []
        for eo in so.get("entries", []):
            be = entries.get(eo.get("id"))
            if not be or eo["id"] in used_entries:
                warnings.append(f"Unknown or duplicate entry '{eo.get('id')}' — ignored.")
                continue
            used_entries.add(eo["id"])
            src_text = _entry_text(be)
            src_nums = set(re.findall(r"\d+(?:\.\d+)?", src_text))
            src_terms = vocab.terms_in(src_text, user_terms)
            bmap = {b["id"]: b["text"] for b in be.get("bullets", [])}
            bullets, used_b = [], set()
            for bo in eo.get("bullets", []):
                sid, text = bo.get("source_id"), (bo.get("text") or "").strip()
                if sid not in bmap:
                    warnings.append(f"{be['heading']}: bullet cites unknown source '{sid}' — dropped: “{text}”")
                    continue
                if sid in used_b:
                    continue
                used_b.add(sid)
                problems = []
                new_nums = set(re.findall(r"\d+(?:\.\d+)?", text)) - src_nums
                if new_nums:
                    problems.append(f"new number(s) {sorted(new_nums)}")
                new_terms = vocab.terms_in(text, user_terms, strict=True) - src_terms
                if new_terms:
                    problems.append(f"new technology {sorted(new_terms)}")
                if problems:
                    warnings.append(f"{be['heading']}: reverted a rewrite that added {', '.join(problems)}: “{text}”")
                    text = bmap[sid]
                if text != bmap[sid]:
                    rewrites.append({"entry": be["heading"], "before": bmap[sid], "after": text})
                bullets.append(text)
            if not bullets:
                bullets = [b["text"] for b in be.get("bullets", [])][:2]
            new_entries.append({**_hdr(be), "bullets": bullets})
        if new_entries:
            doc_sections.append({"id": base_s["id"], "title": base_s["title"], "entries": new_entries})

    # Pinned entries the model left out go back in, in their original section.
    for s in r["sections"]:
        for e in s["entries"]:
            if e["id"] in pinned and e["id"] not in used_entries:
                warnings.append(f"Re-added pinned entry {e['heading']}.")
                tgt = next((d for d in doc_sections if d["id"] == s["id"]), None)
                if tgt is None:
                    tgt = {"id": s["id"], "title": s["title"], "entries": []}
                    doc_sections.append(tgt)
                tgt["entries"].append({**_hdr(e), "bullets": [b["text"] for b in e["bullets"]]})
    # Keep sections in base order (Experience before Projects, etc.).
    order = [s["id"] for s in r["sections"]]
    doc_sections.sort(key=lambda s: order.index(s["id"]))

    skills = []
    for so in out.get("skills", []):
        items = []
        for it in so.get("items", []):
            canon = skills_ok.get(it.lower().strip())
            if canon is None:
                warnings.append(f"Dropped skill not on your resume: {it}")
            elif canon not in items:
                items.append(canon)
        if items:
            skills.append({"category": so.get("category") or "Skills", "items": items})
    if not skills:
        skills = [{"category": k, "items": v} for k, v in (r.get("skills") or {}).items()]

    base_cw = {c.lower(): c for e in r.get("education", []) for c in e.get("coursework") or []}
    cw = [base_cw[c.lower()] for c in out.get("coursework") or [] if c.lower() in base_cw]
    education = [_edu(e, cw or e.get("coursework")) for e in r.get("education", [])]

    summary = (out.get("summary") or "").strip()
    if summary:
        all_src = json.dumps(r)
        new_nums = set(re.findall(r"\d+(?:\.\d+)?", summary)) - set(re.findall(r"\d+(?:\.\d+)?", all_src))
        new_terms = vocab.terms_in(summary, user_terms, strict=True) - vocab.terms_in(all_src, user_terms)
        if new_nums or new_terms:
            warnings.append(f"Removed summary that introduced {sorted(new_nums | new_terms)}: “{summary}”")
            summary = r.get("summary") or ""
    if not (r.get("tailoring") or {}).get("allow_summary", True):
        summary = r.get("summary") or ""

    doc = {"name": r["name"], "contact": r["contact"], "summary": summary, "education": education,
           "sections": doc_sections, "skills": skills,
           "skills_first": bool((r.get("layout") or {}).get("skills_first"))}
    return doc, warnings, rewrites


# ---------------------------------------------------------------------------------------
def _coverage(jd: str, text: str, user_terms: set[str]) -> tuple[set[str], set[str]]:
    jd_terms = vocab.terms_in(jd, user_terms, strict=True)
    have = vocab.terms_in(text, user_terms) & jd_terms
    return jd_terms, have


def _structure_diff(before: dict, after: dict) -> list[str]:
    lines = []
    b_ids = {s["id"]: [e["heading"] for e in s["entries"]] for s in before["sections"]}
    for s in after["sections"]:
        a = [e["heading"] for e in s["entries"]]
        b = b_ids.get(s["id"], [])
        dropped = [x for x in b if x not in a]
        if a != [x for x in b if x in a]:
            lines.append(f"**{s['title']}** reordered → {', '.join(a)}")
        if dropped:
            lines.append(f"**{s['title']}** left out: {', '.join(dropped)}")
    for sid, b in b_ids.items():
        if sid not in {s['id'] for s in after['sections']}:
            lines.append(f"Section **{sid}** left out entirely")
    bs = [i for s in before["skills"] for i in s["items"]]
    as_ = [i for s in after["skills"] for i in s["items"]]
    if as_ != bs:
        moved = [x for x in as_[:6] if x in bs and bs.index(x) >= 6]
        dropped = [x for x in bs if x not in as_]
        added = [x for x in as_ if x not in bs]
        parts = []
        if moved:
            parts.append(f"moved up {', '.join(moved)}")
        if added:
            parts.append(f"added (from extra_skills) {', '.join(added)}")
        if dropped:
            parts.append(f"hid {', '.join(dropped)}")
        lines.append("**Skills** " + ("; ".join(parts) if parts else "reordered"))
    return lines


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:50]


def tailor(job: Job, jd: str, resume_path: str | Path, out_dir: str | Path, model: str | None = None) -> Result:
    r = load_resume(resume_path)
    tcfg = r.get("tailoring") or {}
    model = model or os.environ.get("INTERNWATCH_MODEL") or tcfg.get("model") or DEFAULT_MODEL
    out = _call_claude(r, job, jd, model, tcfg.get("bullet_budget", 14))
    doc, warnings, rewrites = validate(r, out)
    pdf, doc, cut_notes = fit_and_render(doc, tcfg.get("max_pages", 1), set(tcfg.get("pinned", [])))

    before = base_doc(r)
    user_terms = set(_all_skills(r))
    jd_terms, have_before = _coverage(jd, resume_plaintext(before), user_terms)
    _, have_after = _coverage(jd, resume_plaintext(doc), user_terms)
    missing = sorted(jd_terms - have_after)

    fit = out.get("fit", {})
    md = [f"# {job.company} — {job.title}", f"Apply: {job.url}", "",
          f"## Fit: {fit.get('score', '?')}/10 — {fit.get('one_liner', '')}"]
    if fit.get("strengths"):
        md += ["**Strengths**"] + [f"- {s}" for s in fit["strengths"]]
    if fit.get("gaps"):
        md += ["**Gaps** (what they ask for that your resume doesn't show)"] + [f"- {g}" for g in fit["gaps"]]
    if fit.get("interview_prep"):
        md += ["**Prep before an interview**"] + [f"- {g}" for g in fit["interview_prep"]]
    md += ["", "## What changed and why"] + [f"- **{c['what']}** — {c['why']}" for c in out.get("changes", [])]
    struct = _structure_diff(before, doc)
    if struct:
        md += ["", "### Structure"] + [f"- {x}" for x in struct]
    if rewrites:
        md += ["", "### Bullet rewrites"]
        for rw in rewrites:
            md += [f"- *{rw['entry']}*", f"  - before: ~~{rw['before']}~~", f"  - after: {rw['after']}"]
    if (doc.get("summary") or "") != (before.get("summary") or ""):
        md += ["", "### Summary line", f"- {doc.get('summary') or '(removed)'}"]
    if jd_terms:
        md += ["", f"## JD keyword coverage: {len(have_before)}/{len(jd_terms)} → {len(have_after)}/{len(jd_terms)}",
               f"- On your resume: {', '.join(sorted(have_after)) or '—'}"]
        if missing:
            md += [f"- In the posting, not on your resume: {', '.join(missing)}",
                   "  - Only add these if you've really used them — then put them in resume.yaml so future tailoring can use them."]
    if not jd or len(jd) < 300:
        warnings.insert(0, "Couldn't retrieve the full job description; tailoring was based on the title only.")
    if warnings or cut_notes:
        md += ["", "## Warnings"] + [f"- {w}" for w in warnings + cut_notes]
    summary_text = "\n".join(md)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(r['name'])}_{_slug(job.company)}_{_slug(job.title)}"
    pdf_path = out_dir / f"{stem}.pdf"
    pdf_path.write_bytes(pdf)
    md_path = out_dir / f"{stem}_changes.md"
    md_path.write_text(summary_text)

    push = [f"Fit {fit.get('score', '?')}/10: {fit.get('one_liner', '')}"]
    push += [f"• {c['what']}" for c in out.get("changes", [])[:4]]
    if jd_terms:
        push.append(f"Keywords {len(have_before)}→{len(have_after)}/{len(jd_terms)}")
    if warnings:
        push.append(f"⚠ {len(warnings)} warning(s) — see email")
    return Result(pdf_path, md_path, int(fit.get("score") or 0), fit.get("one_liner", ""), push, summary_text, warnings)
