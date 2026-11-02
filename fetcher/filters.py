"""Title, seniority, location, sponsorship and PERM rules.

Everything here is plain regex over feed fields and description text.
"""

from __future__ import annotations

import re

# ================================================================ titles

def title_passes(title: str, settings: dict) -> bool:
    """Keep analytics-family titles; drop senior/lead/manager etc. and internships.

    `drop_unless` drops a word unless one of its allowed phrases is also in the
    title, e.g. "engineer" is dropped except in "analytics engineer".
    """
    t = title.lower()
    titles = settings["titles"]
    keep = any(re.search(rf"\b{re.escape(k)}\b", t) for k in titles["keep"])
    drop = any(re.search(rf"\b{re.escape(d)}\b", t) for d in titles["drop"])
    for word, allowed in titles.get("drop_unless", {}).items():
        if re.search(rf"\b{re.escape(word)}s?\b", t) and not any(
            re.search(rf"\b{re.escape(a)}s?\b", t) for a in allowed
        ):
            drop = True
    return keep and not drop


_YEARS_RE = re.compile(
    r"(?:minimum of |at least )?(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?",
    re.IGNORECASE,
)
_PREFERRED_RE = re.compile(r"\b(preferred|nice to have|bonus|a plus|ideally)\b", re.IGNORECASE)


def min_years_required(description: str) -> int | None:
    """Smallest 'N years of experience' figure stated as a requirement.

    Taking the smallest is deliberate: "5+ years with a BS or 3+ with an MS"
    should not be dropped, and a false keep is cheaper than a false drop.
    """
    figures = []
    for sentence in split_sentences(description):
        if "experience" not in sentence.lower() or _PREFERRED_RE.search(sentence):
            continue
        figures += [int(m.group(1)) for m in _YEARS_RE.finditer(sentence) if int(m.group(1)) <= 15]
    return min(figures) if figures else None


# ================================================================ sentences

def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation and line breaks. "U.S." is collapsed to
    "US" first so it doesn't end a sentence mid-phrase."""
    text = re.sub(r"\bU\.S\.(?:A\.)?", "US", text)
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


# ================================================================ location

_US_COUNTRY = {"us", "usa", "united states", "united states of america"}
_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    "Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    "Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    "New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    "Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    "West Virginia|Wisconsin|Wyoming"
)
_US_TEXT_RE = re.compile(
    rf"\b(United States|USA|US|U\.S\.|{_STATE_NAMES})\b|,\s*({'|'.join(_STATES)})\b"
    rf"|^({'|'.join(_STATES)})\b",
)
_NON_US_RE = re.compile(
    r"\b(Canada|Mexico|UK|United Kingdom|England|London|Ireland|Germany|France|Spain|Italy|"
    r"Netherlands|Poland|Portugal|India|Bangalore|Bengaluru|Hyderabad|Pune|Mumbai|Singapore|"
    r"Japan|Tokyo|China|Australia|Sydney|Brazil|Israel|Toronto|Vancouver|Philippines|EMEA|APAC|LATAM)\b",
    re.IGNORECASE,
)


# Country names only (no cities), so "New London, CT" or "Cambridge, MA" are safe.
_FOREIGN_COUNTRY_RE = re.compile(
    r"\b(Canada|(?<!New )Mexico|United Kingdom|UK|England|Scotland|Wales|Ireland|Germany|France|Spain|"
    r"Italy|Netherlands|Belgium|Switzerland|Austria|Denmark|Sweden|Norway|Finland|Iceland|Poland|"
    r"Portugal|Greece|Czech Republic|Czechia|Hungary|Romania|Bulgaria|Croatia|Serbia|Ukraine|"
    r"Estonia|Latvia|Lithuania|Turkey|Israel|UAE|United Arab Emirates|Saudi Arabia|Qatar|Egypt|"
    r"Nigeria|Kenya|South Africa|India|Pakistan|Bangladesh|Sri Lanka|Singapore|Malaysia|Indonesia|"
    r"Thailand|Vietnam|Philippines|China|Hong Kong|Taiwan|Japan|South Korea|Korea|Australia|"
    r"New Zealand|Brazil|Argentina|Chile|Colombia|Peru|Costa Rica)\b"
)


def names_foreign_country(title: str, location: str) -> bool:
    """True when the title or location names a non-US country, e.g.
    "Business Consultant (Denmark)". Overrides a feed that says US."""
    return bool(_FOREIGN_COUNTRY_RE.search(title) or _FOREIGN_COUNTRY_RE.search(location))


# Separators between locations in a multi-location string,
# e.g. "Remote, Canada; Remote, United States" or "Remote - US or Canada".
_LOCATION_SPLIT = re.compile(r"\s*(?:;|\||\n|\s/\s|\bor\b)\s*")


def lists_us_location(location: str) -> bool:
    """True when any one of the listed locations is in the US."""
    return any(_US_TEXT_RE.search(part) and not _FOREIGN_COUNTRY_RE.search(part)
               for part in _LOCATION_SPLIT.split(location) if part)


def us_status(title: str, location: str, country_hint: str) -> str:
    """'us', 'non_us', or 'uncertain' (e.g. a bare "Remote").

    A foreign country in the title always drops the job (the Veeva Denmark
    case). In the location it drops the job only when no listed location is
    in the US: "Remote, Canada; Remote, United States" is kept."""
    if _FOREIGN_COUNTRY_RE.search(title):
        return "non_us"
    if _FOREIGN_COUNTRY_RE.search(location):
        return "us" if lists_us_location(location) else "non_us"
    if country_hint:
        return "us" if country_hint.strip().lower() in _US_COUNTRY else "non_us"
    if _US_TEXT_RE.search(location):
        return "us"
    if _NON_US_RE.search(location):
        return "non_us"
    return "uncertain"


_HYBRID_TEXT = re.compile(
    r"\bhybrid\b|\b\d\s*(?:days?|x)\s*(?:a|per)\s*week\s*(?:in|from)\s*(?:the\s*)?office",
    re.IGNORECASE,
)
_REMOTE_TEXT = re.compile(
    r"\b(fully remote|100% remote|remote[- ]first|this (?:role|position) is remote|remote position|work from anywhere in the US)\b",
    re.IGNORECASE,
)


def work_mode(job) -> tuple[str, str]:
    """(mode, source). Feed field first, then location text, then description."""
    hint = job.workplace_hint.lower().replace("-", "").replace(" ", "")
    if hint in {"remote", "hybrid", "onsite"}:
        return hint, "feed"
    loc = job.location.lower()
    if "hybrid" in loc:
        return "hybrid", "location"
    if "remote" in loc:
        return "remote", "location"
    if _HYBRID_TEXT.search(job.description):
        return "hybrid", "description"
    if _REMOTE_TEXT.search(job.description):
        return "remote", "description"
    return "onsite", "default"


def location_group(location: str, mode: str, settings: dict) -> str:
    """Group comes from the location text first: a remote role that lists
    Boston is still "Boston area". Work mode only decides the rest."""
    towns = settings["locations"]["boston_area"]
    if any(re.search(rf"\b{re.escape(t)}\b", location, re.IGNORECASE) for t in towns):
        return "Boston area"
    if mode == "remote":
        return "Remote (US)"
    return "Hybrid (US)" if mode == "hybrid" else "Onsite (US)"


# ================================================================ sponsorship

TIER1_PHRASES = [
    "unable to sponsor", "will not sponsor", "do not sponsor", "does not sponsor",
    "without sponsorship now or in the future", "ineligible for visa sponsorship",
    "not able to provide sponsorship", "no visa sponsorship",
]
TIER2_TRIGGERS = ["us citizen", "citizens only", "security clearance", "itar", "export control"]
TIER2_CONTEXT = re.compile(r"\b(must|required|requires|only|eligib\w* to obtain)\b", re.IGNORECASE)
EEO_SKIP = ["without regard to", "equal opportunity", "regardless of", "protected veteran"]


def sponsorship_no(description: str) -> tuple[bool, str, list[dict]]:
    """Does the description say the employer won't sponsor?

    Returns (is_no, evidence, tier2_log). tier2_log records every sentence that
    contained a Tier 2 trigger and what we decided, so precision can be checked.
    """
    text = description.lower()
    for phrase in TIER1_PHRASES:
        if phrase in text:
            return True, phrase, []

    log = []
    for sentence in split_sentences(description):
        low = sentence.lower()
        trigger = next((t for t in TIER2_TRIGGERS if re.search(rf"\b{t}", low)), None)
        if not trigger:
            continue
        if any(skip in low for skip in EEO_SKIP):
            log.append({"decision": "skipped_eeo", "trigger": trigger, "sentence": sentence})
        elif TIER2_CONTEXT.search(sentence):
            log.append({"decision": "tagged_no", "trigger": trigger, "sentence": sentence})
            return True, sentence, log
        else:
            log.append({"decision": "no_context", "trigger": trigger, "sentence": sentence})
    return False, "", log


# ================================================================ PERM notices

_PERM_TEXT_MARKERS = {
    "or foreign equivalent": re.compile(r"or (?:a )?foreign equivalent", re.IGNORECASE),
    "knowledge gained prior/concurrently": re.compile(r"gained prior to or concurrently with", re.IGNORECASE),
    "unanticipated client locations": re.compile(r"unanticipated (?:client )?locations", re.IGNORECASE),
    "telecommuting permitted": re.compile(
        r"telecommuting (?:is )?permitted|may telecommute from any(?:where| location)", re.IGNORECASE),
    "reference job code": re.compile(
        r"\b(?i:reference|ref)\.?\s*[:#]?\s*(?:(?i:job\s+)?(?i:code|number|no\.?|#)\s*[:#]?\s*)?[A-Z0-9-]*\d{3,}"),
}
_CONCLUSIVE = re.compile(r"experience in the job offered", re.IGNORECASE)
_STREET_ADDRESS = re.compile(
    r"\b\d{2,6}\s+[\w .'-]+?\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|drive|dr\.?|"
    r"parkway|pkwy|lane|ln\.?|way|plaza|place)\b.*\b(?:suite|ste\.?|building|bldg\.?|floor|fl\.?)\s*\w+",
    re.IGNORECASE,
)
_EXACT_DOLLAR = re.compile(r"(?<![-–—]\s)(?<![-–—])(?<!to\s)\$(\d{1,3}(?:,\d{3})+)(?:\.\d{2})?(?!\s*(?:-|–|—|to)\s*\$)")
_EMAIL_APPLY = re.compile(
    r"(?:send|email|e-mail|mail|submit|forward)\b[^.\n]{0,80}\b(?:resume|cv)s?\b[^.\n]{0,80}[\w.+-]+@[\w-]+\.[\w.]+"
    r"|(?:resume|cv)s?\b[^.\n]{0,60}\bto\s+[\w.+-]+@[\w-]+\.[\w.]+",
    re.IGNORECASE,
)


def perm_markers(job) -> tuple[bool, list[str]]:
    """(is_perm_likely, markers found). Flag at 2+ markers, or on the
    near-conclusive phrase alone, or on the board's own PERM flag."""
    text = job.description
    markers = []
    if job.perm_flag:
        markers.append("board PERM flag")
    if _CONCLUSIVE.search(text):
        markers.append("experience in the job offered")
    markers += [name for name, rx in _PERM_TEXT_MARKERS.items() if rx.search(text)]
    if _EMAIL_APPLY.search(text) and "reference job code" not in markers:
        markers.append("apply by email")
    if _STREET_ADDRESS.search(job.location):
        markers.append("street address in location")
    for m in _EXACT_DOLLAR.finditer(text):
        value = int(m.group(1).replace(",", ""))
        if value >= 20000 and value % 1000:
            markers.append(f"exact salary ${m.group(1)}")
            break

    conclusive = "board PERM flag" in markers or "experience in the job offered" in markers
    return conclusive or len(markers) >= 2, markers
