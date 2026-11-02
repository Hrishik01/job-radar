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
from datetime import datetime, timezone
from pathlib import Path

import filters
import scoring
from boards import BOARDS
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


def tag(job: Job, settings: dict, matchers, sponsor_likely: bool) -> tuple[dict, list[dict]]:
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

    for n, company in enumerate(companies, 1):
        board = BOARDS[company["board"]]
        try:
            listed = board.list_jobs(company, settings)
        except FetchError as exc:
            run["boards_failed"] += 1
            failures.append(f"{company['name']} ({company['board']}/{company['slug']}): {exc}")
            log.warning("[%d/%d] %s: FAILED %s", n, len(companies), company["name"], exc)
            continue
        run["boards_ok"] += 1
        run["listed"] += len(listed)
        fetched_boards.add((company["board"], company["slug"]))
        board_first = first_fetched.setdefault(board_key(company["board"], company["slug"]), started)

        sponsor_likely = False
        if lookup:
            key, how = uscis_match(company, lookup)
            sponsor_likely = how in ("exact", "alias")
            if how in ("prefix", "unverified"):
                near_misses.append(f"{how:<10} {company['name']!r} -> {key!r}")

        kept_here = 0
        for job in listed:
            if not filters.title_passes(job.title, settings):
                run["dropped_title"] += 1
                continue

            previous = known.get(job.id)
            try:
                if board.NEEDS_DETAILS and can_reuse_details(previous):
                    for f in DETAIL_FIELDS:
                        setattr(job, f, previous.get(f) or getattr(job, f))
                elif board.NEEDS_DETAILS:
                    board.fill_details(company, job, settings)
            except FetchError as exc:
                run["detail_errors"] += 1
                log.warning("  detail failed for %s: %s", job.title, exc)
                continue

            years = filters.min_years_required(job.description)
            if years is not None and years > settings["max_years_required"]:
                run["dropped_years"] += 1
                continue
            us = filters.us_status(job.title, job.location, job.country_hint)
            if us == "non_us":
                run["dropped_non_us"] += 1
                continue

            record, tier2 = tag(job, settings, matchers, sponsor_likely)
            first_seen = previous["first_seen_utc"] if previous else started  # never updated
            record.update({
                "us_uncertain": us == "uncertain",
                "min_years": years,
                "first_seen_utc": first_seen,
                "last_seen_utc": started,
                "backfill": first_seen <= board_first,
                "active": True,
            })
            if not previous:
                run["new"] += 1
                tier2_lines += [f"{e['decision']:<12} | {job.company} | {job.title} | {e['sentence']}" for e in tier2]
            known[job.id] = record
            seen_ids.add(job.id)
            kept_here += 1

        run["kept"] += kept_here
        log.info("[%d/%d] %s: %d listed, %d kept", n, len(companies), company["name"], len(listed), kept_here)

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

    log.info("\nBoards ok %d, failed %d | listed %d | kept %d (%d new) | dropped: title %d, years %d, non-US %d",
             run["boards_ok"], run["boards_failed"], run["listed"], run["kept"], run["new"],
             run["dropped_title"], run["dropped_years"], run["dropped_non_us"])
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
