"""Cap-exempt employers: the national list behind the "Cap-exempt" badge, and
a board finder for them.

    python fetcher/cap_exempt.py lists            # rebuild data/cap_exempt.json
    python fetcher/cap_exempt.py boards           # find + verify boards, print a report
    python fetcher/cap_exempt.py boards --apply   # ...and add them to data/companies.json

Confirmed tier (badge "Cap-exempt"), names only, each list with its source and date:
- Carnegie Classifications 2025 "Research 1" institutions (public list,
  carnegieclassifications.acenet.edu), all nonprofit or public universities.
- AAU members. aau.edu answers 403 to our honest User-Agent, so the list is
  read from Wikipedia's member table, which cites aau.edu. US members only.
- Each university's own named academic medical center, curated by hand in
  settings.json (cap_exempt.medical_centers). The board finder checks that
  the center's own homepage names its university.
- The 10 Boston employers in settings.json (cap_exempt_employers).

The possible tier ("Cap-exempt?") is not stored here: fetch.py derives it from
the USCIS lookup at run time (NAICS sector 61 plus a university-like name).

Board finding never guesses a slug: it starts at the employer's official
website (Wikidata "official website" for universities, settings for medical
centers), follows its own job/career links, and takes the ATS link it finds.
Supported boards are verified through their own API; anything else is
recorded as unsupported with the system's name.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from net import FetchError, get_text, probe_status

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CAP_PATH = DATA / "cap_exempt.json"
CARNEGIE_R1 = "https://carnegieclassifications.acenet.edu/institutions/?research2025[]=1"
AAU_WIKI = ("https://en.wikipedia.org/w/api.php?action=parse&page=Association_of_American_Universities"
            "&prop=wikitext&format=json&formatversion=2")
WIKIDATA = "https://www.wikidata.org/w/api.php"
WEEK = 7 * 24 * 3600

# Carnegie spells out campuses; the common name is what boards and USCIS use.
# Only suffixes that never change which institution is meant are dropped.
_CAMPUS_SUFFIX = re.compile(r"(?:-Main Campus| Campus Immersion| in the City of New York)$")
_PROVINCES = {"Ontario", "Quebec", "British Columbia", "Alberta", "Nova Scotia", "Manitoba"}


# ---------------------------------------------------------------- lists

def parse_carnegie_page(page: str) -> list[str]:
    names = re.findall(r'<td class="institution-title">\s*<a href="[^"]+">([^<]+)</a>', page)
    return [html.unescape(n).strip() for n in names]


def carnegie_r1() -> dict:
    first = get_text(CARNEGIE_R1, delay=2, cache_seconds=WEEK)
    pages = max([1] + [int(p) for p in re.findall(r"/institutions/page/(\d+)/", first)])
    names = parse_carnegie_page(first)
    for n in range(2, pages + 1):
        url = f"https://carnegieclassifications.acenet.edu/institutions/page/{n}/?research2025%5B0%5D=1"
        names += parse_carnegie_page(get_text(url, delay=2, cache_seconds=WEEK))
    return {"title": "Carnegie Classifications 2025: Research 1 (Very High Research Spending and Doctorate Production)",
            "source": CARNEGIE_R1, "retrieved": date.today().isoformat(), "names": sorted(set(names))}


def parse_aau(wikitext: str) -> list[str]:
    """US members from the 'Current members' table: each row starts with
    `! scope="row" |[[Name]]` and the next cell is the state or province."""
    section = wikitext[wikitext.find("Current members"):]
    section = section[: section.find("\n|}")]
    names = []
    for m in re.finditer(r'!\s*scope="row"\s*\|\s*\[\[([^\]|]+)(?:\|([^\]]+))?\]\][^\n]*\n\|\s*([^\n]+)', section):
        target, shown, state = m.group(1), m.group(2), m.group(3)
        if re.sub(r"\[\[|\]\]", "", state).strip() in _PROVINCES:
            continue
        names.append((shown or target).strip())
    return names


def aau_members() -> dict:
    wikitext = json.loads(get_text(AAU_WIKI, delay=2, cache_seconds=WEEK))["parse"]["wikitext"]
    return {"title": "Association of American Universities members (US)",
            "source": "https://en.wikipedia.org/wiki/Association_of_American_Universities "
                      "(member table citing https://www.aau.edu/who-we-are/our-members, which returns "
                      "403 to automated clients)",
            "retrieved": date.today().isoformat(), "names": sorted(set(parse_aau(wikitext)))}


def common_name(name: str) -> str:
    return _CAMPUS_SUFFIX.sub("", name).strip()


def _wikidata(query: str) -> dict:
    """One Wikidata API call, slow and cached; on a 429 wait once and retry."""
    url = f"{WIKIDATA}?{query}&format=json"
    try:
        return json.loads(get_text(url, delay=3, cache_seconds=WEEK))
    except FetchError as exc:
        if "429" not in str(exc):
            raise
        time.sleep(60)
        return json.loads(get_text(url, delay=3, cache_seconds=WEEK))


def official_websites(names: list[str]) -> dict[str, str | None]:
    """Wikidata 'official website' (P856) of the first search hit described
    as a university, college, institute or school. One search per name, then
    entity claims fetched 50 at a time."""
    item = {}
    for name in names:
        hits = _wikidata(f"action=wbsearchentities&search={quote(name)}&language=en&type=item&limit=5").get("search", [])
        item[name] = next((h["id"] for h in hits
                           if re.search(r"universit|college|institute|school", (h.get("description") or "").lower())), None)
    ids = sorted({i for i in item.values() if i})
    sites = {}
    for k in range(0, len(ids), 50):
        entities = _wikidata(f"action=wbgetentities&ids={'|'.join(ids[k:k + 50])}&props=claims")["entities"]
        for qid, ent in entities.items():
            urls = [c.get("mainsnak", {}).get("datavalue", {}).get("value") for c in ent.get("claims", {}).get("P856", [])]
            sites[qid] = next((u for u in urls if u), None)
    return {name: sites.get(qid) if qid else None for name, qid in item.items()}


def build_lists() -> dict:
    r1, aau = carnegie_r1(), aau_members()
    if len(r1["names"]) < 150 or len(aau["names"]) < 60:
        sys.exit(f"Lists look incomplete (R1 {len(r1['names'])}, AAU {len(aau['names'])}); not writing.")
    # One website per university; the AAU spelling wins when both lists have it
    names = list(dict.fromkeys(aau["names"] + [common_name(n) for n in r1["names"]]))
    universities = official_websites(names)
    return {"lists": {"r1": r1, "aau": aau},
            # Extra spellings that mean the same institution (Carnegie campus names)
            "aliases": {n: common_name(n) for n in r1["names"] if common_name(n) != n},
            "websites": universities}


def load() -> dict | None:
    return json.loads(CAP_PATH.read_text("utf-8")) if CAP_PATH.exists() else None


def confirmed_names(settings: dict, cap: dict | None) -> list[str]:
    """Every confirmed-tier name: both lists, Carnegie's campus spellings,
    the medical centers and the Boston employers."""
    names = set(settings["cap_exempt_employers"])
    names |= {mc["name"] for mc in settings["cap_exempt"]["medical_centers"]}
    if cap:
        for lst in cap["lists"].values():
            names |= set(lst["names"])
        names |= set(cap["aliases"].values())
    return sorted(names)


def possible_keys(employers: dict[str, list], settings: dict) -> set[str]:
    """USCIS employer keys for the "Cap-exempt?" tier: NAICS sector 61
    (educational services) and a university-like name, minus for-profit
    schools and anything hospital-like (hospitals are only ever confirmed)."""
    from uscis import normalize_company
    cfg = settings["cap_exempt"]
    name_rx, never_rx = re.compile(cfg["possible_name"], re.I), re.compile(cfg["never_possible"], re.I)
    excluded = {normalize_company(n) for n in cfg["possible_exclude"]}
    return {key for key, entry in employers.items()
            if len(entry) > 2 and entry[2] == cfg["possible_sector"] and name_rx.search(key)
            and not never_rx.search(key) and key not in excluded}


# ---------------------------------------------------------------- boards

ATS_SYSTEMS = [  # (regex on a link, system name); supported ones first
    (r"myworkdayjobs\.com|myworkdaysite\.com", "Workday"),
    (r"greenhouse\.io", "Greenhouse"), (r"jobs\.lever\.co", "Lever"), (r"ashbyhq\.com", "Ashby"),
    (r"smartrecruiters\.com", "SmartRecruiters"),
    (r"peopleadmin\.com", "PeopleAdmin"), (r"taleo\.net", "Oracle Taleo"),
    (r"oraclecloud\.com", "Oracle Recruiting Cloud"), (r"successfactors\.\w+|sapsf\.", "SAP SuccessFactors"),
    (r"brassring\.com|kenexa", "Kenexa BrassRing"), (r"icims\.com", "iCIMS"),
    (r"ultipro\.com|ukg\.net|rec\.pro\.ukg", "UKG"), (r"jobvite\.com", "Jobvite"),
    (r"csod\.com|cornerstoneondemand", "Cornerstone"), (r"pageuppeople\.com", "PageUp"),
    (r"silkroad\.com", "SilkRoad"), (r"avature\.net", "Avature"), (r"peopleclick\.com", "PeopleClick"),
    (r"governmentjobs\.com|schooljobs\.com", "NEOGOV"), (r"hiretouch\.com", "HireTouch"),
    (r"dayforcehcm\.com", "Dayforce"), (r"applytojob\.com", "JazzHR"), (r"paylocity\.com", "Paylocity"),
]
SUPPORTED = {"Workday", "Greenhouse", "Lever", "Ashby", "SmartRecruiters"}
_LINK = re.compile(r'href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
# Staff job pages, not student career centers ("Career Services", "Handshake")
_JOBBY = re.compile(r"\b(?:jobs?|employment|work (?:at|for|with)|join (?:us|our team)|human resources|hr|careers?)\b", re.I)
_STUDENT = re.compile(r"career (?:center|services|development|education)|handshake|student employment|for students|alumni", re.I)
_WD = re.compile(r"https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
                 r"|https?://wd(\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9-]+)/([A-Za-z0-9_-]+)")


def _system(url: str) -> str | None:
    for rx, name in ATS_SYSTEMS:
        if re.search(rx, url, re.I):
            return name
    return None


def _site(url: str) -> str:
    """'www.cs.clemson.edu' -> 'clemson.edu': links must stay on the employer's site."""
    host = urlsplit(url).netloc.lower().split(":")[0]
    return ".".join(host.split(".")[-2:])


def find_ats_links(start: str, max_pages: int = 8) -> tuple[list[str], list[str]]:
    """(ATS links found, pages read), breadth-first from the homepage through
    links that look like staff job pages on the employer's own site."""
    home = _site(start)
    queue, seen, found, read = [start], set(), [], []
    while queue and len(read) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = get_text(url, delay=1.5, cache_seconds=WEEK)
        except FetchError:
            continue
        read.append(url)
        nxt = []
        for href, text in _LINK.findall(page):
            link = urljoin(url, html.unescape(href.strip()))
            label = re.sub(r"<[^>]+>", " ", text)
            if not link.startswith("http"):
                continue
            if _system(link):
                found.append(link)
            elif _site(link) == home and _JOBBY.search(f"{label} {urlsplit(link).path}") \
                    and not _STUDENT.search(f"{label} {link}") and link not in seen:
                # "jobs"/"employment" links first, plain "careers" last
                nxt.append((0 if re.search(r"jobs?|employment", f"{label} {link}", re.I) else 1, link))
        if found and any(_system(f) in SUPPORTED for f in found):
            break
        queue += [link for _, link in sorted(nxt)][:6]
    return found, read


def verify(link: str) -> dict | None:
    """A companies.json entry for a supported board link, if its API has jobs."""
    m = _WD.search(link)
    if m:
        tenant, pod, site = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
        status, data = probe_status("POST", f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                                    body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, delay=1.5)
        if status == 200 and (data or {}).get("jobPostings"):
            return {"board": "workday", "slug": f"{tenant}-{site}", "tenant": tenant, "pod": int(pod), "site": site}
        return None
    patterns = {"greenhouse": r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
                "lever": r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
                "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"}
    from discover import probe  # imported here so `lists` doesn't load discovery
    for board, rx in patterns.items():
        m = re.search(rx, link)
        if m and probe(board, m.group(1), 1.5)["ok"]:
            return {"board": board, "slug": m.group(1)}
    return None


def find_board(name: str, website: str | None, must_mention: str | None = None) -> dict:
    """One employer's result: {"name", "status": ok|unsupported|not_found, ...}."""
    if not website:
        return {"name": name, "status": "not_found", "why": "no official website on record"}
    if must_mention:  # medical centers: the homepage must name its university
        try:
            page = get_text(website, delay=1.5, cache_seconds=WEEK)
        except FetchError as exc:
            return {"name": name, "status": "not_found", "why": f"homepage unreachable ({exc})"}
        if must_mention.lower() not in html.unescape(page).lower():
            return {"name": name, "status": "not_found", "why": f"homepage doesn't name {must_mention}"}
    links, read = find_ats_links(website)
    for link in links:
        if _system(link) in SUPPORTED:
            entry = verify(link)
            if entry:
                return {"name": name, "status": "ok", "entry": entry, "evidence": link}
    systems = [s for s in (_system(link) for link in links) if s]
    if systems:
        top = max(set(systems), key=systems.count)
        note = "Workday link found but its CXS API returned no jobs" if top in SUPPORTED else f"Uses {top}"
        return {"name": name, "status": "unsupported", "system": top, "why": note,
                "evidence": next(link for link in links if _system(link) == top)}
    return {"name": name, "status": "not_found", "why": f"no ATS link within {len(read)} pages of {website}"}


def find_all(settings: dict, cap: dict) -> list[dict]:
    # One crawl per website: the first spelling (AAU's) names the board.
    # Names Wikidata had no website for are reported, not crawled.
    jobs, seen = [], set()
    for name, url in cap["websites"].items():
        host = urlsplit(url).netloc.lower().removeprefix("www.") if url else None
        if host and host in seen:
            continue
        seen.add(host)
        jobs.append((name, url, None))
    jobs += [(mc["name"], mc["website"], mc["university_word"]) for mc in settings["cap_exempt"]["medical_centers"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda j: find_board(*j), jobs))


def apply(results: list[dict]) -> tuple[int, int]:
    """Add verified boards (source manual, so discovery keeps them) and
    unsupported markers to companies.json; existing boards are left alone."""
    path = DATA / "companies.json"
    store = json.loads(path.read_text("utf-8"))
    have_boards = {(c["board"], c["slug"].lower()) for c in store["companies"]}
    have_names = {c["name"].lower() for c in store["companies"]}
    added = marked = 0
    today = date.today().isoformat()
    for r in results:
        if r["status"] == "ok" and (r["entry"]["board"], r["entry"]["slug"].lower()) not in have_boards:
            store["companies"].append({"name": r["name"], **r["entry"], "source": "manual", "status": "ok",
                                       "note": f"Cap-exempt. Found from the official site via {r['evidence']}; "
                                               f"API returned live jobs ({today})."})
            added += 1
        elif r["status"] == "unsupported" and r["name"].lower() not in have_names:
            store["companies"].append({"name": r["name"], "board": r["system"].lower().replace(" ", "-"),
                                       "slug": re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-"),
                                       "source": "manual", "status": "unsupported",
                                       "note": f"Cap-exempt. {r['why']} ({r['evidence']}). No adapter ({today})."})
            marked += 1
    path.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return added, marked


def main(argv: list[str]) -> None:
    settings = json.loads((DATA / "settings.json").read_text("utf-8"))
    if argv[:1] == ["lists"]:
        cap = build_lists()
        CAP_PATH.write_text(json.dumps(cap, indent=1, ensure_ascii=False) + "\n", "utf-8")
        missing = sum(1 for v in cap["websites"].values() if not v)
        print(f"R1 {len(cap['lists']['r1']['names'])}, AAU {len(cap['lists']['aau']['names'])}, "
              f"{len(cap['websites'])} universities ({missing} without a website) -> {CAP_PATH.relative_to(ROOT)}")
    elif argv[:1] == ["boards"]:
        cap = load() or sys.exit("Run `cap_exempt.py lists` first.")
        results = find_all(settings, cap)
        out = DATA / "cache" / "cap_exempt_boards.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=1), "utf-8")
        for status in ("ok", "unsupported", "not_found"):
            rows = [r for r in results if r["status"] == status]
            print(f"\n{status}: {len(rows)}")
            for r in rows:
                detail = r.get("entry", {}).get("slug") or r.get("system") or r.get("why")
                print(f"  {r['name']}: {detail}")
        if "--apply" in argv:
            added, marked = apply(results)
            print(f"\ncompanies.json: {added} boards added, {marked} marked unsupported")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
