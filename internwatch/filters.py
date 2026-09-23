from __future__ import annotations

import re
import time

from .models import Job

_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
           "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_STATE_NAMES = ("alabama alaska arizona arkansas california colorado connecticut delaware florida georgia "
                "hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts "
                "michigan minnesota mississippi missouri montana nebraska nevada ohio oklahoma oregon "
                "pennsylvania tennessee texas utah vermont virginia washington wisconsin wyoming").split() + [
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
    "rhode island", "south carolina", "south dakota", "west virginia"]
_US_RE = re.compile(
    r",\s*(" + "|".join(_STATES) + r")\b|^\s*us\b|\busa\b|united states|\bu\.s\.|\b(" + "|".join(_STATE_NAMES) + r")\b",
    re.I)
_FOREIGN_RE = re.compile(
    r"canada|ontario|british columbia|quebec|united kingdom|\buk\b|england|london|ireland|dublin|india|"
    r"bangalore|bengaluru|hyderabad|china|shanghai|beijing|shenzhen|taiwan|taipei|japan|tokyo|korea|seoul|"
    r"singapore|germany|berlin|munich|france|paris|netherlands|amsterdam|poland|warsaw|israel|tel aviv|"
    r"switzerland|zurich|sweden|spain|madrid|barcelona|italy|mexico|brazil|australia|sydney|vietnam|"
    r"philippines|romania|czech|hungary|portugal|denmark|finland|norway|belgium|austria|greece|turkey|"
    r"argentina|colombia|chile|uae|dubai|saudi|egypt|nigeria|kenya|south africa|malaysia|indonesia|"
    r"thailand|hong kong|new zealand|serbia|ukraine|costa rica|armenia|estonia|lithuania|latvia", re.I)
_SEASON_YEAR = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*[-/]?\s*'?(20\d\d|\d\d)\b", re.I)
_YEAR = re.compile(r"\b(20[2-3]\d)\b")
_INTERN = re.compile(r"(?<![a-z])(intern|internship|co-?op)(?![a-z])", re.I)
_NEWGRAD = re.compile(r"new\s*grad|university\s*grad|college\s*grad|early[\s-]*career|entry[\s-]*level|"
                      r"graduate\s*(program|engineer|software)|rotational", re.I)


def _kw_regex(word: str) -> re.Pattern:
    # \b fails around symbols like c++ / c#, so use look-arounds instead.
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9+#])", re.I)


def is_us(loc: str) -> bool | None:
    """True / False, or None when the string doesn't say ("3 Locations", "")."""
    if not loc or re.fullmatch(r"\s*\d+\s+locations?\s*", loc, re.I):
        return None
    if _US_RE.search(loc):
        return True
    if _FOREIGN_RE.search(loc):
        return False
    if re.search(r"remote|anywhere|hybrid", loc, re.I):
        return True
    return None


class Filter:
    def __init__(self, cfg: dict):
        f = cfg.get("filters", {})
        self.title_include = [_kw_regex(w) for w in f.get("title_include", ["intern", "internship", "co-op", "coop"])]
        self.title_exclude = [_kw_regex(w) for w in f.get("title_exclude", [])]
        self.terms = [t.lower() for t in f.get("terms", [])]
        self.term_years = {y for t in self.terms for y in _YEAR.findall(t)}
        self.categories = {c.lower() for c in f.get("categories", [])}
        self.us_only = f.get("us_only", True)
        self.max_age_h = f.get("max_age_hours", 72)
        self.exclude_companies = {c.lower() for c in f.get("exclude_companies", [])}
        self.degrees = {d.lower() for d in f.get("degrees", [])}
        s = cfg.get("scoring", {})
        self.kw = [(w, _kw_regex(w), float(v)) for w, v in s.get("title_keywords", {}).items()]
        self.company_boost = {k.lower(): float(v) for k, v in s.get("company_boost", {}).items()}
        self.loc_boost = [(k, _kw_regex(k), float(v)) for k, v in s.get("location_boost", {}).items()]

    # ---- hard filters -------------------------------------------------------------
    def reject_reason(self, job: Job, now: float | None = None) -> str | None:
        now = now or time.time()
        title = job.title
        from_list = job.source.split(":")[0] not in {"greenhouse", "lever", "ashby", "workday"}
        if job.company.lower() in self.exclude_companies:
            return "excluded company"
        # Lists are already curated internship/new-grad lists; ATS boards list every job.
        if not from_list and not any(r.search(title) for r in self.title_include):
            return "not an internship/new-grad title"
        if any(r.search(title) for r in self.title_exclude):
            return "title excluded"
        degs = {d.lower() for d in (job.extra.get("degrees") or [])}
        if self.degrees and degs and not degs & self.degrees:
            return f"degree {sorted(degs)}"
        if self.categories and job.category and job.category.lower() not in self.categories:
            return f"category {job.category}"
        if self.terms:
            terms = [t for t in job.terms if t.strip().upper() not in ("N/A", "")]
            if terms and not any(t.lower() in self.terms for t in terms):
                return f"terms {terms}"
            sy = _SEASON_YEAR.findall(title)
            if sy:
                named = {f"{'fall' if s.lower() == 'autumn' else s.lower()} {y if len(y) == 4 else '20' + y}"
                         for s, y in sy}
                if not named & set(self.terms):
                    return f"season in title {sorted(named)}"
            elif not terms and self.term_years:
                years = set(_YEAR.findall(title))
                if years and not years & self.term_years:
                    return f"year in title {years}"
        if self.us_only and job.locations:
            verdicts = [is_us(l) for l in job.locations]
            if verdicts and all(v is False for v in verdicts):
                return "not in US"
        if self.max_age_h and job.posted_at and now - job.posted_at > self.max_age_h * 3600:
            return "too old"
        return None

    # ---- ranking ------------------------------------------------------------------
    def score(self, job: Job) -> Job:
        s, why = 0.0, []
        text = job.title
        for w, r, v in self.kw:
            if r.search(text):
                s += v
                why.append(f"{w} {v:+g}")
        for co, v in self.company_boost.items():
            if co == job.company.lower() or _kw_regex(co).search(job.company):
                s += v
                why.append(f"{job.company} {v:+g}")
                break
        locs = " | ".join(job.locations)
        for name, r, v in self.loc_boost:
            if r.search(locs):
                s += v
                why.append(f"{name} {v:+g}")
                break
        job.score, job.score_reasons = s, why
        job.extra["kind"] = kind_of(job)
        return job


def kind_of(job: Job) -> str:
    if _INTERN.search(job.title):
        return "internship"
    if "newgrad" in job.source.replace("-", "").replace("_", "") or _NEWGRAD.search(job.title):
        return "new grad"
    return "internship" if job.terms else "role"
