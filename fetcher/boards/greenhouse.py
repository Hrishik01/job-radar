"""Greenhouse job board API (boards-api.greenhouse.io)."""

from __future__ import annotations

from models import Job
from net import request_json
from parse import clean_url, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards"
NEEDS_DETAILS = True  # the listing has no description


def _perm_flag(metadata: list | None) -> bool | None:
    """Some companies tag PERM notices with a custom 'PERM Job?' field."""
    for field in metadata or []:
        if "perm" in (field.get("name") or "").lower() and isinstance(field.get("value"), bool):
            return field["value"]
    return None


def list_jobs(company: dict, settings: dict) -> list[Job]:
    http = settings["http"]
    data = request_json(
        "GET", f"{API}/{company['slug']}/jobs",
        delay=http["delay_seconds"], cache_seconds=http["list_cache_minutes"] * 60,
    )
    jobs = []
    for item in data.get("jobs", []):
        jobs.append(Job(
            company=company["name"],
            board="greenhouse",
            slug=company["slug"],
            native_id=str(item["id"]),
            title=item.get("title", "").strip(),
            location=(item.get("location") or {}).get("name", ""),
            apply_url=clean_url(item.get("absolute_url", "")),
            posted_raw=item.get("updated_at"),
            posted_source="greenhouse.updated_at",
            perm_flag=_perm_flag(item.get("metadata")),
        ))
    return jobs


def fill_details(company: dict, job: Job, settings: dict) -> None:
    data = request_json(
        "GET", f"{API}/{company['slug']}/jobs/{job.native_id}?pay_transparency=true",
        delay=settings["http"]["delay_seconds"], cache_seconds=7 * 24 * 3600,
    )
    job.description = html_to_text(data.get("content"))
    # first_published is a real creation date when present; updated_at is not.
    if data.get("first_published"):
        job.posted_raw = data["first_published"]
        job.posted_source = "greenhouse.first_published"
    for pay in data.get("pay_input_ranges") or []:
        if pay.get("min_cents") is not None:
            job.salary_hint = {
                "min": pay["min_cents"] / 100,
                "max": (pay.get("max_cents") or pay["min_cents"]) / 100,
                "currency": pay.get("currency_type") or "USD",
                "interval": "year",  # Greenhouse pay ranges are annual
            }
            break
