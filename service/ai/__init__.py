"""AI provider switch. RTS_AI_PROVIDER=mock (default, free) or claude (needs ANTHROPIC_API_KEY)."""
from __future__ import annotations

from ..config import Settings


def make_generator(settings: Settings):
    """Return a fresh generate_json callable for one run (it records usage per call)."""
    if settings.ai_provider == "mock":
        from .mock import MockAI
        return MockAI(delay_seconds=settings.mock_delay)
    if settings.ai_provider == "claude":
        from .claude import make_claude_adapter
        return make_claude_adapter()
    raise ValueError(f"Unknown RTS_AI_PROVIDER: {settings.ai_provider!r} (use mock or claude)")
