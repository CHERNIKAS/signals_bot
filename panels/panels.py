#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Панели управления сделками и их callback-обработчики"""

from typing import Optional
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes

# Локальные импорты для избежания циклов
from database import db_enabled, db_get_panel_message_id, db_set_panel_message_id


# =============================================================================
# TRADES PANEL HELPERS (single-message panel)
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: ensure_panel_message
# ОПИСАНИЕ: Проверяет наличие сообщения панели сделок, создает если нет
#           Возвращает ID сообщения для последующих обновлений
# ============================================================================
async def ensure_panel_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Ensure there is a single message for the trades panel for this user and return its id."""
    if not db_enabled():
        return None
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    pmid = db_get_panel_message_id(uid)
    print(f"[DEBUG] ensure_panel_message uid={uid} chat={chat_id} pmid={pmid}")
    if pmid:
        try:
            # Touch message to verify existence (light edit to remove keyboard)
            await context.bot.edit_message_text(chat_id=chat_id, message_id=pmid, text="Загрузка панели…")
            print(f"[DEBUG] ensure_panel_message touched message id={pmid}")
            return pmid
        except Exception:
            print(f"[DEBUG] ensure_panel_message: stored pmid invalid, will recreate")
    # create new message holder
    msg = await context.bot.send_message(chat_id=chat_id, text="Загрузка панели…")
    db_set_panel_message_id(uid, msg.message_id)
    print(f"[DEBUG] ensure_panel_message created new pmid={msg.message_id}")
    return msg.message_id
# ============ КОНЕЦ ФУНКЦИИ: ensure_panel_message ============


# ============================================================================
# ФУНКЦИЯ: recreate_panel_message
# ОПИСАНИЕ: Удаляет старое сообщение панели и создает новое внизу чата
#           Обновляет ID сообщения в БД
# ============================================================================
async def recreate_panel_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete existing panel message (if any) and create a fresh one at bottom.

    Returns new message_id and updates user_prefs.panel_message_id.
    """
    if not db_enabled():
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="Загрузка панели…")
        return m.message_id
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    pmid = db_get_panel_message_id(uid)
    if pmid:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=pmid)
            print(f"[DEBUG] recreate_panel_message deleted old pmid={pmid}")
        except Exception as e:
            print(f"[DEBUG] recreate_panel_message delete failed pmid={pmid}: {e}")
    m = await context.bot.send_message(chat_id=chat_id, text="Загрузка панели…")
    db_set_panel_message_id(uid, m.message_id)
    print(f"[DEBUG] recreate_panel_message created pmid={m.message_id}")
    return m.message_id
# ============ КОНЕЦ ФУНКЦИИ: recreate_panel_message ============


# ============================================================================
# ФУНКЦИЯ: edit_panel
# ОПИСАНИЕ: Редактирует существующее сообщение панели сделок
#           Если редактирование не удается, создает новое сообщение
# ============================================================================
async def edit_panel(context: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    if not db_enabled():
        return
    pmid = db_get_panel_message_id(uid)
    if pmid is None:
        # fallback: send new
        msg = await context.bot.send_message(chat_id=uid, text=text, reply_markup=kb, disable_web_page_preview=True)
        try:
            db_set_panel_message_id(uid, msg.message_id)
        except Exception:
            pass
        return
    try:
        await context.bot.edit_message_text(chat_id=uid, message_id=pmid, text=text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        # If edit fails (e.g., message deleted) — send a new one and update id
        try:
            print(f"[DEBUG] edit_panel failed for uid={uid}, pmid={pmid}: {e}")
            msg = await context.bot.send_message(chat_id=uid, text=text, reply_markup=kb, disable_web_page_preview=True)
            db_set_panel_message_id(uid, msg.message_id)
        except Exception as e2:
            print(f"[ERROR] edit_panel fallback failed for uid={uid}: {e2}")
# ============ КОНЕЦ ФУНКЦИИ: edit_panel ============


# ============================================================================
# ФУНКЦИЯ: panel_toggle_auto_close
# ОПИСАНИЕ: Обработчик кнопки переключения автозакрытия (функция отключена)
#           Показывает уведомление и обновляет панель сделок
# ============================================================================
async def panel_toggle_auto_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Feature removed
    try:
        await update.callback_query.answer("Функция отключена", show_alert=False)
    except Exception:
        pass
    # await _show_trades_panel(update, context)  # будет импортировано в handlers
# ============ КОНЕЦ ФУНКЦИИ: panel_toggle_auto_close ============


# =============================================================================
# TRADES PANEL – Callback Router
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: cb_trades_router
# ОПИСАНИЕ: Маршрутизатор callback-запросов для панели сделок
#           Обрабатывает все клики по кнопкам панели (открытие, закрытие, детали)
# ============================================================================
async def cb_trades_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    try:
        print(f"[DEBUG] cb_trades_router data='{data}' from user {update.effective_user.id}")
    except Exception:
        pass
    
    # Примечание: полная реализация роутера будет в handlers,
    # так как требует множество вспомогательных функций (_show_trades_panel, _show_symbol_trades и т.д.)
    # Здесь оставляем базовую структуру для экспорта
    
    if data in ("trades:", "trades:home", "home"):
        # Always answer callback to avoid Telegram UI spinner
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        # Refresh the panel - делегируется handlers
        # await _show_trades_panel(update, context)
        return
    
    if data.startswith("trades:symbol:"):
        # supports trades:symbol:ETHUSDT or trades:symbol:ETHUSDT:open:page=2
        parts = data.split(":")
        if len(parts) >= 3:
            symbol = parts[2]
            page = 1
            # look for page token
            if "page=" in data:
                try:
                    page = int(data.split("page=")[-1])
                except Exception:
                    page = 1
            print(f"[DEBUG] route -> _show_symbol_trades symbol={symbol} page={page}")
            # await _show_symbol_trades(update, context, symbol, page)
            return
    
    if data.startswith("trade:") and data.endswith(":close_manual"):
        trade_id = data.split(":")[1]
        # await _initiate_manual_close(update, context, trade_id)
        return
    
    if data.startswith("trade:") and data.endswith(":close_menu"):
        trade_id = int(data.split(":")[1])
        # await _show_close_trade_menu(update, context, trade_id)
        return
    
    if data.startswith("trade:") and ":close:" in data:
        # Handle partial close callbacks: trade:{id}:close:{type}
        parts = data.split(":")
        if len(parts) >= 4:
            trade_id = int(parts[1])
            close_type = parts[3]
            # await _handle_close_callback(update, context, trade_id, close_type, parts[4:] if len(parts) > 4 else [])
        return
    
    if data.startswith("trade:") and data.endswith(":details"):
        trade_id = data.split(":")[1]
        # await _show_trade_details(update, context, trade_id)
        return
    
    if data.startswith("trade:") and data.endswith(":csv"):
        trade_id = data.split(":")[1]
        # await _export_trade_csv(update, context, trade_id)
        return
    
    if data == "prefs:auto_close:toggle":
        await panel_toggle_auto_close(update, context)
        return
    
    if data == "back:symbols":
        # await _show_trades_panel(update, context)
        return
    
    # fallback to existing on_cb routing for other buttons
    try:
        await update.callback_query.answer()
    except Exception:
        pass
# ============ КОНЕЦ ФУНКЦИИ: cb_trades_router ============


# =============================================================================
# TRADE CLOSING FUNCTIONS
# =============================================================================

async def _show_close_trade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id: int):
    """Show close trade menu with options"""
    from database import _db_conn
    from utils import _fmt_price
    from telegram import InlineKeyboardButton
    from telegram.constants import ParseMode
    
    uid = update.effective_user.id
    
    # Load trade data
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, side, amount_usdt, entry_price_user, tp1, tp2, sl, timeframe,
                   remaining_qty, fee_rate_bps
            FROM user_trades 
            WHERE id=%s AND user_id=%s AND status='ACTIVE'
        """, (trade_id, uid))
        trade = cur.fetchone()
    
    if not trade:
        await update.effective_message.reply_text("Сделка не найдена или уже закрыта.")
        return
    
    trade_id, symbol, side, amount_usdt, entry_price, tp1, tp2, sl, timeframe, remaining_qty, fee_rate_bps = trade
    
    # Calculate remaining amount in USDT
    remaining_amount_usdt = remaining_qty * entry_price
    
    side_icon = '🟢' if side == 'LONG' else '🔴'
    entry_fmt = _fmt_price(entry_price, entry_price, symbol)
    sl_fmt = _fmt_price(sl, sl, symbol) if sl else "—"
    tp1_fmt = _fmt_price(tp1, tp1, symbol) if tp1 else "—"
    tp2_fmt = _fmt_price(tp2, tp2, symbol) if tp2 else "—"
    
    text = (
        f"Закрытие сделки #{trade_id}\n\n"
        f"{side_icon} <b>{symbol}</b> {side} • {timeframe}\n"
        f"Сумма: {amount_usdt:,.0f} USDT\n"
        f"Вход (user): {entry_fmt}\n"
        f"SL: {sl_fmt}   TP1/TP2: {tp1_fmt} / {tp2_fmt}\n\n"
        f"Выберите вариант закрытия:"
    )
    
    # Create inline keyboard
    keyboard = []
    
    # Add close options if levels exist
    if tp1:
        keyboard.append([InlineKeyboardButton("🎯 Закрытие по TP1", callback_data=f"trade:{trade_id}:close:tp1")])
    if tp2:
        keyboard.append([InlineKeyboardButton("🎯 Закрытие по TP2", callback_data=f"trade:{trade_id}:close:tp2")])
    if sl:
        keyboard.append([InlineKeyboardButton("🧱 Закрытие по SL", callback_data=f"trade:{trade_id}:close:sl")])
    
    keyboard.append([InlineKeyboardButton("✅ Закрыть всю сделку", callback_data=f"trade:{trade_id}:close:full")])
    keyboard.append([InlineKeyboardButton("✏️ Ввести свою цену", callback_data=f"trade:{trade_id}:close:custom")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:trades")])
    
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _show_close_size_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id: int, close_type: str, exit_price: float = None):
    """Show size selection menu (amount vs quantity)"""
    from database import _db_conn
    from telegram import InlineKeyboardButton
    from telegram.constants import ParseMode
    
    uid = update.effective_user.id
    
    # Load trade data
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT symbol, side, entry_price_user, remaining_qty, fee_rate_bps
            FROM user_trades 
            WHERE id=%s AND user_id=%s AND status='ACTIVE'
        """, (trade_id, uid))
        trade = cur.fetchone()
    
    if not trade:
        await update.effective_message.reply_text("Сделка не найдена.")
        return
    
    symbol, side, entry_price, remaining_qty, fee_rate_bps = trade
    # Ensure plain floats for math
    entry_price = float(entry_price or 0)
    remaining_qty = float(remaining_qty or 0)
    fee_rate_bps = float(fee_rate_bps or 0)
    remaining_amount_usdt = remaining_qty * entry_price
    
    # Store close context
    context.user_data["close_trade_context"] = {
        "trade_id": trade_id,
        "close_type": close_type,
        "exit_price": exit_price,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "remaining_qty": remaining_qty,
        "remaining_amount_usdt": remaining_amount_usdt,
        "fee_rate_bps": fee_rate_bps
    }
    
    text = (
        f"Как указать размер закрытия?\n\n"
        f"Доступно к закрытию: {remaining_amount_usdt:,.2f} USDT (≈ {remaining_qty:.8f} {symbol.replace('USDT', '')})"
    )
    
    keyboard = [
        [InlineKeyboardButton("💵 По сумме (USDT)", callback_data=f"trade:{trade_id}:close:mode:amount")],
        [InlineKeyboardButton("🪙 По количеству", callback_data=f"trade:{trade_id}:close:mode:qty")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"trade:{trade_id}:close_menu")]
    ]
    
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _show_close_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id: int, close_type: str, exit_price: float, size_mode: str, size_value: float):
    """Show close confirmation with calculations"""
    from database import _db_conn
    from utils import _fmt_price
    from jobs import _calculate_pnl_gross, _calculate_fee_close, _calculate_pnl_net
    from telegram import InlineKeyboardButton
    from telegram.constants import ParseMode
    
    ctx = context.user_data.get("close_trade_context", {})
    if not ctx:
        await update.effective_message.reply_text("Ошибка: контекст закрытия не найден.")
        return
    
    # Ensure required fields exist in context
    required_keys = ("symbol", "side", "entry_price", "remaining_qty", "fee_rate_bps")
    if any(k not in ctx for k in required_keys):
        uid = update.effective_user.id
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, entry_price_user, remaining_qty, fee_rate_bps
                FROM user_trades
                WHERE id=%s AND user_id=%s
            """, (trade_id, uid))
            row = cur.fetchone()
        if not row:
            await update.effective_message.reply_text("Сделка не найдена.")
            return
        symbol_db, side_db, entry_price_db, remaining_qty_db, fee_rate_bps_db = row
        ctx.setdefault("symbol", symbol_db)
        ctx.setdefault("side", side_db)
        ctx.setdefault("entry_price", float(entry_price_db or 0))
        ctx.setdefault("remaining_qty", float(remaining_qty_db or 0))
        ctx.setdefault("fee_rate_bps", float(fee_rate_bps_db or 0))
        ctx.setdefault("remaining_amount_usdt", float(ctx["remaining_qty"]) * float(ctx["entry_price"]))
        context.user_data["close_trade_context"] = ctx
    
    symbol = ctx["symbol"]
    side = ctx["side"]
    entry_price = float(ctx["entry_price"])
    remaining_qty = float(ctx["remaining_qty"])
    remaining_amount_usdt = float(ctx.get("remaining_amount_usdt", remaining_qty * entry_price))
    fee_rate_bps = float(ctx["fee_rate_bps"])
    
    # Ensure all values are floats
    size_value = float(size_value)
    exit_price = float(exit_price)
    
    # Calculate quantities based on size mode
    if size_mode == "amount":
        close_amount_usdt = size_value
        close_qty = close_amount_usdt / entry_price
    else:
        close_qty = size_value
        close_amount_usdt = close_qty * entry_price
    
    close_qty = float(close_qty)
    close_amount_usdt = float(close_amount_usdt)
    
    # Calculate PnL and fees
    pnl_gross = _calculate_pnl_gross(entry_price, exit_price, close_amount_usdt, side)
    fee_close = _calculate_fee_close(exit_price, close_qty, fee_rate_bps)
    pnl_net = _calculate_pnl_net(pnl_gross, fee_close)
    
    # Calculate remaining after close
    new_remaining_qty = float(remaining_qty) - float(close_qty)
    new_remaining_amount_usdt = float(new_remaining_qty) * float(entry_price)
    
    # Format prices
    exit_fmt = _fmt_price(exit_price, exit_price, symbol)
    close_qty_fmt = f"{close_qty:.8f}"
    close_amount_fmt = f"{close_amount_usdt:,.2f}"
    remaining_qty_fmt = f"{new_remaining_qty:.8f}"
    remaining_amount_fmt = f"{new_remaining_amount_usdt:,.2f}"
    
    # Type emoji
    type_emoji = {
        "tp1": "🎯",
        "tp2": "🎯", 
        "sl": "🧱",
        "full": "✅",
        "custom": "✏️"
    }.get(close_type, "✏️")
    
    text = (
        f"Подтвердите закрытие:\n\n"
        f"Тип: {type_emoji} {close_type.upper()}\n"
        f"Цена выхода: {exit_fmt}\n"
        f"Размер: {close_amount_fmt} USDT (≈ {close_qty_fmt} {symbol.replace('USDT', '')})\n\n"
        f"Комиссия (оценка): {fee_close:.2f} USDT\n"
        f"PnL (грязный): {pnl_gross:+.2f} USDT\n"
        f"PnL (чистый): {pnl_net:+.2f} USDT\n"
        f"Останется открыто: {remaining_amount_fmt} USDT (≈ {remaining_qty_fmt} {symbol.replace('USDT', '')})"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"trade:{trade_id}:close:confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"trade:{trade_id}:close_menu")]
    ]
    
    # Store final close data
    context.user_data["close_final_data"] = {
        "trade_id": trade_id,
        "close_type": close_type,
        "exit_price": exit_price,
        "close_qty": close_qty,
        "close_amount_usdt": close_amount_usdt,
        "pnl_gross": pnl_gross,
        "fee_close": fee_close,
        "pnl_net": pnl_net,
        "size_mode": size_mode,
        "size_value": size_value
    }
    
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _execute_partial_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute the partial close operation"""
    from database import _db_conn
    from utils import _fmt_price
    from telegram import InlineKeyboardButton
    from telegram.constants import ParseMode
    
    close_data = context.user_data.get("close_final_data")
    if not close_data:
        await update.effective_message.reply_text("Ошибка: данные закрытия не найдены.")
        return
    
    trade_id = close_data["trade_id"]
    close_type = close_data["close_type"]
    exit_price = close_data["exit_price"]
    close_qty = close_data["close_qty"]
    close_amount_usdt = close_data["close_amount_usdt"]
    pnl_gross = close_data["pnl_gross"]
    fee_close = close_data["fee_close"]
    pnl_net = close_data["pnl_net"]
    
    uid = update.effective_user.id
    
    try:
        with _db_conn() as c, c.cursor() as cur:
            # Start transaction
            cur.execute("BEGIN")
            
            # Get current trade data
            cur.execute("""
                SELECT remaining_qty, pnl_gross_total, fee_close_total, pnl_net_total, symbol, side, timeframe
                FROM user_trades 
                WHERE id=%s AND user_id=%s AND status='ACTIVE'
            """, (trade_id, uid))
            trade = cur.fetchone()
            
            if not trade:
                cur.execute("ROLLBACK")
                await update.effective_message.reply_text("Сделка не найдена или уже закрыта.")
                return
            
            current_remaining_qty, current_pnl_gross_total, current_fee_close_total, current_pnl_net_total, symbol, side, timeframe = trade
            
            # Normalize to floats
            current_remaining_qty = float(current_remaining_qty or 0)
            current_pnl_gross_total = float(current_pnl_gross_total or 0)
            current_fee_close_total = float(current_fee_close_total or 0)
            current_pnl_net_total = float(current_pnl_net_total or 0)
            close_qty = float(close_qty or 0)
            exit_price = float(exit_price or 0)
            pnl_gross = float(pnl_gross or 0)
            fee_close = float(fee_close or 0)
            pnl_net = float(pnl_net or 0)

            # Validate remaining quantity
            if close_qty > current_remaining_qty:
                cur.execute("ROLLBACK")
                await update.effective_message.reply_text("Недостаточно количества для закрытия.")
                return
            
            # Calculate new remaining quantity
            new_remaining_qty = current_remaining_qty - close_qty
            
            # Update trade totals
            new_pnl_gross_total = current_pnl_gross_total + pnl_gross
            new_fee_close_total = current_fee_close_total + fee_close
            new_pnl_net_total = current_pnl_net_total + pnl_net
            
            # Determine if trade should be closed completely
            new_status = 'CLOSED' if new_remaining_qty <= 1e-10 else 'ACTIVE'
            
            # Update trade
            if new_status == 'CLOSED':
                cur.execute("""
                    UPDATE user_trades 
                    SET remaining_qty = %s,
                        pnl_gross_total = %s,
                        fee_close_total = %s,
                        pnl_net_total = %s,
                        status = %s,
                        closed_at = now()
                    WHERE id = %s
                """, (new_remaining_qty, new_pnl_gross_total, new_fee_close_total, new_pnl_net_total, new_status, trade_id))
            else:
                cur.execute("""
                    UPDATE user_trades 
                    SET remaining_qty = %s,
                        pnl_gross_total = %s,
                        fee_close_total = %s,
                        pnl_net_total = %s,
                        status = %s
                    WHERE id = %s
                """, (new_remaining_qty, new_pnl_gross_total, new_fee_close_total, new_pnl_net_total, new_status, trade_id))
            
            cur.execute("COMMIT")
            
            # Send confirmation message
            side_icon = '🟢' if side == 'LONG' else '🔴'
            exit_fmt = _fmt_price(exit_price, exit_price, symbol)
            close_qty_fmt = f"{close_qty:.8f}"
            close_amount_fmt = f"{close_amount_usdt:,.2f}"
            
            if new_status == 'CLOSED':
                text = (
                    f"Сделка полностью закрыта ✅\n\n"
                    f"{side_icon} <code>{symbol}</code> {side} • {timeframe}\n"
                    f"Выход: {exit_fmt}\n"
                    f"Размер: {close_amount_fmt} USDT (≈ {close_qty_fmt} {symbol.replace('USDT', '')})\n\n"
                    f"PnL (грязный): {pnl_gross:+.2f} USDT\n"
                    f"Комиссия: {fee_close:.2f} USDT\n"
                    f"PnL (чистый): {pnl_net:+.2f} USDT\n\n"
                    f"#{trade_id} • {symbol} • {side} • {timeframe}"
                )
            else:
                remaining_qty_fmt = f"{new_remaining_qty:.8f}"
                remaining_amount_usdt = new_remaining_qty * exit_price
                remaining_amount_fmt = f"{remaining_amount_usdt:,.2f}"
                
                text = (
                    f"Частичное закрытие ✅\n\n"
                    f"{side_icon} <code>{symbol}</code> {side} • {timeframe}\n"
                    f"Выход: {exit_fmt}\n"
                    f"Закрыто: {close_amount_fmt} USDT (≈ {close_qty_fmt} {symbol.replace('USDT', '')})\n\n"
                    f"PnL (грязный): {pnl_gross:+.2f} USDT\n"
                    f"Комиссия: {fee_close:.2f} USDT\n"
                    f"PnL (чистый): {pnl_net:+.2f} USDT\n\n"
                    f"Осталось открыто: {remaining_amount_fmt} USDT (≈ {remaining_qty_fmt} {symbol.replace('USDT', '')})\n\n"
                    f"#{trade_id} • {symbol} • {side} • {timeframe}"
                )
            
            await update.effective_message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить панель", callback_data="trades:")]])
            )
            
            # Clean up context
            context.user_data.pop("close_trade_context", None)
            context.user_data.pop("close_final_data", None)
            
    except Exception as e:
        print(f"[ERROR] _execute_partial_close failed: {e}")
        await update.effective_message.reply_text(f"Ошибка при закрытии сделки: {e}")

