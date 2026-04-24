"""Log every external call (Campflare, Anthropic) to SQLite with an estimated cost.

Why this exists: the #1 way AI projects get expensive is that nobody tracks the
spend until the bill arrives. Two thin primitives:

  - `log_api_call(service, endpoint)` — context manager; records latency + count.
  - `log_llm_call(model, input_tokens, output_tokens, cached_input_tokens=0)` —
    records token counts and computes a dollar estimate using built-in rates.

Query the DB directly or call `print_summary()` for a quick rollup.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("COST_DB_PATH", "cost_tracker.db"))

# USD per 1M tokens. Update when Anthropic changes pricing.
# Source: https://www.anthropic.com/pricing
PRICING = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cached_input": 0.10},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cached_input": 0.30},
    "claude-opus-4-7":   {"input": 15.00, "output": 75.00, "cached_input": 1.50},
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            ts REAL NOT NULL,
            service TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            ok INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            ts REAL NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            purpose TEXT
        )
    """)
    return c


@contextlib.contextmanager
def log_api_call(service: str, endpoint: str):
    start = time.time()
    ok = 1
    try:
        yield
    except Exception:
        ok = 0
        raise
    finally:
        latency_ms = (time.time() - start) * 1000
        with _conn() as c:
            c.execute(
                "INSERT INTO api_calls VALUES (?, ?, ?, ?, ?)",
                (start, service, endpoint, latency_ms, ok),
            )


def log_llm_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    purpose: str | None = None,
) -> float:
    rates = PRICING.get(model)
    if rates is None:
        raise ValueError(f"Unknown model '{model}' — add it to PRICING")
    fresh_input = max(0, input_tokens - cached_input_tokens)
    cost = (
        fresh_input * rates["input"]
        + cached_input_tokens * rates["cached_input"]
        + output_tokens * rates["output"]
    ) / 1_000_000
    with _conn() as c:
        c.execute(
            "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), model, input_tokens, cached_input_tokens,
             output_tokens, cost, purpose),
        )
    return cost


def print_summary() -> None:
    with _conn() as c:
        llm_total = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls").fetchone()[0]
        llm_count = c.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        api_count = c.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        api_errors = c.execute("SELECT COUNT(*) FROM api_calls WHERE ok = 0").fetchone()[0]
    print(f"LLM calls: {llm_count}, total cost: ${llm_total:.4f}")
    print(f"API calls: {api_count} ({api_errors} errors)")


if __name__ == "__main__":
    print_summary()
