"""Free, automated checks on tailoring edits before anything is audited or saved.

Two kinds of checks:
- structure: the edit refers to a real line, quotes it correctly, and uses an operation that line allows;
- facts: new wording adds no number, tool or years-of-experience claim that isn't in the resume or
  in a confirmed supplemental fact, and no tool the candidate confirmed they don't have.
Wording that may overstate (e.g. "led", "production") is flagged as a warning for the auditor.
"""

import re

from .resume_tex import Unit, same_text

ALLOWED_OPS = {"summary": {"rewrite"}, "skill": {"rewrite"}, "bullet": {"rewrite", "remove", "move", "add"},
               "entry": {"remove"}}

# Tools and skills recruiters screen for. A term in new wording must already appear in the resume or a
# confirmed fact. Multi-word and symbol terms are matched literally; the list is easy to extend.
KNOWN_TERMS = [
    "python", "sql", "sqlite", "postgresql", "postgres", "mysql", "snowflake", "bigquery", "redshift",
    "databricks", "spark", "pyspark", "hadoop", "airflow", "dbt", "etl", "elt", "aws", "azure", "gcp",
    "google cloud", "docker", "kubernetes", "tableau", "power bi", "looker", "streamlit", "excel",
    "google sheets", "pandas", "numpy", "scipy", "scikit-learn", "sklearn", "tensorflow", "keras",
    "pytorch", "xgboost", "lightgbm", "hugging face", "langchain", "llm", "llms", "openai", "claude",
    "gemini", "chatgpt", "matlab", "sas", "spss", "stata", "julia", "java", "scala", "c++", "javascript",
    "git", "github", "latex", "jupyter", "mixpanel", "amplitude", "salesforce", "fhir", "hl7", "epic",
    "a/b test", "a/b testing", "causal inference", "nlp", "computer vision", "cnn", "lstm", "bilstm",
    "rnn", "transformer", "transformers", "gan", "reinforcement learning", "mlops", "mlflow", "fastapi",
    "flask", "react", "seaborn", "matplotlib", "ggplot2", "plotly", "shiny", "sagemaker", "vertex ai",
    "linux", "bash", "rest api", "survival analysis", "time-series", "time series", "bayesian",
]
# Words that can overstate scope or seniority; flagged (not blocked) when new.
INFLATION_WORDS = ["led", "lead", "managed", "owned", "spearheaded", "expert", "senior", "production",
                   "deployed", "stakeholders", "cross-functional", "end-to-end", "scalable", "enterprise",
                   "clients", "customers", "mentored", "architected"]
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_YEARS_CLAIM_RE = re.compile(r"\b\d+\+?\s*(?:years?|yrs?)\b", re.I)


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<![\w+]){re.escape(term)}(?![\w+])", text) is not None


def parse_facts(facts_md: str) -> tuple[str, list[str]]:
    """Returns (text of confirmed facts, list of 'confirmed not held' items)."""
    confirmed, not_held = [], []
    for block in re.split(r"\n(?=###? )", facts_md):
        if block.startswith("## Confirmed not held"):
            not_held = [re.sub(r"\s*\(confirmed[^)]*\).*", "", l[2:]).strip()
                        for l in block.splitlines() if l.startswith("- ")]
        elif block.startswith("### ") and re.search(r"confirmation status:\s*confirmed\s*$", block, re.M | re.I):
            confirmed += [l for l in block.splitlines() if l.startswith(("- Fact:", "- Context:"))]
    return "\n".join(confirmed), not_held


def check_structure(edits: list[dict], units: list[Unit]) -> dict[int, list[str]]:
    by_id = {u.id: u for u in units}
    problems: dict[int, list[str]] = {}
    claimed: dict[str, int] = {}
    removed_entries = {e["target_id"] for e in edits if e.get("op") == "remove" and e.get("target_id", "").startswith("P")}
    for i, e in enumerate(edits, 1):
        p = problems.setdefault(i, [])
        u = by_id.get(e.get("target_id", ""))
        if u is None:
            p.append(f"target_id {e.get('target_id')!r} is not an editable line")
            continue
        if e["op"] not in ALLOWED_OPS[u.kind]:
            p.append(f"'{e['op']}' is not allowed on {u.id} (allowed: {', '.join(sorted(ALLOWED_OPS[u.kind]))})")
        quoted = re.sub(rf"^{re.escape(u.label)}:\s*", "", e.get("target_text", "")) if u.kind == "skill" else e.get("target_text", "")
        if u.kind == "entry":  # project title, ignoring link labels like "(GitHub)" and the date
            core = lambda t: re.sub(r"\s*\([^)]*\)", "", t.split(" — ")[0])
            ok = same_text(core(quoted), core(u.text))
        else:
            ok = same_text(quoted, u.text)
        if not ok:
            p.append(f"target_text does not match line {u.id} exactly; the line reads: {u.text!r}")
        if e["op"] in ("rewrite", "add") and not (e.get("new_text") or "").strip():
            p.append("new_text is empty")
        if e["op"] != "add":
            if u.id in claimed:
                p.append(f"{u.id} is already changed by edit {claimed[u.id]}; use one edit per line")
            claimed.setdefault(u.id, i)
        if u.kind == "bullet" and any(x.id in removed_entries and x.start <= u.start < x.end for x in units):
            p.append(f"{u.id} belongs to a project that another edit removes")
        if e["op"] == "move":
            a = by_id.get(e.get("anchor_id", ""))
            if a is None or a.kind != "bullet" or a.block != u.block or a.id == u.id:
                p.append("move needs an anchor_id that is another bullet in the same list")
            if e.get("position") not in ("before", "after"):
                p.append("move needs position 'before' or 'after'")
    # A list must keep at least one bullet, or LaTeX fails to compile.
    for block in {u.block for u in units if u.kind == "bullet"}:
        ids = {u.id for u in units if u.kind == "bullet" and u.block == block}
        gone = {e["target_id"] for e in edits if e.get("op") in ("remove",) and e.get("target_id") in ids}
        added = any(e.get("op") == "add" and e.get("target_id") in ids for e in edits)
        if ids and gone == ids and not added:
            entry_removed = any(u.kind == "entry" and u.id in removed_entries and
                                any(b.start >= u.start and b.end <= u.end and b.id in ids for b in units) for u in units)
            if not entry_removed:
                last = max(i for i, e in enumerate(edits, 1) if e.get("target_id") in ids)
                problems[last].append("this removes every bullet in a list; remove the whole project instead (P line) or keep one")
    return {i: p for i, p in problems.items() if p}


def check_facts(edits: list[dict], units: list[Unit], resume_text: str, facts_md: str) -> tuple[dict, dict]:
    """Returns (problems, warnings) keyed by 1-based edit number."""
    confirmed, not_held = parse_facts(facts_md)
    corpus = (resume_text + "\n" + confirmed).lower()
    corpus_numbers = {n.replace(",", "") for n in _NUMBER_RE.findall(corpus)}
    by_id = {u.id: u for u in units}
    problems, warnings = {}, {}
    for i, e in enumerate(edits, 1):
        new = (e.get("new_text") or "")
        if e["op"] not in ("rewrite", "add") or not new:
            continue
        low = new.lower()
        original = by_id[e["target_id"]].text.lower() if e["target_id"] in by_id and e["op"] == "rewrite" else ""
        p, w = [], []
        for n in _NUMBER_RE.findall(new):
            if n.replace(",", "") not in corpus_numbers:
                p.append(f"number {n} does not appear in the resume or confirmed facts")
        for claim in _YEARS_CLAIM_RE.findall(new):
            if claim.lower() not in original:
                p.append(f"'{claim}' states a duration; durations must come from dated positions")
        for item in not_held:
            if _has(item.lower(), low) or any(_has(t, low) for t in re.split(r"\s+or\s+|/", item.lower()) if len(t) > 2 and t in KNOWN_TERMS):
                p.append(f"mentions {item}, which the candidate confirmed they do not have")
        for term in KNOWN_TERMS:
            if _has(term, low) and not _has(term, corpus):
                p.append(f"'{term}' is not in the resume or a confirmed fact")
        for word in INFLATION_WORDS:
            if _has(word, low) and not _has(word, original):
                w.append(f"new wording uses '{word}'; check it doesn't overstate scope or seniority")
        if p:
            problems[i] = p
        if w:
            warnings[i] = w
    return problems, warnings
