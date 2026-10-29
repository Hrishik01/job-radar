"""Lever postings API (api.lever.co). Paged with skip/limit because some
boards return tens of megabytes in one response."""

from __future__ import annotations

from datetime import datetime, timezone

from models import Job
from net import request_json
from parse import clean_url, html_to_text

API = "https://api.lever.co/v0/postings"
NEEDS_DETAILS = False
PAGE = 100


def _description(item: dict) -> str:
    """Rebuild the posting with its section headings so scoring can find
    the requirements section."""
    parts = [item.get("descriptionPlain") or ""]
    for section in item.get("lists") or []:
        parts.append(f"{section.get('text', '').strip()}\n{html_to_text(section.get('content'))}")
    parts.append(item.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def list_jobs(company: dict, settings: dict) -> list[Job]:
    http = settings["http"]
    jobs = []
    skip = 0
    while True:
        page = request_json(
            "GET", f"{API}/{company['slug']}?mode=json&skip={skip}&limit={PAGE}",
            delay=http["delay_seconds"], cache_seconds=http["list_cache_minutes"] * 60,
        )
        for item in page:
            created = item.get("createdAt")
            salary = item.get("salaryRange") or None
            jobs.append(Job(
                company=company["name"],
                board="lever",
                slug=company["slug"],
                native_id=item["id"],
                title=item.get("text", "").strip(),
                location=(item.get("categories") or {}).get("location") or "",
                apply_url=clean_url(item.get("hostedUrl", "")),
                description=_description(item),
                posted_raw=(datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
                            if created else None),
                posted_source="lever.createdAt" if created else None,
                workplace_hint=item.get("workplaceType") or "",
                country_hint=item.get("country") or "",
                salary_hint=salary and {
                    "min": salary.get("min"), "max": salary.get("max"),
                    "currency": salary.get("currency"), "interval": salary.get("interval"),
                },
            ))
        if len(page) < PAGE:
            return jobs
        skip += PAGE


def fill_details(company: dict, job: Job, settings: dict) -> None:
    """Lever's listing already carries everything."""
