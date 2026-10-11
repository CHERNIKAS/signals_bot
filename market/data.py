#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Слой рыночных данных Binance USDⓈ-M для стратегии.

Правила, которых держится весь модуль:
  * Свечи — ТОЛЬКО закрытые. Последний (формирующийся) бар Binance отбрасываем,
    иначе сигнал "перерисовывается" внутри бара.
  * Порядок данных — ASC (старые → новые), индекс DatetimeIndex UTC по open time.
    Это единая конвенция для v1 и v2.
  * Всё кэшируется в общем MARKET_CACHE до момента, когда данные реально могут
    поменяться, поэтому N пользователей не умножают запросы к Binance на N.
  * Funding нормализуется на интервал начисления (1h/4h/8h у разных пар).

Документация:
  klines             https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
  openInterestHist   .../market-data/rest-api/Open-Interest-Statistics  (только 30 дней, 1000 req/5min/IP)
  fundingInfo        .../market-data/rest-api/Get-Funding-Rate-Info      (500 req/5min/IP общий с fundingRate)
"""

import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from binance.binance import BINANCE_FAPI, SESSION, _canonical_symbol
from market.cache import MARKET_CACHE


TF_MS: Dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

# Периоды, которые поддерживают эндпоинты /futures/data/*
STATS_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")

DEFAULT_FUNDING_INTERVAL_H = 8

# Запас после закрытия бара, чтобы Binance успел его отдать как закрытый
_KLINE_GRACE_S = 2.0
# Статистика (OI/taker/LS) публикуется с задержкой после конца периода
_STATS_GRACE_S = 60.0
_STATS_RETRY_S = 30.0


class BinanceRateLimited(RuntimeError):
    pass


# =============================================================================
# HTTP с уважением к лимитам
# =============================================================================

_pause_until = 0.0
_pause_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 8.0, retries: int = 3) -> Any:
    """GET к fapi. На 429/418 ставит глобальную паузу по Retry-After и не долбит дальше."""
    global _pause_until
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        if time.time() < _pause_until:
            raise BinanceRateLimited(f"paused for {_pause_until - time.time():.0f}s")
        try:
            r = SESSION.get(f"{BINANCE_FAPI}{path}", params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            time.sleep(0.3 * (2 ** attempt))
            continue

        used = r.headers.get("X-MBX-USED-WEIGHT-1M")
        if used and used.isdigit() and int(used) > 1800:
            print(f"[MARKET] high weight usage: {used}/2400 per minute")

        if r.status_code == 200:
            return r.json()
        if r.status_code in (418, 429):
            retry_after = float(r.headers.get("Retry-After") or 60)
            with _pause_lock:
                _pause_until = max(_pause_until, time.time() + retry_after)
            print(f"[MARKET] HTTP {r.status_code} on {path}, pausing {retry_after:.0f}s")
            raise BinanceRateLimited(f"HTTP {r.status_code}")
        if 400 <= r.status_code < 500:
            raise RuntimeError(f"[MARKET] HTTP {r.status_code} {path} {params}: {r.text[:200]}")
        last_err = RuntimeError(f"HTTP {r.status_code}")
        time.sleep(0.3 * (2 ** attempt))
    raise RuntimeError(f"[MARKET] {path} failed after {retries} tries: {last_err}")


def _next_boundary_s(period_ms: int) -> float:
    now = _now_ms()
    return ((now // period_ms) + 1) * period_ms / 1000.0


# =============================================================================
# KLINES (только закрытые)
# =============================================================================

def _parse_klines(raw: List[list]) -> pd.DataFrame:
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols).drop(columns=["ignore"])
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    df["trades"] = df["trades"].astype("int64")
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.index.name = "time"
    return df


def closed_klines(symbol: str, tf: str, limit: int = 300) -> pd.DataFrame:
    """Последние `limit` ЗАКРЫТЫХ свечей, ASC, кэш до закрытия следующей свечи.

    Колонки: open_time, open, high, low, close, volume, close_time, quote_volume,
    trades, taker_buy_base, taker_buy_quote. Плюс legacy-колонка `ts` (= open_time).
    """
    if tf not in TF_MS:
        raise ValueError(f"unsupported timeframe {tf}")
    api_symbol = _canonical_symbol(symbol)

    def load():
        raw = _get("/fapi/v1/klines", {"symbol": api_symbol, "interval": tf, "limit": min(limit + 1, 1500)})
        df = _parse_klines(raw)
        df = df[df["close_time"] < _now_ms()]  # выкидываем формирующийся бар
        df = df.tail(limit).copy()
        df["ts"] = df["open_time"]
        return df, _next_boundary_s(TF_MS[tf]) + _KLINE_GRACE_S

    return MARKET_CACHE.get_or_load(("klines", api_symbol, tf, limit), load)


# =============================================================================
# FUNDING
# =============================================================================

def funding_info_map() -> Dict[str, Dict[str, float]]:
    """symbol -> {interval_h, cap, floor} для пар с нестандартными параметрами. Кэш 15 мин."""
    def load():
        try:
            raw = _get("/fapi/v1/fundingInfo")
        except Exception as e:
            print(f"[MARKET] fundingInfo failed: {e}")
            return {}, time.time() + 60
        out = {}
        for x in raw or []:
            out[x["symbol"]] = {
                "interval_h": int(x.get("fundingIntervalHours") or DEFAULT_FUNDING_INTERVAL_H),
                "cap": float(x.get("adjustedFundingRateCap") or 0) or None,
                "floor": float(x.get("adjustedFundingRateFloor") or 0) or None,
            }
        return out, time.time() + 900

    return MARKET_CACHE.get_or_load(("fundingInfo",), load)


def premium_snapshot(symbol: str) -> Dict[str, Any]:
    """mark/index/basis + funding, нормализованный на интервал. Кэш 15 сек."""
    api_symbol = _canonical_symbol(symbol)

    def load():
        j = _get("/fapi/v1/premiumIndex", {"symbol": api_symbol})
        mark = float(j["markPrice"])
        index = float(j["indexPrice"])
        rate = float(j.get("lastFundingRate") or 0.0)
        info = funding_info_map().get(api_symbol, {})
        interval_h = int(info.get("interval_h") or DEFAULT_FUNDING_INTERVAL_H)
        snap = {
            "mark": mark,
            "index": index,
            "basis": (mark - index) / index if index > 0 else 0.0,
            "funding": rate,                                  # сырая ставка за свой интервал
            "funding_interval_h": interval_h,
            "funding_8h": rate * 8.0 / interval_h,            # приведено к классическим 8ч
            "funding_apr": rate * (24.0 / interval_h) * 365,  # годовых, для человека
            "funding_cap": info.get("cap"),
            "funding_floor": info.get("floor"),
            "next_funding_ms": int(j.get("nextFundingTime") or 0),
        }
        return snap, time.time() + 15

    return MARKET_CACHE.get_or_load(("premium", api_symbol), load)


def funding_24h(symbol: str) -> Dict[str, Any]:
    """Сумма начислений funding за последние 24ч и её знак. Кэш 30 мин
    (лимит fundingRate — 500 req/5min/IP, общий с fundingInfo)."""
    api_symbol = _canonical_symbol(symbol)

    def load():
        since = _now_ms() - 86_400_000
        try:
            raw = _get("/fapi/v1/fundingRate", {"symbol": api_symbol, "startTime": since, "limit": 100})
        except Exception as e:
            print(f"[MARKET] fundingRate {api_symbol} failed: {e}")
            return {"sum": 0.0, "sign": 0, "count": 0}, time.time() + 120
        rates = [float(x["fundingRate"]) for x in raw or [] if int(x["fundingTime"]) >= since]
        total = sum(rates)
        sign = 1 if total > 0 else -1 if total < 0 else 0
        return {"sum": total, "sign": sign, "count": len(rates)}, time.time() + 1800

    return MARKET_CACHE.get_or_load(("funding24h", api_symbol), load)


# =============================================================================
# /futures/data/* статистика (OI, taker, top LS)
# =============================================================================

def stats_period(tf: str) -> str:
    """Период статистики для ТФ: тот же, если поддерживается, иначе ближайший меньший."""
    if tf in STATS_PERIODS:
        return tf
    tf_ms = TF_MS.get(tf, 3_600_000)
    candidates = [p for p in STATS_PERIODS if TF_MS[p] <= tf_ms]
    return candidates[-1] if candidates else "5m"


def _stats(path: str, symbol: str, period: str, limit: int, parse) -> pd.DataFrame:
    api_symbol = _canonical_symbol(symbol)

    def load():
        raw = _get(path, {"symbol": api_symbol, "period": period, "limit": min(limit, 500)})
        rows = sorted(raw or [], key=lambda x: int(x["timestamp"]))
        df = pd.DataFrame([{"timestamp": int(x["timestamp"]), **parse(x)} for x in rows])
        if df.empty:
            return df, time.time() + _STATS_RETRY_S
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.index.name = "time"
        period_ms = TF_MS[period]
        # Если свежая точка ещё не опубликована — перепроверим скоро, иначе ждём след. период
        lagging = df["timestamp"].iloc[-1] < _now_ms() - 2 * period_ms
        expires = time.time() + _STATS_RETRY_S if lagging else _next_boundary_s(period_ms) + _STATS_GRACE_S
        return df, expires

    return MARKET_CACHE.get_or_load((path, api_symbol, period, limit), load)


def open_interest_hist(symbol: str, period: str, limit: int = 200) -> pd.DataFrame:
    """ASC: timestamp, open_interest (в монетах), open_interest_value (в USDT)."""
    return _stats("/futures/data/openInterestHist", symbol, period, limit, lambda x: {
        "open_interest": float(x["sumOpenInterest"]),
        "open_interest_value": float(x["sumOpenInterestValue"]),
    })


def taker_ratio_hist(symbol: str, period: str, limit: int = 60) -> pd.DataFrame:
    """ASC: timestamp, buy_sell_ratio, buy_share (= buy/(buy+sell))."""
    def parse(x):
        r = float(x["buySellRatio"])
        return {"buy_sell_ratio": r, "buy_share": r / (1.0 + r) if r >= 0 else 0.5}
    return _stats("/futures/data/takerlongshortRatio", symbol, period, limit, parse)


def top_ls_accounts_hist(symbol: str, period: str, limit: int = 60) -> pd.DataFrame:
    """ASC: timestamp, long_short_ratio."""
    return _stats("/futures/data/topLongShortAccountRatio", symbol, period, limit, lambda x: {
        "long_short_ratio": float(x["longShortRatio"]),
    })


# =============================================================================
# BTC контекст
# =============================================================================

def btc_context(tfs: List[str], limit: int = 120) -> Dict[str, pd.DataFrame]:
    """{tf: closed_klines(BTCUSDT, tf)} — общий для всех символов и юзеров."""
    out: Dict[str, pd.DataFrame] = {}
    for tf in tfs:
        try:
            out[tf] = closed_klines("BTCUSDT", tf, limit)
        except Exception as e:
            print(f"[MARKET] BTC {tf} failed: {e}")
    return out
