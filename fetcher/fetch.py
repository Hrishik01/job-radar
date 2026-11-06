"""Fetch every watched board and write data/jobs.json.

    python fetcher/fetch.py              # all companies with status "ok"
    python fetcher/fetch.py --only veeva # one slug, handy while tuning
    python fetcher/fetch.py --limit 20   # first N companies

Pipeline per job: list -> title filter -> details (new jobs only) ->
years filter -> US filter -> tag (location, work mode, salary,
sponsorship, PERM, match %) -> merge with what we already had.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import filters
import scoring
from boards import BOARDS
from company_lists import ListLookup
import cap_exempt as cap_exempt_lists
from models import Job
from net import FetchError
from parse import parse_salary, salary_from_hint
from uscis import Lookup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS_PATH = DATA / "jobs.json"
LOG_DIR = DATA / "cache" / "logs"
MAX_RUNS_KEPT = 500

# A run fails (exit 1, jobs.json untouched) only when more than this share of
# boards errored, which points to an outage or a network block. A single dead
# board, like a company that left its ATS, is logged and skipped.
MAX_FAILED_SHARE = 0.5

# Boards fetched at once. Politeness is per host and enforced in net.py, so
# workers sharing a host (all Greenhouse boards) still queue one at a time;
# workers on different hosts (each Workday tenant) run side by side.
FETCH_WORKERS = 8

# Raw fields that only fill_details() provides; reused for jobs we already know.
DETAIL_FIELDS = ("description", "salary_hint", "country_hint", "posted_raw",
                 "posted_source", "closes_raw", "location", "apply_url")


def can_reuse_details(previous: dict | None) -> bool:
    """True when a stored record already has every detail field. Records
    written before a field was added get their details fetched once more."""
    return bool(previous and previous.get("description")
                and all(f in previous for f in DETAIL_FIELDS))

log = logging.getLogger("fetch")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def first_full_sweep(runs: list[dict]) -> str | None:
    """Start time of the first run that read at least 90% of the most boards
    any run has read. Used once, to seed board first-fetch times for data
    collected before they were recorded."""
    if not runs:
        return None
    most = max(r["boards_ok"] for r in runs)
    return next(r["started_utc"] for r in runs if r["boards_ok"] >= 0.9 * most)


def run_failed(boards_ok: int, boards_failed: int) -> bool:
    """True when too many boards errored for this run's results to be trusted."""
    return boards_failed > MAX_FAILED_SHARE * (boards_ok + boards_failed)


def board_key(board: str, slug: str) -> str:
    return f"{board}:{slug}"


def uscis_match(company: dict, lookup: Lookup) -> tuple[str | None, str]:
    """(employer key, how). Only "exact" and "alias" count as a sponsor.
    A USCIS-guessed Lever/Ashby board is "unverified" (the feed has no company
    name to confirm the guess), and a closest-name "prefix" match is only a
    near-miss for review; both leave sponsorship "unknown"."""
    if company.get("uscis_key"):
        return company["uscis_key"], "exact" if company.get("verified") is not False else "unverified"
    return lookup.match(company["name"])


def search_settings(settings: dict, cap_exempt: bool) -> dict:
    """Settings for listing one board. Workday and SmartRecruiters only list
    what the search terms find, so cap-exempt employers also search for
    their extra titles ("data coordinator", "research")."""
    if not cap_exempt:
        return settings
    return {**settings, "workday_search_terms": settings["workday_search_terms"] + settings["cap_exempt_search_terms"]}


COMPANY_REASON = "company: "  # prefix of the reason added by the company-level rule


def company_staffing(jobs: list[dict], min_flagged: int) -> None:
    """When min_flagged or more of a company's active jobs are flagged on
    their own evidence, flag the rest of its active jobs too. Recomputed from
    scratch every run, so a company that drops below the bar loses it."""
    own = {}  # company -> active jobs flagged on their own evidence
    for j in jobs:
        j["staffing_reasons"] = [r for r in j.get("staffing_reasons") or [] if not r.startswith(COMPANY_REASON)]
        if j["active"] and j["staffing_reasons"]:
            own[j["company"]] = own.get(j["company"], 0) + 1
    for j in jobs:
        n = own.get(j["company"], 0)
        if j["active"] and not j["staffing_reasons"] and n >= min_flagged:
            j["staffing_reasons"] = [f"{COMPANY_REASON}{n} of its jobs are flagged"]
        j["staffing"] = bool(j["staffing_reasons"])


def staffing_log(jobs: list[dict], settings: dict) -> list[str]:
    """One line per flagged company (active jobs only), then one indented
    line per job with the reasons, for checking the staffing flag by hand.
    A job flagged on a single weak phrase is marked [weak]."""
    by_company: dict[str, list[dict]] = {}
    for j in jobs:
        if j["active"] and j.get("staffing"):
            by_company.setdefault(j["company"], []).append(j)
    lines = []
    for company in sorted(by_company, key=str.lower):
        flagged = by_company[company]
        lines.append(f"{company} ({len(flagged)} job{'s' if len(flagged) != 1 else ''})")
        for j in flagged:
            weak = "[weak] " if filters.weak_only(j["staffing_reasons"], settings) else ""
            lines.append(f"    {weak}{j['title']} | {' || '.join(j['staffing_reasons'])}")
    return lines


def tag(job: Job, settings: dict, matchers, sponsor_likely: bool, naics_sector: str = "") -> tuple[dict, list[dict]]:
    """Derive every computed field for one job. Returns (record, tier2 log)."""
    mode, mode_source = filters.work_mode(job)

    salary = salary_from_hint(job.salary_hint)
    salary_source = f"{job.board}.feed" if salary else None
    if not salary:
        salary = parse_salary(job.description)
        salary_source = "description" if salary else None

    is_no, evidence, tier2 = filters.sponsorship_no(job.description)
    sponsorship = "no" if is_no else "likely" if sponsor_likely else "unknown"

    perm, markers = filters.perm_markers(job)
    staffing = filters.staffing_reasons(job.description, naics_sector, settings, job.company)
    match_pct, matched = scoring.score(job.description, matchers, settings["match_cap"])

    record = job.to_dict()
    record.update({
        "work_mode": mode,
        "work_mode_source": mode_source,
        "location_group": filters.location_group(job.location, mode, settings),
        "salary_min": salary and salary["min"],
        "salary_max": salary and salary["max"],
        "salary_hourly": bool(salary and salary["hourly"]),
        "salary_source": salary_source,
        "sponsorship": sponsorship,
        "sponsorship_evidence": evidence or None,
        "perm_likely": perm,
        "perm_markers": markers,
        "staffing": bool(staffing),
        "staffing_reasons": staffing,
        "match_pct": match_pct,
        "matched_skills": matched,
    })
    return record, tier2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="fetch a single company slug")
    parser.add_argument("--limit", type=int, help="fetch only the first N companies")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = load_json(DATA / "settings.json", None)
    companies = [c for c in load_json(DATA / "companies.json", {"companies": []})["companies"]
                 if c.get("status") == "ok" and c.get("board") in BOARDS]
    if args.only:
        companies = [c for c in companies if c["slug"].lower() == args.only.lower()]
    if args.limit:
        companies = companies[: args.limit]
    if not companies:
        log.error("No companies to fetch.")
        return 1

    matchers = scoring.build_matchers(settings["skills"])
    lookup = Lookup.load(settings["uscis_aliases"])
    if lookup is None:
        log.warning("No USCIS lookup yet (data/uscis/h1b_lookup.json); sponsorship falls back to 'unknown'.")
    # Fortune 500 / unicorn badges: USCIS aliases plus list-only aliases
    lists = ListLookup.load({**settings["uscis_aliases"], **settings.get("list_aliases", {})},
                            settings.get("list_exclude"))
    if lists is None:
        log.warning("No data/company_lists.json yet (run fetcher/company_lists.py); no company badges.")
    # Cap-exempt, confirmed tier: R1 + AAU universities (data/cap_exempt.json),
    # their medical centers and the Boston list. Same exact-or-alias rule as
    # the badges above; Carnegie's campus spellings are aliases.
    cap_data = cap_exempt_lists.load()
    if cap_data is None:
        log.warning("No data/cap_exempt.json yet (run fetcher/cap_exempt.py lists); only the Boston list is cap-exempt.")
    cap_exempt = ListLookup({"cap_exempt": {"names": cap_exempt_lists.confirmed_names(settings, cap_data)}},
                            {**settings["uscis_aliases"], **settings.get("cap_exempt_aliases", {}),
                             **(cap_data or {}).get("aliases", {})})
    # Possible tier: USCIS sector-61 employers with a university-like name
    possible = cap_exempt_lists.possible_keys(lookup.employers, settings) if lookup else set()

    store = load_json(JOBS_PATH, {"meta": {"runs": []}, "jobs": []})
    known = {j["id"]: j for j in store["jobs"]}
    started = now_utc()

    # When each board was first fetched. Jobs found on that first fetch are
    # "backfill": their first-seen time is when tracking started, not when
    # they were posted, so the insights page leaves them out of time charts.
    meta = store["meta"]
    if "boards_first_fetched_utc" not in meta:
        seed = first_full_sweep(meta.get("runs", []))
        meta["first_sweep_utc"] = seed or started
        boards = {board_key(j["board"], j["slug"]) for j in store["jobs"]}
        if meta.get("runs"):  # boards with no kept jobs were in the sweep too
            boards |= {board_key(c["board"], c["slug"]) for c in companies}
        meta["boards_first_fetched_utc"] = {b: meta["first_sweep_utc"] for b in sorted(boards)}
    first_fetched = meta["boards_first_fetched_utc"]

    run = {"started_utc": started, "boards_ok": 0, "boards_failed": 0, "listed": 0,
           "dropped_title": 0, "dropped_years": 0, "dropped_non_us": 0,
           "detail_errors": 0, "kept": 0, "new": 0}
    seen_ids: set[str] = set()
    fetched_boards: set[tuple[str, str]] = set()
    failures: list[str] = []
    tier2_lines: list[str] = []
    near_misses: list[str] = []
    list_near_misses: list[str] = []

    def fetch_board(company: dict) -> dict:
        """List one board and tag its jobs. Runs in a worker thread, so it
        only reads shared state (known, first_fetched) and returns results
        for the main thread to merge. net.py keeps each host's requests
        spaced out however many workers share it."""
        out = {"counts": dict.fromkeys(("dropped_title", "dropped_years", "dropped_non_us", "detail_errors"), 0),
               "records": [], "tier2": [], "near": [], "list_near": [], "warnings": []}
        board = BOARDS[company["board"]]
        sponsor_likely = False
        naics_sector = ""
        uscis_key = None
        if lookup:
            key, how = uscis_match(company, lookup)
            sponsor_likely = how in ("exact", "alias")
            if sponsor_likely:  # never take an industry or a tier from a fuzzy match
                naics_sector = lookup.naics_sector(key)
                uscis_key = key
            if how in ("prefix", "unverified"):
                out["near"].append(f"{how:<10} {company['name']!r} -> {key!r}")

        exempt, exempt_near = cap_exempt.match(company["name"])
        is_exempt = exempt["cap_exempt"]
        maybe_exempt = not is_exempt and uscis_key in possible
        # A shared system board (Beth Israel Lahey Health) lists the
        # cap-exempt member's jobs by location: only those jobs count.
        exempt_locations = [loc.lower() for loc in company.get("cap_exempt_locations", [])]
        # Cap-exempt employers also keep research/clinical data titles. Workday
        # and SmartRecruiters only list what the search terms find, so search
        # for those titles too.
        extra_titles = settings["cap_exempt_titles"] if is_exempt or exempt_locations else []
        try:
            listed = board.list_jobs(company, search_settings(settings, bool(extra_titles)))
        except FetchError as exc:
            out["error"] = str(exc)
            return out
        out["listed"] = len(listed)
        board_first = first_fetched.get(board_key(company["board"], company["slug"]), started)

        badges = {}
        if lists:
            badges, out["list_near"] = lists.match(company["name"])
        out["list_near"] += exempt_near

        for job in listed:
            # A title kept only by the cap-exempt extras must turn out to be
            # a cap-exempt job (checked once the location is known)
            extra_only = not filters.title_passes(job.title, settings)
            if extra_only and not (extra_titles and filters.title_passes(job.title, settings, extra_titles)):
                out["counts"]["dropped_title"] += 1
                continue

            previous = known.get(job.id)
            try:
                if board.NEEDS_DETAILS and can_reuse_details(previous):
                    for f in DETAIL_FIELDS:
                        setattr(job, f, previous.get(f) or getattr(job, f))
                elif board.NEEDS_DETAILS:
                    board.fill_details(company, job, settings)
            except FetchError as exc:
                out["counts"]["detail_errors"] += 1
                out["warnings"].append(f"  detail failed for {job.title}: {exc}")
                continue

            job_exempt = is_exempt or any(loc in job.location.lower() for loc in exempt_locations)
            if extra_only and not job_exempt:
                out["counts"]["dropped_title"] += 1
                continue

            years = filters.min_years_required(job.description)
            if not filters.experience_passes(years, settings):
                out["counts"]["dropped_years"] += 1
                continue
            us = filters.us_status(job.title, job.location, job.country_hint)
            if us == "non_us":
                out["counts"]["dropped_non_us"] += 1
                continue

            record, tier2 = tag(job, settings, matchers, sponsor_likely, naics_sector)
            first_seen = previous["first_seen_utc"] if previous else started  # never updated
            band, band_reasons = filters.experience_band(job.title, years, settings)
            record.update({
                "us_uncertain": us == "uncertain",
                "min_years": years,
                "band": band,
                "band_reasons": band_reasons,
                # "confirmed" (badge "Cap-exempt"), "possible" ("Cap-exempt?") or None
                "cap_exempt": "confirmed" if job_exempt else "possible" if maybe_exempt else None,
                "people": filters.people_search(job.title, settings),
                # Display only: a figure stated just under "Preferred qualifications"
                "min_years_preferred": None if years is not None else filters.min_years_preferred(job.description),
                "first_seen_utc": first_seen,
                "last_seen_utc": started,
                "backfill": first_seen <= board_first,
                "active": True,
                "fortune500": badges.get("fortune500", False),
                "unicorn": badges.get("unicorn", False),
            })
            out["records"].append((record, previous is None))
            if not previous:
                out["tier2"] += [f"{e['decision']:<12} | {job.company} | {job.title} | {e['sentence']}" for e in tier2]
        return out

    # Merge in board order, so the output doesn't depend on thread timing
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for n, (company, out) in enumerate(zip(companies, pool.map(fetch_board, companies)), 1):
            if "error" in out:
                run["boards_failed"] += 1
                failures.append(f"{company['name']} ({company['board']}/{company['slug']}): {out['error']}")
                log.warning("[%d/%d] %s: FAILED %s", n, len(companies), company["name"], out["error"])
                continue
            run["boards_ok"] += 1
            run["listed"] += out["listed"]
            for k, v in out["counts"].items():
                run[k] += v
            fetched_boards.add((company["board"], company["slug"]))
            first_fetched.setdefault(board_key(company["board"], company["slug"]), started)
            for record, is_new in out["records"]:
                known[record["id"]] = record
                seen_ids.add(record["id"])
                run["new"] += is_new
            run["kept"] += len(out["records"])
            tier2_lines += out["tier2"]
            near_misses += out["near"]
            list_near_misses += out["list_near"]
            for w in out["warnings"]:
                log.warning(w)
            log.info("[%d/%d] %s: %d listed, %d kept", n, len(companies), company["name"],
                     out["listed"], len(out["records"]))

    if run_failed(run["boards_ok"], run["boards_failed"]):
        log.error("%d of %d boards failed; jobs.json left untouched.",
                  run["boards_failed"], run["boards_ok"] + run["boards_failed"])
        for f in failures:
            log.error("  %s", f)
        return 1

    # A job that disappeared from a board we read successfully is no longer
    # active. Jobs from boards that errored keep their previous state, so a
    # flaky board can never shrink the file.
    for job_id, record in known.items():
        if job_id not in seen_ids and (record["board"], record["slug"]) in fetched_boards:
            record["active"] = False
        if "backfill" not in record:  # jobs this run didn't touch, from before the field existed
            board_first = first_fetched.get(board_key(record["board"], record["slug"]), meta["first_sweep_utc"])
            record["backfill"] = record["first_seen_utc"] <= board_first

    company_staffing(list(known.values()), settings["staffing"]["company_min_flagged"])

    jobs = sorted(known.values(), key=lambda j: (j["first_seen_utc"], j["company"], j["title"]), reverse=True)
    ids = [j["id"] for j in jobs]
    assert len(ids) == len(set(ids)), "duplicate job IDs"

    store["jobs"] = jobs
    store["meta"]["generated_utc"] = now_utc()
    store["meta"]["job_count"] = len(jobs)
    store["meta"]["active_count"] = sum(j["active"] for j in jobs)
    store["meta"]["runs"] = (store["meta"].get("runs", []) + [run])[-MAX_RUNS_KEPT:]
    JOBS_PATH.write_text(json.dumps(store, indent=1, ensure_ascii=False), "utf-8")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if tier2_lines:
        with open(LOG_DIR / "sponsorship_tier2.log", "a", encoding="utf-8") as f:
            f.write(f"# run {started}\n" + "\n".join(tier2_lines) + "\n")
    (LOG_DIR / "uscis_near_misses.log").write_text("\n".join(near_misses) + "\n", "utf-8")
    (LOG_DIR / "company_list_near_misses.log").write_text("\n".join(list_near_misses) + "\n", "utf-8")
    staffing_lines = staffing_log(jobs, settings)
    (LOG_DIR / "staffing.log").write_text("\n".join(staffing_lines) + "\n", "utf-8")

    log.info("\nBoards ok %d, failed %d | listed %d | kept %d (%d new) | dropped: title %d, years %d, non-US %d",
             run["boards_ok"], run["boards_failed"], run["listed"], run["kept"], run["new"],
             run["dropped_title"], run["dropped_years"], run["dropped_non_us"])
    flagged = sum(1 for line in staffing_lines if not line.startswith(" "))
    log.info("Staffing firms flagged: %d companies -> data/cache/logs/staffing.log", flagged)
    if tier2_lines:
        log.info("Tier 2 sponsorship sentences logged: %d -> data/cache/logs/sponsorship_tier2.log", len(tier2_lines))
    if failures:
        log.info("Failed boards:\n  " + "\n  ".join(failures))
        if os.environ.get("GITHUB_ACTIONS"):  # yellow warnings on the Actions run page
            for f in failures:
                print(f"::warning title=Board failed::{f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
