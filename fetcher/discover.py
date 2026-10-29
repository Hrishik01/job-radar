"""Build data/companies.json from public sources.

    python fetcher/discover.py               # manual + SimplifyJobs + USCIS probes
    python fetcher/discover.py --no-uscis    # skip the slow slug-probing step
    python fetcher/discover.py --top 500     # probe fewer USCIS employers

Sources, in priority order:
  1. Manual entries already in companies.json (source "manual") are kept as-is.
  2. Greenhouse / Lever / Ashby links in the SimplifyJobs new-grad README.
  3. Likely slugs for the top USCIS H-1B employers, probed on each board.

A board counts only if its API returns valid JSON with at least one job.
Probe results are cached in data/cache/discovery.json and re-checked after
settings["discovery"]["recheck_days"] days.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from net import FetchError, get_text, request_json
from uscis import Lookup, normalize_company

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
COMPANIES_PATH = DATA / "companies.json"
PROBE_CACHE = DATA / "cache" / "discovery.json"

PROBE_BOARDS = ("greenhouse", "ashby", "lever")  # preference order when a company has several
LINK_PATTERNS = {
    "greenhouse": re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
}
NOT_SLUGS = {"embed", "jobs", "api", "v1"}
# Words dropped when guessing a short slug ("veeva systems" -> "veeva").
GENERIC_WORDS = {
    "the", "and", "group", "holdings", "technologies", "technology", "services", "systems",
    "solutions", "usa", "us", "america", "americas", "international", "global", "company",
    "enterprises", "consulting", "software", "labs", "north",
}


# ---------------------------------------------------------------- sources

def simplify_boards(settings: dict) -> set[tuple[str, str]]:
    """(board, slug) pairs linked from the SimplifyJobs README."""
    url = settings["discovery"]["simplify_readme_url"]
    text = get_text(url, cache_seconds=24 * 3600)
    found = set()
    for board, rx in LINK_PATTERNS.items():
        for slug in rx.findall(text):
            slug = unquote(slug).strip(".")
            if slug.lower() not in NOT_SLUGS:
                found.add((board, slug))
    return found


def slug_guesses(employer_key: str) -> list[str]:
    tokens = employer_key.split()
    core = [t for t in tokens if t not in GENERIC_WORDS] or tokens
    guesses = ["".join(tokens), "-".join(tokens), "".join(core)]
    return list(dict.fromkeys(g for g in guesses if len(g) >= 3))  # dedupe, keep order


# ---------------------------------------------------------------- probing

def probe(board: str, slug: str, delay: float) -> dict:
    """Ask one board API whether this slug exists. Never raises for a miss."""
    if board == "greenhouse":
        data = request_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                            delay=delay, allow_404=True, retries=2)
        jobs = (data or {}).get("jobs") or []
        name = jobs[0].get("company_name") if jobs else None
    elif board == "lever":
        data = request_json("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
                            delay=delay, allow_404=True, retries=2)
        jobs, name = data or [], None
    else:
        data = request_json("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                            delay=delay, allow_404=True, retries=2)
        jobs, name = (data or {}).get("jobs") or [], None
    return {"ok": bool(jobs), "name": name, "checked": datetime.now(timezone.utc).isoformat()}


def run_probes(pairs: list[tuple[str, str]], cache: dict, settings: dict) -> None:
    """Probe every (board, slug) not freshly cached. One worker per board host,
    so each host still sees requests one at a time with a delay between them."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings["discovery"]["recheck_days"])
    todo: dict[str, list[str]] = {b: [] for b in PROBE_BOARDS}
    for board, slug in pairs:
        hit = cache.get(f"{board}:{slug}")
        if not hit or datetime.fromisoformat(hit["checked"]) < cutoff:
            todo[board].append(slug)
    total = sum(len(v) for v in todo.values())
    print(f"Probing {total:,} board/slug pairs ({len(pairs) - total:,} cached)...")

    lock = threading.Lock()
    done = [0]
    delay = settings["http"]["probe_delay_seconds"]

    def worker(board: str):
        for slug in todo[board]:
            try:
                result = probe(board, slug, delay)
            except FetchError as exc:  # rate limits / server errors: retry next run
                print(f"  {board}/{slug}: {exc}")
                continue
            with lock:
                cache[f"{board}:{slug}"] = result
                done[0] += 1
                if done[0] % 250 == 0:
                    save_cache(cache)
                    print(f"  {done[0]:,}/{total:,} probed")

    with ThreadPoolExecutor(max_workers=len(PROBE_BOARDS)) as pool:
        list(pool.map(worker, PROBE_BOARDS))
    save_cache(cache)


def save_cache(cache: dict) -> None:
    PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROBE_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True), "utf-8")


def names_agree(a: str, b: str) -> bool:
    """Loose check that a board's own company name matches the employer we guessed."""
    na, nb = normalize_company(a), normalize_company(b)
    sa, sb = na.replace(" ", ""), nb.replace(" ", "")
    if sa.startswith(sb) or sb.startswith(sa):
        return True
    ta, tb = set(na.split()), set(nb.split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Build data/companies.json")
    parser.add_argument("--no-uscis", action="store_true", help="skip USCIS slug probing")
    parser.add_argument("--no-simplify", action="store_true", help="skip the SimplifyJobs README")
    parser.add_argument("--top", type=int, help="how many top USCIS employers to probe")
    args = parser.parse_args()

    settings = json.loads((DATA / "settings.json").read_text("utf-8"))
    existing = json.loads(COMPANIES_PATH.read_text("utf-8"))["companies"]
    manual = [c for c in existing if c.get("source") == "manual"]
    cache = json.loads(PROBE_CACHE.read_text("utf-8")) if PROBE_CACHE.exists() else {}

    simplify: set[tuple[str, str]] = set()
    if not args.no_simplify:
        try:
            simplify = simplify_boards(settings)
            print(f"SimplifyJobs README: {len(simplify)} board links")
        except FetchError as exc:
            print(f"SimplifyJobs README unavailable: {exc}")

    uscis_targets: list[tuple[str, list[str]]] = []
    lookup = Lookup.load(settings["uscis_aliases"])
    if not args.no_uscis:
        if lookup is None:
            print("No USCIS lookup yet; skipping USCIS discovery (run fetcher/uscis.py first).")
        else:
            top_n = args.top or settings["discovery"]["uscis_top_n"]
            uscis_targets = [(key, slug_guesses(key)) for key in lookup.top(top_n)]
            print(f"USCIS: probing likely slugs for the top {len(uscis_targets):,} employers")

    pairs = set(simplify)
    for _, guesses in uscis_targets:
        pairs.update((board, g) for board in PROBE_BOARDS for g in guesses)
    run_probes(sorted(pairs), cache, settings)

    # Assemble: manual first, then SimplifyJobs, then USCIS. First claim on a board wins.
    companies = list(manual)
    claimed = {(c["board"], c["slug"].lower()) for c in manual}

    def claim(entry: dict) -> None:
        key = (entry["board"], entry["slug"].lower())
        if key not in claimed:
            claimed.add(key)
            companies.append(entry)

    for board, slug in sorted(simplify):
        hit = cache.get(f"{board}:{slug}")
        if hit and hit["ok"]:
            name = hit["name"] or slug.replace("-", " ").title()
            claim({"name": name, "board": board, "slug": slug, "source": "simplify",
                   "status": "ok", "verified": True})

    for key, guesses in uscis_targets:
        for board in PROBE_BOARDS:
            hit = next((cache.get(f"{board}:{g}") | {"slug": g} for g in guesses
                        if (cache.get(f"{board}:{g}") or {}).get("ok")), None)
            if not hit:
                continue
            # Greenhouse tells us the company name, so a wrong slug can be rejected.
            # Lever and Ashby don't, so those hits are kept but marked unverified.
            if hit["name"] and not names_agree(hit["name"], key):
                continue
            claim({"name": hit["name"] or key.title(), "board": board, "slug": hit["slug"],
                   "source": "uscis", "uscis_key": key, "status": "ok",
                   "verified": bool(hit["name"])})
            break

    counts: dict[str, int] = {}
    for c in companies:
        if c["status"] == "ok":
            label = f"{c['source']}/{c['board']}"
            counts[label] = counts.get(label, 0) + 1
    payload = {"generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
               "counts": dict(sorted(counts.items())), "companies": companies}
    COMPANIES_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False), "utf-8")

    working = sum(1 for c in companies if c["status"] == "ok")
    print(f"\nWorking boards: {working:,}")
    for label, n in sorted(counts.items()):
        print(f"  {label:<22} {n:,}")
    unverified = sum(1 for c in companies if c.get("verified") is False)
    if unverified:
        print(f"  ({unverified} USCIS-guessed Lever/Ashby boards are unverified; eyeball companies.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
