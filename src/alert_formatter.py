"""Turn a raw Campflare webhook payload into a human-readable Discord message.

This is the "LLM last-mile" — we don't need the model to *decide* anything,
just to write nicer copy than an f-string. Using Claude Haiku with structured
output (Pydantic) keeps it cheap and reliable.

Lessons this file demonstrates:
  - ChatAnthropic via LangChain (tool-calling path)
  - `.with_structured_output(Pydantic)` for reliable JSON
  - ChatPromptTemplate with system/human split
  - Recording token usage + cost after every call
"""

from __future__ import annotations

import json

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from .cost_tracker import log_llm_call

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You are writing short, friendly alerts for a vanlifer who is
watching for campsite availability. Given a raw availability payload, write:

  - a 1-sentence summary ("Bear Head Lake opened up 3 weekday nights in July")
  - 2–4 bullet highlights (specific dates, nights, any standout fact)
  - an urgency signal: "low" (book within a week), "medium" (book within 48h),
    or "high" (book in the next few hours — rare/hot site)

Rules:
  - No emoji in the summary (Discord will add a header emoji separately).
  - Use short, specific dates like "Jul 8–10" not "the week of July 8th".
  - If the payload shows multiple windows, pick the best 2–3.
  - Never invent dates or campground names that aren't in the payload.
"""


class FormattedAlert(BaseModel):
    summary: str = Field(description="1-sentence summary, no emoji")
    highlights: list[str] = Field(description="2-4 short bullet points")
    urgency: str = Field(description="One of: low, medium, high")


def format_alert(payload: dict) -> FormattedAlert:
    """Call Haiku to structure the alert. Returns FormattedAlert or raises."""
    llm = ChatAnthropic(model=MODEL, temperature=0).with_structured_output(
        FormattedAlert, include_raw=True
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "Raw webhook payload:\n\n```json\n{payload_json}\n```"),
    ])
    chain = prompt | llm
    result = chain.invoke({"payload_json": json.dumps(payload, indent=2, default=str)})

    raw_msg = result["raw"]
    usage = getattr(raw_msg, "usage_metadata", None) or {}
    log_llm_call(
        model=MODEL,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cached_input_tokens=(
            usage.get("input_token_details", {}).get("cache_read", 0)
            if isinstance(usage.get("input_token_details"), dict)
            else 0
        ),
        purpose="format_alert",
    )
    return result["parsed"]


if __name__ == "__main__":
    # Quick local demo: `python -m src.alert_formatter`
    from dotenv import load_dotenv

    load_dotenv()
    demo_payload = {
        "alert_id": "alr_demo",
        "campground": {"id": "cg_bearhead", "name": "Bear Head Lake State Park"},
        "openings": [
            {"start_date": "2026-07-08", "end_date": "2026-07-10", "nights": 2,
             "campsite_kind": "standard"},
            {"start_date": "2026-07-14", "end_date": "2026-07-15", "nights": 1,
             "campsite_kind": "rv"},
        ],
    }
    out = format_alert(demo_payload)
    print(out.model_dump_json(indent=2))
