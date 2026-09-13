"""/morning — manual trigger mirroring the scheduled 07:00 Warsaw job."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from ...agents.market import collect_market_data
from ...db import get_db

log = logging.getLogger(__name__)


async def cmd_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the 07:00 Warsaw morning brief.

    Mirrors `scheduler.morning_brief_job` but writes to the user who typed
    /morning (so testing doesn't require the scheduled chat_id). Two
    messages: 🚨 СРОЧНО + 💼 совет по портфелю.
    """
    from ...agents.portfolio_advisor import generate_morning_portfolio_advice  # noqa: PLC0415
    from ...services.alerts import (  # noqa: PLC0415
        get_active_alerts_no_dedup,
        get_recently_fired_alerts,
    )
    from ...services.portfolio import get_portfolio_with_pnl  # noqa: PLC0415
    from ..views import (  # noqa: PLC0415
        render_portfolio_morning_advice,
        render_urgent_section,
    )

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        market_data, _ = await collect_market_data()
    except Exception as e:  # noqa: BLE001
        log.error("/morning: market fetch failed: %s", e)
        market_data = {}

    try:
        active = await get_active_alerts_no_dedup(market_data)
        recent = await get_recently_fired_alerts(hours=12)
    except Exception as e:  # noqa: BLE001
        log.error("/morning: alerts read failed: %s", e)
        active, recent = [], []

    # Top breaking news from last 12h
    breaking_news: list[dict] = []
    try:
        async with get_db() as conn:
            async with conn.execute(
                """SELECT title, source_id, url FROM signals
                   WHERE is_breaking = 1
                     AND datetime(published_at) >= datetime('now', '-12 hours')
                   ORDER BY published_at DESC LIMIT 4"""
            ) as cur:
                breaking_news = [dict(r) for r in await cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.error("/morning: breaking news fetch failed: %s", e)

    try:
        portfolio = await get_portfolio_with_pnl(market_data)
    except Exception as e:  # noqa: BLE001
        log.error("/morning: portfolio fetch failed: %s", e)
        portfolio = {"holdings": [], "totals": {}}

    portfolio_movers = sorted(
        [
            h for h in (portfolio.get("holdings") or [])
            if h.get("change_24h_pct") is not None
            and abs(h.get("change_24h_pct") or 0) >= 2.0
        ],
        key=lambda h: abs(h.get("change_24h_pct") or 0),
        reverse=True,
    )

    await update.message.reply_text(
        render_urgent_section(
            active, recent,
            date=today_iso,
            breaking_news=breaking_news,
            portfolio_movers=portfolio_movers,
        ),
        parse_mode="HTML",
    )

    try:
        advice = await generate_morning_portfolio_advice(portfolio)
    except Exception as e:  # noqa: BLE001
        log.error("/morning: portfolio advice failed: %s", e)
        advice = []

    await update.message.reply_text(
        render_portfolio_morning_advice(advice, portfolio, date=today_iso),
        parse_mode="HTML",
    )
