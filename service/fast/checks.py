"""Deterministic checks for the fast pipeline: free, instant, and exact on hard facts.

They cover what code can judge reliably: every item cites real sources, evidence stays with its
own role, numbers appear in the cited sources, skills appear in the cited sources, and no other
employer's name leaks into a role. Judgement calls (overstated scope, implied ownership) are left
to the quick AI check that runs after these.
"""
from __future__ import annotations

import re
from datetime import date

STOP = {"and", "of", "the", "for", "in", "with", "to", "a", "an", "&", "on"}
NUMBER = re.compile(r"(?<![A-Za-z0-9])\$?\d[\d,]*(?:\.\d+)?")
WORD = re.compile(r"[A-Za-z][A-Za-z+#.]*")
MAX_WORDS = {"summary": 110, "bullet": 50, "scope": 40}


# ---------- items ----------
def items_of(draft: dict) -> list[dict]:
    """Flatten a fast draft into checkable items with stable IDs."""
    out = [{"item_id": "summary", "kind": "summary", "role_id": None, **_block(draft["summary"])}]
    for r in draft["roles"]:
        rid = r["role_id"]
        out.append({"item_id": f"{rid}.scope", "kind": "scope", "role_id": rid, **_block(r["scope"])})
        for i, b in enumerate(r["bullets"], start=1):
            out.append({"item_id": f"{rid}.b{i}", "kind": "bullet", "role_id": rid, **_block(b)})
    for g, group in enumerate(draft["skills"], start=1):
        for i, s in enumerate(group["items"], start=1):
            out.append({"item_id": f"skill.{g}.{i}", "kind": "skill", "role_id": None,
                        "text": s["text"], "sources": list(s["sources"]), "highlights": []})
    return out


def _block(b: dict) -> dict:
    return {"text": b["text"], "sources": list(b["sources"]), "highlights": list(b.get("highlights") or [])}


# ---------- source text ----------
def source_index(inp: dict) -> dict[str, dict]:
    """Source ID -> {text, role_id, kind} for everything an item may cite."""
    idx = {}
    for r in inp["profile"]["roles"]:
        idx[r["role_id"]] = {"kind": "role", "role_id": r["role_id"],
                             "text": f"{r['title']} {r['company']} {r['start']} {r['end'] or 'present'} {r.get('location') or ''}"}
    for e in inp["profile"]["education"]:
        idx[e["id"]] = {"kind": "education", "role_id": None, "text": e["text"]}
    for c in inp["profile"]["certifications"]:
        idx[c["id"]] = {"kind": "certification", "role_id": None, "text": c["text"]}
    for s in inp["statements"]:
        idx[s["source_id"]] = {"kind": "statement", "role_id": s["role_id"], "text": s["text"]}
    return idx


def _years(start: str, end: str | None, as_of: date) -> float:
    def parse(v):
        if not v:
            return as_of
        y, m = int(v[:4]), int(v[5:7]) if len(v) >= 7 else 1
        return date(y, m, 1)
    s, e = parse(start), parse(end)
    return max(0.0, (e.year - s.year) + (e.month - s.month) / 12)


def allowed_numbers(inp: dict, cited_text: str) -> set[str]:
    """Numbers an item may contain: those in its cited sources, plus years of experience from dates."""
    allowed = {_norm_num(m.group()) for m in NUMBER.finditer(cited_text)}
    as_of = date.fromisoformat(inp["as_of_date"])
    roles = inp["profile"]["roles"]
    spans = [_years(r["start"], r["end"], as_of) for r in roles]
    total = inp["total_years"]
    for y in spans + [total]:
        allowed |= {str(int(y)), str(round(y))}
    allowed.add(str(len(roles)))
    return allowed


def _norm_num(s: str) -> str:
    s = s.replace("$", "").replace(",", "")
    return s[:-2] if s.endswith(".0") else s


def _stem(w: str) -> str:
    w = w.lower()
    return w[:5] if len(w) > 5 else w


def _supported_skill(skill: str, texts: list[str]) -> bool:
    """A skill is supported if it appears in a cited text, or (for titles) its words match by stem."""
    low = skill.lower().strip()
    if not low:
        return False
    for t in texts:
        if re.search(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])", t.lower()):
            return True
    words = [w for w in WORD.findall(low) if w not in STOP]
    if not words:
        return False
    stems = {_stem(w) for t in texts for w in WORD.findall(t)}
    return all(_stem(w) in stems for w in words)


# ---------- checks ----------
def check(inp: dict, draft: dict) -> list[dict]:
    """Findings: {item_id, severity, issue, origin='code', action='fix'|'remove'}."""
    idx = source_index(inp)
    roles = {r["role_id"]: r for r in inp["profile"]["roles"]}
    out = []

    def add(item, severity, issue, action="fix"):
        out.append({"item_id": item["item_id"], "severity": severity, "issue": issue, "origin": "code",
                    "action": action})

    for item in items_of(draft):
        text = item["text"].strip()
        if not text:
            continue
        unknown = [s for s in item["sources"] if s not in idx]
        known = [s for s in item["sources"] if s in idx]
        if unknown:
            add(item, "MAJOR", f"Cites unknown sources: {', '.join(unknown)}")
        if not known:
            add(item, "MAJOR", "Doesn't cite any source", "remove" if item["kind"] in ("skill", "bullet") else "fix")
            continue
        if item["role_id"]:
            moved = [s for s in known if idx[s]["role_id"] not in (None, item["role_id"])]
            if moved:
                add(item, "MAJOR", f"Uses evidence from another role ({', '.join(moved)})")
        if item["kind"] == "bullet" and not any(idx[s]["kind"] == "statement" for s in known):
            add(item, "MAJOR", "Experience bullet without any candidate statement behind it", "remove")
            continue
        cited = " ".join(idx[s]["text"] for s in known)
        if item["kind"] == "skill":
            if not _supported_skill(text, [idx[s]["text"] for s in known]):
                add(item, "MAJOR", f"Skill '{text}' isn't in the cited sources", "remove")
            continue
        allowed = allowed_numbers(inp, cited)
        bad = sorted({_norm_num(m.group()) for m in NUMBER.finditer(text)} - allowed)
        if bad:
            add(item, "MAJOR", f"Number(s) not in the cited sources: {', '.join(bad)}")
        if item["role_id"]:
            own = cited.lower()
            for rid, r in roles.items():
                name = r["company"].strip()
                if rid != item["role_id"] and len(name) >= 3 and name.lower() not in own and \
                        re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", text, re.I):
                    add(item, "MAJOR", f"Mentions {name}, another employer")
        limit = MAX_WORDS.get(item["kind"])
        if limit and len(text.split()) > limit:
            out.append({"item_id": item["item_id"], "severity": "MINOR", "origin": "code", "action": "none",
                        "issue": f"Long: {len(text.split())} words (aim for under {limit})"})
    return out


def structure_problems(inp: dict, draft: dict) -> list[str]:
    """Problems that make the draft unusable (the step is retried once, then the run fails)."""
    want = [r["role_id"] for r in inp["profile"]["roles"]]
    got = [r["role_id"] for r in draft["roles"]]
    problems = []
    if got != want:
        problems.append(f"roles must list exactly {want} in this order (got {got})")
    if not draft["summary"]["text"].strip():
        problems.append("summary is empty")
    return problems
