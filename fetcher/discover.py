"""Build data/companies.json from public sources.

    python fetcher/discover.py               # manual + public lists + USCIS probes
    python fetcher/discover.py --no-uscis    # skip the slow USCIS probing step
    python fetcher/discover.py --top 500     # probe fewer USCIS employers

Sources, in priority order:
  1. Manual entries already in companies.json (source "manual") are kept as-is.
  2. Board links in public new-grad job lists on GitHub (settings
     discovery.github_lists): Greenhouse, Lever, Ashby, SmartRecruiters and
     Workday. The company name comes from the list row.
  3. Likely slugs for the top USCIS H-1B employers, probed on Greenhouse,
     Ashby, Lever and SmartRecruiters.
  4. Workday for the largest USCIS employers: find the tenant's pod, then
     try the most common careers-site names.

A board counts only if its API returns at least one job. Nothing is guessed
into companies.json: every entry was answered by the board itself. Probe
results are cached in data/cache/discovery.json and re-checked after
settings["discovery"]["recheck_days"] days.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from net import FetchError, get_text, probe_status, request_json
from uscis import Lookup, normalize_company

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
COMPANIES_PATH = DATA / "companies.json"
PROBE_CACHE = DATA / "cache" / "discovery.json"

PROBE_BOARDS = ("greenhouse", "ashby", "lever", "smartrecruiters")  # preference order
LINK_PATTERNS = {
    "greenhouse": re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
}
WORKDAY_LINK = re.compile(r"([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")
NOT_SLUGS = {"embed", "jobs", "api", "v1", "job", "details", "oneclick-ui"}
# Company names in list rows: "<strong><a ..>RELX</a></strong>", "<strong>NVIDIA</strong>", "**[Acme](..)**"
ROW_NAME = re.compile(r"<strong>(?:<a[^>]*>)?([^<]+?)(?:</a>)?</strong>|\*\*\[([^\]]+)\]\(")
LOCATION_COUNT = re.compile(r"^\d+\s+locations?$", re.I)
# Words dropped when guessing a short slug ("veeva systems" -> "veeva").
GENERIC_WORDS = {
    "the", "and", "group", "holdings", "technologies", "technology", "services", "systems",
    "solutions", "usa", "us", "america", "americas", "international", "global", "company",
    "enterprises", "consulting", "software", "labs", "north",
}
WORKDAY_BODY = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}


# ---------------------------------------------------------------- sources

def parse_list(text: str, found: dict[tuple, str]) -> None:
    """Add {board key: company name} for every board link in one list. Keys
    are (board, slug), or ("workday", tenant, pod, site) for Workday links.
    A link belongs to the last company name seen (rows like "↳" repeat it)."""
    name = None
    for line in text.splitlines():
        for m in ROW_NAME.finditer(line):
            # Drop leading markers such as "🔥 " so names match USCIS exactly.
            text = re.sub(r"^\W+", "", html.unescape(m.group(1) or m.group(2))).strip()
            # "↳" rows have no company, only a bold "6 locations" summary.
            if not LOCATION_COUNT.match(text):
                name = text
                break
        for board, rx in LINK_PATTERNS.items():
            for slug in rx.findall(line):
                slug = unquote(slug).strip(".")
                if slug.lower() not in NOT_SLUGS:
                    found.setdefault((board, slug), name)
        for tenant, pod, site in WORKDAY_LINK.findall(line):
            if site.lower() not in NOT_SLUGS:
                found.setdefault(("workday", tenant, int(pod), site.lower()), name)


def list_boards(settings: dict) -> dict[tuple, str]:
    """Board links from every public GitHub list in settings."""
    found: dict[tuple, str] = {}
    for url in settings["discovery"]["github_lists"]:
        try:
            text = get_text(url, cache_seconds=24 * 3600)
        except FetchError as exc:
            print(f"  list unavailable ({exc}): {url}")
            continue
        before = len(found)
        parse_list(text, found)
        print(f"  {len(found) - before:,} new board links: {url}")
    return found


def slug_guesses(employer_key: str) -> list[str]:
    tokens = employer_key.split()
    core = [t for t in tokens if t not in GENERIC_WORDS] or tokens
    guesses = ["".join(tokens), "-".join(tokens), "".join(core)]
    return list(dict.fromkeys(g for g in guesses if len(g) >= 3))  # dedupe, keep order


def tenant_guesses(employer_key: str) -> list[str]:
    """Workday tenants are lowercase letters and digits, e.g. "statestreet"."""
    return [g for g in slug_guesses(employer_key) if g.isalnum()]


# ---------------------------------------------------------------- probing

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    elif board == "smartrecruiters":
        # Unknown companies return 200 with no postings, so "ok" needs a job
        data = request_json("GET", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
                            delay=delay, allow_404=True, retries=2)
        jobs = (data or {}).get("content") or []
        name = ((jobs[0].get("company") or {}).get("name") or "").strip() or None if jobs else None
    else:
        data = request_json("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                            delay=delay, allow_404=True, retries=2)
        jobs, name = (data or {}).get("jobs") or [], None
    return {"ok": bool(jobs), "name": name, "checked": now_iso()}


def _workday_url(tenant: str, pod: int, site: str, path: str = "jobs") -> str:
    return f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/{path}"


def probe_workday_site(tenant: str, pod: int, site: str, delay: float) -> dict:
    """A known tenant/pod/site (from a list link): ok if it has a job."""
    status, data = probe_status("POST", _workday_url(tenant, pod, site), body=WORKDAY_BODY, delay=delay)
    ok = status == 200 and bool((data or {}).get("jobPostings"))
    return {"ok": ok, "name": None, "checked": now_iso()}


def find_workday_tenant(tenant: str, settings: dict, delay: float) -> dict:
    """Workday answers 422 for a tenant that isn't on a pod and 404 for the
    right tenant with a wrong site. So: find the pod, then try the most
    common site names there. The board counts as verified only if the
    tenant's own "About" sidebar names it."""
    disc = settings["discovery"]
    for pod in disc["workday_pods"]:
        status, _ = probe_status("POST", _workday_url(tenant, pod, "external"), body=WORKDAY_BODY, delay=delay)
        if status in (200, 404):
            break
    else:
        return {"ok": False, "pod": None, "checked": now_iso()}

    for template in disc["workday_sites"]:
        site = template.format(tenant=tenant)
        status, data = probe_status("POST", _workday_url(tenant, pod, site), body=WORKDAY_BODY, delay=delay)
        if status == 200 and (data or {}).get("jobPostings"):
            _, sidebar = probe_status("GET", _workday_url(tenant, pod, site, "sidebar"), delay=delay)
            about = re.sub(r"[^a-z0-9]", "", html.unescape(json.dumps(sidebar or "")).lower())
            return {"ok": True, "pod": pod, "site": site, "verified": tenant in about, "checked": now_iso()}
    return {"ok": False, "pod": pod, "site": None, "checked": now_iso()}


def run_probes(lanes: dict[str, list[tuple[str, object]]], cache: dict, settings: dict) -> None:
    """Run (cache key, probe function) tasks not freshly cached. One worker per
    lane (board host), so each host still sees requests one at a time with
    a delay between them."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings["discovery"]["recheck_days"])
    todo = {lane: [(k, fn) for k, fn in tasks
                   if k not in cache or datetime.fromisoformat(cache[k]["checked"]) < cutoff]
            for lane, tasks in lanes.items()}
    total = sum(len(v) for v in todo.values())
    print(f"Probing {total:,} boards ({sum(len(v) for v in lanes.values()) - total:,} cached): "
          + ", ".join(f"{lane} {len(v):,}" for lane, v in todo.items()))

    lock = threading.Lock()
    done = [0]

    def worker(lane: str):
        for key, fn in todo[lane]:
            try:
                result = fn()
            except FetchError as exc:  # rate limits / server errors: retry next run
                print(f"  {key}: {exc}")
                continue
            with lock:
                cache[key] = result
                done[0] += 1
                if done[0] % 500 == 0:
                    save_cache(cache)
                    print(f"  {done[0]:,}/{total:,} probed")

    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        list(pool.map(worker, todo))
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

def board_key(entry: dict) -> tuple:
    """Identity of a board, so the same one is never listed twice."""
    if entry["board"] == "workday":
        return ("workday", entry["tenant"].lower(), entry["site"].lower())
    return (entry["board"], entry["slug"].lower())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build data/companies.json")
    parser.add_argument("--no-uscis", action="store_true", help="skip USCIS probing")
    parser.add_argument("--no-lists", "--no-simplify", action="store_true", help="skip the GitHub job lists")
    parser.add_argument("--top", type=int, help="how many top USCIS employers to probe")
    args = parser.parse_args()

    settings = json.loads((DATA / "settings.json").read_text("utf-8"))
    disc = settings["discovery"]
    existing = json.loads(COMPANIES_PATH.read_text("utf-8"))["companies"]
    manual = [c for c in existing if c.get("source") == "manual"]
    cache = json.loads(PROBE_CACHE.read_text("utf-8")) if PROBE_CACHE.exists() else {}
    delay = settings["http"]["probe_delay_seconds"]

    listed: dict[tuple, str] = {}
    if not args.no_lists:
        print("Public job lists:")
        listed = list_boards(settings)

    uscis_keys: list[str] = []
    workday_keys: list[str] = []
    lookup = Lookup.load(settings["uscis_aliases"])
    if not args.no_uscis:
        if lookup is None:
            print("No USCIS lookup yet; skipping USCIS discovery (run fetcher/uscis.py first).")
        else:
            uscis_keys = lookup.top(args.top or disc["uscis_top_n"])
            workday_keys = uscis_keys[: disc["workday_probe_top_n"]]
            print(f"USCIS: top {len(uscis_keys):,} employers; Workday for the top {len(workday_keys):,}")

    # Every probe, grouped by host so each host is asked one thing at a time
    lanes: dict[str, dict[str, object]] = {b: {} for b in (*PROBE_BOARDS, "workday")}

    def add(board: str, slug: str) -> None:
        if board == "smartrecruiters" and "-" in slug:
            return  # SmartRecruiters IDs have no hyphens
        lanes[board][f"{board}:{slug}"] = lambda b=board, s=slug: probe(b, s, delay)

    for key in listed:
        if key[0] == "workday":
            _, tenant, pod, site = key
            lanes["workday"][f"workday:{tenant}:{pod}:{site}"] = (
                lambda t=tenant, p=pod, s=site: probe_workday_site(t, p, s, delay))
        else:
            add(*key)
    for key in uscis_keys:
        for board in PROBE_BOARDS:
            for slug in slug_guesses(key):
                add(board, slug)
    for key in workday_keys:
        for tenant in tenant_guesses(key):
            lanes["workday"][f"workday-tenant:{tenant}"] = (
                lambda t=tenant: find_workday_tenant(t, settings, delay))
    run_probes({lane: list(tasks.items()) for lane, tasks in lanes.items()}, cache, settings)

    # Assemble: manual, then public lists, then USCIS. First claim on a board wins.
    companies = list(manual)
    claimed = {board_key(c) for c in manual}

    def claim(entry: dict) -> bool:
        if board_key(entry) in claimed:
            return False
        claimed.add(board_key(entry))
        companies.append(entry)
        return True

    for key, name in sorted(listed.items(), key=lambda kv: str(kv[0])):
        if key[0] == "workday":
            _, tenant, pod, site = key
            if (cache.get(f"workday:{tenant}:{pod}:{site}") or {}).get("ok"):
                claim({"name": name or tenant.title(), "board": "workday", "slug": f"{tenant}-{site}",
                       "tenant": tenant, "pod": pod, "site": site, "source": "list",
                       "status": "ok", "verified": True})
            continue
        board, slug = key
        hit = cache.get(f"{board}:{slug}")
        if hit and hit["ok"]:
            claim({"name": hit["name"] or name or slug.replace("-", " ").title(), "board": board,
                   "slug": slug, "source": "list", "status": "ok", "verified": True})

    for key in uscis_keys:
        found = False
        for board in PROBE_BOARDS:
            hit = next((cache[f"{board}:{g}"] | {"slug": g} for g in slug_guesses(key)
                        if (cache.get(f"{board}:{g}") or {}).get("ok")), None)
            if not hit:
                continue
            # Greenhouse and SmartRecruiters name the company, so a wrong slug
            # can be rejected. Lever and Ashby don't: kept but unverified.
            if hit["name"] and not names_agree(hit["name"], key):
                continue
            claim({"name": hit["name"] or key.title(), "board": board, "slug": hit["slug"],
                   "source": "uscis", "uscis_key": key, "status": "ok",
                   "verified": bool(hit["name"])})
            found = True
            break
        if found or key not in workday_keys:
            continue
        for tenant in tenant_guesses(key):
            hit = cache.get(f"workday-tenant:{tenant}") or {}
            if hit.get("ok"):
                claim({"name": key.title(), "board": "workday", "slug": f"{tenant}-{hit['site']}",
                       "tenant": tenant, "pod": hit["pod"], "site": hit["site"], "source": "uscis",
                       "uscis_key": key, "status": "ok", "verified": hit["verified"]})
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
        print(f"  {label:<26} {n:,}")
    unverified = sum(1 for c in companies if c.get("verified") is False)
    if unverified:
        print(f"  ({unverified} USCIS-guessed boards are unverified; their sponsorship stays 'unknown')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
