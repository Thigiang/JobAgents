"""Assisted applying: open the job's application form in a visible browser, fill the standard contact
fields and attach the tailored resume. The candidate reviews everything, answers the remaining
questions and clicks Submit themselves; nothing here ever submits a form.

Filled automatically (only when the field is empty): name, email, phone, location, LinkedIn, GitHub,
website, and the resume file. Left to the candidate: dropdowns and yes/no questions (work
authorization, sponsorship, start date), custom questions, voluntary demographic questions, and any
login or CAPTCHA.
"""

import re
from pathlib import Path

from .sources import NO_FETCH_DOMAINS

# (applicant key, pattern matched against a field's label, name, id, placeholder and aria-label)
FIELD_PATTERNS = [
    ("first_name", r"first[\s_-]*name|given[\s_-]*name|\bfname\b"),
    ("last_name", r"last[\s_-]*name|family[\s_-]*name|surname|\blname\b"),
    ("full_name", r"^\s*((full|legal)[\s_-]*)?name\b"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile"),
    ("location", r"^\s*(current\s+)?location|^\s*city\b|where are you (located|based)"),
    ("linkedin", r"linked\s*in"),
    ("github", r"git\s*hub"),
    ("website", r"website|portfolio|personal\s+(site|url)"),
]
_RESUME_RE = re.compile(r"resume|résumé|\bcv\b", re.I)
_SKIP_RE = re.compile(r"preferred|emergency|reference|referr|recruiter|manager|company|employer|school|\bother\b",
                      re.I)

# Reads every form field in a frame with the text that describes it. Runs in the page.
_FIELDS_JS = r"""() => [...document.querySelectorAll('input, textarea')].map((el, i) => {
  // Own label first; nearby label text only for fields without one (it can belong to a neighbour).
  const own = (el.labels ? [...el.labels].map(l => l.innerText).join(' ') : '') || el.getAttribute('aria-label') || '';
  const wrap = own ? null : el.closest('.application-question, .field, [class*="field"], [class*="question"]');
  const near = wrap ? (wrap.querySelector('label, .application-label, [class*="label"]') || {}).innerText || '' : '';
  const r = el.getBoundingClientRect();
  return {i, type: (el.type || 'text').toLowerCase(), value: el.value || '',
          visible: r.width > 0 && r.height > 0 && !el.disabled && !el.readOnly,
          label: (own || near).replace(/\s+/g, ' ').trim(),
          attrs: [el.name, el.id, el.placeholder].filter(Boolean).join(' ')};
})"""
_TEXT_TYPES = {"text", "email", "tel", "url", "textarea", "search", ""}


def application_url(row) -> str:
    """The page with the form: Lever and Ashby put it on a sub-page of the posting."""
    url = row["url"].rstrip("/")
    if "jobs.lever.co" in url and not url.endswith("/apply"):
        return url + "/apply"
    if "jobs.ashbyhq.com" in url and not url.endswith("/application"):
        return url + "/application"
    return url


def _value_for(field: dict, applicant: dict) -> str | None:
    # The visible label decides; name/id/placeholder are used only when a field has no label.
    text = field["label"] or re.sub(r"[\[\]_]", " ", field["attrs"])
    if _SKIP_RE.search(text):
        return None
    for key, pattern in FIELD_PATTERNS:
        if re.search(pattern, text, re.I):
            value = applicant.get(key) or ""
            if key == "full_name" and not value:
                value = f"{applicant.get('first_name', '')} {applicant.get('last_name', '')}".strip()
            return value or None
    return None


def fill_form(page, applicant: dict, resume_pdf: Path) -> tuple[list[str], bool]:
    """Fill empty standard fields in every frame (company sites often embed the form in an iframe).
    Returns (descriptions of what was filled, whether the resume was attached)."""
    filled, attached = [], False
    for frame in page.frames:
        try:
            fields = frame.evaluate(_FIELDS_JS)
        except Exception:  # frame navigated away or is cross-origin and not ready
            continue
        inputs = frame.locator("input, textarea")
        for f in fields:
            name = f["label"] or f["attrs"]
            if f["type"] == "file":  # labels are often just "Attach", so check the id/name too
                if not attached and _RESUME_RE.search(f"{f['label']} {f['attrs']}"):
                    inputs.nth(f["i"]).set_input_files(str(resume_pdf))
                    attached = True
                    filled.append(f"resume: attached {resume_pdf.name}")
                continue
            # Real fields have a name or id; unnamed ones are widget helpers (e.g. inside a dropdown).
            if f["type"] not in _TEXT_TYPES or not f["visible"] or f["value"] or not f["attrs"]:
                continue
            value = _value_for(f, applicant)
            if value:
                inputs.nth(f["i"]).fill(value)
                filled.append(f"{name[:40]}: {value}")
    if not attached:  # a single unlabeled upload box is the resume
        for frame in page.frames:
            files = frame.locator("input[type=file]")
            if files.count() == 1:
                files.first.set_input_files(str(resume_pdf))
                filled.append(f"resume: attached {resume_pdf.name} (only upload field on the page)")
                attached = True
                break
    return filled, attached


def open_and_fill(url: str, applicant: dict, resume_pdf: Path, profile_dir: Path, ask=input, log=print,
                  headless: bool = False) -> bool:
    """Open the form, fill what's safe, then wait for the candidate. Returns True if they say they submitted.
    The browser keeps its own profile in profile_dir, so logins (e.g. Workday accounts) persist."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless, no_viewport=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("input[type=file], form input", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)  # let scripted forms finish rendering
            if "myworkdayjobs.com" in url:
                filled, attached = [], False
                log("  Workday needs you to sign in and click Apply first; the tool can't fill it.")
            elif any(d in url for d in NO_FETCH_DOMAINS):
                filled, attached = [], False
                log("  This site doesn't allow automation; the page is open for you to apply yourself.")
            else:
                filled, attached = fill_form(page, applicant, resume_pdf)
            log(f"  Filled {len(filled)} field(s):")
            for item in filled:
                log(f"    - {item}")
            if not attached:
                log(f"  ! Resume not attached; upload it yourself: {resume_pdf}")
            log("\n  Now in the browser: check every filled field, answer the remaining questions,\n"
                "  and click Submit yourself. The tool never submits.")
            answers = applicant.get("answers") or {}
            if answers:
                log("  Your saved answers (from config.toml [applicant.answers]):")
                for question, answer in answers.items():
                    log(f"    - {question}: {answer or '(not set)'}")
            reply = ask("\n  Did you submit the application? [y/N] (the browser closes after you answer): ")
            return reply.strip().lower().startswith("y")
        finally:
            ctx.close()
