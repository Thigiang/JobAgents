# Job Agent

Finds jobs on company Greenhouse/Lever/Ashby/Workday boards (or takes jobs you found yourself), scores how well each one fits your resume using Claude, and keeps a review queue in SQLite. For jobs you pick, it tailors your resume truthfully, builds the PDF, and opens the application form with the basics filled in. You review it and click Submit yourself.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # or run `ant auth login`
```

## Files you edit

| File | What it is |
|---|---|
| `master_resume.md` | Your resume content: the source of truth for every fact about you. |
| `ThiGiang_Resume_Master.tex` | Your resume layout (LaTeX). Tailored PDFs are built from it. Keep its content in sync with `master_resume.md`. |
| `config.toml` | Job boards to watch, title/location filters, free requirement checks (max years, PhD), your preferences, scoring settings. |
| `prompts/job_matching_master_prompt.md` | Instructions for scoring a job against your resume. |
| `prompts/resume_tailoring_master_prompt.md` | Instructions for tailoring your resume to a job. |
| `prompts/resume_auditor_prompt.md` | Instructions for the independent reviewer that checks every tailoring change. |
| `prompts/resume_tailoring_config.md` | Stable tailoring preferences: page limit, format, style, locations. |
| `verified_supplemental_facts.md` | Confirmed facts about you that aren't in the resume yet (skills, tools, answers to tailoring questions). Scoring counts `confirmed` facts as evidence; tailoring may add them to the resume. |

## Usage

```bash
.venv/bin/python main.py fetch            # get new postings from all boards (free, ~7 min)
.venv/bin/python main.py fetch --days 3   # only postings from the last 3 days (also works with run)
.venv/bin/python main.py add [<link>]     # add a job you found yourself (free)
.venv/bin/python main.py score [--limit N] # quick-score queued jobs (~5 cents each)
.venv/bin/python main.py run --limit 10   # fetch + score in one step
.venv/bin/python main.py list [--min N]   # scored jobs at or above the score threshold
.venv/bin/python main.py show <id>        # score breakdown, strengths, gaps, deal-breakers
.venv/bin/python main.py approve <id>     # mark a job to pursue
.venv/bin/python main.py skip <id>
.venv/bin/python main.py analyze [<id>]   # full analysis of approved jobs (~15 cents each)
.venv/bin/python main.py tailor <id>      # tailored resume -> tailored/<name>/ (~25-40 cents each)
.venv/bin/python main.py tailor <id> --anyway   # apply despite a missing requirement (e.g. years); stays truthful
.venv/bin/python main.py apply <id>       # open the form, fill basics + attach resume; YOU submit (free)
.venv/bin/python main.py applied <id>     # mark a job you applied to outside the tool
.venv/bin/python main.py status           # counts per stage, jobs ready to apply, jobs applied
```

Fetching is free and deduplicated. It also reads each posting and skips, for free, jobs that require more years than `max_years_required` or a PhD with no alternative; it prints what it skipped and why, and marks already-queued jobs that no longer pass as `filtered`. Only `score`/`run`, `analyze` and `tailor` call the Claude API and cost money; everything else is free. Use `--limit` with `score` to try a few jobs first.

### Adding jobs you found yourself (LinkedIn, Indeed, ...)

`add` puts one job in the queue, skipping the finding step. Give it a link and it reads the posting for free: Greenhouse, Lever, Ashby and Workday links through their APIs, and most other company career pages through the job data they embed. LinkedIn, Indeed, Glassdoor and ZipRecruiter forbid automated reading, so for those it asks you to paste the description (end with a line `END`), company, title and location; `--desc-file job.txt` reads the description from a file instead. If a LinkedIn or Indeed posting has an "Apply on company website" button, add that link instead: the tool can then read the posting itself, and it is the page you'll apply on.

Jobs you add skip the title/location filters and are never filtered out by `fetch`; requirement warnings (e.g. 3+ years) are shown but don't block them. Then run `score` or `analyze <id>` and `tailor <id>` as usual.

## How applying works

One-time setup: `.venv/bin/pip install -r requirements.txt` and `.venv/bin/python -m playwright install chromium`, then check your details in `config.toml` under `[applicant]` and fill in `[applicant.answers]` (work authorization, sponsorship, start date). Those answers are shown to you while you fill the form; the tool doesn't select them.

`apply <id>` checks the posting is still open, then opens the application page in a visible browser and fills the empty standard fields (name, email, phone, location, LinkedIn, GitHub, website), and attaches the tailored PDF (or `--resume file.pdf`). It doesn't touch dropdowns, yes/no or custom questions, demographic questions, or fields that already have text. You check everything, finish the form, and click Submit yourself; the tool never submits. Back in the terminal, answer `y` to mark the job as applied, which stops you applying twice.

Greenhouse, Lever and Ashby forms are filled. Workday needs an account sign-in, so the tool only opens the page; the browser keeps its own profile in `browser-profile/`, so you stay signed in next time. For LinkedIn or Indeed jobs, the tool opens the link and you apply there; automating LinkedIn's Easy Apply is against its terms.

## How tailoring works

`tailor <id>` needs the job's full analysis (`analyze` first). Claude returns a list of edits to numbered lines of
`ThiGiang_Resume_Master.tex` (summary, skills rows, bullets, whole projects); it never writes LaTeX, so the layout
stays exactly as designed. Before anything is saved:

1. Free automated checks: each edit must quote its line correctly, and new wording can't add a number, tool or
   duration that isn't in your resume or a confirmed fact in `verified_supplemental_facts.md`, or anything listed
   there as "Confirmed not held". The resume is compiled to check the page limit.
2. Problems go back to Claude for revision (up to `max_revisions` rounds); edits that still fail are dropped.
3. A separate auditor reviews the changed lines. Serious findings are reworded or the edit is reverted.

Output goes to `tailored/ThiGiang_Resume_<Industry>_<Company>_<Role>/`: the `.tex`, the PDF, and `changes.md`
(every change with its reason, the audit, remaining gaps, and questions for you). Your master files are never
changed. Answer the questions by adding confirmed facts to `verified_supplemental_facts.md`.
