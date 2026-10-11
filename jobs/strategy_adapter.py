#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STRATEGY_ADAPTER.PY — Адаптер для интеграции strategy_v2 в jobs.py

Этот модуль предоставляет простой интерфейс для перехода со старой стратегии на новую.
"""

from typing import Dict, Any, Optional
from strategy.logs import SignalLog, SignalStatus
from strategy.strategy_v2 import StrategyPipeline
from handlers.message_render import render_signal_card
from database import db_log_signal_ban


import threading

# Глобальный экземпляр пайплайна (создаётся один раз, потокобезопасно)
_pipeline = None
_pipeline_lock = threading.Lock()


def get_strategy_pipeline() -> StrategyPipeline:
    """Возвращает синглтон экземпляр StrategyPipeline"""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = StrategyPipeline()
                print("[STRATEGY_ADAPTER] StrategyPipeline initialized")
    return _pipeline


def evaluate_signal_v2(
    symbol: str,
    timeframe: str,
    features: Dict[str, Any],
    uid: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Вычисляет сигнал используя strategy_v2
    
    Args:
        symbol: Торговая пара (например, "BTCUSDT")
        timeframe: Таймфрейм (например, "1h")
        features: Словарь с данными рынка:
            - price_now: текущая цена
            - atr: ATR значение
            - atr_pct: ATR в процентах
            - ohlc: DataFrame с OHLC данными
            - oi_data: данные Open Interest
            - funding: funding rate
            - basis: basis (спот - фьючерс)
            - taker_buy_share: доля покупателей
            - btc_data: данные BTC
            - levels: предрасчитанные уровни (опционально)
        uid: User ID для сохранения ban логов (опционально)
    
    Returns:
        Dict с результатом:
        {
            "signal": {side, entry, sl, tp1, tp2, ...} или None,
            "log": SignalLog,
            "reason": str,
            "symbol": str
        }
    """
    pipeline = get_strategy_pipeline()
    
    # Вызываем pipeline
    result = pipeline.evaluate_signal(
        symbol=symbol,
        timeframe=timeframe,
        features=features
    )
    
    # result это dict с ключами: signal, log, reason
    log = result.get("log")
    
    if log and log.status == SignalStatus.PASSED:
        print(f"[STRATEGY_ADAPTER] ✅ PASSED: {symbol} {timeframe} {log.side}")
        return result
    elif log and log.status == SignalStatus.BANNED:
        print(f"[STRATEGY_ADAPTER] ❌ BANNED: {symbol} {timeframe} - {[r.value for r in log.ban_reasons[:3]]}")
        # Сохраняем ban лог в БД для статистики
        if uid:
            try:
                db_log_signal_ban(uid, log.to_dict())
            except Exception as e:
                print(f"[WARN] Failed to save ban log: {e}")
    
    return None


def format_signal_message_v2(log: SignalLog, tv_url: Optional[str] = None, levels_url: Optional[str] = None, uid: Optional[int] = None) -> str:
    """
    Форматирует SignalLog в HTML-сообщение для Telegram
    
    Args:
        log: SignalLog с заполненными полями
        tv_url: Ссылка на TradingView график (опционально)
        levels_url: Ссылка на страницу уровней (опционально)
        uid: User ID для проверки настроек TPmid (опционально)
    
    Returns:
        HTML-форматированное сообщение
    """
    return render_signal_card(log, tv_url=tv_url, levels_url=levels_url, uid=uid)


def convert_log_to_old_format(log: SignalLog) -> Dict[str, Any]:
    """
    Конвертирует SignalLog в старый dict формат для обратной совместимости
    
    Args:
        log: SignalLog объект
    
    Returns:
        Dict в формате старой стратегии
    """
    return {
        "signal": {
            "symbol": log.symbol,
            "side": log.side,
            "entry": log.entry_price,
            "sl": log.sl_final,
            "tp1": log.tp1_final,
            "tp2": log.tp2_final,
            "lifetime": f"{log.hold_window_min[0]}-{log.hold_window_min[1]}m",
            "trail": log.trail_atr * log.atr if log.atr > 0 else 0,
        },
        "features": {
            "close": log.entry_price,
            "atr": log.atr,
            "atr%": log.atr_pct * 100,
            # Добавьте другие features если нужно
        },
        "reason": log.why if log.why else f"{log.trigger_type} signal",
    }
