"""Fetch job postings from public job-board APIs (Greenhouse, Lever, Ashby, Workday), and single
postings from a link (see fetch_posting)."""

import hashlib
import html
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

import certifi

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


@dataclass
class Job:
    id: str            # globally unique: "<source>:<slug>:<posting id>"
    source: str
    company: str
    title: str
    location: str
    url: str
    description: str   # plain text
    posted: str = ""   # date first posted, YYYY-MM-DD ("" if the board doesn't say)


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    text = "".join(parser.parts).replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _get_json(url: str, body: dict | None = None):
    headers = {"User-Agent": "jobagent/0.1", "Accept": "application/json"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as resp:
        return json.load(resp)


def _greenhouse_job(slug: str, p: dict) -> Job:
    return Job(
        id=f"greenhouse:{slug}:{p['id']}",
        source="greenhouse",
        company=p.get("company_name") or slug,
        title=p["title"],
        location=(p.get("location") or {}).get("name", ""),
        url=p["absolute_url"],
        # Greenhouse returns HTML that is itself entity-escaped.
        description=html_to_text(html.unescape(p.get("content", ""))),
        posted=(p.get("first_published") or p.get("updated_at") or "")[:10],
    )


def fetch_greenhouse(slug: str, search: dict | None = None, max_days: int | None = None) -> list[Job]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [_greenhouse_job(slug, p) for p in data.get("jobs", [])]


def _lever_job(slug: str, p: dict) -> Job:
    cats = p.get("categories") or {}
    locations = cats.get("allLocations") or [cats.get("location", "")]
    if p.get("workplaceType") == "remote":
        locations = [*locations, "Remote"]
    sections = [p.get("descriptionPlain", "")]
    for lst in p.get("lists", []):
        sections.append(f"{lst['text']}\n{html_to_text(lst['content'])}")
    sections.append(p.get("additionalPlain", ""))
    return Job(
        id=f"lever:{slug}:{p['id']}",
        source="lever",
        company=slug,
        title=p["text"],
        location="; ".join(l for l in locations if l),
        url=p["hostedUrl"],
        description="\n\n".join(s.strip() for s in sections if s.strip()),
        posted=datetime.fromtimestamp(p["createdAt"] / 1000, timezone.utc).date().isoformat() if p.get("createdAt") else "",
    )


def fetch_lever(slug: str, search: dict | None = None, max_days: int | None = None) -> list[Job]:
    return [_lever_job(slug, p) for p in _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")]


def fetch_ashby(slug: str, search: dict | None = None, max_days: int | None = None) -> list[Job]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for p in data.get("jobs", []):
        if not p.get("isListed", True):
            continue
        # "location" is often an office label like "SF Office", so add the address city/state too.
        places = [p.get("location", "")]
        for loc in [p, *(p.get("secondaryLocations") or [])]:
            addr = ((loc.get("address") or {}).get("postalAddress") or {})
            city = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x)
            places.append(loc.get("location", "") if loc is not p else "")
            places.append(city)
        if p.get("isRemote") or p.get("workplaceType") == "Remote":
            places.append("Remote")
        jobs.append(Job(
            id=f"ashby:{slug}:{p['id']}",
            source="ashby",
            company=slug,
            title=p["title"],
            location="; ".join(dict.fromkeys(l for l in places if l)),
            url=p["jobUrl"],
            description=p.get("descriptionPlain") or html_to_text(p.get("descriptionHtml", "")),
            posted=(p.get("publishedAt") or "")[:10],
        ))
    return jobs


# Workday career sites (used by many large pharma, medtech and insurers) have no official public API;
# this is the JSON the public careers page itself loads. Slug: "<tenant>/<wdN>/<site>", from a careers
# URL like https://<tenant>.<wdN>.myworkdayjobs.com/<site>. Boards hold thousands of postings, so this
# searches a few keywords and only downloads descriptions for titles that pass the title filters.
# Workday rate-limits busy clients, so requests are paced and retried once.
_WORKDAY_QUERIES = ("data", "analyst", "machine learning", "scientist")
_WORKDAY_MAX_PER_QUERY = 100
_WORKDAY_PAUSE = 1.0  # seconds between requests


def _workday_get(url: str, body: dict | None = None):
    for attempt in range(2):
        time.sleep(_WORKDAY_PAUSE * (1 + 10 * attempt))
        try:
            return _get_json(url, body)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt or isinstance(e, urllib.error.HTTPError) and e.code < 429:
                raise


def _workday_base(tenant: str, wd: str, site: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def _workday_job(tenant: str, wd: str, site: str, path: str) -> Job:
    detail = _workday_get(_workday_base(tenant, wd, site) + path)
    info = detail["jobPostingInfo"]
    places = [info.get("location", ""), *(info.get("additionalLocations") or [])]
    if "remote" in (info.get("remoteType") or "").lower():
        places.append("Remote")
    company = re.sub(r"^\d+\s+", "", (detail.get("hiringOrganization") or {}).get("name", "")) or tenant
    return Job(
        id=f"workday:{tenant}:{info.get('jobReqId') or info['id']}",
        source="workday",
        company=company,
        title=info["title"],
        location="; ".join(dict.fromkeys(p for p in places if p)),
        url=info.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
        description=html_to_text(info.get("jobDescription", "")),
        posted=info.get("startDate", "")[:10],
    )


def _workday_days_ago(posted_on: str) -> int | None:
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 5 Days Ago' -> 5, 'Posted 30+ Days Ago' -> 30."""
    text = posted_on.lower()
    if "today" in text:
        return 0
    if "yesterday" in text:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", text)
    return int(m.group(1)) if m else None


def fetch_workday(slug: str, search: dict | None = None, max_days: int | None = None) -> list[Job]:
    tenant, wd, site = slug.split("/")
    title_only = {**(search or {}), "locations": [], "location_exclude": []}
    paths = {}
    for query in _WORKDAY_QUERIES:
        for offset in range(0, _WORKDAY_MAX_PER_QUERY, 20):
            page = _workday_get(_workday_base(tenant, wd, site) + "/jobs",
                             {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query})
            for p in page.get("jobPostings", []):
                if not p.get("title") or not p.get("externalPath"):
                    continue  # some boards return partial entries on later pages
                age = _workday_days_ago(p.get("postedOn", ""))
                if max_days is not None and age is not None and age > max_days:
                    continue  # skip downloading descriptions of older postings
                if matches_filters(Job("", "", "", p["title"], "", "", ""), title_only):
                    paths[p["externalPath"]] = True
            if offset + 20 >= page.get("total", 0):
                break
    return [_workday_job(tenant, wd, site, path) for path in paths]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "workday": fetch_workday}


# ---------------------------------------------------------------- single postings from a link

class PostingUnavailable(Exception):
    """The link can't be read automatically (closed posting, login wall, or unsupported site)."""


# Sites whose terms forbid automated access; their descriptions must be pasted in.
NO_FETCH_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "tesla.com")


def posted_within(job: Job, max_days: int) -> bool:
    """True if the job was posted in the last max_days days, or if its posting date is unknown."""
    if not job.posted:
        return True
    try:
        return date.fromisoformat(job.posted) >= date.today() - timedelta(days=max_days)
    except ValueError:
        return True


def manual_id(url: str) -> str:
    return "manual:" + hashlib.sha1(url.strip().encode()).hexdigest()[:12]


def fetch_posting(url: str) -> Job:
    """Read one posting from its link. Job-board links (Greenhouse, Lever, Ashby, Workday) use their
    JSON APIs; other company career pages are read from the schema.org JobPosting data most of them
    embed. Raises PostingUnavailable when the posting must be pasted in instead."""
    host = urllib.parse.urlparse(url).netloc.lower()
    path = urllib.parse.urlparse(url).path
    if any(host == d or host.endswith("." + d) for d in NO_FETCH_DOMAINS):
        raise PostingUnavailable(f"{host} does not allow automated reading; paste the description instead")
    try:
        if m := re.match(r"/([^/]+)/jobs/(\d+)", path) if "greenhouse.io" in host else None:
            slug, job_id = m.groups()
            return _greenhouse_job(slug, _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"))
        if m := re.match(r"/([^/]+)/([0-9a-f-]{36})", path) if host == "jobs.lever.co" else None:
            slug, job_id = m.groups()
            return _lever_job(slug, _get_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}"))
        if m := re.match(r"/([^/]+)/([0-9a-f-]{36})", path) if host == "jobs.ashbyhq.com" else None:
            slug, job_id = m.groups()
            for job in fetch_ashby(slug):
                if job.id.endswith(job_id):
                    return job
            raise PostingUnavailable("this Ashby posting is no longer listed (probably closed)")
        if m := re.match(r"([^.]+)\.(wd\d+)\.myworkdayjobs\.com", host):
            tenant, wd = m.groups()
            if p := re.match(r"(?:/[a-z]{2}-[A-Z]{2})?/([^/]+)(/job/.+)", path):
                return _workday_job(tenant, wd, p.group(1), p.group(2))
        return _job_from_page(url)
    except urllib.error.HTTPError as e:
        raise PostingUnavailable(f"the site answered {e.code} (the posting may be closed)") from e
    except urllib.error.URLError as e:
        raise PostingUnavailable(f"could not reach the site: {e.reason}") from e


def _job_from_page(url: str) -> Job:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (jobagent/0.1)"})
    with urllib.request.urlopen(req, timeout=60, context=_SSL_CONTEXT) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return _job_from_schema(url, item)
    raise PostingUnavailable("the page has no readable job data; paste the description instead")


def _job_from_schema(url: str, item: dict) -> Job:
    places = []
    locs = item.get("jobLocation") or []
    for loc in [locs] if isinstance(locs, dict) else locs:  # one location may be a single object
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            places.append(", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x))
    if item.get("jobLocationType") == "TELECOMMUTE":
        places.append("Remote")
    org = item.get("hiringOrganization") or {}
    return Job(
        id=manual_id(url),
        source="manual",
        company=(org.get("name") if isinstance(org, dict) else str(org)) or "",
        title=html.unescape(item.get("title", "")),
        location="; ".join(dict.fromkeys(p for p in places if p)),
        url=url,
        description=html_to_text(html.unescape(item.get("description", ""))),
        posted=str(item.get("datePosted", ""))[:10],
    )

# Paragraphs containing any of these are legal/notice boilerplate with no bearing on fit.
# Deliberately narrow: salary, location, benefits and company descriptions are kept.
_BOILERPLATE_MARKERS = (
    "equal opportunity employer", "equal employment opportunity", "without regard to race",
    "reasonable accommodation", "need for accommodation", "e-verify", "fair chance ordinance",
    "arrest and conviction records", "arrest or conviction records", "california residents",
    "data-collection", "privacy notice", "job scams", "suspicious alerts", "recruitment fraud",
    "authentic oscar health job", "cyber crime",
)


def strip_boilerplate(description: str) -> str:
    """Drop EEO, accommodation, privacy and scam-warning paragraphs before sending to Claude."""
    paragraphs = description.split("\n\n")
    kept = [p for p in paragraphs if not any(m in p.lower() for m in _BOILERPLATE_MARKERS)]
    return "\n\n".join(kept)


def matches_filters(job: Job, search: dict) -> bool:
    title = job.title.lower()
    include = [t.lower() for t in search.get("title_include", [])]
    exclude = [t.lower() for t in search.get("title_exclude", [])]
    locations = [l.lower() for l in search.get("locations", [])]

    if include and not any(t in title for t in include):
        return False
    # Whole words only, so "intern" doesn't drop "Internal Audit" and "lead" doesn't drop "Leadership".
    checked_title = title
    for phrase in search.get("title_exclude_exceptions", []):
        checked_title = checked_title.replace(phrase.lower(), " ")
    if any(re.search(rf"(?<!\w){re.escape(t.strip())}(?!\w)", checked_title) for t in exclude):
        return False
    if locations and not any(l in job.location.lower() for l in locations):
        return False
    # Whole words, so "india" doesn't drop "Indianapolis".
    if any(re.search(rf"(?<!\w){re.escape(l.lower())}(?!\w)", job.location.lower())
           for l in search.get("location_exclude", [])):
        return False
    return True


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}
# "3+ years", "3-5 years", "three (3) years", "5 or more years", "minimum of 4 years"
_YEARS_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s*(?:\(\d+\)\s*)?(?:\+|plus)?\s*"
    r"(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:or more\s+)?years?\b"
)
_PREFERRED_WORDS = ("prefer", "nice to have", "nice-to-have", "bonus", "a plus", "desired", "ideally")
_REQUIRED_HEADINGS = ("require", "minimum", "basic qualification", "must have", "what you'll need",
                      "what you need", "what you bring", "qualifications", "about you", "you have")
_PHD_RE = re.compile(r"\bph\.?\s?d\b\.?", re.I)
# A degree requirement ("PhD in ...", "PhD degree", "... PhD required"), not "our team of PhDs".
_PHD_REQUIREMENT_RE = re.compile(r"\bph\.?\s?d\b\.?[^.;]{0,25}?\b(in|degree)\b|\bph\.?\s?d\b.*\brequired\b", re.I)
_OTHER_DEGREE_RE = re.compile(r"\b(master'?s?|m\.?s|bachelor'?s?|b\.?s|b\.?a|equivalent)\b", re.I)


def _required_lines(description: str):
    """Yield lines outside 'preferred / nice to have' sections, minus clauses that say 'preferred'."""
    in_preferred = False
    for line in description.splitlines():
        bullet = line.lstrip().startswith(("-", "•", "*"))
        low = line.lower().strip(" -•*:\t")
        if not low:
            continue
        if len(low) < 70 and not low.endswith("."):  # looks like a heading
            if any(w in low for w in _PREFERRED_WORDS):
                in_preferred = True
                continue
            if any(w in low for w in _REQUIRED_HEADINGS):
                in_preferred = False
                continue
        if in_preferred:
            continue
        # "3-5 years of experience, healthcare data a plus": only the second clause is optional.
        clauses = re.split(r"[;,]|\.\s", low)
        kept = ", ".join(c for c in clauses if not any(w in c for w in _PREFERRED_WORDS))
        if kept.strip():
            yield kept, bullet


def requirement_blockers(description: str, search: dict) -> list[str]:
    """Free check of the posting text for requirements the candidate clearly can't meet.

    Separate requirement lines all apply, so the strictest one counts; numbers on the same line are
    usually alternatives ("BS + 3 years or MS + 1 year"), so the lowest one on a line counts.
    A PhD blocks only when it is required with no other degree offered.
    """
    blockers = []
    max_years = search.get("max_years_required")
    lines = list(_required_lines(description))
    if max_years is not None:
        per_line = []
        for line, bullet in lines:
            # Bullets are requirement lists ("4+ years in data"); prose needs the word "experience",
            # so company blurbs like "20 years in healthcare" don't count.
            if not bullet and "experience" not in line:
                continue
            found = [_NUMBER_WORDS.get(m.group(1)) or int(m.group(1)) for m in _YEARS_RE.finditer(line)]
            if found:
                per_line.append(min(found))
        if per_line and max(per_line) > max_years:
            blockers.append(f"requires {max(per_line)}+ years of experience")
    if search.get("exclude_phd_required"):
        for line, _ in lines:
            if _PHD_REQUIREMENT_RE.search(line) and not _OTHER_DEGREE_RE.search(_PHD_RE.sub("", line)):
                blockers.append("requires a PhD")
                break
    return blockers
