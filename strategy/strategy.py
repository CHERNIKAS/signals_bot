#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Стратегия: логика генерации сигналов, правила LONG/SHORT, SL/TP/Trail"""

from typing import Dict, Any, Optional, List

from binance import (
    binance_klines,
    binance_premium_index,
    binance_open_interest_hist,
    binance_taker_long_short,
    binance_ls_top_accounts,
    atr,
    rolling_pct_delta,
    pct_change,
)
from config import (
    CONFIG,
    GLOBAL_MIN_ATR, GLOBAL_MAX_ATR,
    TAKER_REQ_LOWLIQ, TAKER_REQ_NORMAL, TAKER_REQ_HIGHLIQ,
    QUIET_MULT_LOWVOL, QUIET_MULT_NORMAL, QUIET_MULT_HIGHVOL,
    LOW_VOL_THRESHOLD, HIGH_VOL_THRESHOLD,
    ATR_RELATIVE_MIN, ATR_RELATIVE_MAX,
    get_liquidity_regime, is_time_banned,
)


# =============================================================================
# ADAPTIVE FILTERS: Helper Functions
# =============================================================================

def get_liquidity_session(now_ts: Optional[float] = None) -> str:
    """
    Определяет текущую сессию ликвидности по UTC времени.
    Адаптер над get_liquidity_regime() для обратной совместимости.
    
    Returns:
        "asia_night"  - низкая ликвидность (00:00-06:00 UTC)
        "normal"      - обычная ликвидность (06:00-13:00 UTC, 20:00-24:00 UTC)
        "us_open"     - высокая ликвидность (13:00-20:00 UTC)
    """
    regime = get_liquidity_regime(now_ts)
    
    # Маппинг из нового формата в старый
    if regime == "low":
        return "asia_night"
    elif regime == "high":
        return "us_open"
    else:
        return "normal"


def get_required_taker_ratio(session: str, side: str) -> float:
    """
    Возвращает требуемый порог taker ratio в зависимости от сессии.
    
    Args:
        session: "asia_night" | "normal" | "us_open"
        side: "LONG" | "SHORT"
    
    Returns:
        Минимальный порог для LONG (buy aggression ≥ threshold)
        Максимальный порог для SHORT (buy aggression ≤ threshold)
    
    Пороги для SHORT:
        - asia_night: 0.50 (было 0.48, смягчено - допускаем умеренных покупателей)
        - normal: 0.48 (было 0.45, смягчено - обычная сессия)
        - us_open: 0.50 (было 0.48, смягчено - не требуем капитуляции)
    """
    if side == "LONG":
        # Для лонгов: требуем агрессию покупателей
        if session == "asia_night":
            return TAKER_REQ_LOWLIQ      # 0.52 (не изменялось)
        elif session == "us_open":
            return TAKER_REQ_HIGHLIQ     # 0.54 (было 0.57, смягчено)
        else:  # normal
            return TAKER_REQ_NORMAL      # 0.53 (было 0.55, смягчено)
    else:  # SHORT
        # Для шортов: требуем отсутствие агрессии покупателей
        if session == "asia_night":
            return 0.50  # смягчено с 0.48 - допускаем умеренную активность покупателей
        elif session == "us_open":
            return 0.50  # смягчено с 0.48 - НЕ требуем полной капитуляции
        else:  # normal
            return 0.48  # смягчено с 0.45 - обычная сессия


def get_quiet_multiplier(atr_pct_now: float) -> tuple[float, str]:
    """
    Возвращает динамический множитель для quiet_range в зависимости от волатильности.
    
    Args:
        atr_pct_now: Текущая волатильность в процентах (0.01 = 1%)
    
    Returns:
        (multiplier, regime_name) - множитель для порога quiet_range и название режима
    """
    if atr_pct_now < LOW_VOL_THRESHOLD:
        return (QUIET_MULT_LOWVOL, "LOW_VOL")    # спокойный рынок - более мягкий порог
    elif atr_pct_now > HIGH_VOL_THRESHOLD:
        return (QUIET_MULT_HIGHVOL, "HIGH_VOL")  # бешеный рынок - более жёсткий порог
    else:
        return (QUIET_MULT_NORMAL, "NORMAL_VOL") # обычный рынок


def is_atr_context_ok(atr_now: float, atr_base: Optional[float] = None) -> tuple[bool, str]:
    """
    Проверяет ATR в контексте нормальной волатильности монеты.
    
    Args:
        atr_now: Текущий atr_pct
        atr_base: Средний atr_pct за последние 24ч (если None - пропускаем проверку)
    
    Returns:
        (ok: bool, reason: str)
    """
    # Если нет базового ATR - пропускаем адаптивную проверку
    if atr_base is None or atr_base <= 0:
        return (True, "atr_ok_no_baseline")
    
    # Слишком тихо относительно нормы монеты
    if atr_now < ATR_RELATIVE_MIN * atr_base:
        return (False, f"too_flat_for_this_pair (now={atr_now:.4f} < {ATR_RELATIVE_MIN}×base={atr_base:.4f})")
    
    # Слишком бешено относительно нормы монеты
    if atr_now > ATR_RELATIVE_MAX * atr_base:
        return (False, f"too_crazy_for_this_pair (now={atr_now:.4f} > {ATR_RELATIVE_MAX}×base={atr_base:.4f})")
    
    return (True, "atr_ok")


# =============================================================================
# FEATURES COMPUTATION
# =============================================================================

def compute_features(symbol: str, tf: str, lookback: int) -> Dict[str, Any]:
    """
    Вычисляет все технические индикаторы и фичи для анализа рынка.
    Собирает данные: свечи, премия, OI, taker ratio, top accounts.
    """
    kl = binance_klines(symbol, tf, limit=lookback).reset_index(drop=True)
    if kl is None or len(kl) < 30:
        raise RuntimeError(f"Not enough klines for {symbol}")

    premium = binance_premium_index(symbol)
    oi = binance_open_interest_hist(symbol, period=tf, limit=min(lookback, 30))
    taker_ratio, taker_share = binance_taker_long_short(symbol, period=tf, limit=min(lookback, 30))
    ls_top = binance_ls_top_accounts(symbol, period=tf, limit=min(lookback, 30))

    price = kl["close"]
    vol = kl["volume"]
    
    # Расчёт ATR и волатильности
    atr_len = CONFIG["sl_tp"]["atr_len"]
    atr_now = float(atr(kl, atr_len))
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
                window_kl = kl.iloc[i-atr_len:i+atr_len]
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
    
    feat: Dict[str, Any] = {
        "price": price,
        "volume": vol,
        "oi": oi,
        "funding": premium["funding"],
        "basis": premium["basis"],
        "taker_buy_share": safe_iloc_last(taker_share, default=0.5),
        "taker_buy_sell_ratio": safe_iloc_last(taker_ratio, default=1.0),
        "ls_top": safe_iloc_last(ls_top, default=1.0),
        "atr": atr_now,
        "atr_pct": atr_pct,
        "atr_base": atr_base,  # средний ATR% за окно (для адаптивного фильтра)
        "price_now": safe_iloc_last(price, default=0.0),
        "price_prev": float(price.iloc[-2]) if len(price) >= 2 else safe_iloc_last(price, default=0.0),
        "volume_now": safe_iloc_last(vol, default=0.0),
        "volume_mean": safe_rolling_mean(vol, window=20, default=0.0),
        "oi_delta_pct": oi_delta_pct,
        "price_delta_pct": price_delta_pct,
        "_debug_windows": {  # Для отладки (можно удалить в проде)
            "tf_minutes": tf_minutes,
            "price_window_bars": price_window_bars,
            "oi_window_bars": oi_window_bars,
        }
    }
    return feat


# =============================================================================
# BTC TREND FILTER
# =============================================================================

def btc_trend_filter(tf: str) -> Optional[str]:
    """
    Определяет тренд BTC на основе EMA50.
    Возвращает: "UP" если цена > EMA50, "DOWN" если <
    """
    from binance import ema
    
    kl = binance_klines("BTCUSDT", tf, limit=200)
    if kl is None or len(kl) < 50:
        return None
    
    close = kl["close"]
    ema50 = ema(close, 50)
    last = float(close.iloc[-1])
    
    return "UP" if last > ema50 else "DOWN"


def check_btc_trend_block(side: str) -> bool:
    """
    Проверяет, блокирует ли тренд BTC данный сигнал.
    Анализирует BTC на нескольких таймфреймах и блокирует противотренд.
    """
    dirs: List[str] = []
    for tf in CONFIG["btc_trend_timeframes"]:
        d = btc_trend_filter(tf)
        if d:
            dirs.append(d)
    
    if not dirs:
        return False
    
    up_count = sum(1 for d in dirs if d == "UP")
    down_count = sum(1 for d in dirs if d == "DOWN")
    
    if side == "LONG" and down_count >= len(dirs):
        return True
    if side == "SHORT" and up_count >= len(dirs):
        return True
    
    return False


# =============================================================================
# SIGNAL EVALUATION RULES
# =============================================================================

def oi_spike_without_volume(feat: Dict[str, Any], sigma_mult: float) -> bool:
    """
    Проверяет наличие спайка OI без подтверждения объёмом.
    Используется для фильтрации манипуляций рынком.
    """
    oi = feat["oi"]
    if len(oi) < 20:  # Минимум 20 для rolling(20)
        return False
    
    # Безопасное вычисление rolling mean и std с проверкой на NaN
    import math
    rolling_mean = oi.rolling(20).mean()
    rolling_std = oi.rolling(20).std()
    
    mu = float(rolling_mean.iloc[-1])
    sd_val = float(rolling_std.iloc[-1]) if len(rolling_std) > 0 else 1e-9
    
    # Проверка на NaN
    if math.isnan(mu):
        mu = float(oi.iloc[-1])  # Fallback к последнему значению
    if math.isnan(sd_val) or sd_val <= 0:
        sd_val = 1e-9
    
    oi_last = float(oi.iloc[-1])
    if math.isnan(oi_last):
        return False
    
    spike = (oi_last - mu) / sd_val if sd_val > 0 else 0.0
    vol_ok = feat["volume_now"] > feat["volume_mean"]
    
    return (spike >= sigma_mult) and (not vol_ok)


def evaluate_signal(symbol: str, feat: Dict[str, Any]) -> Dict[str, Any]:
    """
    Анализирует фичи и генерирует торговый сигнал (LONG/SHORT/None).
    Применяет правила на основе basis, funding, OI, taker ratio.
    
    Возвращает структуру сигнала БЕЗ проверки cooldown (её делаем снаружи).
    """
    # Проверка времени бана (самая первая проверка)
    time_banned, ban_reason = is_time_banned()
    if time_banned:
        print(f"[STRATEGY] {symbol} REJECTED: {ban_reason}")
        return {
            "symbol": symbol,
            "signal": None,
            "reason": ban_reason,
            "reject_details": {
                "gate": "TIME_BAN",
                "reason": ban_reason
            }
        }
    
    th = CONFIG["thresholds"]
    ΔP = feat["price_delta_pct"]
    ΔOI = feat["oi_delta_pct"]
    basis = feat["basis"]
    funding = feat["funding"]
    ls_top = feat["ls_top"]
    taker_share = feat["taker_buy_share"]

    long_candidates: List[str] = []
    short_candidates: List[str] = []

    # LONG rules
    if (ΔP > 0) and (ΔOI > 0) and (basis > 0) and (funding >= 0) and (ls_top <= 2.5):
        long_candidates.append("ΔP↑ & ΔOI↑ & Basis↑ & Funding≥0 & LS≤2.5")

    if (ΔP < 0) and (feat["volume_now"] >= feat["volume_mean"]):
        if (ΔOI < 0) or (funding <= 0):
            long_candidates.append("Тест спроса (эвристика) & (ΔOI↓ или Funding≤0)")

    # SHORT rules
    # Rule #1: Ловушка вверх (цена растет, но OI не подтверждает)
    if (ΔP > 0) and (ΔOI <= 0.001) and (basis <= 0):
        short_candidates.append("ΔP↑ & ΔOI↓/≈ & Basis↓/≈ (ловушка)")
    
    # Rule #2: Цена растет, OI не подтверждён И есть всплеск TakerBuy (более специфичное условие)
    # Проверяем только если Rule #1 не сработал ИЛИ если есть дополнительное условие taker_share
    if (ΔP > 0) and (ΔOI <= 0.0):
        if taker_share >= 0.6:
            # Более специфичное условие - добавляем только если не было Rule #1
            if not ((ΔOI <= 0.001) and (basis <= 0)):
                short_candidates.append("ΔP↑ & ΔOI не подтверждён & TakerBuy всплеск")
        # Если Rule #1 сработал, но basis > 0, то добавляем вариант без basis
        elif (ΔOI <= 0.001) and (basis > 0):
            short_candidates.append("ΔP↑ & ΔOI↓/≈ но Basis↑ (ослабленная ловушка)")

    # Rule #3: Лонги зажаты (цена падает, OI растет, funding положительный)
    if (ΔP < 0) and (ΔOI > 0) and (funding > 0):
        short_candidates.append("ΔP↓ & ΔOI↑ & Funding↑ (лонги зажаты)")

    # Only side filter
    # Приоритет: если оба условия выполняются, выбираем по приоритету (SHORT имеет приоритет)
    only_side: Optional[str] = None
    if (ls_top > th["ls_top_only_short"]) and (funding > 0):
        only_side = "SHORT"
    elif (ls_top < th["ls_top_only_long"]) and (funding < 0):
        only_side = "LONG"

    # Адаптивная проверка taker ratio (по сессии ликвидности)
    session = get_liquidity_session()
    
    def taker_ok(side: str) -> tuple[bool, str]:
        """Проверка taker ratio с адаптивным порогом"""
        required = get_required_taker_ratio(session, side)
        
        if side == "LONG":
            ok = taker_share >= required
            reason = f"taker={taker_share:.3f}<{required:.3f} (session={session})" if not ok else ""
        else:  # SHORT
            ok = taker_share <= required
            reason = f"taker={taker_share:.3f}>{required:.3f} (session={session})" if not ok else ""
        
        return (ok, reason)

    def basis_ok(side: str) -> bool:
        if side == "LONG":
            return basis >= th["basis_long_floor"]
        else:
            return True

    # OI spike filter
    oi_spike_threshold = CONFIG["thresholds"]["oi_spike_without_vol"]
    if oi_spike_without_volume(feat, oi_spike_threshold):
        print(f"[STRATEGY] {symbol} REJECTED: gate=OI_SPIKE_NO_VOLUME")
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": "oi_spike_without_volume",
            "reject_details": {
                "gate": "OI_SPIKE_NO_VOLUME",
                "oi_delta_pct": feat.get("oi_delta_pct", 0),
                "price_delta_pct": feat.get("price_delta_pct", 0),
                "threshold": oi_spike_threshold
            }
        }

    # Проверяем taker ratio (адаптивно)
    taker_long_ok, taker_long_reason = taker_ok("LONG")
    taker_short_ok, taker_short_reason = taker_ok("SHORT")
    
    long_ok = len(long_candidates) > 0 and taker_long_ok and basis_ok("LONG")
    short_ok = len(short_candidates) > 0 and taker_short_ok and basis_ok("SHORT")
    
    # Детальное логирование причин блокировки
    long_reject_reason = []
    short_reject_reason = []
    
    if len(long_candidates) > 0:
        if not taker_long_ok:
            long_reject_reason.append(taker_long_reason)
        if not basis_ok("LONG"):
            long_reject_reason.append(f"basis={basis:.4f}<{th['basis_long_floor']}")
    
    if len(short_candidates) > 0:
        if not taker_short_ok:
            short_reject_reason.append(taker_short_reason)
        if not basis_ok("SHORT"):
            short_reject_reason.append("basis_fail")

    if only_side == "LONG":
        short_ok = False
        if len(short_candidates) > 0:
            short_reject_reason.append("only_side=LONG")
    if only_side == "SHORT":
        long_ok = False
        if len(long_candidates) > 0:
            long_reject_reason.append("only_side=SHORT")

    side: Optional[str] = None
    reasons: List[str] = []
    
    if long_ok and not short_ok:
        side = "LONG"
        reasons = long_candidates
    elif short_ok and not long_ok:
        side = "SHORT"
        reasons = short_candidates
    elif long_ok and short_ok:
        side = "LONG" if (funding >= 0 and ΔOI > 0 and basis > 0) else "SHORT"
        reasons = (long_candidates if side == "LONG" else short_candidates)

    if not side:
        # DEBUG: почему нет сигнала (детально)
        print(f"[STRATEGY] {symbol} no signal: ΔP={ΔP:.3f}%, ΔOI={ΔOI:.3f}%, basis={basis:.4f}, funding={funding:.4f}, ls_top={ls_top:.2f}, taker={taker_share:.3f}")
        print(f"[STRATEGY] {symbol} long_cand={len(long_candidates)}, short_cand={len(short_candidates)}, long_ok={long_ok}, short_ok={short_ok}, only_side={only_side}")
        if long_reject_reason:
            print(f"[STRATEGY] {symbol} LONG rejected: {', '.join(long_reject_reason)}")
        if short_reject_reason:
            print(f"[STRATEGY] {symbol} SHORT rejected: {', '.join(short_reject_reason)}")
        
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": "no_rule_matched",
            "reject_details": {
                "gate": "FINAL_FILTER",
                "long_candidates": len(long_candidates),
                "short_candidates": len(short_candidates),
                "long_ok": long_ok,
                "short_ok": short_ok,
                "long_reject_reasons": long_reject_reason,
                "short_reject_reasons": short_reject_reason,
                "only_side": only_side,
                "session": session,
                "metrics": {
                    "delta_price_pct": ΔP,
                    "delta_oi_pct": ΔOI,
                    "basis": basis,
                    "funding": funding,
                    "ls_top": ls_top,
                    "taker_share": taker_share
                }
            }
        }

    if check_btc_trend_block(side):
        btc_state = feat.get("btc_trend", "unknown")
        print(f"[STRATEGY] {symbol} {side} REJECTED: gate=BTC_HEADWIND, btc_trend={btc_state}")
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": f"btc_headwind_{side.lower()}",
            "reject_details": {
                "gate": "BTC_HEADWIND",
                "side": side,
                "btc_trend": btc_state
            }
        }

    # ======= ADAPTIVE ATR FILTER =======
    a = max(1e-6, feat["atr"])          # ATR на рабочем ТФ
    price = float(feat["price_now"])
    atr_pct = feat.get("atr_pct", a / price)
    atr_base = feat.get("atr_base")     # средний ATR за окно
    
    # 1. Глобальные жёсткие лимиты (защита бренда)
    if atr_pct < GLOBAL_MIN_ATR:
        print(f"[ADAPTIVE] {symbol} REJECTED: gate=ATR_ABSOLUTE, atr_pct={atr_pct:.4f} < GLOBAL_MIN={GLOBAL_MIN_ATR:.4f}")
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": "atr_too_low_absolute",
            "reject_details": {
                "gate": "ATR_ABSOLUTE",
                "atr_pct": atr_pct,
                "atr_abs": float(a),
                "limits": [GLOBAL_MIN_ATR, GLOBAL_MAX_ATR],
                "threshold": GLOBAL_MIN_ATR
            }
        }
    
    if atr_pct > GLOBAL_MAX_ATR:
        print(f"[ADAPTIVE] {symbol} REJECTED: gate=ATR_ABSOLUTE, atr_pct={atr_pct:.4f} > GLOBAL_MAX={GLOBAL_MAX_ATR:.4f}")
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": "atr_too_high_absolute",
            "reject_details": {
                "gate": "ATR_ABSOLUTE",
                "atr_pct": atr_pct,
                "atr_abs": float(a),
                "limits": [GLOBAL_MIN_ATR, GLOBAL_MAX_ATR],
                "threshold": GLOBAL_MAX_ATR
            }
        }
    
    # 2. Адаптивная проверка относительно нормы монеты
    atr_ok, atr_reason = is_atr_context_ok(atr_pct, atr_base)
    if not atr_ok:
        print(f"[ADAPTIVE] {symbol} REJECTED: gate=ATR_CONTEXT, reason={atr_reason}, atr_pct={atr_pct:.4f}, atr_base={atr_base:.4f if atr_base else 'N/A'}")
        return {
            "symbol": symbol, 
            "signal": None, 
            "reason": atr_reason,
            "reject_details": {
                "gate": "ATR_CONTEXT",
                "atr_pct": atr_pct,
                "atr_abs": float(a),
                "atr_base": atr_base,
                "reason": atr_reason,
                "limits": [GLOBAL_MIN_ATR, GLOBAL_MAX_ATR],
                "relative_limits": [ATR_RELATIVE_MIN, ATR_RELATIVE_MAX]
            }
        }
    
    # ======= ADAPTIVE QUIET-RANGE FILTER =======
    quiet_n = int(th.get("quiet_tr_bars", 0))
    if quiet_n >= 1 and "price" in feat:
        # Динамический множитель в зависимости от волатильности
        quiet_mult, vol_regime = get_quiet_multiplier(atr_pct)
        
        tr = (feat["price"].diff().abs()).fillna(0)  # быстрая прокси TrueRange
        if len(tr) >= quiet_n:
            threshold = quiet_mult * a
            range_nbars = float(tr.tail(quiet_n).max())
            
            if (tr.tail(quiet_n) < threshold).all():
                print(f"[ADAPTIVE] {symbol} REJECTED: gate=QUIET_RANGE, last_{quiet_n}_bars={range_nbars:.6f} < {quiet_mult:.2f}×ATR={threshold:.6f}, atr_pct={atr_pct:.4f}, regime={vol_regime}")
                return {
                    "symbol": symbol, 
                    "signal": None, 
                    "reason": "quiet_range_block",
                    "reject_details": {
                        "gate": "QUIET_RANGE", 
                        "quiet_mult": quiet_mult, 
                        "vol_regime": vol_regime,
                        "atr_pct": atr_pct,
                        "atr_abs": float(a),
                        "range_nbars": range_nbars,
                        "threshold": float(threshold),
                        "n_bars": quiet_n
                    }
                }

    # ======= SL/TP CALCULATION (FROM atr_pct WITH CLIPPERS) =======
    # Дополнительная проверка на валидность цены
    if price <= 0:
        print(f"[ERROR] {symbol} REJECTED: invalid price={price} for SL/TP calculation")
        return {
            "symbol": symbol,
            "signal": None,
            "reason": "invalid_price",
            "reject_details": {
                "gate": "PRICE_VALIDATION",
                "price": price
            }
        }
    
    risk = CONFIG["risk"]
    sl_lo, sl_hi = CONFIG["sl_tp"]["scalp_sl_pct_range"]

    # Стоп берём равным atr_pct, но клипуем диапазоном
    sl_pct = max(sl_lo, min(sl_hi, atr_pct))
    if side == "LONG":
        sl = price * (1 - sl_pct)
        # Защита от отрицательного SL
        if sl <= 0:
            sl = price * 0.95  # Fallback: 5% ниже цены
    else:
        sl = price * (1 + sl_pct)

    tp1_lo, tp1_hi = CONFIG["sl_tp"]["tp1_pct_range"]
    tp2_lo, tp2_hi = CONFIG["sl_tp"]["tp2_pct_range"]

    # Базовые коэффициенты TP к ATR
    tp1_pct = max(tp1_lo, min(tp1_hi, 0.9 * atr_pct))
    tp2_pct = max(tp2_lo, min(tp2_hi, 2.7 * atr_pct))

    if side == "LONG":
        tp1 = price * (1 + tp1_pct)
        tp2 = price * (1 + tp2_pct)
        # Защита от отрицательных TP для LONG
        if tp1 <= price:
            tp1 = price * 1.01  # Fallback: 1% выше цены
        if tp2 <= price:
            tp2 = price * 1.02  # Fallback: 2% выше цены
    else:
        tp1 = price * (1 - tp1_pct)
        tp2 = price * (1 - tp2_pct)
        # Защита от отрицательных TP для SHORT (tp должно быть меньше цены)
        if tp1 >= price:
            tp1 = price * 0.99  # Fallback: 1% ниже цены
        if tp2 >= price:
            tp2 = price * 0.98  # Fallback: 2% ниже цены

    trail_step = CONFIG["sl_tp"]["trail_step_atr"] * a

    # Размер позиции как и раньше
    equity = float(risk["equity"])
    k_pct = float(risk["k_pct"])
    # Защита от деления на ноль
    if price > 0:
        qty = (equity * k_pct) / price
    else:
        print(f"[WARN] {symbol} price is zero or negative: {price}, cannot calculate qty")
        qty = 0.0
    life = f"scalp {CONFIG['lifetime']['scalp_min']}-{CONFIG['lifetime']['scalp_max']}m"

    return {
        "symbol": symbol,
        "signal": {
            "side": side,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "trail_step": trail_step,
            "qty_est": qty,
            "lifetime": life
        },
        "reason": "; ".join(reasons),
        "features": {
            "ΔP%": round(100 * feat["price_delta_pct"], 3),
            "ΔOI%": round(100 * feat["oi_delta_pct"], 3),
            "basis%": round(100 * feat["basis"], 3),
            "funding%": round(100 * feat["funding"], 4),
            "LS_top": round(feat["ls_top"], 3),
            "TakerBuyShare": round(feat["taker_buy_share"], 3),
            "ATR": round(feat["atr"], 4),
        },
    }