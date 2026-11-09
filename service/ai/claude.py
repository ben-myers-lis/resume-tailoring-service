"""Claude provider: reuses the adapter in prompt_package/app/claude_adapter.py.

Models per stage, caching, identity redaction, schema validation with one retry, streaming,
and cost/time logging are all configured there (STAGE_MODELS, STAGE_MAX_TOKENS, PRICES).
"""
from __future__ import annotations

import os
from functools import lru_cache

from ..package import PACKAGE_DIR  # noqa: F401  (ensures prompt_package is importable)


@lru_cache(maxsize=1)
def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("RTS_AI_PROVIDER=claude needs ANTHROPIC_API_KEY (see README).")
    import anthropic
    return anthropic.Anthropic()


def make_claude_adapter():
    from app.claude_adapter import ClaudeAdapter  # the package's adapter
    force = os.environ.get("RTS_FORCE_TOOL", "1") != "0"
    return ClaudeAdapter(client=_client(), force_tool=force)
