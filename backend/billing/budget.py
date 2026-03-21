"""Real-cost budget enforcement for Code LM's managed API key mode.

Pricing (Anthropic, 2025, per 1M tokens):
  claude-sonnet-4-6          input $3.00  output $15.00  cache_read $0.30  cache_write $3.75
  claude-haiku-4-5-20251001  input $0.80  output  $4.00  cache_read $0.08  cache_write $1.00
"""
from __future__ import annotations

# ── Pricing table ─────────────────────────────────────────────────────────────

_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input":        3.00,
        "output":      15.00,
        "cache_read":   0.30,   # 10% of input
        "cache_write":  3.75,   # 125% of input
    },
    "claude-haiku-4-5-20251001": {
        "input":        0.80,
        "output":       4.00,
        "cache_read":   0.08,
        "cache_write":  1.00,
    },
}

# ── Constants ─────────────────────────────────────────────────────────────────

# A task that was already running when balance hit $0 may still complete —
# the server will let the *current* LLM turn finish.  But once the cumulative
# overdraft exceeds this limit, no new tool rounds or requests are started.
OVERDRAFT_LIMIT_USD: float = -1.00

# When balance falls to or below 0 we switch to short-answer mode (fewer
# max_tokens) so the LLM wraps up quickly instead of spending another dollar.
FINISH_QUICKLY_MAX_TOKENS: int = 512


# ── Helpers ───────────────────────────────────────────────────────────────────

def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Return the USD cost for one Anthropic API call."""
    prices = _PRICING.get(model, _PRICING["claude-sonnet-4-6"])
    return (
        input_tokens        * prices["input"]        / 1_000_000
        + output_tokens     * prices["output"]       / 1_000_000
        + cache_read_tokens * prices["cache_read"]   / 1_000_000
        + cache_write_tokens * prices["cache_write"] / 1_000_000
    )


def can_start_task(balance_usd: float) -> bool:
    """True if a new task (or next tool round) is allowed to begin.

    Returns False only when we are already past the $1 overdraft floor.
    """
    return balance_usd > OVERDRAFT_LIMIT_USD


def should_finish_quickly(balance_usd: float) -> bool:
    """True when the balance is exhausted — tell the LLM to wrap up concisely."""
    return balance_usd <= 0.0
