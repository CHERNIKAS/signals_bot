#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Конфигурация проекта, настройки и флаги"""

import os
import asyncio
from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv


# =============================================================================
# ФУНКЦИЯ: load_env_with_fallback
# ОПИСАНИЕ: Загружает переменные окружения из .env файла и инициализирует БД
#           Пытается загрузить .env, если нет — .env.example
# ============================================================================
def load_env_with_fallback():
    if os.path.exists(".env"):
        load_dotenv(".env")
    elif os.path.exists(".env.example"):
        load_dotenv(".env.example")

    # локальный импорт для избежания циклов
    from database.database import db_enabled, bootstrap_schema

    # если БД подключена — создадим схему
    if db_enabled():
        bootstrap_schema()

    print(
        "TG token loaded?:", bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "chat loaded?:", bool(os.getenv("TELEGRAM_CHAT_ID")),
        "db?:", bool(os.getenv("DATABASE_URL")),
    )
# ============ КОНЕЦ ФУНКЦИИ: load_env_with_fallback ============


# =============================================================================
# WORKER POOL & TIMEOUTS
# =============================================================================

MAX_WORKERS    = int(os.getenv("WORKERS", "8"))     # 4–8 обычно ок
EVAL_TIMEOUT_S = int(os.getenv("EVAL_TIMEOUT", "20"))
EXEC = ThreadPoolExecutor(max_workers=MAX_WORKERS)   


# =============================================================================
# CONFIG (общие настройки бота; пороги стратегии — config/thresholds.yaml)
# =============================================================================

CONFIG: Dict[str, Any] = {
    "exchange": "binance",
    "lookback": 300,
    "schedule_minutes": 1,
    "debug_no_signal": False,
    "risk": {
        "equity": 10_000.0,
        "k_pct": 0.01,
        "max_concurrent_positions": 3,
        "max_symbol_risk_pct": 0.02,
        "symbol_cooldown_after_2sl_hours": 6,  # кулдаун символа после 2 SL (в часах)
    },
    "position": {
        "tp1_close_share": 0.7,  # Доля позиции закрываемая на TP1 (70%)
        "be_offset_atr": 0.2,    # BE = entry ± 0.2×ATR
    },
    "lifetime": {"scalp_min": 30, "scalp_max": 90, "intraday_till_session_end": True},
    "news_blackouts": [],                 # [{start:"2025-09-14T12:00:00", end:"...", reason:"CPI"}]
    "telegram": {
        "enabled": True,
        "token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_CHAT_ID",  # если задан — строгий один чат без БД
    },
    "cooldown_minutes": 10,
    "stats": {
        "equity_start_usd": 100.0,
        "include_commissions": True,
        "include_funding": False,
        "sort_closed_by": "close_time",  # "close_time" | "open_time"
    },
    
}


# =============================================================================
# UI FLAGS (форматирование карточек сигналов)
# =============================================================================

SHOW_TRAIL_AS_ATR_HINT = True
SHOW_CONFLUENCE_BLOCK = True
HIDE_DELTA_BLOCKS = True
PCT_DECIMALS = 2
XATR_DECIMALS = 2
FULL_CONFLUENCE_DETAILS = True


# =============================================================================
# FRESHNESS FILTER
# =============================================================================

# 0 = фильтр свежести выключен (ничего не дропаем); 1 = включен
FRESH_ONLY = os.getenv("FRESH_ONLY", "0") == "1"


# =============================================================================
# TICK GUARD & TIMEOUT
# =============================================================================

_TICK_TIMEOUT = int(os.getenv("TICK_TIMEOUT", "180"))  # было "55"


# =============================================================================
# TRADES PANEL CONSTANTS
# =============================================================================

PAGE_SIZE = 6            # сделок на страницу
SYMBOLS_PER_PAGE = 8     # символов на страницу

