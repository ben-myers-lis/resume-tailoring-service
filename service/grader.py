"""Test-round grader: one fixed judge for every prompt version and pipeline.

It reads the finished resume against the candidate's inputs and the job, using the package's own
assessment rubric, and reports unsupported claims, requirements the inputs support but the resume
misses, and a 1–5 score. Its prompt isn't part of any prompt version (so a version can't grade
itself more kindly). It runs only for Prompt Studio test runs, never for live resumes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from jsonschema import Draft202012Validator

from .ai import make_generator
from .config import Settings
from .fast.runner import build_input
from .package import PACKAGE_DIR

log = logging.getLogger("rts.grader")
PROMPT = Path(__file__).resolve().parent / "fast" / "prompts" / "20_grade.txt"

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "grade",
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "maxItems": 30, "items": {
            "type": "object", "required": ["text", "reason"], "additionalProperties": False,
            "properties": {"text": {"type": "string"}, "reason": {"type": "string"}}}},
        "missed_requirements": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "summary": {"type": "string"},
    },
    "required": ["unsupported_claims", "missed_requirements", "score", "summary"],
    "additionalProperties": False,
}


def grade_run(settings: Settings, request: dict, result: dict) -> dict:
    """Never raises: a grading problem must not fail the test run itself."""
    if not result.get("resume_markdown"):
        return {"skipped": "No resume to grade (withheld by the safety checks)."}
    gen = make_generator(settings)
    try:
        inp = build_input(request)
        core = (PACKAGE_DIR / "prompts/00_core.txt").read_text(encoding="utf-8")
        data = {"profile": inp["profile"], "statements": inp["statements"], "jd": inp["jd"],
                "jd_terms": inp["jd_terms"], "resume": result["resume_markdown"],
                "assessment_rubric": (PACKAGE_DIR / "reference/assessment_rubric.txt").read_text(encoding="utf-8")}
        out = gen(instructions=core + "\n\n" + PROMPT.read_text(encoding="utf-8"), input_data=data,
                  output_schema=SCHEMA)
        errors = list(Draft202012Validator(SCHEMA).iter_errors(out))
        if errors:
            raise ValueError("; ".join(e.message for e in errors[:3]))
        return {**out, "usage": gen.usage_report()}
    except Exception as e:
        log.warning("grading failed: %s", e)
        usage = gen.usage_report() if hasattr(gen, "usage_report") else None
        return {"error": f"{type(e).__name__}: {e}", "usage": usage}
