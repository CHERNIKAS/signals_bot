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
# STRATEGY VERSION
# =============================================================================
# Флаг для переключения на новую модульную стратегию (strategy_v2.py)
# False = используется старая strategy.py (по умолчанию)
# True = используется новая strategy_v2.py с модулями (импульс, уровни, OI flow, BTC ветер)
USE_STRATEGY_V2 = False  # TODO: переключить на True после тестирования

# =============================================================================
# CONFIG (основная конфигурация стратегии/правил)
# =============================================================================

# =============================================================================
# ADAPTIVE FILTERS: Global Risk Limits (константы защитных рамок)
# =============================================================================

# ATR границы (волатильность в процентах)
GLOBAL_MIN_ATR = 0.002    # 0.2% минимальная волатильность
GLOBAL_MAX_ATR = 0.080    # 8.0% максимальная волатильность

# Taker ratio требования по ликвидности
TAKER_REQ_LOWLIQ  = 0.52  # слабая сессия (азиатская ночь)
TAKER_REQ_NORMAL  = 0.53  # обычная сессия (было 0.55, смягчено)
TAKER_REQ_HIGHLIQ = 0.54  # топ-ликвидная сессия US open (было 0.57, смягчено)

# Quiet range множители
QUIET_MULT_LOWVOL   = 0.25  # спокойный рынок
QUIET_MULT_NORMAL   = 0.40  # обычный рынок
QUIET_MULT_HIGHVOL  = 0.60  # бешеный рынок

# Пороги для определения режима волатильности
LOW_VOL_THRESHOLD   = 0.004   # ~0.4% atr_pct → "сонный" рынок
HIGH_VOL_THRESHOLD  = 0.020   # ~2.0% atr_pct → "разогнанный" рынок

# Адаптация ATR под монету (относительно её средней волы)
ATR_RELATIVE_MIN = 0.5   # не меньше 50% от среднего ATR монеты
ATR_RELATIVE_MAX = 2.5   # не больше 250% от среднего ATR монеты

CONFIG: Dict[str, Any] = {
    "exchange": "binance",
    "lookback": 300,
    "btc_trend_timeframes": ["5m", "15m"],
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
    "thresholds": {
        "oi_delta_abs_pct": 0.5,           # ослаблено с 1.0 до 0.5
        "funding_max_pos": 0.05,
        "ls_top_only_short": 2.5,          # ослаблено с 3.0 до 2.5
        "ls_top_only_long": 0.7,           # ослаблено с 0.6 до 0.7
        "basis_long_floor": -0.0010,       # смягчено с -0.0005 до -0.0010
        "taker_buy_share_min_for_long": 0.55,   # ослаблено с 0.60 до 0.55
        "taker_buy_share_max_for_short": 0.55,  # ослаблено с 0.60 до 0.55
        "oi_spike_without_vol": 2.0,
        # НОВОЕ: фильтры волатильности (ослаблены для больше сигналов)
        "atr_pct_range": (0.005, 0.050),   # впускной коридор волатильности (0.5%–5.0%)
        "quiet_tr_bars": 3,                 # анти-флэт: N баров с TrueRange < 0.4×ATR (ослаблено)
    },
    "sl_tp": {
        "mode": "ATR",
        "atr_len": 14,
        # Чуть шире стоп — меньше «SL до TP1»
        "scalp_sl_pct_range": (0.010, 0.015),
        # Эти диапазоны — КЛИППЕРЫ. Фактические TP берём от atr_pct (см. strategy)
        "tp1_pct_range": (0.006, 0.009),
        "tp2_pct_range": (0.020, 0.035),
        # Шаг трейла в ATR (используется после TP1)
        "trail_step_atr": 0.35,
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
    
    # Запрещённые сессии для торговли (UTC)
    "sessions": {
        "ban_utc": ["03:00-07:00"]  # не торговать в этот период
    },
    
    # Активный пресет (None = использовать значения выше)
    "active_preset": None,  # "A_conservative" / "B_fast_tp1" / "C_trend" / None
    
    # Пресеты для A/B/C тестирования
    "presets": {
        "A_conservative": {
            "sl_tp": {
                "scalp_sl_pct_range": (0.008, 0.015),
                "tp1_pct_range": (0.007, 0.012),
                "tp2_pct_range": (0.020, 0.040),
                "trail_step_atr": 0.35
            },
            "thresholds": { "atr_pct_range": (0.006, 0.030) }
        },
        "B_fast_tp1": {
            "sl_tp": {
                "scalp_sl_pct_range": (0.006, 0.012),
                "tp1_pct_range": (0.006, 0.010),
                "tp2_pct_range": (0.018, 0.035),
                "trail_step_atr": 0.30
            },
            "thresholds": {
                "atr_pct_range": (0.006, 0.025),
                "taker_buy_share_min_for_long": 0.58,
                "taker_buy_share_max_for_short": 0.62
            }
        },
        "C_trend": {
            "sl_tp": {
                "scalp_sl_pct_range": (0.010, 0.018),
                "tp1_pct_range": (0.010, 0.015),
                "tp2_pct_range": (0.030, 0.060),
                "trail_step_atr": 0.25
            },
            "thresholds": { "atr_pct_range": (0.010, 0.050) }
        }
    }
}


# =============================================================================
# APPLY ACTIVE PRESET (если задан)
# =============================================================================

def apply_preset():
    """Применяет активный пресет к CONFIG, если задан"""
    preset_name = CONFIG.get("active_preset")
    if preset_name and preset_name in CONFIG.get("presets", {}):
        ps = CONFIG["presets"][preset_name]
        for block in ("sl_tp", "thresholds"):
            if block in ps:
                CONFIG[block].update(ps[block])
        print(f"[CONFIG] Applied preset: {preset_name}")

# Применить пресет сразу при импорте
apply_preset()


# =============================================================================
# LIQUIDITY REGIME DETECTOR
# =============================================================================

def get_liquidity_regime(now_ts: float = None) -> str:
    """
    Определяет режим ликвидности рынка по времени UTC.
    
    Args:
        now_ts: Unix timestamp (если None - берём текущее время)
    
    Returns:
        'low' - низкая ликвидность (азиатская ночь, 00:00-06:00 UTC)
        'normal' - нормальная ликвидность (06:00-13:00, 20:00-24:00 UTC)
        'high' - высокая ликвидность (US session, 13:00-20:00 UTC)
    """
    from datetime import datetime
    
    if now_ts is None:
        now = datetime.utcnow()
    else:
        now = datetime.utcfromtimestamp(now_ts)
    
    hour = now.hour
    
    # Азиатская ночь (низкая ликвидность)
    if 0 <= hour < 6:
        return "low"
    
    # US session (высокая ликвидность)
    elif 13 <= hour < 20:
        return "high"
    
    # Остальное время (нормальная ликвидность)
    else:
        return "normal"


def is_time_banned(now_ts: float = None) -> tuple[bool, str]:
    """
    Проверяет, находится ли текущее время в забаненном интервале.
    
    Args:
        now_ts: Unix timestamp (если None - берём текущее время)
    
    Returns:
        (is_banned, reason) - True если сейчас бан-время, иначе False
    """
    from datetime import datetime
    
    if now_ts is None:
        now = datetime.utcnow()
    else:
        now = datetime.utcfromtimestamp(now_ts)
    
    hour = now.hour
    minute = now.minute
    current_time_min = hour * 60 + minute
    
    # Читаем ban_utc из конфига
    ban_windows = CONFIG.get("sessions", {}).get("ban_utc", [])
    
    for window in ban_windows:
        # Парсим формат "03:00-07:00"
        try:
            start_str, end_str = window.split("-")
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            
            start_min = start_h * 60 + start_m
            end_min = end_h * 60 + end_m
            
            # Проверяем попадание
            if start_min <= current_time_min < end_min:
                return (True, f"TIME_BAN ({window} UTC)")
        
        except Exception as e:
            print(f"[CONFIG] Ошибка парсинга ban_utc '{window}': {e}")
            continue
    
    return (False, "")


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

