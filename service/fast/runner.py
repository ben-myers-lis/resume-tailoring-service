"""Fast pipeline (v2): one writing call, instant code checks, a quick AI check, a targeted fix.

    FAST_DRAFT (Sonnet)  ->  code checks  ->  FAST_CHECK (Haiku, problems only)
                         ->  FAST_FIX (Sonnet, flagged items only, when needed)  ->  code checks again

Compared with the full pipeline (normalize -> draft -> assess -> repair -> re-assess), the model
writes about a fifth as much: the resume itself plus a source list per item, instead of a claim
ledger, coverage map and full audit. Output tokens drive both time and cost.

Safety: anything that still fails a check after the fix is removed (bullets, skills, scope lines)
or, for the summary, leaves the resume marked "Needs review". An unresolved BLOCKER withholds the
resume, as in the full pipeline. Only APPLICATION mode runs here; illustrative runs use the full
pipeline.

The result has the same shape as the full pipeline's, so rendering, the run page, Drive uploads
and Prompt Studio work unchanged.
"""
from __future__ import annotations

import copy
import re
from datetime import date

from jsonschema import Draft202012Validator

from ..history import months_union
from ..package import PACKAGE_VERSION, find_terms, runner as pkg
from . import schemas
from .checks import check, items_of, structure_problems

FILES = {"draft": "fast/10_draft.txt", "check": "fast/11_check.txt", "fix": "fast/12_fix.txt"}
CORE = "prompts/00_core.txt"
JD_MAX_CHARS = 15000
GENERIC = {"and", "built", "led", "used", "developed"}


class NotApplicable(Exception):
    """This request should use the full pipeline (e.g. illustrative mode)."""


class FastContractError(ValueError):
    """The model's output couldn't be used; nothing is delivered (like the package's ContractError)."""


# ---------- input ----------
def build_input(request: dict) -> dict:
    """The compact input every fast step sees. Name and contact details are never included."""
    cand = request["candidate"]
    roles = [{"role_id": r["role_id"], "title": r["official_title"], "company": r["employer"],
              "start": r["start_date"], "end": r["end_date"], "current": r["is_current"],
              "location": r.get("location")} for r in cand["employment"]]
    statements = []
    for s in request["sources"]:
        role_id, text = None, s["content"]
        for r in cand["employment"]:          # per-role notes are stored as "At <company> as <title>: ..."
            prefix = f"At {r['employer']} as {r['official_title']}:"
            if text.startswith(prefix):
                role_id, text = r["role_id"], text[len(prefix):].strip()
                break
        statements.append({"source_id": s["source_id"], "role_id": role_id, "text": text})
    as_of = date.fromisoformat(request["as_of_date"])
    months = months_union(cand["employment"], as_of)
    jd = request["jd"]
    return {
        "as_of_date": request["as_of_date"],
        "total_years": round(months / 12, 1),
        "profile": {"roles": roles,
                    "education": [{"id": f"EDU{i}", "text": t} for i, t in enumerate(cand["education"], 1)],
                    "certifications": [{"id": f"CERT{i}", "text": t} for i, t in enumerate(cand["certifications"], 1)]},
        "statements": statements,
        "jd": {"role": jd["role"], "company": jd.get("company"), "text": jd["text"][:JD_MAX_CHARS]},
        "jd_terms": find_terms(jd["text"])[:40],
    }


# ---------- model calls ----------
def _call(gen, files: dict, step: str, data: dict, schema: dict) -> dict:
    out = gen(instructions=files[CORE] + "\n\n" + files[FILES[step]], input_data=data, output_schema=schema)
    errors = list(Draft202012Validator(schema).iter_errors(out))
    if errors:
        raise FastContractError(f"{step}: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5]))
    return out


# ---------- editing the draft ----------
def _locate(draft: dict, item_id: str):
    """Return (container, key) for an item so it can be replaced or removed."""
    if item_id == "summary":
        return draft, "summary"
    m = re.fullmatch(r"(R\d+)\.(scope|b(\d+))", item_id)
    if m:
        role = next((r for r in draft["roles"] if r["role_id"] == m.group(1)), None)
        if role is None:
            return None, None
        if m.group(2) == "scope":
            return role, "scope"
        i = int(m.group(3)) - 1
        return (role["bullets"], i) if 0 <= i < len(role["bullets"]) else (None, None)
    m = re.fullmatch(r"skill\.(\d+)\.(\d+)", item_id)
    if m:
        g, i = int(m.group(1)) - 1, int(m.group(2)) - 1
        if 0 <= g < len(draft["skills"]) and 0 <= i < len(draft["skills"][g]["items"]):
            return draft["skills"][g]["items"], i
    return None, None


def _replace(draft: dict, item_id: str, text: str, sources: list, highlights: list) -> bool:
    box, key = _locate(draft, item_id)
    if box is None:
        return False
    if item_id.startswith("skill."):
        box[key] = {"text": text, "sources": sources}
    else:
        box[key] = {"text": text, "sources": sources, "highlights": highlights}
    return True


def _remove(draft: dict, item_ids: set[str]) -> None:
    """Blank the items. They're dropped when the result is assembled, so item IDs (which are
    positions) stay valid for the whole run."""
    for iid in item_ids:
        box, key = _locate(draft, iid)
        if box is None:
            continue
        if iid == "summary":
            continue                                   # never removed; it stays flagged instead
        if iid.startswith("skill."):
            box[key] = {"text": "", "sources": []}
        else:
            box[key] = {"text": "", "sources": [], "highlights": []}


# ---------- the pipeline ----------
def run_fast(request: dict, gen, files: dict, max_fix_rounds: int = 1) -> dict:
    if request["options"]["mode"] != "APPLICATION":
        raise NotApplicable("The fast pipeline runs APPLICATION mode only")
    pkg.validate_request(request)
    inp = build_input(request)

    # 1. Write. A draft with the wrong roles is retried once with the problem stated.
    draft = _call(gen, files, "draft", inp, schemas.DRAFT)
    problems = structure_problems(inp, draft)
    if problems:
        draft = _call(gen, files, "draft", {**inp, "fix_these_problems": problems}, schemas.DRAFT)
        problems = structure_problems(inp, draft)
        if problems:
            raise FastContractError("draft: " + "; ".join(problems))

    findings: list[dict] = []

    # 2. Code checks. Unsupported skills and bullets with no statement behind them are removed now.
    code = check(inp, draft)
    removed_now = {f["item_id"] for f in code if f["action"] == "remove"}
    for f in code:
        if f["action"] == "remove":
            findings.append({**f, "resolved": True, "resolution": "Removed"})
    _remove(draft, removed_now)
    code = check(inp, draft)            # blanked items are skipped

    # 3. Quick AI check: problems only.
    items = [{"item_id": i["item_id"], "text": i["text"], "sources": i["sources"]}
             for i in items_of(draft) if i["text"].strip()]
    known = {i["item_id"] for i in items}
    ai = _call(gen, files, "check", {"profile": inp["profile"], "statements": inp["statements"],
                                     "jd": {"role": inp["jd"]["role"], "company": inp["jd"]["company"]},
                                     "items": items}, schemas.CHECK)
    ai_problems = [{**p, "origin": "check"} for p in ai["problems"] if p["item_id"] in known]

    # 4. Targeted fix for MAJOR/BLOCKER items (MINOR notes ride along when an item is fixed anyway).
    open_code = [f for f in code if f["severity"] in ("MAJOR", "BLOCKER")]
    serious = {f["item_id"] for f in open_code} | {p["item_id"] for p in ai_problems if p["severity"] != "MINOR"}
    fixed: set[str] = set()
    for _ in range(max(0, max_fix_rounds)):
        if not serious:
            break
        current = {i["item_id"]: i for i in items_of(draft)}
        flagged = [{"item_id": iid, "text": current[iid]["text"], "sources": current[iid]["sources"],
                    "problems": [{"issue": f["issue"], "fix": f.get("fix", "")}
                                 for f in open_code + ai_problems if f["item_id"] == iid]}
                   for iid in sorted(serious) if iid in current]
        out = _call(gen, files, "fix", {"profile": inp["profile"], "statements": inp["statements"],
                                        "jd": inp["jd"], "flagged": flagged}, schemas.FIX)
        to_remove = set()
        for fx in out["fixes"]:
            if fx["item_id"] not in serious:
                continue                                 # only flagged items may change
            if fx["text"].strip():
                _replace(draft, fx["item_id"], fx["text"].strip(), fx["sources"], fx["highlights"])
            else:
                to_remove.add(fx["item_id"])
            fixed.add(fx["item_id"])
        for p in ai_problems:
            if p["item_id"] in fixed:
                p["resolved"], p["resolution"] = True, ("Removed" if p["item_id"] in to_remove else "Rewritten")
        _remove(draft, to_remove)
        break  # one round; a fixed item is re-checked by code below, not by the AI again

    # 5. Final code checks. Anything still failing is removed, except the summary (flagged instead).
    final = check(inp, draft)
    still = {f["item_id"] for f in final if f["severity"] in ("MAJOR", "BLOCKER")}
    unfixed_ai = {p["item_id"] for p in ai_problems if p["severity"] != "MINOR" and not p.get("resolved")}
    drop = (still | unfixed_ai) - {"summary"}
    for f in final:
        if f["item_id"] in drop:
            findings.append({**f, "resolved": True, "resolution": "Removed"})
        elif f["severity"] in ("MAJOR", "BLOCKER"):
            findings.append({**f, "resolved": False})
        else:
            findings.append({**f, "resolved": True, "resolution": "Noted"})
    for p in ai_problems:
        if p.get("resolved"):
            findings.append(p)
        elif p["item_id"] in drop:
            findings.append({**p, "resolved": True, "resolution": "Removed"})
        else:
            findings.append({**p, "resolved": p["severity"] == "MINOR"})
    for f in code:
        if f["item_id"] in fixed and f["severity"] in ("MAJOR", "BLOCKER") and f["item_id"] not in still:
            findings.append({**f, "resolved": True, "resolution": "Rewritten"})
    _remove(draft, drop)

    # 6. Assemble the result in the app's usual shape.
    package_draft = to_package_draft(request, inp, draft)
    for i, f in enumerate(findings, start=1):
        f.setdefault("finding_id", f"F{i}")
    blocking = any(not f["resolved"] and f["severity"] == "BLOCKER" for f in findings)
    open_major = any(not f["resolved"] and f["severity"] == "MAJOR" for f in findings)
    has_bullets = any(r["bullets"] for r in package_draft["roles"])
    status = ("REVIEW_NEEDED" if blocking or open_major
              else "READY" if has_bullets else "INCOMPLETE_EVIDENCE")
    assessment = {"pipeline": "fast", "verdict": status,
                  "findings": [{"finding_id": f["finding_id"], "severity": f["severity"], "issue": f["issue"],
                                "item_id": f["item_id"], "origin": f["origin"], "resolved": f["resolved"],
                                "resolution": f.get("resolution")} for f in findings]}
    return {
        "package_version": PACKAGE_VERSION, "pipeline": "fast",
        "draft": package_draft, "assessment": assessment,
        "delivery_status": status, "layout_status": "LAYOUT_UNVERIFIED",
        "resume_markdown": None if blocking else pkg.render_markdown(request, package_draft),
        "report": {"pipeline": "fast", "run_type": request["run_type"], "mode": "APPLICATION",
                   "profile_version": request["candidate"]["profile_version"],
                   "as_of_date": request["as_of_date"], "delivery_status": status,
                   "jd_terms": inp["jd_terms"], "gaps": package_draft["gaps"],
                   "questions": package_draft["confirmation_items"], "findings": assessment["findings"],
                   "fix_round": bool(fixed)},
    }


# ---------- conversion to the full pipeline's draft format ----------
def _highlights(text: str, phrases: list[str]) -> list[dict]:
    """Keep only valid highlights (exact, non-numeric, non-overlapping), using the package's rules."""
    kept: list[dict] = []
    for p in phrases:
        h = {"phrase": p, "occurrence": 1, "kind": "TECHNICAL"}
        if p.lower() in GENERIC:
            continue
        try:
            pkg.highlight_ranges({"text": text, "highlights": kept + [h]})
        except pkg.ContractError:
            continue
        kept.append(h)
    return kept


def to_package_draft(request: dict, inp: dict, fast: dict) -> dict:
    claims: list[dict] = []

    def claim(text, sources, role_id=None) -> list[str]:
        if not text.strip():
            return []
        cid = f"C{len(claims) + 1}"
        paths = sorted({("employment." + s) if re.fullmatch(r"R\d+", s) else
                        "education" if s.startswith("EDU") else "certifications"
                        for s in sources if not s.startswith("S")})
        claims.append({"claim_id": cid, "role_id": role_id, "text": text, "status": "SUPPORTED",
                       "fact_ids": [s for s in sources if s.startswith("S")], "profile_paths": paths,
                       "scope_note": "Fast pipeline: sources listed per item."})
        return [cid]

    def block(b, role_id=None):
        text = b["text"].strip()
        return {"text": text, "claim_ids": claim(text, b["sources"], role_id),
                "highlights": _highlights(text, b.get("highlights") or []) if text else []}

    roles = []
    for r in fast["roles"]:
        kept = [b for b in r["bullets"] if b["text"].strip()]          # removed items were blanked
        roles.append({"role_id": r["role_id"], "scope": block(r["scope"], r["role_id"]),
                      "bullets": [{**block(b, r["role_id"]), "bullet_id": f"{r['role_id']}-B{i:02d}", "metric_ids": []}
                                  for i, b in enumerate(kept, start=1)]})
    skills = []
    for group in fast["skills"]:
        kept = [s for s in group["items"] if s["text"].strip()]
        if kept:
            skills.append({"group_id": f"G{len(skills) + 1}",
                           "items": [{"text": s["text"], "claim_ids": claim(s["text"], s["sources"])} for s in kept]})
    has_bullets = any(r["bullets"] for r in roles)
    return {
        "package_version": PACKAGE_VERSION, "run_id": request["run_id"],
        "candidate_id": request["candidate"]["candidate_id"], "jd_id": request["jd"]["jd_id"],
        "revision_id": "fast-001", "mode": "APPLICATION",
        "status": "COMPLETE_DRAFT" if has_bullets else "INCOMPLETE_EVIDENCE",
        "notices": [], "summary": block(fast["summary"]), "skills": skills, "roles": roles,
        "claims": claims, "metrics": [], "requirement_coverage": [], "term_decisions": [], "role_plan": [],
        "gaps": [g for g in fast["gaps"] if g.strip()][:8], "assumptions": [], "technical_limits": [],
        "company_notes": [], "confirmation_items": [q for q in fast["questions"] if q.strip()][:5],
        "changes": [], "lineage": [],
    }


def copy_files_with_defaults(files: dict, defaults: dict) -> dict:
    out = copy.copy(defaults)
    out.update(files)
    return out
