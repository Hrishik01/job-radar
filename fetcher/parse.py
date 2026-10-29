"""Small text helpers shared by the board adapters and filters."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------- HTML -> text

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """Turn job-description HTML into plain text, keeping line breaks so
    section headings stay on their own lines."""
    if not raw:
        return ""
    # Greenhouse double-escapes its HTML (&lt;p&gt;), so unescape first if needed.
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- URLs

# Query parameters that only track where a click came from. Anything else
# (e.g. gh_jid on company-hosted Greenhouse pages) identifies the job and is kept.
_TRACKING_PARAMS = {
    "gh_src", "trackingid", "source", "src", "ref", "referrer", "lever-source",
    "lever-origin", "fbclid", "gclid", "mc_cid", "mc_eid", "iis", "iisn",
}


def clean_url(url: str) -> str:
    """Strip tracking parameters and fragments from an apply link."""
    if not url:
        return ""
    parts = urlsplit(url)
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


# ---------------------------------------------------------------- salary

HOURS_PER_YEAR = 2080

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?([kK])?"
_RANGE_RE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*(?:USD\s*)?" + _MONEY)
_SINGLE_RE = re.compile(
    _MONEY + r"\s*(?:USD\s*)?(?:per\s+hour|/\s*hour|/\s*hr|an\s+hour|hourly|per\s+year|/\s*year|/\s*yr|annually|per\s+annum)",
    re.IGNORECASE,
)
_HOURLY_HINT = re.compile(r"hour|/\s*hr\b|hourly", re.IGNORECASE)
_NOT_PAY = re.compile(r"revenue|funding|raised|million|billion|valuation|bonus|stipend|relocation", re.IGNORECASE)


def _money(number: str, k: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


def annualize(amount: float, hourly: bool) -> float:
    return round(amount * HOURS_PER_YEAR) if hourly else round(amount)


def parse_salary(text: str) -> dict | None:
    """Find a pay range in free text and return annual USD figures.

    Returns {"min", "max", "hourly"} or None. When a description lists several
    ranges (e.g. per geographic zone) we report the widest span across them.
    """
    if not text:
        return None
    found: list[tuple[float, float, int, int]] = []  # (low, high, match start, match end)

    for m in _RANGE_RE.finditer(text):
        lo, hi = _money(m.group(1), m.group(2)), _money(m.group(3), m.group(4))
        found.append((lo, hi, m.start(), m.end()))
    if not found:
        for m in _SINGLE_RE.finditer(text):
            v = _money(m.group(1), m.group(2))
            found.append((v, v, m.start(), m.end()))

    results = []
    for lo, hi, start, end in found:
        before = text[max(0, start - 40):start]
        after = text[end:end + 30]
        if _NOT_PAY.search(before):
            continue
        hourly = bool(_HOURLY_HINT.search(text[start:end] + after)) or hi < 200
        if not hourly and lo < 20000:
            continue  # monthly figures, small one-off amounts, etc.
        lo_a, hi_a = annualize(lo, hourly), annualize(hi, hourly)
        if lo_a > hi_a or lo_a < 15000 or hi_a > 1_500_000:
            continue
        results.append((lo_a, hi_a, hourly))

    if not results:
        return None
    return {
        "min": min(r[0] for r in results),
        "max": max(r[1] for r in results),
        "hourly": any(r[2] for r in results),
    }


def salary_from_hint(hint: dict | None) -> dict | None:
    """Convert a structured feed salary ({min, max, interval}) to annual USD."""
    if not hint or hint.get("min") is None:
        return None
    if (hint.get("currency") or "USD").upper() != "USD":
        return None
    interval = (hint.get("interval") or "").lower()
    hourly = "hour" in interval
    if not hourly and interval and "year" not in interval and "annual" not in interval:
        return None  # monthly/weekly figures are rare; skip rather than guess
    lo = float(hint["min"])
    hi = float(hint.get("max") or lo)
    return {"min": annualize(lo, hourly), "max": annualize(hi, hourly), "hourly": hourly}
