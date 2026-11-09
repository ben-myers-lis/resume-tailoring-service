"""Bridge to the prompt package (prompt_package/): imports its runner and builds requests."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "prompt_package"
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from app import runner  # noqa: E402  (the package's own runner, unchanged)

PACKAGE_VERSION = runner.VERSION


@lru_cache(maxsize=1)
def catalog_terms() -> list[str]:
    """All technical terms from the package's keyword catalog, longest first."""
    cat = json.loads((PACKAGE_DIR / "reference/ai_keyword_catalog.json").read_text())
    terms = {t for c in cat["categories"] for g in c["groups"] for t in g["terms"]}
    return sorted(terms, key=len, reverse=True)


def find_terms(text: str) -> list[str]:
    """Catalog terms that appear in text (case-insensitive, word-bounded), in order of appearance."""
    found = []
    taken: list[tuple[int, int]] = []
    for term in catalog_terms():
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append((m.start(), term))
                break
    return [t for _, t in sorted(found)]


def build_request(*, run_id: str, candidate: dict, job: dict, mode: str = "APPLICATION",
                  as_of: date | None = None, max_repair_rounds: int = 1) -> dict:
    """Turn a stored candidate + job into the package's request format."""
    employment = []
    for i, r in enumerate(candidate["employment"], start=1):
        current = not r.get("end_date")
        employment.append({
            "role_id": f"R{i}", "employer": r["company"], "official_title": r["title"],
            "start_date": r["start_date"], "end_date": None if current else r["end_date"],
            "is_current": current, "location": r.get("location"),
            "location_type": "WORK_LOCATION" if r.get("location") else "UNKNOWN",
        })
    contacts = []
    if candidate.get("email"):
        contacts.append({"kind": "EMAIL", "value": candidate["email"]})
    if candidate.get("linkedin"):
        contacts.append({"kind": "LINKEDIN", "value": candidate["linkedin"]})
    # Optional facts. Per-role notes are labelled with the employer so the model (and the
    # package's checks) keep the evidence with the right job. Title-only profiles have none.
    raw = [(s.get("kind", "CANDIDATE_STATEMENT"), s["content"]) for s in candidate.get("sources") or []]
    for r in candidate["employment"]:
        if (r.get("notes") or "").strip():
            raw.append(("CANDIDATE_STATEMENT", f"At {r['company']} as {r['title']}: {r['notes'].strip()}"))
    sources = [{"source_id": f"S{i}", "candidate_id": candidate["id"], "kind": kind, "content": content}
               for i, (kind, content) in enumerate(raw, start=1)]
    illustrative = mode == "ILLUSTRATIVE"
    return {
        "package_version": PACKAGE_VERSION,
        "run_id": run_id,
        "run_type": "NEW_JD",
        "as_of_date": (as_of or date.today()).isoformat(),
        "candidate": {
            "candidate_id": candidate["id"],
            "profile_version": str(candidate["profile_version"]),
            "name": candidate["name"],
            "headline": candidate.get("headline"),
            "contacts": contacts,
            "employment": employment,
            "education": candidate.get("education") or [],
            "certifications": candidate.get("certifications") or [],
        },
        "sources": sources,
        "jd": {"jd_id": job["id"], "company": job.get("company"), "role": job["role"], "text": job["text"]},
        "external_context": [],
        "options": {
            "mode": mode,
            "metric_policy": "EVIDENCED_ONLY",
            "hypothetical_metrics_authorized": False,
            "illustrative_scenario": "Internal test" if illustrative else None,
            "output_mode": "RESUME_AND_REPORT",
            "formats": ["DOCX", "PDF"],
            "max_repair_rounds": max_repair_rounds,
            "keyword_category_ids": [],
            "bullet_ceilings": [],
            "max_pages": 2,
        },
        "revision": None,
    }


def stage_of(instructions: str) -> str:
    for line in instructions.splitlines():
        if line.startswith("STAGE:"):
            return line.split(":", 1)[1].split("/")[0].strip().upper()
    raise ValueError("Stage prompt missing a 'STAGE:' line")
