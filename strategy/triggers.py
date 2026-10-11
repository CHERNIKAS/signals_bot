#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TRIGGERS.PY — Типы триггеров и их поправки

Типы триггеров:
- TC (Trend Continuation): продолжение тренда
- BR (Breakout Retest): ретест пробоя
- FVR (FVG Retest): ретест FVG
- LSR (Level Support/Resistance): отскок от уровня
- EPS (Exhaustion Push Short): исто

щение покупателей
- LQS (Liquidity Squeeze): ликвидационный сквиз

Каждый триггер имеет свои поправки к геометрии (SL/TP/Trail)
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
from enum import Enum
import yaml


class TriggerType(Enum):
    """Типы триггеров"""
    TC = "TC"       # Trend Continuation
    BR = "BR"       # Breakout Retest
    FVR = "FVR"     # FVG Retest
    LSR = "LSR"     # Level Support/Resistance
    EPS = "EPS"     # Exhaustion Push Short
    LQS = "LQS"     # Liquidity Squeeze


@dataclass
class TriggerAdjustments:
    """Поправки триггера к геометрии сделки"""
    trigger_type: TriggerType
    
    # SL adjustments
    sl_anchor: Optional[str] = None          # "retest_level", "fvg_far_edge", etc.
    sl_buffer_add_atr: float = 0.0          # Дополнительный буфер к SL
    sl_min_add_atr_pct: float = 0.0         # Добавка к минимальному SL
    
    # TP adjustments
    tp1_add_atr_pct: float = 0.0            # Добавка к TP1
    tp2_mult_add_atr_pct: float = 0.0       # Добавка к TP2
    
    # Trail adjustments
    trail_add_atr: float = 0.0              # Добавка к шагу трейла


class TriggerManager:
    """Управляет триггерами и их поправками"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию триггеров из YAML"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        self.enabled_types = [
            TriggerType(t) for t in self.config['triggers']['types_enabled']
        ]
        self.adjustments_config = self.config['triggers']['adjustments']
    
    def get_adjustments(self, trigger_type: TriggerType) -> TriggerAdjustments:
        """Возвращает поправки для триггера"""
        adj_data = self.adjustments_config.get(trigger_type.value, {})
        
        return TriggerAdjustments(
            trigger_type=trigger_type,
            sl_anchor=adj_data.get('sl_anchor'),
            sl_buffer_add_atr=adj_data.get('sl_buffer_add_atr', 0.0),
            sl_min_add_atr_pct=adj_data.get('sl_min_add_atr_pct', 0.0),
            tp1_add_atr_pct=adj_data.get('tp1_add_atr_pct', 0.0),
            tp2_mult_add_atr_pct=adj_data.get('tp2_mult_add_atr_pct', 0.0),
            trail_add_atr=adj_data.get('trail_add_atr', 0.0)
        )
    
    def detect_trigger_type(
        self,
        impulse_data: Dict[str, Any],
        levels_data: Dict[str, Any],
        flow_data: Dict[str, Any],
        price_now: float
    ) -> TriggerType:
        """
        Определяет тип триггера на основе рыночных условий
        
        Args:
            impulse_data: Данные импульса (PathScore, mono, etc.)
            levels_data: Данные уровней (nearest, type, etc.)
            flow_data: Данные OI Flow (sign, magnitude, etc.)
            price_now: Текущая цена
        
        Returns:
            Определённый тип триггера
        
        Логика детекции:
        - EPS: PathScore↑ (>0.85) & OI_Flow ≤ 0 → покупатели истощены
        - LQS: PathScore↓ (<-0.85) & OI_Flow > 0 → продавцы в ликвидации
        - FVR: цена в зоне FVG (±0.2×ATR)
        - BR: цена отскочила от пробитого уровня (retest)
        - LSR: цена у непробитого уровня
        - TC: дефолт (продолжение тренда)
        """
        pathscore = impulse_data.get('pathscore', 0)
        oi_flow = flow_data.get('oi_flow', 0)
        
        nearest_level = levels_data.get('nearest_counter_level')
        level_type = levels_data.get('level_type', '')
        
        # EPS: PathScore↑ & OI_Flow ≤ 0
        if pathscore > 0.85 and oi_flow <= 0:
            return TriggerType.EPS
        
        # LQS: PathScore↓ & OI_Flow > 0
        if pathscore < -0.85 and oi_flow > 0:
            return TriggerType.LQS
        
        # FVR: уровень типа "FVG"
        if level_type == "FVG":
            return TriggerType.FVR
        
        # BR/LSR: проверка retest vs первое касание
        # (упрощённо - нужна история касаний уровня)
        if nearest_level:
            # TODO: добавить проверку истории касаний
            # Пока всё как LSR
            return TriggerType.LSR
        
        # Дефолт: TC
        return TriggerType.TC


def _get_trigger_manager() -> TriggerManager:
    """Singleton для TriggerManager"""
    if not hasattr(_get_trigger_manager, '_instance'):
        _get_trigger_manager._instance = TriggerManager()
    return _get_trigger_manager._instance

