#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Binance API, REST-клиент, кэши цен и фич"""

from typing import Dict, Any, Tuple
from datetime import datetime
import time
import requests
from requests.adapters import HTTPAdapter

from config import MAX_WORKERS


# =============================================================================
# BINANCE API CONSTANTS & SESSION
# =============================================================================

BINANCE_FAPI = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "UnifiedSignalBot/2.0"})
SESSION.mount("https://", HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

# User-facing aliases -> Binance USD-M symbols
SYMBOL_ALIASES = {
    "PEPEUSDT": "1000PEPEUSDT",
}


def _canonical_symbol(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    return SYMBOL_ALIASES.get(s, s)


# =============================================================================
# PRICE CACHE (short-lived current price cache per symbol)
# =============================================================================

PRICE_CACHE: Dict[str, Tuple[float, float]] = {}


# ============================================================================
# ФУНКЦИЯ: get_mark_price_cached
# ОПИСАНИЕ: Получает текущую цену символа с кешированием (TTL 5 секунд)
#           Запрашивает цену с Binance, использует кеш для снижения нагрузки
# ============================================================================
def get_mark_price_cached(symbol: str, ttl_seconds: float = 5.0) -> float:
    """Return current price for symbol using a short-lived cache.

    Uses Binance futures price endpoint. Falls back to last known price in cache
    if network call fails. Returns 0.0 if no price available.
    """
    from time import time
    api_symbol = _canonical_symbol(symbol)
    try:
        # serve from cache if fresh
        price_ts = PRICE_CACHE.get(api_symbol)
        if price_ts is not None:
            price, ts = price_ts
            if time() - ts <= ttl_seconds:
                return float(price)
        # fetch fresh
        r = SESSION.get(f"{BINANCE_FAPI}/fapi/v1/ticker/price", params={"symbol": api_symbol}, timeout=3)
        j = r.json()
        price = float(j.get("price")) if isinstance(j, dict) and j.get("price") is not None else float(j[0]["price"]) if isinstance(j, list) and j else 0.0
        PRICE_CACHE[api_symbol] = (price, time())
        return price
    except Exception:
        # fallback to stale cache
        price_ts = PRICE_CACHE.get(api_symbol)
        return float(price_ts[0]) if price_ts else 0.0
# ============ КОНЕЦ ФУНКЦИИ: get_mark_price_cached ============


# =============================================================================
# FEATURE & SIGNAL CACHES
# =============================================================================

FEATURE_CACHE: dict[tuple[str, str], Dict[str, Any]] = {}
LAST_SIGNAL: Dict[Tuple[int, str, str], datetime] = {}
PRICE_DECIMALS_CACHE: dict[str, int] = {}
USER_SYMBOLS_CACHE: dict[str, tuple[list, float]] = {}  # Cache for user symbols with TTL

# Avoid hammering Binance with repeated invalid symbol/interval requests
KLINES_BAD_REQUEST_TTL_S = 300
KLINES_BAD_REQUEST_CACHE: dict[tuple[str, str], float] = {}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def sleep_backoff(tries: int, base_ms: int = 50) -> None:
    """Exponential backoff sleep"""
    import time
    import random
    delay = min(5, (base_ms * (2 ** tries)) / 1000.0)
    jitter = random.uniform(0, delay * 0.3)
    time.sleep(delay + jitter)


# =============================================================================
# TECHNICAL INDICATORS
# =============================================================================

def atr(df, period: int = 14) -> float:
    """Calculate ATR (Average True Range)"""
    import pandas as pd
    import numpy as np
    
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    atr_val = tr.rolling(period).mean().iloc[-1]
    return float(atr_val) if not np.isnan(atr_val) else 0.0


def ema(series, span: int) -> float:
    """Calculate EMA (Exponential Moving Average)"""
    import numpy as np
    ema_val = series.ewm(span=span, adjust=False).mean().iloc[-1]
    return float(ema_val) if not np.isnan(ema_val) else 0.0


def rsi(series, period: int = 14) -> float:
    """Calculate RSI (Relative Strength Index)"""
    import numpy as np
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    rsi_val = 100 - (100 / (1 + rs)).iloc[-1]
    return float(rsi_val) if not np.isnan(rsi_val) else 50.0


def rolling_pct_delta(series, window: int) -> float:
    """Calculate percentage change over rolling window"""
    if len(series) < window + 1:
        return 0.0
    old_val = series.iloc[-(window + 1)]
    new_val = series.iloc[-1]
    if old_val == 0:
        return 0.0
    return float((new_val - old_val) / abs(old_val))


def pct_change(new_val: float, old_val: float) -> float:
    """Calculate percentage change between two values"""
    if old_val == 0:
        return 0.0
    return float((new_val - old_val) / abs(old_val))


# =============================================================================
# BINANCE REST API FUNCTIONS
# =============================================================================

def _klines_to_df(raw: list):
    """Convert raw Binance klines to pandas DataFrame"""
    import pandas as pd
    
    cols = [
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades","taker_base","taker_quote","ignore"
    ]
    df = pd.DataFrame(raw, columns=cols)
    df = df[["open_time","open","high","low","close","volume"]]
    
    # Convert to numeric
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    
    df.rename(columns={"open_time": "ts"}, inplace=True)
    return df


def binance_klines(symbol: str, interval: str, limit: int = 500, start_time: int = None, end_time: int = None):
    """Fetch historical klines from Binance Futures API
    
    Args:
        symbol: Trading pair (e.g. "BTCUSDT")
        interval: Timeframe (e.g. "5m", "15m")
        limit: Number of klines to fetch (max 1500, default 500)
        start_time: Start timestamp in milliseconds (optional)
        end_time: End timestamp in milliseconds (optional)
    """
    import pandas as pd

    api_symbol = _canonical_symbol(symbol)
    key = (api_symbol, interval)
    blocked_until = KLINES_BAD_REQUEST_CACHE.get(key, 0.0)
    if blocked_until > time.time():
        remaining = int(blocked_until - time.time())
        raise RuntimeError(f"[klines] blocked after bad request for {symbol} {interval} ({remaining}s left)")

    MAX_TRIES = 6
    tries = 0
    while tries < MAX_TRIES:
        try:
            url = f"{BINANCE_FAPI}/fapi/v1/klines"
            params = {"symbol": api_symbol, "interval": interval, "limit": min(limit, 1500)}
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            
            r = SESSION.get(url, params=params, timeout=8)
            if r.status_code == 200:
                data = r.json()
                KLINES_BAD_REQUEST_CACHE.pop(key, None)
                return _klines_to_df(data)

            # 4xx here is usually invalid symbol/interval for this market: no point retrying
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                KLINES_BAD_REQUEST_CACHE[key] = time.time() + KLINES_BAD_REQUEST_TTL_S
                print(f"[klines] HTTP {r.status_code} {symbol} {interval} (cached {KLINES_BAD_REQUEST_TTL_S}s)")
                raise RuntimeError(f"[klines] bad request {r.status_code} for {symbol} {interval}")

            print(f"[klines] HTTP {r.status_code} {symbol} {interval}")
        except Exception as e:
            print(f"[klines] error {symbol} {interval}: {e}")
        tries += 1
        sleep_backoff(tries)
    
    raise RuntimeError(f"[klines] too many retries for {symbol} {interval}")


def binance_premium_index(symbol: str) -> Dict[str, float]:
    """Fetch premium index (funding rate, mark price) from Binance"""
    url = f"{BINANCE_FAPI}/fapi/v1/premiumIndex"
    params = {"symbol": _canonical_symbol(symbol)}
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    
    index_price = float(j["indexPrice"])
    mark_price = float(j["markPrice"])
    last_funding = float(j.get("lastFundingRate") or 0.0)
    basis = (mark_price - index_price) / index_price
    
    return {
        "index": index_price,
        "mark": mark_price,
        "basis": basis,
        "funding": last_funding
    }


def binance_open_interest_hist(symbol: str, period: str = "5m", limit: int = 30):
    """Fetch Open Interest history from Binance"""
    import pandas as pd
    
    url = f"{BINANCE_FAPI}/futures/data/openInterestHist"
    params = {"symbol": _canonical_symbol(symbol), "period": period, "limit": min(limit, 500)}
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    
    return pd.Series([float(x["sumOpenInterest"]) for x in j])


def binance_taker_long_short(symbol: str, period: str = "5m", limit: int = 30):
    """Fetch Taker Long/Short ratio from Binance"""
    import pandas as pd
    
    url = f"{BINANCE_FAPI}/futures/data/takerlongshortRatio"
    params = {"symbol": _canonical_symbol(symbol), "period": period, "limit": min(limit, 500)}
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    
    ratios = pd.Series([float(x["buySellRatio"]) for x in j])
    share = ratios.apply(lambda rr: rr / (1.0 + rr) if rr >= 0 else 0.5)
    
    return ratios, share


def binance_ls_top_accounts(symbol: str, period: str = "5m", limit: int = 30):
    """Fetch Long/Short ratio for top accounts from Binance"""
    import pandas as pd
    
    url = f"{BINANCE_FAPI}/futures/data/topLongShortAccountRatio"
    params = {"symbol": _canonical_symbol(symbol), "period": period, "limit": min(limit, 500)}
    r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    
    return pd.Series([float(x["longShortRatio"]) for x in j])


def get_symbol_filters(symbol: str) -> dict:
    """Получить фильтры символа с Binance USDⓈ-M"""
    try:
        api_symbol = _canonical_symbol(symbol)
        url = f"https://fapi.binance.com/fapi/v1/exchangeInfo?symbol={api_symbol}"
        resp = SESSION.get(url, timeout=5)
        if resp.status_code != 200:
            print(f"[WARN] Failed to get exchange info for {symbol}: {resp.status_code}")
            return {}
        
        data = resp.json()
        if "symbols" not in data or len(data["symbols"]) == 0:
            print(f"[WARN] Symbol {symbol} not found in exchange info")
            return {}
        
        symbol_info = data["symbols"][0]
        filters = {}
        
        for f in symbol_info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                filters["stepSize"] = float(f.get("stepSize", "0.001"))
                filters["minQty"] = float(f.get("minQty", "0"))
                filters["maxQty"] = float(f.get("maxQty", "10000000"))
            elif f.get("filterType") == "PRICE_FILTER":
                filters["tickSize"] = float(f.get("tickSize", "0.01"))
                filters["minPrice"] = float(f.get("minPrice", "0"))
                filters["maxPrice"] = float(f.get("maxPrice", "1000000"))
            elif f.get("filterType") == "MIN_NOTIONAL":
                filters["minNotional"] = float(f.get("notional", "5"))
        
        # Also store decimals for prices
        if symbol_info.get("pricePrecision"):
            filters["pricePrecision"] = int(symbol_info["pricePrecision"])
        if symbol_info.get("quantityPrecision"):
            filters["quantityPrecision"] = int(symbol_info["quantityPrecision"])
        
        return filters
        
    except Exception as e:
        print(f"[ERROR] get_symbol_filters({symbol}) failed: {e}")
        return {
            "stepSize": 0.001,
            "tickSize": 0.01,
            "minQty": 0.001,
            "maxQty": 10000000,
            "minNotional": 5
        }

