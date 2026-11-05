"""USCIS H-1B Employer Data Hub -> compact lookup of sponsoring employers.

Run once after downloading CSVs into data/uscis/:

    python fetcher/uscis.py data/uscis/*.csv

Writes data/uscis/h1b_lookup.json:
{normalized employer name: [approvals, last_fy, 2-digit NAICS sector]}.
Only employers with at least one approval are kept; tax ID, city and ZIP are dropped.
"""

from __future__ import annotations

import bisect
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOKUP_PATH = ROOT / "data" / "uscis" / "h1b_lookup.json"

_SUFFIXES = re.compile(r"\b(inc|incorporated|corp|corporation|llc|l l c|llp|l l p|lp|ltd|limited|co|plc)\b")
_DBA = re.compile(r"\b(?:dba|d b a)\b")


def normalize_company(name: str) -> str:
    """Lowercase, drop punctuation and legal suffixes, collapse whitespace."""
    n = name.lower().replace("&", " and ")
    n = re.sub(r"[^\w\s]", " ", n)
    n = _SUFFIXES.sub(" ", n)
    return " ".join(n.split())


def _open_export(path: str):
    """USCIS exports vary: older files are UTF-8 comma-separated, newer
    Crosstab exports are UTF-16 tab-separated despite the .csv name."""
    with open(path, "rb") as f:
        head = f.read(4)
    encoding = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    f = open(path, encoding=encoding, errors="replace", newline="")
    delimiter = "\t" if "\t" in f.readline() else ","
    f.seek(0)
    return f, delimiter


def build(csv_paths: list[str]) -> dict:
    employers: dict[str, list[int]] = {}
    for path in csv_paths:
        f, delimiter = _open_export(path)
        with f:
            for row in csv.DictReader(f, delimiter=delimiter):
                # Header names change across years and carry trailing spaces;
                # match them loosely. The "Line by line" row-number column is ignored.
                cols = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                raw = cols.get("employer") or cols.get("employer (petitioner) name") or ""
                fy = int(cols.get("fiscal year") or 0)
                # "56 - Administrative and Support..." -> "56". The export only
                # carries the 2-digit NAICS sector, never the full code.
                sector = (cols.get("industry (naics) code") or "").split(" ")[0]
                approvals = sum(int(v.replace(",", "")) for k, v in cols.items()
                                if "approval" in k and v.replace(",", "").isdigit())
                if not raw or approvals == 0:
                    continue
                # "ACME LLC DBA ACME ANALYTICS" counts under both names.
                for part in _DBA.split(raw.lower()):
                    key = normalize_company(part)
                    if not key:
                        continue
                    entry = employers.setdefault(key, [0, 0, {}])
                    entry[0] += approvals
                    entry[1] = max(entry[1], fy)
                    if sector:
                        entry[2][sector] = entry[2].get(sector, 0) + approvals
    # Keep the sector with the most approvals ("" when none was listed)
    for entry in employers.values():
        entry[2] = max(entry[2], key=entry[2].get) if entry[2] else ""
    return employers


class Lookup:
    """Match a company name against the USCIS employers.

    Order: exact normalized match, alias from settings, then a prefix match on
    the space-free form (so "walmart" finds "wal mart associates"). Prefix
    matches are fuzzy and get logged as near-misses for review.
    """

    def __init__(self, employers: dict[str, list[int]], aliases: dict[str, str]):
        self.employers = employers
        self.aliases = {normalize_company(k): v for k, v in aliases.items()}
        self._squashed = sorted((k.replace(" ", ""), k) for k in employers)
        self._keys = [s for s, _ in self._squashed]

    @classmethod
    def load(cls, aliases: dict[str, str]) -> "Lookup | None":
        if not LOOKUP_PATH.exists():
            return None
        return cls(json.loads(LOOKUP_PATH.read_text("utf-8"))["employers"], aliases)

    def match(self, company: str) -> tuple[str | None, str]:
        """(matched employer key, how) where how is exact|alias|prefix|none."""
        norm = normalize_company(company)
        if norm in self.employers:
            return norm, "exact"
        if norm in self.aliases and self.aliases[norm] in self.employers:
            return self.aliases[norm], "alias"
        squashed = norm.replace(" ", "")
        if len(squashed) < 5:
            return None, "none"  # too short to prefix-match safely
        i = bisect.bisect_left(self._keys, squashed)
        best = None
        while i < len(self._keys) and self._keys[i].startswith(squashed):
            key = self._squashed[i][1]
            if best is None or self.employers[key][0] > self.employers[best][0]:
                best = key
            i += 1
        return (best, "prefix") if best else (None, "none")

    def naics_sector(self, key: str | None) -> str:
        """2-digit NAICS sector for a matched employer, "" if unknown."""
        entry = self.employers.get(key) if key else None
        return entry[2] if entry and len(entry) > 2 else ""

    def top(self, n: int) -> list[str]:
        """Employer keys with the most approvals."""
        return sorted(self.employers, key=lambda k: -self.employers[k][0])[:n]


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: python fetcher/uscis.py data/uscis/<file>.csv [...]")
    employers = build(argv)
    LOOKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source_files": [Path(p).name for p in argv], "employers": dict(sorted(employers.items()))}
    LOOKUP_PATH.write_text(json.dumps(payload, separators=(",", ":")), "utf-8")
    size_kb = LOOKUP_PATH.stat().st_size / 1024
    print(f"{len(employers):,} employers with approvals -> {LOOKUP_PATH.relative_to(ROOT)} ({size_kb:,.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1:])
