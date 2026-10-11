#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Стратегия: сбор фич + модульный пайплайн гейтов (strategy_v2)."""

from strategy.features import compute_features
from strategy.strategy_v2 import StrategyPipeline, get_pipeline

__all__ = [
    "compute_features",
    "StrategyPipeline",
    "get_pipeline",
]
