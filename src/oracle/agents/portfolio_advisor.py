"""Morning portfolio advisor — Step 20.5 (Maksim's request).

A single cheap LLM call that produces ONE-LINE advice for each holding in
the portfolio. Designed for the 07:00 Warsaw morning brief — not a deep
analysis like the evening digest, just a quick "что делать сегодня" per
position.

INPUT: portfolio dict with live P&L (from portfolio.get_portfolio_with_pnl).
OUTPUT: list of `PortfolioAdvice` items, one per holding.

Cost target: ~$0.02-0.05 per morning (gpt-4o-mini, ~2000 input tokens,
~800 output tokens). Cheaper than running the full evening_digest just to
get HOLD verdicts.

Gracefully no-ops if no LLM credentials — returns deterministic HOLD
advice for every position so the morning brief always has SOMETHING to
render.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from ..config import get_settings
from ..services.portfolio import format_portfolio_for_llm
from ..prompts.portfolio_advisor import SYSTEM_PROMPT

log = logging.getLogger(__name__)


# ============================================================================
# Output schemas
# ============================================================================


class PortfolioAdvice(BaseModel):
    """One-line advice for a single holding."""

    asset: str = Field(description="Asset label, e.g. 'CSPX', 'IB1T'")
    action: str = Field(
        description=(
            "One of: HOLD | TRIM | ADD | WATCH | REBALANCE | SELL. "
            "Pick HOLD when nothing notable today (most positions, most days)."
        )
    )
    advice_short: str = Field(
        description=(
            "1-sentence Russian advice for today. Imperative mood. "
            "Examples: "
            "'Базовый рост работает — не трогай', "
            "'Перегрев AI-чипов — готовь TRIM на +8%', "
            "'Risk-off растёт — добирай при просадке к $97'."
        )
    )


class PortfolioMorningAdvice(BaseModel):
    """Per-holding advice array."""

    advice: list[PortfolioAdvice] = Field(
        description="One PortfolioAdvice per holding in the input portfolio.",
    )


# ============================================================================
# Main entry point
# ============================================================================


async def generate_morning_portfolio_advice(
    portfolio: dict[str, Any],
) -> list[PortfolioAdvice]:
    """Single LLM call producing one advice line per holding.

    Returns a deterministic fallback (all HOLD) if no LLM creds.
    Returns empty list if portfolio has no holdings.
    """
    holdings = portfolio.get("holdings") or []
    if not holdings:
        return []

    # Fallback: no LLM → trivial HOLD for every holding
    from ..observability import has_llm_credentials  # noqa: PLC0415
    if not has_llm_credentials():
        log.info("portfolio_advisor: no LLM creds — returning trivial HOLD advice")
        return [
            PortfolioAdvice(
                asset=h.get("asset_label") or "?",
                action="HOLD",
                advice_short="Держи — нет анализа сегодня (LLM недоступен).",
            )
            for h in holdings
        ]

    try:
        from ..observability import get_openai_client, log_llm_usage  # noqa: PLC0415
        client = get_openai_client(agent="portfolio_advisor")
    except Exception as e:  # noqa: BLE001
        log.warning("portfolio_advisor: client init failed — fallback HOLD: %s", e)
        return [
            PortfolioAdvice(
                asset=h.get("asset_label") or "?",
                action="HOLD",
                advice_short="Держи — анализ временно недоступен.",
            )
            for h in holdings
        ]

    settings = get_settings()
    portfolio_blob = format_portfolio_for_llm(portfolio)

    user_msg = (
        f"=== Portfolio snapshot (this morning) ===\n"
        f"{portfolio_blob}\n\n"
        f"Generate one PortfolioAdvice per holding above. "
        f"Total {len(holdings)} advice items expected."
    )

    log.info(
        "portfolio_advisor: calling %s for %d holdings",
        settings.openai_model_light, len(holdings),
    )

    try:
        response = await client.beta.chat.completions.parse(
            model=settings.openai_model_light,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format=PortfolioMorningAdvice,
            temperature=0.3,  # not creative — disciplined advice
        )
    except Exception as e:  # noqa: BLE001
        log.error("portfolio_advisor: LLM call failed: %s — fallback HOLD", e)
        return [
            PortfolioAdvice(
                asset=h.get("asset_label") or "?",
                action="HOLD",
                advice_short="Держи — анализ упал, проверь логи.",
            )
            for h in holdings
        ]

    await log_llm_usage("portfolio_advisor", response)
    parsed = response.choices[0].message.parsed
    if not parsed or not parsed.advice:
        return [
            PortfolioAdvice(
                asset=h.get("asset_label") or "?",
                action="HOLD",
                advice_short="Держи — LLM вернул пусто, проверь логи.",
            )
            for h in holdings
        ]

    # Make sure every holding is covered (LLM might forget one). Auto-fill HOLD.
    covered = {a.asset.upper() for a in parsed.advice}
    for h in holdings:
        label = (h.get("asset_label") or "").upper()
        if label and label not in covered:
            parsed.advice.append(
                PortfolioAdvice(
                    asset=h["asset_label"],
                    action="HOLD",
                    advice_short="Держи — без триггеров.",
                )
            )

    return parsed.advice
