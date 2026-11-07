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
import discover  # noqa: E402
import cap_exempt  # noqa: E402
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
        # DAX and causal inference are gaps, never skills; Spark, Kafka, ODBC are left out on purpose
        matched = self.matched("DAX, causal inference, Spark, Kafka, ODBC, Celonis and SimPy.")
        self.assertEqual(matched, [])
        names = {s["name"].lower() for s in SETTINGS["skills"]}
        for banned in ["dax", "causal inference", "spark", "kafka", "odbc"]:
            self.assertNotIn(banned, names)
        self.assertNotIn("PySpark", self.matched("PySpark pipelines"))

    def test_experience_pool_skills(self):
        text = ("dbt and MetricFlow models in DuckDB, Looker dashboards, A/B testing and cohort analysis, "
                "Alteryx workflows, XGBoost in Python, prompt engineering with the Claude API and LLMs, "
                "Google Sheets, Confluence, data quality and data validation checks, data governance.")
        for skill in ["dbt", "MetricFlow", "DuckDB", "Looker", "A/B test", "Cohort analysis", "Alteryx", "Python",
                      "Prompt engineering", "Claude API", "LLM", "Google Sheets", "Confluence", "Data quality",
                      "Data validation", "Data governance"]:
            self.assertIn(skill, self.matched(text), skill)
        self.assertNotIn("Data governance", self.matched("Corporate governance and board reporting."))

    def test_new_skills_and_aliases(self):
        text = ("Run in-depth interviews and thematic coding, build market sizing and peer benchmarking, "
                "own data governance, write SOPs and automate Excel macros.")
        for skill in ["Qualitative research", "Market sizing", "Peer benchmarking", "Data governance", "SOPs", "Excel"]:
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

    def test_pitchbook_no_sponsorship_bullet_under_preferred(self):
        # PitchBook "Associate Analyst, GTM" was tagged "likely": the line sits under "Preferred".
        jd = ("Skills and Qualifications:\n\nBasic\n\n- 1+ years previous experience with SQL and BI tools.\n\n"
              "Preferred\n\n- Bachelor's degree in Business, Analytics, or a related field\n\n"
              "- Must be authorized to work in the United States without the need for visa sponsorship now or in the future\n\n"
              "Benefits + Compensation at PitchBook:\n\n- Comprehensive health benefits")
        is_no, evidence, _ = filters.sponsorship_no(jd)
        self.assertTrue(is_no)
        self.assertEqual(evidence, "without the need for visa sponsorship")

    def test_flexible_no_sponsorship_wordings(self):
        for text in ["Candidates must be legally authorized to work in the US without sponsorship.",
                     "Must be authorized to work without employer sponsorship now or in the future.",
                     "Applicants must be authorized to work without requiring sponsorship.",
                     "We are not able to sponsor visas.", "We are unable to provide sponsorship.",
                     "We cannot offer visa sponsorship.", "The company does not provide work visa sponsorship.",
                     "This role is not eligible for immigration sponsorship.", "Visa sponsorship is not available.",
                     "No sponsorship available for this position.", "We will not provide immigration sponsorship."]:
            self.assertTrue(filters.sponsorship_no(text)[0], text)
        # Not a no: a question, an offer, or an export-license sentence.
        for text in ["Will you now or in the future require visa sponsorship?", "We offer visa sponsorship for strong candidates.",
                     "Access is conditioned on authorization to receive controlled software without sponsorship for an export license."]:
            self.assertFalse(filters.sponsorship_no(text)[0], text)

    def test_jd_no_overrides_uscis_likely(self):
        job = make_job("Must be authorized to work in the United States without the need for visa sponsorship.")
        record, _ = fetch.tag(job, SETTINGS, scoring.build_matchers(SETTINGS["skills"]), True)
        self.assertEqual(record["sponsorship"], "no")
        record, _ = fetch.tag(make_job("Great analyst role."), SETTINGS, scoring.build_matchers(SETTINGS["skills"]), True)
        self.assertEqual(record["sponsorship"], "likely")

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


class BandTests(unittest.TestCase):
    def band(self, title, years):
        return filters.experience_band(title, years, SETTINGS)[0]

    def test_target_band(self):
        for title, years in (("Data Analyst", 0), ("Data Analyst", 2), ("Business Analyst", None),
                             ("Analyst I, Revenue Operations", None), ("Associate Analyst", 1),
                             ("Junior Data Analyst", None), ("New Grad Data Scientist", None)):
            self.assertEqual(self.band(title, years), "target", (title, years))
        self.assertIn("title: Analyst I", filters.experience_band("Data Analyst I", None, SETTINGS)[1])
        self.assertNotIn("title: Analyst I", filters.experience_band("Data Analyst II", 1, SETTINGS)[1])

    def test_stretch_band(self):
        for title, years in (("Data Analyst", 3), ("Data Scientist II", None), ("Business Analyst III", 1),
                             ("Associate Analyst", 3)):  # stretch beats an entry-level title
            self.assertEqual(self.band(title, years), "stretch", (title, years))
        # Case-sensitive level: "ii" inside a word or lowercase isn't a level
        self.assertEqual(self.band("Data Analyst, Hawaii Ops", None), "target")

    def test_entry_level_titles_are_kept(self):
        for title in ("Associate Analyst", "Associate Consultant", "Analyst I", "Junior Analyst",
                      "Entry Level Analyst", "New Grad Data Analyst"):
            self.assertTrue(filters.title_passes(title, SETTINGS), title)
        self.assertFalse(filters.title_passes("Analyst II", SETTINGS))  # "analyst i" needs a word end
        self.assertFalse(filters.title_passes("Senior Associate Consultant", SETTINGS))  # drop list still wins


class CapExemptTests(unittest.TestCase):
    def lookup(self, aliases=None):
        return ListLookup({"cap_exempt": {"names": SETTINGS["cap_exempt_employers"]}},
                          {**SETTINGS["uscis_aliases"], **(aliases or {})})

    def test_exact_and_alias_only(self):
        lk = self.lookup({"Massachusetts Institute of Technology": "MIT"})
        self.assertTrue(lk.match("Dana-Farber Cancer Institute")[0]["cap_exempt"])
        self.assertTrue(lk.match("Mass General Brigham, Inc.")[0]["cap_exempt"])
        self.assertTrue(lk.match("Massachusetts Institute of Technology")[0]["cap_exempt"])
        badges, near = lk.match("Northeastern University Global")  # close, not exact
        self.assertFalse(badges["cap_exempt"])
        self.assertTrue(near)

    def test_extra_titles_only_when_given(self):
        for title in ("Research Data Analyst", "Clinical Data Analyst", "Data Coordinator"):
            self.assertTrue(filters.title_passes(title, SETTINGS, SETTINGS["cap_exempt_titles"]), title)
        self.assertFalse(filters.title_passes("Data Coordinator", SETTINGS))
        self.assertFalse(filters.title_passes("Senior Research Analyst", SETTINGS, SETTINGS["cap_exempt_titles"]))

    def test_extra_search_terms_only_for_cap_exempt(self):
        plain = fetch.search_settings(SETTINGS, False)
        self.assertIs(plain, SETTINGS)
        extra = fetch.search_settings(SETTINGS, True)
        self.assertEqual(extra["workday_search_terms"],
                         SETTINGS["workday_search_terms"] + SETTINGS["cap_exempt_search_terms"])
        self.assertNotIn("data coordinator", SETTINGS["workday_search_terms"])  # original untouched


class FitSignalTests(unittest.TestCase):
    """Gap flags, role-type mismatch and the function boost (filters.py)."""

    def test_required_gaps_cost_points_preferred_do_not(self):
        text = ("Qualifications\n- SQL and Python\n- Experience with DAX and PySpark\n"
                "Preferred Qualifications\n- Exposure to causal inference\n"
                "About us\n- We use Gainsight internally.\n")
        required, preferred = filters.skill_gaps(text, SETTINGS)
        self.assertEqual(sorted(required), ["DAX", "PySpark"])
        self.assertEqual(preferred, ["causal inference"])  # Gainsight in the blurb doesn't count
        self.assertEqual(filters.gap_adjusted_match(60, required, SETTINGS), 30)
        self.assertEqual(filters.gap_adjusted_match(20, required, SETTINGS), 0)  # floor 0
        self.assertEqual(filters.gap_adjusted_match(40, [], SETTINGS), 40)

    def test_gap_spellings(self):
        text = ("Requirements\n- Difference-in-differences, propensity score matching, marketing mix modeling\n"
                "- Multi-touch attribution and SARIMA or Prophet forecasts\n"
                "- Salesforce Administrator certification required\n")
        required, _ = filters.skill_gaps(text, SETTINGS)
        for gap in ["diff-in-diff", "propensity scoring", "marketing mix modeling", "multi-touch attribution",
                    "ARIMA", "Prophet", "Salesforce Administrator certification"]:
            self.assertIn(gap, required, gap)
        self.assertEqual(filters.skill_gaps("Requirements\n- Reporting in Salesforce.\n", SETTINGS), ([], []))

    def mismatch(self, title, text):
        return sorted({m["type"] for m in filters.role_mismatch(title, text, SETTINGS)})

    def test_mismatch_types(self):
        self.assertEqual(self.mismatch("Financial Analyst", "Requirements\n- CPA preferred\n- GAAP and month-end close\n"),
                         ["accounting"])
        self.assertEqual(self.mismatch("Analyst", "Requirements\n- Java or C++ backend services\n"),
                         ["software engineering"])
        self.assertEqual(self.mismatch("Clinical Data Analyst", "Requirements\n- Current RN license in Florida\n"),
                         ["clinical"])
        self.assertEqual(self.mismatch("Assistant Professor: Analytics", "Teach courses."), ["research faculty"])
        self.assertEqual(self.mismatch("Business Analyst", "Requirements\n- PhD required in statistics\n"),
                         ["research faculty"])
        self.assertEqual(self.mismatch("Sales Development Representative - New Grad", "Book meetings."),
                         ["quota-carrying sales"])

    def test_mismatch_needs_clear_evidence(self):
        # Seen on real postings that are in my field
        self.assertEqual(self.mismatch("Clinical Business Operations Representative",
                                       "Support the Physician and the ARNP/RN with scheduling."), [])
        self.assertEqual(self.mismatch("Data Scientist", "Requirements\n- Python, R, Java or C++\n"), [])
        self.assertEqual(self.mismatch("Revenue Operations Analyst",
                                       "Requirements\n- Partner with account executives and SDR leaders\n"), [])
        self.assertEqual(self.mismatch("Financial Analyst", "Requirements\n- Familiar with GAAP\n"), [])  # one hit

    def test_focus_functions(self):
        for title in ["Revenue Operations Analyst", "Sales Operations Analyst", "Commercial Operations Associate",
                      "BI Analyst", "Business Intelligence Analyst", "RevOps Analyst"]:
            self.assertTrue(filters.focus_function(title, SETTINGS), title)
        for title in ["Data Analyst", "Business Analyst", "Bill Analyst"]:
            self.assertFalse(filters.focus_function(title, SETTINGS), title)


class BestFitTests(unittest.TestCase):
    """site/fit.js under Node: the dashboard's Best fit order."""

    def order(self, jobs):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        script = ("const {fitCompare} = require(process.argv[1]); const jobs = JSON.parse(process.argv[2]);"
                  "const newest = (a, b) => b.posted - a.posted;"
                  "console.log(JSON.stringify(jobs.sort((a, b) => fitCompare(a, b, newest)).map(j => j.id)));")
        out = subprocess.run([node, "-e", script, str(ROOT / "site" / "fit.js"), json.dumps(jobs)],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def job(self, id, match, cap=None, h1b="unknown", band="target", posted=0):
        return {"id": id, "match_pct": match, "cap_exempt": cap, "sponsorship": h1b, "band": band, "posted": posted}

    def test_boosts_only_from_30_percent(self):
        jobs = [self.job("weak-uni", 20, cap="confirmed", h1b="likely"),
                self.job("plain-40", 40),
                self.job("uni-30", 30, cap="confirmed"),
                self.job("h1b-35", 35, h1b="likely"),
                self.job("weak-plain", 25)]
        # 30%+ first, boosted by cap-exempt then H-1B; under 30% sorts after, by match only
        self.assertEqual(self.order(jobs), ["uni-30", "h1b-35", "plain-40", "weak-plain", "weak-uni"])

    def test_focus_function_breaks_equal_match(self):
        jobs = [dict(self.job("data-40", 40, posted=5)), dict(self.job("bi-40", 40, posted=1), focus_function=True),
                dict(self.job("data-45", 45)), dict(self.job("revops-35", 35), focus_function=True)]
        # Only among equal match %: 45% still beats the BI role at 40%
        self.assertEqual(self.order(jobs), ["data-45", "bi-40", "data-40", "revops-35"])

    def test_tiers_band_and_ties(self):
        jobs = [self.job("possible", 50, cap="possible"), self.job("confirmed", 31, cap=True),
                self.job("stretch", 90, cap="confirmed", band="stretch"),
                self.job("old", 60, posted=1), self.job("new", 60, posted=2)]
        self.assertEqual(self.order(jobs), ["confirmed", "possible", "new", "old", "stretch"])


class CacheTests(unittest.TestCase):
    def test_damaged_cache_file_is_a_miss(self):
        import tempfile
        import net
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.json"
            path.write_bytes(b'{"a": "caf\xe1"')  # not UTF-8, cut off
            self.assertIsNone(net._read_cache(path, 3600))
            self.assertIsNone(net._read_cache(Path(d) / "missing.json", 3600))
            path.write_text('{"a": 1}', "utf-8")
            self.assertEqual(net._read_cache(path, 3600), '{"a": 1}')
            self.assertIsNone(net._read_cache(path, 0))  # caching off


class CapExemptTierTests(unittest.TestCase):
    def test_possible_tier(self):
        employers = {
            "state college of florida": [3, 2026, "61"],
            "ohio state university": [900, 2026, "61"],
            "university of phoenix": [5, 2026, "61"],            # for-profit, excluded
            "university hospitals cleveland": [40, 2026, "61"],  # hospital: never possible
            "acme college consulting": [2, 2026, "54"],          # wrong sector
            "brooklyn academy": [1, 2026, "61"],                 # no university-like word
            "old entry": [1, 2024],                              # lookup built before sectors
        }
        self.assertEqual(cap_exempt.possible_keys(employers, SETTINGS),
                         {"state college of florida", "ohio state university"})

    def test_aau_table_parse(self):
        wikitext = ("===Current members===\n{| class=\"wikitable\"\n|-\n"
                    "! scope=\"row\" |[[Arizona State University]]\n|Arizona\n|Public\n|-\n"
                    "! scope=\"row\" |[[University of Toronto]]\n|Ontario\n|Public\n|-\n"
                    "! scope=\"row\" |[[Washington University in St. Louis|Washington University]]\n|Missouri\n|Private\n|}\n")
        self.assertEqual(cap_exempt.parse_aau(wikitext), ["Arizona State University", "Washington University"])

    def test_carnegie_campus_names(self):
        self.assertEqual(cap_exempt.common_name("Pennsylvania State University-Main Campus"),
                         "Pennsylvania State University")
        self.assertEqual(cap_exempt.common_name("Columbia University in the City of New York"), "Columbia University")
        self.assertEqual(cap_exempt.common_name("University of California-Berkeley"),
                         "University of California-Berkeley")  # a campus that is its own institution stays

    def test_board_links(self):
        self.assertEqual(cap_exempt._system("https://osu.wd1.myworkdayjobs.com/OSUCareers"), "Workday")
        self.assertEqual(cap_exempt._system("https://jobs.uky.peopleadmin.com/postings"), "PeopleAdmin")
        self.assertIsNone(cap_exempt._system("https://www.example.edu/jobs"))
        m = cap_exempt._WD.search("https://wd1.myworkdaysite.com/en-US/recruiting/osu/OSUCareers")
        self.assertEqual((m.group(4), m.group(5), m.group(6)), ("1", "osu", "OSUCareers"))


class PeopleSearchTests(unittest.TestCase):
    def test_peer_title_and_manager_keywords(self):
        p = filters.people_search("Data Analyst I - Revenue (Remote)", SETTINGS)
        self.assertEqual(p["peer"], "Data Analyst I")
        self.assertEqual(p["manager"], SETTINGS["people_search"]["default_manager"])
        self.assertIn("data science manager", filters.people_search("Data Scientist II, Growth", SETTINGS)["manager"])
        self.assertIn("BI manager", filters.people_search("BI Analyst", SETTINGS)["manager"])
        self.assertEqual(filters.people_search("Business Analyst | Payments", SETTINGS)["peer"], "Business Analyst")
        self.assertEqual(filters.people_search("Analyst R12345", SETTINGS)["peer"], "Analyst")
        self.assertEqual(p["alumni"], "Northeastern")


class StaffingTests(unittest.TestCase):
    def reasons(self, text, sector=""):
        return [r.split(":")[0] for r in filters.staffing_reasons(text, sector, SETTINGS)]

    def test_staffing_phrases_flag(self):
        self.assertEqual(self.reasons("Contract role. C2C or W2 accepted."), ["C2C"])
        self.assertEqual(self.reasons("Corp-to-corp candidates welcome."), ["C2C"])
        self.assertEqual(self.reasons("Visa: H1B. W2 only, no OPT."), ["W2 only"])
        self.assertEqual(self.reasons("Our client is in need of a data analyst."), ["our client"])
        self.assertEqual(self.reasons("Implementation Partner: Infosys. Client: a bank."), ["implementation partner"])
        self.assertEqual(self.reasons("We have bench consultants ready to deploy."), ["bench"])
        self.assertEqual(self.reasons("Job Duration: 6+ Months Contract role"), ["duration"])
        self.assertEqual(self.reasons("Location: Boston, MA Duration: 12+ Months"), ["duration"])
        self.assertEqual(self.reasons("This is a 12-month contract."), ["duration"])
        self.assertEqual(self.reasons("Contract-to-hire position."), ["contract to hire"])
        self.assertEqual(self.reasons("Type: C2H"), ["contract to hire"])
        self.assertEqual(self.reasons("Saxon is a IT Consulting & Staffing Company located at Irving, TX"),
                         ["self-described staffing"])
        self.assertEqual(self.reasons("We are a global staffing, consulting and technology company."),
                         ["self-described staffing"])
        self.assertEqual(self.reasons("Leading IT staffing provider."), ["self-described staffing"])

    def test_visa_lists(self):
        self.assertEqual(self.reasons("We are looking for OPT/CPT/H1/EAD/GC/Citizen candidates."), ["visa list (OPT, CPT, H-1B, EAD, GC, Citizen)"])
        self.assertEqual(self.reasons("Visa: H-1B, GC, US Citizen only"), ["visa list (H-1B, GC, Citizen)"])
        # Two kinds is not a list; sponsorship wording is a direct employer's policy
        self.assertEqual(self.reasons("Open to GC holders and US Citizens."), [])
        self.assertEqual(self.reasons("We cannot sponsor H-1B, OPT or CPT candidates."), [])
        self.assertEqual(self.reasons("Lowercase opt in to the gc and citizen program."), [])

    def test_manual_company_list(self):
        self.assertEqual(filters.staffing_reasons("Analyst role.", "", SETTINGS, "Tech Tammina, Inc."),
                         ["listed in staffing_companies"])
        self.assertEqual(filters.staffing_reasons("Analyst role.", "", SETTINGS, "Technology Partners"), [])

    def test_weak_only(self):
        self.assertTrue(filters.weak_only(["duration: Duration: 6 Months"], SETTINGS))
        self.assertFalse(filters.weak_only(["duration: x", "C2C: y"], SETTINGS))
        self.assertFalse(filters.weak_only(["C2C: y"], SETTINGS))

    def test_company_level_rule(self):
        def job(company, reasons, active=True):
            return {"company": company, "active": active, "staffing_reasons": reasons}
        jobs = [job("Saxon", ["duration: a"]), job("Saxon", ["C2C: b"]), job("Saxon", []),
                job("Saxon", [], active=False),
                job("Acme", ["duration: c"]), job("Acme", [])]
        fetch.company_staffing(jobs, 2)
        self.assertEqual(jobs[2]["staffing_reasons"], ["company: 2 of its jobs are flagged"])
        self.assertFalse(jobs[3]["staffing"])  # inactive jobs are left alone
        self.assertFalse(jobs[5]["staffing"])  # one flagged job isn't enough
        # Rerun after Saxon drops to one flagged job: the company flag goes away
        jobs[1]["staffing_reasons"] = []
        fetch.company_staffing(jobs, 2)
        self.assertEqual([j["staffing"] for j in jobs[:3]], [True, False, False])

    def test_ordinary_wording_is_not_staffing(self):
        # Seen on real direct-employer postings (Equifax, Citi, OpenAI, Veeva, IQVIA)
        for text in ("This is a direct-hire role and is not open to C2C or vendors.",
                     "Sharpen understanding of our client's needs.",
                     "Present results to our Client Services teams.",
                     "Help shape the bench around a commercial area.",
                     "Run benchmarks on the lab bench.",
                     "Work with our implementation partners on rollouts.",
                     "If contacted by a recruiting agency for an Anduril role, their email will identify it.",
                     "Recruiting Agency Notice",
                     "We do not accept unsolicited resumes from staffing agencies.",
                     "Duration: Full-time, W-2 position",
                     "A 12-week summer internship."):
            self.assertEqual(self.reasons(text), [], text)

    def test_naics_sector_only_when_configured(self):
        settings = {**SETTINGS, "staffing": {**SETTINGS["staffing"], "naics_sectors": ["56"]}}
        self.assertEqual(filters.staffing_reasons("Analyst role.", "56", settings), ["USCIS NAICS sector 56"])
        self.assertEqual(filters.staffing_reasons("Analyst role.", "54", settings), [])
        self.assertEqual(self.reasons("Analyst role.", "56"), [])  # empty by default

    def test_lookup_reports_sector(self):
        lookup = Lookup({"acme staffing": [10, 2026, "56"], "old": [3, 2024]}, {})
        self.assertEqual(lookup.naics_sector("acme staffing"), "56")
        self.assertEqual(lookup.naics_sector("old"), "")  # lookups built before the sector field
        self.assertEqual(lookup.naics_sector(None), "")

    def test_staffing_log_groups_by_company(self):
        jobs = [{"company": "Sriven", "title": "Data Analyst", "active": True, "staffing": True,
                 "staffing_reasons": ["our client: Our client is in need..."]},
                {"company": "Sriven", "title": "Old", "active": False, "staffing": True, "staffing_reasons": ["x"]},
                {"company": "Acme", "title": "Analyst", "active": True, "staffing": False, "staffing_reasons": []}]
        self.assertEqual(fetch.staffing_log(jobs, SETTINGS),
                         ["Sriven (1 job)", "    [weak] Data Analyst | our client: Our client is in need..."])


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

    def test_years_highest_across_bullets_lowest_among_alternatives(self):
        # Ellipsis Health: one bullet, a total plus its parts. Not alternatives: highest, 3 (was 1).
        ellipsis = ("QUALIFICATIONS\n\n- 3 to 4+ years of experience in a data-heavy work environment "
                    "(e.g., finance, consulting), including 2+ years of investment banking, private equity / "
                    "growth equity, and/or consulting, and 1+ years of operational experience at a "
                    "high-growth technology company.\n- Highly proficient in Google Workspace.")
        self.assertEqual(filters.min_years_required(ellipsis), 3)
        # Separate bullets are separate requirements: highest.
        self.assertEqual(filters.min_years_required(
            "Requirements:\n- 2+ years in business operations.\n- 4 years of SQL.\n- 1+ year with Tableau."), 4)
        # One sentence offering alternatives: the lowest option the candidate meets.
        self.assertEqual(filters.min_years_required("5 years of experience, or 2 years with a Master's degree."), 2)
        self.assertEqual(filters.min_years_required(
            "Requirements:\n- MS or MA plus 2 years, or BS or BA plus 3 years, of experience in analytics."), 2)
        # Doctorate options don't count: no PhD.
        self.assertEqual(filters.min_years_required(
            "Requirements:\n- PhD with 1-3 years, MS or MA with 2-6 years, or BS or BA with 4-8 years of data science experience"), 2)
        self.assertEqual(filters.min_years_required("6 years of experience, or 2 years with a PhD."), 6)
        # An alternative that opens the next bullet belongs to the one before it.
        self.assertEqual(filters.min_years_required(
            "Qualifications:\n- BS in statistics with three (3) years of experience or equivalent;\n"
            "- or MS in statistics with one (1) year of experience;"), 1)
        # "and/or" joins items; it is not an alternative.
        self.assertEqual(filters.min_years_required("Requirements:\n- 3+ years in finance and/or 1+ years in consulting."), 3)

    def test_years_single_sentence_alternatives_and_plain(self):
        self.assertEqual(filters.min_years_required("Requires 5+ years of experience with a BS, or 3+ years of experience with an MS."), 3)
        self.assertEqual(filters.min_years_required("4+ years of experience in analytics."), 4)
        self.assertIsNone(filters.min_years_required("5+ years of experience preferred."))

    def test_years_phrasings(self):
        cases = {
            "8+ years of experience in analytics.": 8,
            "8 or more years of experience in analytics.": 8,
            "Minimum of 8 years of experience.": 8,
            "At least 8 years of relevant experience.": 8,
            "8 years of experience with SQL.": 8,
            "8+ yrs experience in BI.": 8,
            "Eight years of experience in consulting.": 8,
            "Eight (8) years of experience required.": 8,
            "5-7 years of experience in analytics.": 5,
            "5 to 7 years of experience in analytics.": 5,
            "5–7 years' experience in analytics.": 5,
            "3 years' experience in a data role.": 3,
            "1 year of experience with Tableau.": 1,
        }
        for text, years in cases.items():
            self.assertEqual(filters.min_years_required(text), years, text)

    def test_years_in_requirements_section_need_no_experience_word(self):
        text = ("About us\nWe have been building GPUs for more than 25 years.\n\n"
                "What we need to see:\n- 8+ years in management consulting at a strategy firm.\n"
                "- A degree in Economics.\n\nWays to stand out from the crowd:\n- 2+ years of SQL experience.")
        self.assertEqual(filters.min_years_required(text), 8)   # not 25 (history), not 2 (stand out)

    def test_years_hedge_in_a_later_clause_still_counts(self):
        # A hedge on the domain ("ideally on data", "(fintech preferred)") never changes the years.
        cases = {"- 7+ years of supply chain systems experience, ideally on data or internal tools": 7,
                 "- 3+ years merchant risk experience in fraud detection (fintech or lending preferred)": 3,
                 "- 6+ years of experience in a quantitative role (ideally within mobility)": 6,
                 "- 5+ years of financial services experience, preferably in payments.": 5}
        for text, years in cases.items():
            self.assertEqual(filters.min_years_required(text), years, text)

    def test_preferred_word_about_the_years_makes_the_figure_preferred(self):
        for text in ["3+ years preferred.", "5+ years of experience preferred.", "Preferably 3 years of SQL.",
                     "Ideally 5+ years of experience in fintech.", "2+ years of SQL experience is a plus.",
                     "- Minimum of 3 years of experience in healthcare revenue cycle management, billing, or "
                     "financial analysis strongly preferred."]:
            self.assertIsNone(filters.min_years_required(text), text)

    def test_additional_qualifications_is_a_preferred_section(self):
        # Harvard HILS: "Basic" stays required; "Additional Qualifications and Skills" is preferred.
        harvard = ("Basic Qualifications:\n\n- Minimum of two years post-secondary education or relevant work experience.\n\n"
                   "Additional Qualifications and Skills:\n\n- Bachelor's degree preferred.\n\n"
                   "- 5+ years' experience in defining requirements for desktop, mobile, or web-based software "
                   "applications and relational databases preferred.\n\nCertificates and Licenses:\n\n- Completion of courses preferred.")
        self.assertEqual(filters.min_years_required(harvard), 2)
        self.assertEqual(filters.min_years_preferred(harvard), 5)
        # A bare 5-year figure under Additional Qualifications is preferred; under Basic it is required.
        self.assertIsNone(filters.min_years_required("Additional Qualifications\n- 5+ years of SQL experience"))
        self.assertEqual(filters.min_years_required("Basic Qualifications\n- 5+ years of SQL experience"), 5)
        self.assertIsNone(filters.min_years_required("Additional Qualifications:\n- 4 years in analytics"))

    def test_preferred_years_in_a_preferred_section_and_with_several_figures(self):
        # University of Chicago: the required 2-5 stays, the strongly preferred 3 does not count.
        uchicago = ("Minimum Qualifications\n\nWork Experience:\n2-5 years of work experience in a related job discipline.\n\n"
                    "Preferred Qualifications\n\nExperience:\n\n- Minimum of 3 years of experience in healthcare "
                    "revenue cycle management, billing, or financial analysis strongly preferred.")
        self.assertEqual(filters.min_years_required(uchicago), 2)
        # With several figures in one bullet only the figure's own clause counts.
        self.assertEqual(filters.min_years_required("Requirements:\n- 5+ years required, 8+ years preferred."), 5)

    def test_years_degree_versus_experience_bullets_are_alternatives(self):
        self.assertEqual(filters.min_years_required(
            "Required Qualifications\n\n- Bachelor's degree and 2 years of experience.\n"
            "- Four years of experience in operations.\n"
            "- Or an equivalent combination of education and experience."), 2)
        # Ohio State: a heading with a parenthetical note still ends the required section.
        osu = ("Required Qualifications\n\n- Bachelor's degree or equivalent combination of education/experience.\n\n"
               "- 2 year's of experience in business operations, administrative support, or related field.\n\n"
               "Desired Qualifications (We're committed to building a team that reflects a variety of skills and "
               "experiences and encourage you to apply even if your experience does not align perfectly)\n\n"
               "- Four (4) years of relevant experience\n\n- Knowledge of University fiscal policies desired.")
        self.assertEqual(filters.min_years_required(osu), 2)
        self.assertEqual(filters.min_years_preferred(osu), 4)
        # Without the equivalency wording, separate bullets stay separate requirements.
        self.assertEqual(filters.min_years_required(
            "Requirements:\n- 2 years of SQL.\n- 4 years of Python."), 4)

    def test_years_without_experience_word_outside_sections(self):
        cases = {"- 3+ years in finance operations or sales operations.": 3,
                 "- 7+ years in a capacity planning and forecasting role": 7,
                 "- +2 years in a cybersecurity skilled role": 2}
        for text, years in cases.items():
            self.assertEqual(filters.min_years_required(text), years, text)
        for history in ["We've had these bonuses for the last 3 years.",
                        "Software built 20-40 years ago.",
                        "We spent 3 years building the rails.",
                        "Named a best workplace 4 years in a row.",
                        "Placed in the INC 5000 list six times in the last ten years."]:
            self.assertIsNone(filters.min_years_required(history), history)

    def test_preferred_section_years_are_display_only(self):
        text = ("Qualifications:\n- Excellent communication skills\n\n"
                "Preferred Qualifications:\n- 1+ years experience in an analytical field")
        self.assertIsNone(filters.min_years_required(text))
        self.assertEqual(filters.min_years_preferred(text), 1)

    def test_mixed_heading_counts_as_requirements(self):
        text = ("Education & Preferred Qualifications\n"
                "- Bachelor's degree in Finance preferred\n"
                "- 5+ years of financial services experience, preferably in payments.\n"
                "- 2+ years of Python experience is a plus")
        self.assertEqual(filters.min_years_required(text), 5)     # the "a plus" line stays preferred
        self.assertEqual(filters.min_years_required(
            "Required and Preferred Qualifications:\n- 4+ years in risk analytics"), 4)
        # Preferred word first: the whole heading is preferred
        text = "Preferred Skills and Experience\n- 6+ years in fintech"
        self.assertIsNone(filters.min_years_required(text))
        self.assertEqual(filters.min_years_preferred(text), 6)

    def test_a_bullet_is_never_a_heading(self):
        text = ("The Expertise and Skills You Bring\n- Bachelor's degree or equivalent experience preferred\n"
                "- 3-5 years of Accounting/Finance or Stock Plan Administration experience")
        self.assertEqual(filters.min_years_required(text), 3)

    def test_years_ignores_history_age_and_preferred(self):
        self.assertIsNone(filters.min_years_required("We have served clients for 30 years."))
        self.assertIsNone(filters.min_years_required("Requirements:\nMust be 18 years or older."))
        self.assertIsNone(filters.min_years_required("Requirements:\n- SQL\nNice to have:\n- 5+ years in fintech"))

    def test_experience_drops_minimum_of_four_or_more(self):
        cases = {"3-5 years of experience in analytics.": True,
                 "4+ years of experience in analytics.": False,
                 "5-7 years of experience in analytics.": False,
                 "2 to 4 years of experience required.": True,
                 "A passion for data.": True}  # nothing stated: keep
        for text, keep in cases.items():
            years = filters.min_years_required(text)
            self.assertEqual(filters.experience_passes(years, SETTINGS), keep, text)

    def test_foreign_cities_and_codes(self):
        t = "Data Analyst"
        for loc in ["Gurugram IN", "Gurugram, Haryana, IN", "Bengaluru, KA, IND", "Toronto, ON",
                    "Toronto, ON, CA", "London", "Dublin", "Singapore", "Paris, FR", "Sydney, AU",
                    "Tel Aviv", "Mexico City", "Remote - Gurugram"]:
            self.assertEqual(filters.us_status(t, loc, ""), "non_us", loc)
        for loc in ["Indianapolis, IN", "Dublin, CA", "Dublin, Ohio", "Melbourne, FL", "Paris, TX",
                    "New London, CT", "Cambridge, MA", "Wilmington, DE", "Boise, ID",
                    "Gurugram IN; Boston, MA"]:
            self.assertEqual(filters.us_status(t, loc, ""), "us", loc)
        self.assertEqual(filters.us_status("Market Analyst - Gurugram", "Remote", ""), "non_us")

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


class DiscoveryTests(unittest.TestCase):
    def test_list_links_take_the_row_company_name(self):
        text = (
            '<td><strong><a href="https://simplify.jobs/c/RELX">RELX</a></strong></td>\n'
            '<td><a href="https://relx.wd3.myworkdayjobs.com/en-US/relx/job/Alpharetta-GA/X_R1">Apply</a></td>\n'
            '<td>↳</td><td><a href="https://relx.wd3.myworkdayjobs.com/en-US/RiskSolutions/job/Y_R2">Apply</a></td>\n'
            '| <a href="https://www.nvidia.com"><strong>NVIDIA</strong></a> | Analyst | '
            '<a href="https://jobs.smartrecruiters.com/BoschGroup/744-analyst">Apply</a> |\n')
        found = {}
        discover.parse_list(text, found)
        self.assertEqual(found[("workday", "relx", 3, "relx")], "RELX")
        self.assertEqual(found[("workday", "relx", 3, "risksolutions")], "RELX")
        self.assertEqual(found[("smartrecruiters", "BoschGroup")], "NVIDIA")  # same-row name wins
        self.assertNotIn(("workday", "relx", 3, "job"), found)

    def test_location_count_is_never_a_company_name(self):
        text = (
            '<td><strong>🔥 Salesforce</strong></td><td>Analyst</td>\n'
            '<td>↳</td><td><details><summary><strong>6 locations</strong></summary>SF, NYC</details></td>'
            '<td><a href="https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/X_R1">Apply</a></td>\n')
        found = {}
        discover.parse_list(text, found)
        self.assertEqual(found[("workday", "salesforce", 12, "external_career_site")], "Salesforce")

    def test_workday_tenant_guesses_are_alphanumeric(self):
        self.assertEqual(discover.tenant_guesses("state street bank and trust"),
                         ["statestreetbankandtrust", "statestreetbanktrust"])


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
