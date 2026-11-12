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
- The skills matcher must never include DAX or causal inference (both are
  gap flags instead). Spark, Kafka and ODBC also stay out of the skills list.

## Layout

```
fetcher/
  fetch.py       main pipeline -> data/jobs.json
  discover.py    builds data/companies.json (manual + public GitHub lists + USCIS probes)
  uscis.py       USCIS H-1B CSV -> data/uscis/h1b_lookup.json, and the Lookup matcher
  company_lists.py  Fortune 500 + unicorn names -> data/company_lists.json, and the badge matcher
  cap_exempt.py  R1 + AAU names -> data/cap_exempt.json; finds and verifies their job boards
  boards/        one module per ATS: greenhouse, lever, ashby, workday, smartrecruiters
  filters.py     title, years, US/non-US, work mode, location group, sponsorship, PERM
  scoring.py     skills match %
  parse.py       HTML-to-text, URL cleaning, salary parsing
  net.py         polite HTTP with disk cache (not http.py: that shadows the stdlib)
  gate.py        workflow freshness gate: skip a scheduled run if the last fetch is under 90 min old
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
  fit.js         Best fit order (loaded before app.js; tested under Node)
  insights.html  charts page (KPIs, heatmap, weekly trend, time to apply,
  insights.js    funnel, location split, top companies), hand-built SVG
tests/           unittest smoke tests and text fixtures
.github/workflows/fetch.yml  scheduled fetch; commits jobs.json as github-actions[bot]
index.html       redirects the GitHub Pages root to site/
docs/screenshots README images (taken with a fresh browser profile, no personal state)
```

The scheduled run fails (and commits nothing) only when more than half the
boards error (`MAX_FAILED_SHARE` in `fetch.py`). Fewer failures show as warnings.

The workflow has backup cron slots every hour at :17 and :47 (6 AM-11:47 PM
ET, every day) because GitHub drops or delays most scheduled runs. Its `gate`
job runs `fetcher/gate.py` (stdlib only, on the runner's own Python, sparse
checkout) and skips the fetch when `meta.generated_utc` is under 90 minutes
old; a manual Run workflow always fetches. Concurrency group `fetch` with
`cancel-in-progress: false`: a fetch in progress is never cancelled.

## Running

Windows paths shown; on macOS/Linux use `.venv/bin/python`.

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

.venv/Scripts/python fetcher/fetch.py              # all boards with status "ok"
.venv/Scripts/python fetcher/fetch.py --only veeva # one slug while tuning
.venv/Scripts/python fetcher/fetch.py --limit 20   # first N companies

.venv/Scripts/python fetcher/discover.py --no-uscis  # fast: manual + public GitHub lists
.venv/Scripts/python fetcher/discover.py             # full: also probes 8,000 USCIS employers + Workday (hours; cached 30 days)

.venv/Scripts/python fetcher/uscis.py data/uscis/<export>.csv  # rebuild the lookup
.venv/Scripts/python fetcher/company_lists.py   # refresh Fortune 500 / unicorn names
.venv/Scripts/python fetcher/cap_exempt.py lists           # refresh R1 + AAU names (slow: Wikidata)
.venv/Scripts/python fetcher/cap_exempt.py boards --apply  # find their boards from official sites

.venv/Scripts/python -m unittest -v
```

The dashboard fetches `../data/jobs.json`, so serve the repo root over HTTP
(opening the file directly won't load data), then visit `http://127.0.0.1:8765/site/`:

```
.venv/Scripts/python -m http.server 8765 --bind 127.0.0.1
```

A full fetch over ~1,400 boards takes about 30 minutes with an empty HTTP cache
(8 boards at a time, see `FETCH_WORKERS`). Logs worth reading after a run:

- `data/cache/logs/uscis_near_misses.log`: closest-name and unverified USCIS
  matches. These are tagged `unknown`; confirm real ones by adding an alias.
- `data/cache/logs/sponsorship_tier2.log`: every citizenship/clearance sentence
  and whether it was tagged `no`, for checking precision.
- `data/cache/logs/staffing.log`: every company with active jobs flagged
  `staffing`, with each job's matched phrase.
- `data/cache/logs/mismatch.log`: every active job tagged with a role-type
  mismatch, with the phrases that triggered it.

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
  Tier 1 also matches flexible wordings anywhere in the description, whatever the section
  ("without (the need for) (visa) sponsorship", "not able to sponsor", "unable to provide
  sponsorship", "not eligible for immigration sponsorship", "sponsorship is not available");
  "preferred" and "additional" headings affect years only. A JD "no" always beats a USCIS "likely".
- **Staffing:** `staffing: true` when any of these hold (all lists in
  `data/settings.json`):
  - the company is in `staffing_companies`;
  - the description matches a `staffing.phrases` regex: C2C, W2 only, "our
    client is/needs…", "Implementation Partner:", bench sales/consultants,
    "Duration: 6 months" / "6+ months contract", contract to hire / C2H, or
    a self-statement ("we are a … staffing", "IT staffing");
  - a line names 3+ of OPT, CPT, H-1B, EAD, GC, Citizen (a staffing visa
    list), unless it talks about sponsorship;
  - 2+ of the company's active jobs are flagged on their own evidence
    (`company_min_flagged`; recomputed every run in `fetch.company_staffing`);
  - the employer's USCIS NAICS sector is in `staffing.naics_sectors`.

  A phrase in a sentence that turns agencies away ("direct-hire", "not open
  to", "unsolicited resumes", "placement fee") doesn't count, and bare
  "recruiting agency" never does (employers' agency notices use it).
  Duration, contract to hire and "our client" are `weak_phrases`: a job
  flagged on one of those alone is marked `[weak]` in the log. USCIS only
  publishes 2-digit sectors; sector 56 also holds credit bureaus, PEOs and
  job boards, so `naics_sectors` starts empty. Hidden by default behind
  "Show staffing firms".
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
  Application numbers start at 1 and are never reused. Won't-sponsor, PERM,
  staffing, mismatched, stretch and older postings are hidden by default behind six toggles that show counts.
- **Freshness** (in `app.js`) is measured from the posted date, or first-seen when
  a job has none; a backfill job with no posted date counts as older. Under 24h
  gets the ink "Fresh" badge and the Fresh only chip; only the last 7 days show
  by default ("Show older postings"). Newest sorts by that date with first-seen
  as the tie-break only; Recently found sorts by first-seen. Greenhouse posted
  dates come from `first_published`, never `updated_at`.
- **Experience:** a job is dropped when the minimum stated years is 4 or more
  (`max_years_required` 3): "3-5 years" stays, "4+" and "5-7" go.
  The figure (`min_years_required`) is the HIGHEST across separate sentences
  and bullets, since each is its own requirement; within one sentence offering
  alternatives joined by "or" ("5 years, or 2 years with a Master's") it is the
  lowest option the candidate meets (doctorate options don't count). A bullet
  opening with "or" continues the one before it. "and/or" is not an alternative.
  A preferred word makes a figure preferred only when it applies to the years themselves:
  just before ("Ideally 5+ years"), just after in the same clause ("3+ years preferred"),
  or ending a one-figure bullet as "strongly/highly preferred", "is preferred" or "a plus".
  Domain hedges ("ideally on data", "(fintech or lending preferred)") leave the years required.
  Degree-versus-
  experience bullets (a degree bullet with a figure, plus an "equivalent combination of
  education and experience" line) are alternatives: the lowest counts. A heading with a
  parenthetical note ("Desired Qualifications (We're committed...)") still ends the
  required section. "Additional Qualifications (and Skills)" is a preferred section, like
  "Preferred" or "Desired Qualifications"; "Basic Qualifications" stays required.
- **Bands** (`filters.experience_band`, patterns in `settings.json` → `bands`):
  every kept job is `stretch` when its minimum required years is 3 or its
  title has a II/III level (case-sensitive), else `target` (0-2 years or none
  stated). A stretch signal beats an entry-level title ("Associate Analyst"
  asking 3 years is stretch); entry-level title signals are only reasons.
  Senior/Sr./Principal never reach banding (the title drop list). The
  dashboard shows a "Target" badge and hides stretch behind "Show stretch roles".
- **Cap-exempt, two tiers** (`cap_exempt` on each job: `"confirmed"`,
  `"possible"` or null). Confirmed: `data/cap_exempt.json` (Carnegie R1 +
  AAU, Carnegie campus spellings as aliases) plus `cap_exempt.medical_centers`
  and `cap_exempt_employers` in `settings.json`. Possible: USCIS employers
  (exact or alias match only) in NAICS sector 61 with a university-like name,
  minus `possible_exclude` (for-profit schools) and anything matching
  `never_possible` (hospitals are badged only when confirmed). Confirmed
  employers are matched
  like the company badges (exact normalized name or alias via
  `cap_exempt_aliases`/`uscis_aliases`; near-misses only logged). A shared
  system board can mark only some jobs with `cap_exempt_locations` on its
  `companies.json` entry (Beth Israel Lahey Health: jobs located at BIDMC).
  Cap-exempt jobs also keep `cap_exempt_titles` (research/clinical data
  analyst, data coordinator), and their Workday/SmartRecruiters boards also
  search `cap_exempt_search_terms`. Badge and chip are neutral, never green.
- **Match %** is skill overlap with `skills` in `settings.json` (my
  experience pool; whole words, "R" only in language forms), then minus
  `gap_penalty` (15) per `gap_skills` entry named in the job's required section,
  floor 0. `match_raw` keeps the score before penalties. Required means the
  required section (the whole description outside preferred sections when
  there are no headings), the same split as the years rule; preferred-only
  gaps show a "Gap" chip with no penalty, and mentions elsewhere are ignored.
- **Role-type mismatch** (`mismatch` in `settings.json`, `filters.role_mismatch`):
  a type applies on a title pattern, or on `min_hits` different requirement
  patterns. Patterns need clear wording: "support the ARNP/RN" isn't nursing,
  a Java/C++ language list isn't software engineering (the languages count
  as one signal), and "partner with account executives" isn't sales.
  The `federal` type (Public Trust, clearance eligible / ability to obtain a
  clearance, federal client, U.S. citizenship required, "Clearance Required:
  <anything but None>", an active Secret/TS clearance, or "Cleared"/TS/SCI in
  the title) has `"scope": "description"`: it reads the whole description,
  because those lines often sit in a header or footer.
  Hidden by default behind "Show mismatched roles"; logged to `mismatch.log`.
- **Find people:** each card links to LinkedIn and Google (`site:linkedin.com/in`)
  searches for the hiring manager, a recruiter, a peer and Northeastern
  alumni. Keywords come from `filters.people_search` and `people_search` in
  `settings.json`. Links only: nothing ever fetches LinkedIn or Google.
- **Best fit** is the default sort (`site/fit.js`, tested under Node by
  `BestFitTests`): jobs matching 30%+ (`FIT_MIN_MATCH`) come before all
  others. Within each group Target band comes first; cap-exempt (confirmed,
  then possible) and sponsorship likely boost only the 30%+ group; then
  match %, then focus functions (`focus_functions`: revenue/sales/commercial
  operations, BI titles) ahead of other roles with the same match %, then
  Newest. Saved prefs without `sortV` switch to it once.
- **Shortlist:** `state.shortlist` (`{id: {added, sent}}`) in `jobradar.v1`,
  included in Export/Import. "Export to career-ops" downloads `shortlist.json`
  (`app: "job-radar"`, `kind: "career-ops-shortlist"`) with the unsent jobs
  and marks them sent. The local career-ops install's `import-jobradar.mjs`
  reads it; keep the field names stable.
- **Company badges:** `fetcher/company_lists.py` builds `data/company_lists.json`
  (2026 Fortune 500 and the CB Insights unicorn tracker, names only, with list
  dates). Badges need an exact normalized or alias match (`uscis_aliases` plus
  `list_aliases`); same-name different companies go in `list_exclude`. Prefix
  matches are only logged to `data/cache/logs/company_list_near_misses.log`.
- The site has one green accent, used only for sponsorship-likely and key
  numbers. Buttons, tags and chips must never wrap or overflow (`white-space: nowrap`).
- **Discovery honesty:** every board in `companies.json` answered its own API
  with a live job. Workday sponsor probing relies on 422 (no tenant on that
  pod) vs 404 (right tenant, wrong site); Workday's site root returns 406 to
  our User-Agent, so the site name is never read from there. Guessed boards
  without a company-name check (Lever, Ashby, unconfirmed Workday) are
  `"verified": false` and stay `unknown` for sponsorship.
- Board modules expose `list_jobs(company, settings)`, `fill_details(company, job, settings)`
  and `NEEDS_DETAILS`. To support a new ATS, add a module in `fetcher/boards/`
  and register it in `boards/__init__.py`.
- Keep code commented and simple; no abstractions until three cases need them.
- Add a test in `tests/test_smoke.py` for every filter rule you change.

## Adding a company

1. Find the company's job board. Supported: Greenhouse
   (`boards-api.greenhouse.io/v1/boards/<slug>/jobs`), Lever
   (`api.lever.co/v0/postings/<slug>`), Ashby
   (`api.ashbyhq.com/posting-api/job-board/<slug>`), Workday
   (`<tenant>.wd<pod>.myworkdayjobs.com/<site>`) and SmartRecruiters
   (`api.smartrecruiters.com/v1/companies/<id>/postings`, IDs case-insensitive).
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
