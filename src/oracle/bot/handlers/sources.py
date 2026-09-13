"""/sources card + the /add_source ConversationHandler flow."""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ...agents.custom import (
    VALID_CATEGORIES,
    VALID_SOURCE_TYPES,
    add_user_source,
    load_active_sources,
    remove_user_source,
)
from ...db import get_db
from ..views import (
    SOURCE_CATEGORY_LABELS,
    SOURCE_TYPE_LABELS,
    add_source_category_kb,
    add_source_confirm_kb,
    add_source_type_kb,
    remove_source_list_kb,
    render_real_sources_card,
    render_source_stats_card,
    sources_buttons,
)
from ._common import html_escape

log = logging.getLogger(__name__)


async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show real user sources from oracle_data.db (Step 8)."""
    sources = await load_active_sources()
    await update.message.reply_text(
        render_real_sources_card(sources),
        reply_markup=sources_buttons(),
        parse_mode="HTML",
    )


# ============================================================================
# /add_source — interactive ConversationHandler (Step 8)
# ============================================================================
#
# State machine:
#
#   /add_source  →  ASK_TYPE
#   user picks type        →  ASK_CATEGORY
#   user picks category    →  ASK_URL  (text input)
#   user sends URL/handle  →  ASK_NAME (text input)
#   user sends name        →  ASK_CONFIRM (yes/cancel)
#   user confirms          →  END (insert into user_sources)
#   /cancel at any state   →  END
#
# Per-user draft lives in ctx.user_data["new_source"] — single user bot, but
# python-telegram-bot scopes user_data per Telegram user automatically.

ASK_TYPE, ASK_CATEGORY, ASK_URL, ASK_NAME, ASK_CONFIRM = range(5)


# Type-specific instructions for the URL input step
URL_INSTRUCTIONS = {
    "telegram_channel": (
        "💬 <b>Telegram channel</b>\n\n"
        "Send the channel handle (e.g. <code>@bensbites</code>) "
        "or a t.me link.\n\n"
        "<i>Note: requires one-time auth via "
        "</i><code>uv run python -m oracle.agents.custom auth-tg</code><i> "
        "from your terminal first.</i>"
    ),
    "youtube_channel": (
        "📺 <b>YouTube channel</b>\n\n"
        "Send the channel ID (e.g. <code>UCqhFigXTltY4tldNOyzj7sg</code>).\n"
        "Find it in the channel URL: <code>youtube.com/channel/UC...</code>\n\n"
        "<i>Requires </i><code>YOUTUBE_API_KEY</code><i> in .env.</i>"
    ),
    "website_blog": (
        "🌐 <b>Website / blog</b>\n\n"
        "Send the full site URL (e.g. <code>https://danluu.com</code>).\n\n"
        "<i>I'll auto-discover the RSS feed if there is one.</i>"
    ),
    "rss_custom": (
        "📰 <b>RSS feed</b>\n\n"
        "Send the full RSS/Atom URL "
        "(e.g. <code>https://example.com/feed.xml</code>)."
    ),
    "reddit_custom": (
        "🔥 <b>Subreddit</b>\n\n"
        "Send the subreddit name (e.g. <code>r/SaaS</code> or just <code>SaaS</code>)."
    ),
}


def _draft(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.user_data.setdefault("new_source", {})


def _clear_draft(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("new_source", None)


def _normalize_url_input(source_type: str, raw: str) -> str:
    """Normalize user input to the canonical form for each source type."""
    raw = raw.strip()
    if source_type == "telegram_channel":
        # @bensbites or t.me/bensbites or https://t.me/bensbites → @bensbites
        if "t.me/" in raw:
            raw = raw.split("t.me/", 1)[1].rstrip("/")
        if not raw.startswith("@"):
            raw = "@" + raw
        return raw
    if source_type == "youtube_channel":
        # https://youtube.com/channel/UC... → UC...
        if "/channel/" in raw:
            raw = raw.split("/channel/", 1)[1].split("/")[0]
        return raw
    if source_type == "reddit_custom":
        # /r/SaaS or r/SaaS or SaaS → r/SaaS
        cleaned = raw.lstrip("/").lstrip("r/").lstrip("/")
        return f"r/{cleaned}"
    if source_type in ("website_blog", "rss_custom"):
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        return raw
    return raw


# ----- Entry points -----


async def cmd_add_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: /add_source slash command."""
    _clear_draft(ctx)
    await update.message.reply_text(
        "➕ <b>Add a new source</b>\n\n"
        "Where should I pull signals from?",
        reply_markup=add_source_type_kb(),
        parse_mode="HTML",
    )
    return ASK_TYPE


async def add_source_callback_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: ➕ Add source button on /sources card."""
    query = update.callback_query
    await query.answer()
    _clear_draft(ctx)
    await query.message.reply_text(
        "➕ <b>Add a new source</b>\n\n"
        "Where should I pull signals from?",
        reply_markup=add_source_type_kb(),
        parse_mode="HTML",
    )
    return ASK_TYPE


# ----- State handlers -----


async def add_source_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_TYPE → ASK_CATEGORY: user picked source type."""
    query = update.callback_query
    await query.answer()
    source_type = query.data.replace("addtype_", "", 1)
    if source_type not in VALID_SOURCE_TYPES:
        await query.edit_message_text("⚠️ Invalid source type. Cancelled.")
        _clear_draft(ctx)
        return ConversationHandler.END

    _draft(ctx)["source_type"] = source_type
    type_label = SOURCE_TYPE_LABELS.get(source_type, source_type)
    await query.edit_message_text(
        f"➕ <b>Add source</b> · {type_label}\n\n"
        f"Now pick a <b>category</b>:\n"
        f"<i>(business ideas are ORACLE's primary focus)</i>",
        reply_markup=add_source_category_kb(),
        parse_mode="HTML",
    )
    return ASK_CATEGORY


async def add_source_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_CATEGORY → ASK_URL: user picked category, ask for URL."""
    query = update.callback_query
    await query.answer()
    category = query.data.replace("addcat_", "", 1)
    if category not in VALID_CATEGORIES:
        await query.edit_message_text("⚠️ Invalid category. Cancelled.")
        _clear_draft(ctx)
        return ConversationHandler.END

    draft = _draft(ctx)
    draft["category"] = category

    source_type = draft.get("source_type", "")
    instructions = URL_INSTRUCTIONS.get(source_type, "Send the source URL or identifier.")

    await query.edit_message_text(
        f"{instructions}\n\n"
        f"<i>Send /cancel to abort.</i>",
        parse_mode="HTML",
    )
    return ASK_URL


async def add_source_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_URL → ASK_NAME: user sent URL as text message."""
    raw_url = (update.message.text or "").strip()
    if not raw_url:
        await update.message.reply_text("⚠️ Empty URL. Send a valid URL or /cancel.")
        return ASK_URL
    if len(raw_url) > 500:
        await update.message.reply_text("⚠️ URL too long (>500 chars). Send a shorter one or /cancel.")
        return ASK_URL

    draft = _draft(ctx)
    source_type = draft.get("source_type", "")
    normalized = _normalize_url_input(source_type, raw_url)
    draft["source_url"] = normalized

    await update.message.reply_text(
        f"✓ URL: <code>{html_escape(normalized)}</code>\n\n"
        f"Now send a short <b>display name</b> for this source "
        f"(e.g. <i>Ben's Bites</i>, <i>YC News</i>, <i>Dan Luu blog</i>).",
        parse_mode="HTML",
    )
    return ASK_NAME


async def add_source_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_NAME → ASK_CONFIRM: user sent name, show summary."""
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("⚠️ Empty name. Send a name or /cancel.")
        return ASK_NAME
    if len(name) > 100:
        await update.message.reply_text("⚠️ Name too long (>100 chars). Send a shorter one or /cancel.")
        return ASK_NAME

    draft = _draft(ctx)
    draft["display_name"] = name

    type_label = SOURCE_TYPE_LABELS.get(draft.get("source_type", ""), draft.get("source_type", "?"))
    cat_label = SOURCE_CATEGORY_LABELS.get(draft.get("category", ""), draft.get("category", "?"))

    summary = (
        "<b>Add this source?</b>\n\n"
        f"Type:     {type_label}\n"
        f"Category: {cat_label}\n"
        f"URL:      <code>{html_escape(draft.get('source_url', ''))}</code>\n"
        f"Name:     <b>{html_escape(name)}</b>"
    )
    await update.message.reply_text(
        summary,
        reply_markup=add_source_confirm_kb(),
        parse_mode="HTML",
    )
    return ASK_CONFIRM


async def add_source_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_CONFIRM → END: user confirmed, INSERT into user_sources."""
    query = update.callback_query
    await query.answer()

    if query.data != "addconf_yes":
        # treated as cancel — fall through to cancel path
        await query.edit_message_text("Cancelled. Source not added.")
        _clear_draft(ctx)
        return ConversationHandler.END

    draft = _draft(ctx)
    try:
        new_id = await add_user_source(
            source_type=draft["source_type"],
            source_url=draft["source_url"],
            display_name=draft["display_name"],
            category=draft["category"],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("add_source insert failed")
        await query.edit_message_text(
            f"⚠️ Failed to add source: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
        _clear_draft(ctx)
        return ConversationHandler.END

    type_label = SOURCE_TYPE_LABELS.get(draft["source_type"], draft["source_type"])
    cat_label = SOURCE_CATEGORY_LABELS.get(draft["category"], draft["category"])
    await query.edit_message_text(
        f"✅ <b>Source #{new_id} added!</b>\n\n"
        f"{type_label} · {cat_label}\n"
        f"<b>{html_escape(draft['display_name'])}</b>\n"
        f"<code>{html_escape(draft['source_url'])}</code>\n\n"
        f"<i>It'll be included in the next digest collection.</i>",
        parse_mode="HTML",
    )
    _clear_draft(ctx)
    return ConversationHandler.END


async def add_source_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel slash command — exits the conversation cleanly from any state."""
    _clear_draft(ctx)
    await update.message.reply_text("Cancelled. No source added.")
    return ConversationHandler.END


async def add_source_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel button — exits the conversation from any state."""
    query = update.callback_query
    await query.answer()
    _clear_draft(ctx)
    await query.edit_message_text("Cancelled. No source added.")
    return ConversationHandler.END


# ============================================================================
# /sources card button handlers
# ============================================================================


async def _handle_source_stats(query: Any) -> None:
    """📊 Source stats button — shows aggregated counts."""
    stats = await _gather_source_stats()
    await query.message.reply_text(
        render_source_stats_card(stats),
        parse_mode="HTML",
    )


async def _handle_remove_source_list(query: Any) -> None:
    """🗑️ Remove source button — show list of sources, one button per source."""
    sources = await load_active_sources()
    if not sources:
        await query.message.reply_text(
            "📭 No sources to remove. Tap ➕ Add source to add one first."
        )
        return
    await query.message.reply_text(
        f"🗑 <b>Pick a source to remove</b> ({len(sources)} active):",
        reply_markup=remove_source_list_kb(sources),
        parse_mode="HTML",
    )


async def _handle_remove_source_action(query: Any, payload: str) -> None:
    """rmsrc_<id> or rmsrc_cancel — execute the removal."""
    if payload == "cancel":
        await query.edit_message_text("Cancelled. No source removed.")
        return
    try:
        source_pk = int(payload)
    except ValueError:
        await query.edit_message_text(f"⚠️ Invalid source ID: <code>{payload}</code>", parse_mode="HTML")
        return
    n = await remove_user_source(source_pk)
    if n:
        await query.edit_message_text(
            f"✅ Removed source #{source_pk}.\n\n"
            f"<i>It won't appear in future digests.</i>",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(f"⚠️ No source with id={source_pk}.", parse_mode="HTML")


async def _gather_source_stats() -> dict:
    """Aggregate stats for the /source_stats card."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM user_sources WHERE is_active = 1"
        ) as cur:
            active = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM user_sources WHERE is_active = 0"
        ) as cur:
            inactive = (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM signals") as cur:
            total = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM signals WHERE is_breaking = 1"
        ) as cur:
            breaking = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM signals "
            "GROUP BY source_type ORDER BY n DESC LIMIT 10"
        ) as cur:
            by_type = [(r["source_type"], r["n"]) for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT us.display_name, COUNT(s.id) AS n "
            "FROM user_sources us LEFT JOIN signals s "
            "ON s.source_id LIKE '%/' || us.display_name "
            "  OR s.source_id LIKE '%' || us.display_name "
            "WHERE us.is_active = 1 "
            "GROUP BY us.id ORDER BY n DESC LIMIT 5"
        ) as cur:
            top_user = [(r["display_name"], r["n"]) for r in await cur.fetchall()]

    return {
        "user_sources_active": active,
        "user_sources_inactive": inactive,
        "signals_total": total,
        "signals_breaking": breaking,
        "by_source_type": by_type,
        "top_user_sources": top_user,
    }
