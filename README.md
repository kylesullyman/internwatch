# internwatch

Finds new internship postings within minutes of their going live and pings your phone. For strong matches it also attaches a resume tailored to that posting, plus a summary of what changed and why.

```
every 10 min (GitHub Actions)
  ├─ GitHub lists ── SimplifyJobs, vanshb03, speedyapply            (JSON / README tables)
  ├─ Company ATSs ── Greenhouse, Lever, Ashby, Workday boards       (public job APIs)
  │
  ├─ dedupe against everything seen before (URL + company/title/location)
  ├─ hard filters: internship title, season (Summer 2027), US, not PhD/MBA/sales…
  ├─ score: graphics/GPU/CUDA/game/ML/C++ keywords, NVIDIA/Epic/Riot…, OC/LA location
  │
  ├─ score ≥ 8 → fetch JD → Claude tailors resume.yaml → code checks for fabrication
  │              → one-page PDF + change summary → push + email with PDF attached
  ├─ score ≥ 5 → instant push (no resume)
  └─ the rest  → one low-priority digest per run
```

**Why poll the ATSs directly?** A GitHub list only shows a posting after someone (or Simplify's crawler) adds it, which can take hours. The Greenhouse, Lever, Ashby and Workday APIs are what the companies' own career pages read from, so a posting shows up there the moment it's published. Polling every 10 minutes (plus GitHub's scheduling delay) keeps you well inside the hour.

## Setup (≈15 min)

### 1. Create the repo
Push this folder to a new GitHub repo.

- **Public repo (recommended):** Actions minutes are free and unlimited. Your resume never enters git: it's stored in a secret, and tailored PDFs arrive by email or push only. Job titles appear in the run logs; nothing personal does.
- **Private repo:** 10-minute polling costs about 4,300 Actions minutes a month. That's over the free 2,000, and over the 3,000 you get with Pro or the GitHub Student Pack. Change the cron to every 20 minutes (`4,24,44 * * * *`). In exchange you get the PDFs saved as run artifacts, and you can commit `resume.yaml` directly.

### 2. Write your base resume
(Already done for you: `resume.yaml` was converted from `currentresume-Sep2026.docx`.)
Copy `resume.example.yaml` to `resume.yaml` and fill in your real resume. **Put in everything true, including more bullets than fit on one page.** The tailor chooses the relevant ones for each job, but it can't use anything that isn't in this file.

Check it renders:
```bash
pip install -r requirements-dev.txt
python -c "from internwatch.tailor import load_resume, base_doc; from internwatch.render import fit_and_render; \
open('base.pdf','wb').write(fit_and_render(base_doc(load_resume('resume.yaml')))[0])"
```

### 3. Set up notifications (any or all)
| Channel | What you get | Setup |
|---|---|---|
| **ntfy** (push) | Instant phone notification with the PDF attached and an Apply button | Install the ntfy app (iOS/Android) and subscribe to a long random topic, e.g. `kyle-iw-7f3a9c2e41`. The topic name works as the password, so make it unguessable. |
| **Email** | Full change summary, the PDF, and the .md changelog | Gmail: turn on 2FA → [App passwords](https://myaccount.google.com/apppasswords) → create one. `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`. |
| **Discord** | Message plus PDF in a channel | Channel settings → Integrations → Webhooks → copy the URL. |

### 4. Add repo secrets
Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com (needed for tailoring only) |
| `RESUME_YAML` | the **entire contents** of your `resume.yaml` |
| `NTFY_TOPIC` | your topic name |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `EMAIL_TO` | for email |
| `DISCORD_WEBHOOK_URL` | optional |

When you edit your resume, update the `RESUME_YAML` secret too.

### 5. Turn it on
Actions tab → enable workflows → **internwatch** → *Run workflow*. The first run records everything currently posted without alerting (otherwise you'd get thousands of pushes). From then on, you're only alerted about postings that are new.

To test notifications locally: `NTFY_TOPIC=... python -m internwatch test-notify`

## Using it

- **Tailor for any posting on demand**, e.g. one a friend sent you: Actions → internwatch → Run workflow → paste the URL (optionally add company and title). The tailored PDF arrives by push or email. Locally: `python -m internwatch tailor <url> --company X --title Y --print`
- **Run on your own machine** instead of Actions: `python -m internwatch loop --every 300` (loads env vars from your shell).
- **Preview what would alert** without sending anything: `python -m internwatch run --dry-run -v`
- **Check every source is reachable:** `python -m internwatch sources`

## Tuning (`config.yaml`)

- **`filters.terms`** + new-grad lists: set up for **Summer 2027 internships and 2027 new-grad roles**. Titles naming another season/year, "Immediate Start" roles, and Master's/PhD-only listings are dropped. Alerts are labeled "New internship" or "New new-grad role".
- **`scoring`** controls what counts as a strong match. Title keywords, company boosts and location boosts are all additive. Adjust `notify.instant_min_score` (default 5) and `tailoring.min_score` (default 8) until the number of pushes feels right. Running `run --dry-run -v` against a fresh state shows you the effect.
- **Adding a company:** find its job URL. `job-boards.greenhouse.io/<token>`, `jobs.lever.co/<token>` and `jobs.ashbyhq.com/<token>` give you the token directly. For Workday, a URL like `https://<host>/en-US/<site>/job/...` gives you the host and site. The first poll of a new company is silent.
- **`tailoring.max_per_run`** caps how many Claude calls one run can make if a batch of postings drops at once.

## How tailoring stays honest

Claude proposes the edits: which entries and bullets to use and in what order, reworded bullets, skill order, and an optional one-line summary. **Code then checks the result before anything is rendered:**

- Every output bullet must cite a bullet ID from your base resume, from the same entry.
- A rewritten bullet can't add a **number** or a **technology** that its entry never mentioned. If it does, the original wording is restored and a warning is logged. This means "Python controller" can't become "C++/CUDA controller", even though C++ is on your resume, because that project didn't use it.
- Skills come only from `skills` and `extra_skills`.
- Titles, organizations, dates and locations are always copied from your base resume, never from the model.
- Pinned entries are always included.

The check can't catch a subtle claim written in plain words, like adding "used by the whole lab". That's why the change summary shows the **before and after of every rewritten bullet**. Skim it before you submit; it takes 20 seconds.

Each change summary also includes:
- A fit score out of 10, with honest gaps (things the posting asks for that your resume doesn't show) and what to prep for an interview.
- JD keyword coverage before and after, plus the terms in the posting that aren't on your resume. Only add those to `resume.yaml` if you've actually used them.

## Limits worth knowing

- GitHub's `schedule` trigger is best-effort: runs can start 5–15 minutes late, and occasionally one is skipped. The concurrency group stops overlapping runs from colliding.
- Workday search results aren't sorted by date, so every poll pages through all results for the search term (NVIDIA is about 50 requests, which takes a few seconds).
- Some company career sites render with JavaScript only, so their job description can't be fetched. In that case tailoring works from the title alone and says so in the warnings.
- Seen-job state is kept in the Actions cache, not in git. If the cache is ever evicted, the next run re-seeds silently: you might miss what was posted in that window, but you won't be spammed.

## Layout
```
internwatch/
  sources/github_lists.py   SimplifyJobs-style JSON + markdown-table READMEs
  sources/ats.py            Greenhouse, Lever, Ashby, Workday
  filters.py                hard filters + scoring
  state.py                  seen-job store
  jd.py                     job description fetcher (per-ATS APIs, HTML fallback)
  tailor.py                 Claude call + fabrication checks + change summary
  render.py, templates/     Typst → one-page PDF (auto-tightens, then trims)
  notify.py                 ntfy / email / Discord
config.yaml                 sources, filters, scoring, tailoring knobs
resume.example.yaml         schema for your resume.yaml
.github/workflows/watch.yml the scheduler
```
