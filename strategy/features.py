#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Сбор фич для стратегии по ЗАКРЫТЫМ свечам (через market.*: общий кэш, нормализованный funding)."""

from typing import Dict, Any, List

import pandas as pd
import yaml

from binance import atr, rolling_pct_delta, pct_change
from market import (
    closed_klines,
    premium_snapshot,
    funding_24h,
    stats_period,
    open_interest_hist,
    taker_ratio_hist,
    top_ls_accounts_hist,
    btc_context,
)

ATR_LEN = 14

with open("config/thresholds.yaml", "r", encoding="utf-8") as _f:
    _THRESHOLDS = yaml.safe_load(_f) or {}


def _mode_for_tf(tf: str) -> str:
    return _THRESHOLDS.get("tf_to_mode", {}).get(tf, "intraday")


def _btc_anchor_tfs(tf: str) -> List[str]:
    return _THRESHOLDS.get("btc", {}).get("wind_anchor_tfs", {}).get(_mode_for_tf(tf), [tf])


def _align_stats(df, last_close_ms: int):
    """Отбрасываем точки статистики новее закрытия последнего бара — без заглядывания вперёд."""
    if df is None or df.empty:
        return df
    return df[df["timestamp"] <= last_close_ms + 1]


def compute_features(symbol: str, tf: str, lookback: int) -> Dict[str, Any]:
    """
    Собирает фичи для обеих версий стратегии по ЗАКРЫТЫМ свечам.

    Данные идут через market.* (общий кэш на всех пользователей, только закрытые бары,
    funding нормализован на интервал начисления). Все ряды — ASC (старые → новые).

    Ключи для v2: ohlc, oi_data, btc_data, atr_btc, funding_24h_sign, bar_open_ms, bar_close_ms.
    Legacy-ключи v1 сохранены (price, volume, oi, funding, basis, taker_buy_share, ...).
    """
    kl = closed_klines(symbol, tf, limit=lookback)
    if kl is None or len(kl) < 30:
        raise RuntimeError(f"Not enough closed klines for {symbol} {tf}")

    last_close_ms = int(kl["close_time"].iloc[-1])
    period = stats_period(tf)

    premium = premium_snapshot(symbol)
    oi_df = _align_stats(open_interest_hist(symbol, period, limit=200), last_close_ms)
    taker_df = _align_stats(taker_ratio_hist(symbol, period, limit=60), last_close_ms)
    ls_df = _align_stats(top_ls_accounts_hist(symbol, period, limit=60), last_close_ms)
    f24 = funding_24h(symbol)

    oi = oi_df["open_interest"].reset_index(drop=True) if oi_df is not None and not oi_df.empty else pd.Series(dtype=float)
    taker_share = taker_df["buy_share"] if taker_df is not None and not taker_df.empty else pd.Series(dtype=float)
    taker_ratio = taker_df["buy_sell_ratio"] if taker_df is not None and not taker_df.empty else pd.Series(dtype=float)
    ls_top = ls_df["long_short_ratio"] if ls_df is not None and not ls_df.empty else pd.Series(dtype=float)

    price = kl["close"].reset_index(drop=True)
    vol = kl["volume"].reset_index(drop=True)

    # Расчёт ATR и волатильности
    atr_len = ATR_LEN
    atr_now = float(atr(kl.reset_index(drop=True), atr_len))
    atr_pct = atr_now / float(price.iloc[-1]) if price.iloc[-1] > 0 else 0.0
    
    # Расчёт базового ATR (средний за большее окно для адаптации)
    # Используем скользящее окно ATR за последние N баров (примерно 24ч данных)
    atr_base = None
    if len(kl) >= 50:  # достаточно данных для baseline
        # Рассчитываем ATR на разных точках и берём медиану
        import numpy as np
        atr_samples = []
        for i in range(max(0, len(kl) - 100), len(kl) - atr_len, 5):
            if i >= atr_len:
                window_kl = kl.reset_index(drop=True).iloc[i-atr_len:i+atr_len]
                if len(window_kl) >= atr_len:
                    atr_sample = atr(window_kl, atr_len)
                    price_sample = float(window_kl["close"].iloc[-1])
                    if price_sample > 0:
                        atr_samples.append(atr_sample / price_sample)
        
        if atr_samples:
            atr_base = float(np.median(atr_samples))  # медиана ATR% за окно
    
    # Адаптивное окно для вычисления изменений (в зависимости от таймфрейма)
    # Цель: примерно 1-3 часа данных для анализа изменений
    tf_minutes_map = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    tf_minutes = tf_minutes_map.get(tf, 60)
    
    # Для price_delta: целевое окно 1-2 часа (минимум 5 баров, максимум 20)
    target_price_window_minutes = 90  # 1.5 часа
    price_window_bars = max(5, min(20, int(target_price_window_minutes / tf_minutes)))
    price_window_bars = min(price_window_bars, len(price) - 1)  # Не больше доступных баров
    
    # Для oi_delta: используем то же окно, но ограничиваем доступными данными
    oi_window_bars = min(price_window_bars, len(oi) - 1) if len(oi) > 1 else min(5, len(oi) - 1)
    oi_window_bars = max(1, oi_window_bars)  # Минимум 1 бар
    
    # Вычисляем изменения с адаптивными окнами
    oi_delta_pct = 0.0
    if len(oi) > oi_window_bars:
        oi_delta_pct = float(rolling_pct_delta(oi, window=oi_window_bars))
    
    price_delta_pct = 0.0
    if len(price) > price_window_bars:
        price_delta_pct = float(pct_change(price.iloc[-1], price.iloc[-(price_window_bars + 1)]))
    
    # Безопасное извлечение значений из Series с проверкой на пустоту
    def safe_iloc_last(series, default=0.0):
        """Безопасное получение последнего элемента Series"""
        if series is None or len(series) == 0:
            return default
        try:
            val = float(series.iloc[-1])
            # Проверяем на NaN
            import math
            return val if not math.isnan(val) else default
        except (IndexError, ValueError, TypeError):
            return default
    
    def safe_rolling_mean(series, window, default=0.0):
        """Безопасное вычисление rolling mean с защитой от NaN"""
        if series is None or len(series) < window:
            return default
        try:
            rolling = series.rolling(window).mean()
            val = float(rolling.iloc[-1])
            import math
            return val if not math.isnan(val) else default
        except (IndexError, ValueError, TypeError):
            return default
    
    # BTC контекст по опорным ТФ режима (общий кэш, не качается заново на каждый символ)
    btc_tfs = _btc_anchor_tfs(tf)
    btc_data = btc_context(btc_tfs)
    atr_btc = 0.0
    btc_tf_kl = btc_data.get(tf)
    if btc_tf_kl is None:
        try:
            btc_tf_kl = closed_klines("BTCUSDT", tf, limit=120)
        except Exception:
            btc_tf_kl = None
    if btc_tf_kl is not None and len(btc_tf_kl) > atr_len:
        btc_close = float(btc_tf_kl["close"].iloc[-1])
        atr_btc = float(atr(btc_tf_kl.reset_index(drop=True), atr_len)) / btc_close if btc_close > 0 else 0.0

    feat: Dict[str, Any] = {
        # ---- общие / v1 ----
        "price": price,
        "volume": vol,
        "oi": oi,
        # funding приведён к 8ч, чтобы пороги значили одно и то же на парах с 1h/4h/8h
        "funding": premium["funding_8h"],
        "funding_raw": premium["funding"],
        "funding_interval_h": premium["funding_interval_h"],
        "funding_apr": premium["funding_apr"],
        "next_funding_ms": premium["next_funding_ms"],
        "basis": premium["basis"],
        "mark_price": premium["mark"],
        "taker_buy_share": safe_iloc_last(taker_share, default=0.5),
        "taker_buy_sell_ratio": safe_iloc_last(taker_ratio, default=1.0),
        "ls_top": safe_iloc_last(ls_top, default=1.0),
        "atr": atr_now,
        "atr_pct": atr_pct,
        "atr_base": atr_base,  # медианный ATR% за окно (для адаптивного фильтра)
        "price_now": safe_iloc_last(price, default=0.0),
        "price_prev": float(price.iloc[-2]) if len(price) >= 2 else safe_iloc_last(price, default=0.0),
        "volume_now": safe_iloc_last(vol, default=0.0),
        "volume_mean": safe_rolling_mean(vol, window=20, default=0.0),
        "oi_delta_pct": oi_delta_pct,
        "price_delta_pct": price_delta_pct,
        "delta_p_pct": price_delta_pct * 100.0,
        "delta_oi_pct": oi_delta_pct * 100.0,
        # ---- v2 ----
        "ohlc": kl,                       # ASC, DatetimeIndex UTC, только закрытые бары
        "oi_data": oi_df,                 # ASC: timestamp, open_interest, open_interest_value
        "funding_24h_sign": f24["sign"],
        "funding_24h_sum": f24["sum"],
        "btc_data": btc_data,             # {tf: ASC ohlc}
        "atr_btc": atr_btc,
        "stats_period": period,
        "bar_open_ms": int(kl["open_time"].iloc[-1]),
        "bar_close_ms": last_close_ms,
        "_debug_windows": {
            "tf_minutes": tf_minutes,
            "price_window_bars": price_window_bars,
            "oi_window_bars": oi_window_bars,
        }
    }
    return feat
