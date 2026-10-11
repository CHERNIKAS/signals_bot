#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Вспомогательные функции для handlers"""

import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from telegram import Update


# ============================================================================
# ФУНКЦИЯ: utcnow
# ОПИСАНИЕ: Возвращает текущее время в UTC
# ============================================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# ФУНКЦИЯ: _auth
# ОПИСАНИЕ: Проверка прав доступа (заглушка, всегда возвращает True)
# ============================================================================
def _auth(update: Update) -> bool:
    """Basic auth check (always True for now)"""
    return True


# ============================================================================
# ФУНКЦИЯ: _get_message
# ОПИСАНИЕ: Получает сообщение из Update (callback_query или message)
# ============================================================================
def _get_message(update: Update):
    """Helper to get message from Update (works with both callback and regular messages)"""
    if update.callback_query:
        return update.callback_query.message
    return update.message


# ============================================================================
# ФУНКЦИЯ: _fmt_price
# ОПИСАНИЕ: Форматирует цену с учетом десятичных знаков символа
# ============================================================================
def _get_price_decimals(val: float) -> int:
    """
    Определяет количество значащих десятичных знаков в цене
    
    Args:
        val: цена
    
    Returns:
        количество знаков после запятой (не более 8)
    """
    # Форматируем с максимальной точностью
    price_str = f"{val:.10f}"
    
    if '.' in price_str:
        integer_part, decimal_part = price_str.split('.')
        # Убираем trailing zeros
        decimal_part = decimal_part.rstrip('0')
        
        if decimal_part:
            # Количество знаков = длина decimal части
            decimals = len(decimal_part)
            # Ограничиваем максимумом (8 знаков как у Binance)
            return min(decimals, 8)
    
    return 1  # Минимум 1 знак для красоты


def _fmt_price(val: float, ref: float, symbol: str, decimals: int = None) -> str:
    """
    Format price with specified precision.
    
    Args:
        val: значение цены
        ref: референсная цена (не используется, оставлено для совместимости)
        symbol: символ (не используется, оставлено для совместимости)
        decimals: количество знаков после запятой (если None - определяется автоматически)
    
    Returns:
        Отформатированная цена
    """
    if decimals is None:
        decimals = _get_price_decimals(val)
    
    # Форматируем с указанной точностью
    return f"{val:.{decimals}f}"


# ============================================================================
# ФУНКЦИЯ: format_qty
# ОПИСАНИЕ: Форматирует количество с учетом step_size
# ============================================================================
def format_qty(qty: float, step_size: float) -> str:
    """Format quantity according to stepSize filter"""
    if step_size >= 1:
        return f"{int(qty)}"
    # Count decimals in step_size
    step_str = f"{step_size:.10f}".rstrip('0')
    if '.' in step_str:
        decimals = len(step_str.split('.')[1])
    else:
        decimals = 0
    return f"{qty:.{decimals}f}"


# ============================================================================
# ФУНКЦИЯ: get_symbol_filters
# ОПИСАНИЕ: Получает фильтры символа с Binance (stepSize и т.д.)
# ============================================================================
def get_symbol_filters(symbol: str) -> Dict[str, Any]:
    """Get symbol filters from Binance (stepSize, etc.)"""
    # Локальный импорт
    from binance import SESSION, BINANCE_FAPI, _canonical_symbol
    api_symbol = _canonical_symbol(symbol)
    
    try:
        r = SESSION.get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=5)
        data = r.json()
        for s in data.get("symbols", []):
            if s.get("symbol") == api_symbol:
                filters = {}
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        filters["stepSize"] = float(f.get("stepSize", 0.001))
                    elif f.get("filterType") == "PRICE_FILTER":
                        filters["tickSize"] = float(f.get("tickSize", 0.01))
                return filters
    except Exception as e:
        print(f"[WARN] get_symbol_filters({symbol}) failed: {e}")
    return {"stepSize": 0.001, "tickSize": 0.01}


# ============================================================================
# ФУНКЦИЯ: within_news_blackout
# ОПИСАНИЕ: Проверяет, находимся ли мы в периоде блокировки новостей
# ============================================================================
def within_news_blackout(now: datetime, blackouts: list) -> Optional[str]:
    """Check if we're within a news blackout period"""
    for b in blackouts:
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"])
        if start <= now <= end:
            return b.get("reason", "News blackout")
    return None


# ============================================================================
# ФУНКЦИЯ: _get_symbol_price_decimals
# ОПИСАНИЕ: Получает количество знаков после запятой для цены символа
#           Использует кеш и загружает данные с Binance
# ============================================================================
def _get_symbol_price_decimals(symbol: str) -> int:
    """Cached fetch from Binance exchangeInfo for symbol, fallback heuristic"""
    # Локальный импорт
    from binance import PRICE_DECIMALS_CACHE, SESSION, _canonical_symbol
    api_symbol = _canonical_symbol(symbol)

    if api_symbol in PRICE_DECIMALS_CACHE:
        return PRICE_DECIMALS_CACHE[api_symbol]
    try:
        r = SESSION.get(f"https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5)
        j = r.json()
        for s in j.get("symbols", []):
            if s.get("symbol") == api_symbol:
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = f.get("tickSize")
                        if tick:
                            # count decimals in tickSize like 0.001 -> 3
                            parts = (tick.split(".") + [""])[1]
                            dec = len(parts.rstrip("0"))
                            PRICE_DECIMALS_CACHE[api_symbol] = max(dec, 0)
                            return PRICE_DECIMALS_CACHE[api_symbol]
        # fallback: infer by price magnitude conventions
        dec = 6 if api_symbol.endswith("USDT") else 4
        PRICE_DECIMALS_CACHE[api_symbol] = dec
        return dec
    except Exception:
        return PRICE_DECIMALS_CACHE.setdefault(api_symbol, 4)


# ============================================================================
# ФУНКЦИЯ: _get_user_symbols_cached
# ОПИСАНИЕ: Получает список символов пользователя с кешированием (TTL 60s)
#           Снижает нагрузку на БД при частых запросах
# ============================================================================
def _get_user_symbols_cached(uid: int) -> list:
    """Get user symbols with caching (60s TTL)"""
    import time
    # Локальный импорт
    from binance import USER_SYMBOLS_CACHE
    from database import db_enabled, db_user_pairs
    
    cache_key = f"symbols_{uid}"
    now = time.time()
    
    if cache_key in USER_SYMBOLS_CACHE:
        cached_data, timestamp = USER_SYMBOLS_CACHE[cache_key]
        if now - timestamp < 60:  # 60s cache
            return cached_data
    
    # Fetch from DB
    symbols = []
    try:
        if db_enabled():
            symbols = db_user_pairs(uid) or []
        else:
            symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]
    except Exception:
        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]
    
    # Sort: favorites first, then by frequency, then alphabetically
    symbols = sorted(symbols)
    
    # Cache for 60s
    USER_SYMBOLS_CACHE[cache_key] = (symbols, now)
    return symbols

