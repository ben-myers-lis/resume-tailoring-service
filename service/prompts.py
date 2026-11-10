"""Prompt versions: editable files, protected rules, checks, diffs, and running a run on a chosen version.

What can change from the app: prompt text, the assessment rubric and the keyword catalog.
What can't: output schemas and the package's contract checks (code). A badly edited prompt can
therefore make resumes worse, but it can't make the app deliver output that fails the checks.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re

from pathlib import Path

from .package import PACKAGE_DIR, stage_of

FAST_DIR = Path(__file__).resolve().parent / "fast" / "prompts"
PIPELINE_FILE = "settings/pipeline.txt"
PIPELINES = {"full": "Full (v1: 4 AI steps with full audit, ~3–6 min)",
             "fast": "Fast (v2: 1 writing step + quick check, ~11–25 s)"}

EDITABLE = [
    "prompts/00_core.txt",
    "prompts/01_normalize.txt",
    "prompts/02_draft.txt",
    "prompts/03_assess.txt",
    "prompts/04_repair.txt",
    "prompts/05_revision.txt",
    "prompts/07_prompt_maintenance.txt",
    "reference/assessment_rubric.txt",
    "reference/ai_keyword_catalog.json",
    PIPELINE_FILE,
    "fast/10_draft.txt",
    "fast/11_check.txt",
    "fast/12_fix.txt",
]
FILE_LABELS = {
    "prompts/00_core.txt": "Core policy (used in every step)",
    "prompts/01_normalize.txt": "Step 1: extract facts and job requirements",
    "prompts/02_draft.txt": "Step 2: write the tailored resume",
    "prompts/03_assess.txt": "Step 3: fact-check and assess",
    "prompts/04_repair.txt": "Step 4: repair flagged problems",
    "prompts/05_revision.txt": "Revisions requested by a user",
    "prompts/07_prompt_maintenance.txt": "Prompt maintenance (used by Improve with AI)",
    "reference/assessment_rubric.txt": "Assessment rubric",
    "reference/ai_keyword_catalog.json": "Keyword catalog",
    PIPELINE_FILE: "Pipeline: 'full' (v1) or 'fast' (v2)",
    "fast/10_draft.txt": "Fast v2: write the tailored resume",
    "fast/11_check.txt": "Fast v2: quick fact-check (problems only)",
    "fast/12_fix.txt": "Fast v2: fix flagged items",
}
FAST_STAGE_FILES = {"FAST_DRAFT": "fast/10_draft.txt", "FAST_CHECK": "fast/11_check.txt",
                    "FAST_FIX": "fast/12_fix.txt"}
STAGE_FILES = {
    "NORMALIZE": "prompts/01_normalize.txt", "DRAFT": "prompts/02_draft.txt", "ASSESS": "prompts/03_assess.txt",
    "REPAIR": "prompts/04_repair.txt", "REVISION": "prompts/05_revision.txt",
}
CORE = "prompts/00_core.txt"
PROTECTED_RULES = {
    "P02": "Portable context and fresh starts",
    "P03": "Evidence and claims (no invented experience)",
    "P04": "Illustrative mode labelling",
    "P06": "History, tenure and identity",
    "P12": "Metrics",
}
PROTECTED_FILES = {"prompts/03_assess.txt": "Fact-check and assessment step",
                   "fast/11_check.txt": "Fast v2 fact-check step"}
RULE_HEAD = re.compile(r"^(P\d\d) — ", re.M)


def default_file(f: str) -> str:
    if f == PIPELINE_FILE:
        return "full\n"
    if f.startswith("fast/"):
        return (FAST_DIR / f.split("/", 1)[1]).read_text(encoding="utf-8")
    return (PACKAGE_DIR / f).read_text(encoding="utf-8")


def package_files() -> dict[str, str]:
    """The files on disk: your v1 package, plus the fast v2 prompts (pipeline 'full')."""
    return {f: default_file(f) for f in EDITABLE}


def with_defaults(files: dict[str, str]) -> dict[str, str]:
    """Fill in files added by an app update (e.g. the fast pipeline's prompts) for older versions."""
    missing = [f for f in EDITABLE if f not in files]
    return {**files, **{f: default_file(f) for f in missing}} if missing else files


def pipeline_of(files: dict[str, str]) -> str:
    value = (files.get(PIPELINE_FILE) or "full").strip().lower()
    return value if value in PIPELINES else "full"


def content_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for f in EDITABLE:
        h.update(f.encode() + b"\0" + files.get(f, "").encode() + b"\0")
    return h.hexdigest()[:16]


def rule_blocks(core: str) -> dict[str, str]:
    heads = list(RULE_HEAD.finditer(core))
    return {m.group(1): core[m.start(): heads[i + 1].start() if i + 1 < len(heads) else len(core)].strip()
            for i, m in enumerate(heads)}


def protected_changes(base: dict[str, str], files: dict[str, str]) -> list[dict]:
    """Protected rules/files that differ from `base` (normally the active version)."""
    out = []
    old_rules, new_rules = rule_blocks(base.get(CORE, "")), rule_blocks(files.get(CORE, ""))
    for rid, label in PROTECTED_RULES.items():
        if old_rules.get(rid) != new_rules.get(rid):
            what = "removed" if rid not in new_rules else "changed"
            out.append({"kind": "rule", "id": rid, "label": f"{rid} {label}", "change": what})
    for f, label in PROTECTED_FILES.items():
        if base.get(f) != files.get(f):
            out.append({"kind": "file", "id": f, "label": label, "change": "changed"})
    return out


def edit_touches_protected(file: str, find: str, core_text: str) -> list[str]:
    """Which protected items a find/replace edit would touch."""
    hits = []
    if file in PROTECTED_FILES:
        hits.append(PROTECTED_FILES[file])
    if file == CORE:
        pos = core_text.find(find)
        if pos >= 0:
            for rid, block in rule_blocks(core_text).items():
                start = core_text.find(block)
                if rid in PROTECTED_RULES and start <= pos + len(find) and pos <= start + len(block):
                    hits.append(f"{rid} {PROTECTED_RULES[rid]}")
    return hits


def structural_problems(files: dict[str, str]) -> list[str]:
    problems = []
    for f in EDITABLE:
        if not files.get(f, "").strip():
            problems.append(f"{f} is empty or missing")
    for stage, f in {**STAGE_FILES, **FAST_STAGE_FILES}.items():
        first = files.get(f, "").lstrip().splitlines()[:1]
        if not first or first[0].upper().split("/")[0].strip() != f"STAGE: {stage}":
            problems.append(f"{f} must start with the line 'STAGE: {stage}'")
    if (files.get(PIPELINE_FILE) or "").strip().lower() not in PIPELINES:
        problems.append(f"{PIPELINE_FILE} must contain 'full' or 'fast'")
    if "prompts/07_prompt_maintenance.txt" in files and not files["prompts/07_prompt_maintenance.txt"].lstrip().startswith("STAGE:"):
        problems.append("prompts/07_prompt_maintenance.txt must start with a 'STAGE:' line")
    missing = [r for r in PROTECTED_RULES if r not in rule_blocks(files.get(CORE, ""))]
    if missing:
        problems.append("Core policy is missing protected rule(s): " + ", ".join(missing))
    try:
        cat = json.loads(files.get("reference/ai_keyword_catalog.json", ""))
        cats = cat["categories"]
        assert isinstance(cats, list) and cats
        for c in cats:
            assert isinstance(c["id"], str) and isinstance(c["name"], str)
            for g in c["groups"]:
                assert isinstance(g["terms"], list) and all(isinstance(t, str) for t in g["terms"])
    except (ValueError, KeyError, TypeError, AssertionError):
        problems.append("Keyword catalog must be valid JSON: {categories: [{id, name, groups: [{name, terms: [...]}]}]}")
    return problems


def diff(base: dict[str, str], files: dict[str, str], context: int = 3) -> list[dict]:
    """Per changed file: line operations with a little surrounding context."""
    out = []
    for f in EDITABLE:
        a, b = base.get(f, "").splitlines(), files.get(f, "").splitlines()
        if a == b:
            continue
        ops = []
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for group in sm.get_grouped_opcodes(context):
            ops.append({"type": "gap"})
            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    ops += [{"type": "same", "text": t} for t in a[i1:i2]]
                else:
                    ops += [{"type": "del", "text": t} for t in a[i1:i2]]
                    ops += [{"type": "add", "text": t} for t in b[j1:j2]]
        added = sum(1 for o in ops if o["type"] == "add")
        removed = sum(1 for o in ops if o["type"] == "del")
        out.append({"file": f, "label": FILE_LABELS[f], "added": added, "removed": removed, "ops": ops})
    return out


def apply_edit(files: dict[str, str], edit: dict) -> dict[str, str]:
    """Apply one find/replace edit. `find` must occur exactly once."""
    f, find, replace = edit["file"], edit["find"], edit["replace"]
    if f not in EDITABLE:
        raise ValueError(f"{f} can't be edited from the app")
    text = files[f]
    n = text.count(find) if find else 0
    if n != 1:
        raise ValueError(f"The text to replace was found {n} times in {f}; it must appear exactly once")
    new = dict(files)
    new[f] = text.replace(find, replace, 1)
    return new


class VersionedGenerator:
    """Wraps a generator (mock or Claude) so each step uses a chosen version's prompts.

    The package runner builds instructions from the files on disk; this swaps in the version's
    core policy, step prompt, rubric and keyword catalog. Output schemas are left untouched.
    """

    def __init__(self, inner, files: dict[str, str]):
        self.inner, self.files = inner, files
        self.catalog = json.loads(files["reference/ai_keyword_catalog.json"])

    def __call__(self, *, instructions: str, input_data: dict, output_schema: dict) -> dict:
        stage = stage_of(instructions)
        new_instructions = self.files[CORE] + "\n\n" + self.files[STAGE_FILES[stage]]
        data = copy.copy(input_data)
        if "assessment_rubric" in data:
            data["assessment_rubric"] = self.files["reference/assessment_rubric.txt"]
        if "keyword_reference" in data:
            catalog = copy.deepcopy(self.catalog)
            selected = set(data["request"]["options"]["keyword_category_ids"])
            if selected:
                catalog["categories"] = [c for c in catalog["categories"] if c["id"] in selected]
            data["keyword_reference"] = catalog
        return self.inner(instructions=new_instructions, input_data=data, output_schema=output_schema)

    def usage_report(self) -> dict:
        return self.inner.usage_report()
