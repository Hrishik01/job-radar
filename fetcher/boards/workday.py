"""Workday candidate-experience ("CXS") endpoints.

Search:  POST https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Detail:  GET  https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

Large tenants list thousands of jobs, so instead of paging through all of
them we run one search per term in settings["workday_search_terms"].
"""

from __future__ import annotations

from models import Job
from net import request_json
from parse import clean_url, html_to_text

NEEDS_DETAILS = True  # the search results have no description
PAGE = 20        # Workday rejects larger page sizes
MAX_PAGES = 25   # per search term; 500 results is plenty after title filtering


def _base(company: dict) -> str:
    return f"https://{company['tenant']}.wd{company['pod']}.myworkdayjobs.com"


def list_jobs(company: dict, settings: dict) -> list[Job]:
    http = settings["http"]
    url = f"{_base(company)}/wday/cxs/{company['tenant']}/{company['site']}/jobs"
    seen: dict[str, Job] = {}

    for term in settings["workday_search_terms"]:
        total = None
        for page in range(MAX_PAGES):
            body = {"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": term}
            data = request_json("POST", url, body=body, delay=http["delay_seconds"],
                                cache_seconds=http["list_cache_minutes"] * 60)
            postings = data.get("jobPostings") or []
            if total is None:
                total = data.get("total") or 0  # only the first page reports it reliably
            for item in postings:
                path = item.get("externalPath")
                if not path or path in seen:
                    continue
                seen[path] = Job(
                    company=company["name"],
                    board="workday",
                    slug=company["slug"],
                    native_id=path,  # stable across runs, unlike the apply URL
                    title=item.get("title", "").strip(),
                    location=item.get("locationsText") or "",
                    apply_url=f"{_base(company)}/{company['site']}{path}",
                    posted_raw=item.get("postedOn"),
                    posted_source="workday.postedOn (relative)",
                )
            if not postings or (page + 1) * PAGE >= total:
                break
    return list(seen.values())


def fill_details(company: dict, job: Job, settings: dict) -> None:
    url = f"{_base(company)}/wday/cxs/{company['tenant']}/{company['site']}{job.native_id}"
    data = request_json("GET", url, delay=settings["http"]["delay_seconds"],
                        cache_seconds=7 * 24 * 3600)
    info = data.get("jobPostingInfo") or {}
    job.description = html_to_text(info.get("jobDescription"))
    job.location = info.get("location") or job.location
    job.country_hint = (info.get("country") or {}).get("descriptor", "")
    if info.get("externalUrl"):
        job.apply_url = clean_url(info["externalUrl"])
    if info.get("startDate"):
        job.posted_raw = info["startDate"]
        job.posted_source = "workday.startDate"
    job.closes_raw = info.get("endDate")  # e.g. "2026-11-13"; many postings have none
