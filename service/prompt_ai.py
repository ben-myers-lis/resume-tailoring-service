"""Improve with AI: turn plain-language feedback into proposed prompt edits.

Claude receives the core policy + the package's prompt-maintenance prompt, the feedback, the current
files, and (if available) resumes from the latest test round. It returns small find/replace edits that
a person accepts or rejects one by one. Nothing is applied automatically.
"""
from __future__ import annotations

import os
import time

from .prompts import CORE, EDITABLE, FILE_LABELS, edit_touches_protected

PROPOSE_TOOL = {
    "name": "submit_prompt_edits",
    "description": "Submit proposed edits to the prompt package.",
    "input_schema": {
        "type": "object",
        "required": ["summary", "edits", "warnings", "needs_code_change"],
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences: what you changed and why"},
            "edits": {"type": "array", "maxItems": 12, "items": {
                "type": "object", "required": ["file", "find", "replace", "reason"],
                "properties": {
                    "file": {"type": "string", "enum": EDITABLE},
                    "find": {"type": "string", "description": "Exact text copied verbatim from the current file; must appear exactly once"},
                    "replace": {"type": "string", "description": "Replacement text"},
                    "reason": {"type": "string"}}}},
            "warnings": {"type": "array", "items": {"type": "string"},
                         "description": "Risks, conflicts with protected rules, or parts of the request not adopted"},
            "needs_code_change": {"type": "array", "items": {"type": "string"},
                                  "description": "Requested changes that need schema/validator/code changes instead of prompt text"},
        },
    },
}

APP_RULES = """APP CONTEXT
You are improving the prompt package of a resume tailoring web app. A person will review each edit you propose and accept or reject it. Then the new version is tested on sample candidates before an administrator publishes it.

OUTPUT RULES
- Return edits only by calling `submit_prompt_edits`. Each edit is a find/replace on one file. `find` must be copied verbatim from the current file and appear exactly once (use a whole sentence or line).
- Make the smallest consistent change. Replace contradicting text rather than appending a competing instruction.
- Editable files: prompt text, the assessment rubric and the keyword catalog (JSON; keep it valid). Output schemas, validators and code cannot be edited; list such needs in `needs_code_change` instead of approximating them in prompt text.
- Protected rules: P02, P03, P04, P06, P12 in the core policy, and the assessment prompt (03). Do not weaken evidence, identity, tenure, metric or labelling rules. If the feedback asks for something that would require it (for example inventing experience, inflating numbers or stretching titles), do not make that change; explain in `warnings`.
- Every STAGE prompt must keep its first line ('STAGE: ...').
- Feedback about one candidate is evidence about the prompts, not a fact to add to any resume.
"""


def _files_block(files: dict[str, str]) -> str:
    return "\n\n".join(f'<file path="{f}" label="{FILE_LABELS[f]}">\n{files[f]}\n</file>' for f in EDITABLE)


def _examples_block(examples: list[dict]) -> str:
    if not examples:
        return ""
    parts = [f'<example candidate="{e["candidate"]}" job="{e["job"]}" status="{e["status"]}">\n{e["resume"]}\n</example>'
             for e in examples]
    return "<recent_test_resumes>\n" + "\n\n".join(parts) + "\n</recent_test_resumes>\n\n"


def propose(files: dict[str, str], instruction: str, provider: str, examples: list[dict] | None = None) -> dict:
    result = _claude(files, instruction, examples or []) if provider == "claude" else _mock(files, instruction)
    result["edits"] = [check_edit(files, e) for e in result["edits"]]
    return result


def check_edit(files: dict[str, str], e: dict) -> dict:
    e = {**e, "status": "PENDING", "problem": None}
    if e["file"] not in EDITABLE:
        e.update(status="INVALID", problem="File can't be edited from the app")
        return e
    n = files[e["file"]].count(e["find"]) if e["find"] else 0
    if n != 1:
        e.update(status="INVALID", problem=f"Text to replace found {n} times (must be exactly once)")
    e["protected"] = edit_touches_protected(e["file"], e["find"], files[CORE])
    return e


def _mock(files: dict[str, str], instruction: str) -> dict:
    """Free stand-in: adds the feedback as a clearly labelled guidance line to the most relevant step."""
    low = instruction.lower()
    target = ("prompts/03_assess.txt" if any(w in low for w in ("assess", "fact-check", "check", "audit"))
              else "prompts/01_normalize.txt" if any(w in low for w in ("extract", "requirement", "normaliz"))
              else "prompts/02_draft.txt")
    lines = [ln for ln in files[target].splitlines() if ln.strip()]
    last = lines[-1]
    note = instruction.strip().replace("\n", " ")
    return {
        "summary": "Mock proposal (no AI used): adds your feedback as a guidance line to the most relevant step. "
                   "Switch to Claude for real, targeted edits.",
        "edits": [{"file": target, "find": last, "replace": f"{last}\nAdditional guidance: {note}",
                   "reason": "Mock: records the feedback as an explicit instruction."}],
        "warnings": ["This proposal was made by the mock AI and does not analyse your prompts."],
        "needs_code_change": [], "provider": "mock", "cost_usd": 0.0, "seconds": 0.0,
    }


def _claude(files: dict[str, str], instruction: str, examples: list[dict]) -> dict:
    from .ai.claude import _client
    from app.claude_adapter import PRICES, cost  # the package's adapter (pricing table)

    model = os.environ.get("RTS_PROMPT_MODEL", "claude-opus-5-5")
    system = [
        {"type": "text", "text": files[CORE] + "\n\n" + files["prompts/07_prompt_maintenance.txt"],
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": APP_RULES},
    ]
    content = [
        {"type": "text", "cache_control": {"type": "ephemeral"},
         "text": "<current_files>\n" + _files_block(files) + "\n</current_files>"},
        {"type": "text", "text": _examples_block(examples) + f"<feedback>\n{instruction}\n</feedback>"},
    ]
    t0 = time.time()
    with _client().messages.stream(model=model, max_tokens=16000, system=system, tools=[PROPOSE_TOOL],
                                   tool_choice={"type": "tool", "name": "submit_prompt_edits"},
                                   messages=[{"role": "user", "content": content}]) as stream:
        msg = stream.get_final_message()
    block = next((b for b in msg.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError(f"The AI did not return edits (stop_reason={msg.stop_reason})")
    u = msg.usage
    usage = {"input": u.input_tokens, "output": u.output_tokens,
             "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
             "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0}
    out = dict(block.input)
    out.setdefault("warnings", [])
    out.setdefault("needs_code_change", [])
    out.update(provider=f"claude:{model}", cost_usd=round(cost(model, usage) if model in PRICES else 0.0, 4),
               seconds=round(time.time() - t0, 1))
    return out
