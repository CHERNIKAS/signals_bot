#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MODES.PY — Режимы торговли (Scalp/Intraday/Swing)

Определяет параметры для каждого режима:
- Validity/Hold windows
- SL/TP буферы и множители
- BE/Trail параметры
- BTC lock минимумы
"""

from dataclasses import dataclass
from typing import Tuple, Dict, Any
import yaml


@dataclass
class ModeConfig:
    """Конфигурация режима торговли"""
    name: str
    validity_mult: Tuple[int, int]      # Множители TF для validity window
    hold_mult: Tuple[int, int]          # Множители TF для hold window
    sl_buffer_atr: float                # Буфер SL в ATR
    sl_min_mult_atr_pct: Tuple[float, float]  # Минимальный SL как множитель atr_pct
    tp1_offset_atr: Tuple[float, float]  # Отступ TP1 от уровня
    tp2_mult_atr_pct: Tuple[float, float]  # TP2 как множитель atr_pct
    btc_lock_min: int                   # Минимальный BTC lock (минуты)


class ModeManager:
    """Управляет режимами торговли и их параметрами"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию режимов из YAML"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        self.modes: Dict[str, ModeConfig] = {}
        self.tf_to_mode: Dict[str, str] = self.config['tf_to_mode']
        
        # Загружаем режимы
        for mode_name, mode_data in self.config['modes'].items():
            self.modes[mode_name] = ModeConfig(
                name=mode_name,
                validity_mult=tuple(mode_data['validity_mult']),
                hold_mult=tuple(mode_data['hold_mult']),
                sl_buffer_atr=mode_data['sl_buffer_atr'],
                sl_min_mult_atr_pct=tuple(mode_data['sl_min_mult_atr_pct']),
                tp1_offset_atr=tuple(mode_data['tp1_offset_atr']),
                tp2_mult_atr_pct=tuple(mode_data['tp2_mult_atr_pct']),
                btc_lock_min=mode_data['btc_lock_min']
            )
    
    def get_mode_for_tf(self, timeframe: str) -> str:
        """Возвращает режим для заданного таймфрейма"""
        return self.tf_to_mode.get(timeframe, "intraday")
    
    def get_mode_config(self, mode_name: str) -> ModeConfig:
        """Возвращает конфигурацию режима"""
        return self.modes.get(mode_name, self.modes["intraday"])
    
    def calculate_validity_window(self, timeframe_minutes: int, mode: ModeConfig) -> Tuple[int, int]:
        """
        Вычисляет validity window в минутах
        
        Returns:
            (min_validity_min, max_validity_min)
        """
        return (
            timeframe_minutes * mode.validity_mult[0],
            timeframe_minutes * mode.validity_mult[1]
        )
    
    def calculate_hold_window(self, timeframe_minutes: int, mode: ModeConfig) -> Tuple[int, int]:
        """
        Вычисляет hold window в минутах
        
        Returns:
            (min_hold_min, max_hold_min)
        """
        return (
            timeframe_minutes * mode.hold_mult[0],
            timeframe_minutes * mode.hold_mult[1]
        )
    
    def calculate_sl_from_structure(
        self, 
        entry_price: float,
        structural_level: float,
        side: str,
        atr: float,
        atr_pct: float,
        mode: ModeConfig,
        buffer_atr: float = None,
        min_mult_add: float = 0.0
    ) -> float:
        """
        SL за структурным уровнем с буфером, но НЕ ближе волатильного минимума.

        Берётся более ДАЛЬНИЙ из двух стопов:
            LONG:  min(level - buffer, entry × (1 - k × atr_pct))
            SHORT: max(level + buffer, entry × (1 + k × atr_pct))
        где k = sl_min_mult_atr_pct(mode) + min_mult_add.
        (Раньше тут был max/min наоборот — стоп ставился ближе минимума, внутрь шума.)
        """
        buffer = (mode.sl_buffer_atr if buffer_atr is None else buffer_atr) * atr
        k = mode.sl_min_mult_atr_pct[0] + min_mult_add
        
        if side == "LONG":
            sl_structural = structural_level - buffer
            sl_min = entry_price * (1 - k * atr_pct)
            return min(sl_structural, sl_min)
        else:  # SHORT
            sl_structural = structural_level + buffer
            sl_min = entry_price * (1 + k * atr_pct)
            return max(sl_structural, sl_min)
    
    def calculate_tp1_from_level(
        self,
        counter_level: float,
        side: str,
        atr: float,
        mode: ModeConfig
    ) -> float:
        """
        Вычисляет TP1 от встречного уровня с отступом
        
        TP1 = counter_level ∓ tp1_offset_atr(mode) × ATR
        
        Args:
            counter_level: Встречный уровень
            side: "LONG" или "SHORT"
            atr: ATR значение
            mode: Конфигурация режима
        
        Returns:
            TP1 цена
        """
        offset = mode.tp1_offset_atr[0] * atr
        
        if side == "LONG":
            return counter_level - offset
        else:  # SHORT
            return counter_level + offset
    
    def calculate_tp2_backup(
        self,
        entry_price: float,
        side: str,
        atr_pct: float,
        mode: ModeConfig,
        mult_add: float = 0.0
    ) -> float:
        """
        Вычисляет TP2 как RR-бэкап (если нет уровня)
        
        TP2 = entry ± tp2_mult_atr_pct(mode) × atr_pct
        
        Args:
            entry_price: Цена входа
            side: "LONG" или "SHORT"
            atr_pct: ATR в процентах
            mode: Конфигурация режима
        
        Returns:
            TP2 цена
        """
        mult = mode.tp2_mult_atr_pct[0] + mult_add
        
        if side == "LONG":
            return entry_price * (1 + mult * atr_pct)
        else:  # SHORT
            return entry_price * (1 - mult * atr_pct)


def _get_mode_manager() -> ModeManager:
    """Singleton для ModeManager"""
    if not hasattr(_get_mode_manager, '_instance'):
        _get_mode_manager._instance = ModeManager()
    return _get_mode_manager._instance

