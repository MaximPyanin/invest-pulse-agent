"""/start command."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes


WELCOME_HTML = """\
👋 <b>Welcome to ORACLE</b>

I scan news, tech trends, Reddit, Telegram, YouTube, and websites to surface
SaaS / AI business ideas <b>before</b> they hype, plus secondary investment
signals.

<b>Commands:</b>
• /digest — full digest right now
• /morning — morning brief
• /sources — manage your sources
• /add_source — add a custom source
• /portfolio — your holdings + live P&amp;L
• /add_holding — add/update a position
• /remove_holding &lt;ASSET&gt; — drop a position
• /history &lt;topic&gt; — topic history
• /saved — your saved items
• /stats — feedback + learning calibration state
• /calibrate — force-run learning calibration now
• /cost — LLM cost breakdown (tokens, agents, days)
• /settings — focus weights
• /pause &lt;days&gt; — pause digests

<i>Tap 🔥 / ❌ / 📌 buttons on each idea — every 20 feedbacks I'll
auto-recalibrate to learn what you actually want.</i>
"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_HTML, parse_mode="HTML")
