"""Shared per-application-state helpers used across the handlers package.

Digest items (ideas/signals) live only in `application.bot_data` for the
lifetime of the bot process — see the package docstring in `__init__.py`
for the full storage model.
"""

from __future__ import annotations

import html

from telegram.ext import ContextTypes

from ...models import BusinessIdea, InvestmentSignal


def _idea_store(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, BusinessIdea]:
    return ctx.application.bot_data.setdefault("idea_store", {})


def _signal_store(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, InvestmentSignal]:
    return ctx.application.bot_data.setdefault("signal_store", {})


def _current_digest_id(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.application.bot_data.get("current_digest_id", "")


def html_escape(s: str) -> str:
    return html.escape(s or "")
