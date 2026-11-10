"""Claude adapter for app.runner.run_pipeline.

Implements the package's generate_json(instructions, input_data, output_schema) contract
with the Anthropic API, using forced tool use for structured output.

Design choices
- Model per stage (see STAGE_MODELS). Sonnet drafts and repairs; Haiku normalizes.
  ASSESS defaults to Sonnet because it is the main safety gate; switch to Haiku to save cost
  once tests show it catches the same issues.
- Prompt caching: the core policy (identical for every stage and candidate) and the static
  references (rubric, keyword catalog) are sent first and marked cacheable. Per-run data follows.
- Privacy: candidate name and contact details are removed before sending. The runner renders
  them from the host's own copy of the request, so the resume is unaffected.
  (Free-text sources may still mention them; strip at ingestion if that matters.)
- Every call is validated against the canonical schema. On failure the stage is retried once
  with the validation errors, as docs/APPLICATION_INTEGRATION.md recommends.
- Token usage, cost and time are recorded per call in adapter.calls.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field

from jsonschema import Draft202012Validator, FormatChecker

STAGE_MODELS = {
    "NORMALIZE": "claude-haiku-5-5",
    "DRAFT": "claude-sonnet-5-5",
    "REVISION": "claude-sonnet-5-5",
    "ASSESS": "claude-sonnet-5-5",
    "REPAIR": "claude-sonnet-5-5",
    # Fast pipeline (service/fast): one writing call, a quick problems-only check, a targeted fix.
    "FAST_DRAFT": "claude-sonnet-5-5",
    "FAST_CHECK": "claude-haiku-5-5",
    "FAST_FIX": "claude-sonnet-5-5",
    # Test-round grader (same judge for every version, so comparisons are fair).
    "GRADE": "claude-sonnet-5-5",
}

# Output can be long: the draft carries a claim ledger, coverage map and metric register.
STAGE_MAX_TOKENS = {
    "NORMALIZE": 16000,
    "DRAFT": 32000,
    "REVISION": 32000,
    "ASSESS": 16000,
    "REPAIR": 32000,
    "FAST_DRAFT": 8000,
    "FAST_CHECK": 3000,
    "FAST_FIX": 4000,
    "GRADE": 3000,
}

# USD per million tokens (Nov 2026). Haiku rates apply to prompts up to 100K tokens.
PRICES = {
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_write": 5.00, "cache_read": 0.20},
}

STATIC_KEYS = ("assessment_rubric", "keyword_reference")  # identical across runs -> cacheable


def stage_of(instructions: str) -> str:
    for line in instructions.splitlines():
        if line.startswith("STAGE:"):
            return line.split(":", 1)[1].split("/")[0].strip().upper()
    raise ValueError("Stage prompt missing a 'STAGE:' line")


def split_instructions(instructions: str) -> tuple[str, str]:
    i = instructions.rfind("\n\nSTAGE:")
    return (instructions[:i], instructions[i + 2:]) if i > 0 else (instructions, "")


def redact(input_data: dict) -> dict:
    data = copy.deepcopy(input_data)
    cand = data.get("request", {}).get("candidate")
    if cand:
        cand["name"] = "[CANDIDATE NAME WITHHELD]"
        cand["contacts"] = []
    return data


def tool_schema(output_schema: dict) -> dict:
    return {k: v for k, v in output_schema.items() if k not in ("$schema", "$id")}


def cost(model: str, u: dict) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    return (u["input"] * p["in"] + u["output"] * p["out"]
            + u["cache_write"] * p["cache_write"] + u["cache_read"] * p["cache_read"]) / 1e6


@dataclass
class ClaudeAdapter:
    client: object                      # anthropic.Anthropic()
    stage_models: dict = field(default_factory=lambda: dict(STAGE_MODELS))
    redact_identity: bool = True
    force_tool: bool = True             # set False if your account rejects forced tool use
    calls: list = field(default_factory=list)

    def __call__(self, *, instructions: str, input_data: dict, output_schema: dict) -> dict:
        stage = stage_of(instructions)
        model = self.stage_models[stage]
        core, stage_prompt = split_instructions(instructions)
        data = redact(input_data) if self.redact_identity else copy.deepcopy(input_data)
        static = {k: data.pop(k) for k in STATIC_KEYS if k in data}

        system = [
            {"type": "text", "text": core, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": stage_prompt + "\n\nReturn the result only by calling the "
             "`submit_result` tool with an object that matches its schema."},
        ]
        content = []
        if static:
            content.append({"type": "text", "cache_control": {"type": "ephemeral"},
                            "text": "<reference_material>\n" + json.dumps(static, ensure_ascii=False)
                                    + "\n</reference_material>"})
        content.append({"type": "text", "text": "<input_data>\n" + json.dumps(data, ensure_ascii=False)
                        + "\n</input_data>"})

        validator = Draft202012Validator(output_schema, format_checker=FormatChecker())
        messages = [{"role": "user", "content": content}]
        for attempt in (1, 2):
            result, msg = self._request(stage, model, system, messages, output_schema)
            errors = list(validator.iter_errors(result))
            if not errors:
                return result
            if attempt == 2:
                return result  # runner raises ContractError with details; nothing is delivered
            detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:8])
            messages = messages + [
                {"role": "assistant", "content": msg.content},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": self._tool_id(msg), "is_error": True,
                     "content": f"Schema validation failed: {detail}. Call submit_result again "
                                "with a corrected complete object. Do not invent data to satisfy it."}]},
            ]

    def _request(self, stage, model, system, messages, output_schema):
        kwargs = dict(
            model=model,
            max_tokens=STAGE_MAX_TOKENS[stage],
            system=system,
            tools=[{"name": "submit_result", "description": f"Submit the {stage} output.",
                    "input_schema": tool_schema(output_schema)}],
            tool_choice={"type": "tool", "name": "submit_result"} if self.force_tool else {"type": "auto"},
            messages=messages,
        )
        t0 = time.time()
        with self.client.messages.stream(**kwargs) as stream:   # streaming: long outputs
            msg = stream.get_final_message()
        elapsed = time.time() - t0

        u = msg.usage
        usage = {"input": u.input_tokens, "output": u.output_tokens,
                 "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0}
        self.calls.append({"stage": stage, "model": model, "seconds": round(elapsed, 1),
                           "stop_reason": msg.stop_reason, **usage,
                           "cost_usd": round(cost(model, usage), 4)})
        if msg.stop_reason == "max_tokens":
            raise RuntimeError(f"{stage}: output hit max_tokens={STAGE_MAX_TOKENS[stage]}; raise the limit")
        block = next((b for b in msg.content if b.type == "tool_use"), None)
        if block is None:
            raise RuntimeError(f"{stage}: model did not call submit_result (stop_reason={msg.stop_reason})")
        return block.input, msg

    @staticmethod
    def _tool_id(msg):
        return next(b.id for b in msg.content if b.type == "tool_use")

    def usage_report(self) -> dict:
        total = {k: sum(c[k] for c in self.calls)
                 for k in ("input", "output", "cache_write", "cache_read", "seconds", "cost_usd")}
        total["cost_usd"] = round(total["cost_usd"], 4)
        total["seconds"] = round(total["seconds"], 1)
        return {"calls": self.calls, "total": total}
