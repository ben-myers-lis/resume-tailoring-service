"""Work-history helpers: conservative tenure (overlaps counted once) and title classification."""
from __future__ import annotations

import re
from datetime import date


def _parse(value: str | None, default: date) -> date:
    if not value:
        return default
    y, m = int(value[:4]), int(value[5:7])
    return date(y, m, 1)


def months_union(roles: list[dict], as_of: date | None = None) -> int:
    """Total months covered by roles, counting overlapping periods once."""
    as_of = as_of or date.today()
    spans = sorted((_parse(r["start_date"], as_of), _parse(r.get("end_date"), as_of)) for r in roles)
    total, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += _months(cur_s, cur_e)
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += _months(cur_s, cur_e)
    return total


def _months(s: date, e: date) -> int:
    return max(0, (e.year - s.year) * 12 + (e.month - s.month) + 1)


# Job families. Order matters: first match wins.
FAMILIES = [
    ("ml", r"machine learning|\bml\b|\bai\b|artificial intelligence|data scien|applied scien|research scien|"
           r"\bnlp\b|computer vision|deep learning|\bllm\b|mlops|perception"),
    ("software", r"software|backend|back-end|frontend|front-end|full[- ]stack|developer|site reliability|"
                 r"\bsre\b|devops|platform|infrastructure|cloud|engineer"),
    ("data", r"data analyst|analytics|business intelligence|\bbi\b|data engineer|analyst"),
]
ADJACENT = {("ml", "software"), ("software", "ml"), ("ml", "data"), ("data", "ml"),
            ("software", "data"), ("data", "software")}


def family(title: str) -> str:
    t = title.lower()
    for name, pattern in FAMILIES:
        if re.search(pattern, t):
            return name
    return "other"


LEVELS = [(5, r"director|head of|vp\b|vice president|chief"),
          (4, r"staff|principal|lead|manager|architect"),
          (3, r"senior|\bsr\.?\b|\biii\b"),
          (1, r"junior|\bjr\.?\b|associate|intern|graduate|entry")]


def level(title: str) -> int:
    t = title.lower()
    for value, pattern in LEVELS:
        if re.search(pattern, t):
            return value
    return 2
