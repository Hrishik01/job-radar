"""Ashby public posting API (api.ashbyhq.com)."""

from __future__ import annotations

from models import Job
from net import request_json
from parse import clean_url

API = "https://api.ashbyhq.com/posting-api/job-board"
NEEDS_DETAILS = False


def _salary(compensation: dict | None) -> dict | None:
    for comp in (compensation or {}).get("summaryComponents") or []:
        if comp.get("compensationType") == "Salary" and comp.get("minValue") is not None:
            return {
                "min": comp["minValue"], "max": comp.get("maxValue"),
                "currency": comp.get("currencyCode"),
                "interval": comp.get("interval"),  # "1 YEAR", "1 HOUR"
            }
    return None


def list_jobs(company: dict, settings: dict) -> list[Job]:
    http = settings["http"]
    data = request_json(
        "GET", f"{API}/{company['slug']}?includeCompensation=true",
        delay=http["delay_seconds"], cache_seconds=http["list_cache_minutes"] * 60,
    )
    jobs = []
    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue
        address = ((item.get("address") or {}).get("postalAddress") or {})
        workplace = item.get("workplaceType") or ("Remote" if item.get("isRemote") else "")
        jobs.append(Job(
            company=company["name"],
            board="ashby",
            slug=company["slug"],
            native_id=item["id"],
            title=item.get("title", "").strip(),
            location=item.get("location") or "",
            apply_url=clean_url(item.get("jobUrl", "")),
            description=item.get("descriptionPlain") or "",
            posted_raw=item.get("publishedAt"),
            posted_source="ashby.publishedAt" if item.get("publishedAt") else None,
            workplace_hint=workplace,
            country_hint=address.get("addressCountry") or "",
            salary_hint=_salary(item.get("compensation")),
        ))
    return jobs


def fill_details(company: dict, job: Job, settings: dict) -> None:
    """Ashby's listing already carries everything."""
