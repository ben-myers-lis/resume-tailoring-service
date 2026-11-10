"""Compact output schemas for the fast pipeline. Small outputs are what make it fast and cheap."""
from __future__ import annotations

_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 12}
_HIGHLIGHTS = {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 3}
_TEXT_BLOCK = {"type": "object", "properties": {"text": _STR, "sources": _IDS, "highlights": _HIGHLIGHTS},
               "required": ["text", "sources", "highlights"], "additionalProperties": False}

DRAFT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "fast_draft",
    "type": "object",
    "properties": {
        "summary": _TEXT_BLOCK,
        "skills": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "required": ["items"], "additionalProperties": False,
            "properties": {"items": {"type": "array", "maxItems": 14, "items": {
                "type": "object", "required": ["text", "sources"], "additionalProperties": False,
                "properties": {"text": _STR, "sources": _IDS}}}}}},
        "roles": {"type": "array", "items": {
            "type": "object", "required": ["role_id", "scope", "bullets"], "additionalProperties": False,
            "properties": {"role_id": _STR, "scope": _TEXT_BLOCK,
                           "bullets": {"type": "array", "maxItems": 8, "items": _TEXT_BLOCK}}}},
        "gaps": {"type": "array", "items": _STR, "maxItems": 8},
        "questions": {"type": "array", "items": _STR, "maxItems": 5},
    },
    "required": ["summary", "skills", "roles", "gaps", "questions"],
    "additionalProperties": False,
}

CHECK = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "fast_check",
    "type": "object",
    "properties": {"problems": {"type": "array", "maxItems": 25, "items": {
        "type": "object", "required": ["item_id", "severity", "issue", "fix"], "additionalProperties": False,
        "properties": {"item_id": _STR, "severity": {"enum": ["BLOCKER", "MAJOR", "MINOR"]},
                       "issue": _STR, "fix": _STR}}}},
    "required": ["problems"],
    "additionalProperties": False,
}

FIX = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "fast_fix",
    "type": "object",
    "properties": {"fixes": {"type": "array", "maxItems": 40, "items": {
        "type": "object", "required": ["item_id", "text", "sources", "highlights"], "additionalProperties": False,
        "properties": {"item_id": _STR, "text": _STR, "sources": _IDS, "highlights": _HIGHLIGHTS}}}},
    "required": ["fixes"],
    "additionalProperties": False,
}
