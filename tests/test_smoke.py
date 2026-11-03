"""Smoke tests for the Phase 1 fetcher. Run from the repo root:

    python -m unittest -v
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fetcher"))

import fetch  # noqa: E402
import filters  # noqa: E402
from boards import workday  # noqa: E402
from company_lists import ListLookup  # noqa: E402
import scoring  # noqa: E402
from models import Job, stable_id  # noqa: E402
from parse import clean_url, parse_salary, salary_from_hint  # noqa: E402
from uscis import Lookup, normalize_company  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SETTINGS = json.loads((ROOT / "data" / "settings.json").read_text("utf-8"))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


def make_job(description="", location="", **kw) -> Job:
    return Job(company="Acme", board="greenhouse", slug="acme", native_id="123",
               title="Data Analyst", description=description, location=location, **kw)


class StableIdTests(unittest.TestCase):
    def test_id_ignores_apply_url_tracking_params(self):
        a = make_job(apply_url="https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x")
        b = make_job(apply_url="https://boards.greenhouse.io/acme/jobs/123?gh_src=zzz&trackingId=9")
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.id, stable_id("greenhouse", "acme", "123"))

    def test_different_job_gets_different_id(self):
        self.assertNotEqual(stable_id("greenhouse", "acme", "123"), stable_id("greenhouse", "acme", "124"))

    def test_clean_url_strips_tracking_but_keeps_job_param(self):
        url = "https://acme.com/careers/job?gh_jid=555&utm_campaign=x&gh_src=y&trackingId=1#apply"
        self.assertEqual(clean_url(url), "https://acme.com/careers/job?gh_jid=555")


class SalaryTests(unittest.TestCase):
    def test_hourly_to_annual(self):
        s = parse_salary("Pay: $45.00 - $55.00 per hour, plus benefits.")
        self.assertEqual((s["min"], s["max"], s["hourly"]), (93600, 114400, True))

    def test_single_hourly_figure(self):
        self.assertEqual(parse_salary("This role pays $30/hr.")["min"], 62400)

    def test_annual_k_range(self):
        s = parse_salary("Base salary $85K–$110K annually")
        self.assertEqual((s["min"], s["max"], s["hourly"]), (85000, 110000, False))

    def test_revenue_is_not_salary(self):
        self.assertIsNone(parse_salary("We grew revenue from $10,000,000 to $50,000,000."))

    def test_structured_hourly_hint(self):
        s = salary_from_hint({"min": 40, "max": 50, "currency": "USD", "interval": "1 HOUR"})
        self.assertEqual((s["min"], s["max"]), (83200, 104000))


class SkillMatchTests(unittest.TestCase):
    matchers = scoring.build_matchers(SETTINGS["skills"])

    def matched(self, text):
        return scoring.score(text, self.matchers, SETTINGS["match_cap"])[1]

    def test_sap_not_in_asap(self):
        self.assertNotIn("SAP", self.matched("Please reply ASAP."))
        self.assertIn("SAP", self.matched("Experience with SAP ERP."))

    def test_git_not_in_digital(self):
        self.assertNotIn("Git", self.matched("A digital transformation role."))
        self.assertIn("Git", self.matched("Version control with Git."))

    def test_sql_not_in_nosql(self):
        self.assertNotIn("SQL", self.matched("Familiarity with NoSQL stores."))

    def test_r_not_in_r_and_d(self):
        for text in ["Partner with our R&D team.", "Support R & D projects.", "R&D, finance and ops."]:
            self.assertNotIn("R", self.matched(text), text)

    def test_r_language_forms(self):
        for text in ["Strong R programming skills.", "Analysis in R or Python.",
                     "Use R/Python daily.", "Python/R preferred.", "Tools: R, SQL, Tableau.",
                     "Tools: SQL, R and Tableau."]:
            self.assertIn("R", self.matched(text), text)

    def test_excel_verb_is_not_excel(self):
        self.assertNotIn("Excel", self.matched("You will excel in a fast-paced team."))
        self.assertIn("Excel", self.matched("Advanced Excel and VBA."))

    def test_excluded_skills_never_score(self):
        matched = self.matched("dbt, DAX, Looker, LookML, A/B testing, causal inference, Celonis and SimPy.")
        self.assertEqual(matched, [])
        names = {s["name"].lower() for s in SETTINGS["skills"]}
        for banned in ["dbt", "dax", "looker", "lookml", "a/b testing", "causal inference", "celonis", "simpy"]:
            self.assertNotIn(banned, names)

    def test_new_skills_and_aliases(self):
        text = ("Run in-depth interviews and thematic coding, build market sizing and peer benchmarking, "
                "own data governance, write SOPs and automate Excel macros.")
        for skill in ["Qualitative research", "Market sizing", "Peer benchmarking", "Governance", "SOPs", "Excel"]:
            self.assertIn(skill, self.matched(text), skill)

    def test_requirements_section_weighs_more(self):
        text = fixture("normal_job.txt")
        pct, matched = scoring.score(text, self.matchers, SETTINGS["match_cap"])
        self.assertIn("Salesforce", matched)   # "About the role" -> 0.5
        self.assertIn("SQL", matched)          # "Qualifications" -> 1.0
        self.assertGreater(pct, 0)


class SponsorshipTests(unittest.TestCase):
    def test_eeo_boilerplate_is_not_tagged_no(self):
        is_no, _, log = filters.sponsorship_no(fixture("sponsor_eeo_boilerplate.txt"))
        self.assertFalse(is_no)
        self.assertTrue(log, "borderline sentences should still be logged")

    def test_tier1_phrase(self):
        is_no, evidence, _ = filters.sponsorship_no(fixture("sponsor_tier1.txt"))
        self.assertTrue(is_no)
        self.assertEqual(evidence, "unable to sponsor")

    def test_tier2_with_requirement_context(self):
        is_no, evidence, log = filters.sponsorship_no(fixture("sponsor_tier2.txt"))
        self.assertTrue(is_no)
        self.assertIn("citizen", evidence.lower())
        self.assertEqual(log[-1]["decision"], "tagged_no")

    def test_eligible_to_obtain_clearance(self):
        is_no, _, _ = filters.sponsorship_no(
            "- Eligible to obtain and maintain an active US Top Secret security clearance")
        self.assertTrue(is_no)


class PermTests(unittest.TestCase):
    def test_perm_notice_is_flagged(self):
        job = make_job(fixture("perm_notice.txt"), location="100 Wood Ave, Suite 300, Edison, NJ")
        perm, markers = filters.perm_markers(job)
        self.assertTrue(perm)
        self.assertIn("experience in the job offered", markers)
        self.assertIn("or foreign equivalent", markers)
        self.assertGreaterEqual(len(markers), 5, markers)

    def test_normal_job_is_not_flagged(self):
        perm, markers = filters.perm_markers(make_job(fixture("normal_job.txt"), location="Boston, MA"))
        self.assertFalse(perm, markers)

    def test_board_perm_flag_is_conclusive(self):
        self.assertTrue(filters.perm_markers(make_job("Plain text.", perm_flag=True))[0])


class FilterTests(unittest.TestCase):
    def test_titles(self):
        keep = ["Revenue Operations Analyst", "Data Analyst II", "Analytics Engineer", "Sales Operations Specialist",
                "Business Systems Analyst", "Wholesale Operations Analyst", "Product Analyst"]
        drop = ["Senior Data Analyst", "Sr. Business Analyst", "Analytics Manager",
                "Lead Data Scientist", "Software Engineer", "Data Science Intern", "Financial Analyst",
                "Data Analytics & Management, AVP", "Business Analyst - Real Assets, AVP"]
        for t in keep:
            self.assertTrue(filters.title_passes(t, SETTINGS), t)
        for t in drop:
            self.assertFalse(filters.title_passes(t, SETTINGS), t)

    def test_engineer_titles_only_analytics_or_bi(self):
        keep = ["Analytics Engineer", "BI Engineer", "Business Intelligence Engineer II"]
        drop = ["Systems Engineer, Insights and Analytics", "Data Engineer, Analytics",
                "Software Engineers - Business Intelligence Platform"]
        for t in keep:
            self.assertTrue(filters.title_passes(t, SETTINGS), t)
        for t in drop:
            self.assertFalse(filters.title_passes(t, SETTINGS), t)

    def test_mba_titles_dropped(self):
        self.assertFalse(filters.title_passes("MBA Full-Time Hire - Strategic Finance & Business Operations", SETTINGS))

    def test_years_takes_smallest_requirement(self):
        self.assertEqual(filters.min_years_required("Requires 5+ years of experience with a BS, or 3+ years of experience with an MS."), 3)
        self.assertEqual(filters.min_years_required("4+ years of experience in analytics."), 4)
        self.assertIsNone(filters.min_years_required("5+ years of experience preferred."))

    def test_experience_drops_minimum_of_four_or_more(self):
        cases = {"3-5 years of experience in analytics.": True,
                 "4+ years of experience in analytics.": False,
                 "5-7 years of experience in analytics.": False,
                 "2 to 4 years of experience required.": True,
                 "A passion for data.": True}  # nothing stated: keep
        for text, keep in cases.items():
            years = filters.min_years_required(text)
            self.assertEqual(filters.experience_passes(years, SETTINGS), keep, text)

    def test_us_status(self):
        t = "Data Analyst"
        self.assertEqual(filters.us_status(t, "Boston, MA", ""), "us")
        self.assertEqual(filters.us_status(t, "Remote US", ""), "us")
        self.assertEqual(filters.us_status(t, "London, UK", ""), "non_us")
        self.assertEqual(filters.us_status(t, "Toronto, Canada", ""), "non_us")
        self.assertEqual(filters.us_status(t, "Anywhere", "IN"), "non_us")
        self.assertEqual(filters.us_status(t, "Remote", ""), "uncertain")
        self.assertEqual(filters.us_status(t, "New London, CT", ""), "us")
        self.assertEqual(filters.us_status(t, "Albuquerque, New Mexico", ""), "us")

    def test_multi_country_keeps_any_us_location(self):
        t = "Business Analyst"
        self.assertEqual(filters.us_status(t, "Remote, Canada; Remote, United States", ""), "us")
        self.assertEqual(filters.us_status(t, "Remote - US or Canada", ""), "us")
        self.assertEqual(filters.us_status(t, "Toronto, Canada | Boston, MA", ""), "us")
        self.assertEqual(filters.us_status(t, "Remote, Canada; Dublin, Ireland", ""), "non_us")
        self.assertEqual(filters.us_status(t, "Toronto, Canada", "US"), "non_us")

    def test_foreign_country_in_title_overrides_feed(self):
        # The Veeva case: feed says US, title says Denmark.
        title = "Master Class - Business Consultant - Life Sciences Analytics (Denmark)"
        self.assertEqual(filters.us_status(title, "Massachusetts - Boston", "US"), "non_us")

    def test_location_groups(self):
        self.assertEqual(filters.location_group("Waltham, MA", "hybrid", SETTINGS), "Boston area")
        # A remote role that lists Boston is still Boston area.
        self.assertEqual(filters.location_group("Massachusetts - Boston", "remote", SETTINGS), "Boston area")
        self.assertEqual(filters.location_group("United States", "remote", SETTINGS), "Remote (US)")
        self.assertEqual(filters.location_group("Austin, TX", "hybrid", SETTINGS), "Hybrid (US)")
        self.assertEqual(filters.location_group("Austin, TX", "onsite", SETTINGS), "Onsite (US)")

    def test_work_mode_prefers_feed(self):
        self.assertEqual(filters.work_mode(make_job("Fully remote role.", workplace_hint="OnSite")), ("onsite", "feed"))
        self.assertEqual(filters.work_mode(make_job("3 days a week in the office.")), ("hybrid", "description"))


class UscisTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_company("Veeva Systems, Inc."), "veeva systems")
        self.assertEqual(normalize_company("ACME   CORP"), "acme")

    def test_match_exact_alias_prefix(self):
        lookup = Lookup({"veeva systems": [21, 2023], "wal mart associates": [1275, 2023],
                         "zs associates": [119, 2023]}, {"walmart": "wal mart associates"})
        self.assertEqual(lookup.match("Veeva Systems Inc"), ("veeva systems", "exact"))
        self.assertEqual(lookup.match("Walmart"), ("wal mart associates", "alias"))
        self.assertEqual(lookup.match("Veeva"), ("veeva systems", "prefix"))
        self.assertEqual(lookup.match("ZS"), (None, "none"))  # too short to prefix-match

    def test_only_exact_and_alias_count_as_sponsor(self):
        lookup = Lookup({"harvey nash": [50, 2026], "palantir technologies": [101, 2026]},
                        {"palantir": "palantir technologies"})
        self.assertEqual(fetch.uscis_match({"name": "Harvey"}, lookup), ("harvey nash", "prefix"))
        self.assertEqual(fetch.uscis_match({"name": "Palantir"}, lookup), ("palantir technologies", "alias"))
        guessed = {"name": "Pattern", "uscis_key": "pattern", "verified": False}
        self.assertEqual(fetch.uscis_match(guessed, lookup)[1], "unverified")
        checked = {"name": "Acme", "uscis_key": "acme", "verified": True}
        self.assertEqual(fetch.uscis_match(checked, lookup)[1], "exact")


class RerunTests(unittest.TestCase):
    """Two runs over the same listing must produce one record per job."""

    def test_no_duplicate_ids_across_runs(self):
        store = {}
        for run in range(2):
            listing = [make_job(apply_url=f"https://x/jobs/123?utm_source=run{run}"),
                       Job(company="Acme", board="greenhouse", slug="acme", native_id="456", title="BI Analyst")]
            for job in listing:
                previous = store.get(job.id)
                store[job.id] = {"id": job.id,
                                 "first_seen_utc": previous["first_seen_utc"] if previous else f"run{run}"}
        self.assertEqual(len(store), 2)
        self.assertTrue(all(r["first_seen_utc"] == "run0" for r in store.values()))


class BackfillTests(unittest.TestCase):
    def test_first_full_sweep_skips_partial_runs(self):
        runs = [{"started_utc": "2026-10-29T17:04:14+00:00", "boards_ok": 3},
                {"started_utc": "2026-10-29T17:08:58+00:00", "boards_ok": 115},
                {"started_utc": "2026-10-29T19:05:05+00:00", "boards_ok": 237},
                {"started_utc": "2026-11-02T13:00:00+00:00", "boards_ok": 236}]
        self.assertEqual(fetch.first_full_sweep(runs), "2026-10-29T19:05:05+00:00")
        self.assertIsNone(fetch.first_full_sweep([]))

    def test_backfill_compares_iso_times(self):
        board_first = "2026-10-29T19:05:05+00:00"
        self.assertTrue("2026-10-29T17:08:58+00:00" <= board_first)   # found in the sweep
        self.assertFalse("2026-11-02T13:00:00+00:00" <= board_first)  # found later: a real new posting


class RunHealthTests(unittest.TestCase):
    def test_one_dead_board_does_not_fail_the_run(self):
        self.assertFalse(fetch.run_failed(boards_ok=239, boards_failed=1))
        self.assertFalse(fetch.run_failed(boards_ok=1, boards_failed=1))  # exactly half is still ok

    def test_most_boards_failing_fails_the_run(self):
        self.assertTrue(fetch.run_failed(boards_ok=100, boards_failed=140))
        self.assertTrue(fetch.run_failed(boards_ok=0, boards_failed=1))   # --only on a dead board


class CompanyListTests(unittest.TestCase):
    lists = {"fortune500": {"names": ["MetLife", "State Street Corp."]},
             "unicorn": {"names": ["Anduril", "BrainCo", "Plaid"]}}

    def test_exact_and_alias_badge(self):
        lk = ListLookup(self.lists, {"Anduril Industries": "Anduril"})
        self.assertTrue(lk.match("State Street")[0]["fortune500"])     # suffixes normalized away
        self.assertTrue(lk.match("Plaid")[0]["unicorn"])
        self.assertTrue(lk.match("Anduril Industries")[0]["unicorn"])  # via alias

    def test_near_miss_is_logged_never_badged(self):
        badges, near = ListLookup(self.lists, {}).match("Metlife Group")
        self.assertFalse(badges["fortune500"])
        self.assertTrue(near and "metlife" in near[0])

    def test_excluded_same_name_is_not_badged(self):
        lk = ListLookup(self.lists, {}, {"Brainco": "different company"})
        self.assertFalse(lk.match("Brainco")[0]["unicorn"])


class DetailTests(unittest.TestCase):
    def test_records_missing_a_detail_field_are_refetched_once(self):
        current = {f: None for f in fetch.DETAIL_FIELDS} | {"description": "text"}
        older = {k: v for k, v in current.items() if k != "closes_raw"}
        self.assertTrue(fetch.can_reuse_details(current))
        self.assertFalse(fetch.can_reuse_details(older))
        self.assertFalse(fetch.can_reuse_details(None))

    def test_workday_end_date_is_kept(self):
        company = {"name": "Acme", "slug": "acme", "tenant": "acme", "pod": 1, "site": "Careers"}
        job = Job(company="Acme", board="workday", slug="acme", native_id="/job/R-1", title="Data Analyst")
        detail = {"jobPostingInfo": {"jobDescription": "<p>SQL</p>", "startDate": "2026-10-14", "endDate": "2026-11-13"}}
        original, workday.request_json = workday.request_json, lambda *a, **kw: detail
        try:
            workday.fill_details(company, job, SETTINGS)
        finally:
            workday.request_json = original
        self.assertEqual((job.posted_raw, job.closes_raw), ("2026-10-14", "2026-11-13"))


if __name__ == "__main__":
    unittest.main()
