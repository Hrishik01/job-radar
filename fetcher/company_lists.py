"""Fortune 500 and unicorn company lists -> data/company_lists.json, and the
matcher behind the "Fortune 500" and "Unicorn" badges.

    python fetcher/company_lists.py    # re-download both lists (rarely: unicorns change)

Sources, both public pages fetched once through net.py:
- Fortune 500: fortune.com's ranking page embeds the full list as JSON.
- Unicorns: CB Insights' unicorn tracker (private companies valued at $1B+),
  an HTML table.
Only company names are kept, plus each list's date.
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from net import get_text
from uscis import normalize_company

ROOT = Path(__file__).resolve().parent.parent
LISTS_PATH = ROOT / "data" / "company_lists.json"
FORTUNE_URL = "https://fortune.com/ranking/fortune500/"
UNICORN_URL = "https://www.cbinsights.com/research-unicorn-companies"


def parse_fortune(page: str) -> dict:
    """Names ranked 1-500 from the page's embedded Next.js data."""
    raw = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    search = json.loads(raw.group(1))["props"]["pageProps"]["franchiseSearch"]
    names = sorted({i["name"].strip() for i in search["items"] if int(i["rank"]) <= 500})
    published = datetime.fromtimestamp(int(search["dateGmt"]), timezone.utc).date().isoformat()
    return {"title": f"{search['year']} Fortune 500", "source": FORTUNE_URL,
            "published": published, "names": names}


def parse_unicorns(page: str) -> dict:
    """Company column of the tracker table. The page has no 'updated' stamp,
    so the newest 'Date Joined' is recorded as a lower bound on freshness."""
    names, joined = set(), []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 3 or not cells[0]:
            continue
        names.add(cells[0])
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", cells[2]):
            joined.append(datetime.strptime(cells[2], "%m/%d/%Y").date())
    return {"title": "CB Insights unicorn tracker", "source": UNICORN_URL,
            "latest_joined": max(joined).isoformat() if joined else None, "names": sorted(names)}


class ListLookup:
    """Badge matching, same rules as the USCIS Lookup: exact normalized name,
    then an alias from settings. A prefix match either way is only a
    near-miss for the log, never a badge."""

    def __init__(self, lists: dict, aliases: dict[str, str], exclude: dict[str, str] | None = None):
        self.lists = {key: {normalize_company(n) for n in lst["names"]} for key, lst in lists.items()}
        self.aliases = {normalize_company(k): normalize_company(v) for k, v in aliases.items()}
        # Same name, different company (checked by hand): never badged
        self.exclude = {normalize_company(k) for k in (exclude or {})}

    @classmethod
    def load(cls, aliases: dict[str, str], exclude: dict[str, str] | None = None) -> "ListLookup | None":
        if not LISTS_PATH.exists():
            return None
        return cls(json.loads(LISTS_PATH.read_text("utf-8"))["lists"], aliases, exclude)

    def match(self, company: str) -> tuple[dict[str, bool], list[str]]:
        """({list key: badge?}, near-miss lines for the log)."""
        norm = normalize_company(company)
        if norm in self.exclude:
            return {key: False for key in self.lists}, []
        alias = self.aliases.get(norm)
        badges, near = {}, []
        for key, names in self.lists.items():
            badges[key] = norm in names or alias in names
            if badges[key] or len(norm.replace(" ", "")) < 5:
                continue
            squashed = norm.replace(" ", "")
            close = sorted(n for n in names if len(n.replace(" ", "")) >= 5 and
                           (n.replace(" ", "").startswith(squashed) or squashed.startswith(n.replace(" ", ""))))
            if close:
                near.append(f"{key:<10} {company!r} ~ {close[:3]}")
        return badges, near


def main() -> None:
    fortune = parse_fortune(get_text(FORTUNE_URL, delay=2))
    unicorns = parse_unicorns(get_text(UNICORN_URL, delay=2))
    if len(fortune["names"]) < 490 or len(unicorns["names"]) < 500:
        sys.exit(f"Lists look incomplete ({len(fortune['names'])} / {len(unicorns['names'])}); not writing.")
    payload = {"retrieved": date.today().isoformat(),
               "lists": {"fortune500": fortune, "unicorn": unicorns}}
    LISTS_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False), "utf-8")
    print(f"{fortune['title']} (published {fortune['published']}): {len(fortune['names'])} names")
    print(f"Unicorns (newest joined {unicorns['latest_joined']}): {len(unicorns['names'])} names")
    print(f"-> {LISTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
