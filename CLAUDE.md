# job-radar

Tracks entry-level analytics jobs from public company job boards and tags each
one with location group, work mode, salary, H-1B sponsorship signal, PERM
notice flag and a skills match percentage. Output is one file: `data/jobs.json`.

## Hard rules

- **No AI/LLM calls at runtime.** Everything is regex and plain Python.
- **Public job-board APIs only.** No LinkedIn, Indeed or Handshake scraping.
- **Honest User-Agent only** (`job-radar/0.1 ...` in `fetcher/net.py`). Never
  spoof browser headers. If a board blocks honest requests, mark it
  `"status": "unsupported"` with a note instead of working around it.
- **Be polite:** every request goes through `fetcher/net.py`, which adds a
  per-host delay, retries with backoff and caches responses in `data/cache/`.
- **No secrets, API keys or personal details in the repo**, including this file.
- **Ask before installing packages.** Dependencies are the stdlib plus `requests`.
- The skills matcher must never include dbt, DAX, Looker/LookML, A/B testing
  or causal inference.

## Layout

```
fetcher/
  fetch.py       main pipeline -> data/jobs.json
  discover.py    builds data/companies.json (manual + SimplifyJobs + USCIS probes)
  uscis.py       USCIS H-1B CSV -> data/uscis/h1b_lookup.json, and the Lookup matcher
  boards/        one module per ATS: greenhouse, lever, ashby, workday
  filters.py     title, years, US/non-US, work mode, location group, sponsorship, PERM
  scoring.py     skills match %
  parse.py       HTML-to-text, URL cleaning, salary parsing
  net.py         polite HTTP with disk cache (not http.py: that shadows the stdlib)
  models.py      Job dataclass and stable ID
data/
  settings.json  editable lists: titles, skills, Boston-area towns, USCIS aliases
  companies.json boards to fetch (generated; manual entries survive regeneration)
  jobs.json      output, committed
  uscis/         raw CSV gitignored; compact lookup committed
  cache/         HTTP cache, probe cache, logs (gitignored)
site/
  index.html     dashboard: New / Applied / Removed tabs, filters, dialogs
  styles.css     ledger theme, light and dark (system default plus toggle)
  app.js         loads ../data/jobs.json; your status lives in localStorage
  insights.html  charts page (KPIs, heatmap, weekly trend, time to apply,
  insights.js    funnel, location split, top companies), hand-built SVG
tests/           unittest smoke tests and text fixtures
.github/workflows/fetch.yml  scheduled fetch; commits jobs.json as github-actions[bot]
index.html       redirects the GitHub Pages root to site/
docs/screenshots README images (taken with a fresh browser profile, no personal state)
```

The scheduled run fails (and commits nothing) only when more than half the
boards error (`MAX_FAILED_SHARE` in `fetch.py`). Fewer failures show as warnings.

## Running

Windows paths shown; on macOS/Linux use `.venv/bin/python`.

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

.venv/Scripts/python fetcher/fetch.py              # all boards with status "ok"
.venv/Scripts/python fetcher/fetch.py --only veeva # one slug while tuning
.venv/Scripts/python fetcher/fetch.py --limit 20   # first N companies

.venv/Scripts/python fetcher/discover.py --no-uscis  # fast: manual + SimplifyJobs
.venv/Scripts/python fetcher/discover.py             # full: also probes USCIS slugs (~80 min)

.venv/Scripts/python fetcher/uscis.py data/uscis/<export>.csv  # rebuild the lookup

.venv/Scripts/python -m unittest -v
```

The dashboard fetches `../data/jobs.json`, so serve the repo root over HTTP
(opening the file directly won't load data), then visit `http://127.0.0.1:8765/site/`:

```
.venv/Scripts/python -m http.server 8765 --bind 127.0.0.1
```

A full fetch over ~240 boards takes about 10 minutes. Logs worth reading after a run:

- `data/cache/logs/uscis_near_misses.log`: closest-name and unverified USCIS
  matches. These are tagged `unknown`; confirm real ones by adding an alias.
- `data/cache/logs/sponsorship_tier2.log`: every citizenship/clearance sentence
  and whether it was tagged `no`, for checking precision.

## Conventions

- **Job IDs** are `sha1(board:slug:native_id)[:16]`, never derived from URLs.
  Reruns must never create duplicate IDs; `first_seen_utc` is never overwritten.
- A job missing from a board that fetched successfully becomes `active: false`.
  Jobs from a board that errored are left untouched, so a flaky board can't
  shrink the file.
- **Sponsorship:** `no` if the description says so (Tier 1 phrases, or Tier 2
  citizenship/clearance sentences with requirement wording, EEO boilerplate
  excluded). Otherwise `likely` only on an exact or alias USCIS match.
  Closest-name (prefix) matches and unverified USCIS-guessed boards stay `unknown`.
- **Location:** a foreign country in the title drops the job. In the location
  it drops the job only if no listed location is in the US. Location group
  comes from location text (Boston-area towns first); work mode from the feed
  field, then location text, then description.
- Tunable lists live in `data/settings.json`, not in code.
- **Backfill:** `meta.boards_first_fetched_utc` records each board's first
  fetch, and jobs found on it get `"backfill": true`. Their first-seen time is
  when tracking started, not when they were posted, so time-based charts and
  "new this week" leave them out. `meta.first_sweep_utc` is when tracking began.
- **Insights charts never draw from too little history.** Each one has a
  minimum (the `MIN` table in `insights.js`) and shows a "Not enough history
  yet" panel with progress until it's met. Charts are plain SVG; ask before
  adding a chart library.
- **Dashboard state** (applications, removed, opened, preferences, the outreach
  background text) lives only in the browser's `localStorage` under
  `jobradar.v1`. Export/Import JSON is the backup. Never commit an export.
  Application numbers start at 1 and are never reused. Won't-sponsor and PERM
  jobs are hidden by default behind two toggles that show counts.
- The site has one green accent, used only for sponsorship-likely and key
  numbers. Buttons, tags and chips must never wrap or overflow (`white-space: nowrap`).
- Board modules expose `list_jobs(company, settings)`, `fill_details(company, job, settings)`
  and `NEEDS_DETAILS`. To support a new ATS, add a module in `fetcher/boards/`
  and register it in `boards/__init__.py`.
- Keep code commented and simple; no abstractions until three cases need them.
- Add a test in `tests/test_smoke.py` for every filter rule you change.

## Adding a company

1. Find the company's job board. Supported: Greenhouse
   (`boards-api.greenhouse.io/v1/boards/<slug>/jobs`), Lever
   (`api.lever.co/v0/postings/<slug>`), Ashby
   (`api.ashbyhq.com/posting-api/job-board/<slug>`) and Workday
   (`<tenant>.wd<pod>.myworkdayjobs.com/<site>`).
2. Add an entry to `data/companies.json` with `"source": "manual"`:

   ```json
   {"name": "HubSpot", "board": "greenhouse", "slug": "hubspotjobs", "source": "manual", "status": "ok"}
   {"name": "Moderna", "board": "workday", "slug": "moderna", "tenant": "modernatx", "pod": 1, "site": "M_tx", "source": "manual", "status": "ok"}
   ```

   Unsupported boards (iCIMS, Avature, blocked) get `"status": "unsupported"`
   and a `note`, so the next person doesn't research them again.
3. Run `fetcher/fetch.py --only <slug>` and check the output.
4. If the company sponsors H-1B but its USCIS name differs (e.g. "Walmart" vs
   "WAL-MART ASSOCIATES INC"), add `"<company name>": "<normalized USCIS name>"`
   to `uscis_aliases` in `data/settings.json`. Verify the USCIS entry's city,
   state and industry match the company before aliasing.
