#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Клавиатуры и кнопки для Telegram"""

import os

from telegram import KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup


# =============================================================================
# КОНСТАНТЫ КНОПОК
# =============================================================================

BTN_MY_DATA     = "📇ТВОИ ДАННЫЕ" 
BTN_TF_SETTINGS  = "⚙️НАСТРОЙКА ТФ"
BTN_TF_ANALYSIS = "➕/➖ ТФ ДЛЯ АНАЛИЗА"
BTN_TF_SIGNAL   = "➕/➖ ТФ ДЛЯ СИГНАЛА"
BTN_TF_MODE     = "🔀 РЕЖИМ СИГНАЛОВ"
BTN_TF_DONE     = "✅ НАСТРОЙКА ЗАВЕРШЕНА"
BTN_BACK        = "⬅️ НАЗАД"
BTN_LEVELS       = "🧮УРОВНИ"
BTN_STATS        = "📊СТАТИСТИКА"
BTN_U_STATS      = "📊 Статистика ваших сделок"
BTN_TRADES       = "🧾 Ваши сделки"
BTN_ADMIN        = "🛠 Админ"
BTN_STATS_D      = "День"
BTN_STATS_W      = "Неделя"
BTN_STATS_M      = "Месяц"
BTN_STATS_CSV    = "📥 CSV отчёт"
BTN_STATS_BY_SYMBOL = "📊 По символам"
BTN_STATS_BY_TF = "⏰ По таймфреймам"
BTN_STATS_BY_SESSION = "🌍 По торговым сессиям"
BTN_CLEAR_STATS = "🗑️ Очистить статистику"
BTN_CLEAR_DAY = "Очистить за день"
BTN_CLEAR_WEEK = "Очистить за неделю"
BTN_CLEAR_MONTH = "Очистить за месяц"
BTN_NOTIFICATIONS = "🔔 Уведомления"  # deprecated
BTN_NOTIF_STATS = "📊 Статистика"     # deprecated
BTN_NOTIF_DRAWDOWN = "📉 Просадка"    # deprecated
BTN_NOTIF_WEEKLY = "📅 Еженедельные"  # deprecated
BTN_TIMEZONE = "🌍 Часовой пояс"

# TPmid submenu buttons
BTN_TPMID_MENU        = "TPmid"
BTN_TPMID_TOGGLE      = "Вкл/Выкл TPmid"
BTN_TPMID_AUTO_TOGGLE = "Вкл/Выкл авто-режим"
BTN_TPMID_SETTINGS    = "Настройка TPmid"
BTN_TPMID_RESET       = "Сброс настроек"

BTN_SESS_START   = "🟢НАЧАЛО СЕССИИ"
BTN_SESS_END     = "🔴КОНЕЦ СЕССИИ"
BTN_PAIR_TOGGLE = "➕ДОБАВИТЬ/➖УДАЛИТЬ ПАРУ"

# Торговые сессии
TRADING_SESSIONS = {
    "asian": {"name": "🇨🇳 Азиатская", "start": 0, "end": 8},
    "european": {"name": "🇪🇺 Европейская", "start": 8, "end": 16}, 
    "american": {"name": "🇺🇸 Американская", "start": 16, "end": 24}
}

# Админы (управление парами пользователей)
_DEFAULT_ADMIN_USER_IDS = {481638710}
_env_admin_ids = set()
for _raw_uid in os.getenv("ADMIN_USER_IDS", "").split(","):
    _raw_uid = _raw_uid.strip()
    if not _raw_uid:
        continue
    if _raw_uid.isdigit():
        _env_admin_ids.add(int(_raw_uid))
    else:
        print(f"[WARN] Invalid ADMIN_USER_IDS value ignored: {_raw_uid}")

ADMIN_USER_IDS = _DEFAULT_ADMIN_USER_IDS | _env_admin_ids
MAX_PAIRS_PER_USER = 200  # Лимит пар на пользователя


# =============================================================================
# ГЕНЕРАТОРЫ КЛАВИАТУР
# =============================================================================

def _kb_main(user_id=None):
    keyboard = [
        [KeyboardButton(BTN_SESS_START),  KeyboardButton(BTN_SESS_END)],
        [KeyboardButton(BTN_PAIR_TOGGLE), KeyboardButton(BTN_TF_SETTINGS)],
        [KeyboardButton(BTN_MY_DATA),     KeyboardButton(BTN_LEVELS)],
        [KeyboardButton(BTN_STATS),       KeyboardButton(BTN_TRADES)],
        [KeyboardButton(BTN_U_STATS)],
        [KeyboardButton(BTN_TIMEZONE)],
    ]
    
    # Добавляем кнопку админа только для админов
    if user_id in ADMIN_USER_IDS:
        keyboard[4].append(KeyboardButton(BTN_ADMIN))  # Добавляем к BTN_U_STATS в том же ряду
    
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _kb_stats():
    # Подменю статистики: День / Неделя / Месяц + CSV + Назад
    rows = [
        [BTN_STATS_D, BTN_STATS_W, BTN_STATS_M],
        [BTN_STATS_BY_SYMBOL, BTN_STATS_BY_TF],
        [BTN_STATS_BY_SESSION],
        [BTN_CLEAR_STATS],
        [BTN_STATS_CSV],
        [BTN_BACK],
    ]
    keyboard = [[KeyboardButton(x) for x in row if x] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _kb_tp_mid() -> ReplyKeyboardMarkup:
    # Подменю настроек промежуточного TP
    rows = [
        [BTN_TPMID_TOGGLE],
        [BTN_TPMID_AUTO_TOGGLE],
        [BTN_TPMID_SETTINGS],
        [BTN_TPMID_RESET],
        [BTN_BACK],
    ]
    keyboard = [[KeyboardButton(x) for x in row if x] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _kb_levels_filters(symbol: str, timeframe: str, page: int, has_next: bool, prev_symbol: str = None, next_symbol: str = None) -> InlineKeyboardMarkup:
    # Symbol navigation buttons - always show if we have multiple symbols
    symbol_nav = []
    if prev_symbol or next_symbol:  # Show switcher if we have any navigation
        if prev_symbol:
            symbol_nav.append(InlineKeyboardButton("◀️", callback_data=f"levels:symbol:prev"))
        if next_symbol:
            symbol_nav.append(InlineKeyboardButton("▶️", callback_data=f"levels:symbol:next"))
    
    # Page navigation buttons
    page_nav = []
    if page > 1:
        page_nav.append(InlineKeyboardButton("◀️", callback_data=f"levels:page:{symbol}:{timeframe}:{max(1,page-1)}"))
    if has_next:
        page_nav.append(InlineKeyboardButton("▶️", callback_data=f"levels:page:{symbol}:{timeframe}:{page+1}"))
    
    rows = []
    
    # Add symbol switcher if we have navigation
    if symbol_nav:
        rows.append(symbol_nav)
    
    # Main action buttons
    rows.extend([
        [InlineKeyboardButton("➕ Добавить", callback_data="levels:add"),
         InlineKeyboardButton("🗑 Удалить всё", callback_data="levels:del")],
        [InlineKeyboardButton("⬆️ Экспорт CSV", callback_data="levels:export"),
         InlineKeyboardButton("⬇️ Импорт CSV", callback_data="levels:import")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="levels:cfg")],
    ])
    
    # Add page navigation if we have it
    if page_nav:
        rows.append(page_nav)
    
    rows.append([InlineKeyboardButton("⬅️ К монетам", callback_data="levels:panel:back_to_picker")])
    return InlineKeyboardMarkup(rows)


def _kb_clear_stats():
    # Подменю очистки статистики
    rows = [
        [BTN_CLEAR_DAY, BTN_CLEAR_WEEK, BTN_CLEAR_MONTH],
        [BTN_BACK],
    ]
    keyboard = [[KeyboardButton(x) for x in row if x] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _kb_timezone():
    # Подменю выбора часового пояса
    rows = [
        ["🇹🇷 Turkey (UTC+3)", "🇩🇪 Germany (UTC+1)"],
        ["🇷🇺 Moscow (UTC+3)", "🇺🇸 New York (UTC-5)"],
        ["🇬🇧 London (UTC+0)", "🇯🇵 Tokyo (UTC+9)"],
        ["🇦🇺 Sydney (UTC+10)", "🇨🇳 Beijing (UTC+8)"],
        [BTN_BACK],
    ]
    keyboard = [[KeyboardButton(x) for x in row if x] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _kb_notifications():
    # Подменю настроек уведомлений
    rows = [
        [BTN_NOTIF_STATS, BTN_NOTIF_DRAWDOWN],
        [BTN_NOTIF_WEEKLY],
        [BTN_BACK],
    ]
    keyboard = [[KeyboardButton(x) for x in row if x] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)

