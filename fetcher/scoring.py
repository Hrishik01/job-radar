"""Match % = keyword overlap between a description and the skills list.

Whole-word matching only, so "SAP" never matches "ASAP", "Git" never matches
"digital", and "SQL" never matches "NoSQL". "R" has its own rule below.
"""

from __future__ import annotations

import re

# "R" counts only in explicit language forms, and never as part of "R&D".
_NOT_AMP = r"(?!\s*&)"
_R_PATTERNS = [
    r"(?<![\w&])R\s+programming\b",
    r"\bin R(?![\w&])" + _NOT_AMP,
    r"(?<![\w&/])R\s*/\s*(?:Python|SQL|SAS|Stata)\b",
    r"\b(?:Python|SQL|SAS|Stata)\s*/\s*R(?![\w&])" + _NOT_AMP,
    r"(?<![\w&])R,",
    r",\s*R(?![\w&])" + _NOT_AMP,
]
R_REGEX = re.compile("|".join(_R_PATTERNS))  # case-sensitive on purpose

# Headings that open a requirements section, and headings that end one.
_REQ_HEADING = re.compile(
    r"^(?:basic |minimum |required |preferred |desired |key )?"
    r"(?:qualifications|requirements|skills(?: and| &) (?:experience|qualifications)|"
    r"what you(?:'|’)ll (?:need|bring)|what we(?:'|’)re looking for|who you are|you (?:have|bring)|"
    r"about you|experience)\b",
    re.IGNORECASE,
)
_OTHER_HEADING = re.compile(
    r"^(?:responsibilities|what you(?:'|’)ll do|about (?:us|the (?:role|team|company|job))|benefits|perks|"
    r"what we offer|compensation|pay|salary|equal (?:employment )?opportunity|our values|why join|"
    r"location|the role|your role|job description|overview)\b",
    re.IGNORECASE,
)


def _is_heading(line: str) -> bool:
    return 0 < len(line) <= 60 and len(line.split()) <= 8 and not line.endswith(".")


def split_sections(description: str) -> tuple[str, str, bool]:
    """(requirements_text, other_text, found). If no requirements heading is
    found, everything is 'other' and found is False."""
    req, other, in_req, found = [], [], False, False
    for line in description.splitlines():
        stripped = line.strip().lstrip("-•* ").rstrip(":")
        if _is_heading(stripped):
            if _REQ_HEADING.match(stripped):
                in_req, found = True, True
                continue
            if _OTHER_HEADING.match(stripped):
                in_req = False
        (req if in_req else other).append(line)
    return "\n".join(req), "\n".join(other), found


def build_matchers(skills: list[dict]) -> list[tuple[str, re.Pattern]]:
    matchers = []
    for skill in skills:
        if skill.get("special") == "r_language":
            matchers.append((skill["name"], R_REGEX))
            continue
        alternatives = "|".join(re.escape(a) for a in skill["aliases"])
        flags = 0 if skill.get("case_sensitive") else re.IGNORECASE
        pattern = rf"(?<!\w)(?:{alternatives})(?!\w)"
        if skill["name"] == "Excel":
            pattern += r"(?!\s+(?:in|at)\b)"  # "excel in a fast-paced..." is a verb
        matchers.append((skill["name"], re.compile(pattern, flags)))
    return matchers


def score(description: str, matchers: list[tuple[str, re.Pattern]], cap: int) -> tuple[int, list[str]]:
    """Returns (match_pct, matched skill names).

    A skill found in a detected requirements section weighs 1.0; elsewhere 0.5.
    If no requirements section is detected, every hit weighs 1.0.
    match_pct = total weight / cap, so hitting `cap` skills is 100%.
    """
    req, other, found = split_sections(description)
    total, matched = 0.0, []
    for name, rx in matchers:
        if found and rx.search(req):
            total += 1.0
        elif rx.search(other if found else description):
            total += 0.5 if found else 1.0
        else:
            continue
        matched.append(name)
    return round(min(total, cap) / cap * 100), matched
