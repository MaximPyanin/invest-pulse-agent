"""Central callback-query dispatcher — routes every inline-button press
by its callback_data prefix to the right handler module."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..views import dislike_reason_buttons
from ._common import _signal_store
from .feedback import (
    _handle_idea_deep_dive,
    _handle_idea_feedback,
    _handle_signal_deep_dive,
    _handle_signal_feedback,
)
from .portfolio import _handle_pf_remove
from .sources import (
    _handle_remove_source_action,
    _handle_remove_source_list,
    _handle_source_stats,
)

log = logging.getLogger(__name__)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # acknowledge — removes the spinner
    data = query.data or ""
    log.info("callback: %s", data)

    # ----- Idea card buttons -----
    if data.startswith("like_"):
        await _handle_idea_feedback(ctx, query, data[len("like_"):], "like", reason=None)
    elif data.startswith("dislike_"):
        idea_id = data[len("dislike_"):]
        # Edit message to show 6 reason buttons — that's how the system learns
        await query.edit_message_reply_markup(reply_markup=dislike_reason_buttons(idea_id))
    elif data.startswith("save_"):
        await _handle_idea_feedback(ctx, query, data[len("save_"):], "save", reason=None)
    elif data.startswith("build_"):
        # High-intent signal — heaviest weight in learning loop
        await _handle_idea_feedback(ctx, query, data[len("build_"):], "build", reason=None)
    elif data.startswith("deep_"):
        await _handle_idea_deep_dive(ctx, query, data[len("deep_"):])
    elif data.startswith("reason_"):
        # reason_<reason>_<idea_id>  →  3-part split
        parts = data.split("_", 2)
        if len(parts) != 3:
            log.warning("malformed reason callback: %s", data)
            return
        _, reason, idea_id = parts
        await _handle_idea_feedback(ctx, query, idea_id, "dislike", reason=reason)

    # ----- Investment card buttons -----
    elif data.startswith("inv_like_"):
        await _handle_signal_feedback(ctx, query, data[len("inv_like_"):], "like")
    elif data.startswith("inv_skip_"):
        await _handle_signal_feedback(ctx, query, data[len("inv_skip_"):], "dislike")
    elif data.startswith("inv_watch_"):
        sig_id = data[len("inv_watch_"):]
        # Watch = feedback save + add to active watchlist (alerts fire on ±5%)
        await _handle_signal_feedback(ctx, query, sig_id, "save")
        try:
            sig = _signal_store(ctx).get(sig_id)
            if sig and sig.price and sig.price > 0:
                from ...services.watchlist import add_to_watchlist  # noqa: PLC0415
                await add_to_watchlist(
                    asset_label=sig.asset,
                    baseline_price=float(sig.price),
                    source_sig_id=sig_id,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("inv_watch_: failed to add to watchlist: %s", e)
    elif data.startswith("inv_deep_"):
        await _handle_signal_deep_dive(ctx, query, data[len("inv_deep_"):])

    # ----- Portfolio management buttons (per-holding) -----
    elif data.startswith("pf_remove_"):
        await _handle_pf_remove(ctx, query, data[len("pf_remove_"):])
    # pf_addusd_ / pf_subusd_ are caught by their ConversationHandler entry

    # ----- Sources card buttons (Step 8 — real implementations) -----
    # Note: "add_source" is captured by ConversationHandler entry_point, NOT here
    elif data == "source_stats":
        await _handle_source_stats(query)
    elif data == "remove_source":
        await _handle_remove_source_list(query)
    elif data.startswith("rmsrc_"):
        await _handle_remove_source_action(query, data[len("rmsrc_"):])

    else:
        log.warning("unknown callback: %s", data)
        await query.edit_message_text(f"⚠️ Unknown callback: <code>{data}</code>", parse_mode="HTML")
