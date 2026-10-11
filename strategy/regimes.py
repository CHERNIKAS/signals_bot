#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
REGIMES.PY — Режимы рынка и BTC ветер

Функции:
- BTC Wind: анализ направления BTC по опорным таймфреймам
- BTC Lock: динамический lock на основе волатильности BTC
- Vol/Trend/Flow режимы (для будущих спринтов)
"""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np
import yaml


@dataclass
class BTCWindVote:
    """Голос одного таймфрейма по BTC"""
    timeframe: str
    direction: int              # +1 (UP), -1 (DOWN), 0 (NEUTRAL)
    strength: float             # Сила тренда (0-1)
    atr_pct: float              # Волатильность BTC на этом TF


@dataclass
class BTCWindMetrics:
    """Метрики BTC ветра"""
    direction: int              # Общее направление (+1, -1, 0)
    votes: List[BTCWindVote]    # Голоса по каждому TF
    consensus: float            # Консенсус (0-1, где 1 = все согласны)
    lock_minutes: int           # Динамический lock (минуты)
    
    # Детали
    base_lock_minutes: int      # Базовый lock из режима
    atr_mult: float             # Множитель от волатильности
    median_atr_pct: float       # Медианная ATR BTC


class BTCWindAnalyzer:
    """Анализатор BTC ветра"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию BTC"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            self.btc_config = config['btc']
            self.modes_config = config['modes']
    
    def analyze_btc_wind(
        self,
        btc_data: Dict[str, pd.DataFrame],
        mode: str,
        current_btc_atr_pct: float = 0.01
    ) -> BTCWindMetrics:
        """
        Анализирует BTC ветер по опорным таймфреймам
        
        Args:
            btc_data: Словарь {timeframe: ohlc_df}
                     Каждый ohlc_df должен иметь ['open', 'high', 'low', 'close']
                     отсортированный по времени DESC (новые первые)
            mode: "scalp", "intraday", "swing"
            current_btc_atr_pct: Текущая волатильность BTC (для lock mult)
        
        Returns:
            BTCWindMetrics с результатами анализа
        """
        # Опорные таймфреймы для режима
        anchor_tfs = self.btc_config['wind_anchor_tfs'][mode]
        
        # Базовый lock из режима
        base_lock_minutes = self.modes_config[mode]['btc_lock_min']
        
        votes = []
        atr_pcts = []
        
        # Анализируем каждый опорный TF
        for tf in anchor_tfs:
            if tf not in btc_data or btc_data[tf] is None or len(btc_data[tf]) < 20:
                # Нет данных - пропускаем
                continue
            
            ohlc = btc_data[tf]
            
            # Вычисляем направление и силу
            direction, strength = self._calculate_direction(ohlc)
            
            # Вычисляем ATR% для этого TF
            atr_pct = self._calculate_atr_pct(ohlc)
            atr_pcts.append(atr_pct)
            
            votes.append(BTCWindVote(
                timeframe=tf,
                direction=direction,
                strength=strength,
                atr_pct=atr_pct
            ))
        
        if not votes:
            # Нет данных - возвращаем нейтральный ветер
            return self._neutral_wind(base_lock_minutes)
        
        # Определяем общее направление (требуется 2/3 голосов)
        up_votes = sum(1 for v in votes if v.direction > 0)
        down_votes = sum(1 for v in votes if v.direction < 0)
        total_votes = len(votes)
        
        threshold = (2 * total_votes) / 3
        
        if up_votes >= threshold:
            overall_direction = +1
            consensus = up_votes / total_votes
        elif down_votes >= threshold:
            overall_direction = -1
            consensus = down_votes / total_votes
        else:
            overall_direction = 0
            consensus = max(up_votes, down_votes) / total_votes if total_votes > 0 else 0
        
        # Вычисляем динамический lock
        median_atr_pct = np.median(atr_pcts) if atr_pcts else 0.01
        atr_mult = current_btc_atr_pct / median_atr_pct if median_atr_pct > 0 else 1.0
        
        # Ограничиваем множитель (0.5 - 2.0)
        atr_mult = max(0.5, min(2.0, atr_mult))
        
        lock_minutes = int(base_lock_minutes * atr_mult)
        
        return BTCWindMetrics(
            direction=overall_direction,
            votes=votes,
            consensus=consensus,
            lock_minutes=lock_minutes,
            base_lock_minutes=base_lock_minutes,
            atr_mult=atr_mult,
            median_atr_pct=median_atr_pct
        )
    
    def _calculate_direction(
        self,
        ohlc: pd.DataFrame,
        lookback: int = 20
    ) -> tuple[int, float]:
        """
        Вычисляет направление тренда на таймфрейме
        
        Используем простую логику:
        - Сравниваем текущую цену с MA(lookback)
        - Strength = |price - MA| / MA
        
        Returns:
            (direction, strength) где direction = +1/-1/0, strength = 0-1
        """
        if len(ohlc) < lookback:
            return 0, 0.0
        
        closes = ohlc['close'].iloc[:lookback].values
        current_price = closes[0]
        ma = np.mean(closes)
        
        diff_pct = (current_price - ma) / ma
        strength = min(abs(diff_pct) / 0.02, 1.0)  # Нормализуем до 1 при 2% отклонении
        
        if diff_pct > 0.005:  # > 0.5%
            direction = +1
        elif diff_pct < -0.005:  # < -0.5%
            direction = -1
        else:
            direction = 0
            strength = 0.0
        
        return direction, strength
    
    def _calculate_atr_pct(
        self,
        ohlc: pd.DataFrame,
        period: int = 14
    ) -> float:
        """
        Вычисляет ATR% для таймфрейма
        
        Returns:
            ATR / Close (в процентах)
        """
        if len(ohlc) < period + 1:
            return 0.01  # Дефолтная волатильность 1%
        
        highs = ohlc['high'].iloc[:period+1].values
        lows = ohlc['low'].iloc[:period+1].values
        closes = ohlc['close'].iloc[:period+1].values
        
        true_ranges = []
        for i in range(period):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i+1]) if i < period else 0,
                abs(lows[i] - closes[i+1]) if i < period else 0
            )
            true_ranges.append(tr)
        
        atr = np.mean(true_ranges)
        current_price = closes[0]
        
        atr_pct = atr / current_price if current_price > 0 else 0.01
        
        return atr_pct
    
    def _neutral_wind(self, base_lock_minutes: int) -> BTCWindMetrics:
        """Возвращает нейтральный ветер (нет данных)"""
        return BTCWindMetrics(
            direction=0,
            votes=[],
            consensus=0.0,
            lock_minutes=base_lock_minutes,
            base_lock_minutes=base_lock_minutes,
            atr_mult=1.0,
            median_atr_pct=0.01
        )
    
    def check_btc_wind_gate(
        self,
        metrics: BTCWindMetrics,
        side: str
    ) -> tuple[bool, str]:
        """
        Проверяет прохождение BTC wind гейта
        
        Args:
            metrics: BTCWindMetrics
            side: "LONG" или "SHORT"
        
        Returns:
            (pass, reason)
        """
        # Если ветер нейтральный - пропускаем
        if metrics.direction == 0:
            return True, ""
        
        # Если ветер против нас - блокируем
        if side == "LONG" and metrics.direction < 0:
            return False, f"BTC wind DOWN (consensus {metrics.consensus:.0%})"
        
        if side == "SHORT" and metrics.direction > 0:
            return False, f"BTC wind UP (consensus {metrics.consensus:.0%})"
        
        return True, ""


def _get_btc_wind_analyzer() -> BTCWindAnalyzer:
    """Singleton для BTCWindAnalyzer"""
    if not hasattr(_get_btc_wind_analyzer, '_instance'):
        _get_btc_wind_analyzer._instance = BTCWindAnalyzer()
    return _get_btc_wind_analyzer._instance

