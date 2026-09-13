"""Command and callback handlers for the ORACLE Telegram bot.

Split by domain into sibling modules — each slash command lives with the
callbacks and (if any) `ConversationHandler` state machine it belongs to:

  _common.py    shared `application.bot_data` accessors + `html_escape`
  start.py      /start
  digest.py     /digest, /lastdigest, /more
  morning.py    /morning
  sources.py    /sources + the /add_source ConversationHandler
  portfolio.py  /portfolio, /add_holding, /remove_holding + the three
                portfolio ConversationHandlers (add-to-portfolio,
                adjust +$/-$, add-new-position)
  feedback.py   /clearfeedback, /preferences, /saved, /stats, /calibrate,
                feedback callbacks, deep-dive callbacks
  settings.py   /history, /cost, /settings, /pause
  callbacks.py  the single callback_handler dispatcher

Storage:
  - Digest items survive only until the bot process restarts (in-memory
    `application.bot_data`). Feedback IS persisted to
    oracle_data.db.feedback.

This `__init__.py` re-exports every name `bot/main.py` wires up, so the
package can be split internally without changing anything outside it.
"""

from __future__ import annotations

from .callbacks import callback_handler
from .digest import cmd_digest, cmd_lastdigest, cmd_more
from .feedback import (
    cmd_calibrate,
    cmd_clearfeedback,
    cmd_preferences,
    cmd_saved,
    cmd_stats,
)
from .morning import cmd_morning
from .portfolio import (
    ASK_ADJUST_AMOUNT,
    ASK_HOLDING_PRICE,
    ASK_HOLDING_QTY,
    ASK_NEW_AMOUNT,
    PICK_NEW_TICKER,
    add_holding_callback_entry,
    add_holding_cancel_cmd,
    add_holding_price,
    add_holding_qty,
    cmd_add_holding,
    cmd_portfolio,
    cmd_remove_holding,
    pf_addnew_amount,
    pf_addnew_cancel_cmd,
    pf_addnew_entry,
    pf_addnew_pick,
    pf_adjust_amount,
    pf_adjust_callback_entry,
    pf_adjust_cancel_cmd,
)
from .settings import cmd_cost, cmd_history, cmd_pause, cmd_settings
from .sources import (
    ASK_CATEGORY,
    ASK_CONFIRM,
    ASK_TYPE,
    ASK_URL,
    ASK_NAME,
    add_source_callback_entry,
    add_source_cancel_cb,
    add_source_cancel_cmd,
    add_source_category,
    add_source_confirm,
    add_source_name,
    add_source_type,
    add_source_url,
    cmd_add_source,
    cmd_sources,
)
from .start import cmd_start

__all__ = [
    "ASK_ADJUST_AMOUNT",
    "ASK_CATEGORY",
    "ASK_CONFIRM",
    "ASK_HOLDING_PRICE",
    "ASK_HOLDING_QTY",
    "ASK_NAME",
    "ASK_NEW_AMOUNT",
    "ASK_TYPE",
    "ASK_URL",
    "PICK_NEW_TICKER",
    "add_holding_callback_entry",
    "add_holding_cancel_cmd",
    "add_holding_price",
    "add_holding_qty",
    "add_source_callback_entry",
    "add_source_cancel_cb",
    "add_source_cancel_cmd",
    "add_source_category",
    "add_source_confirm",
    "add_source_name",
    "add_source_type",
    "add_source_url",
    "callback_handler",
    "cmd_add_holding",
    "cmd_add_source",
    "cmd_calibrate",
    "cmd_clearfeedback",
    "cmd_cost",
    "cmd_digest",
    "cmd_history",
    "cmd_lastdigest",
    "cmd_more",
    "cmd_morning",
    "cmd_pause",
    "cmd_portfolio",
    "cmd_preferences",
    "cmd_remove_holding",
    "cmd_saved",
    "cmd_settings",
    "cmd_sources",
    "cmd_start",
    "cmd_stats",
    "pf_addnew_amount",
    "pf_addnew_cancel_cmd",
    "pf_addnew_entry",
    "pf_addnew_pick",
    "pf_adjust_amount",
    "pf_adjust_callback_entry",
    "pf_adjust_cancel_cmd",
]
