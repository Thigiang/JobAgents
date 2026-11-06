"""Job agent CLI.

    python main.py fetch [--days N]     # pull new postings from configured boards (only last N days)
    python main.py add [<url>]          # add a job you found yourself (link, or paste the description)
    python main.py score [--limit N]    # quick SCREEN of unscored postings (cheap)
    python main.py run [--limit N]      # fetch + score
    python main.py list [--min N]       # show top-scoring jobs awaiting review
    python main.py show <id>            # screening and full-analysis details for one job
    python main.py approve <id>...      # mark jobs to pursue
    python main.py skip <id>...         # dismiss jobs
    python main.py analyze [<id>...]    # FULL analysis of approved jobs (or the given ids)
    python main.py tailor <id>...       # tailored resume (.tex, PDF, change report) for analyzed jobs
    python main.py tailor <id> --anyway # ...even when a required qualification is missing
    python main.py apply <id>           # open the application form, fill basics + resume; YOU submit
    python main.py applied <id>...      # mark jobs you applied to outside the tool
    python main.py status               # pipeline overview: counts, ready to apply, applied
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

import anthropic

from jobagent import db
from jobagent.apply import application_url, open_and_fill
from jobagent.scorer import Scorer
from jobagent.tailor import Inputs, Refused, tailor_job
from jobagent.sources import (FETCHERS, NO_FETCH_DOMAINS, Job, PostingUnavailable, fetch_posting, manual_id,
                              matches_filters, posted_within, requirement_blockers)

CONFIG_PATH = Path("config.toml")
PERSONAL_PATH = Path("personal.toml")  # applicant details; not in Git


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    if PERSONAL_PATH.exists():
        with PERSONAL_PATH.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def make_scorer(cfg: dict) -> Scorer:
    profile, scoring = cfg["profile"], cfg["scoring"]
    resume_path = Path(profile["resume_path"])
    resume = resume_path.read_text()
    if "REPLACE THIS" in resume:
        sys.exit(f"Fill in {resume_path} with your resume before scoring.")
    facts_path = Path(profile.get("facts_path", "verified_supplemental_facts.md"))
    facts = facts_path.read_text() if facts_path.exists() else ""
    prompt = Path(scoring["prompt_path"]).read_text()
    return Scorer(prompt, resume, facts, profile.get("preferences", ""), scoring["model"], scoring["effort"])


def cmd_fetch(cfg: dict, max_days: int | None = None):
    search = cfg.get("search", {})
    if max_days is not None:
        print(f"Only postings from the last {max_days} day(s); jobs whose board gives no date are kept.")
    with db.connect() as conn:
        for board in cfg.get("boards", []):
            source, slug = board["source"], board["slug"]
            try:
                jobs = FETCHERS[source](slug, search, max_days)
            except Exception as e:
                print(f"  ! {source}/{slug}: {e}")
                continue
            if max_days is not None:
                jobs = [j for j in jobs if posted_within(j, max_days)]
            kept, blocked = [], []
            for j in filter(lambda j: matches_filters(j, search), jobs):
                reasons = requirement_blockers(j.description, search)
                (blocked if reasons else kept).append((j, reasons))
            new = sum(db.insert_job(conn, j) for j, _ in kept)
            print(f"  {source}/{slug}: {len(jobs)} postings, {len(kept) + len(blocked)} match filters, "
                  f"{len(blocked)} out of reach, {new} new")
            for j, reasons in blocked:
                print(f"      - {j.title}: {', '.join(reasons)}")

        # Filters may have been tightened since older jobs were fetched; don't pay to score those.
        for row in db.jobs_with_status(conn, "new"):
            if row["source"] == "manual":  # added by hand with `add`
                continue
            job = Job(row["id"], row["source"], row["company"], row["title"], row["location"],
                      row["url"], row["description"])
            reasons = [] if matches_filters(job, search) else ["no longer matches title/location filters"]
            reasons += requirement_blockers(job.description, search)
            if reasons:
                db.set_status(conn, job.id, "filtered")
                print(f"  queued job filtered out: {job.company} - {job.title}: {', '.join(reasons)}")


def _ask(label: str, default: str = "") -> str:
    answer = input(f"  {label}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def _paste_description() -> str:
    print("  Paste the job description, then type END on its own line and press Enter:")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def cmd_add(cfg: dict, url: str | None, desc_file: str | None):
    """Add one job found by hand (e.g. on LinkedIn or Indeed). Job-board and most company career links
    are read automatically; otherwise the description is pasted in. Title/location filters are not
    applied, since you chose the job; requirement warnings are shown but don't block it."""
    url = (url or _ask("Job link (the company's apply page is best; leave empty if none)")).strip()
    job = None
    if url and not desc_file:
        try:
            job = fetch_posting(url)
            print(f"  Read automatically: {job.company} - {job.title} ({job.location or 'location not given'})")
        except PostingUnavailable as e:
            print(f"  Can't read this link automatically: {e}.")
            if "linkedin.com" in url or "indeed.com" in url:
                print("  Tip: if the posting has an \"Apply on company website\" button, adding that link instead\n"
                      "  lets the tool read the posting itself and later open the right application page.")
    if job is None:
        description = Path(desc_file).read_text() if desc_file else _paste_description()
        if len(description) < 200:
            sys.exit("  The description looks too short; nothing added.")
        job = Job(manual_id(url or description[:500]), "manual", _ask("Company"), _ask("Job title"),
                  _ask("Location (e.g. San Francisco, CA or Remote)"), url, description)
    job.source = "manual"  # marks it as hand-picked, so fetch never filters it out (the id stays the board's)
    with db.connect() as conn:
        if not db.insert_job(conn, job):
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job.id,)).fetchone()
            print(f"  Already in the database (status: {row['status']}): {job.id}")
            return
    print(f"  Added: {job.id}")
    if not matches_filters(job, cfg.get("search", {})):
        print("  Note: this job is outside your title/location filters (kept, since you added it).")
    for reason in requirement_blockers(job.description, cfg.get("search", {})):
        print(f"  Note: the posting {reason} (kept anyway).")
    print(f"  Next: python main.py score          (quick screen, ~5 cents a job)\n"
          f"     or python main.py analyze {job.id}   (full analysis, ~15 cents)")


def _call(fn, row):
    """Run one Claude call. Returns (result, stop) where stop=True means abort the batch."""
    try:
        return fn(row["title"], row["company"], row["location"], row["url"], row["description"]), False
    except anthropic.RateLimitError:
        print("  ! Rate limited; stopping. Re-run later to continue.")
        return None, True
    except anthropic.APIStatusError as e:
        print(f"  ! {row['id']}: API error {e.status_code}: {e.message}")
        return None, False
    except anthropic.APIConnectionError as e:
        print(f"  ! Connection error: {e}; stopping.")
        return None, True


def _report(result: dict, row):
    print(f"  {result['overall_score']:>3}  {result['recommendation']:<20} {row['company']} - {row['title']}")
    for w in result["score_check_warnings"]:
        print(f"       ! {w}")


def cmd_score(cfg: dict, limit: int | None):
    scorer = make_scorer(cfg)
    with db.connect() as conn:
        pending = db.jobs_with_status(conn, "new", limit)
        print(f"Screening {len(pending)} job(s) with {cfg['scoring']['model']}...")
        for row in pending:
            result, stop = _call(scorer.screen, row)
            if stop:
                break
            if result is None:
                continue
            db.save_score(conn, row["id"], result["overall_score"], result)
            conn.commit()  # keep progress if interrupted
            _report(result, row)


def cmd_analyze(cfg: dict, ids: list[str]):
    scorer = make_scorer(cfg)
    with db.connect() as conn:
        if ids:
            rows = [r for i in ids if (r := conn.execute("SELECT * FROM jobs WHERE id = ?", (i,)).fetchone())]
            if len(rows) != len(ids):
                print("  ! Some ids were not found")
        else:
            rows = db.approved_without_analysis(conn)
        print(f"Running full analysis on {len(rows)} job(s) with {cfg['scoring']['model']}...")
        for row in rows:
            result, stop = _call(scorer.full, row)
            if stop:
                break
            if result is None:
                continue
            db.save_analysis(conn, row["id"], result["overall_score"], result)
            conn.commit()
            _report(result, row)


def cmd_tailor(cfg: dict, ids: list[str], anyway: bool = False):
    profile, t = cfg["profile"], cfg["tailoring"]
    facts_path = Path(profile.get("facts_path", "verified_supplemental_facts.md"))
    inp = Inputs(
        tex=Path(profile["resume_tex_path"]).read_text(),
        resume_md=Path(profile["resume_path"]).read_text(),
        facts=facts_path.read_text() if facts_path.exists() else "",
        tailor_prompt=Path(t["prompt_path"]).read_text(),
        tailor_config=Path(t["config_path"]).read_text(),
        audit_prompt=Path(t["audit_prompt_path"]).read_text(),
        page_limit=t["page_limit"], model=t["model"], effort=t["effort"], audit_model=t["audit_model"],
        max_revisions=t["max_revisions"], file_prefix=t["file_prefix"], out_root=Path(t["output_dir"]),
    )
    with db.connect() as conn:
        for job_id in ids:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                print(f"  ! No job with id {job_id}")
                continue
            if not row["analysis"]:
                print(f"  ! {row['company']} - {row['title']}: run `analyze {job_id}` first (tailoring uses the full analysis)")
                continue
            print(f"Tailoring for {row['company']} - {row['title']} with {t['model']}...")
            try:
                report = tailor_job(row, json.loads(row["analysis"]), inp, anyway=anyway)
            except Refused as e:
                print(f"  ! {e}")
                continue
            except anthropic.RateLimitError:
                print("  ! Rate limited; stopping. Re-run later to continue.")
                break
            except anthropic.APIStatusError as e:
                print(f"  ! API error {e.status_code}: {e.message}")
                continue
            except anthropic.APIConnectionError as e:
                print(f"  ! Connection error: {e}; stopping.")
                break
            db.save_tailoring(conn, job_id, report)
            conn.commit()
            cost = f"${report['cost']:.2f}" if report.get("cost") is not None else "unknown"
            if report["decision"] == "skip":
                print(f"  Skipped: {report['result']['decision_reason']}  (cost {cost})")
            else:
                print(f"  {len(report['applied_edits'])} edits applied, {report['pages']} page(s), cost {cost}")
                print(f"  Saved to {report['output_dir']}/ (see changes.md)")
                if report["result"]["questions"]:
                    print(f"  {len(report['result']['questions'])} question(s) for you in changes.md")


def cmd_apply(cfg: dict, job_id: str, resume: str | None):
    applicant = cfg.get("applicant", {})
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            sys.exit(f"  ! No job with id {job_id}")
        if row["status"] == "applied":
            sys.exit(f"  Already applied on {row['applied_at']}: {row['company']} - {row['title']}")
        report = json.loads(row["tailoring"]) if row["tailoring"] else {}
        pdf = Path(resume) if resume else Path(report["pdf"]) if report.get("pdf") else None
        if pdf is None:
            sys.exit(f"  ! No tailored resume yet. Run `tailor {job_id}`, or pass --resume <file.pdf>.")
        if not pdf.exists():
            sys.exit(f"  ! Resume file not found: {pdf}")
        if not row["url"]:
            sys.exit("  ! This job has no link to apply on.")
        # A posting that closed since it was fetched would waste the effort.
        if row["source"] != "manual" or not any(d in row["url"] for d in NO_FETCH_DOMAINS):
            try:
                fetch_posting(row["url"])
            except PostingUnavailable as e:
                # "no readable job data" is an ordinary page the tool can't parse, not a closed posting
                if "no readable job data" not in str(e):
                    print(f"  ! The posting may be closed: {e}.")
                    if input("  Open it anyway? [y/N] ").strip().lower() != "y":
                        return
        url = application_url(row)
        print(f"Opening {row['company']} - {row['title']}\n  {url}\n  Resume: {pdf}")
        if open_and_fill(url, applicant, pdf, Path(cfg.get("apply", {}).get("browser_profile", "browser-profile"))):
            db.mark_applied(conn, job_id)
            print("  Marked as applied.")
        else:
            print("  Not marked as applied; run `apply` again when you're ready.")


def cmd_status():
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        print("  " + ", ".join(f"{s}: {counts[s]}" for s in
                               ("new", "scored", "approved", "tailored", "applied", "skipped", "filtered") if s in counts))
        for status, heading in (("tailored", "Ready to apply"), ("applied", "Applied")):
            rows = conn.execute(f"SELECT * FROM jobs WHERE status = ? ORDER BY {'applied_at' if status == 'applied' else 'first_seen'}",
                                (status,)).fetchall()
            if rows:
                print(f"\n{heading}:")
            for r in rows:
                when = f" on {r['applied_at'][:10]}" if r["applied_at"] else ""
                print(f"  {r['score'] if r['score'] is not None else '-':>3}  {r['company']} - {r['title']}{when}\n       id: {r['id']}")


def cmd_list(cfg: dict, min_score: int | None):
    threshold = cfg["scoring"]["min_score"] if min_score is None else min_score
    with db.connect() as conn:
        rows = db.scored_jobs(conn, threshold)
    if not rows:
        print(f"No scored jobs at or above {threshold}.")
        return
    for r in rows:
        v = json.loads(r["verdict"])
        posted = f"  (posted {r['posted_at']})" if r["posted_at"] else ""
        print(f"{r['score']:>3}  {r['company']} - {r['title']}  [{r['location']}]{posted}")
        if "overall_score" in v:
            print(f"     {v['recommendation']} | requirements: {v['mandatory_requirement_status']} | confidence: {v['confidence']}")
            print(f"     {v['summary']}")
        else:  # scored with the first version of the scorer
            print(f"     {v['summary']}  (old scoring format)")
        print(f"     {r['url']}")
        print(f"     id: {r['id']}\n")


def cmd_show(job_id: str):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if r is None:
        sys.exit(f"No job with id {job_id}")
    print(f"{r['company']} - {r['title']}  [{r['location']}]\n{r['url']}\nstatus: {r['status']}")
    if r["analysis"]:
        print_result(json.loads(r["analysis"]))
    elif r["verdict"]:
        v = json.loads(r["verdict"])
        if "overall_score" not in v:
            print(f"\nScore: {v['score']} ({v['verdict']}) - old scoring format, re-score for the new report\n{v['summary']}")
            return
        print_result(v)
        print("\n(Screening only. Approve the job and run `analyze` for the full analysis.)")
    if r["tailoring"]:
        t = json.loads(r["tailoring"])
        print("\n== Tailoring ==")
        if t["decision"] == "skip":
            print(f"Skipped: {t['result']['decision_reason']}")
        else:
            print(f"{len(t['applied_edits'])} edits applied, {t['pages']} page(s). Files: {t['output_dir']}/")
            for q in t["result"]["questions"]:
                print(f"  Question: {q['question']}")


def _bullets(items: list[str], indent: str = "  "):
    for item in items:
        print(f"{indent}- {item}")


def print_result(v: dict):
    print(f"\n== Match summary ({v['mode']}) ==")
    print(f"Score: {v['overall_score']}/100  |  {v['recommendation']}  |  confidence: {v['confidence']}")
    print(f"Mandatory requirements: {v['mandatory_requirement_status']}")
    print(v["summary"])
    print("Top strengths:")
    _bullets(v["top_strengths"])
    print("Top gaps or uncertainties:")
    _bullets(v["top_gaps_or_uncertainties"])
    for w in v["score_check_warnings"]:
        print(f"! Score check: {w}")

    print("\n== Score breakdown ==")
    for c in v["score_breakdown"]:
        pts = f"{c['points_awarded']:>2}/{c['max_points']:<2}" if c["applicable"] else " n/a "
        print(f"{pts}  {c['category']}")
        print(f"       {c['explanation']}")

    if v["mandatory_issues"]:
        print("\n== Mandatory requirements / potential deal-breakers ==")
        for m in v["mandatory_issues"]:
            print(f"  [{m['status']}] {m['requirement']}: {m['explanation']}")

    if v["mode"] == "FULL":
        print("\n== Requirement-by-requirement ==")
        for req in v["requirement_comparison"]:
            print(f"  [{req['match_classification']}] ({req['importance']}) {req['jd_requirement']}")
            print(f"      evidence: {req['resume_evidence']}")
            print(f"      impact: {req['impact']}")

        rec = v["application_recommendation"]
        print("\n== Application recommendation ==")
        print(f"Worth the time? {rec['worth_the_time']}")
        print(f"Most relevant strengths: {rec['most_relevant_strengths']}")
        print(f"Disqualifying vs. learnable gaps: {rec['disqualifying_vs_learnable_gaps']}")
        print(f"What would change this: {rec['what_would_change_assessment']}")

        h = v["tailoring_handoff"]
        print("\n== For resume tailoring ==")
        print("Priorities:")
        _bullets(h["tailoring_priorities"])
        print("Claims not to make without verification:")
        _bullets(h["claims_not_to_make"])

    if v["limitations"]:
        print("\nLimitations:")
        _bullets(v["limitations"])


def main():
    parser = argparse.ArgumentParser(description="Find and score jobs.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--days", type=int, help="only postings from the last N days")
    p_add = sub.add_parser("add")
    p_add.add_argument("url", nargs="?")
    p_add.add_argument("--desc-file", help="text file with the job description (instead of pasting)")
    p_score = sub.add_parser("score")
    p_score.add_argument("--limit", type=int)
    p_run = sub.add_parser("run")
    p_run.add_argument("--limit", type=int)
    p_run.add_argument("--days", type=int, help="only postings from the last N days")
    p_list = sub.add_parser("list")
    p_list.add_argument("--min", type=int, dest="min_score")
    p_show = sub.add_parser("show")
    p_show.add_argument("id")
    for name in ("approve", "skip"):
        sub.add_parser(name).add_argument("ids", nargs="+")
    sub.add_parser("analyze").add_argument("ids", nargs="*")
    p_tailor = sub.add_parser("tailor")
    p_tailor.add_argument("ids", nargs="+")
    p_tailor.add_argument("--anyway", action="store_true", help="tailor even when a required qualification is missing")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("id")
    p_apply.add_argument("--resume", help="PDF to attach instead of the tailored resume")
    sub.add_parser("applied").add_argument("ids", nargs="+")
    sub.add_parser("status")
    args = parser.parse_args()

    cfg = load_config()
    if args.cmd in ("fetch", "run"):
        cmd_fetch(cfg, args.days)
    if args.cmd == "add":
        cmd_add(cfg, args.url, args.desc_file)
    if args.cmd in ("score", "run"):
        cmd_score(cfg, args.limit)
    if args.cmd == "list":
        cmd_list(cfg, args.min_score)
    if args.cmd == "show":
        cmd_show(args.id)
    if args.cmd == "approve":
        cmd_set_status(args.ids, "approved")
    if args.cmd == "skip":
        cmd_set_status(args.ids, "skipped")
    if args.cmd == "analyze":
        cmd_analyze(cfg, args.ids)
    if args.cmd == "tailor":
        cmd_tailor(cfg, args.ids, args.anyway)
    if args.cmd == "apply":
        cmd_apply(cfg, args.id, args.resume)
    if args.cmd == "applied":
        with db.connect() as conn:
            for job_id in args.ids:
                print(f"  {'marked applied' if db.mark_applied(conn, job_id) else 'not found'}: {job_id}")
    if args.cmd == "status":
        cmd_status()


def cmd_set_status(ids: list[str], status: str):
    with db.connect() as conn:
        for job_id in ids:
            ok = db.set_status(conn, job_id, status)
            print(f"  {'ok' if ok else 'not found'}: {job_id}")


if __name__ == "__main__":
    main()
