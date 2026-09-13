"""Feedback commands (/clearfeedback, /preferences, /saved, /stats,
/calibrate), feedback-callback handlers, and deep-dive callbacks."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ...db import get_db
from ...observability import get_cost_summary
from ...services.learning import (
    CALIBRATION_THRESHOLD,
    calibrate_from_feedback,
    feedbacks_at_last_calibration,
    get_weekly_summary,
    last_calibration_at,
    maybe_calibrate,
    total_feedback_count,
)
from ._common import _current_digest_id, _idea_store, _signal_store, html_escape

log = logging.getLogger(__name__)


async def cmd_clearfeedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe feedback + learning calibration to start with a clean slate.

    Usage:
      /clearfeedback           — show what would be deleted (dry run)
      /clearfeedback confirm   — actually delete

    Removes:
      - all rows from `feedback` table
      - cached `prompt_injection_ideas`, `weekly_summary`, `manual_preferences`
        from `learning_weights` (resets the auto-learned profile)
      - calibration counter

    Does NOT touch: portfolio_holdings, signals, user_sources, watchlist.
    """
    args = " ".join(ctx.args or []).strip().lower()
    confirm = args == "confirm"

    async with get_db() as conn:
        async with conn.execute("SELECT COUNT(*) FROM feedback") as cur:
            fb_count = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM learning_weights WHERE key IN "
            "('prompt_injection_ideas','weekly_summary','manual_preferences',"
            "'last_calibration_at','calibrations_run','feedbacks_at_last_calibration')"
        ) as cur:
            lw_count = (await cur.fetchone())[0]

    if not confirm:
        await update.message.reply_text(
            f"🗑️ <b>/clearfeedback</b> — dry run\n\n"
            f"Будет удалено:\n"
            f"• <b>{fb_count}</b> строк из <code>feedback</code>\n"
            f"• <b>{lw_count}</b> learning ключей (auto-prefs, manual prefs, weekly summary)\n\n"
            f"Не тронем: portfolio, signals, user_sources, watchlist.\n\n"
            f"Подтвердить: <code>/clearfeedback confirm</code>",
            parse_mode="HTML",
        )
        return

    async with get_db() as conn:
        await conn.execute("DELETE FROM feedback")
        await conn.execute(
            "DELETE FROM learning_weights WHERE key IN "
            "('prompt_injection_ideas','weekly_summary','manual_preferences',"
            "'last_calibration_at','calibrations_run','feedbacks_at_last_calibration')"
        )
        await conn.commit()
    log.info("clearfeedback: wiped %d feedback rows + %d learning keys", fb_count, lw_count)
    await update.message.reply_text(
        f"✅ Удалено: <b>{fb_count}</b> фидбэков + <b>{lw_count}</b> learning ключей.\n\n"
        f"<i>Бот начнёт собирать предпочтения с нуля.</i>",
        parse_mode="HTML",
    )


async def cmd_preferences(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual preference override for idea_generator.

    Usage:
      /preferences                  — show current manual prefs + auto-learned
      /preferences <text>           — set/replace manual preference text
      /preferences clear            — wipe manual preferences (back to auto only)

    The text is injected into idea_generator's system prompt with HIGHEST
    authority (above auto-learned preferences). Examples:
      /preferences хочу больше health и fitness, меньше b2b SaaS
      /preferences избегай ai_tools на ближайший месяц
      /preferences ищу только идеи где revenue $50-200/mo, MVP <= 4 weeks
    """
    from ...services.learning import (  # noqa: PLC0415
        get_manual_preferences,
        get_prompt_injection_ideas,
        set_manual_preferences,
    )

    args_text = " ".join(ctx.args or []).strip()

    if not args_text:
        # Show current state
        manual = await get_manual_preferences()
        learned = await get_prompt_injection_ideas()
        parts: list[str] = ["⚙️ <b>Preferences</b>\n"]
        parts.append("<b>Manual</b> (твои явные правила):")
        parts.append(f"<i>{html_escape(manual)}</i>" if manual else "<i>(не задано)</i>")
        parts.append("")
        parts.append("<b>Auto-learned</b> (из лайков/дизлайков, после ≥20 фидбэков):")
        parts.append(f"<i>{html_escape(learned)}</i>" if learned else "<i>(пока недостаточно фидбэков)</i>")
        parts.append("")
        parts.append("<b>Установить:</b> <code>/preferences хочу больше health, меньше b2b</code>")
        parts.append("<b>Очистить:</b> <code>/preferences clear</code>")
        await update.message.reply_text("\n".join(parts), parse_mode="HTML")
        return

    if args_text.lower() == "clear":
        await set_manual_preferences("")
        await update.message.reply_text(
            "🗑️ <i>Manual preferences cleared.</i> Используются только auto-learned.",
            parse_mode="HTML",
        )
        return

    if len(args_text) > 1000:
        await update.message.reply_text(
            "⚠️ Слишком длинное правило (>1000 символов). Сократи.",
            parse_mode="HTML",
        )
        return

    await set_manual_preferences(args_text)
    await update.message.reply_text(
        f"✅ <b>Manual preferences saved.</b>\n\n"
        f"<i>{html_escape(args_text)}</i>\n\n"
        f"Применится со следующего /digest. Очистить: <code>/preferences clear</code>",
        parse_mode="HTML",
    )


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show saved items pulled from the feedback table (works in Step 3)."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT item_type, item_snapshot, created_at FROM feedback "
            "WHERE feedback_type = 'save' ORDER BY created_at DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text(
            "📭 No saved items yet. Tap 📌 <b>Save</b> on any idea or signal.",
            parse_mode="HTML",
        )
        return
    lines = ["📌 <b>Your saved items:</b>\n"]
    for row in rows:
        snapshot = json.loads(row["item_snapshot"])
        title = snapshot.get("title") or snapshot.get("asset") or "(unknown)"
        lines.append(f"• [{row['item_type']}] {title}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Feedback stats + learning calibration state (Step 14)."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT feedback_type, COUNT(*) FROM feedback GROUP BY feedback_type"
        ) as cur:
            rows = await cur.fetchall()

    parts: list[str] = ["📊 <b>Feedback stats</b>\n"]

    if rows:
        for row in rows:
            parts.append(f"• {row[0]}: {row[1]}")
    else:
        parts.append("<i>No feedback yet.</i>")

    # Learning calibration state
    total = await total_feedback_count()
    last = await feedbacks_at_last_calibration()
    last_at = await last_calibration_at()
    pending = max(0, total - last)
    parts.append("")
    parts.append("🧠 <b>Learning calibration</b>")
    parts.append(f"• Total feedbacks: {total}")
    parts.append(f"• Since last calibration: {pending}")
    parts.append(f"• Threshold: every {CALIBRATION_THRESHOLD}")
    if last_at:
        parts.append(f"• Last calibrated: <code>{last_at[:19].replace('T', ' ')} UTC</code>")
    else:
        parts.append("• Last calibrated: <i>never</i>")

    # Weekly summary from latest calibration (if any)
    summary = await get_weekly_summary()
    if summary:
        parts.append("")
        parts.append("📈 <b>Latest weekly summary</b>")
        parts.append(html_escape(summary))
    else:
        parts.append("")
        parts.append("<i>Tap </i>/calibrate<i> to run learning calibration manually "
                     "(needs at least a few feedbacks + OPENAI_API_KEY).</i>")

    # Step 17: one-line cost summary (details via /cost)
    try:
        cost = await get_cost_summary(days=30)
        if cost["total_calls"]:
            parts.append("")
            parts.append(
                f"💰 <b>LLM cost (30d):</b> ${cost['total_cost_usd']:.4f} "
                f"· {cost['total_calls']} calls · /cost for details"
            )
    except Exception as e:  # noqa: BLE001
        log.debug("cmd_stats: cost summary failed: %s", e)

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


async def cmd_calibrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-run a learning calibration NOW (Step 14)."""
    await update.message.reply_text(
        "🧠 Running calibration on your recent feedback...",
        parse_mode="HTML",
    )

    total = await total_feedback_count()
    if total == 0:
        await update.message.reply_text(
            "📭 No feedback yet. Tap 🔥/❌/📌 buttons on a /digest first.",
        )
        return

    result = await calibrate_from_feedback()
    if not result:
        await update.message.reply_text(
            "⚠️ Calibration did not run.\n\n"
            "Possible reasons:\n"
            "• <code>OPENAI_API_KEY</code> not set in .env\n"
            "• OpenAI API call failed (check bot logs)\n"
            "• <code>openai</code> package not installed",
            parse_mode="HTML",
        )
        return

    msg = (
        f"✅ <b>Calibration complete</b>\n\n"
        f"Analyzed: <b>{result.total_analyzed}</b> feedbacks "
        f"({result.likes} 🔥 / {result.dislikes} ❌ / {result.saves} 📌)\n\n"
        f"<b>Updated preferences (will steer next /digest):</b>\n"
        f"<i>{html_escape(result.prompt_injection_ideas)}</i>\n\n"
        f"<b>Weekly summary:</b>\n"
        f"{html_escape(result.weekly_summary)}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def _handle_idea_feedback(
    ctx: ContextTypes.DEFAULT_TYPE,
    query: Any,
    idea_id: str,
    feedback_type: str,
    reason: str | None,
) -> None:
    idea = _idea_store(ctx).get(idea_id)
    if not idea:
        await query.edit_message_text(
            "⚠️ Idea not found (bot may have restarted since the digest was sent).",
        )
        return
    await _save_feedback(
        item_type="idea",
        item_id=idea_id,
        digest_id=_current_digest_id(ctx),
        feedback_type=feedback_type,
        reason=reason,
        item_snapshot=idea.model_dump(),
    )
    label = {
        "like":    "🔥 Loved",
        "save":    "📌 Saved",
        "dislike": "❌ Rejected",
        "build":   "🚀 Going to build",
    }.get(feedback_type, feedback_type)
    suffix = f" ({reason})" if reason else ""

    # For LIKE / SAVE / BUILD — only toast, do NOT replace card text.
    # User wants to keep the card visible to scroll back / press Deep dive.
    # For DISLIKE with reason — replace, since the reason-keyboard already
    # replaced the original markup and user finished the flow.
    if feedback_type in ("like", "save", "build"):
        try:
            await query.answer(
                text=f"{label}: записано",
                show_alert=False,
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # Dislike / dislike-with-reason → replace card to confirm completion
    await query.edit_message_text(
        f"{label}{suffix}: <b>{idea.title}</b>\n\n"
        f"<i>Feedback recorded — ORACLE learns from this.</i>",
        parse_mode="HTML",
    )


async def _handle_signal_feedback(
    ctx: ContextTypes.DEFAULT_TYPE,
    query: Any,
    sig_id: str,
    feedback_type: str,
) -> None:
    sig = _signal_store(ctx).get(sig_id)
    if not sig:
        await query.edit_message_text(
            "⚠️ Signal not found (bot may have restarted since the digest was sent).",
        )
        return
    await _save_feedback(
        item_type="investment_signal",
        item_id=sig_id,
        digest_id=_current_digest_id(ctx),
        feedback_type=feedback_type,
        reason=None,
        item_snapshot=sig.model_dump(),
    )
    label = {
        "like": "✅ Marked useful",
        "save": "📌 Watching",
        "dislike": "❌ Skipped",
    }[feedback_type]
    from ...services.portfolio import display_name  # noqa: PLC0415

    # Like / Save / dislike on investment signals — toast only, keep card visible.
    # User wants to read the card again, click Deep dive, Add to portfolio etc.
    try:
        await query.answer(
            text=f"{label}: {display_name(sig.asset)}",
            show_alert=False,
        )
    except Exception:  # noqa: BLE001
        pass


async def _handle_idea_deep_dive(
    ctx: ContextTypes.DEFAULT_TYPE,
    query: Any,
    idea_id: str,
) -> None:
    """Real deep-dive on an idea — runs a focused LLM call (gpt-5.4-mini)
    that produces pricing analysis, CAC estimate, top 3 actionable next
    steps, and risk-callouts. Cheap (~$0.01) but high-value.

    Fallback (no LLM creds) — static placeholder.
    """
    idea = _idea_store(ctx).get(idea_id)
    if not idea:
        await query.edit_message_text("⚠️ Idea not found (bot may have restarted).")
        return

    # Show working state — deep-dive takes 5-15 sec
    try:
        progress = await query.message.reply_text(
            f"🔍 <i>Готовлю deep-dive по «{html_escape(idea.title)}»...\n"
            f"Анализ цен / CAC / next steps. ~10 сек.</i>",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        progress = None

    try:
        from ...agents.deep_dive import run_idea_deep_dive  # noqa: PLC0415
        dive = await run_idea_deep_dive(idea.model_dump())
    except Exception as e:  # noqa: BLE001
        log.warning("deep_dive: failed — falling back to static: %s", e)
        dive = None

    if dive:
        text = (
            f"🔍 <b>Deep dive: {html_escape(idea.title)}</b>\n\n"
            f"💵 <b>Pricing анализ:</b>\n{html_escape(dive.get('pricing_analysis', '—'))}\n\n"
            f"🎯 <b>CAC оценка:</b>\n{html_escape(dive.get('cac_estimate', '—'))}\n\n"
            f"📊 <b>Конкуренты (углублённо):</b>\n"
            f"{html_escape(dive.get('competitor_landscape', '—'))}\n\n"
            f"🚀 <b>Top 3 next steps:</b>\n"
        )
        next_steps = dive.get("next_steps") or []
        for i, step in enumerate(next_steps[:3], start=1):
            text += f"{i}. {html_escape(step)}\n"
        risks = dive.get("risk_callouts") or []
        if risks:
            text += f"\n⚠️ <b>Риски/блокеры:</b>\n"
            for r in risks[:3]:
                text += f"• {html_escape(r)}\n"
        if progress:
            try:
                await progress.edit_text(text, parse_mode="HTML")
                return
            except Exception:
                pass
        await query.message.reply_text(text, parse_mode="HTML")
        return

    # Fallback static
    competitors = "\n".join(f"• {c}" for c in idea.competitors) if idea.competitors else "<i>(none found)</i>"
    text = (
        f"🔍 <b>Deep dive: {idea.title}</b>\n\n"
        f"<b>Competitors:</b>\n{competitors}\n\n"
        f"<b>Customer acquisition path (placeholder):</b>\n"
        f"1. Post on relevant subreddits with case study\n"
        f"2. Launch on ProductHunt with demo video\n"
        f"3. Outreach to first 20 leads on LinkedIn\n\n"
        f"<b>Architecture sketch:</b>\n<code>{' → '.join(idea.mvp_stack)}</code>\n\n"
        f"🚧 <i>Full deep-dive (web search + market sizing) ships in Step 12 (validator).</i>"
    )
    await query.message.reply_text(text, parse_mode="HTML")


async def _handle_signal_deep_dive(
    ctx: ContextTypes.DEFAULT_TYPE,
    query: Any,
    sig_id: str,
) -> None:
    """Real investment deep-dive — LLM call producing technical levels +
    upcoming catalysts + historical analogues + sizing + downside risk.

    Anchored to Maksim's actual portfolio allocation (computed fresh
    from DB). Cost ~$0.01 per click.
    """
    sig = _signal_store(ctx).get(sig_id)
    if not sig:
        await query.edit_message_text("⚠️ Signal not found (bot may have restarted).")
        return

    from ...services.portfolio import display_name  # noqa: PLC0415

    progress = None
    try:
        progress = await query.message.reply_text(
            f"📊 <i>Готовлю deep-dive по {display_name(sig.asset)}...\n"
            f"Technical levels + catalysts + sizing для твоего портфеля. ~10 сек.</i>",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        pass

    # Fetch fresh portfolio snapshot (always current — reads DB)
    portfolio_summary = ""
    try:
        from ...agents.market import collect_market_data  # noqa: PLC0415
        from ...services.portfolio import (  # noqa: PLC0415
            format_portfolio_for_llm,
            get_portfolio_with_pnl,
        )
        market_data, _ = await collect_market_data()
        portfolio = await get_portfolio_with_pnl(market_data)
        portfolio_summary = format_portfolio_for_llm(portfolio)
    except Exception as e:  # noqa: BLE001
        log.warning("invest_deep_dive: portfolio fetch failed: %s", e)
        portfolio_summary = "(portfolio context unavailable)"

    # Call the deep-dive agent
    dive = None
    try:
        from ...agents.invest_deep_dive import run_invest_deep_dive  # noqa: PLC0415
        dive = await run_invest_deep_dive(sig.model_dump(), portfolio_summary)
    except Exception as e:  # noqa: BLE001
        log.warning("invest_deep_dive: failed: %s", e)

    if dive:
        catalysts = "\n".join(f"• {c}" for c in (dive.get("upcoming_catalysts") or []))
        text = (
            f"📊 <b>Deep-dive · {display_name(sig.asset)}</b>\n"
            f"💵 ${sig.price:,.2f} ({sig.change_24h:+.1f}% 24h)\n\n"
            f"📐 <b>Технические уровни:</b>\n{html_escape(dive.get('technical_levels', '—'))}\n\n"
            f"📅 <b>Ближайшие катализаторы:</b>\n{html_escape(catalysts) if catalysts else '<i>—</i>'}\n\n"
            f"📚 <b>Историческая аналогия:</b>\n{html_escape(dive.get('historical_analogues', '—'))}\n\n"
            f"⚖️ <b>Сайзинг под твой портфель:</b>\n{html_escape(dive.get('sizing_recommendation', '—'))}\n\n"
            f"⚠️ <b>Downside-риск:</b> {html_escape(dive.get('downside_risk', '—'))}\n\n"
            f"<i>Educational only. NOT financial advice.</i>"
        )
        try:
            if progress:
                await progress.edit_text(text, parse_mode="HTML")
                return
        except Exception:
            pass
        await query.message.reply_text(text, parse_mode="HTML")
        return

    # Fallback: re-show signal card content if LLM failed
    news = "\n".join(f"• {n}" for n in (sig.news_highlights or [])[:5]) or "<i>(нет новостей)</i>"
    text = (
        f"📊 <b>Полный анализ: {display_name(sig.asset)}</b>\n\n"
        f"💵 <b>Цена:</b> ${sig.price:,.2f} ({sig.change_24h:+.1f}% 24h)\n\n"
        f"📰 <b>Новости:</b>\n{news}\n\n"
        f"📊 <b>Тренд:</b> {sig.trend or '—'}\n\n"
        f"🐂 <b>Бык:</b> {sig.critic_bull or '—'}\n\n"
        f"🐻 <b>Медведь:</b> {sig.critic_bear or '—'}\n\n"
        f"🔮 <b>Прогноз:</b> {sig.prediction or sig.future_outlook or '—'}\n\n"
        f"<i>(Deep-dive LLM упал — показываю основную карточку)</i>"
    )
    if progress:
        try:
            await progress.edit_text(text, parse_mode="HTML")
            return
        except Exception:
            pass
    await query.message.reply_text(text, parse_mode="HTML")


async def _save_feedback(
    *,
    item_type: str,
    item_id: str,
    digest_id: str,
    feedback_type: str,
    reason: str | None,
    item_snapshot: dict,
) -> None:
    """Persist a feedback row + maybe trigger learning calibration (Step 14)."""
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO feedback (item_type, item_id, digest_id, feedback_type,
                                       reason, item_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item_type,
                item_id,
                digest_id,
                feedback_type,
                reason,
                json.dumps(item_snapshot, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await conn.commit()
    log.info(
        "feedback saved: type=%s id=%s feedback=%s reason=%s",
        item_type, item_id, feedback_type, reason,
    )

    # Step 14: fire-and-forget calibration trigger. Doesn't block the user's
    # button-click response. The bot keeps responding instantly; calibration
    # runs in the background. maybe_calibrate() is a no-op if threshold not
    # reached, and never raises (catches all errors internally).
    asyncio.create_task(_background_calibration())


async def _background_calibration() -> None:
    """Wraps maybe_calibrate so the create_task fire-and-forget is clean."""
    try:
        result = await maybe_calibrate()
        if result:
            log.info("learning: background calibration completed (%d analyzed)",
                     result.total_analyzed)
    except Exception as e:  # noqa: BLE001 — never crash the bot
        log.error("learning: background calibration error: %s", e)
