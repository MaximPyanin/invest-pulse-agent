"""/portfolio, /add_holding, /remove_holding + the three portfolio
ConversationHandler flows (add-to-portfolio, adjust +$/-$, add-new-position)."""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ...agents.market import collect_market_data
from ...models import InvestmentSignal
from ...services.portfolio import (
    TRACKABLE_PORTFOLIO_TICKERS,
    add_or_update_holding,
    add_usd_holding,
    adjust_usd_invested,
    display_name,
    get_portfolio_with_pnl,
    is_trackable_for_portfolio,
    list_holdings,
    remove_holding,
    validate_asset_label,
)
from ._common import _signal_store, html_escape

log = logging.getLogger(__name__)

# Add-to-portfolio ConversationHandler states (Step 20.5 — Maksim's request)
# Entered via "➕ В мой портфель" inline button under each investment card.
#
#   click button         →  ASK_HOLDING_QTY  (text input, supports "usd 1500")
#   user sends quantity  →  ASK_HOLDING_PRICE (text input, or "-" for current)
#   user sends price     →  END (insert into portfolio_holdings)
#   /cancel              →  END (clears draft)
ASK_HOLDING_QTY, ASK_HOLDING_PRICE = range(100, 102)

# Portfolio adjust (+$/-$) ConversationHandler
#   Click 💰 +$ or 💸 -$ → ASK_ADJUST_AMOUNT (text input)
#   User sends number → adjust_usd_invested → END
ASK_ADJUST_AMOUNT = 200

# Portfolio "add new position from scratch" ConversationHandler
#   Click ➕ Добавить новую позицию → PICK_NEW_TICKER (inline keyboard with whitelist)
#   Pick ticker → ASK_NEW_AMOUNT (text input)
#   User sends $ amount → add_usd_holding → END
PICK_NEW_TICKER, ASK_NEW_AMOUNT = range(300, 302)


def _holding_draft(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.user_data.setdefault("pending_holding", {})


def _clear_holding_draft(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("pending_holding", None)


async def add_holding_callback_entry(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Entry point — callback ^inv_add_<sig_id>$ pressed by Maksim.

    Looks up the InvestmentSignal in bot_data to get the asset_label and the
    current market price (used as default if user skips the price prompt),
    then asks for quantity.
    """
    query = update.callback_query
    await query.answer()

    sig_id = (query.data or "")[len("inv_add_"):]
    store = _signal_store(ctx)
    sig: InvestmentSignal | None = store.get(sig_id)
    if sig is None:
        await query.message.reply_text(
            "⚠️ Сигнал не найден в текущем дайджесте (возможно, бот перезапускался). "
            "Добавь позицию руками: <code>/add_holding TICKER QTY PRICE</code>",
            parse_mode="HTML",
        )
        return ConversationHandler.END


    # Whitelist check: only the 11 tickers we actually track are addable
    if not is_trackable_for_portfolio(sig.asset):
        await query.message.reply_text(
            f"⚠️ <b>{display_name(sig.asset)}</b> не в списке отслеживаемых для портфеля.\n\n"
            f"Бот трекает реальные позиции — добавлять можно только: "
            f"<code>CSPX, SMH, NATO, NUCL, EXH1, IB1T, ETH-CORE, IB01, CASH-USD, GOLD-PHYS, NVDA</code>.\n\n"
            f"Если хочешь экспозицию на {display_name(sig.asset)} — найди соответствующий "
            f"UCITS-ETF в твоём списке и добавь его.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    draft = _holding_draft(ctx)
    draft.clear()
    draft["sig_id"] = sig_id
    draft["asset_label"] = sig.asset
    draft["current_price"] = float(sig.price) if sig.price else 0.0
    draft["signal_type"] = sig.signal_type

    await query.message.reply_text(
        f"➕ <b>Добавляем {display_name(sig.asset)} в портфель</b>\n\n"
        f"Цена сейчас: <code>${sig.price:,.2f}</code>\n\n"
        f"Сколько штук купил?\n"
        f"• Число → <code>10</code> (10 штук)\n"
        f"• Или сумма в USD → <code>usd 1500</code> (для ETF без точного количества)\n\n"
        f"<i>Отмена: /cancel</i>",
        parse_mode="HTML",
    )
    return ASK_HOLDING_QTY


async def add_holding_qty(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_HOLDING_QTY → ASK_HOLDING_PRICE — parse quantity or USD amount."""
    draft = _holding_draft(ctx)
    text = (update.message.text or "").strip().lower().replace(",", ".")

    if text.startswith("usd "):
        # USD-only mode
        try:
            usd_amount = float(text[4:].strip())
        except ValueError:
            await update.message.reply_text(
                "⚠️ Не могу прочитать сумму. Пример: <code>usd 1500</code>",
                parse_mode="HTML",
            )
            return ASK_HOLDING_QTY
        if usd_amount <= 0:
            await update.message.reply_text("⚠️ Сумма должна быть > 0.")
            return ASK_HOLDING_QTY
        draft["mode"] = "usd"
        draft["usd_invested"] = usd_amount
        current = draft.get("current_price") or 0.0
        await update.message.reply_text(
            f"💰 Сумма: <code>${usd_amount:,.2f}</code>\n\n"
            f"По какой средней цене входил?\n"
            f"• Число → <code>{current:.2f}</code> например\n"
            f"• Или <code>-</code> чтобы взять текущую (${current:,.2f}) — "
            f"P&L начнётся с 0%\n\n"
            f"<i>Отмена: /cancel</i>",
            parse_mode="HTML",
        )
        return ASK_HOLDING_PRICE

    # Plain quantity mode
    try:
        qty = float(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Не могу прочитать число. Пример: <code>10</code> или <code>usd 1500</code>",
            parse_mode="HTML",
        )
        return ASK_HOLDING_QTY
    if qty <= 0:
        await update.message.reply_text("⚠️ Количество должно быть > 0.")
        return ASK_HOLDING_QTY
    draft["mode"] = "qty"
    draft["quantity"] = qty
    current = draft.get("current_price") or 0.0
    await update.message.reply_text(
        f"📦 Количество: <code>{qty:g}</code>\n\n"
        f"По какой средней цене входил? (USD)\n"
        f"• Число → <code>{current:.2f}</code>\n"
        f"• Или <code>-</code> чтобы взять текущую (${current:,.2f})\n\n"
        f"<i>Отмена: /cancel</i>",
        parse_mode="HTML",
    )
    return ASK_HOLDING_PRICE


async def add_holding_price(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """ASK_HOLDING_PRICE → END — parse price, insert into DB, confirm."""
    draft = _holding_draft(ctx)
    text = (update.message.text or "").strip().replace(",", ".")
    current_price = draft.get("current_price") or 0.0

    if text in ("-", ""):
        price = current_price
    else:
        try:
            price = float(text)
        except ValueError:
            await update.message.reply_text(
                "⚠️ Не могу прочитать цену. Пример: <code>185.50</code> или <code>-</code>",
                parse_mode="HTML",
            )
            return ASK_HOLDING_PRICE
        if price < 0:
            await update.message.reply_text("⚠️ Цена должна быть ≥ 0.")
            return ASK_HOLDING_PRICE

    asset_label = draft.get("asset_label", "?")
    mode = draft.get("mode", "qty")
    dname = display_name(asset_label)

    try:
        if mode == "usd":
            from ...services.portfolio import add_usd_holding  # noqa: PLC0415
            usd_inv = float(draft.get("usd_invested") or 0.0)
            await add_usd_holding(
                asset_label=asset_label,
                usd_invested=usd_inv,
                price_at_add=price if price > 0 else None,
                notes=f"added via inline button on signal {draft.get('sig_id', '?')}",
            )
            await update.message.reply_text(
                f"✅ Добавлено: <b>{dname}</b> — "
                f"${usd_inv:,.2f} @ ${price:,.2f}\n\n"
                f"<i>Посмотри полный портфель: /portfolio</i>",
                parse_mode="HTML",
            )
        else:
            qty = float(draft.get("quantity") or 0.0)
            await add_or_update_holding(
                asset_label=asset_label,
                quantity=qty,
                buy_price_usd=price,
                notes=f"added via inline button on signal {draft.get('sig_id', '?')}",
                price_at_add=price if price > 0 else None,
            )
            await update.message.reply_text(
                f"✅ Добавлено: <b>{dname}</b> — "
                f"{qty:g} шт @ ${price:,.2f}\n\n"
                f"<i>Посмотри полный портфель: /portfolio</i>",
                parse_mode="HTML",
            )
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ Не получилось: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("add_holding via callback failed")
        await update.message.reply_text(
            f"⚠️ Ошибка: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )

    _clear_holding_draft(ctx)
    return ConversationHandler.END


async def add_holding_cancel_cmd(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """/cancel inside the add-holding flow."""
    _clear_holding_draft(ctx)
    await update.message.reply_text(
        "❌ Отменено. Позиция не добавлена."
    )
    return ConversationHandler.END



async def pf_adjust_callback_entry(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Entry: pf_addusd_<LABEL> or pf_subusd_<LABEL>."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data.startswith("pf_addusd_"):
        mode = "add"
        label = data[len("pf_addusd_"):]
    elif data.startswith("pf_subusd_"):
        mode = "sub"
        label = data[len("pf_subusd_"):]
    else:
        return ConversationHandler.END

    ctx.user_data["pf_adjust"] = {"label": label, "mode": mode}

    verb = "Добавить" if mode == "add" else "Снять"
    sign = "+" if mode == "add" else "-"
    await query.message.reply_text(
        f"{sign}💰 <b>{verb} деньги: {display_name(label)}</b>\n\n"
        f"Сколько USD?\n"
        f"• Число → <code>500</code> ({verb.lower()} $500)\n\n"
        f"<i>Отмена: /cancel</i>",
        parse_mode="HTML",
    )
    return ASK_ADJUST_AMOUNT


async def pf_adjust_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse amount, call adjust_usd_invested, confirm."""
    draft = ctx.user_data.get("pf_adjust") or {}
    label = draft.get("label", "")
    mode = draft.get("mode", "add")

    text = (update.message.text or "").strip().replace(",", ".").replace("$", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Не могу прочитать число. Пример: <code>500</code>",
            parse_mode="HTML",
        )
        return ASK_ADJUST_AMOUNT
    if amount <= 0:
        await update.message.reply_text("⚠️ Сумма должна быть > 0.")
        return ASK_ADJUST_AMOUNT

    delta = amount if mode == "add" else -amount

    try:
        updated = await adjust_usd_invested(label, delta)
    except Exception as e:  # noqa: BLE001
        log.exception("pf_adjust failed")
        await update.message.reply_text(
            f"⚠️ Ошибка: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
        ctx.user_data.pop("pf_adjust", None)
        return ConversationHandler.END

    if updated is None and mode == "sub":
        # Final would be negative → offer to remove
        await update.message.reply_text(
            f"⚠️ Снятие ${amount:,.0f} опустит позицию ниже нуля.\n"
            f"Нажми 🗑️ <b>Удалить</b> в /portfolio чтобы убрать её полностью, "
            f"или попробуй снять меньшую сумму.",
            parse_mode="HTML",
        )
        ctx.user_data.pop("pf_adjust", None)
        return ConversationHandler.END

    if updated is None:
        await update.message.reply_text("⚠️ Позиция не найдена.")
        ctx.user_data.pop("pf_adjust", None)
        return ConversationHandler.END

    new_usd = float(updated.get("usd_invested") or 0)
    verb = "добавлено" if mode == "add" else "снято"
    await update.message.reply_text(
        f"✅ <b>{display_name(label)}</b>: {verb} ${amount:,.0f}\n"
        f"💰 Текущая позиция: ${new_usd:,.0f}\n\n"
        f"<i>Применится в следующем /morning и /digest.</i>",
        parse_mode="HTML",
    )
    ctx.user_data.pop("pf_adjust", None)
    return ConversationHandler.END


async def pf_adjust_cancel_cmd(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """/cancel inside the +/- adjust flow."""
    ctx.user_data.pop("pf_adjust", None)
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END



async def pf_addnew_entry(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Entry: '➕ Добавить новую позицию' inline button.

    Shows a keyboard with all whitelist tickers NOT yet in portfolio.
    """

    from ...services.portfolio import (  # noqa: PLC0415
        display_name,
        list_holdings,
    )

    query = update.callback_query
    await query.answer()

    existing = {h["asset_label"].upper() for h in await list_holdings()}
    addable = [t for t in TRACKABLE_PORTFOLIO_TICKERS if t.upper() not in existing]

    if not addable:
        await query.message.reply_text(
            "📭 <i>Все 11 трекаемых тикеров уже в портфеле. "
            "Удалить ненужный через 🗑️ или используй 💸 -$ чтобы обнулить.</i>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Build keyboard 2 per row for readability
    buttons = [
        InlineKeyboardButton(display_name(t), callback_data=f"pf_pick_{t}")
        for t in addable
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="pf_pick_cancel")])

    await query.message.reply_text(
        "➕ <b>Выбери тикер для новой позиции:</b>\n\n"
        "<i>Только из whitelist — это активы, которые мы реально трекаем "
        "(цены, новости, P&amp;L).</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PICK_NEW_TICKER


async def pf_addnew_pick(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """User picked a ticker → ask for $ amount."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "pf_pick_cancel":
        ctx.user_data.pop("pf_new", None)
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    label = data[len("pf_pick_"):]

    if not is_trackable_for_portfolio(label):
        await query.edit_message_text(
            f"⚠️ <code>{label}</code> не в whitelist'е. Отменено.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    ctx.user_data["pf_new"] = {"label": label}
    await query.message.reply_text(
        f"➕ <b>Новая позиция: {display_name(label)}</b>\n\n"
        f"Сколько USD ты вложил?\n"
        f"• Число → <code>500</code> ($500)\n"
        f"• Или <code>0</code> если хочешь только трекать без денег "
        f"(например физ. золото)\n\n"
        f"<i>Отмена: /cancel</i>",
        parse_mode="HTML",
    )
    return ASK_NEW_AMOUNT


async def pf_addnew_amount(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Parse amount, INSERT, confirm."""
    draft = ctx.user_data.get("pf_new") or {}
    label = draft.get("label", "")
    if not label:
        await update.message.reply_text("⚠️ Сессия потеряна. Запусти /portfolio заново.")
        return ConversationHandler.END

    text = (update.message.text or "").strip().replace(",", ".").replace("$", "")
    try:
        usd = float(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Не могу прочитать число. Пример: <code>500</code>",
            parse_mode="HTML",
        )
        return ASK_NEW_AMOUNT
    if usd < 0:
        await update.message.reply_text("⚠️ Сумма должна быть ≥ 0.")
        return ASK_NEW_AMOUNT

    from ...agents.market import collect_market_data  # noqa: PLC0415
    from ...services.portfolio import (  # noqa: PLC0415
        _lookup_current_price,
    )

    # Capture entry-price snapshot so P&L drift works
    price_at_add = None
    try:
        market_data, _ = await collect_market_data()
        price_at_add = _lookup_current_price(label, market_data)
    except Exception as e:  # noqa: BLE001
        log.debug("pf_addnew: market fetch failed: %s", e)

    try:
        await add_usd_holding(
            asset_label=label,
            usd_invested=usd,
            price_at_add=price_at_add,
            notes=f"added via /portfolio ➕ button",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("pf_addnew: insert failed")
        await update.message.reply_text(
            f"⚠️ Ошибка: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
        ctx.user_data.pop("pf_new", None)
        return ConversationHandler.END

    ctx.user_data.pop("pf_new", None)
    price_str = f" @ ${price_at_add:,.2f}" if price_at_add else ""
    await update.message.reply_text(
        f"✅ <b>{display_name(label)}</b> добавлена в портфель: "
        f"${usd:,.0f}{price_str}\n\n"
        f"<i>Появится в следующем /morning и /digest. "
        f"Посмотри: /portfolio</i>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def pf_addnew_cancel_cmd(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> int:
    ctx.user_data.pop("pf_new", None)
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


async def _handle_pf_remove(
    ctx: ContextTypes.DEFAULT_TYPE,
    query: Any,
    label: str,
) -> None:
    """🗑️ Удалить — drop position from portfolio_holdings."""
    from ...services.portfolio import remove_holding  # noqa: PLC0415

    removed = await remove_holding(label)
    if removed:
        await query.edit_message_text(
            f"🗑️ <b>{display_name(label)}</b> удалена из портфеля.\n"
            f"<i>В следующем /morning и /digest её больше не будет.</i>",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            f"⚠️ Позиция <b>{label}</b> не найдена (возможно, уже удалена).",
            parse_mode="HTML",
        )



def _fmt_money(v: float | None, *, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"${v:,.{decimals}f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _render_portfolio_html(portfolio: dict) -> str:
    """Render the /portfolio Telegram card."""
    holdings = portfolio.get("holdings") or []
    totals = portfolio.get("totals") or {}
    by_class = portfolio.get("by_class") or {}

    if not holdings:
        return (
            "📊 <b>Your portfolio is empty</b>\n\n"
            "Add a position with:\n"
            "<code>/add_holding BTC 0.15 52000 long-term</code>\n"
            "<code>/add_holding NVDA 12 480</code>\n"
            "<code>/add_holding GOLD 5 2100 hedge</code>\n"
            "<code>/add_holding CASH_USD 5000 1</code>\n\n"
            "<i>Asset labels must match what ORACLE tracks. "
            "Use /add_holding without args to see the full list.</i>"
        )

    tot_mv = totals.get("market_value_usd", 0.0)
    tot_cost = totals.get("cost_basis_usd", 0.0)
    tot_pnl = totals.get("unrealized_pnl_usd", 0.0)
    tot_pct = totals.get("pnl_pct", 0.0)
    pnl_emoji = "📈" if tot_pnl >= 0 else "📉"

    parts: list[str] = [
        "📊 <b>Your portfolio</b>\n",
        f"<b>Total value:</b> {_fmt_money(tot_mv, decimals=0)}",
        f"<b>Cost basis:</b> {_fmt_money(tot_cost, decimals=0)}",
        f"<b>Unrealized P&amp;L:</b> {pnl_emoji} {_fmt_money(tot_pnl, decimals=0)} ({_fmt_pct(tot_pct)})",
        "",
    ]

    if by_class:
        parts.append("<b>By asset class:</b>")
        for cls, b in sorted(
            by_class.items(),
            key=lambda kv: kv[1].get("market_value_usd", 0),
            reverse=True,
        ):
            mv = b.get("market_value_usd", 0)
            cnt = int(b.get("count", 0))
            pct = (mv / tot_mv * 100) if tot_mv else 0
            parts.append(f"• {cls}: {_fmt_money(mv, decimals=0)} ({pct:.0f}%) · {cnt} pos")
        parts.append("")


    parts.append("<b>Positions:</b>")
    for h in holdings:
        label = h["asset_label"]
        dname = display_name(label)
        qty = h["quantity"] or 0
        avg = h["avg_buy_price"] or 0
        cur = h.get("current_price")
        mv = h.get("market_value_usd")
        pnl_pct = h.get("pnl_pct")
        status = h.get("price_status", "?")
        usd_inv = h.get("usd_invested") or 0
        isin = h.get("isin")
        notes = h.get("notes") or ""

        if status == "cash":
            cash_amt = float(usd_inv) if usd_inv else float(qty)
            line = f"• <b>{dname}</b>: {_fmt_money(cash_amt, decimals=2)}"
        elif status == "live":
            line = (
                f"• <b>{dname}</b>: {qty:g} @ {_fmt_money(avg)} avg → "
                f"now {_fmt_money(cur)} · MV {_fmt_money(mv, decimals=0)} · {_fmt_pct(pnl_pct)}"
            )
        elif status == "usd_drift":
            entry = h.get("price_at_add") or 0
            line = (
                f"• <b>{dname}</b>: {_fmt_money(usd_inv, decimals=0)} invested → "
                f"now {_fmt_money(mv, decimals=0)} ({_fmt_pct(pnl_pct)} vs entry @ {_fmt_money(entry)})"
            )
        elif status == "usd_basis":
            line = (
                f"• <b>{dname}</b>: {_fmt_money(usd_inv, decimals=0)} invested "
                f"<i>(no entry-price snapshot — P&L unknown)</i>"
            )
        else:
            # Fallback for stale qty-mode rows (no live price)
            if usd_inv and not qty:
                line = (
                    f"• <b>{dname}</b>: {_fmt_money(usd_inv, decimals=0)} invested "
                    f"<i>(no live price)</i>"
                )
            else:
                line = (
                    f"• <b>{dname}</b>: {qty:g} @ {_fmt_money(avg)} avg "
                    f"<i>(no live price)</i>"
                )
        if isin:
            line += f" <code>{html_escape(isin)}</code>"
        if notes:
            line += f" <i>({html_escape(notes)})</i>"
        parts.append(line)

    parts.append("")
    parts.append(
        "<i>Investment analyzer will use these positions to personalize signals "
        "in your next /digest.</i>"
    )
    return "\n".join(parts)


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current holdings with live P&L + per-holding management buttons.

    Always reads FRESH data from the DB. Morning brief and digest also
    re-read from DB on every run, so manual edits via these buttons are
    picked up immediately by the next /morning or /digest.
    """

    from ...services.portfolio import display_name  # noqa: PLC0415

    await update.message.reply_text(
        "📡 Подгружаю свежие цены...",
        parse_mode="HTML",
    )
    try:
        market_data, errors = await collect_market_data()
        if errors:
            log.warning("portfolio: market collection had %d errors", len(errors))
        portfolio = await get_portfolio_with_pnl(market_data)
    except Exception as e:  # noqa: BLE001
        log.exception("cmd_portfolio failed")
        await update.message.reply_text(
            f"⚠️ Could not load portfolio: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    # 1. Aggregate overview
    await update.message.reply_text(
        _render_portfolio_html(portfolio),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # 2. Per-holding management cards (compact)
    holdings = portfolio.get("holdings") or []
    if not holdings:
        return

    for h in holdings:
        label = h["asset_label"]
        dname = display_name(label)
        usd_inv = h.get("usd_invested") or 0
        mv = h.get("market_value_usd") or usd_inv
        pnl_pct = h.get("pnl_pct")
        c24 = h.get("change_24h_pct")

        sign24 = "+" if (c24 or 0) >= 0 else ""
        live_24h = f"{sign24}{c24:.1f}%" if c24 is not None else "—"
        sign_pnl = "+" if (pnl_pct or 0) >= 0 else ""
        pnl_str = f"{sign_pnl}{pnl_pct:.1f}% от входа" if pnl_pct is not None else ""

        line = f"<b>{html_escape(dname)}</b>"
        if usd_inv:
            line += f"\n💰 ${usd_inv:,.0f} invested"
        if mv and mv != usd_inv:
            line += f" · сейчас ${mv:,.0f}"
        line += f"\n📊 24h: {live_24h}"
        if pnl_str:
            line += f" · {pnl_str}"

        # Inline buttons row
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 +$", callback_data=f"pf_addusd_{label}"),
                InlineKeyboardButton("💸 -$", callback_data=f"pf_subusd_{label}"),
            ],
            [
                InlineKeyboardButton("🗑️ Удалить", callback_data=f"pf_remove_{label}"),
            ],
        ])
        try:
            await update.message.reply_text(line, parse_mode="HTML", reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            log.warning("cmd_portfolio: render %s failed: %s", label, e)

    # Footer + always-show "Add new position" button
    existing = {(h.get("asset_label") or "").upper() for h in holdings}
    addable = [t for t in TRACKABLE_PORTFOLIO_TICKERS if t.upper() not in existing]

    legend_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"➕ Добавить новую позицию ({len(addable)} доступно)",
            callback_data="pf_addnew",
        )],
    ])

    await update.message.reply_text(
        "<i>💰 +$ — добавить денег к позиции\n"
        "💸 -$ — снять деньги с позиции\n"
        "🗑️ — удалить позицию полностью\n"
        "➕ — добавить новый тикер из whitelist\n\n"
        "Изменения сразу попадают в /morning и /digest.</i>",
        parse_mode="HTML",
        reply_markup=legend_kb,
    )

ADD_HOLDING_USAGE = (
    "💼 <b>/add_holding</b> — add or update a position\n\n"
    "<b>Usage:</b>\n"
    "<code>/add_holding ASSET QUANTITY BUY_PRICE_USD [notes]</code>\n\n"
    "<b>Examples:</b>\n"
    "• <code>/add_holding BTC 0.15 52000 long-term hold</code>\n"
    "• <code>/add_holding NVDA 12 480 AI exposure</code>\n"
    "• <code>/add_holding GOLD 5 2100 hedge</code>\n"
    "• <code>/add_holding OIL_WTI 10 75 swing trade</code>\n"
    "• <code>/add_holding EURPLN 1000 4.30 vacation fund</code>\n"
    "• <code>/add_holding CASH_USD 5000 1</code>\n\n"
    "<b>If you re-add an asset, the avg buy price is weighted-averaged.</b>\n\n"
    "<b>Tracked assets:</b>\n"
    "<i>Crypto:</i> BTC, ETH, SOL, XRP\n"
    "<i>Mega-cap + AI stocks:</i> NVDA, MSFT, GOOGL, AAPL, TSLA, AMD, META, AMZN, "
    "TSM, AVGO, NFLX, PLTR, SMCI, ARM, ASML, MU, VRT, SOUN, AI\n"
    "<i>Nuclear:</i> SMR, OKLO, NNE, LEU, CCJ, BWXT, UEC, URA, VST, CEG\n"
    "<i>Drones &amp; defense:</i> AVAV, KTOS, RCAT, ONDS, RKLB, EH, UMAC\n"
    "<i>Trump/political:</i> DJT, RUM, PSQH, PHUN\n"
    "<i>ETFs:</i> SPY, QQQ, XLK, XLE, XLF, VNQ\n"
    "<i>Commodities:</i> GOLD, SILVER, OIL_WTI, OIL_BRENT, NATGAS, COPPER, WHEAT\n"
    "<i>Forex:</i> EURUSD, USDPLN, EURPLN, USDBYN, EURBYN, USDRUB, DXY\n"
    "<i>Cash:</i> CASH_USD, CASH_EUR, CASH_PLN, CASH_BYN"
)


async def cmd_add_holding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Add or update a portfolio position."""
    args = ctx.args or []
    if len(args) < 3:
        await update.message.reply_text(ADD_HOLDING_USAGE, parse_mode="HTML")
        return

    asset_label = args[0].upper().strip()
    try:
        quantity = float(args[1].replace(",", "."))
        buy_price = float(args[2].replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "⚠️ QUANTITY and BUY_PRICE must be numbers.\n\n" + ADD_HOLDING_USAGE,
            parse_mode="HTML",
        )
        return

    notes = " ".join(args[3:]) if len(args) > 3 else None


    ok, asset_class_or_err = validate_asset_label(asset_label)
    if not ok:
        await update.message.reply_text(
            f"⚠️ Unknown asset <code>{html_escape(asset_label)}</code>.\n\n"
            f"{ADD_HOLDING_USAGE}",
            parse_mode="HTML",
        )
        return

    if not is_trackable_for_portfolio(asset_label):
        await update.message.reply_text(
            f"⚠️ <code>{asset_label}</code> не в whitelist'е портфеля.\n\n"
            f"Бот трекает реальные позиции — добавлять можно только:\n"
            f"<code>CSPX, SMH, NATO, NUCL, EXH1, IB1T, ETH-CORE, IB01, "
            f"CASH-USD, GOLD-PHYS, NVDA</code>",
            parse_mode="HTML",
        )
        return

    if quantity <= 0:
        await update.message.reply_text("⚠️ Quantity must be > 0.")
        return
    if buy_price < 0:
        await update.message.reply_text("⚠️ Buy price must be ≥ 0.")
        return

    try:
        row = await add_or_update_holding(
            asset_label=asset_label,
            quantity=quantity,
            buy_price_usd=buy_price,
            notes=notes,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("add_holding failed")
        await update.message.reply_text(
            f"⚠️ Could not add holding: <code>{html_escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    final_qty = float(row.get("quantity", quantity))
    final_avg = float(row.get("avg_buy_price", buy_price))
    cls = row.get("asset_class", asset_class_or_err)
    notes_str = row.get("notes") or ""

    msg = (
        f"✅ <b>Position saved</b>\n\n"
        f"<b>{asset_label}</b> [{cls}]\n"
        f"Quantity: <b>{final_qty:g}</b>\n"
        f"Avg buy: <b>{_fmt_money(final_avg)}</b>\n"
        f"Cost basis: <b>{_fmt_money(final_qty * final_avg, decimals=0)}</b>\n"
    )
    if notes_str:
        msg += f"Notes: <i>{html_escape(notes_str)}</i>\n"
    msg += "\n<i>Use /portfolio to see live P&amp;L.</i>"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_remove_holding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a portfolio position by asset label."""
    args = ctx.args or []
    if len(args) != 1:
        # Show list of current holdings if no arg
        rows = await list_holdings()
        if not rows:
            await update.message.reply_text(
                "📭 No holdings to remove. Add one with /add_holding first.",
                parse_mode="HTML",
            )
            return
        labels = ", ".join(f"<code>{r['asset_label']}</code>" for r in rows)
        await update.message.reply_text(
            "💼 <b>/remove_holding</b> — drop a position\n\n"
            "<b>Usage:</b> <code>/remove_holding ASSET</code>\n\n"
            f"<b>Your positions:</b> {labels}",
            parse_mode="HTML",
        )
        return

    asset_label = args[0].upper().strip()
    removed = await remove_holding(asset_label)
    if removed:
        await update.message.reply_text(
            f"✅ Removed <b>{asset_label}</b> from your portfolio.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"⚠️ No position with label <code>{html_escape(asset_label)}</code>. "
            f"Use /portfolio to see your current positions.",
            parse_mode="HTML",
        )

