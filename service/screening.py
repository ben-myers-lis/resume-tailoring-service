"""Fit screening before tailoring, designed for title-only profiles.

What it can judge from titles, employers and dates: field of work, years of relevant
experience, seniority, and hard credentials named in the JD (PhD, licences).
Skills are unknown from titles alone, so they are reported as "unknown" and never lower the score.
Only clear mismatches should fall below the threshold.
"""
from __future__ import annotations

import re
from datetime import date

from .history import ADJACENT, FAMILIES, family, level, months_union

CREDENTIALS = [  # (pattern in JD requirement text, pattern that satisfies it in education/certifications)
    (r"\bph\.?d\b|doctorate", r"\bph\.?d\b|doctor"),
    (r"\bmaster'?s\b|\bm\.?s\.?\b(?= in)", r"\bmaster|\bm\.?s\b|\bm\.?sc\b|\bmba\b|\bph\.?d\b"),
    (r"\bcpa\b", r"\bcpa\b|certified public accountant"),
    (r"\brn\b|registered nurse", r"\brn\b|registered nurse"),
]


def job_family(job: dict) -> str:
    """Field of the job: from its title, or (if the title is missing or unclear) from the description."""
    f = family(job["role"])
    if f != "other":
        return f
    text = job["text"].lower()
    counts = {name: len(re.findall(pattern, text)) for name, pattern in FAMILIES}
    best = max(counts, key=counts.get)
    return best if counts[best] else "other"


def job_level(job: dict) -> int:
    if family(job["role"]) != "other":
        return level(job["role"])
    head = job["text"][:300].lower()            # only clear seniority words; "lead a team" etc. is ignored
    for value, pattern in ((5, r"\b(director|head of|vp)\b"), (4, r"\b(staff|principal)\b"),
                           (3, r"\bsenior\b"), (1, r"\b(junior|intern|entry[- ]level|graduate)\b")):
        if re.search(pattern, head):
            return value
    return 2


def required_years(jd_text: str) -> int | None:
    years = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", jd_text, flags=re.I)]
    return max(years) if years else None


def screen(candidate: dict, job: dict, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    roles = candidate["employment"]
    text = job["text"]
    jd_family, jd_level = job_family(job), job_level(job)

    # 1. Field of work (30 pts): best match across the candidate's titles, recent roles first.
    rel_roles, adj_roles = [], []
    for r in roles:
        f = family(r["title"])
        if f == jd_family and f != "other":
            rel_roles.append(r)
        elif (f, jd_family) in ADJACENT:
            adj_roles.append(r)
    field_pts = 30 if rel_roles else 18 if adj_roles else 0

    # 2. Years of relevant experience (40 pts). Adjacent roles count at half weight.
    rel_m = months_union(rel_roles, as_of)
    adj_m = months_union(rel_roles + adj_roles, as_of) - rel_m
    rel_years = (rel_m + adj_m / 2) / 12
    need = required_years(text)
    years_pts = 40 * min(1.0, rel_years / need) if need else 40 * min(1.0, rel_years)

    # 3. Seniority (20 pts): compare the job's level with the candidate's highest recent level.
    recent = sorted(roles, key=lambda r: r["start_date"], reverse=True)[:3]
    cand_level = max((level(r["title"]) for r in recent), default=1)
    gap = jd_level - cand_level
    seniority_pts = 20 if gap <= 0 else 14 if gap == 1 else 6 if gap == 2 else 0

    # 4. Hard credentials named in the JD (10 pts, or a 20-pt penalty if a required one is missing).
    creds_text = " ".join(candidate.get("education", []) + candidate.get("certifications", [])).lower()
    req_text = _required_section(text).lower()
    missing_creds = [pat for pat, ok in CREDENTIALS if re.search(pat, req_text) and not re.search(ok, creds_text)]
    cred_pts = -20 if missing_creds else 10

    score = max(0, min(100, round(field_pts + years_pts + seniority_pts + cred_pts)))
    met, missing = [], []
    (met if field_pts == 30 else missing).append(
        f"Field: {'related titles' if field_pts == 30 else 'adjacent titles' if field_pts else 'no related titles'}")
    if need:
        (met if rel_years >= need else missing).append(f"Experience: ~{rel_years:.1f} relevant years vs {need}+ required")
    (met if gap <= 0 else missing).append(f"Seniority: candidate level {cand_level} vs role level {jd_level}")
    for pat in missing_creds:
        missing.append(f"Credential required by JD not listed ({pat.split('|')[0].strip(chr(92) + 'b')})")
    return {
        "fit_score": score, "method": "title-rules-v1",
        "met": met, "missing": missing,
        "unknown": ["Specific skills and tools (not visible from titles alone)"],
        "band": band(score), "reason": _reason(score, field_pts, rel_years, need, gap, missing_creds),
        "parts": {"field": field_pts, "years": round(years_pts, 1), "seniority": seniority_pts, "credentials": cred_pts},
    }


def _required_section(text: str) -> str:
    m = re.search(r"required:?(.*?)(preferred|nice to have|responsibilities|$)", text, flags=re.I | re.S)
    return m.group(1) if m else text


def band(score: int) -> str:
    return "STRONG" if score >= 80 else "GOOD" if score >= 60 else "STRETCH" if score >= 40 else "POOR"


def _reason(score, field_pts, rel_years, need, gap, missing_creds) -> str:
    bits = []
    if field_pts == 0:
        bits.append("no related job titles")
    elif field_pts < 30:
        bits.append("adjacent rather than direct experience")
    if need and rel_years < need:
        bits.append(f"about {rel_years:.1f} of {need}+ required years")
    if gap == 1:
        bits.append("one level below the role")
    elif gap >= 2:
        bits.append("a large seniority jump")
    if missing_creds:
        bits.append("a required credential is not listed")
    label = {"STRONG": "Strong fit", "GOOD": "Good fit", "STRETCH": "Stretch", "POOR": "Poor fit"}[band(score)]
    if not bits:
        return f"{label}: titles, tenure and seniority align with the role."
    return f"{label}: " + ", ".join(bits) + "."
