// Best fit ranking. Loaded by index.html before app.js, and run under Node by
// tests/test_smoke.py (BestFitTests), so the order is tested, not just eyeballed.

// Cap-exempt and H-1B likely only boost a job whose skills match is at least
// this; every job below it sorts after all jobs at or above it.
const FIT_MIN_MATCH = 30;

// Cap-exempt tiers from the fetcher: "confirmed" (R1/AAU universities, their
// medical centers, the Boston list) or "possible" (USCIS education employers
// with a university-like name). Older data used true for confirmed.
const capTier = (job) => (job.cap_exempt === true ? "confirmed" : job.cap_exempt || null);
const capRank = (job) => ({ confirmed: 2, possible: 1 }[capTier(job)] || 0);

// 30%+ matches first. Within each group: Target band first; then, for 30%+
// jobs only, confirmed cap-exempt, possible cap-exempt, H-1B likely; then
// match %, then the `newest` tie-break app.js passes in.
function fitCompare(a, b, newest) {
  const strong = (j) => (j.match_pct || 0) >= FIT_MIN_MATCH;
  return strong(b) - strong(a) ||
    (b.band === "target") - (a.band === "target") ||
    (strong(a) && (capRank(b) - capRank(a) ||
      (b.sponsorship === "likely") - (a.sponsorship === "likely"))) ||
    (b.match_pct || 0) - (a.match_pct || 0) ||
    newest(a, b);
}

if (typeof module !== "undefined") module.exports = { FIT_MIN_MATCH, capTier, capRank, fitCompare };
