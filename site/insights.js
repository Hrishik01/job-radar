// Job Radar insights. Plain JS and hand-built SVG, no chart library.
// Reads ../data/jobs.json and the dashboard's status in localStorage.
//
// Jobs found on a board's first fetch are "backfill" (set by fetch.py):
// their first-seen time is when tracking started, not when they were
// posted, so every time-based chart leaves them out. Each chart also has a
// minimum amount of history and shows a "not enough history yet" panel
// until it has it, instead of drawing a misleading spike.
"use strict";

const STORE_KEY = "jobradar.v1";
const DAY = 86400000;

// Minimum history before a chart is drawn
const MIN = {
  sparkDays: 7,          // KPI sparklines
  heatPostings: 50,      // heatmap: new postings seen...
  heatDays: 14,          // ...over at least two weeks
  trendWeeks: 2,         // full Mon–Sun weeks after the first sweep
  ttaApps: 5,            // timed applications from the feed...
  ttaWeeks: 2,           // ...spread over at least two weeks
};

const GROUPS = ["Boston area", "Remote (US)", "Hybrid (US)", "Onsite (US)"];
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let jobs = [], meta = {}, jobById = new Map(), state = { apps: [], openedLog: {}, prefs: {} };

const $ = (s) => document.querySelector(s);

// ---------- Helpers ----------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function daysSince(t) {
  return Math.max(0, Math.floor((Date.now() - t) / DAY));
}

function fmtDay(t, opts = { month: "short", day: "numeric" }) {
  return new Date(t).toLocaleDateString("en-US", { ...opts, timeZone: "UTC" });
}

// "2026-10-12" -> local Date at noon (used when an application has no exact time)
function localNoon(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d, 12).getTime();
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

// Day of week (0 = Mon), hour and calendar date of a timestamp in Eastern time
const ET_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", hourCycle: "h23", weekday: "short",
});
function etParts(t) {
  const p = Object.fromEntries(ET_FMT.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return { dow: DOW.indexOf(p.weekday), hour: Number(p.hour) % 24, date: Date.UTC(+p.year, +p.month - 1, +p.day) };
}

// Monday of the ET week containing t, as a UTC-midnight timestamp (a date key)
function weekStart(t) {
  const p = etParts(t);
  return p.date - p.dow * DAY;
}

function hoursLabel(h) {
  return h < 48 ? `${Math.round(h)} h` : `${(h / 24).toFixed(1)} d`;
}

// ---------- Shared chart pieces ----------

function svgOpen(w, h, label) {
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}">`;
}

// The "not enough history yet" panel: what's missing, progress, and when
function waitPanel({ body, progress = [], ready }) {
  const rows = progress.map(({ label, have, need }) => `
    <div class="wait-row">
      <span class="mono">${Math.min(have, need)} of ${need}</span>
      <span class="wait-bar" role="img" aria-label="${have} of ${need} ${esc(label)}"><i style="width:${Math.min(100, (have / need) * 100)}%"></i></span>
      <span>${esc(label)}</span>
    </div>`).join("");
  return `<div class="wait">
    <p class="wait-title">Not enough history yet</p>
    <p>${body}</p>
    ${rows}
    ${ready ? `<p class="wait-ready mono">${esc(ready)}</p>` : ""}
  </div>`;
}

// Horizontal bars with an optional "H-1B likely" portion in the accent color
function barList(rows, { showLikely = true } = {}) {
  const max = Math.max(...rows.map((r) => r.count), 1);
  return `<div class="bars">${rows.map((r) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(r.label)}">${esc(r.label)}</span>
      <span class="bar-track" role="img" aria-label="${esc(r.label)}: ${r.count}${showLikely ? `, ${r.likely} H-1B likely` : ""}">
        <i style="width:${(r.count / max) * 100}%">${showLikely && r.count ? `<b style="width:${(r.likely / r.count) * 100}%"></b>` : ""}</i>
      </span>
      <span class="bar-num mono">${r.count}${r.note ? ` <span class="muted">${esc(r.note)}</span>` : ""}</span>
    </div>`).join("")}</div>
    ${showLikely ? `<p class="legend"><span class="key likely"></span>H-1B likely <span class="key"></span>Other</p>` : ""}`;
}

// ---------- Data views ----------

function sweepTime() {
  return Date.parse(meta.first_sweep_utc || "") || Math.min(...jobs.map((j) => Date.parse(j.first_seen_utc)));
}

function freshJobs() {
  return jobs.filter((j) => !j.backfill);
}

// Applications from the feed, with hours from first seen to applied
function timedApps() {
  return state.apps.filter((a) => !a.manual && a.job_id && jobById.has(a.job_id)).map((a) => {
    const job = jobById.get(a.job_id);
    const applied = a.applied_utc ? Date.parse(a.applied_utc) : localNoon(a.date);
    return {
      applied,
      exact: !!a.applied_utc,
      hours: Math.max(0, (applied - Date.parse(job.first_seen_utc)) / 3600000),
    };
  });
}

// ---------- KPIs ----------

function dailyCounts(times, days = 14) {
  const out = new Array(days).fill(0);
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - (days - 1));
  for (const t of times) {
    const i = Math.floor((t - start.getTime()) / DAY);
    if (i >= 0 && i < days) out[i]++;
  }
  return out;
}

function spark(id, values) {
  const el = document.getElementById(id);
  const days = daysSince(sweepTime());
  if (days < MIN.sparkDays) {
    el.className = "spark-wait";
    el.title = `Sparklines need ${MIN.sparkDays} days of tracking`;
    el.textContent = `trend in ${MIN.sparkDays - days}d`;
    return;
  }
  const w = 64, h = 22, max = Math.max(...values, 1);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 1 - (v / max) * (h - 2)}`).join(" ");
  el.className = "";
  el.innerHTML = `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" points="${pts}"/></svg>`;
}

function renderKpis() {
  const weekAgo = Date.now() - 7 * DAY;
  const fresh = freshJobs();
  const seen = fresh.map((j) => Date.parse(j.first_seen_utc));
  $("#kpi-new").textContent = seen.filter((t) => t >= weekAgo).length;
  spark("spark-new", dailyCounts(seen));

  const appTimes = state.apps.map((a) => localNoon(a.date));
  $("#kpi-applied-week").textContent = appTimes.filter((t) => t >= weekAgo).length;
  spark("spark-applied", dailyCounts(appTimes));
  $("#kpi-applied-all").textContent = state.apps.length;
  // Running total: applications up to each of the last 14 days
  const daily = dailyCounts(appTimes);
  const before = appTimes.length - daily.reduce((a, b) => a + b, 0);
  let run = before;
  spark("spark-applied-all", daily.map((n) => (run += n)));

  const active = jobs.filter((j) => j.active);
  const likely = active.filter((j) => j.sponsorship === "likely").length;
  $("#kpi-spons").textContent = active.length ? Math.round((likely / active.length) * 100) + "%" : "–";
  const all = dailyCounts(seen), lik = dailyCounts(fresh.filter((j) => j.sponsorship === "likely").map((j) => Date.parse(j.first_seen_utc)));
  let last = 0;
  spark("spark-spons", all.map((n, i) => (last = n ? lik[i] / n : last)));
}

// ---------- Heatmap ----------

function renderHeatmap(el) {
  const fresh = freshJobs();
  const sweep = sweepTime();
  const days = daysSince(sweep);
  if (fresh.length < MIN.heatPostings || days < MIN.heatDays) {
    el.innerHTML = waitPanel({
      body: `The heatmap needs new postings spread over at least two weeks. With fewer, a single fetch run would light up one cell and look like a pattern. The ${jobs.length - fresh.length} jobs from the first sweep don't count, because their first-seen time is when tracking started.`,
      progress: [
        { label: "new postings", have: fresh.length, need: MIN.heatPostings },
        { label: "days tracked", have: days, need: MIN.heatDays },
      ],
      ready: `Earliest: ${fmtDay(sweep + MIN.heatDays * DAY, { weekday: "short", month: "short", day: "numeric" })}, if ${MIN.heatPostings} new postings have come in by then`,
    });
    return;
  }

  const grid = DOW.map(() => new Array(24).fill(0));
  for (const j of fresh) {
    const p = etParts(Date.parse(j.first_seen_utc));
    grid[p.dow][p.hour]++;
  }
  const max = Math.max(...grid.flat(), 1);
  const W = Math.max(el.clientWidth, 300);
  const left = 34, top = 18, gap = 2;
  const cw = (W - left) / 24, ch = Math.min(Math.max(cw, 14), 24);
  const H = top + 7 * ch + 4;
  const hourName = (h) => (h % 12 || 12) + (h < 12 ? "a" : "p");
  let s = svgOpen(W, H, "Heatmap of new postings by weekday and hour, Eastern time");
  for (let h = 0; h < 24; h += 3) s += `<text class="axis" x="${left + h * cw + cw / 2}" y="12" text-anchor="middle">${hourName(h)}</text>`;
  grid.forEach((row, d) => {
    const y = top + d * ch;
    s += `<text class="axis" x="0" y="${y + ch / 2 + 4}">${DOW[d]}</text>`;
    row.forEach((n, h) => {
      const x = left + h * cw;
      s += n
        ? `<rect class="heat" x="${x}" y="${y}" width="${cw - gap}" height="${ch - gap}" fill-opacity="${0.15 + 0.85 * (n / max)}"><title>${DOW[d]} ${hourName(h)}: ${n}</title></rect>`
        : `<rect class="heat-empty" x="${x}" y="${y}" width="${cw - gap}" height="${ch - gap}"><title>${DOW[d]} ${hourName(h)}: 0</title></rect>`;
    });
  });
  el.innerHTML = s + `</svg><p class="legend">Darker = more postings first seen in that hour. Busiest cell: ${max}.</p>`;
}

// ---------- Weekly trend ----------

function renderTrend(el) {
  const sweep = sweepTime();
  const firstFull = weekStart(sweep) + 7 * DAY;            // first Monday after the sweep
  const thisWeek = weekStart(Date.now());
  const fullWeeks = Math.max(0, Math.round((thisWeek - firstFull) / (7 * DAY)));
  if (fullWeeks < MIN.trendWeeks) {
    el.innerHTML = waitPanel({
      body: "Weekly trends need two full weeks of tracking. The week of the first sweep doesn't count: it's partial and mostly backfill.",
      progress: [{ label: "full weeks", have: fullWeeks, need: MIN.trendWeeks }],
      ready: `Ready: ${fmtDay(firstFull + MIN.trendWeeks * 7 * DAY, { weekday: "short", month: "short", day: "numeric" })}`,
    });
    return;
  }

  // Full weeks plus the current partial week, last 12 at most
  const weeks = [];
  for (let w = firstFull; w <= thisWeek; w += 7 * DAY) weeks.push(w);
  const shown = weeks.slice(-12);
  const posts = shown.map((w) => freshJobs().filter((j) => weekStart(Date.parse(j.first_seen_utc)) === w).length);
  const apps = shown.map((w) => state.apps.filter((a) => weekStart(localNoon(a.date)) === w).length);

  const W = Math.max(el.clientWidth, 280), H = 190;
  const left = 30, bottom = 22, top = 10, plotH = H - top - bottom;
  const max = Math.max(...posts, ...apps, 1);
  const bw = (W - left) / shown.length;
  const y = (v) => top + plotH - (v / max) * plotH;
  let s = svgOpen(W, H, "Weekly new postings and applications");
  s += `<text class="axis" x="0" y="${top + 8}">${max}</text><text class="axis" x="0" y="${top + plotH}">0</text>`;
  s += `<line class="gridline" x1="${left}" x2="${W}" y1="${top + plotH}" y2="${top + plotH}"/>`;
  shown.forEach((w, i) => {
    const x = left + i * bw;
    const partial = w === thisWeek;
    s += `<rect class="bar${partial ? " partial" : ""}" x="${x + 3}" y="${y(posts[i])}" width="${bw - 6}" height="${top + plotH - y(posts[i])}"><title>Week of ${fmtDay(w)}: ${posts[i]} new${partial ? " (so far)" : ""}</title></rect>`;
    if (i % Math.ceil(shown.length / 6) === 0 || partial) s += `<text class="axis" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${partial ? "now" : fmtDay(w)}</text>`;
  });
  const pts = apps.map((v, i) => `${left + i * bw + bw / 2},${y(v)}`).join(" ");
  s += `<polyline class="line" points="${pts}"/>`;
  apps.forEach((v, i) => { s += `<circle class="dot" cx="${left + i * bw + bw / 2}" cy="${y(v)}" r="3"><title>${v} applications</title></circle>`; });
  el.innerHTML = s + `</svg><p class="legend"><span class="key bar"></span>New postings <span class="key line"></span>Your applications <span class="muted">· lighter bar = this week so far</span></p>`;
}

// ---------- Time to apply ----------

function renderTta(el) {
  const timed = timedApps();
  const weeks = [...new Set(timed.map((a) => weekStart(a.applied)))].sort((a, b) => a - b);
  if (timed.length < MIN.ttaApps || weeks.length < MIN.ttaWeeks) {
    el.innerHTML = waitPanel({
      body: "Time to apply needs a handful of applications to jobs from the feed, spread over two weeks. Manually logged applications don't count: there's no first-seen time to measure from.",
      progress: [
        { label: "applications from the feed", have: timed.length, need: MIN.ttaApps },
        { label: "weeks with one", have: weeks.length, need: MIN.ttaWeeks },
      ],
    });
    return;
  }

  const meds = weeks.map((w) => median(timed.filter((a) => weekStart(a.applied) === w).map((a) => a.hours)));
  const overall = median(timed.map((a) => a.hours));
  const approx = timed.filter((a) => !a.exact).length;
  const shown = weeks.slice(-12), vals = meds.slice(-12);

  const W = Math.max(el.clientWidth, 280), H = 170;
  const left = 40, bottom = 22, top = 10, plotH = H - top - bottom;
  const max = Math.max(...vals, 1);
  const step = shown.length > 1 ? (W - left - 10) / (shown.length - 1) : 0;
  const x = (i) => left + 5 + i * step;
  const y = (v) => top + plotH - (v / max) * plotH;
  let s = svgOpen(W, H, "Weekly median hours from first seen to application");
  s += `<text class="axis" x="0" y="${top + 8}">${hoursLabel(max)}</text><text class="axis" x="0" y="${top + plotH}">0</text>`;
  s += `<line class="gridline" x1="${left}" x2="${W}" y1="${top + plotH}" y2="${top + plotH}"/>`;
  s += `<polyline class="line" points="${vals.map((v, i) => `${x(i)},${y(v)}`).join(" ")}"/>`;
  vals.forEach((v, i) => {
    s += `<circle class="dot" cx="${x(i)}" cy="${y(v)}" r="3"><title>Week of ${fmtDay(shown[i])}: median ${hoursLabel(v)}</title></circle>`;
    if (i % Math.ceil(shown.length / 6) === 0) s += `<text class="axis" x="${x(i)}" y="${H - 6}" text-anchor="middle">${fmtDay(shown[i])}</text>`;
  });
  el.innerHTML = `<p class="big-stat"><span class="mono signal">${hoursLabel(overall)}</span> median across ${timed.length} applications</p>` +
    s + `</svg>` + (approx ? `<p class="legend">${approx} older application${approx === 1 ? "" : "s"} only had a date, so ${approx === 1 ? "it's" : "they're"} counted from noon that day.</p>` : "");
}

// ---------- Funnel ----------

function renderFunnel(el) {
  const runs = meta.runs || [];
  const lastRun = runs[runs.length - 1] || {};
  const fromFeed = state.apps.filter((a) => !a.manual && a.job_id);
  const opened = new Set([...Object.keys(state.openedLog || {}), ...fromFeed.map((a) => a.job_id)]);
  const manual = state.apps.length - fromFeed.length;

  const stages = [
    { label: "Found", count: lastRun.listed || 0, note: "listings on watched boards, latest run" },
    { label: "Kept", count: jobs.length, note: "passed your filters, all-time" },
    { label: "Opened", count: opened.size, note: "jobs you opened from the dashboard" },
    { label: "Applied", count: fromFeed.length, note: manual ? `from the feed, plus ${manual} logged manually` : "from the feed" },
  ];
  // Each bar is the share of the stage before it, so small stages stay visible
  el.innerHTML = `<div class="funnel">${stages.map((st, i) => {
    const prev = i ? stages[i - 1].count : st.count;
    const pct = prev ? (st.count / prev) * 100 : 0;
    return `<div class="funnel-row">
      <span class="funnel-label">${st.label}</span>
      <span class="funnel-num mono${i === 3 ? " signal" : ""}">${st.count.toLocaleString("en-US")}</span>
      <span class="funnel-track"><i style="width:${i ? Math.max(pct, st.count ? 1 : 0) : 100}%"></i></span>
      <span class="funnel-pct mono">${i ? `${pct < 1 && pct > 0 ? pct.toFixed(2) : Math.round(pct)}%` : ""}</span>
      <span class="funnel-note">${esc(st.note)}</span>
    </div>`;
  }).join("")}</div>
  <p class="legend">Bars show each stage as a share of the one above it.</p>
  ${opened.size === 0 && fromFeed.length === 0
    ? `<p class="wait-inline">Opened and Applied fill in as you use the dashboard. Opens are recorded from Nov 2, 2026; earlier ones weren't saved.</p>`
    : ""}`;
}

// ---------- Snapshots: location and companies ----------

// Same rule as the dashboard: posted over 7 days ago. No posted date means
// first-seen, except backfill jobs, whose age is unknown and counts as older.
function isOlder(job) {
  const t = Date.parse(job.posted_raw || "") || (job.backfill ? 0 : Date.parse(job.first_seen_utc) || 0);
  return Date.now() - t > 7 * 86400000;
}

// Listed jobs, hiding won't-sponsor, PERM and older jobs unless the
// dashboard's toggles show them, so these charts count what the dashboard shows
function snapshotJobs() {
  const p = state.prefs || {};
  const active = jobs.filter((j) => j.active);
  const shown = active.filter((j) => (p.showNo || j.sponsorship !== "no") &&
    (p.showPerm || !j.perm_likely) && (p.showOlder || !isOlder(j)));
  const hidden = [];
  const no = active.filter((j) => j.sponsorship === "no").length;
  const perm = active.filter((j) => j.perm_likely).length;
  const older = active.filter(isOlder).length;
  if (!p.showNo && no) hidden.push(`${no} won't-sponsor`);
  if (!p.showPerm && perm) hidden.push(`${perm} PERM`);
  if (!p.showOlder && older) hidden.push(`${older} older (posted 7+ days ago)`);
  const note = hidden.length
    ? `Hides ${hidden.slice(0, -1).join(", ")}${hidden.length > 1 ? " and " : ""}${hidden.at(-1)} jobs, like the dashboard. Change this with the dashboard's toggles.`
    : "Includes won't-sponsor, PERM and older jobs, because the dashboard's toggles show them.";
  return { list: shown, note: `<p class="legend">${esc(note)}</p>` };
}

function renderLocation(el) {
  const { list: active, note } = snapshotJobs();
  const rows = GROUPS.map((g) => {
    const list = active.filter((j) => j.location_group === g);
    return {
      label: g, count: list.length, likely: list.filter((j) => j.sponsorship === "likely").length,
      note: active.length ? `${Math.round((list.length / active.length) * 100)}%` : "",
    };
  });
  el.innerHTML = barList(rows) + note;
}

function renderTop(el) {
  const { list: active, note } = snapshotJobs();
  const by = new Map();
  for (const j of active) {
    const name = j.company.trim();
    const r = by.get(name) || { label: name, count: 0, likely: 0 };
    r.count++;
    if (j.sponsorship === "likely") r.likely++;
    by.set(name, r);
  }
  const rows = [...by.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label)).slice(0, 10);
  el.innerHTML = (rows.length ? barList(rows) : waitPanel({ body: "No jobs are listed right now." })) + note;
}

// ---------- Page ----------

function renderHeader() {
  const sweep = sweepTime();
  const fresh = freshJobs().length;
  const backfill = jobs.length - fresh;
  const days = daysSince(sweep);
  $("#dateline").textContent = meta.generated_utc
    ? `Data updated ${new Date(meta.generated_utc).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} ET`
    : "";
  $("#history-note").innerHTML =
    `Tracking since <b>${esc(fmtDay(sweep, { month: "short", day: "numeric", year: "numeric" }))}</b> (${days} day${days === 1 ? "" : "s"}). ` +
    `The first sweep found ${backfill} jobs; their first-seen time is when tracking began, so they're left out of time charts. ` +
    `<b class="mono">${fresh}</b> new posting${fresh === 1 ? "" : "s"} since.`;
}

function renderCharts() {
  renderHeatmap($("#c-heat"));
  renderTrend($("#c-trend"));
  renderTta($("#c-tta"));
  renderFunnel($("#c-funnel"));
  renderLocation($("#c-loc"));
  renderTop($("#c-top"));
}

function applyTheme() {
  const urlT = new URLSearchParams(location.search).get("theme");
  const t = urlT === "light" || urlT === "dark" ? urlT : state.prefs?.theme || "system";
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  $("#theme-btn").textContent = `Theme: ${t}`;
}

function wire() {
  $("#theme-btn").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    state.prefs = state.prefs || {};
    state.prefs.theme = order[(order.indexOf(state.prefs.theme || "system") + 1) % order.length];
    try {
      // Read-modify-write so the dashboard's other saved status is untouched
      const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
      saved.prefs = { ...(saved.prefs || {}), theme: state.prefs.theme };
      localStorage.setItem(STORE_KEY, JSON.stringify(saved));
    } catch (e) { /* storage blocked: theme just won't persist */ }
    applyTheme();
  });
  // Charts are drawn at the container's pixel width, so redraw when it changes
  let lastW = 0, timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const w = $("#c-heat").clientWidth;
      if (w !== lastW) { lastW = w; renderCharts(); }
    }, 150);
  });
}

async function start() {
  try { state = { ...state, ...JSON.parse(localStorage.getItem(STORE_KEY) || "{}") }; }
  catch (e) { /* no saved status: charts use feed data only */ }
  state.apps = state.apps || [];
  applyTheme();
  wire();
  try {
    const res = await fetch("../data/jobs.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    jobs = data.jobs || [];
    meta = data.meta || {};
  } catch (e) {
    $("#dateline").textContent = "Couldn't load data/jobs.json. Serve the repo root over HTTP (see CLAUDE.md).";
    return;
  }
  jobById = new Map(jobs.map((j) => [j.id, j]));
  renderHeader();
  renderKpis();
  renderCharts();
}

start();
