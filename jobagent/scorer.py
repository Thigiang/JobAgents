"""Score how well a job posting matches the master resume, using Claude.

The instructions live in prompts/job_matching_master_prompt.md. Two modes:
- SCREEN: a short assessment run on every new job (cheap).
- FULL:   the complete analysis plus the tailoring handoff, run only on jobs
          you are considering (e.g. approved ones).

Claude returns structured data; the code computes the total, the recommendation
and the mandatory-requirement status from it, so those follow fixed rules.
"""

from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .sources import strip_boilerplate

# Mirrors the rubric in the prompt; passed on to the tailoring stage unchanged.
RUBRIC = {
    "Required qualifications and core responsibilities": 30,
    "Technical skills, tools, and methods": 25,
    "Relevant work experience and projects": 20,
    "Education and relevant certifications": 10,
    "Preferred qualifications and domain knowledge": 10,
    "Seniority and scope alignment": 5,
}
# The only categories the prompt allows to be marked not applicable.
MAY_BE_NOT_APPLICABLE = {
    "Education and relevant certifications",
    "Preferred qualifications and domain knowledge",
}

Category = Literal[
    "Required qualifications and core responsibilities",
    "Technical skills, tools, and methods",
    "Relevant work experience and projects",
    "Education and relevant certifications",
    "Preferred qualifications and domain knowledge",
    "Seniority and scope alignment",
]


class CategoryScore(BaseModel):
    category: Category
    applicable: bool = Field(description="False only if the JD states nothing for this category (see section 4).")
    points_awarded: int = Field(description="0 up to the category's maximum points; 0 if not applicable.")
    explanation: str = Field(description="One or two sentences citing the key evidence.")


class MandatoryIssue(BaseModel):
    requirement: str
    status: Literal["CONFIRMED_GAP", "POTENTIAL_GAP", "UNVERIFIED"]
    explanation: str


class ScreenResult(BaseModel):
    job_title: str
    company: str
    score_breakdown: list[CategoryScore] = Field(description="Exactly six entries, one per rubric category.")
    mandatory_issues: list[MandatoryIssue] = Field(description="Section 5 issues only; empty if none.")
    confidence: Literal["High", "Medium", "Low"]
    top_strengths: list[str] = Field(description="Top three strengths.")
    top_gaps_or_uncertainties: list[str] = Field(description="Top three gaps or uncertainties.")
    summary: str = Field(description="One sentence.")
    limitations: list[str] = Field(description="Anything that could not be assessed. Empty if none.")


class RequirementComparison(BaseModel):
    jd_requirement: str
    importance: Literal["Required", "Preferred", "Responsibility"]
    resume_evidence: str
    match_classification: Literal["Verified match", "Partial/transferable match", "Unverified", "Gap"]
    impact: str


class ApplicationRecommendation(BaseModel):
    worth_the_time: str = Field(description="Why this role is or is not a good use of application time.")
    most_relevant_strengths: str = Field(description="Strengths most relevant to the hiring manager.")
    disqualifying_vs_learnable_gaps: str
    what_would_change_assessment: str


class TailoringHandoff(BaseModel):
    ranked_required_qualifications: list[str]
    ranked_preferred_qualifications: list[str]
    keywords_and_concepts: list[str]
    tailoring_priorities: list[str] = Field(description="Most important areas for truthful resume tailoring.")
    claims_not_to_make: list[str] = Field(description="Claims that must not be made without further verification.")


class FullAnalysis(ScreenResult):
    requirement_comparison: list[RequirementComparison] = Field(description="The 8-12 most important JD requirements.")
    application_recommendation: ApplicationRecommendation
    tailoring_handoff: TailoringHandoff


class Scorer:
    def __init__(self, prompt: str, resume: str, facts: str, match_config: str, model: str, effort: str):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        # Prompt, resume, facts and config are identical on every call (both modes), so cache them.
        system_text = (
            f"{prompt.strip()}\n\n"
            f"<SOURCE_RESUME>\n{resume.strip()}\n</SOURCE_RESUME>\n\n"
            f"<VERIFIED_SUPPLEMENTAL_FACTS>\n{facts.strip()}\n</VERIFIED_SUPPLEMENTAL_FACTS>\n\n"
            f"<MATCH_CONFIG>\n{match_config.strip()}\n</MATCH_CONFIG>\n"
        )
        self.system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    def screen(self, title, company, location, url, description) -> dict | None:
        return self._run("SCREEN", ScreenResult, 16000, title, company, location, url, description)

    def full(self, title, company, location, url, description) -> dict | None:
        return self._run("FULL", FullAnalysis, 32000, title, company, location, url, description)

    def _run(self, mode, schema, max_tokens, title, company, location, url, description) -> dict | None:
        """Returns the result as a dict, or None if the model declined the request."""
        posting = (
            f"<JOB_DESCRIPTION>\nJob title: {title}\nCompany: {company}\nLocation: {location}\n"
            f"Job posting URL: {url}\n\n{strip_boilerplate(description)}\n</JOB_DESCRIPTION>\n\n"
            f"MODE: {mode}\n\nEvaluate this job description against the candidate's resume."
        )
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            system=self.system,
            messages=[{"role": "user", "content": posting}],
            output_format=schema,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return None
        return finalize(response.parsed_output, mode, url)


def recommendation_for(score: int, has_confirmed_gap: bool) -> str:
    if score >= 85:
        rec = "Strong match"
    elif score >= 70:
        rec = "Worth applying"
    elif score >= 55:
        rec = "Stretch application"
    else:
        rec = "Low priority"
    if has_confirmed_gap and rec in ("Strong match", "Worth applying"):
        rec = "Stretch application"
    return rec


def finalize(result: ScreenResult, mode: str, url: str) -> dict:
    """Apply the prompt's fixed rules in code: total, recommendation, mandatory status."""
    out = result.model_dump()
    warnings = []

    seen = [c.category for c in result.score_breakdown]
    missing = set(RUBRIC) - set(seen)
    if missing:
        warnings.append(f"Missing categories (scored as 0): {', '.join(sorted(missing))}")
    if len(seen) != len(set(seen)):
        warnings.append("Duplicate categories returned; only the first of each was used")

    awarded, applicable_max, used = 0, 0, set()
    for c, row in zip(result.score_breakdown, out["score_breakdown"]):
        if c.category in used:
            continue
        used.add(c.category)
        max_pts = RUBRIC[c.category]
        row["max_points"] = max_pts
        if not c.applicable and c.category not in MAY_BE_NOT_APPLICABLE:
            warnings.append(f"{c.category} was marked not applicable, which the rubric doesn't allow; scored as applicable")
            row["applicable"] = True
        if not row["applicable"]:
            continue
        points = min(max(c.points_awarded, 0), max_pts)
        if points != c.points_awarded:
            warnings.append(f"{c.category}: {c.points_awarded} points is outside 0-{max_pts}; using {points}")
            row["points_awarded"] = points
        awarded += points
        applicable_max += max_pts
    applicable_max += sum(RUBRIC[m] for m in missing)

    total = round(awarded / applicable_max * 100) if applicable_max else 0
    statuses = {i.status for i in result.mandatory_issues}
    if "CONFIRMED_GAP" in statuses:
        mandatory_status = "Potential disqualifier"
    elif statuses:
        mandatory_status = "Needs verification"
    else:
        mandatory_status = "No issue identified"

    out.update(
        mode=mode,
        overall_score=total,
        recommendation=recommendation_for(total, "CONFIRMED_GAP" in statuses),
        mandatory_requirement_status=mandatory_status,
        rubric=RUBRIC,
        job_url=url,
        score_check_warnings=warnings,
    )
    if mode == "FULL":
        # Evidence lives once, in requirement_comparison; group it for the tailoring stage.
        by_class = {}
        for req in out["requirement_comparison"]:
            by_class.setdefault(req["match_classification"], []).append(req["jd_requirement"])
        out["tailoring_handoff"].update(
            verified_matches=by_class.get("Verified match", []),
            partial_or_transferable=by_class.get("Partial/transferable match", []),
            unverified_requirements=by_class.get("Unverified", []),
            gaps=by_class.get("Gap", []),
        )
    return out
