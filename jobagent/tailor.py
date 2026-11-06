"""Tailor the master resume to one job, using Claude, with automated checks and an independent audit.

Flow for one job (it must have a FULL analysis):
  1. Tailoring call: Claude returns a decision (tailor/skip) and a list of edits to numbered lines.
  2. Free checks: each edit must quote its line correctly, use an allowed operation, and add no
     number, tool or duration that isn't in the resume or a confirmed fact. The edits are applied
     to the .tex and compiled to count pages.
  3. If anything failed or the resume runs over the page limit, Claude gets WORKFLOW_FEEDBACK and
     revises (up to `max_revisions` times). Edits still failing after that are dropped.
  4. Audit call: a separate reviewer checks only the changed lines. CRITICAL/HIGH/MEDIUM findings
     are fixed with the auditor's wording (re-checked) or the edit is reverted.
  5. The tailored .tex, PDF and a change report are saved under tailored/.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import factcheck, resume_tex
from .sources import strip_boilerplate

# $ per million tokens: input, output, cache write (5 min), cache read. Used for the cost estimate only.
PRICES = {"claude-opus-5-5": (4.00, 20.00, 5.00, 0.20), "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20)}


# ---------------------------------------------------------------- what Claude returns

class Edit(BaseModel):
    op: Literal["rewrite", "remove", "move", "add"]
    target_id: str = Field(description="ID of the line, e.g. S1, K4, B7, P2. For add: the bullet to insert after.")
    target_text: str = Field(description="The text of that line copied verbatim from SOURCE_RESUME, without the [ID] and bullet dash.")
    new_text: Optional[str] = Field(None, description="rewrite/add only: plain text, no LaTeX. For skills lines, only the list after the label.")
    anchor_id: Optional[str] = Field(None, description="move only: the bullet to move before or after (same list).")
    position: Optional[Literal["before", "after"]] = None
    reason: str = Field(description="Which job requirement this serves.")
    evidence_source: str = Field(description="Resume line ID(s) or the supplemental fact that supports the wording.")
    meaning_changed: bool
    meaning_change_note: str = Field("", description="If meaning_changed, why the change is still supported.")


class Question(BaseModel):
    question: str
    missing_evidence: str
    why_it_matters: str
    resume_section: str
    acceptable_evidence: str


class TailoringResult(BaseModel):
    decision: Literal["tailor", "skip"]
    decision_reason: str
    strategy: list[str] = Field(description="3-6 bullets: which verified evidence was prioritized and why. Empty if skip.")
    edits: list[Edit]
    remaining_gaps: list[str]
    questions: list[Question] = Field(description="At most three.")
    limitations: list[str]
    industry: str = Field(description="One word for the file name, e.g. HealthTech, Biotech, MedTech.")
    company_short: str = Field(description="Short company name for the file name, e.g. Unlearn, Doximity.")


class AuditFinding(BaseModel):
    edit_number: int
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    issue: str
    recommended_text: str = Field(description="Corrected plain-text wording for the whole line, changing only what the finding requires; empty only when the edit must be reverted.")


class AuditResult(BaseModel):
    overall_verdict: str
    findings: list[AuditFinding]
    unverified_claims: list[str]
    readability_notes: list[str]
    sources_sufficient: bool
    limitations: list[str]


# ---------------------------------------------------------------- API calls

@dataclass
class Usage:
    model: str
    tokens: dict = field(default_factory=lambda: {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0})

    def add(self, u):
        self.tokens["in"] += u.input_tokens
        self.tokens["out"] += u.output_tokens
        self.tokens["cache_write"] += u.cache_creation_input_tokens or 0
        self.tokens["cache_read"] += u.cache_read_input_tokens or 0

    def cost(self) -> float | None:
        p = PRICES.get(self.model)
        if not p:
            return None
        t = self.tokens
        return (t["in"] * p[0] + t["out"] * p[1] + t["cache_write"] * p[2] + t["cache_read"] * p[3]) / 1e6


class Refused(Exception):
    pass


def _call(client, model, effort, system, messages, schema, usage: Usage):
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort},
        system=system,
        messages=messages,
        output_format=schema,
    ) as stream:
        response = stream.get_final_message()
    usage.add(response.usage)
    if response.stop_reason == "refusal":
        raise Refused("the model declined the request")
    return response


# ---------------------------------------------------------------- the pipeline

@dataclass
class Inputs:
    tex: str
    resume_md: str
    facts: str
    tailor_prompt: str
    tailor_config: str
    audit_prompt: str
    page_limit: int
    model: str
    effort: str
    audit_model: str
    max_revisions: int
    file_prefix: str
    out_root: Path


def _analysis_for_prompt(analysis: dict) -> str:
    keep = {k: v for k, v in analysis.items() if k not in ("rubric", "job_url", "mode", "score_check_warnings")}
    return json.dumps(keep, indent=1)


def _feedback(structure: dict, facts: dict, pages: int, page_limit: int, compile_error: str) -> str:
    lines = []
    for i in sorted(set(structure) | set(facts)):
        for msg in structure.get(i, []) + facts.get(i, []):
            lines.append(f"- Edit {i}: {msg}")
    if compile_error:
        lines.append(f"- The edited resume failed to compile: {compile_error}")
    elif pages > page_limit:
        lines.append(f"- The compiled resume is {pages} pages; the limit is {page_limit}. Shorten it using the cut order.")
    return "\n".join(lines)


def fit_pages(tex: str, units, edits: list[dict], page_limit: int) -> tuple[list[dict], list[str]]:
    """The audit can undo cuts that made room. If the result overflows, undo added lines, then the rewrites
    that grew the most, until it fits. Local compiles only, so free."""
    by_id = {u.id: u for u in units}
    growth = lambda e: len(e["new_text"]) - len(by_id[e["target_id"]].text)
    candidates = [e for e in reversed(edits) if e["op"] == "add"]
    candidates += sorted((e for e in edits if e["op"] == "rewrite" and growth(e) > 0), key=growth, reverse=True)
    kept = list(edits)
    fits = lambda es: (lambda r: not r[1] and r[0] <= page_limit)(resume_tex.page_count(resume_tex.apply_edits(tex, units, es)))
    undone = []
    for e in candidates:
        if fits(kept):
            break
        kept.remove(e)
        undone.append(e)
    # Undoing in order can overshoot; put back any undone edit that still fits.
    for e in reversed(undone[:-1]):
        trial = [x for x in edits if x in kept or x is e]
        if fits(trial):
            kept, undone = trial, [x for x in undone if x is not e]
    actions = [f"{e['target_id']} {'addition' if e['op'] == 'add' else 'rewrite'} undone to fit "
               f"{page_limit} page(s): {e['new_text']}" for e in undone]
    return kept, actions


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", text)).strip("-")


def tailor_job(row, analysis: dict, inp: Inputs, log=print, anyway: bool = False) -> dict:
    client = anthropic.Anthropic()
    usage = Usage(inp.model)
    audit_usage = Usage(inp.audit_model)
    units = resume_tex.parse(inp.tex)
    resume_text = resume_tex.render(inp.tex, units)

    # Stable across jobs, so cached: instructions, config, resume and facts.
    system = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": (
        f"{inp.tailor_prompt.strip()}\n\n<RESUME_TAILORING_CONFIG>\n{inp.tailor_config.strip()}\n</RESUME_TAILORING_CONFIG>\n\n"
        f"<SOURCE_RESUME>\n{resume_text}\n</SOURCE_RESUME>\n\n"
        f"<VERIFIED_SUPPLEMENTAL_FACTS>\n{inp.facts.strip()}\n</VERIFIED_SUPPLEMENTAL_FACTS>\n")}]
    job_block = (
        f"<JOB_DESCRIPTION>\nJob title: {row['title']}\nCompany: {row['company']}\nLocation: {row['location']}\n"
        f"Job posting URL: {row['url']}\n\n{strip_boilerplate(row['description'])}\n</JOB_DESCRIPTION>\n\n"
        f"<EXISTING_MATCH_ANALYSIS>\n{_analysis_for_prompt(analysis)}\n</EXISTING_MATCH_ANALYSIS>\n\n"
        f"Tailor the resume for this job.")
    # Measured locally (free) so Claude can plan length up front instead of overflowing and revising.
    spare = resume_tex.spare_lines(inp.tex, units, inp.page_limit)
    if spare == 0:
        job_block += (f"\n\nPage budget: the source resume already fills its {inp.page_limit}-page limit. "
                      "Any edit that makes the resume longer (an added bullet, or a rewrite that wraps onto "
                      "another line) must be offset by a cut elsewhere.")
    elif spare > 0:
        job_block += (f"\n\nPage budget: about {spare} more single line(s) fit within the "
                      f"{inp.page_limit}-page limit; beyond that, offset additions with cuts.")
    if anyway:  # the candidate chose to apply despite the gaps; tailoring still must not hide them
        job_block += ("\n\nThe candidate has decided to apply despite the gaps in the match analysis. Do not skip "
                      "because of a mandatory-requirement gap; tailor truthfully, and do not hide or misstate the gap.")
    messages = [{"role": "user", "content": job_block}]

    # 1-3: tailor, check, revise
    rounds = []
    for attempt in range(inp.max_revisions + 1):
        response = _call(client, inp.model, inp.effort, system, messages, TailoringResult, usage)
        result: TailoringResult = response.parsed_output
        if result.decision == "skip":
            log(f"  Claude recommends skipping: {result.decision_reason}")
            return {"decision": "skip", "result": result.model_dump(), "usage": usage.tokens,
                    "cost": usage.cost(), "rounds": rounds}
        edits = [e.model_dump() for e in result.edits]
        structure = factcheck.check_structure(edits, units)
        facts_problems, warnings = factcheck.check_facts(edits, units, resume_text, inp.facts)
        ok_edits = [e for i, e in enumerate(edits, 1) if i not in structure and i not in facts_problems]
        new_tex = resume_tex.apply_edits(inp.tex, units, ok_edits)
        pages, compile_error = resume_tex.page_count(new_tex)
        feedback = _feedback(structure, facts_problems, pages, inp.page_limit, compile_error)
        rounds.append({"edits": len(edits), "failed": len(structure) + len(facts_problems), "pages": pages,
                       "feedback": feedback})
        log(f"  round {attempt + 1}: {len(edits)} edits, {len(edits) - len(ok_edits)} failed checks, {pages} page(s)")
        if not feedback or attempt == inp.max_revisions:
            break
        messages += [{"role": "assistant", "content": response.content},
                     {"role": "user", "content": f"<WORKFLOW_FEEDBACK>\n{feedback}\n</WORKFLOW_FEEDBACK>\n\n"
                      "Revise as described in Step 6 and return the complete result again, with the full edit list."}]

    dropped = [{"edit": e, "problems": structure.get(i, []) + facts_problems.get(i, [])}
               for i, e in enumerate(edits, 1) if i in structure or i in facts_problems]

    # 4: independent audit of the changed lines only
    by_id = {u.id: u for u in units}
    changes = []
    for n, e in enumerate(ok_edits, 1):
        original = by_id[e["target_id"]].text
        if e["op"] == "add":
            line = f"Edit {n} (add after {e['target_id']}): NEW LINE: {e['new_text']}"
        elif e["op"] == "rewrite":
            line = f"Edit {n} (rewrite {e['target_id']}): ORIGINAL: {original}\n  NEW: {e['new_text']}"
        elif e["op"] == "remove" and by_id[e["target_id"]].kind == "entry":  # removes the project with its bullets
            entry = by_id[e["target_id"]]
            inside = [u.text for u in units if u.kind == "bullet" and entry.start <= u.start < entry.end]
            line = (f"Edit {n} (remove {e['target_id']}, the whole entry with its bullets): REMOVED: {original}"
                    + "".join(f"\n  REMOVED bullet: {t}" for t in inside))
        elif e["op"] == "remove":
            line = f"Edit {n} (remove {e['target_id']}): REMOVED: {original}"
        else:
            line = f"Edit {n} (move {e['target_id']} {e['position']} {e['anchor_id']}): {original}"
        notes = warnings.get(edits.index(e) + 1, [])
        changes.append(line + (f"\n  Automated check notes: {'; '.join(notes)}" if notes else ""))
    audit = None
    if ok_edits:
        audit_system = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": (
            f"{inp.audit_prompt.strip()}\n\n<ORIGINAL_RESUME>\n{resume_text}\n</ORIGINAL_RESUME>\n\n"
            f"<VERIFIED_SUPPLEMENTAL_FACTS>\n{inp.facts.strip()}\n</VERIFIED_SUPPLEMENTAL_FACTS>\n")}]
        audit_msg = (f"<JOB_DESCRIPTION>\nJob title: {row['title']}\nCompany: {row['company']}\n\n"
                     f"{strip_boilerplate(row['description'])}\n</JOB_DESCRIPTION>\n\n"
                     f"<PROPOSED_CHANGES>\n" + "\n\n".join(changes) + "\n</PROPOSED_CHANGES>\n\nAudit these changes.")
        audit_resp = _call(client, inp.audit_model, inp.effort, audit_system,
                           [{"role": "user", "content": audit_msg}], AuditResult, audit_usage)
        audit = audit_resp.parsed_output
        log(f"  audit: {audit.overall_verdict}")

    # Apply audit findings: fix with the auditor's wording, or revert.
    audit_actions = []
    final_edits = list(ok_edits)
    if audit:
        for f in sorted(audit.findings, key=lambda f: -f.edit_number):
            if f.severity == "LOW" or not 1 <= f.edit_number <= len(ok_edits):
                continue
            e = ok_edits[f.edit_number - 1]
            if e not in final_edits:
                continue
            fixed = dict(e, new_text=f.recommended_text) if f.recommended_text.strip() and e["op"] in ("rewrite", "add") else None
            if fixed:
                s = factcheck.check_structure([fixed], units)
                p, _ = factcheck.check_facts([fixed], units, resume_text, inp.facts)
                if not s and not p:
                    final_edits[final_edits.index(e)] = fixed
                    audit_actions.append(f"Edit {f.edit_number}: reworded per audit ({f.severity}: {f.issue})")
                    continue
            final_edits.remove(e)
            audit_actions.append(f"Edit {f.edit_number}: reverted ({f.severity}: {f.issue})")

    final_edits, fit_actions = fit_pages(inp.tex, units, final_edits, inp.page_limit)
    audit_actions += fit_actions

    # 5: build and save
    role = _slug(row["title"])
    name = f"{inp.file_prefix}_{_slug(result.industry)}_{_slug(result.company_short)}_{role}"
    out_dir = inp.out_root / name
    final_tex = resume_tex.apply_edits(inp.tex, units, final_edits)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.tex").write_text(final_tex)
    pdf, pages, compile_error = resume_tex.compile_pdf(final_tex, out_dir, name)

    report = {
        "decision": "tailor", "result": result.model_dump(), "applied_edits": final_edits, "dropped_edits": dropped,
        "check_warnings": {str(k): v for k, v in warnings.items()},
        "audit": audit.model_dump() if audit else None, "audit_actions": audit_actions,
        "rounds": rounds, "pages": pages, "compile_error": compile_error,
        "output_dir": str(out_dir), "pdf": str(pdf) if pdf else None,
        "usage": {"tailor": usage.tokens, "audit": audit_usage.tokens},
        "cost": (usage.cost() or 0) + (audit_usage.cost() or 0),
    }
    (out_dir / "changes.md").write_text(change_report(row, report, units))
    return report


def change_report(row, rep: dict, units) -> str:
    by_id = {u.id: u for u in units}
    r = rep["result"]
    out = [f"# {row['company']} - {row['title']}", "", row["url"], ""]
    out += [f"Pages: {rep['pages']}" + (f"  (compile error: {rep['compile_error']})" if rep["compile_error"] else ""),
            f"Estimated cost: ${rep['cost']:.2f}", "", "## Strategy"] + [f"- {s}" for s in r["strategy"]]
    out += ["", "## Changes applied"]
    for e in rep["applied_edits"]:
        u = by_id[e["target_id"]]
        if e["op"] == "rewrite":
            out += [f"- **{e['target_id']} rewritten** ({e['reason']})", f"  - was: {u.text}", f"  - now: {e['new_text']}"]
        elif e["op"] == "add":
            out += [f"- **added after {e['target_id']}** ({e['reason']}; source: {e['evidence_source']})", f"  - {e['new_text']}"]
        elif e["op"] == "remove":
            out += [f"- **{e['target_id']} removed** ({e['reason']})", f"  - {u.text}"]
        else:
            out += [f"- **{e['target_id']} moved {e['position']} {e['anchor_id']}** ({e['reason']})"]
    if rep["audit_actions"]:
        out += ["", "## Audit changes"] + [f"- {a}" for a in rep["audit_actions"]]
    if rep["dropped_edits"]:
        out += ["", "## Edits dropped by automated checks"]
        out += [f"- {d['edit']['target_id']}: {'; '.join(d['problems'])}" for d in rep["dropped_edits"]]
    if rep["audit"]:
        a = rep["audit"]
        out += ["", "## Audit", a["overall_verdict"]]
        out += [f"- [{f['severity']}] edit {f['edit_number']}: {f['issue']}" for f in a["findings"]]
        if a["unverified_claims"]:
            out += ["", "Unverified claims:"] + [f"- {c}" for c in a["unverified_claims"]]
    out += ["", "## Remaining gaps"] + [f"- {g}" for g in r["remaining_gaps"]]
    if r["questions"]:
        out += ["", "## Questions for you (answers go in verified_supplemental_facts.md)"]
        out += [f"- {q['question']}\n  - why: {q['why_it_matters']}\n  - counts as evidence: {q['acceptable_evidence']}"
                for q in r["questions"]]
    if r["limitations"]:
        out += ["", "## Limitations"] + [f"- {l}" for l in r["limitations"]]
    return "\n".join(out) + "\n"
