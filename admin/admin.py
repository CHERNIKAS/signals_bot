#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Админ-панель для управления пользователями и их парами"""

from typing import List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ForceReply
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import CONFIG
from database import (
    db_enabled, _db_conn,
    db_user_pairs, db_add_pair, db_rm_pair,
    db_list_all_symbols,
)
from keyboards import _kb_main, ADMIN_USER_IDS


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_message(update: Update):
    """Get message from update"""
    return update.message or update.effective_message


def db_search_users(query: str) -> List[dict]:
    """Поиск пользователей по username или user_id"""
    with _db_conn() as c, c.cursor() as cur:
        # Если query - число, ищем по ID
        if query.strip().isdigit():
            cur.execute("""
                SELECT user_id, username, created_at
                FROM users
                WHERE user_id = %s
            """, (int(query.strip()),))
        else:
            # Иначе по username (убираем @ если есть)
            username = query.strip().lstrip('@')
            cur.execute("""
                SELECT user_id, username, created_at
                FROM users
                WHERE username ILIKE %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (f"%{username}%",))
        
        return [{"user_id": r[0], "username": r[1], "created_at": r[2]} for r in cur.fetchall()]


def db_get_user_info(uid: int) -> dict:
    """Получить информацию о пользователе"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT user_id, username, created_at, timezone
            FROM users
            WHERE user_id = %s
        """, (uid,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "username": row[1],
            "created_at": row[2],
            "timezone": row[3] if len(row) > 3 else "Europe/Istanbul"
        }


# =============================================================================
# MAIN ADMIN PANEL
# =============================================================================

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show simplified admin panel - only user pairs management"""
    print(f"[DEBUG] show_admin_panel called for user {update.effective_user.id}")
    
    # Проверка прав доступа
    if update.effective_user.id not in ADMIN_USER_IDS:
        await _get_message(update).reply_text("Доступ запрещён.")
        return
    
    if not db_enabled():
        print("[DEBUG] DB not enabled")
        await _get_message(update).reply_text("Админ-панель доступна только с БД.")
        return
    
    text = (
        "🛠 <b>Админ: Управление парами</b>\n\n"
        "Выберите действие:\n"
        "• 👥 <b>Пользователи</b> — открыть профиль и управлять парами"
    )
    
    keyboard = [
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin:back")]
    ]
    
    await _get_message(update).reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


# =============================================================================
# ADMIN CALLBACK ROUTER
# =============================================================================

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handle admin panel callbacks - full routing for pairs management"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.callback_query.answer("Доступ запрещён", show_alert=True)
        return
    
    # Parse callback data
    parts = data.split(":")
    
    try:
        # Main navigation
        if data == "admin:home":
            await show_admin_panel(update, context)
        
        elif data == "admin:back":
            # Return to main menu
            await update.effective_message.delete()
            await update.effective_message.reply_text("Главное меню", reply_markup=_kb_main(update.effective_user.id))
        
        elif data == "admin:users":
            await show_admin_users(update, context)
        
        elif data.startswith("admin:users:recent?page="):
            page = int(data.split("=")[1])
            await show_admin_users(update, context, page)
        
        elif data == "admin:users:search":
            # ForceReply for search
            context.user_data['admin_action'] = 'search_user'
            await update.effective_message.reply_text(
                "🔍 <b>Поиск пользователя</b>\n\nВведите @username или user_id:",
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(selective=True)
            )
        
        # User profile
        elif data.startswith("admin:user:open:"):
            uid = int(parts[3])
            await show_admin_user_profile(update, context, uid)
        
        elif ":pairs:page=" in data:
            # admin:user:123:pairs:page=2
            uid = int(parts[2])
            page = int(data.split("=")[1])
            await show_admin_user_profile(update, context, uid, page)
        
        # Add pairs
        elif data.endswith(":pairs:add:start"):
            uid = int(parts[2])
            context.user_data['admin_selected_pairs'] = set()
            await show_admin_add_pairs_catalog(update, context, uid)
        
        elif ":pairs:add:list?page=" in data:
            uid = int(parts[2])
            page = int(data.split("=")[1])
            await show_admin_add_pairs_catalog(update, context, uid, page)
        
        elif ":pairs:add:pick:" in data:
            # Toggle symbol selection
            uid = int(parts[2])
            symbol = parts[5]
            if 'admin_selected_pairs' not in context.user_data:
                context.user_data['admin_selected_pairs'] = set()
            
            if symbol in context.user_data['admin_selected_pairs']:
                context.user_data['admin_selected_pairs'].remove(symbol)
            else:
                context.user_data['admin_selected_pairs'].add(symbol)
            
            # Refresh catalog (extract page if exists)
            page = 1
            if "?page=" in data:
                page = int(data.split("=")[1])
            await show_admin_add_pairs_catalog(update, context, uid, page)
        
        elif data.endswith(":pairs:add:input"):
            uid = int(parts[2])
            context.user_data['admin_action'] = f'add_pairs_text:{uid}'
            await update.effective_message.reply_text(
                "⌨️ <b>Добавить пары</b>\n\nВведите символы через пробел или запятую:\n<code>BTCUSDT ETHUSDT SOLUSDT</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(selective=True)
            )
        
        elif data.endswith(":pairs:add:save"):
            uid = int(parts[2])
            await handle_admin_add_pairs_save(update, context, uid)
        
        elif data.endswith(":pairs:add:cancel"):
            uid = int(parts[2])
            context.user_data['admin_selected_pairs'] = set()
            await show_admin_user_profile(update, context, uid)
        
        # Delete pairs
        elif data.endswith(":pairs:del:start"):
            uid = int(parts[2])
            context.user_data['admin_delete_pairs'] = set()
            await show_admin_del_pairs_list(update, context, uid)
        
        elif ":pairs:del:list?page=" in data:
            uid = int(parts[2])
            page = int(data.split("=")[1])
            await show_admin_del_pairs_list(update, context, uid, page)
        
        elif ":pairs:del:pick:" in data:
            # Toggle symbol for deletion
            uid = int(parts[2])
            symbol = parts[5]
            if 'admin_delete_pairs' not in context.user_data:
                context.user_data['admin_delete_pairs'] = set()
            
            if symbol in context.user_data['admin_delete_pairs']:
                context.user_data['admin_delete_pairs'].remove(symbol)
            else:
                context.user_data['admin_delete_pairs'].add(symbol)
            
            # Refresh list
            page = 1
            if "?page=" in data:
                page = int(data.split("=")[1])
            await show_admin_del_pairs_list(update, context, uid, page)
        
        elif data.endswith(":pairs:del:all"):
            uid = int(parts[2])
            all_pairs = set(db_user_pairs(uid))
            context.user_data['admin_delete_pairs'] = all_pairs
            await show_admin_del_pairs_list(update, context, uid)
        
        elif data.endswith(":pairs:del:none"):
            uid = int(parts[2])
            context.user_data['admin_delete_pairs'] = set()
            await show_admin_del_pairs_list(update, context, uid)
        
        elif data.endswith(":pairs:del:input"):
            uid = int(parts[2])
            context.user_data['admin_action'] = f'del_pairs_text:{uid}'
            await update.effective_message.reply_text(
                "⌨️ <b>Удалить пары</b>\n\nВведите символы через пробел или запятую:\n<code>BTCUSDT ETHUSDT</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(selective=True)
            )
        
        elif data.endswith(":pairs:del:confirm"):
            uid = int(parts[2])
            await handle_admin_del_pairs_confirm(update, context, uid)
        
        elif data.endswith(":pairs:del:cancel"):
            uid = int(parts[2])
            context.user_data['admin_delete_pairs'] = set()
            await show_admin_user_profile(update, context, uid)
        
        # Export pairs
        elif data.endswith(":pairs:export"):
            uid = int(parts[2])
            await handle_admin_export_pairs(update, context, uid)
        
        # No-op (for pagination counters)
        elif data == "noop":
            await update.callback_query.answer()
        
        else:
            # Unknown callback
            print(f"[WARN] Unknown admin callback: {data}")
            await update.callback_query.answer("Неизвестная команда", show_alert=True)
    
    except Exception as e:
        print(f"[ERROR] Admin callback handler failed: {e}")
        import traceback
        traceback.print_exc()
        await update.callback_query.answer("Ошибка обработки", show_alert=True)


# =============================================================================
# ADMIN USERS LIST
# =============================================================================

async def show_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    """Show users list with pagination"""
    per_page = 15
    offset = (page - 1) * per_page
    
    with _db_conn() as c, c.cursor() as cur:
        # Get total count
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        
        # Get users for current page
        cur.execute("""
            SELECT user_id, username, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        users = cur.fetchall()
    
    total_pages = (total + per_page - 1) // per_page
    
    text = f"👥 <b>Пользователи</b> (стр. {page}/{total_pages}, всего {total})\n\n"
    
    keyboard = []
    
    if users:
        for user_id, username, created_at in users:
            username_str = f"@{username}" if username else "—"
            date_str = created_at.strftime('%d.%m.%y') if created_at else "—"
            btn_text = f"{user_id} • {username_str} • {date_str}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin:user:open:{user_id}")])
    
    # Pagination buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin:users:recent?page={page-1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin:users:recent?page={page+1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    # Action buttons
    keyboard.append([InlineKeyboardButton("🔍 Поиск", callback_data="admin:users:search")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")])
    
    await update.effective_message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


# =============================================================================
# USER PROFILE
# =============================================================================

async def show_admin_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, pairs_page: int = 1):
    """Show user profile with pairs"""
    user_info = db_get_user_info(uid)
    if not user_info:
        await update.effective_message.edit_text(
            "❌ Пользователь не найден",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:users")]])
        )
        return
    
    pairs = db_user_pairs(uid)
    total_pairs = len(pairs)
    
    # Pagination for pairs
    per_page = 30
    total_pages = (total_pairs + per_page - 1) // per_page if total_pairs > 0 else 1
    start_idx = (pairs_page - 1) * per_page
    end_idx = start_idx + per_page
    pairs_slice = pairs[start_idx:end_idx]
    
    username = user_info['username'] or f"User_{uid}"
    text = f"👤 <b>{username}</b> (id: <code>{uid}</code>)\n\n"
    text += f"Пар: <b>{total_pairs}</b>\n\n"
    
    if pairs_slice:
        # Display pairs in compact format
        pairs_text = " • ".join(pairs_slice)
        text += f"{pairs_text}\n"
        
        # Pagination info
        if total_pages > 1:
            text += f"\n<i>Стр. {pairs_page}/{total_pages}</i>"
    else:
        text += "<i>Нет пар</i>"
    
    keyboard = []
    
    # Pagination for pairs
    if total_pages > 1:
        nav_row = []
        if pairs_page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin:user:{uid}:pairs:page={pairs_page-1}"))
        if pairs_page < total_pages:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin:user:{uid}:pairs:page={pairs_page+1}"))
        keyboard.append(nav_row)
    
    # Action buttons
    keyboard.append([
        InlineKeyboardButton("➕ Добавить пары", callback_data=f"admin:user:{uid}:pairs:add:start"),
        InlineKeyboardButton("➖ Удалить пары", callback_data=f"admin:user:{uid}:pairs:del:start")
    ])
    keyboard.append([
        InlineKeyboardButton("⬇️ Экспорт CSV", callback_data=f"admin:user:{uid}:pairs:export")
    ])
    keyboard.append([InlineKeyboardButton("⬅️ К пользователям", callback_data="admin:users")])
    
    await update.effective_message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


# =============================================================================
# ADD PAIRS
# =============================================================================

async def show_admin_add_pairs_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, page: int = 1):
    """Show catalog of available symbols for adding"""
    all_symbols = db_list_all_symbols()
    user_pairs = set(db_user_pairs(uid))
    
    # Available symbols (not yet added)
    available = [s for s in all_symbols if s not in user_pairs]
    
    # Get selected symbols from context
    if 'admin_selected_pairs' not in context.user_data:
        context.user_data['admin_selected_pairs'] = set()
    selected = context.user_data['admin_selected_pairs']
    
    per_page = 24
    total = len(available)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    symbols_slice = available[start_idx:end_idx]
    
    text = f"➕ <b>Добавить пары</b> (стр. {page}/{total_pages})\n\n"
    text += f"Доступно: {total} • Выбрано: {len(selected)}\n\n"
    text += "<i>Нажмите на символ для выбора</i>"
    
    keyboard = []
    
    # Symbol grid (4 columns)
    row = []
    for symbol in symbols_slice:
        mark = "✅ " if symbol in selected else ""
        btn = InlineKeyboardButton(f"{mark}{symbol}", callback_data=f"admin:user:{uid}:pairs:add:pick:{symbol}")
        row.append(btn)
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    # Pagination
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin:user:{uid}:pairs:add:list?page={page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin:user:{uid}:pairs:add:list?page={page+1}"))
    keyboard.append(nav_row)
    
    # Actions
    keyboard.append([
        InlineKeyboardButton("⌨️ Ввести текстом", callback_data=f"admin:user:{uid}:pairs:add:input"),
    ])
    if selected:
        keyboard.append([InlineKeyboardButton(f"✅ Сохранить ({len(selected)})", callback_data=f"admin:user:{uid}:pairs:add:save")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"admin:user:{uid}:pairs:add:cancel")])
    
    await update.effective_message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def handle_admin_add_pairs_save(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Save selected pairs"""
    selected = context.user_data.get('admin_selected_pairs', set())
    
    if not selected:
        await update.callback_query.answer("Ничего не выбрано", show_alert=True)
        return
    
    current_pairs = db_user_pairs(uid)
    limit = MAX_PAIRS_PER_USER
    
    if len(current_pairs) + len(selected) > limit:
        await update.callback_query.answer(f"❌ Превышен лимит ({limit} пар)", show_alert=True)
        return
    
    # Add pairs
    added = 0
    for symbol in selected:
        try:
            db_add_pair(uid, symbol)
            added += 1
        except Exception as e:
            print(f"[ERROR] Failed to add pair {symbol}: {e}")
    
    # Clear selection
    context.user_data['admin_selected_pairs'] = set()
    
    await update.callback_query.answer(f"✅ Добавлено: {added}", show_alert=False)
    
    # Return to profile
    await show_admin_user_profile(update, context, uid)


# =============================================================================
# DELETE PAIRS
# =============================================================================

async def show_admin_del_pairs_list(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, page: int = 1):
    """Show user's pairs for deletion with checkboxes"""
    user_pairs = db_user_pairs(uid)
    
    if not user_pairs:
        await update.effective_message.edit_text(
            "❌ У пользователя нет пар",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:user:open:{uid}")]])
        )
        return
    
    # Get selected pairs for deletion from context
    if 'admin_delete_pairs' not in context.user_data:
        context.user_data['admin_delete_pairs'] = set()
    selected = context.user_data['admin_delete_pairs']
    
    per_page = 24
    total = len(user_pairs)
    total_pages = (total + per_page - 1) // per_page
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    pairs_slice = user_pairs[start_idx:end_idx]
    
    text = f"➖ <b>Удалить пары</b> (стр. {page}/{total_pages})\n\n"
    text += f"Всего пар: {total} • К удалению: {len(selected)}\n\n"
    text += "<i>Нажмите на пару для выбора</i>"
    
    keyboard = []
    
    # Pairs grid (4 columns)
    row = []
    for symbol in pairs_slice:
        mark = "☑️ " if symbol in selected else ""
        btn = InlineKeyboardButton(f"{mark}{symbol}", callback_data=f"admin:user:{uid}:pairs:del:pick:{symbol}")
        row.append(btn)
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    # Pagination
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin:user:{uid}:pairs:del:list?page={page-1}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin:user:{uid}:pairs:del:list?page={page+1}"))
        keyboard.append(nav_row)
    
    # Select all/none
    keyboard.append([
        InlineKeyboardButton("✅ Все", callback_data=f"admin:user:{uid}:pairs:del:all"),
        InlineKeyboardButton("❌ Снять все", callback_data=f"admin:user:{uid}:pairs:del:none")
    ])
    
    # Action buttons
    keyboard.append([
        InlineKeyboardButton("⌨️ Ввести текстом", callback_data=f"admin:user:{uid}:pairs:del:input")
    ])
    if selected:
        keyboard.append([InlineKeyboardButton(f"🗑 Удалить ({len(selected)})", callback_data=f"admin:user:{uid}:pairs:del:confirm")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"admin:user:{uid}:pairs:del:cancel")])
    
    await update.effective_message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


async def handle_admin_del_pairs_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Confirm and delete selected pairs"""
    selected = context.user_data.get('admin_delete_pairs', set())
    
    if not selected:
        await update.callback_query.answer("Ничего не выбрано", show_alert=True)
        return
    
    # Delete pairs
    deleted = 0
    for symbol in selected:
        try:
            if db_rm_pair(uid, symbol):
                deleted += 1
        except Exception as e:
            print(f"[ERROR] Failed to delete pair {symbol}: {e}")
    
    # Clear selection
    context.user_data['admin_delete_pairs'] = set()
    
    await update.callback_query.answer(f"✅ Удалено: {deleted}", show_alert=False)
    
    # Return to profile
    await show_admin_user_profile(update, context, uid)


# =============================================================================
# EXPORT
# =============================================================================

async def handle_admin_export_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Export user pairs to CSV"""
    user_info = db_get_user_info(uid)
    if not user_info:
        await update.callback_query.answer("Пользователь не найден", show_alert=True)
        return
    
    pairs = db_user_pairs(uid)
    
    if not pairs:
        await update.callback_query.answer("Нет пар для экспорта", show_alert=True)
        return
    
    # Create CSV
    import io
    import csv
    from datetime import datetime
    
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['user_id', 'username', 'symbol'])
    
    username = user_info['username'] or ''
    for symbol in sorted(pairs):
        writer.writerow([uid, username, symbol])
    
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    fname = f"user_pairs_{uid}_{date_str}.csv"
    
    await update.callback_query.answer("Генерация CSV...", show_alert=False)
    
    # Send CSV
    await update.effective_message.reply_document(
        document=InputFile(data, filename=fname),
        caption=f"📊 Пары пользователя {username} (ID: {uid})\nВсего: {len(pairs)}"
    )


# =============================================================================
# TEXT INPUT HANDLERS
# =============================================================================

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_action: str, text: str):
    """Handle admin text inputs (search, add/delete pairs by text)"""
    from utils import normalize_symbol
    
    if admin_action == 'search_user':
        # Search user by username or ID
        users = db_search_users(text)
        if not users:
            await update.message.reply_text("❌ Пользователь не найден")
            return
        
        if len(users) == 1:
            # Direct to profile
            uid = users[0]['user_id']
            msg = await update.message.reply_text("Загрузка профиля...")
            
            # Create fake update for edit
            fake_update = update
            fake_update.effective_message = msg
            fake_update.callback_query = type('obj', (object,), {'answer': lambda *a, **k: None})()
            
            await show_admin_user_profile(fake_update, context, uid)
        else:
            # Show search results
            text_msg = f"🔍 <b>Результаты поиска</b> ({len(users)})\n\n"
            keyboard = []
            for u in users:
                uid = u['user_id']
                username_str = f"@{u['username']}" if u['username'] else "—"
                date_str = u['created_at'].strftime('%d.%m.%y') if u['created_at'] else "—"
                btn_text = f"{uid} • {username_str} • {date_str}"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin:user:open:{uid}")])
            
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:users")])
            
            await update.message.reply_text(
                text_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
    
    elif admin_action.startswith('add_pairs_text:'):
        uid = int(admin_action.split(':')[1])
        
        # Parse symbols from text
        import re
        symbols_raw = re.split(r'[\s,]+', text.strip())
        symbols = []
        for s in symbols_raw:
            if s:
                normalized = normalize_symbol(s)
                if normalized:
                    symbols.append(normalized)
        
        if not symbols:
            await update.message.reply_text("❌ Не удалось распознать символы")
            return
        
        # Add to selection
        if 'admin_selected_pairs' not in context.user_data:
            context.user_data['admin_selected_pairs'] = set()
        context.user_data['admin_selected_pairs'].update(symbols)
        
        # Show catalog with selected
        msg = await update.message.reply_text("Добавляю к выбранным...")
        fake_update = update
        fake_update.effective_message = msg
        await show_admin_add_pairs_catalog(fake_update, context, uid)
    
    elif admin_action.startswith('del_pairs_text:'):
        uid = int(admin_action.split(':')[1])
        
        # Parse symbols from text
        import re
        symbols_raw = re.split(r'[\s,]+', text.strip())
        symbols = []
        for s in symbols_raw:
            if s:
                normalized = normalize_symbol(s)
                if normalized:
                    symbols.append(normalized)
        
        if not symbols:
            await update.message.reply_text("❌ Не удалось распознать символы")
            return
        
        # Add to deletion selection
        if 'admin_delete_pairs' not in context.user_data:
            context.user_data['admin_delete_pairs'] = set()
        context.user_data['admin_delete_pairs'].update(symbols)
        
        # Show deletion list with selected
        msg = await update.message.reply_text("Добавляю к удалению...")
        fake_update = update
        fake_update.effective_message = msg
        await show_admin_del_pairs_list(fake_update, context, uid)

