#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LEVELS.PY — Детекция и ранжирование уровней

Типы уровней:
- Swings: N-bar pivots (локальные максимумы/минимумы)
- FVG (Fair Value Gaps): разрывы в ценовом действии
- Liquidity Pools: equal highs/lows (кластеры экстремумов)
- HTF Levels: уровни со старших таймфреймов

Процесс:
1. Детекция всех уровней на текущем и HTF таймфреймах
2. Кластеризация (объединение близких уровней)
3. Ранжирование (топ-K по score)
4. Поиск "встречного" уровня для entry
"""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum
import pandas as pd
import numpy as np
import yaml


class LevelType(Enum):
    """Типы уровней"""
    SWING = "swing"
    FVG = "FVG"
    LIQUIDITY = "liquidity"
    HTF = "HTF"


@dataclass
class Level:
    """Уровень поддержки/сопротивления"""
    price: float
    level_type: LevelType
    timeframe: str
    timestamp: pd.Timestamp      # Время детекции
    
    # Метрики качества
    reaction_strength: float = 0.0   # Реакция цены (в ATR)
    touches: int = 1                 # Количество касаний
    is_broken: bool = False          # Был ли пробит
    
    # Для FVG
    fvg_low: Optional[float] = None
    fvg_high: Optional[float] = None
    
    # Для Liquidity
    equal_count: int = 0             # Количество equal highs/lows
    
    # Для HTF
    htf_zone_low: Optional[float] = None
    htf_zone_high: Optional[float] = None
    
    # Кластеризация
    cluster_id: Optional[int] = None
    cluster_size: int = 1
    
    # Итоговый score для ранжирования
    score: float = 0.0


class LevelDetector:
    """Детектор уровней всех типов"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию уровней"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)['levels']
        
        self.tf_minutes = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440
        }
    
    def detect_all_levels(
        self,
        ohlc: pd.DataFrame,
        timeframe: str,
        atr: float,
        spread: float = 0.0001,
        include_htf: bool = True
    ) -> List[Level]:
        """
        Детектирует все уровни на текущем и HTF таймфреймах
        
        Args:
            ohlc: DataFrame с колонками ['open', 'high', 'low', 'close', 'volume']
                  Index должен быть pd.DatetimeIndex
            timeframe: Текущий таймфрейм
            atr: ATR значение
            spread: Спред (для epsilon в equal highs/lows)
            include_htf: Включать ли HTF уровни
        
        Returns:
            Список всех детектированных уровней
        """
        all_levels = []
        
        # 1. Swings на текущем TF
        swings = self._detect_swings(ohlc, timeframe, atr)
        all_levels.extend(swings)
        
        # 2. FVG на текущем TF
        fvgs = self._detect_fvg(ohlc, timeframe, atr)
        all_levels.extend(fvgs)
        
        # 3. Liquidity pools на текущем TF
        liq_pools = self._detect_liquidity_pools(ohlc, timeframe, atr, spread)
        all_levels.extend(liq_pools)
        
        # 4. HTF уровни (если включено)
        if include_htf:
            htf_levels = self._detect_htf_levels(ohlc, timeframe, atr)
            all_levels.extend(htf_levels)
        
        return all_levels
    
    def _detect_swings(
        self,
        ohlc: pd.DataFrame,
        timeframe: str,
        atr: float
    ) -> List[Level]:
        """
        Детектирует swing highs/lows (N-bar pivots)
        
        Swing High: high[i] > max(high[i-k:i], high[i+1:i+k+1])
        Swing Low: low[i] < min(low[i-k:i], low[i+1:i+k+1])
        
        Где k = swing_k_by_tf[timeframe]
        """
        k = self.config['swing_k_by_tf'].get(timeframe, 2)
        levels = []
        
        highs = ohlc['high'].values
        lows = ohlc['low'].values
        timestamps = ohlc.index
        
        # Нужно как минимум 2k+1 баров
        if len(ohlc) < 2*k + 1:
            return levels
        
        # Детекция Swing Highs
        for i in range(k, len(highs) - k):
            left_max = highs[i-k:i].max()
            right_max = highs[i+1:i+k+1].max()
            
            if highs[i] > left_max and highs[i] > right_max:
                # Это swing high
                # Проверяем силу реакции (падение после пика)
                reaction = 0.0
                for j in range(i+1, min(i+k+1, len(lows))):
                    drop = highs[i] - lows[j]
                    if drop > reaction:
                        reaction = drop
                
                reaction_atr = reaction / atr if atr > 0 else 0
                
                # Фильтр: реакция должна быть ≥ 0.8×ATR
                if reaction_atr >= 0.8:
                    levels.append(Level(
                        price=highs[i],
                        level_type=LevelType.SWING,
                        timeframe=timeframe,
                        timestamp=timestamps[i],
                        reaction_strength=reaction_atr
                    ))
        
        # Детекция Swing Lows
        for i in range(k, len(lows) - k):
            left_min = lows[i-k:i].min()
            right_min = lows[i+1:i+k+1].min()
            
            if lows[i] < left_min and lows[i] < right_min:
                # Это swing low
                # Проверяем силу реакции (рост после дна)
                reaction = 0.0
                for j in range(i+1, min(i+k+1, len(highs))):
                    rise = highs[j] - lows[i]
                    if rise > reaction:
                        reaction = rise
                
                reaction_atr = reaction / atr if atr > 0 else 0
                
                # Фильтр: реакция должна быть ≥ 0.8×ATR
                if reaction_atr >= 0.8:
                    levels.append(Level(
                        price=lows[i],
                        level_type=LevelType.SWING,
                        timeframe=timeframe,
                        timestamp=timestamps[i],
                        reaction_strength=reaction_atr
                    ))
        
        return levels
    
    def _detect_fvg(
        self,
        ohlc: pd.DataFrame,
        timeframe: str,
        atr: float
    ) -> List[Level]:
        """
        Детектирует Fair Value Gaps
        
        Bullish FVG: low[i] > high[i-2]  (разрыв вверх)
        Bearish FVG: high[i] < low[i-2]  (разрыв вниз)
        
        Минимальная ширина: fvg_min_width_atr × ATR
        """
        min_width = self.config['fvg_min_width_atr'] * atr
        levels = []
        
        highs = ohlc['high'].values
        lows = ohlc['low'].values
        timestamps = ohlc.index
        
        if len(ohlc) < 3:
            return levels
        
        for i in range(2, len(ohlc)):
            # Bullish FVG
            if lows[i] > highs[i-2]:
                gap_low = highs[i-2]
                gap_high = lows[i]
                width = gap_high - gap_low
                
                if width >= min_width:
                    mid_price = (gap_low + gap_high) / 2
                    levels.append(Level(
                        price=mid_price,
                        level_type=LevelType.FVG,
                        timeframe=timeframe,
                        timestamp=timestamps[i],
                        fvg_low=gap_low,
                        fvg_high=gap_high,
                        reaction_strength=width / atr
                    ))
            
            # Bearish FVG
            elif highs[i] < lows[i-2]:
                gap_high = lows[i-2]
                gap_low = highs[i]
                width = gap_high - gap_low
                
                if width >= min_width:
                    mid_price = (gap_low + gap_high) / 2
                    levels.append(Level(
                        price=mid_price,
                        level_type=LevelType.FVG,
                        timeframe=timeframe,
                        timestamp=timestamps[i],
                        fvg_low=gap_low,
                        fvg_high=gap_high,
                        reaction_strength=width / atr
                    ))
        
        return levels
    
    def _detect_liquidity_pools(
        self,
        ohlc: pd.DataFrame,
        timeframe: str,
        atr: float,
        spread: float
    ) -> List[Level]:
        """
        Детектирует Liquidity Pools (equal highs/lows)
        
        Equal Highs/Lows: ≥2-3 экстремума в окне ε
        ε = max(atr_mult × ATR, spread_mult × spread)
        """
        eps_atr = self.config['equal_eps']['atr_mult'] * atr
        eps_spread = self.config['equal_eps']['spread_mult'] * spread
        epsilon = max(eps_atr, eps_spread)
        
        levels = []
        
        # Находим локальные максимумы (highs)
        highs = ohlc['high'].values
        high_peaks = []
        
        for i in range(1, len(highs) - 1):
            if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]:
                high_peaks.append((i, highs[i], ohlc.index[i]))
        
        # Группируем близкие максимумы
        high_clusters = self._cluster_prices(
            [(p, ts) for _, p, ts in high_peaks],
            epsilon
        )
        
        for cluster in high_clusters:
            if len(cluster) >= 2:  # Минимум 2 касания
                prices = [p for p, _ in cluster]
                timestamps = [ts for _, ts in cluster]
                avg_price = np.mean(prices)
                
                levels.append(Level(
                    price=avg_price,
                    level_type=LevelType.LIQUIDITY,
                    timeframe=timeframe,
                    timestamp=timestamps[-1],  # Последнее касание
                    equal_count=len(cluster),
                    touches=len(cluster),
                    reaction_strength=1.0  # TODO: рассчитать реакцию после касания
                ))
        
        # Аналогично для локальных минимумов (lows)
        lows = ohlc['low'].values
        low_troughs = []
        
        for i in range(1, len(lows) - 1):
            if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]:
                low_troughs.append((i, lows[i], ohlc.index[i]))
        
        low_clusters = self._cluster_prices(
            [(p, ts) for _, p, ts in low_troughs],
            epsilon
        )
        
        for cluster in low_clusters:
            if len(cluster) >= 2:
                prices = [p for p, _ in cluster]
                timestamps = [ts for _, ts in cluster]
                avg_price = np.mean(prices)
                
                levels.append(Level(
                    price=avg_price,
                    level_type=LevelType.LIQUIDITY,
                    timeframe=timeframe,
                    timestamp=timestamps[-1],
                    equal_count=len(cluster),
                    touches=len(cluster),
                    reaction_strength=1.0
                ))
        
        return levels
    
    def _detect_htf_levels(
        self,
        ohlc: pd.DataFrame,
        current_tf: str,
        atr: float
    ) -> List[Level]:
        """
        Детектирует уровни со старшего таймфрейма
        
        HTF = 1-2 таймфрейма выше текущего
        Зона HTF: ±0.2-0.3×ATR(HTF)
        
        TODO: Требует загрузки OHLC с HTF, пока заглушка
        """
        # TODO: реализовать загрузку HTF данных
        # Пока возвращаем пустой список
        return []
    
    def _cluster_prices(
        self,
        price_timestamp_pairs: List[Tuple[float, pd.Timestamp]],
        epsilon: float
    ) -> List[List[Tuple[float, pd.Timestamp]]]:
        """
        Кластеризует цены в группы, где расстояние между ценами ≤ epsilon
        
        Args:
            price_timestamp_pairs: [(price, timestamp), ...]
            epsilon: Максимальное расстояние для кластера
        
        Returns:
            Список кластеров [[( price, timestamp), ...], ...]
        """
        if not price_timestamp_pairs:
            return []
        
        # Сортируем по цене
        sorted_pairs = sorted(price_timestamp_pairs, key=lambda x: x[0])
        
        clusters = []
        current_cluster = [sorted_pairs[0]]
        
        for i in range(1, len(sorted_pairs)):
            price, ts = sorted_pairs[i]
            prev_price, _ = current_cluster[-1]
            
            if abs(price - prev_price) <= epsilon:
                # Добавляем в текущий кластер
                current_cluster.append((price, ts))
            else:
                # Начинаем новый кластер
                clusters.append(current_cluster)
                current_cluster = [(price, ts)]
        
        # Добавляем последний кластер
        if current_cluster:
            clusters.append(current_cluster)
        
        return clusters
    
    def cluster_levels(
        self,
        levels: List[Level],
        atr: float
    ) -> List[Level]:
        """
        Кластеризует уровни (объединяет близкие)
        
        Расстояние для объединения: cluster_delta_atr × ATR
        Объединение: средняя цена, суммируются метрики
        """
        if not levels:
            return []
        
        delta = self.config['cluster_delta_atr'] * atr
        
        # Сортируем по цене
        sorted_levels = sorted(levels, key=lambda l: l.price)
        
        clustered = []
        current_cluster = [sorted_levels[0]]
        cluster_id = 0
        
        for i in range(1, len(sorted_levels)):
            level = sorted_levels[i]
            prev_level = current_cluster[-1]
            
            if abs(level.price - prev_level.price) <= delta:
                # Добавляем в кластер
                current_cluster.append(level)
            else:
                # Объединяем кластер и начинаем новый
                merged = self._merge_cluster(current_cluster, cluster_id)
                clustered.append(merged)
                
                cluster_id += 1
                current_cluster = [level]
        
        # Последний кластер
        if current_cluster:
            merged = self._merge_cluster(current_cluster, cluster_id)
            clustered.append(merged)
        
        return clustered
    
    def _merge_cluster(
        self,
        cluster: List[Level],
        cluster_id: int
    ) -> Level:
        """
        Объединяет кластер уровней в один
        
        - Средняя цена
        - Максимальная reaction_strength
        - Сумма touches
        - Последний timestamp
        - Приоритет типа: HTF > LIQUIDITY > FVG > SWING
        """
        if len(cluster) == 1:
            cluster[0].cluster_id = cluster_id
            cluster[0].cluster_size = 1
            return cluster[0]
        
        # Средняя цена
        avg_price = np.mean([l.price for l in cluster])
        
        # Максимальная реакция
        max_reaction = max(l.reaction_strength for l in cluster)
        
        # Сумма касаний
        total_touches = sum(l.touches for l in cluster)
        
        # Последний timestamp
        latest_ts = max(l.timestamp for l in cluster)
        
        # Приоритет типа
        type_priority = {
            LevelType.HTF: 4,
            LevelType.LIQUIDITY: 3,
            LevelType.FVG: 2,
            LevelType.SWING: 1
        }
        best_level = max(cluster, key=lambda l: type_priority[l.level_type])
        
        # Создаём объединённый уровень
        merged = Level(
            price=avg_price,
            level_type=best_level.level_type,
            timeframe=best_level.timeframe,
            timestamp=latest_ts,
            reaction_strength=max_reaction,
            touches=total_touches,
            cluster_id=cluster_id,
            cluster_size=len(cluster)
        )
        
        # Копируем специфичные поля
        if best_level.level_type == LevelType.FVG:
            merged.fvg_low = best_level.fvg_low
            merged.fvg_high = best_level.fvg_high
        elif best_level.level_type == LevelType.LIQUIDITY:
            merged.equal_count = best_level.equal_count
        elif best_level.level_type == LevelType.HTF:
            merged.htf_zone_low = best_level.htf_zone_low
            merged.htf_zone_high = best_level.htf_zone_high
        
        return merged
    
    def rank_levels(
        self,
        levels: List[Level],
        current_price: float,
        current_time: pd.Timestamp,
        top_k: Optional[int] = None
    ) -> List[Level]:
        """
        Ранжирует уровни по score и возвращает Top-K
        
        Score = weighted_sum(
            recency, reaction, type, htf, cluster, distance
        )
        
        Веса из конфига: weights
        """
        if not levels:
            return []
        
        weights = self.config['weights']
        top_k = top_k or self.config['top_k']
        
        for level in levels:
            # 1. Recency: давность уровня (0 to 1, новее = лучше)
            time_diff_hours = (current_time - level.timestamp).total_seconds() / 3600
            recency_score = 1.0 / (1.0 + time_diff_hours / 24)  # Decay за сутки
            
            # 2. Reaction: сила реакции (уже в ATR)
            reaction_score = min(level.reaction_strength / 2.0, 1.0)  # Нормализуем до 1
            
            # 3. Type: приоритет типа
            type_scores = {
                LevelType.HTF: 1.0,
                LevelType.LIQUIDITY: 0.85,
                LevelType.FVG: 0.70,
                LevelType.SWING: 0.60
            }
            type_score = type_scores[level.level_type]
            
            # 4. HTF: бонус для HTF уровней
            htf_score = 1.0 if level.level_type == LevelType.HTF else 0.5
            
            # 5. Cluster: размер кластера
            cluster_score = min(level.cluster_size / 5.0, 1.0)  # Нормализуем до 1
            
            # 6. Distance: близость к цене (ближе = лучше, но не слишком близко)
            distance = abs(level.price - current_price)
            distance_pct = distance / current_price
            # Оптимальное расстояние: 0.5-2%
            if 0.005 <= distance_pct <= 0.02:
                distance_score = 1.0
            elif distance_pct < 0.005:
                distance_score = distance_pct / 0.005  # Штраф за слишком близкие
            else:
                distance_score = max(0, 1.0 - (distance_pct - 0.02) / 0.03)  # Decay после 2%
            
            # Итоговый score
            level.score = (
                weights['recency'] * recency_score +
                weights['reaction'] * reaction_score +
                weights['type'] * type_score +
                weights['htf'] * htf_score +
                weights['cluster'] * cluster_score +
                weights['distance'] * distance_score
            )
        
        # Сортируем по score и берём Top-K
        ranked = sorted(levels, key=lambda l: l.score, reverse=True)
        return ranked[:top_k]
    
    def find_counter_level(
        self,
        levels: List[Level],
        entry_price: float,
        side: str
    ) -> Optional[Level]:
        """
        Находит ближайший "встречный" уровень для сделки
        
        Args:
            levels: Список ранжированных уровней
            entry_price: Цена входа
            side: "LONG" или "SHORT"
        
        Returns:
            Ближайший встречный уровень или None
        """
        if side == "LONG":
            # Для LONG ищем уровень ВЫШЕ entry
            counter_levels = [l for l in levels if l.price > entry_price]
        else:  # SHORT
            # Для SHORT ищем уровень НИЖЕ entry
            counter_levels = [l for l in levels if l.price < entry_price]
        
        if not counter_levels:
            return None
        
        # Возвращаем ближайший по расстоянию
        return min(counter_levels, key=lambda l: abs(l.price - entry_price))


def _get_level_detector() -> LevelDetector:
    """Singleton для LevelDetector"""
    if not hasattr(_get_level_detector, '_instance'):
        _get_level_detector._instance = LevelDetector()
    return _get_level_detector._instance

