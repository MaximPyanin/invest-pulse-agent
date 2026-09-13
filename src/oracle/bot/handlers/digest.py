"""/digest, /lastdigest, /more — the core digest delivery flow."""

from __future__ import annotations

import logging
import uuid

from telegram import Update
from telegram.ext import ContextTypes

from ...models import BusinessIdea, InvestmentSignal
from ..views import idea_buttons, investment_buttons, render_idea_card, render_investment_card
from ._common import _idea_store, _signal_store, html_escape

log = logging.getLogger(__name__)


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the REAL ORACLE pipeline and deliver live ideas + investment signals.

    Uses run_once() from oracle.main which executes the full graph:
    collectors -> synthesizer -> idea_generator<->critic (Reflexion) ->
    validator -> investment_analyzer -> formatter. The investment_analyzer
    reads the user's portfolio from oracle_data.db for personalized scenarios.
    """
    from ...main import run_once  # noqa: PLC0415 — local import avoids circular dep

    digest_id = uuid.uuid4().hex[:8]
    ctx.application.bot_data["current_digest_id"] = digest_id
    ctx.application.bot_data["idea_store"] = {}
    ctx.application.bot_data["signal_store"] = {}

    progress = await update.message.reply_text(
        f"📡 Running full ORACLE pipeline <code>#{digest_id}</code>...\n"
        f"<i>Collecting signals (market + scout + trend + custom) → "
        f"synthesizer → idea_generator ↔ critic (up to 3 rounds) → "
        f"validator → investment_analyzer.</i>\n"
        f"<i>This takes ~1-3 minutes. Your portfolio will be used for "
        f"personalized investment scenarios.</i>",
        parse_mode="HTML",
    )

    try:
        result = await run_once(thread_id=f"digest-{digest_id}")
    except Exception as e:  # noqa: BLE001
        log.exception("cmd_digest: pipeline failed")
        await progress.edit_text(
            f"⚠️ Pipeline failed: <code>{html_escape(str(e))}</code>\n\n"
            f"<i>Check bot logs for full traceback.</i>",
            parse_mode="HTML",
        )
        return

    final_digest = result.get("final_digest") or {}
    ideas_raw = final_digest.get("ideas") or []
    ideas_extra_raw = final_digest.get("ideas_extra") or []
    investments_raw = final_digest.get("investments") or []
    errors = result.get("errors") or []

    # Coerce dicts from graph state into Pydantic models for rendering
    ideas: list[BusinessIdea] = []
    for d in ideas_raw:
        try:
            ideas.append(BusinessIdea.model_validate(d))
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_digest: failed to parse idea %r: %s", d.get("title"), e)

    # Stash extras for /more (raw dicts — parsed lazily there)
    ctx.application.bot_data["ideas_pool"] = ideas_extra_raw
    ctx.application.bot_data["ideas_picked_industries"] = list({
        (i.industry or "other").lower() for i in ideas
    })

    signals: list[InvestmentSignal] = []
    for d in investments_raw:
        try:
            signals.append(InvestmentSignal.model_validate(d))
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_digest: failed to parse signal %r: %s", d.get("asset"), e)

    # Run-cost line (one-liner shown under the header)
    cost_line = ""
    try:
        from ...observability import get_last_run_cost  # noqa: PLC0415
        cost = await get_last_run_cost(f"digest-{digest_id}")
        if cost.get("total_calls"):
            cost_line = (
                f"\n💰 {cost['total_calls']} LLM calls · "
                f"${cost['total_cost_usd']:.4f} · "
                f"in={cost['total_input_tokens']} out={cost['total_output_tokens']} tokens"
            )
    except Exception as e:  # noqa: BLE001
        log.debug("cmd_digest: cost summary failed: %s", e)

    await progress.edit_text(
        f"✅ Pipeline done <code>#{digest_id}</code>\n"
        f"• {len(ideas)} business idea(s) survived\n"
        f"• {len(signals)} investment signal(s)\n"
        f"• {len(errors)} non-fatal source error(s)"
        f"{cost_line}",
        parse_mode="HTML",
    )

    # Ideas — one message per idea
    if ideas:
        for i, idea in enumerate(ideas, start=1):
            idea_id = f"i{i}_{digest_id}"
            _idea_store(ctx)[idea_id] = idea
            try:
                await update.message.reply_text(
                    render_idea_card(idea, i),
                    reply_markup=idea_buttons(idea_id),
                    parse_mode="HTML",
                )
            except Exception as e:  # noqa: BLE001
                log.warning("cmd_digest: render idea %d failed: %s", i, e)
    else:
        await update.message.reply_text(
            "📭 <i>No business ideas survived the critic this run. "
            "Try again later when signals refresh.</i>",
            parse_mode="HTML",
        )

    # Investment signals — one message per signal
    if signals:
        for i, sig in enumerate(signals, start=1):
            sig_id = f"s{i}_{digest_id}"
            _signal_store(ctx)[sig_id] = sig
            try:
                await update.message.reply_text(
                    render_investment_card(sig, i),
                    reply_markup=investment_buttons(sig_id),
                    parse_mode="HTML",
                )
            except Exception as e:  # noqa: BLE001
                log.warning("cmd_digest: render signal %d failed: %s", i, e)
    else:
        await update.message.reply_text(
            "📭 <i>No investment scenarios this run.</i>",
            parse_mode="HTML",
        )

    await update.message.reply_text(
        f"✅ Digest <code>#{digest_id}</code> delivered. "
        f"Tap 🔥 / ❌ / 📌 buttons to give feedback — every 20 feedbacks "
        f"triggers auto-calibration.",
        parse_mode="HTML",
    )


async def cmd_lastdigest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-show ideas from the current bot session's last digest.

    Useful when Maksim clicked 'Like' on a card and wants to scroll back to it
    (the toast ack doesn't remove the card anymore, but if bot restarted or
    chat scrolled away, this re-renders them).

    Reads `idea_store` and `signal_store` from bot_data — survives until bot
    restart. No new LLM calls.
    """
    store = _idea_store(ctx)
    if not store:
        await update.message.reply_text(
            "📭 <i>Не нашёл идей в кэше — возможно, бот перезапускался "
            "или digest ещё не запускался. Запусти /digest.</i>",
            parse_mode="HTML",
        )
        return

    from ..views import idea_buttons, render_idea_card  # noqa: PLC0415

    await update.message.reply_text(
        f"🔄 <b>Re-render: {len(store)} идей из последнего digest'а</b>",
        parse_mode="HTML",
    )
    for idea_id, idea in store.items():
        try:
            await update.message.reply_text(
                render_idea_card(idea, 0),
                reply_markup=idea_buttons(idea_id),
                parse_mode="HTML",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_lastdigest: render %s failed: %s", idea_id, e)


async def cmd_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Surface 3 MORE business ideas from this run's pool, prioritizing
    industries NOT yet shown. Reads `ideas_pool` + `ideas_picked_industries`
    populated by the previous /digest call.

    No new LLM calls — uses already-validated ideas that didn't make the
    top-3 cut due to diversity quota. Cheap (~0 cost).
    """
    pool_raw: list[dict] = ctx.application.bot_data.get("ideas_pool") or []
    seen_industries: set[str] = {
        i.lower() for i in ctx.application.bot_data.get("ideas_picked_industries") or []
    }
    if not pool_raw:
        await update.message.reply_text(
            "📭 <i>Нет дополнительных идей в пуле этого digest'а.\n"
            "Запусти новый /digest когда захочешь свежих.</i>",
            parse_mode="HTML",
        )
        return

    # Re-apply diversity selector against the pool, biased to new buckets.
    # Trick: feed the selector a copy of the pool but pre-seed seen_industries
    # by removing same-bucket candidates from contention.
    from ...agents.idea_generator import _normalize_industry, TECH_BUCKET  # noqa: PLC0415

    def _bucket(industry: str) -> str:
        return "TECH" if industry in TECH_BUCKET else industry

    seen_buckets = {_bucket(i) for i in seen_industries}

    # Normalize industry on every idea in pool
    for d in pool_raw:
        d["industry"] = _normalize_industry(d.get("industry"))

    # Sort by score same as diversity selector does
    def _score(d: dict) -> tuple[int, int, int, int]:
        verdict = (d.get("verdict") or "").upper()
        strong = 1 if verdict == "STRONG_PASS" else 0
        conf = int(d.get("confidence") or 0)
        rounds = int(d.get("reflexion_rounds_passed") or 0)
        tier = 1 if conf >= 60 else 0
        return (tier, strong, conf, rounds)

    pool_sorted = sorted(pool_raw, key=_score, reverse=True)

    picked_raw: list[dict] = []
    used_buckets: set[str] = set()
    # Pass 1: only NEW buckets (not in seen_buckets)
    for d in pool_sorted:
        b = _bucket(d.get("industry") or "other")
        if b in seen_buckets or b in used_buckets:
            continue
        picked_raw.append(d)
        used_buckets.add(b)
        if len(picked_raw) >= 3:
            break
    # Pass 2: if short, allow any bucket (still skipping previously-used in /digest)
    if len(picked_raw) < 3:
        picked_ids = {id(d) for d in picked_raw}
        for d in pool_sorted:
            if id(d) in picked_ids:
                continue
            b = _bucket(d.get("industry") or "other")
            if b in used_buckets:
                continue
            picked_raw.append(d)
            used_buckets.add(b)
            if len(picked_raw) >= 3:
                break

    if not picked_raw:
        await update.message.reply_text(
            "📭 <i>В пуле остались только идеи из тех же отраслей что уже показывал.\n"
            "Запусти свежий /digest для новых тем.</i>",
            parse_mode="HTML",
        )
        return

    # Render each via the standard idea card + register feedback buttons
    digest_id = ctx.application.bot_data.get("current_digest_id", "more")
    ideas: list[BusinessIdea] = []
    for d in picked_raw:
        try:
            ideas.append(BusinessIdea.model_validate(d))
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_more: failed to parse idea: %s", e)

    if not ideas:
        await update.message.reply_text("⚠️ Не удалось распарсить идеи из пула.")
        return

    # Update tracker so a SECOND /more skips these buckets too
    new_industries = list({(i.industry or "other").lower() for i in ideas})
    ctx.application.bot_data["ideas_picked_industries"] = list(
        set(ctx.application.bot_data.get("ideas_picked_industries") or []) | set(new_industries)
    )
    # Remove picked from pool so subsequent /more doesn't repeat
    picked_titles = {i.title for i in ideas}
    ctx.application.bot_data["ideas_pool"] = [
        d for d in pool_raw if d.get("title") not in picked_titles
    ]

    from ..views import idea_buttons, render_idea_card  # noqa: PLC0415

    await update.message.reply_text(
        f"🔄 <b>Ещё {len(ideas)} идей</b> из этого digest'а — другие отрасли.",
        parse_mode="HTML",
    )
    for i, idea in enumerate(ideas, start=1):
        idea_id = f"m{i}_{digest_id}"
        _idea_store(ctx)[idea_id] = idea
        try:
            await update.message.reply_text(
                render_idea_card(idea, i),
                reply_markup=idea_buttons(idea_id),
                parse_mode="HTML",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_more: render idea %d failed: %s", i, e)
