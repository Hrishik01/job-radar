"""The Job record every board adapter produces, plus its stable ID."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


def stable_id(board: str, slug: str, native_id: str) -> str:
    """Hash of company board + slug + the board's own job ID.

    The apply URL is deliberately left out: it carries tracking parameters
    that change between runs and would mint a new ID for the same job.
    """
    key = f"{board}:{slug.lower()}:{native_id}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class Job:
    # Identity
    company: str
    board: str
    slug: str
    native_id: str
    # Raw fields from the feed
    title: str
    location: str = ""
    apply_url: str = ""
    description: str = ""          # plain text; may be filled later by fill_details()
    posted_raw: str | None = None  # whatever the board gives, unverified
    posted_source: str | None = None  # which feed field posted_raw came from
    # Board-specific hints, used before falling back to text parsing
    workplace_hint: str = ""       # e.g. Lever/Ashby workplaceType
    country_hint: str = ""         # e.g. Lever "US", Ashby "United States"
    salary_hint: dict | None = None  # {"min", "max", "interval", "currency"}
    perm_flag: bool | None = None  # Greenhouse "PERM Job?" metadata, when present
    extra: dict = field(default_factory=dict)  # adapter-private data (e.g. Workday detail path)

    @property
    def id(self) -> str:
        return stable_id(self.board, self.slug, self.native_id)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("extra")
        return {"id": self.id, **data}
