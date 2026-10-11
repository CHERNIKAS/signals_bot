#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Логика стратегии и генерации сигналов"""

# ========== V2 СТРАТЕГИЯ (НОВАЯ МОДУЛЬНАЯ) ==========
from strategy.strategy_v2 import StrategyPipeline

# Синглтон пайплайна
_pipeline = None

def get_pipeline() -> StrategyPipeline:
    """Возвращает глобальный экземпляр StrategyPipeline"""
    global _pipeline
    if _pipeline is None:
        _pipeline = StrategyPipeline()
    return _pipeline

# ========== СТАРАЯ СТРАТЕГИЯ (для обратной совместимости) ==========
from strategy.strategy import (
    compute_features as _compute_features_old,
    evaluate_signal as _evaluate_signal_old,
    btc_trend_filter,
    check_btc_trend_block,
    oi_spike_without_volume,
)

# Алиасы (временно для совместимости)
compute_features = _compute_features_old
evaluate_signal = _evaluate_signal_old

__all__ = [
    # V2
    "StrategyPipeline",
    "get_pipeline",
    # Старые (алиасы)
    "compute_features",
    "evaluate_signal",
    "btc_trend_filter",
    "check_btc_trend_block",
    "oi_spike_without_volume",
]
