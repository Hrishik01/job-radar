"""SmartRecruiters public postings API (api.smartrecruiters.com).

List:   GET /v1/companies/{id}/postings?q={term}&limit=100&offset=N
Detail: GET /v1/companies/{id}/postings/{postingId}

Company IDs are case-insensitive. Large companies post thousands of jobs, so
like Workday we run one search per term in settings["workday_search_terms"]
instead of paging through everything.
"""

from __future__ import annotations

from urllib.parse import quote

from models import Job
from net import request_json
from parse import clean_url, html_to_text

API = "https://api.smartrecruiters.com/v1/companies"
NEEDS_DETAILS = True  # the listing has no description
PAGE = 100
MAX_PAGES = 10   # per search term
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def list_jobs(company: dict, settings: dict) -> list[Job]:
    http = settings["http"]
    seen: dict[str, Job] = {}
    for term in settings["workday_search_terms"]:
        for page in range(MAX_PAGES):
            data = request_json(
                "GET", f"{API}/{company['slug']}/postings?q={quote(term)}&limit={PAGE}&offset={page * PAGE}",
                delay=http["delay_seconds"], cache_seconds=http["list_cache_minutes"] * 60,
            )
            items = data.get("content") or []
            for item in items:
                if item["id"] in seen:
                    continue
                loc = item.get("location") or {}
                place = loc.get("fullLocation") or ", ".join(
                    p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
                seen[item["id"]] = Job(
                    company=company["name"],
                    board="smartrecruiters",
                    slug=company["slug"],
                    native_id=item["id"],
                    title=(item.get("name") or "").strip(),
                    location=place,
                    apply_url=f"https://jobs.smartrecruiters.com/{company['slug']}/{item['id']}",
                    posted_raw=item.get("releasedDate"),
                    posted_source="smartrecruiters.releasedDate" if item.get("releasedDate") else None,
                    # Only trust the flags when set; False doesn't prove onsite
                    workplace_hint="Remote" if loc.get("remote") else "Hybrid" if loc.get("hybrid") else "",
                    country_hint=loc.get("country") or "",  # ISO code, e.g. "us"
                )
            if not items or (page + 1) * PAGE >= (data.get("totalFound") or 0):
                break
    return list(seen.values())


def fill_details(company: dict, job: Job, settings: dict) -> None:
    data = request_json("GET", f"{API}/{company['slug']}/postings/{job.native_id}",
                        delay=settings["http"]["delay_seconds"], cache_seconds=7 * 24 * 3600)
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in SECTIONS:
        section = sections.get(key) or {}
        text = html_to_text(section.get("text"))
        if text:
            # Keep the section title ("Qualifications") as a heading line, so
            # the years rule and match % can find the requirements section.
            parts.append(f"{section.get('title') or key}\n{text}")
    job.description = "\n\n".join(parts)
    if data.get("postingUrl"):
        job.apply_url = clean_url(data["postingUrl"])
