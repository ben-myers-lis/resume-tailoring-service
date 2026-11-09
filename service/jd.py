"""Best-effort job title / company detection from a pasted, unstructured job description.

Only used for labels, file names and application records; people can correct it at any time.
The tailoring itself always reads the full pasted text.
"""
from __future__ import annotations

import re

LABELS_TITLE = r"(?:job\s*title|position|role|title)"
LABELS_COMPANY = r"(?:company(?:\s*name)?|employer|organi[sz]ation)"
TITLE_WORDS = re.compile(r"\b(engineer|developer|scientist|analyst|manager|director|lead|architect|designer|"
                         r"specialist|consultant|administrator|coordinator|officer|head|intern|researcher|"
                         r"associate|technician|representative|accountant|nurse|writer|strategist|owner)s?\b", re.I)
NOT_A_TITLE = re.compile(r"^(about|who we are|responsibilities|requirements|qualifications|what you|benefits|"
                         r"job description|overview|summary|location|salary)\b", re.I)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" \t-–—:|•*#")
    return s[:150]


def guess_title_company(text: str) -> tuple[str | None, str | None]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = "\n".join(lines[:15])
    title = company = None

    m = re.search(rf"^\s*{LABELS_TITLE}\s*[:\-–]\s*(.+)$", head, re.I | re.M)
    if m:
        title = _clean(m.group(1))
    m = re.search(rf"^\s*{LABELS_COMPANY}\s*[:\-–]\s*(.+)$", head, re.I | re.M)
    if m:
        company = _clean(m.group(1))

    if not title:  # "X is hiring a Senior Data Engineer to ..." / "We are looking for an ML Engineer who ..."
        m = re.search(r"(?:hiring|looking for|seeking|searching for)\s+(?:an?\s+|our\s+(?:next\s+|first\s+)?)?"
                      r"([A-Z][\w/&+.,()' -]{2,80}?)(?=\s+(?:to|who|that|with|for|in|at|on|based|join)\b|[.,;:\n(])", head)
        if m and TITLE_WORDS.search(m.group(1)):
            title = _clean(m.group(1))
    if not company:  # "Acme Corp is hiring ..." / "... at Acme Corp." / "Join Acme Corp"
        m = (re.search(r"^([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})\s+(?:is|are)\s+(?:hiring|looking|seeking)", head, re.M)
             or re.search(r"\b(?:at|join)\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})(?=[\s,.!;:]|$)", head))
        if m and m.group(1).lower() not in {"we", "our", "the", "you", "us"}:
            company = _clean(m.group(1))

    if not title:  # a short early line that looks like a job title, e.g. "Senior Backend Engineer (Remote)"
        for ln in lines[:6]:
            if len(ln) <= 90 and TITLE_WORDS.search(ln) and not NOT_A_TITLE.search(ln) and not ln.endswith("."):
                cand = re.split(r"\s+(?:at|@|-|–|—|\|)\s+", ln, maxsplit=1)
                title = _clean(cand[0])
                if not company and len(cand) > 1 and len(cand[1]) <= 60:
                    company = _clean(cand[1])
                nxt = lines[lines.index(ln) + 1] if lines.index(ln) + 1 < len(lines) else ""
                if (not company and nxt and len(nxt) <= 60 and not nxt.endswith(".") and ":" not in nxt
                        and not TITLE_WORDS.search(nxt) and not NOT_A_TITLE.search(nxt) and nxt[:1].isupper()
                        and "," not in nxt and not re.search(r"\b(remote|hybrid|on-?site|full[- ]time|part[- ]time|"
                                                              r"contract|usa?|uk|\$|salary)\b", nxt, re.I)):
                    company = _clean(nxt)        # company on the line under the title
                break
    if company:
        company = re.split(r"\.\s", company)[0].rstrip(".") or None   # stop at the end of a sentence
    return title, company
