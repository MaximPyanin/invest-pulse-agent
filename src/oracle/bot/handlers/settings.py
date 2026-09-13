"""/history, /cost, /settings, /pause — small standalone commands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from ...observability import get_cost_summary
from ...services.scheduler import get_pause_until_iso, set_pause_days

log = logging.getLogger(__name__)


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    topic = " ".join(ctx.args) if ctx.args else None
    if not topic:
        await update.message.reply_text("Usage: <code>/history &lt;topic&gt;</code>", parse_mode="HTML")
        return
    await update.message.reply_text(
        f"🚧 <i>/history for </i><b>{topic}</b><i> ships in Step 9 with topic_timeline tracking.</i>",
        parse_mode="HTML",
    )


async def cmd_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Detailed LLM cost breakdown (Step 17)."""
    args = ctx.args
    try:
        days = int(args[0]) if args else 30
    except ValueError:
        days = 30
    days = max(1, min(days, 365))

    cost = await get_cost_summary(days=days)

    if not cost["total_calls"]:
        await update.message.reply_text(
            f"💰 <b>LLM cost — last {days} days</b>\n\n"
            f"<i>No LLM calls logged yet. Set </i><code>OPENAI_API_KEY</code>"
            f"<i> in .env and run </i>/digest<i>.</i>",
            parse_mode="HTML",
        )
        return

    parts: list[str] = [
        f"💰 <b>LLM cost — last {days} days</b>\n",
        f"• Total calls: <b>{cost['total_calls']}</b>",
        f"• Total cost: <b>${cost['total_cost_usd']:.4f}</b>",
        f"• Input tokens: {cost['total_input_tokens']:,}",
        f"• Output tokens: {cost['total_output_tokens']:,}",
    ]

    # Monthly projection (naive extrapolation if days < 30)
    if days < 30 and cost["total_cost_usd"] > 0:
        projected = cost["total_cost_usd"] * (30 / days)
        parts.append(f"• Projected 30d: <b>${projected:.4f}</b>")

    if cost["by_agent"]:
        parts.append("")
        parts.append("<b>By agent:</b>")
        for agent, calls, agent_cost in cost["by_agent"]:
            pct = (agent_cost / cost["total_cost_usd"] * 100) if cost["total_cost_usd"] > 0 else 0
            parts.append(
                f"• <code>{agent}</code>: {calls} calls · "
                f"${agent_cost:.4f} ({pct:.0f}%)"
            )

    if cost["by_day"]:
        parts.append("")
        parts.append("<b>By day (recent):</b>")
        for day, day_cost in cost["by_day"][:7]:
            parts.append(f"• <code>{day}</code>: ${day_cost:.4f}")

    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🚧 <i>/settings UI ships in Step 14.</i>\n"
        "For now, edit <code>IDEA_FOCUS_WEIGHT</code> in <code>.env</code> "
        "(default 0.70 = 70% ideas / 30% investments).",
        parse_mode="HTML",
    )


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause or resume the morning/evening scheduled digests (Step 15).

    Usage:
      /pause          → show current state
      /pause 3        → pause for 3 days
      /pause 0        → resume immediately
    """
    args = ctx.args

    # No args → show current state
    if not args:
        pause_iso = await get_pause_until_iso()
        if not pause_iso:
            await update.message.reply_text(
                "▶️ <b>ORACLE is running.</b>\n\n"
                "Morning brief @ 07:00 · Evening digest @ 19:00 (Warsaw).\n\n"
                "Use <code>/pause 3</code> to pause for 3 days, "
                "<code>/pause 0</code> to resume.",
                parse_mode="HTML",
            )
            return
        try:
            until = datetime.fromisoformat(pause_iso)
            now = datetime.now(timezone.utc)
            if until > now:
                remaining_h = int((until - now).total_seconds() / 3600)
                remaining_d = remaining_h // 24
                await update.message.reply_text(
                    f"⏸ <b>ORACLE is paused</b>\n\n"
                    f"Resumes: <code>{until.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
                    f"Remaining: ~{remaining_d}d {remaining_h % 24}h\n\n"
                    f"Use <code>/pause 0</code> to resume now.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    "▶️ ORACLE is running (paused timestamp expired).",
                )
        except ValueError:
            await update.message.reply_text(
                "⚠️ Pause state corrupt. Use <code>/pause 0</code> to reset.",
                parse_mode="HTML",
            )
        return

    # /pause N
    try:
        days = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "Usage: <code>/pause &lt;days&gt;</code>\n\n"
            "Examples:\n"
            "• <code>/pause 3</code> — pause for 3 days\n"
            "• <code>/pause 0</code> — resume now\n"
            "• <code>/pause</code> — show current state",
            parse_mode="HTML",
        )
        return

    if days < 0:
        await update.message.reply_text("⚠️ Days must be ≥ 0")
        return

    if days == 0:
        await set_pause_days(0)
        await update.message.reply_text(
            "▶️ <b>ORACLE resumed.</b>\n"
            "Morning brief + evening digest back on schedule.",
            parse_mode="HTML",
        )
        return

    until_iso = await set_pause_days(days)
    try:
        until = datetime.fromisoformat(until_iso)
        until_human = until.strftime('%Y-%m-%d %H:%M UTC')
    except ValueError:
        until_human = until_iso

    await update.message.reply_text(
        f"⏸ <b>ORACLE paused for {days} day(s).</b>\n\n"
        f"Will resume at: <code>{until_human}</code>\n\n"
        f"Manual commands (/digest, /morning) still work. "
        f"Only the scheduled cron jobs are paused.",
        parse_mode="HTML",
    )
