# Resume Tailoring Configuration

This file stores stable preferences for tailoring resumes. It is not evidence of candidate skills or experience.

## Source of truth
- Master resume: `ThiGiang_Resume_Master.tex`. The tailoring stage receives it as plain text with numbered editable lines (`SOURCE_RESUME`). `master_resume.md` is a plain copy with the same content, used by the scoring stage.
- The master resume is the authoritative baseline for candidate facts.
- Do not modify or overwrite the master resume; each tailored version is saved separately.
- Only add facts absent from the master resume when they are explicitly verified by the candidate and recorded in `verified_supplemental_facts.md`.

## Page length
- Preferred page limit: 1 page
- Preserve relevant evidence rather than deleting important qualifications solely to meet the page limit.
- If the page limit conflicts with readability or evidence preservation, explain the trade-off.

## Formatting
- The visual format comes from the master LaTeX resume (`ThiGiang_Resume_Master.tex`, in the project folder). The workflow applies edits to it, so section headings, typography, spacing, bullet style, and overall visual design are preserved.
- Make targeted edits rather than redesigning the resume.
- Keep the document ATS-readable.
- Do not add decorative graphics, icons, columns, or tables unless already present or explicitly requested.
- Edits change wording, order, or inclusion of lines only; never formatting.

## Target seniority
- Target: entry-level / early-career roles, generally around 1–2 years of relevant experience.
- This describes the target roles, not a claim that the candidate has 1–2 years of experience.
- Never state or imply a duration unless supported by verified dates and experience.
- Consider transferable skills from research, projects, coursework, certificates, and professional experience where relevant.
- Do not automatically exclude roles solely because the title or experience range differs slightly; assess actual requirements and disclose material gaps.

## Location and work arrangement
Prioritize:
- Remote roles for which the candidate is eligible to work from their actual location.
- Roles in the San Francisco Bay Area.
- Roles in Austin, Texas.

Rules:
- “Remote” does not necessarily mean work from any state. Check employer geographic restrictions when the information is available.
- Do not imply willingness to relocate unless the candidate has explicitly confirmed it.
- Do not misrepresent the candidate's location or work authorization.
- Job search and scoring use these same locations (set in `config.toml`). For resume content, do not add a location or availability claim unless accurate and appropriate.

## Writing style
- Professional, clear, concise, specific, and natural.
- Favor concrete descriptions of tasks, methods, tools, and documented outcomes.
- Avoid inflated language, generic corporate buzzwords, unnecessary self-promotion, and claims that cannot be defended in an interview.
- Preserve meaningful technical detail.

## Output preferences
- Return a list of targeted edits, not a rewritten resume. The workflow applies the edits to the master resume to produce the complete tailored resume and PDF.
- For each edit, include the reason, the evidence source, and whether the meaning changed; this serves as the change log. Factuality is audited separately by the workflow's automated checks and the independent auditor.
- Keep the master resume unchanged.
- The workflow saves each tailored version separately and associates it with the relevant job posting.
- The workflow checks every edit automatically (line quoted correctly, no new numbers, tools, or durations) and compiles the resume to check the page count, reporting problems back for revision.
- The independent auditor then reviews the changed lines; its corrections are applied, or the edit is reverted, before the final PDF is generated.
- Do not submit an application or attest to answers without the separate workflow's required human review and approval.
