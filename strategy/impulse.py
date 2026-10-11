#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IMPULSE.PY — Анализ ценового импульса

Метрики импульса:
- N: размер окна в барах (base_minutes / TF_minutes, clamp к n_bounds)
- PathScore: |Close_now - Close_N| / Σ(TrueRange_N)
- Anti-chop: максимальный откат внутри окна (в ATR)
- Монотоничность:
  - body_share: доля бычьих/медвежьих тел
  - body_ratio: отношение тел к теням

Гейты по режиму (Scalp/Intraday/Swing):
- PathScore ≥ порога
- Anti-chop ≤ порога
- Монотоничность ≥ порога
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
import yaml


@dataclass
class ImpulseMetrics:
    """Метрики импульса цены"""
    n_bars: int                      # Размер окна
    pathscore: float                 # PathScore
    anti_chop_atr: float             # Максимальный откат (в ATR)
    
    # Монотоничность
    body_share: float                # Доля направленных тел (0-1)
    body_ratio: float                # Отношение тел к теням
    
    # Детали
    price_change: float              # |Close_now - Close_N|
    true_range_sum: float            # Σ(TrueRange)
    max_retracement: float           # Максимальный откат (в ценах)
    bullish_bodies: int              # Количество бычьих баров
    bearish_bodies: int              # Количество медвежьих баров
    total_body_size: float           # Сумма тел
    total_wick_size: float           # Сумма теней


class ImpulseAnalyzer:
    """Анализатор ценового импульса"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию импульса"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)['impulse']
        
        self.tf_minutes = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440
        }
    
    def calculate_impulse(
        self,
        ohlc: pd.DataFrame,
        timeframe: str,
        atr: float
    ) -> ImpulseMetrics:
        """
        Вычисляет метрики импульса
        
        Args:
            ohlc: DataFrame с колонками ['open', 'high', 'low', 'close']
                  Должен быть отсортирован по времени DESC (новые первые)
            timeframe: Текущий таймфрейм
            atr: ATR значение
        
        Returns:
            ImpulseMetrics со всеми метриками
        """
        # 1. Вычисляем N (размер окна)
        tf_min = self.tf_minutes.get(timeframe, 60)
        base_minutes = self.config['base_minutes']
        n_calculated = base_minutes / tf_min
        
        n_min, n_max = self.config['n_bounds']
        n_bars = int(np.clip(np.round(n_calculated), n_min, n_max))
        
        # Проверяем достаточность данных
        if len(ohlc) < n_bars + 1:
            return self._empty_metrics(n_bars)
        
        # Берём окно (последние n_bars + текущий бар)
        window = ohlc.iloc[:n_bars+1].copy()
        
        # 2. PathScore = |Close_now - Close_N| / Σ(TrueRange)
        close_now = window['close'].iloc[0]
        close_n = window['close'].iloc[-1]
        price_change = abs(close_now - close_n)
        
        # True Range = max(high - low, |high - prev_close|, |low - prev_close|)
        highs = window['high'].values
        lows = window['low'].values
        closes = window['close'].values
        
        true_ranges = []
        for i in range(len(window)):
            if i == len(window) - 1:
                # Последний бар - просто high - low
                tr = highs[i] - lows[i]
            else:
                prev_close = closes[i+1]
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - prev_close),
                    abs(lows[i] - prev_close)
                )
            true_ranges.append(tr)
        
        true_range_sum = sum(true_ranges)
        
        if true_range_sum > 0:
            pathscore = price_change / true_range_sum
        else:
            pathscore = 0.0
        
        # Определяем знак (направление импульса)
        if close_now > close_n:
            # Бычий импульс
            pathscore = pathscore
        else:
            # Медвежий импульс
            pathscore = -pathscore
        
        # 3. Anti-chop: максимальный откат внутри окна
        # Для бычьего импульса: макс падение от пика
        # Для медвежьего: макс рост от дна
        
        if pathscore > 0:  # Бычий
            # Находим максимум и минимум после него
            max_retracement = 0.0
            running_high = highs[0]
            
            for i in range(len(highs)):
                running_high = max(running_high, highs[i])
                retracement = running_high - lows[i]
                max_retracement = max(max_retracement, retracement)
        
        else:  # Медвежий
            # Находим минимум и максимум после него
            max_retracement = 0.0
            running_low = lows[0]
            
            for i in range(len(lows)):
                running_low = min(running_low, lows[i])
                retracement = highs[i] - running_low
                max_retracement = max(max_retracement, retracement)
        
        anti_chop_atr = max_retracement / atr if atr > 0 else 0.0
        
        # 4. Монотоничность
        opens = window['open'].values
        closes = window['close'].values
        
        bullish_bodies = 0
        bearish_bodies = 0
        total_body_size = 0.0
        total_wick_size = 0.0
        
        for i in range(len(window)):
            body = abs(closes[i] - opens[i])
            upper_wick = highs[i] - max(opens[i], closes[i])
            lower_wick = min(opens[i], closes[i]) - lows[i]
            total_wick = upper_wick + lower_wick
            
            total_body_size += body
            total_wick_size += total_wick
            
            if closes[i] > opens[i]:
                bullish_bodies += 1
            elif closes[i] < opens[i]:
                bearish_bodies += 1
        
        # body_share: доля направленных баров
        if pathscore > 0:  # Бычий импульс
            body_share = bullish_bodies / len(window) if len(window) > 0 else 0.0
        else:  # Медвежий импульс
            body_share = bearish_bodies / len(window) if len(window) > 0 else 0.0
        
        # body_ratio: тела / тени
        if total_wick_size > 0:
            body_ratio = total_body_size / total_wick_size
        else:
            body_ratio = total_body_size / 1e-6  # Избегаем деления на 0
        
        return ImpulseMetrics(
            n_bars=n_bars,
            pathscore=pathscore,
            anti_chop_atr=anti_chop_atr,
            body_share=body_share,
            body_ratio=body_ratio,
            price_change=price_change,
            true_range_sum=true_range_sum,
            max_retracement=max_retracement,
            bullish_bodies=bullish_bodies,
            bearish_bodies=bearish_bodies,
            total_body_size=total_body_size,
            total_wick_size=total_wick_size
        )
    
    def check_impulse_gate(
        self,
        metrics: ImpulseMetrics,
        mode: str
    ) -> tuple[bool, str]:
        """
        Проверяет прохождение импульсного гейта
        
        Args:
            metrics: ImpulseMetrics
            mode: "scalp", "intraday", "swing"
        
        Returns:
            (pass, reason) где pass=True если прошёл, reason - причина BAN
        """
        # Пороги из конфига
        pathscore_min = self.config['pathscore_min'][mode]
        anti_chop_max = self.config['anti_chop_atr'][mode]
        
        mono_config = self.config['monotonicity']
        body_share_min = mono_config['body_share_min'][mode]
        body_ratio_min = mono_config['body_ratio_min'][mode]
        
        # Проверка PathScore
        if abs(metrics.pathscore) < pathscore_min:
            return False, f"|PathScore|={abs(metrics.pathscore):.3f} < {pathscore_min}"
        
        # Проверка Anti-chop
        if metrics.anti_chop_atr > anti_chop_max:
            return False, f"Anti-chop={metrics.anti_chop_atr:.3f}×ATR > {anti_chop_max}"
        
        # Проверка Монотоничности
        if metrics.body_share < body_share_min:
            return False, f"Body share={metrics.body_share:.2%} < {body_share_min:.2%}"
        
        if metrics.body_ratio < body_ratio_min:
            return False, f"Body ratio={metrics.body_ratio:.2f} < {body_ratio_min}"
        
        return True, ""
    
    def _empty_metrics(self, n_bars: int) -> ImpulseMetrics:
        """Возвращает пустые метрики (недостаточно данных)"""
        return ImpulseMetrics(
            n_bars=n_bars,
            pathscore=0.0,
            anti_chop_atr=0.0,
            body_share=0.0,
            body_ratio=0.0,
            price_change=0.0,
            true_range_sum=0.0,
            max_retracement=0.0,
            bullish_bodies=0,
            bearish_bodies=0,
            total_body_size=0.0,
            total_wick_size=0.0
        )


def _get_impulse_analyzer() -> ImpulseAnalyzer:
    """Singleton для ImpulseAnalyzer"""
    if not hasattr(_get_impulse_analyzer, '_instance'):
        _get_impulse_analyzer._instance = ImpulseAnalyzer()
    return _get_impulse_analyzer._instance

