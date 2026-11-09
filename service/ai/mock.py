"""Mock AI: a free, instant stand-in for Claude that honours the package's contracts.

It implements the same generate_json(instructions, input_data, output_schema) contract as the
Claude adapter, so the app runs the package's real pipeline and safety checks end to end.

Behaviour mirrors APPLICATION-mode rules: it writes only what the inputs support.
- Title-only candidates get a summary built from titles/dates and no experience bullets.
- Candidate statements (sources), if supplied, become facts and bullets quoting them.
- JD requirements are split from the JD text; terms come from the package's keyword catalog.
It never invents work. Text quality is placeholder: real wording comes from Claude.
"""
from __future__ import annotations

import copy
import re
import time
from datetime import date

from ..history import months_union
from ..package import PACKAGE_VERSION, find_terms, stage_of

LABELS = {
    "required": "REQUIRED", "requirements": "REQUIRED", "qualifications": "REQUIRED",
    "must have": "REQUIRED", "preferred": "PREFERRED", "nice to have": "PREFERRED",
    "bonus": "PREFERRED", "responsibilities": "RESPONSIBILITY", "what you will do": "RESPONSIBILITY",
}
MAX_REQUIREMENTS = 25


class MockAI:
    def __init__(self, delay_seconds: float = 0.0):
        self.delay = delay_seconds
        self.calls: list[dict] = []

    # ---- contract ----
    def __call__(self, *, instructions: str, input_data: dict, output_schema: dict) -> dict:
        stage = stage_of(instructions)
        t0 = time.time()
        if self.delay:
            time.sleep(self.delay)
        data = copy.deepcopy(input_data)
        handler = {"NORMALIZE": self._normalize, "DRAFT": self._draft, "REVISION": self._draft,
                   "ASSESS": self._assess, "REPAIR": self._repair}[stage]
        out = handler(data)
        self.calls.append({"stage": stage, "model": "mock", "seconds": round(time.time() - t0, 2),
                           "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost_usd": 0.0,
                           "stop_reason": "mock"})
        return out

    def usage_report(self) -> dict:
        return {"calls": self.calls,
                "total": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                          "seconds": round(sum(c["seconds"] for c in self.calls), 2), "cost_usd": 0.0}}

    # ---- helpers ----
    @staticmethod
    def _envelope(req: dict) -> dict:
        return {"package_version": PACKAGE_VERSION, "run_id": req["run_id"],
                "candidate_id": req["candidate"]["candidate_id"], "jd_id": req["jd"]["jd_id"]}

    @staticmethod
    def _requirements(jd_text: str) -> list[dict]:
        reqs, priority, alt = [], None, 0
        for m in re.finditer(r"[^.;:\n]+", jd_text):
            piece = m.group().strip().lstrip("-*•·– ").strip()     # pasted bullets: drop the marker
            key = piece.lower()
            if key in LABELS:
                priority = LABELS[key]
                continue
            if len(piece) < 3 or len(reqs) >= MAX_REQUIREMENTS:
                continue
            terms = find_terms(piece)
            prio = priority or ("REQUIRED" if terms or re.search(r"years|experience", key) else "RESPONSIBILITY")
            group = None
            if " or " in piece and len(terms) >= 2:
                alt += 1
                group = f"ALT{alt}"
            reqs.append({"requirement_id": f"Q{len(reqs) + 1}", "text": piece, "quote": piece,
                         "priority": prio, "technical_terms": terms, "alternative_group": group,
                         "threshold": None})
        return reqs

    @staticmethod
    def _facts(req: dict) -> list[dict]:
        roles = req["candidate"]["employment"]
        status = {"CANDIDATE_STATEMENT": "SUPPLIED", "CANDIDATE_DOCUMENT": "DOCUMENTED",
                  "UNCONFIRMED": "UNCONFIRMED"}
        facts = []
        for src in req["sources"]:
            role_id = next((r["role_id"] for r in roles
                            if r["employer"] and r["employer"].lower() in src["content"].lower()), None)
            for m in re.finditer(r"[^.!?]+[.!?]?", src["content"]):
                quote = m.group().strip()
                if len(quote) < 8:
                    continue
                facts.append({"fact_id": f"F{len(facts) + 1}", "role_id": role_id,
                              "fact_type": "OUTCOME" if re.search(r"\d", quote) else "PROJECT",
                              "statement": quote, "source_id": src["source_id"], "quote": quote,
                              "status": status[src["kind"]]})
        return facts

    # ---- stages ----
    def _normalize(self, data: dict) -> dict:
        req = data["request"]
        facts = self._facts(req)
        inventory = []
        for term in sorted({t for f in facts for t in find_terms(f["quote"])}):
            fids = [f["fact_id"] for f in facts if term in find_terms(f["quote"])]
            roles = {f["role_id"] for f in facts if f["fact_id"] in fids}
            inventory.append({"term": term, "role_id": roles.pop() if len(roles) == 1 else None,
                              "fact_ids": fids, "usage_status": "UNKNOWN"})
        gaps = [] if facts else ["Only job titles, employers and dates were supplied; no work statements."]
        return {**self._envelope(req), "profile_version": req["candidate"]["profile_version"],
                "facts": facts, "requirements": self._requirements(req["jd"]["text"]),
                "inventory": inventory, "company_context": [], "source_conflicts": [],
                "evidence_gaps": gaps}

    def _draft(self, data: dict) -> dict:
        req, norm = data["request"], data["normalized"]
        cand = req["candidate"]
        roles = cand["employment"]
        facts = norm["facts"]
        claims, skills_items = [], []

        def claim(text, role_id=None, fact_ids=(), paths=(), status="SUPPORTED"):
            cid = f"C{len(claims) + 1}"
            claims.append({"claim_id": cid, "role_id": role_id, "text": text, "status": status,
                           "fact_ids": list(fact_ids), "profile_paths": list(paths),
                           "scope_note": "Mock output: built only from supplied inputs."})
            return cid

        # Summary from structured history (titles, employers, dates, education).
        months = months_union(roles, date.fromisoformat(req["as_of_date"]))
        years = f"{months // 12}+ years" if months >= 24 else f"{months} months"  # conservative
        latest = roles[0] if roles else None
        paths = [f"employment.{r['role_id']}" for r in roles]
        summary_ids, summary_text = [], ""
        if latest:
            summary_text = (f"{latest['official_title']} with {years} of experience across "
                            f"{len(roles)} role{'s' if len(roles) != 1 else ''}, most recently at "
                            f"{latest['employer']}.")
            summary_ids.append(claim(summary_text, paths=paths))
        if cand["education"]:
            edu = f" Education: {cand['education'][0]}."
            summary_text += edu
            summary_ids.append(claim(edu.strip(), paths=["education"]))

        # Skills only from terms evidenced in candidate facts.
        inv_claims = {}
        for item in norm["inventory"]:
            cid = claim(item["term"], fact_ids=item["fact_ids"])
            inv_claims[item["term"]] = cid
            skills_items.append({"text": item["term"], "claim_ids": [cid]})
        skills = [{"group_id": f"G{i // 6 + 1}", "items": skills_items[i:i + 6]}
                  for i in range(0, len(skills_items), 6)]

        # Bullets only from facts tied to a specific employer.
        role_blocks, fact_to_bullet = [], {}
        for r in roles:
            bullets = []
            for f in [f for f in facts if f["role_id"] == r["role_id"] and f["status"] != "UNCONFIRMED"]:
                text = re.sub(r"^At .+? as .+?:\s*", "", f["quote"])   # employer label added at intake
                text = re.sub(r"^I\s+", "", text).strip()
                text = text[:1].upper() + text[1:]
                cid = claim(text, role_id=r["role_id"], fact_ids=[f["fact_id"]])
                bid = f"{r['role_id']}-B{len(bullets) + 1:02d}"
                fact_to_bullet[f["fact_id"]] = bid
                bullets.append({"bullet_id": bid, "text": text, "claim_ids": [cid],
                                "highlights": _highlight(text), "metric_ids": []})
            role_blocks.append({"role_id": r["role_id"],
                                "scope": {"text": "", "claim_ids": [], "highlights": []},
                                "bullets": bullets})

        coverage, gaps = [], []
        for q in norm["requirements"]:
            hits = [t for t in q["technical_terms"] if t in inv_claims]
            fids = sorted({fid for t in hits for i in norm["inventory"] if i["term"] == t for fid in i["fact_ids"]})
            bids = sorted({fact_to_bullet[f] for f in fids if f in fact_to_bullet})
            if hits:
                full = len(hits) == len(q["technical_terms"]) or q["alternative_group"]
                coverage.append({"requirement_id": q["requirement_id"], "status": "SUPPORTED",
                                 "extent": "FULL" if full else "PARTIAL", "fact_ids": fids, "bullet_ids": bids,
                                 "placement": "EXPERIENCE" if bids else "SKILLS_ONLY",
                                 "note": "Term appears in candidate statements."})
            else:
                coverage.append({"requirement_id": q["requirement_id"], "status": "MISSING", "extent": "NONE",
                                 "fact_ids": [], "bullet_ids": [], "placement": "OMITTED",
                                 "note": "No supplied evidence."})
                if q["priority"] == "REQUIRED":
                    gaps.append(f"No supplied evidence for: {q['text']}")
        terms = []
        for t in sorted({t for q in norm["requirements"] for t in q["technical_terms"]}):
            terms.append({"term": t, "decision": "INCLUDED" if t in inv_claims else "GAP",
                          "claim_ids": [inv_claims[t]] if t in inv_claims else [],
                          "note": "Evidenced in candidate statements." if t in inv_claims else "Not evidenced."})

        has_bullets = any(rb["bullets"] for rb in role_blocks)
        illustrative = req["options"]["mode"] == "ILLUSTRATIVE"
        questions = [f"What did you build, own or improve as {r['official_title']} at {r['employer']}?"
                     for r in roles if not any(rb["role_id"] == r["role_id"] and rb["bullets"] for rb in role_blocks)]
        missing_terms = [t["term"] for t in terms if t["decision"] == "GAP"][:3]
        if missing_terms:
            questions.append(f"Have you used {', '.join(missing_terms)}? If so, in which role and for what?")
        return {
            **self._envelope(req),
            "revision_id": "draft-001", "mode": req["options"]["mode"],
            "status": "ILLUSTRATIVE" if illustrative else ("COMPLETE_DRAFT" if has_bullets else "INCOMPLETE_EVIDENCE"),
            "notices": ["Illustrative resume draft"] if illustrative else [],
            "summary": {"text": summary_text, "claim_ids": summary_ids, "highlights": []},
            "skills": skills, "roles": role_blocks, "claims": claims, "metrics": [],
            "requirement_coverage": coverage, "term_decisions": terms,
            "role_plan": [{"role_id": r["role_id"], "business_overlap": "UNKNOWN",
                           "emphasis": "Title, employer and dates as supplied.",
                           "requirement_ids": sorted({c["requirement_id"] for c in coverage
                                                      if any(b.startswith(r["role_id"] + "-") for b in c["bullet_ids"])})}
                          for r in roles],
            "gaps": gaps[:10], "assumptions": [],
            "technical_limits": [] if has_bullets else ["No work statements supplied; experience bullets omitted."],
            "company_notes": ["Mock run: no company research performed."],
            "confirmation_items": questions[:5], "changes": [], "lineage": [],
        }

    def _assess(self, data: dict) -> dict:
        req, draft = data["request"], data["draft"]
        bullets = [b for r in draft["roles"] for b in r["bullets"]]
        claims = {c["claim_id"]: c for c in draft["claims"]}
        findings = [{"finding_id": f"A{i + 1}", "rule_id": "P05", "severity": "GAP", "location": "JD",
                     "issue": g, "evidence": "Not present in supplied inputs.",
                     "recommended_action": "Report as a gap; do not invent experience.", "resolved": False}
                    for i, g in enumerate(draft["gaps"])]
        audit = [{"bullet_id": b["bullet_id"], "action": "KEEP", "evidence_status": "SUPPORTED",
                  "fact_ids": sorted({f for c in b["claim_ids"] for f in claims[c]["fact_ids"]}),
                  "reason": "Bullet restates a supplied candidate statement.", "correction_or_question": "",
                  "disposition": "RETAINED", "target_bullet_ids": []} for b in bullets]
        if draft["mode"] == "ILLUSTRATIVE":
            verdict = "ILLUSTRATIVE"
        elif draft["status"] == "COMPLETE_DRAFT":
            verdict = "READY"
        else:
            verdict = "INCOMPLETE_EVIDENCE"
        sections = ["IDENTITY_HISTORY", "SUMMARY", "SKILLS", "METRICS", "BUSINESS_CONTEXT",
                    "TECHNICAL_COHERENCE", "JD_COVERAGE", "READABILITY", "LAYOUT"]
        checks = [{"section": s, "status": "UNVERIFIED" if s == "LAYOUT" else "PASS",
                   "note": "Mock assessment." if s != "LAYOUT" else "Checked by the renderer, not the model."}
                  for s in sections]
        return {**self._envelope(req), "revision_id": draft["revision_id"], "verdict": verdict,
                "findings": findings, "bullet_audit": audit, "section_checks": checks,
                "requirement_coverage": draft["requirement_coverage"], "term_decisions": draft["term_decisions"],
                "confirmation_items": draft["confirmation_items"], "layout_status": "LAYOUT_UNVERIFIED",
                "page_count": None, "summary": f"Mock assessment: {verdict}."}

    def _repair(self, data: dict) -> dict:
        draft = copy.deepcopy(data["draft"])
        n = int(draft["revision_id"].rsplit("-", 1)[-1]) + 1
        draft["revision_id"] = f"draft-{n:03d}"
        draft["changes"] = draft.get("changes", []) + ["Mock repair: no content changes."]
        return draft


def _highlight(text: str) -> list[dict]:
    """Bold the first catalog term in a bullet, using the exact casing in the text."""
    for term in find_terms(text):
        m = re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, flags=re.IGNORECASE)
        if m and m.group().lower() not in {"and", "built", "led", "used", "developed"}:
            return [{"phrase": m.group(), "occurrence": text[:m.start()].count(m.group()) + 1, "kind": "TECHNICAL"}]
    return []
