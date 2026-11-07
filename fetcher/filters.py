"""Title, seniority, location, sponsorship and PERM rules.

Everything here is plain regex over feed fields and description text.
"""

from __future__ import annotations

import re

from uscis import normalize_company

# ================================================================ titles

def title_passes(title: str, settings: dict, extra_keep: list[str] | tuple = ()) -> bool:
    """Keep analytics-family titles; drop senior/lead/manager etc. and internships.

    `drop_unless` drops a word unless one of its allowed phrases is also in the
    title, e.g. "engineer" is dropped except in "analytics engineer".
    `extra_keep` adds keep phrases for one company (cap-exempt employers also
    keep "research analyst", "data coordinator"...); the drop list still applies.
    """
    t = title.lower()
    titles = settings["titles"]
    keep = any(re.search(rf"\b{re.escape(k)}\b", t) for k in [*titles["keep"], *extra_keep])
    drop = any(re.search(rf"\b{re.escape(d)}\b", t) for d in titles["drop"])
    for word, allowed in titles.get("drop_unless", {}).items():
        if re.search(rf"\b{re.escape(word)}s?\b", t) and not any(
            re.search(rf"\b{re.escape(a)}s?\b", t) for a in allowed
        ):
            drop = True
    return keep and not drop


_NUMBER_WORDS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split())}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
# The first number of: "8+ years", "8 or more years", "eight (8) years",
# "5-7 years", "5 to 7 years", "8+ yrs", "8 years'", optionally after
# "minimum of" / "at least" (which need no special handling: the number follows).
_YEARS_RE = re.compile(
    rf"(?<![\w.$]){_NUM}\s*(?:\(\s*\d{{1,2}}\s*\)\s*)?(?:\+|\s*plus)?\s*"
    rf"(?:(?:-|–|—|to)\s*{_NUM}\s*\+?\s*)?(?:or more\s+)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_PREFERRED_RE = re.compile(r"\b(preferred|nice to have|bonus|a plus|ideally)\b", re.IGNORECASE)
_EXPERIENCE_WORDS = re.compile(r"\b(experience|background|track record)\b", re.IGNORECASE)

# Section headings, for finding where requirements are stated
_REQUIRED_HEAD = re.compile(
    r"^(?:basic |minimum |required |key )?(?:qualifications|requirements|"
    r"what you(?:'|’)ll need|what you need|what we(?:'|’)re looking for|what we need to see|"
    r"who you are|you have|what you have|you bring|what you bring|about you|experience|must haves?|"
    r"skills(?: and| &) (?:experience|qualifications))\b",
    re.IGNORECASE,
)
# "Additional Qualifications (and Skills)" lists extras beyond the basic requirements.
_PREFERRED_HEAD = re.compile(
    r"\b(preferred|nice to have|bonus|plus|stand out|desired)\b|^additional\s+(?:qualifications|skills|requirements)\b",
    re.IGNORECASE)
_OTHER_HEAD = re.compile(
    r"^(?:responsibilities|what you(?:'|’)ll do|about (?:us|the (?:role|team|company|job))|benefits|perks|"
    r"what we offer|compensation|pay|salary|equal (?:employment )?opportunity|our values|why join|"
    r"location|the role|your role|job description|overview)\b",
    re.IGNORECASE,
)


# Inside a mixed section, the words that make one line a preference
_PREFERRED_LINE = re.compile(r"\b(preferred|nice to have|a plus|ideally)\b", re.IGNORECASE)


def _is_mixed_heading(head: str, preferred: re.Match) -> bool:
    """"Education & Preferred Qualifications", "Required and Preferred
    Qualifications": a requirement part joined to the preferred word by
    &/and/,/slash. "Preferred Skills and Experience" (word first) is not."""
    return bool(re.search(r"\w.*(?:&|\band\b|/|,)", head[:preferred.start()], re.IGNORECASE))


def _section_text(description: str, keep: str) -> str:
    """Lines by section. keep="required": under requirement headings
    ("Qualifications", "What we need to see"); "preferred": under preferred
    ones ("Nice to have", "Ways to stand out"); "not_preferred": everything
    outside preferred sections. A mixed heading counts as requirements, and
    only its lines that say "preferred", "a plus" etc. count as preferred.
    Bulleted lines are never headings."""
    lines, state = [], None
    for line in description.splitlines():
        head = line.strip().rstrip(":").strip()
        if "(" in head and not head.startswith(("-", "•", "*")) and not (len(head) <= 60 and len(head.split()) <= 8):
            # "Desired Qualifications (We're committed to ... apply anyway)":
            # a heading followed by a parenthetical note. Judge the part before it.
            pre = head.split("(")[0].strip().rstrip(":").strip()
            if 0 < len(pre.split()) <= 5 and (
                    _PREFERRED_HEAD.search(pre) or _REQUIRED_HEAD.match(pre) or _OTHER_HEAD.match(pre)):
                head = pre
        if (0 < len(head) <= 60 and len(head.split()) <= 8 and not head.endswith(".")
                and not head.startswith(("-", "•", "*"))):
            preferred = _PREFERRED_HEAD.search(head)
            if preferred and _is_mixed_heading(head, preferred):
                state = "mixed"
                continue
            if preferred:
                state = "preferred"
                continue
            if _REQUIRED_HEAD.match(head):
                state = "required"
                continue
            if _OTHER_HEAD.match(head):
                state = None
                continue
        line_state = state
        if state == "mixed":
            line_state = "preferred" if _PREFERRED_LINE.search(line) else "required"
        if (line_state == keep) or (keep == "not_preferred" and line_state != "preferred"):
            lines.append(line)
    return "\n".join(lines)


_PREF_WORD = r"(?:(?:strongly|highly)\s+)?(?:preferred|preferably|desired|ideally|a plus|nice to have|bonus)"
_PREF_BEFORE = re.compile(rf"\b{_PREF_WORD}\b\s*:?(?:\s+\w+){{0,2}}\s*$", re.IGNORECASE)
_PREF_AFTER = re.compile(rf"(?:\s+\w+){{0,3}}?\s+(?:(?:is|are)\s+)?{_PREF_WORD}\b", re.IGNORECASE)
_PREF_TRAILING = re.compile(
    r"(?:\b(?:strongly|highly)\s+preferred|\b(?:is|are)\s+(?:preferred|desired|a plus)|\ba plus|\bnice to have)\W*$",
    re.IGNORECASE)


def _is_preferred(sentence: str, m: re.Match, sole_figure: bool = False) -> bool:
    """True when a preferred word applies to the YEARS themselves, not to the
    domain around them. Three shapes count:
      - just before the figure: "Ideally 5+ years", "preferably 3 years";
      - just after it, in the same clause: "3+ years preferred", "5 years of
        experience is a plus";
      - at the very end of a bullet with one figure, when it is clearly about the
        whole requirement: "Minimum of 3 years in billing, or analysis, strongly
        preferred" ("strongly/highly preferred", "is/are preferred", "a plus",
        "nice to have").
    Anything in parentheses is a hedge on the content, and so is "ideally on
    data" in a later clause: "7+ years of supply chain experience, ideally on
    data" and "3+ years in fraud (fintech or lending preferred)" stay required.
    With several figures in a bullet only the first two shapes apply, so "5+
    years required, 8+ years preferred" keeps the 5."""
    masked = re.sub(r"\([^)]*\)", lambda x: " " * len(x.group()), sentence)
    before = masked[max(masked.rfind(c, 0, m.start()) for c in ",;.(") + 1:m.start()]
    if _PREF_BEFORE.search(before):
        return True
    ends = [i for i in (masked.find(c, m.end()) for c in ",;.(") if i != -1]
    if _PREF_AFTER.match(masked[m.end():min(ends, default=len(masked))]):
        return True
    return sole_figure and bool(_PREF_TRAILING.search(masked))


# "5+ years in Sales Ops", "3 years of SQL", "2 years as an analyst": a
# requirement even without the word "experience". "for the last 3 years",
# "built 20-40 years ago" and "3 years in a row" are history, not requirements.
_REQUIREMENT_AFTER = re.compile(r"\s+(?:in|of|as|with|working|doing|across)\b(?!\s+a\s+row)", re.IGNORECASE)
_HISTORY_BEFORE = re.compile(r"\b(?:for|last|past|over|than|spent|since|within)\s+(?:the\s+)?(?:last\s+|past\s+)?$", re.IGNORECASE)


def _reads_as_requirement(sentence: str, m: re.Match) -> bool:
    return bool(_REQUIREMENT_AFTER.match(sentence, m.end())) and not _HISTORY_BEFORE.search(sentence[:m.start()])


_EQUIVALENCY = re.compile(
    r"\bequivalent\s+(?:combination|education|experience)\b|\bcombination\s+of\s+(?:education|training|study)\b",
    re.IGNORECASE)
_DEGREE_WORD = re.compile(
    r"\b(?:bachelor|master|degree|diploma|post-secondary|ged|high school)\w*", re.IGNORECASE)
_ALTERNATIVE_OR = re.compile(r"(?<![/\w])or\b", re.IGNORECASE)
_NOT_MY_DEGREE = re.compile(r"\b(?:ph\.?\s?d|doctorate|doctoral)\b", re.IGNORECASE)


def _sentence_years(sentence: str, need_experience_word: bool) -> int | None:
    """One years figure per sentence (a bullet is one sentence).

    Figures joined by "or" are alternatives ("5 years, or 2 years with a
    Master's"): the lowest one the candidate can meet counts, so an option tied
    to a doctorate is ignored unless it is the only one. Figures not joined by
    "or" are all required, even when one is part of another ("3 to 4+ years in
    a data-heavy role, including 2+ in consulting and 1+ at a startup"): the
    highest counts. "and/or" between two items is not an alternative."""
    found = []  # (n, start, end)
    matches = list(_YEARS_RE.finditer(sentence))
    for m in matches:
        if need_experience_word and not (_EXPERIENCE_WORDS.search(sentence) or _reads_as_requirement(sentence, m)):
            continue
        if _is_preferred(sentence, m, sole_figure=len(matches) == 1):
            continue
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMBER_WORDS[m.group(1).lower()]
        if n <= 15:  # "18 years or older" is an age, "25 years" a company history
            found.append((n, m.start(), m.end()))
    if not found:
        return None
    groups = [[0]]
    for i in range(1, len(found)):
        if _ALTERNATIVE_OR.search(sentence[found[i - 1][2]:found[i][1]]):
            groups[-1].append(i)
        else:
            groups.append([i])
    # An option's own clause (between commas, semicolons and "or") names the
    # degree it needs: "PhD with 1-3 years" and "2 years with a PhD" alike.
    bounds = [m.start() for m in re.finditer(r"[,;]|(?<![/\w])or\b", sentence, re.IGNORECASE)]
    values = []
    for g in groups:
        options = []
        for i in g:
            _, start, end = found[i]
            left = max((b for b in bounds if b < start), default=0)
            right = min((b for b in bounds if b >= end), default=len(sentence))
            options.append((found[i][0], bool(_NOT_MY_DEGREE.search(sentence[left:right]))))
        reachable = [n for n, phd in options if not phd]
        values.append(min(reachable) if reachable else max(n for n, _ in options))
    return max(values)


_CONTINUES_PREVIOUS = re.compile(r"^[-•*\s]*(?:or|in the alternative|alternatively)\b", re.IGNORECASE)


def _year_sentences(text: str, need_experience_word: bool) -> list[tuple[str, int]]:
    """(sentence, figure) per sentence with one. A sentence that opens with "or" or "in the
    alternative" is the next option of the one before it ("BS plus 3 years;"
    then "or MS plus 1 year"), so the two are read as one sentence."""
    sentences: list[str] = []
    for sentence in split_sentences(text):
        if sentences and _CONTINUES_PREVIOUS.match(sentence):
            sentences[-1] += " " + sentence
        else:
            sentences.append(sentence)
    pairs = []
    for sentence in sentences:
        n = _sentence_years(sentence, need_experience_word)
        if n is not None:
            pairs.append((sentence, n))
    return pairs


def _year_figures(text: str, need_experience_word: bool) -> list[int]:
    return [n for _, n in _year_sentences(text, need_experience_word)]


def min_years_required(description: str) -> int | None:
    """Required years: the HIGHEST figure across separate sentences/bullets,
    since each bullet is its own requirement ("3 to 4+ years in data" and "1+
    years in operations" means 3). Within one sentence, see _sentence_years:
    alternatives take the lowest option the candidate meets.

    Inside a requirements section any years phrase counts ("8+ years in
    consulting"). Without one, or if it states no figure, a sentence must
    also mention experience, so "for more than 25 years" in a company blurb
    is ignored."""
    text = _section_text(description, "required")
    pairs = _year_sentences(text, need_experience_word=False)
    if not pairs:
        text = _section_text(description, "not_preferred")
        pairs = _year_sentences(text, need_experience_word=True)
    if not pairs:
        return None
    figures = [n for _, n in pairs]
    # "Bachelor's and 2 years" / "Four years of experience" / "equivalent
    # combination of education and experience": bullets that are degree-versus-
    # experience alternatives, so the lowest counts (the candidate holds the
    # degrees). Needs both signs: a degree bullet that carries a figure AND an
    # equivalency line. The line alone is boilerplate ("An equivalent combination
    # of education and experience may substitute"), so it never lowers a figure.
    if _EQUIVALENCY.search(text) and any(_DEGREE_WORD.search(s) for s, _ in pairs):
        return min(figures)
    return max(figures)


def min_years_preferred(description: str) -> int | None:
    """Smallest years figure under a preferred heading ("Preferred
    Qualifications"). Shown on the card only; never used to drop a job."""
    figures = _year_figures(_section_text(description, "preferred"), need_experience_word=False)
    return min(figures) if figures else None


def experience_passes(years: int | None, settings: dict) -> bool:
    """Keep unless the minimum required years exceeds max_years_required (3):
    "3-5 years" stays, "4+ years" and "5-7 years" go. No stated figure keeps."""
    return years is None or years <= settings["max_years_required"]


# ================================================================ fit: gaps, mismatch, focus

def _requirements_text(description: str) -> str:
    """The required section, or, when the posting has no section headings,
    everything outside a preferred section (same fallback as the years rule)."""
    return _section_text(description, "required") or _section_text(description, "not_preferred")


def skill_gaps(description: str, settings: dict) -> tuple[list[str], list[str]]:
    """(required gaps, preferred-only gaps): gap_skills named in the required
    section, and those named only under a preferred heading. Mentions in other
    sections (company blurb, benefits) don't count."""
    required_text = _requirements_text(description)
    preferred_text = _section_text(description, "preferred")
    case_sensitive = set(settings.get("gap_case_sensitive", []))
    required, preferred = [], []
    for name, pattern in settings["gap_skills"].items():
        if name.startswith("_"):
            continue
        rx = re.compile(pattern, 0 if name in case_sensitive else re.IGNORECASE)
        if rx.search(required_text):
            required.append(name)
        elif rx.search(preferred_text):
            preferred.append(name)
    return required, preferred


def gap_adjusted_match(match_pct: int, required_gaps: list[str], settings: dict) -> int:
    """Match % minus gap_penalty per required gap, never below 0."""
    return max(0, match_pct - settings["gap_penalty"] * len(required_gaps))


def _snippet(text: str, m: re.Match) -> str:
    return " ".join(text[max(0, m.start() - 40):m.end() + 40].split())


def role_mismatch(title: str, description: str, settings: dict) -> list[dict]:
    """[{"type", "phrase", "where", "context"}] for each mismatch type that
    applies: a title pattern, or min_hits different requirement patterns.
    A type with "scope": "description" (federal) reads the whole description,
    since clearance lines often sit in a header or footer. Empty when the job
    is in my field."""
    requirements = _requirements_text(description)
    found = []
    for kind, cfg in settings["mismatch"].items():
        if kind.startswith("_"):
            continue
        whole = cfg.get("scope") == "description"
        text, where = (description, "description") if whole else (requirements, "requirements")
        title_hits = [m for p in cfg["title"] if (m := re.search(p, title, re.IGNORECASE))]
        req_hits = [m for p in cfg["requirements"] if (m := re.search(p, text, re.IGNORECASE))]
        phrase = lambda m: " ".join(m.group(0).split())  # "Clearance Required:\nActive" on one line
        if title_hits:
            found += [{"type": kind, "phrase": phrase(m), "where": "title", "context": title} for m in title_hits]
        elif len(req_hits) >= cfg["min_hits"]:
            found += [{"type": kind, "phrase": phrase(m), "where": where, "context": _snippet(text, m)}
                      for m in req_hits]
    return found


def focus_function(title: str, settings: dict) -> bool:
    """Revenue/sales/commercial operations or BI in the title: Best fit ranks
    these above other roles with the same match %."""
    return any(re.search(p, title, re.IGNORECASE) for p in settings["focus_functions"]["patterns"])


_TITLE_TAIL = re.compile(r"\s*(?:\(.*?\)|\[.*?\]|\s[-–—|,:]\s.*|,.*|\s#?\b[A-Z]{0,4}-?\d{4,}\b.*)$")


def people_search(title: str, settings: dict) -> dict:
    """Keywords for the dashboard's "Find people" search links (links only;
    nothing is fetched). manager: who likely leads this team, from the first
    matching title family in settings; peer: the job's own title without
    its tail ("Data Analyst I - Revenue (Remote)" -> "Data Analyst I")."""
    cfg = settings["people_search"]
    manager = next((phrases for pattern, phrases in cfg["manager"]
                    if re.search(pattern, title, re.IGNORECASE)), cfg["default_manager"])
    peer = title
    for _ in range(3):  # tails can stack: "Analyst (Remote) - Revenue"
        peer = _TITLE_TAIL.sub("", peer).strip() or peer
    return {"manager": manager, "recruiter": cfg["recruiter"], "peer": peer, "alumni": cfg["alumni"]}


def experience_band(title: str, years: int | None, settings: dict) -> tuple[str, list[str]]:
    """("target" | "stretch", reasons). Stretch: the minimum required years
    reaches stretch_years (3), or the title has a stretch level (II, III).
    Everything else that survived the filters is target. A stretch signal
    wins over an entry-level title: "Associate Analyst" asking 3 years is
    stretch. Senior/Sr./Principal never get here (the title filter drops them)."""
    cfg = settings["bands"]
    stretch, target = [], []
    if years is not None and years >= cfg["stretch_years"]:
        stretch.append(f"{years} years required")
    for name, pattern in cfg["stretch_title"].items():
        if re.search(pattern, title):  # case-sensitive: "II", not "ii"
            stretch.append(f"title: {name}")
    if stretch:
        return "stretch", stretch
    target.append("no years stated" if years is None else f"{years} years required")
    for name, pattern in cfg["target_title"].items():
        if re.search(pattern, title, re.IGNORECASE):
            target.append(f"title: {name}")
    return "target", target


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


# Foreign cities with no well-known US namesake: foreign even beside a
# state-like code, so "Gurugram IN" is India, not Indiana.
_FOREIGN_CITY_RE = re.compile(
    r"\b(Gurugram|Gurgaon|Noida|New Delhi|Bengaluru|Bangalore|Hyderabad|Pune|Mumbai|Chennai|Kolkata|"
    r"Ahmedabad|Edinburgh|Glasgow|Munich|München|Frankfurt|Düsseldorf|Amsterdam|Rotterdam|Madrid|"
    r"Barcelona|Lisbon|Milan|Zurich|Zürich|Geneva|Stockholm|Copenhagen|Oslo|Helsinki|Warsaw|Krakow|"
    r"Kraków|Wroclaw|Prague|Budapest|Bucharest|Kyiv|Tel Aviv|Dubai|Abu Dhabi|Riyadh|Doha|Cairo|Lagos|"
    r"Nairobi|Cape Town|Johannesburg|Singapore|Kuala Lumpur|Jakarta|Bangkok|Manila|Ho Chi Minh|Hanoi|"
    r"Shanghai|Beijing|Shenzhen|Taipei|Seoul|Tokyo|Osaka|Sydney|Auckland|Montreal|Montréal|Ottawa|"
    r"Calgary|Mexico City|Guadalajara|Monterrey|São Paulo|Sao Paulo|Rio de Janeiro|Buenos Aires|"
    r"Bogotá|Bogota|Medellín|Medellin)\b",
    re.IGNORECASE,
)
# Foreign cities that share a name with US places ("Dublin, CA", "Melbourne,
# FL", "Paris, TX"): foreign only when no US state is named alongside.
_AMBIGUOUS_CITY_RE = re.compile(
    r"\b((?<!New )London|Manchester|Dublin|Paris|Berlin|Athens|Delhi|Lima|Toronto|Vancouver|"
    r"Melbourne|Hamburg|Birmingham)\b",
    re.IGNORECASE,
)
_CA_PROVINCE_RE = re.compile(r",\s*(ON|BC|QC|AB|MB|NS|NB|NL|PE|SK)\b")  # "Toronto, ON"
# Country codes at the end of a location that can't be a US state. IN, CA,
# DE, ID and the like are left out on purpose: they are also state codes.
_FOREIGN_CODE_RE = re.compile(
    r"(?:,|\s)\s*(GB|IE|FR|ES|NL|BE|CH|AT|DK|PL|PT|CZ|HU|RO|AE|SG|MY|JP|CN|HK|TW|KR|AU|NZ|BR|MX|PH|"
    r"TH|VN|ZA|NG|KE|EG|TR|UA|IND|GBR|CAN|DEU|FRA|ESP|ITA|NLD|IRL|POL|PRT|CHE|SGP|JPN|CHN|AUS|BRA|"
    r"MEX|ISR|PHL|ARE)\s*$"
)

# Separators between locations in a multi-location string,
# e.g. "Remote, Canada; Remote, United States" or "Remote - US or Canada".
_LOCATION_SPLIT = re.compile(r"\s*(?:;|\||\n|\s/\s|\bor\b)\s*")


def _place_is_foreign(part: str) -> bool:
    """One location (or a title): names a foreign country, city or code."""
    if (_FOREIGN_COUNTRY_RE.search(part) or _FOREIGN_CITY_RE.search(part)
            or _CA_PROVINCE_RE.search(part) or _FOREIGN_CODE_RE.search(part)):
        return True
    return bool(_AMBIGUOUS_CITY_RE.search(part)) and not _US_TEXT_RE.search(part)


def names_foreign_place(text: str) -> bool:
    """True when any listed place is foreign, e.g. "Business Consultant
    (Denmark)" or "Gurugram IN". Overrides a feed that says US."""
    return any(_place_is_foreign(part) for part in _LOCATION_SPLIT.split(text) if part)


def lists_us_location(location: str) -> bool:
    """True when any one of the listed locations is in the US."""
    return any(_US_TEXT_RE.search(part) and not _place_is_foreign(part)
               for part in _LOCATION_SPLIT.split(location) if part)


def us_status(title: str, location: str, country_hint: str) -> str:
    """'us', 'non_us', or 'uncertain' (e.g. a bare "Remote").

    A foreign place in the title always drops the job (the Veeva Denmark
    case). In the location it drops the job only when no listed location is
    in the US: "Remote, Canada; Remote, United States" is kept."""
    if names_foreign_place(title):
        return "non_us"
    if names_foreign_place(location):
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
# Flexible Tier 1 wordings, matched anywhere in the description. Section never
# matters here: a no-sponsorship line under "Preferred" or "Additional
# Qualifications" is still a no. (Those headings only affect years.)
_SPONSOR_KIND = r"(?:(?:visa|employer|employment|immigration|work)\s+)*"
TIER1_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    # "without sponsorship", "without the need for visa sponsorship", "without requiring employer sponsorship"
    rf"\bwithout\s+(?:the\s+need\s+(?:for|of)\s+|needing\s+|requiring\s+)?(?:any\s+)?{_SPONSOR_KIND}sponsorship\b(?!\s+for\s+(?:an?\s+)?(?:export|license))",
    # "not able to sponsor", "unable to provide sponsorship", "cannot offer visa sponsorship"
    rf"\b(?:unable|not\s+able|cannot|can\s?not|can(?:'|’)?t)\s+to\s+(?:provide\s+|offer\s+|extend\s+|support\s+)?{_SPONSOR_KIND}sponsor(?:ship)?\b",
    rf"\bcannot\s+(?:provide\s+|offer\s+|extend\s+|support\s+)?{_SPONSOR_KIND}sponsor(?:ship)?\b",
    # "will not sponsor", "does not provide sponsorship"
    r"\b(?:will|do|does|did)\s+not\s+sponsor\b",
    rf"\b(?:will|do|does|did)\s+not\s+(?:provide|offer|extend|support)\s+{_SPONSOR_KIND}sponsorship\b",
    # "not eligible for sponsorship", "sponsorship is not available", "no visa sponsorship"
    rf"\bnot\s+(?:be\s+)?(?:eligible|available)\s+for\s+{_SPONSOR_KIND}sponsorship\b",
    r"\bsponsorship\s+(?:is\s+)?(?:not\s+(?:available|offered|provided)|unavailable)\b",
    r"\bno\s+(?:(?:visa|employer|employment|immigration)\s+)+sponsorship\b",
    r"\bno\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered)\b",
)]
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
    for rx in TIER1_PATTERNS:
        m = rx.search(description)
        if m:
            return True, " ".join(m.group(0).split()).lower(), []

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


# ================================================================ staffing firms

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
# Sentences that push staffing firms away rather than come from one:
# "This is a direct-hire role and is not open to C2C", "We do not accept
# unsolicited resumes from staffing agencies". A phrase in one doesn't count.
_NOT_STAFFING = re.compile(
    r"\bdirect[- ]hire\b|\bnot open to\b|\bno third[- ]part(?:y|ies)\b|\bunsolicited\b|\bplacement fees?\b"
    r"|\b(?:will not|won't|do not|does not|don't|doesn't|not) (?:be )?(?:accept|consider|pay)\w*",
    re.IGNORECASE)
_SPONSOR_WORDING = re.compile(r"\bsponsor", re.IGNORECASE)


def _squash(name: str) -> str:
    """"Tech Tammina, Inc." -> "techtammina": legal suffixes, spaces and
    punctuation dropped, for comparing company names."""
    return normalize_company(name).replace(" ", "")


def staffing_reasons(description: str, naics_sector: str, settings: dict, company: str = "") -> list[str]:
    """Why a job looks like it comes from a staffing firm, e.g.
    ["C2C: Contract role, C2C or W2 accepted."]. Empty means not staffing.
    The company-level rule (2+ flagged jobs) runs later, in fetch.py."""
    cfg = settings["staffing"]
    reasons = []
    if company and _squash(company) in {_squash(c) for c in settings.get("staffing_companies", [])}:
        reasons.append("listed in staffing_companies")
    if naics_sector and naics_sector in cfg["naics_sectors"]:
        reasons.append(f"USCIS NAICS sector {naics_sector}")
    sentences = _SENTENCE_SPLIT.split(description or "")
    for name, pattern in cfg["phrases"].items():
        rx = re.compile(pattern, re.IGNORECASE)
        for sentence in sentences:
            if rx.search(sentence) and not _NOT_STAFFING.search(sentence):
                reasons.append(f"{name}: {' '.join(sentence.split())[:160]}")
                break
    # Visa lists ("OPT/CPT/H1/EAD/GC/Citizen candidates") are checked per line,
    # not per sentence, so "U.S. Citizen" isn't cut at the abbreviation.
    visa = cfg["visa_list"]
    tokens = {name: re.compile(p) for name, p in visa["tokens"].items()}
    for line in (description or "").splitlines():
        named = [name for name, rx in tokens.items() if rx.search(line)]
        if len(named) >= visa["min"] and not _SPONSOR_WORDING.search(line) and not _NOT_STAFFING.search(line):
            reasons.append(f"visa list ({', '.join(named)}): {' '.join(line.split())[:160]}")
            break
    return reasons


def weak_only(reasons: list[str], settings: dict) -> bool:
    """True when a job's only signal is a single weak phrase (duration,
    contract to hire, "our client"), so it is worth checking by hand."""
    weak = settings["staffing"]["weak_phrases"]
    return len(reasons) == 1 and reasons[0].split(":")[0] in weak
