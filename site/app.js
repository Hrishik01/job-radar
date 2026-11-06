// Job Radar dashboard. Plain JS, no build step.
// Reads ../data/jobs.json (written by fetcher/fetch.py) and keeps your own
// status (applied, removed, opened, preferences) in localStorage.
"use strict";

const STORE_KEY = "jobradar.v1";
const EXPORT_NAG_DAYS = 14;
const DUPE_THRESHOLD = 0.6;           // Dice similarity of title tokens
const MODES = ["remote", "hybrid", "onsite"];

// ---------- State ----------

function defaultState() {
  return {
    apps: [],          // {n, job_id|null, company, title, url, date, manual}
    nextNum: 1,        // running application number, never reused
    removed: {},       // job id -> ISO time removed
    opened: {},        // job id -> ISO time Open was clicked (pending "Did you apply?")
    openedLog: {},     // job id -> ISO time first opened; never cleared (insights funnel)
    lastExport: null,  // ISO time of the last export
    shortlist: {},     // job id -> {added: ISO time, sent: ISO time | null} (career-ops export)
    prefs: {
      tab: "new", theme: "system", sort: "fit", sortV: 1, loc: "all",
      modes: MODES.slice(), showNo: false, showPerm: false, showStaffing: false, showStretch: false,
      showOlder: false, freshOnly: false, bigCo: false, capExempt: false,
      spons: "any", salary: 0, match: 0, background: "",
    },
  };
}

// Merges saved state over the defaults. Saved prefs from before Best fit
// existed (no sortV) switch to Best fit once; after that your choice sticks.
function mergeState(saved) {
  const base = defaultState();
  const prefs = { ...base.prefs, ...saved.prefs };
  if (!saved.prefs?.sortV) { prefs.sort = "fit"; prefs.sortV = 1; }
  return { ...base, ...saved, prefs };
}

function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (saved) return mergeState(saved);
  } catch (e) { /* storage blocked or corrupt: start fresh */ }
  return defaultState();
}

function saveState() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); }
  catch (e) { toast("Couldn't save to this browser's storage. Export a backup."); }
}

let state = loadState();
let jobs = [];             // from jobs.json
let meta = {};
let jobById = new Map();
let currentId = null;      // row selected with j/k
let visibleIds = [];       // ids in the order they're rendered
let openedThisSession = false;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------- Small helpers ----------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Only allow http(s) links into href attributes
function safeUrl(u) {
  return /^https?:\/\//i.test(u || "") ? u : "";
}

function todayLocal() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// "2026-10-12" -> local Date (avoids the UTC shift of new Date("2026-10-12"))
function localDate(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function shortDate(ymd) {
  return localDate(ymd).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function daysSince(t) {
  return Math.max(0, Math.floor((Date.now() - t) / 86400000));
}

function ageLabel(t) {
  if (!t) return "";
  const d = daysSince(t);
  return d === 0 ? "today" : `${d}d`;
}

// The company's own posted date, or 0 when the board gives none
function postedOn(job) {
  return Date.parse(job.posted_raw || "") || 0;
}

// For sorting and freshness: the posted date, else when the fetcher first saw
// the job. A backfill job with no posted date was already listed when tracking
// began, so its age is unknown and it counts as older than everything (0).
function postedTime(job) {
  if (postedOn(job)) return postedOn(job);
  return job.backfill ? 0 : Date.parse(job.first_seen_utc) || 0;
}

// Freshness rules, all measured from postedTime()
const FRESH_HOURS = 24;   // "Fresh" badge and the Fresh only chip
const RECENT_DAYS = 7;    // older postings hidden unless "Show older postings" is on

function ageHours(job) {
  return (Date.now() - postedTime(job)) / 3600000;
}
const isFresh = (job) => ageHours(job) < FRESH_HOURS;
const isOlder = (job) => ageHours(job) > RECENT_DAYS * 24;

// Fortune 500 / unicorn badges, set by the fetcher from data/company_lists.json
const isBigCo = (job) => Boolean(job.fortune500 || job.unicorn);
// capTier / capRank live in fit.js (loaded first), next to the Best fit order
const isCapExempt = (job) => capRank(job) > 0;
// Experience band from the fetcher: "target" (0-2 years, none stated) or "stretch"
const isStretch = (job) => job.band === "stretch";

function companyBadges(job) {
  return (job.fortune500 ? `<span class="co-badge" title="On the Fortune 500 list">Fortune 500</span>` : "") +
    (job.unicorn ? `<span class="co-badge" title="Private, valued at $1B+ (unicorn list)">Unicorn</span>` : "") +
    (capTier(job) === "confirmed" ? `<span class="co-badge" title="R1/AAU university, its medical center, or a listed Boston employer: H-1B cap-exempt, no lottery">Cap-exempt</span>`
      : capTier(job) === "possible" ? `<span class="co-badge soft" title="USCIS lists this employer under educational services with a university-like name. Probably cap-exempt; confirm before relying on it.">Cap-exempt?</span>` : "");
}

// "Find people": search links only. The app never fetches LinkedIn or Google;
// each link opens the search in a new tab. Keywords come from the fetcher
// (settings.json people_search).
function peopleMenu(job) {
  const ppl = job.people || { manager: ["analytics manager", "head of data"], peer: job.title,
    recruiter: ["recruiter", "talent acquisition", "university recruiting"], alumni: "Northeastern" };
  const co = `"${job.company.trim()}"`;
  const any = (words) => `(${words.map((w) => `"${w}"`).join(" OR ")})`;
  const rows = [
    ["Hiring manager", `${co} ${any(ppl.manager)}`],
    ["Recruiter", `${co} ${any(ppl.recruiter)}`],
    ["Peer", `${co} "${ppl.peer}"`],
    [`${ppl.alumni} alumni`, `${co} "${ppl.alumni}"`],
  ];
  const link = (href, text, label) =>
    `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(label)}">${text}</a>`;
  return `<details class="people-menu"><summary class="btn small">Find people</summary><div class="people-pop">
    ${rows.map(([label, q]) => `<span>${esc(label)}</span>
      ${link(`https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(q)}`, "LinkedIn ↗", `${label} on LinkedIn`)}
      ${link(`https://www.google.com/search?q=${encodeURIComponent(`site:linkedin.com/in ${q}`)}`, "Google ↗", `${label} via Google`)}`).join("")}
  </div></details>`;
}

function bandTag(job) {
  const why = esc((job.band_reasons || []).join(", "));
  if (job.band === "target") return `<span class="tag target" title="${why}">Target</span>`;
  if (job.band === "stretch") return `<span class="tag" title="${why}">Stretch</span>`;
  return "";
}

// "3+ yrs required" from the fetcher's minimum-years figure, when stated;
// else "2+ yrs preferred" when only a preferred section names one
function yearsLabel(job) {
  if (job.min_years != null) return job.min_years === 0 ? "no minimum years" : `${job.min_years}+ yrs required`;
  if (job.min_years_preferred) return `${job.min_years_preferred}+ yrs preferred`;
  return "";
}

// Workday's application end date ("2026-11-13"), counted in local days
function closesLabel(job) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(job.closes_raw || "");
  if (!m) return "";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((new Date(+m[1], m[2] - 1, +m[3]) - today) / 86400000);
  if (days < 0) return "close date passed";
  if (days === 0) return "closes today";
  return days === 1 ? "closes tomorrow" : `closes in ${days} days`;
}

function kMoney(n) {
  return "$" + Math.round(n / 1000) + "k";
}

function salaryText(job) {
  const lo = job.salary_min, hi = job.salary_max;
  if (!lo && !hi) return null;
  if (!lo || !hi || lo === hi) return kMoney(lo || hi);
  return `${kMoney(lo)}–${kMoney(hi)}`;
}

function salaryFull(job) {
  const f = (n) => "$" + n.toLocaleString("en-US");
  const lo = job.salary_min, hi = job.salary_max;
  if (!lo && !hi) return "";
  return lo && hi && lo !== hi ? `${f(lo)} to ${f(hi)}` : f(lo || hi);
}

function topPay(job) {
  return job.salary_max || job.salary_min || 0;
}

function modeLabel(m) {
  return m ? m[0].toUpperCase() + m.slice(1) : "Unknown";
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // Fallback for browsers that block the async clipboard (e.g. file://)
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

// ---------- Duplicate detection ----------
// A new job is flagged when the company matches an application and the
// titles share most of their words (Dice coefficient on word tokens).

const COMPANY_SUFFIX = /\b(inc|incorporated|corp|corporation|llc|ltd|co|company|plc|lp)\b/g;
const TITLE_STOP = new Set(["and", "the", "of", "for", "a", "an", "to", "in", "i", "ii", "iii", "iv", "us", "remote", "hybrid"]);

function normCompany(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(COMPANY_SUFFIX, " ").replace(/\s+/g, " ").trim();
}

function titleTokens(s) {
  return new Set(String(s || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w && !TITLE_STOP.has(w)));
}

function dice(a, b) {
  if (!a.size || !b.size) return 0;
  let shared = 0;
  for (const w of a) if (b.has(w)) shared++;
  return (2 * shared) / (a.size + b.size);
}

function sameCompany(a, b) {
  if (!a || !b) return false;
  return a === b || a.startsWith(b + " ") || b.startsWith(a + " ");
}

// Returns the best-matching application for a job, or null
function findDupe(job) {
  const co = normCompany(job.company);
  const tt = titleTokens(job.title);
  let best = null, bestScore = 0;
  for (const app of state.apps) {
    if (app.job_id === job.id) continue;
    if (!sameCompany(co, normCompany(app.company))) continue;
    const score = dice(tt, titleTokens(app.title));
    if (score >= DUPE_THRESHOLD && score > bestScore) { best = app; bestScore = score; }
  }
  return best;
}

// ---------- Applications ----------

function appliedJobIds() {
  return new Set(state.apps.filter((a) => a.job_id).map((a) => a.job_id));
}

function addApplication({ job_id = null, company, title, url = "", date = todayLocal(), manual = false }) {
  // applied_utc is the exact time you marked it, for time-to-apply; manual
  // entries are backdated, so they only get the date
  const app = { n: state.nextNum, job_id, company: company.trim(), title: title.trim(), url, date, manual,
                applied_utc: manual ? null : new Date().toISOString() };
  state.apps.push(app);
  state.nextNum += 1;
  if (job_id) delete state.opened[job_id];
  saveState();
  return app;
}

function applyToJob(job, date) {
  if (appliedJobIds().has(job.id)) return null;
  delete state.removed[job.id];
  return addApplication({ job_id: job.id, company: job.company, title: job.title, url: job.apply_url, date });
}

// ---------- Filtering ----------

// Active jobs that are still yours to act on
function newPool() {
  const applied = appliedJobIds();
  return jobs.filter((j) => j.active && !applied.has(j.id) && !state.removed[j.id]);
}

function matchesSearch(job, terms) {
  if (!terms.length) return true;
  const hay = `${job.title} ${job.company} ${job.location} ${(job.matched_skills || []).join(" ")}`.toLowerCase();
  return terms.every((t) => hay.includes(t));
}

function searchTerms() {
  return $("#search").value.toLowerCase().split(/\s+/).filter(Boolean);
}

function sortJobs(list) {
  const p = state.prefs;
  // Company's posted date (see postedTime), so Fresh jobs lead. A recent
  // first-seen time only breaks ties; it never lifts an older posting.
  const newest = (a, b) => postedTime(b) - postedTime(a) ||
    Date.parse(b.first_seen_utc) - Date.parse(a.first_seen_utc);
  // Best fit (fit.js): 30%+ matches first; cap-exempt and H-1B likely only
  // boost those
  const fit = (a, b) => fitCompare(a, b, newest);
  const by = {
    fit,
    newest,
    found: (a, b) => Date.parse(b.first_seen_utc) - Date.parse(a.first_seen_utc) || postedTime(b) - postedTime(a),
    match: (a, b) => (b.match_pct || 0) - (a.match_pct || 0) || newest(a, b),
    pay: (a, b) => topPay(b) - topPay(a) || newest(a, b),
    boston: (a, b) => (b.location_group === "Boston area") - (a.location_group === "Boston area") || newest(a, b),
  }[p.sort] || fit;
  return list.sort(by);
}

// Filters the New tab and returns the list plus the counts shown in the UI
function filterNew() {
  const p = state.prefs;
  const pool = newPool();
  const counts = {
    pool: pool.length,
    no: pool.filter((j) => j.sponsorship === "no").length,
    perm: pool.filter((j) => j.perm_likely).length,
    staffing: pool.filter((j) => j.staffing).length,
    stretch: pool.filter(isStretch).length,
    older: pool.filter(isOlder).length,
  };

  const terms = searchTerms();
  const titleQ = $("#f-title").value.trim().toLowerCase();
  const companyQ = $("#f-company").value.trim().toLowerCase();

  // Everything except location and work mode, so their chips can show counts
  const base = pool.filter((j) =>
    (p.showNo || j.sponsorship !== "no") &&
    (p.showPerm || !j.perm_likely) &&
    (p.showStaffing || !j.staffing) &&
    (p.showStretch || !isStretch(j)) &&
    (p.showOlder || !isOlder(j)) &&
    matchesSearch(j, terms) &&
    (!titleQ || j.title.toLowerCase().includes(titleQ)) &&
    (!companyQ || j.company.toLowerCase().includes(companyQ)) &&
    (p.spons === "any" || j.sponsorship === p.spons) &&
    (!p.salary || topPay(j) >= p.salary) &&
    (j.match_pct || 0) >= p.match);

  const inMode = (j) => p.modes.includes(j.work_mode) || (!MODES.includes(j.work_mode) && p.modes.length === MODES.length);
  const isBoston = (j) => j.location_group === "Boston area";

  // The quick-filter chips (Fresh only, Fortune 500 / Unicorn, Cap-exempt)
  // each count what they would show under every other chip
  const underChips = (j) => inMode(j) && (p.loc !== "boston" || isBoston(j));
  const QUICK = [["freshOnly", isFresh], ["bigCo", isBigCo], ["capExempt", isCapExempt]];
  const quickOk = (j, skip) => QUICK.every(([key, test]) => key === skip || !p[key] || test(j));
  counts.fresh = base.filter((j) => isFresh(j) && underChips(j) && quickOk(j, "freshOnly")).length;
  counts.bigCo = base.filter((j) => isBigCo(j) && underChips(j) && quickOk(j, "bigCo")).length;
  counts.capExempt = base.filter((j) => isCapExempt(j) && underChips(j) && quickOk(j, "capExempt")).length;
  const shown = base.filter((j) => quickOk(j));

  const modeOk = shown.filter(inMode);
  counts.locAll = modeOk.length;
  counts.locBoston = modeOk.filter(isBoston).length;

  const locOk = p.loc === "boston" ? shown.filter(isBoston) : shown;
  for (const m of MODES) counts[m] = locOk.filter((j) => j.work_mode === m).length;

  return { list: sortJobs(locOk.filter(inMode)), counts };
}

// ---------- Rendering ----------

function sponsTag(job) {
  if (job.sponsorship === "likely") return `<span class="tag likely" title="Company appears in USCIS H-1B data">H-1B likely</span>`;
  if (job.sponsorship === "no") return `<span class="tag no" title="${esc(job.sponsorship_evidence || "Description says no sponsorship")}">Won't sponsor</span>`;
  return `<span class="tag">Sponsor unknown</span>`;
}

function jobRow(job, tab) {
  const salary = salaryText(job);
  const dupe = tab === "new" ? findDupe(job) : null;
  const skills = job.matched_skills || [];
  const pct = job.match_pct || 0;
  const url = safeUrl(job.apply_url);
  const opened = state.opened[job.id];

  const listed = state.shortlist[job.id];
  const actions = tab === "removed"
    ? `<button class="btn small" data-act="restore">Restore</button>`
    : tab === "shortlist"
    ? `<button class="btn small" data-act="applied">Mark applied</button>
       <button class="btn small" data-act="unshortlist">Remove from shortlist</button>`
    : `<button class="btn small" data-act="applied">Mark applied</button>
       <button class="btn small" data-act="remove">Remove</button>
       <button class="btn small" data-act="${listed ? "unshortlist" : "shortlist"}" aria-pressed="${Boolean(listed)}">${listed ? "Shortlisted ✓" : "Shortlist"}</button>`;

  return `
  <article class="job${job.id === currentId ? " current" : ""}" data-id="${esc(job.id)}" aria-label="${esc(job.title)} at ${esc(job.company)}">
    ${dupe ? `<p class="dupe">Possible repost: similar to <b>#${dupe.n} ${esc(dupe.title)}</b>, applied ${esc(shortDate(dupe.date))}</p>` : ""}
    <div class="job-main">
      ${isFresh(job) ? `<p class="fresh-badge">Fresh</p>` : ""}
      <h2 class="job-title">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-act="open">${esc(job.title)}</a>` : esc(job.title)}</h2>
      <p class="job-co">${esc(job.company.trim())} ${companyBadges(job)}<span class="muted">· ${esc(job.location)}</span></p>
      <p class="job-meta mono">
        ${job.location_group === "Boston area"
          ? `<span>Boston area · ${esc(modeLabel(job.work_mode))}</span>`
          : `<span>${esc(job.location_group || modeLabel(job.work_mode))}</span>`}
        ${postedOn(job) ? `<span>posted ${ageLabel(postedOn(job))}</span>` : ""}
        <span>found ${ageLabel(Date.parse(job.first_seen_utc))}</span>
        ${yearsLabel(job) ? `<span>${yearsLabel(job)}</span>` : ""}
        ${job.active && closesLabel(job) ? `<span>${closesLabel(job)}</span>` : ""}
        ${job.active ? "" : "<span>no longer listed</span>"}
      </p>
      <div class="match">
        <div class="match-row">
          <div class="match-bar" role="img" aria-label="${pct}% skills match"><i style="width:${Math.min(pct, 100)}%"></i></div>
          <span class="match-pct mono">${pct}% match</span>
        </div>
        <p class="match-skills">${skills.length ? esc(skills.join(" · ")) : "No listed skills found in the description"}</p>
      </div>
    </div>
    <div class="actions">
      ${url ? `<a class="btn small primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-act="open">Open ↗</a>` : ""}
      ${actions}
      ${peopleMenu(job)}
      <button class="btn small" data-act="outreach">Copy outreach</button>
      <button class="btn small" data-act="tailor">Copy tailoring</button>
    </div>
    <div class="job-side">
      ${salary ? `<span class="salary mono" title="${esc(salaryFull(job))}">${salary}</span>
                  <span class="salary-note">${job.salary_hourly ? "from hourly × 2080" : "per year"}</span>`
               : `<span class="salary-note">No salary listed</span>`}
      ${bandTag(job)}
      ${sponsTag(job)}
      ${listed?.sent ? `<span class="tag" title="Exported to career-ops ${esc(new Date(listed.sent).toLocaleString())}">Sent ${esc(shortDate(listed.sent.slice(0, 10)))}</span>` : ""}
      ${job.perm_likely ? `<span class="tag perm" title="${esc((job.perm_markers || []).join(", "))}">PERM notice</span>` : ""}
      ${job.staffing ? `<span class="tag staffing" title="${esc((job.staffing_reasons || []).join(" | "))}">Staffing</span>` : ""}
      ${opened ? `<span class="tag opened">Opened</span>` : ""}
    </div>
  </article>`;
}

function appRow(app) {
  const job = app.job_id ? jobById.get(app.job_id) : null;
  const url = safeUrl(app.url || job?.apply_url);
  const id = app.job_id || `manual-${app.n}`;
  return `
  <article class="job${id === currentId ? " current" : ""}" data-id="${esc(id)}" data-app="${app.n}" aria-label="Application ${app.n}: ${esc(app.title)} at ${esc(app.company)}">
    <div class="job-main">
      <h2 class="job-title">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(app.title)}</a>` : esc(app.title)}</h2>
      <p class="job-co">${esc(app.company)}${job ? ` <span class="muted">· ${esc(job.location)}</span>` : ""}</p>
      <p class="job-meta mono">
        <span>applied ${esc(shortDate(app.date))}</span>
        <span>${app.manual ? "logged manually" : "from the feed"}</span>
        ${job && !job.active ? "<span>no longer listed</span>" : ""}
      </p>
    </div>
    <div class="actions">
      ${url ? `<a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : ""}
      ${job ? `<button class="btn small" data-act="tailor">Copy tailoring</button>` : ""}
      <button class="btn small" data-act="undo">${app.manual ? "Delete" : "Move back to New"}</button>
    </div>
    <div class="job-side">
      <span class="app-num mono">#${app.n}</span>
      ${job ? sponsTag(job) : ""}
    </div>
  </article>`;
}

function emptyState(title, body) {
  return `<div class="empty"><h2>${esc(title)}</h2><p>${esc(body)}</p></div>`;
}

function render() {
  const p = state.prefs;
  const tab = p.tab;
  const terms = searchTerms();
  const applied = appliedJobIds();
  const list = $("#list");
  let html = "";
  let line = "";

  // Tab buttons and counts
  for (const b of $$('[role="tab"]')) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  const pool = newPool();
  $("#count-new").textContent = pool.length;
  $("#count-applied").textContent = state.apps.length;
  $("#count-removed").textContent = Object.keys(state.removed).filter((id) => !applied.has(id)).length;
  $("#count-shortlist").textContent = Object.keys(state.shortlist).length;
  $("#new-filters").hidden = tab !== "new";
  $("#shortlist-bar").hidden = tab !== "shortlist";

  if (tab === "new") {
    const { list: rows, counts } = filterNew();
    visibleIds = rows.map((j) => j.id);
    html = rows.map((j) => jobRow(j, "new")).join("");
    if (!rows.length) html = emptyState("No jobs match these filters", "Loosen a filter or clear the search to see more.");

    $('[data-count="loc-all"]').textContent = counts.locAll;
    $('[data-count="loc-boston"]').textContent = counts.locBoston;
    for (const m of MODES) $(`[data-count="${m}"]`).textContent = counts[m];
    $('[data-count="fresh"]').textContent = counts.fresh;
    // Shown on the folded "Filters and sort" toggle on phones
    const active = [p.sort !== "fit", $("#f-title").value.trim(), $("#f-company").value.trim(),
      p.spons !== "any", p.salary > 0, p.match > 0].filter(Boolean).length;
    $("#active-filters").textContent = active ? `(${active})` : "";
    $("#count-no").textContent = `(${counts.no})`;
    $("#count-perm").textContent = `(${counts.perm})`;
    $("#count-staffing").textContent = `(${counts.staffing})`;
    $("#count-stretch").textContent = `(${counts.stretch})`;
    $("#count-older").textContent = `(${counts.older})`;
    $('[data-count="bigco"]').textContent = counts.bigCo;
    $('[data-count="capexempt"]').textContent = counts.capExempt;

    const hidden = [];
    if (!p.showNo && counts.no) hidden.push(`${counts.no} won't-sponsor`);
    if (!p.showPerm && counts.perm) hidden.push(`${counts.perm} PERM`);
    if (!p.showStaffing && counts.staffing) hidden.push(`${counts.staffing} staffing`);
    if (!p.showStretch && counts.stretch) hidden.push(`${counts.stretch} stretch`);
    if (!p.showOlder && counts.older) hidden.push(`${counts.older} posted over ${RECENT_DAYS} days ago`);
    line = `Showing ${rows.length} of ${counts.pool}` + (hidden.length ? ` · hidden: ${hidden.join(", ")}` : "");
  } else if (tab === "applied") {
    const apps = state.apps
      .filter((a) => !terms.length || terms.every((t) => `${a.title} ${a.company}`.toLowerCase().includes(t)))
      .sort((a, b) => b.n - a.n);
    visibleIds = apps.map((a) => a.job_id || `manual-${a.n}`);
    html = apps.map(appRow).join("") ||
      emptyState("Your applications land here", "Click Open on a job and you'll be asked if you applied next time. Or log one you found elsewhere.");
    line = `${apps.length} application${apps.length === 1 ? "" : "s"}`;
  } else if (tab === "shortlist") {
    // jobs.json keeps closed jobs (active: false), so a shortlisted job is
    // only missing if its file entry is gone; those are skipped
    const rows = Object.keys(state.shortlist).map((id) => jobById.get(id))
      .filter((j) => j && matchesSearch(j, terms))
      .sort((a, b) => state.shortlist[b.id].added.localeCompare(state.shortlist[a.id].added));
    visibleIds = rows.map((j) => j.id);
    html = rows.map((j) => jobRow(j, "shortlist")).join("") ||
      emptyState("Your shortlist is empty", "Click Shortlist on a job in New to collect it here, then export it to career-ops.");
    const unsent = rows.filter((j) => !state.shortlist[j.id].sent).length;
    line = `${rows.length} shortlisted · ${unsent} not yet sent to career-ops`;
    $("#export-shortlist").disabled = unsent === 0;
  } else {
    const rows = jobs.filter((j) => state.removed[j.id] && !applied.has(j.id) && matchesSearch(j, terms));
    rows.sort((a, b) => state.removed[b.id].localeCompare(state.removed[a.id]));
    visibleIds = rows.map((j) => j.id);
    html = rows.map((j) => jobRow(j, "removed")).join("") ||
      emptyState("Nothing removed", "Jobs you remove from New are kept here so you can restore them.");
    line = `${rows.length} removed`;
  }

  list.innerHTML = html;
  $("#result-line").textContent = line;
  if (!visibleIds.includes(currentId)) currentId = null;
  renderKpis();
  renderExportNote();
}

// ---------- KPIs and sparklines ----------

// Buckets times into the last `days` local days, oldest first
function dailyCounts(times, days = 14) {
  const out = new Array(days).fill(0);
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - (days - 1));
  for (const t of times) {
    const i = Math.floor((t - start.getTime()) / 86400000);
    if (i >= 0 && i < days) out[i]++;
  }
  return out;
}

const SPARK_MIN_DAYS = 7;

// Days of real history since the first full sweep (jobs from the sweep
// itself are backfill and say nothing about when things were posted)
function trackedDays() {
  const sweep = Date.parse(meta.first_sweep_utc || "");
  return sweep ? daysSince(sweep) : 0;
}

function spark(id, values) {
  let svg = document.getElementById(id);
  const days = trackedDays();
  // Too little history: a line would just be one spike. Say so instead.
  if (days < SPARK_MIN_DAYS) {
    svg.outerHTML = `<span class="spark-wait" id="${id}" title="Sparklines need ${SPARK_MIN_DAYS} days of tracking">trend in ${SPARK_MIN_DAYS - days}d</span>`;
    return;
  }
  if (svg.tagName.toLowerCase() !== "svg") {
    svg.outerHTML = `<svg class="spark" id="${id}" aria-hidden="true"></svg>`;
    svg = document.getElementById(id);
  }
  const w = 64, h = 22, max = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 1 - (v / max) * (h - 2)}`).join(" ");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<title>Last 14 days</title><polyline fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" points="${pts}"/>`;
}

function renderKpis() {
  const active = jobs.filter((j) => j.active);
  const seen = (list) => list.map((j) => Date.parse(j.first_seen_utc));
  const weekAgo = Date.now() - 7 * 86400000;

  const fresh = jobs.filter((j) => !j.backfill);   // found after the first sweep
  $("#kpi-new").textContent = fresh.filter((j) => Date.parse(j.first_seen_utc) >= weekAgo).length;
  spark("spark-new", dailyCounts(seen(fresh)));

  const likely = active.filter((j) => j.sponsorship === "likely");
  $("#kpi-spons").textContent = active.length ? Math.round((likely.length / active.length) * 100) + "%" : "–";
  // Daily share of newly found jobs tagged likely (carries forward on quiet days)
  const all = dailyCounts(seen(fresh)), lik = dailyCounts(seen(fresh.filter((j) => j.sponsorship === "likely")));
  let last = 0;
  spark("spark-spons", all.map((n, i) => (last = n ? lik[i] / n : last)));

  // Same basis as the Boston area chip with no other filters: jobs in New,
  // minus won't-sponsor, PERM, staffing and older jobs unless their toggles are on
  const p = state.prefs;
  const boston = newPool().filter((j) => j.location_group === "Boston area" &&
    (p.showNo || j.sponsorship !== "no") && (p.showPerm || !j.perm_likely) &&
    (p.showStaffing || !j.staffing) && (p.showStretch || !isStretch(j)) && (p.showOlder || !isOlder(j)));
  $("#kpi-boston").textContent = boston.length;
  spark("spark-boston", dailyCounts(seen(fresh.filter((j) => j.location_group === "Boston area"))));

  $("#kpi-applied").textContent = state.apps.length;
  spark("spark-applied", dailyCounts(state.apps.map((a) => localDate(a.date).getTime())));
}

function renderDateline() {
  const runs = meta.runs || [];
  const lastRun = runs[runs.length - 1];
  const now = new Date().toLocaleDateString("en-US", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  let txt = now;
  if (meta.generated_utc) {
    const g = new Date(meta.generated_utc);
    const when = g.toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    txt += ` · updated ${when} ET`;
  }
  if (lastRun) txt += ` · ${lastRun.boards_ok} boards`;
  $("#dateline").textContent = txt;
}

// ---------- Export / import ----------

function hasStatus() {
  return state.apps.length > 0 || Object.keys(state.removed).length > 0 || Object.keys(state.shortlist).length > 0;
}

function renderExportNote() {
  const note = $("#export-note");
  const banner = $("#export-banner");
  const days = state.lastExport ? daysSince(Date.parse(state.lastExport)) : null;
  const overdue = hasStatus() && (days === null || days >= EXPORT_NAG_DAYS);

  note.textContent = days === null ? "Never exported" : days === 0 ? "Exported today" : `Last export ${days} day${days === 1 ? "" : "s"} ago`;
  note.classList.toggle("overdue", overdue);

  banner.hidden = !overdue;
  if (overdue) {
    banner.innerHTML = `<span>${days === null ? "You haven't exported a backup yet." : `It's been ${days} days since your last backup.`}
      Your status lives only in this browser. Clearing site data would lose it.</span>
      <button class="btn small primary" type="button" data-export>Export now</button>`;
  }
}

function exportBackup() {
  state.lastExport = new Date().toISOString();
  saveState();
  const payload = { app: "job-radar", version: 1, exported_utc: state.lastExport, state };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `job-radar-backup-${todayLocal()}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast("Backup downloaded");
  render();
}

// ---------- Shortlist export (career-ops) ----------

// Downloads shortlist.json with every shortlisted job not sent yet, then marks
// them sent so they're never exported twice. career-ops' import-jobradar.mjs
// reads the newest shortlist*.json from Downloads.
function exportShortlist() {
  const ids = Object.keys(state.shortlist).filter((id) => !state.shortlist[id].sent && jobById.has(id));
  if (!ids.length) { toast("Nothing new to send: every shortlisted job was already exported"); return; }
  const now = new Date().toISOString();
  const day = (t) => (t ? new Date(t).toISOString().slice(0, 10) : null);
  const out = ids.map((id) => {
    const j = jobById.get(id);
    return {
      id: j.id, apply_url: j.apply_url, title: j.title, company: j.company.trim(), location: j.location,
      work_mode: j.work_mode, posted: day(postedOn(j)), band: j.band || null,
      h1b: j.sponsorship, cap_exempt: capTier(j), match_pct: j.match_pct || 0,
      matched_skills: j.matched_skills || [], min_years: j.min_years ?? null,
      staffing: Boolean(j.staffing), perm_likely: Boolean(j.perm_likely),
    };
  });
  const payload = { app: "job-radar", kind: "career-ops-shortlist", version: 1, exported_utc: now, jobs: out };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "shortlist.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  for (const id of ids) state.shortlist[id].sent = now;
  saveState();
  toast(`Exported ${ids.length} job${ids.length === 1 ? "" : "s"} to shortlist.json`);
  render();
}

async function importBackup(file) {
  let data;
  try { data = JSON.parse(await file.text()); }
  catch (e) { toast("That file isn't valid JSON."); return; }
  if (data?.app !== "job-radar" || !Array.isArray(data.state?.apps)) {
    toast("That doesn't look like a Job Radar backup.");
    return;
  }
  const s = data.state;
  const ok = confirm(`Replace your current status with this backup?\n\n` +
    `Backup from ${new Date(data.exported_utc).toLocaleString()}: ${s.apps.length} applications, ` +
    `${Object.keys(s.removed || {}).length} removed, ${Object.keys(s.shortlist || {}).length} shortlisted.\n` +
    `Now: ${state.apps.length} applications, ${Object.keys(state.shortlist).length} shortlisted.`);
  if (!ok) return;
  state = mergeState(s);
  saveState();
  applyPrefsToControls();
  applyTheme();
  render();
  toast(`Imported ${state.apps.length} applications`);
}

// ---------- "Did you apply?" ----------

function pendingOpened() {
  const applied = appliedJobIds();
  return Object.keys(state.opened).filter((id) => jobById.has(id) && !applied.has(id));
}

function askNext() {
  const dlg = $("#ask-dialog");
  if (dlg.open) return;
  const id = pendingOpened()[0];
  if (!id) return;
  const job = jobById.get(id);
  const when = new Date(state.opened[id]).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  $("#ask-body").innerHTML = `You opened <b>${esc(job.title)}</b> at ${esc(job.company.trim())} on ${esc(when)}.`;
  dlg.dataset.id = id;
  dlg.returnValue = "";
  dlg.showModal();
}

function onAskClose() {
  const dlg = $("#ask-dialog");
  const job = jobById.get(dlg.dataset.id);
  if (!job) return;
  if (dlg.returnValue === "yes") {
    const app = applyToJob(job);
    if (app) toast(`Applied #${app.n}: moved to Applied`);
  } else if (dlg.returnValue === "no") {
    delete state.opened[job.id];
    saveState();
  } else {
    return render();     // Esc: ask again next visit
  }
  render();
  askNext();
}

// ---------- Copy prompts ----------

function outreachPrompt(job) {
  const bg = state.prefs.background.trim() || "[Add your background in Job Radar settings]";
  return [
    `Write three LinkedIn connection notes for the ${job.title} role at ${job.company.trim()}.`,
    `Each note must be under 300 characters. Write one for a recruiter, one for the likely hiring manager, and one for an analyst on the team.`,
    `Keep them specific to this role and company, friendly, and not pushy.`,
    ``,
    `Job: ${job.title}`,
    `Company: ${job.company.trim()}`,
    `Location: ${job.location}`,
    `Link: ${job.apply_url}`,
    ``,
    `My background:`,
    bg,
  ].join("\n");
}

function tailoringPrompt(job) {
  return [
    `Tailor my resume for this job.`,
    ``,
    `Title: ${job.title}`,
    `Company: ${job.company.trim()}`,
    `Location: ${job.location} (${job.location_group}, ${modeLabel(job.work_mode).toLowerCase()})`,
    `Salary: ${salaryFull(job) || "not listed"}`,
    `Match: ${job.match_pct || 0}% (matched skills: ${(job.matched_skills || []).join(", ") || "none"})`,
    `Link: ${job.apply_url}`,
    ``,
    `Job description:`,
    job.description || "(no description in the feed)",
  ].join("\n");
}

// ---------- Actions ----------

function moveCurrentAfter(id) {
  const i = visibleIds.indexOf(id);
  currentId = visibleIds[i + 1] || visibleIds[i - 1] || null;
}

function markOpened(job) {
  if (appliedJobIds().has(job.id)) return;
  state.opened[job.id] = new Date().toISOString();
  state.openedLog[job.id] ??= state.opened[job.id];
  openedThisSession = true;
  saveState();
  // Re-render after the click so the "Opened" tag shows when you come back
  setTimeout(render, 0);
}

async function doAction(act, row) {
  const id = row.dataset.id;
  const job = jobById.get(id);
  currentId = id;

  if (act === "open" && job) return markOpened(job);
  if (act === "applied" && job) {
    moveCurrentAfter(id);
    const app = applyToJob(job);
    if (app) toast(`Applied #${app.n}: moved to Applied`);
  } else if (act === "remove" && job) {
    moveCurrentAfter(id);
    state.removed[id] = new Date().toISOString();
    delete state.opened[id];
    saveState();
    toast("Moved to Removed");
  } else if (act === "shortlist" && job) {
    state.shortlist[id] = { added: new Date().toISOString(), sent: null };
    saveState();
    toast("Added to Shortlist");
  } else if (act === "unshortlist") {
    if (state.prefs.tab === "shortlist") moveCurrentAfter(id);
    delete state.shortlist[id];
    saveState();
    toast("Removed from Shortlist");
  } else if (act === "restore") {
    delete state.removed[id];
    saveState();
    toast("Restored to New");
  } else if (act === "undo") {
    const n = Number(row.dataset.app);
    const app = state.apps.find((a) => a.n === n);
    if (!app) return;
    if (app.manual && !confirm(`Delete application #${n} (${app.title} at ${app.company})?`)) return;
    state.apps = state.apps.filter((a) => a.n !== n);
    saveState();
    toast(app.manual ? `Deleted #${n}` : "Moved back to New");
  } else if (act === "outreach" && job) {
    const ok = await copyText(outreachPrompt(job));
    toast(!ok ? "Couldn't copy" : state.prefs.background.trim() ? "Outreach prompt copied" : "Copied. Add your background in Settings to fill it in.");
    return;
  } else if (act === "tailor" && job) {
    toast((await copyText(tailoringPrompt(job))) ? "Tailoring prompt copied" : "Couldn't copy");
    return;
  }
  render();
}

// ---------- Theme ----------

function urlTheme() {
  const t = new URLSearchParams(location.search).get("theme");
  return t === "light" || t === "dark" ? t : null;
}

function applyTheme() {
  const t = urlTheme() || state.prefs.theme;   // ?theme= is a non-saved override (screenshots)
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const label = `Theme: ${t}`;
  $("#theme-btn").textContent = label;
  $("#theme-btn").setAttribute("aria-label", label);
}

// ---------- Controls ----------

function applyPrefsToControls() {
  const p = state.prefs;
  $("#sort").value = p.sort;
  $("#f-spons").value = p.spons;
  $("#f-salary").value = String(p.salary);
  $("#f-match").value = String(p.match);
  $("#show-no").checked = p.showNo;
  $("#show-perm").checked = p.showPerm;
  $("#show-staffing").checked = p.showStaffing;
  $("#show-stretch").checked = p.showStretch;
  $("#show-older").checked = p.showOlder;
  $("#fresh-only").setAttribute("aria-pressed", String(p.freshOnly));
  $("#big-co").setAttribute("aria-pressed", String(p.bigCo));
  $("#cap-exempt").setAttribute("aria-pressed", String(p.capExempt));
  $("#background").value = p.background;
  for (const b of $$("[data-loc]")) b.setAttribute("aria-pressed", String(b.dataset.loc === p.loc));
  for (const b of $$("[data-mode]")) b.setAttribute("aria-pressed", String(p.modes.includes(b.dataset.mode)));
}

function setPref(key, value) {
  state.prefs[key] = value;
  saveState();
  render();
}

function wireControls() {
  for (const b of $$('[role="tab"]')) b.addEventListener("click", () => { currentId = null; setPref("tab", b.dataset.tab); });

  for (const b of $$("[data-loc]")) b.addEventListener("click", () => {
    state.prefs.loc = b.dataset.loc;
    applyPrefsToControls();
    setPref("loc", b.dataset.loc);
  });
  for (const b of $$("[data-mode]")) b.addEventListener("click", () => {
    const m = b.dataset.mode;
    const modes = state.prefs.modes.includes(m) ? state.prefs.modes.filter((x) => x !== m) : [...state.prefs.modes, m];
    state.prefs.modes = modes;
    applyPrefsToControls();
    setPref("modes", modes);
  });

  $("#sort").addEventListener("change", (e) => setPref("sort", e.target.value));
  $("#f-spons").addEventListener("change", (e) => setPref("spons", e.target.value));
  $("#f-salary").addEventListener("change", (e) => setPref("salary", Number(e.target.value)));
  $("#f-match").addEventListener("change", (e) => setPref("match", Number(e.target.value)));
  $("#show-no").addEventListener("change", (e) => setPref("showNo", e.target.checked));
  $("#show-perm").addEventListener("change", (e) => setPref("showPerm", e.target.checked));
  $("#show-staffing").addEventListener("change", (e) => setPref("showStaffing", e.target.checked));
  $("#show-stretch").addEventListener("change", (e) => setPref("showStretch", e.target.checked));
  $("#show-older").addEventListener("change", (e) => setPref("showOlder", e.target.checked));
  for (const [id, key] of [["#fresh-only", "freshOnly"], ["#big-co", "bigCo"], ["#cap-exempt", "capExempt"]]) {
    $(id).addEventListener("click", () => {
      state.prefs[key] = !state.prefs[key];
      applyPrefsToControls();
      setPref(key, state.prefs[key]);
    });
  }
  for (const id of ["#search", "#f-title", "#f-company"]) $(id).addEventListener("input", render);

  $("#list").addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    const row = e.target.closest(".job");
    if (el && row) doAction(el.dataset.act, row);
    else if (row) { currentId = row.dataset.id; markCurrent(); }
  });

  $("#theme-btn").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    state.prefs.theme = order[(order.indexOf(state.prefs.theme) + 1) % order.length];
    saveState();
    applyTheme();
  });

  $("#export-btn").addEventListener("click", exportBackup);
  $("#export-shortlist").addEventListener("click", exportShortlist);
  // One "Find people" menu open at a time; a click elsewhere closes it
  document.addEventListener("click", (e) => {
    for (const d of $$(".people-menu[open]")) if (!d.contains(e.target)) d.open = false;
  });
  $("#export-banner").addEventListener("click", (e) => { if (e.target.closest("[data-export]")) exportBackup(); });
  $("#import-btn").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (f) importBackup(f);
  });

  $("#settings-btn").addEventListener("click", () => $("#settings-dialog").showModal());
  $("#settings-dialog").addEventListener("close", () => {
    state.prefs.background = $("#background").value;
    saveState();
  });

  $("#ask-dialog").addEventListener("close", onAskClose);

  $("#log-btn").addEventListener("click", () => {
    const form = $("#log-form");
    form.reset();
    form.elements.date.value = todayLocal();
    $("#log-error").hidden = true;
    $("#log-dialog").showModal();
  });
  $("#log-form").addEventListener("submit", (e) => {
    if (e.submitter?.value !== "save") return;
    const f = e.target.elements;
    const url = f.url.value.trim();
    let err = "";
    if (!f.company.value.trim() || !f.title.value.trim()) err = "Enter a company and a title.";
    else if (url && !safeUrl(url)) err = "Links need to start with http:// or https://.";
    else if (!f.date.value) err = "Pick the date you applied.";
    if (err) {
      e.preventDefault();
      $("#log-error").textContent = err;
      $("#log-error").hidden = false;
      return;
    }
    const app = addApplication({ company: f.company.value, title: f.title.value, url, date: f.date.value, manual: true });
    toast(`Logged application #${app.n}`);
    render();
  });
  $("#log-form").addEventListener("input", () => { $("#log-error").hidden = true; });

  // Coming back to the tab after clicking Open counts as your next visit
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && openedThisSession) {
      openedThisSession = false;
      askNext();
    }
  });

  document.addEventListener("keydown", onKey);
}

// ---------- Keyboard ----------

function markCurrent() {
  for (const el of $$(".job.current")) el.classList.remove("current");
  if (!currentId) return;
  const row = document.querySelector(`.job[data-id="${CSS.escape(currentId)}"]`);
  if (row) {
    row.classList.add("current");
    row.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }
}

function onKey(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  const typing = e.target.closest?.("input, textarea, select");
  if (typing) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "/") {
    e.preventDefault();
    $("#search").focus();
    return;
  }
  if (e.key === "j" || e.key === "k") {
    if (!visibleIds.length) return;
    const i = visibleIds.indexOf(currentId);
    const next = e.key === "j" ? Math.min(i + 1, visibleIds.length - 1) : Math.max(i - 1, 0);
    currentId = visibleIds[i === -1 ? 0 : next];
    markCurrent();
    return;
  }
  const row = currentId && document.querySelector(`.job[data-id="${CSS.escape(currentId)}"]`);
  if (!row) return;
  if (e.key === "o") {
    const link = row.querySelector("a[data-act='open'], .actions a");
    if (link) link.click();
  } else if (e.key === "a" && state.prefs.tab === "new") {
    doAction("applied", row);
  } else if (e.key === "x" && state.prefs.tab === "new") {
    doAction("remove", row);
  }
}

// ---------- Start ----------

async function start() {
  // Phones start with the sort/filter fields folded away
  if (matchMedia("(max-width: 640px)").matches) $("#more-filters").open = false;
  applyTheme();
  applyPrefsToControls();
  wireControls();
  try {
    const res = await fetch("../data/jobs.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    jobs = data.jobs || [];
    meta = data.meta || {};
  } catch (e) {
    $("#dateline").textContent = "Couldn't load data/jobs.json. Serve the repo root over HTTP (see CLAUDE.md).";
    $("#list").innerHTML = emptyState("No job data", "The dashboard needs data/jobs.json from the fetcher.");
    return;
  }
  jobById = new Map(jobs.map((j) => [j.id, j]));
  $("#company-list").innerHTML = [...new Set(jobs.map((j) => j.company.trim()))].sort()
    .map((c) => `<option value="${esc(c)}">`).join("");
  renderDateline();
  render();
  askNext();
}

start();
