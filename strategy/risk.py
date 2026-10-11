#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RISK.PY — Риск менеджмент и лимиты

Функции:
- k_pct расчёт (адаптивный размер позиции)
- Max concurrent positions (по режимам)
- Cooldowns (между сигналами, после SL)
- Per-symbol caps
- Safety checks
"""

from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime, timedelta
import yaml


@dataclass
class RiskMetrics:
    """Метрики риска"""
    k_pct: float                    # Размер позиции (%)
    position_size: float            # Размер в единицах актива
    conf_score: float               # Confidence score (0.5 - 1.2)
    
    # Лимиты
    max_concurrent: int             # Макс одновременных позиций
    current_positions: int          # Текущие позиции
    per_symbol_cap_reached: bool    # Достигнут ли лимит на символ
    
    # Cooldowns
    cooldown_active: bool           # Активен ли кулдаун
    cooldown_reason: str            # Причина кулдауна
    cooldown_until: Optional[datetime]  # До какого времени


class RiskManager:
    """Менеджер рисков"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию рисков"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)['risk']
        
        self.tf_minutes = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "4h": 240, "1d": 1440
        }
        
        # История позиций (для cooldowns)
        # В реальной системе это должно быть в БД
        self.position_history: Dict[str, list] = {}  # symbol -> [{'side', 'outcome', 'timestamp'}, ...]
    
    def calculate_k_pct(
        self,
        timeframe: str,
        conf_score: float = 1.0,
        mode: str = "intraday"
    ) -> float:
        """
        Вычисляет адаптивный размер позиции
        
        k_pct = k_base × min(1, √(TFmin/60)) × conf_score
        
        Args:
            timeframe: Таймфрейм
            conf_score: Confidence score (0.5 - 1.2)
                - Высокий conf: много гейтов прошли с запасом
                - Низкий conf: гейты прошли впритык
            mode: Режим (для дополнительной настройки)
        
        Returns:
            k_pct (размер позиции в % от капитала)
        """
        k_base = self.config['k_pct_base']
        
        # TF adjustment: √(TFmin/60)
        # - 15m: √(15/60) = 0.5 → k_pct × 0.5
        # - 1h: √(60/60) = 1.0 → k_pct × 1.0
        # - 4h: √(240/60) = 2.0 → k_pct × 2.0 (cap at 1.0)
        tf_min = self.tf_minutes.get(timeframe, 60)
        tf_mult = min(1.0, (tf_min / 60) ** 0.5)
        
        # Confidence adjustment
        conf_clamped = max(0.5, min(1.2, conf_score))
        
        k_pct = k_base * tf_mult * conf_clamped
        
        return k_pct
    
    def check_concurrent_limit(
        self,
        mode: str,
        current_positions: int
    ) -> tuple[bool, str]:
        """
        Проверяет лимит одновременных позиций
        
        Returns:
            (pass, reason)
        """
        max_concurrent = self.config['max_concurrent'][mode]
        
        if current_positions >= max_concurrent:
            return False, f"Max concurrent {max_concurrent} reached (current: {current_positions})"
        
        return True, ""
    
    def check_symbol_cap(
        self,
        symbol: str,
        current_symbol_positions: int,
        k_pct: float
    ) -> tuple[bool, str]:
        """
        Проверяет лимит на символ
        
        Per-symbol cap: k_pct × per_symbol_cap_mult
        
        Returns:
            (pass, reason)
        """
        cap_mult = self.config['per_symbol_cap_mult']
        k_base = self.config['k_pct_base']
        max_k_per_symbol = k_base * cap_mult
        
        total_k = current_symbol_positions * k_pct
        
        if total_k + k_pct > max_k_per_symbol:
            return False, f"Symbol cap {max_k_per_symbol:.2%} reached (current: {total_k:.2%})"
        
        return True, ""
    
    def check_cooldown(
        self,
        symbol: str,
        mode: str,
        current_time: datetime
    ) -> tuple[bool, str, Optional[datetime]]:
        """
        Проверяет кулдауны
        
        Types:
        - between_signals_min: между любыми сигналами
        - after_1sl_min: после 1 SL
        - after_2sl_hours: после 2 SL подряд
        
        Returns:
            (cooldown_active, reason, cooldown_until)
        """
        if symbol not in self.position_history:
            return False, "", None
        
        history = self.position_history[symbol]
        if not history:
            return False, "", None
        
        # Сортируем по времени DESC
        sorted_history = sorted(history, key=lambda x: x['timestamp'], reverse=True)
        last_position = sorted_history[0]
        
        # 1. between_signals_min: базовый кулдаун между сигналами
        cooldown_minutes = self.config['cooldown']['between_signals_min']
        time_since_last = (current_time - last_position['timestamp']).total_seconds() / 60
        
        if time_since_last < cooldown_minutes:
            cooldown_until = last_position['timestamp'] + timedelta(minutes=cooldown_minutes)
            return True, f"Base cooldown (last signal {time_since_last:.0f}m ago)", cooldown_until
        
        # 2. after_1sl_min: кулдаун после 1 SL
        if last_position.get('outcome') == 'SL':
            sl_cooldown_min = self.config['cooldown']['after_1sl_min'][mode]
            
            if time_since_last < sl_cooldown_min:
                cooldown_until = last_position['timestamp'] + timedelta(minutes=sl_cooldown_min)
                return True, f"Cooldown after 1 SL ({time_since_last:.0f}m / {sl_cooldown_min}m)", cooldown_until
        
        # 3. after_2sl_hours: кулдаун после 2 SL подряд
        if len(sorted_history) >= 2:
            if (sorted_history[0].get('outcome') == 'SL' and 
                sorted_history[1].get('outcome') == 'SL'):
                
                sl2_cooldown_hours = self.config['cooldown']['after_2sl_hours'][mode]
                time_since_first_sl = (current_time - sorted_history[1]['timestamp']).total_seconds() / 3600
                
                if time_since_first_sl < sl2_cooldown_hours:
                    cooldown_until = sorted_history[1]['timestamp'] + timedelta(hours=sl2_cooldown_hours)
                    return True, f"Cooldown after 2 SL ({time_since_first_sl:.1f}h / {sl2_cooldown_hours}h)", cooldown_until
        
        return False, "", None
    
    def add_position_to_history(
        self,
        symbol: str,
        side: str,
        outcome: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ):
        """
        Добавляет позицию в историю
        
        Args:
            symbol: Символ
            side: "LONG" или "SHORT"
            outcome: "SL", "TP1", "TP2", None (если ещё открыта)
            timestamp: Время открытия (по умолчанию сейчас)
        """
        if symbol not in self.position_history:
            self.position_history[symbol] = []
        
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        self.position_history[symbol].append({
            'side': side,
            'outcome': outcome,
            'timestamp': timestamp
        })
        
        # Ограничиваем размер истории (последние 50 позиций)
        if len(self.position_history[symbol]) > 50:
            self.position_history[symbol] = self.position_history[symbol][-50:]
    
    def calculate_risk_metrics(
        self,
        symbol: str,
        timeframe: str,
        mode: str,
        conf_score: float,
        entry_price: float,
        current_positions: int = 0,
        current_symbol_positions: int = 0,
        current_time: Optional[datetime] = None
    ) -> RiskMetrics:
        """
        Вычисляет полные метрики риска
        
        Returns:
            RiskMetrics
        """
        if current_time is None:
            current_time = datetime.utcnow()
        
        # k_pct
        k_pct = self.calculate_k_pct(timeframe, conf_score, mode)
        
        # Position size (в единицах актива)
        # Упрощённо: (капитал × k_pct) / entry_price
        # В реальности нужно учитывать leverage, комиссии и т.д.
        position_size = k_pct * entry_price  # Placeholder
        
        # Concurrent limit
        max_concurrent = self.config['max_concurrent'][mode]
        
        # Symbol cap
        cap_mult = self.config['per_symbol_cap_mult']
        k_base = self.config['k_pct_base']
        max_k_per_symbol = k_base * cap_mult
        per_symbol_cap_reached = (current_symbol_positions * k_pct >= max_k_per_symbol)
        
        # Cooldown
        cooldown_active, cooldown_reason, cooldown_until = self.check_cooldown(
            symbol, mode, current_time
        )
        
        return RiskMetrics(
            k_pct=k_pct,
            position_size=position_size,
            conf_score=conf_score,
            max_concurrent=max_concurrent,
            current_positions=current_positions,
            per_symbol_cap_reached=per_symbol_cap_reached,
            cooldown_active=cooldown_active,
            cooldown_reason=cooldown_reason,
            cooldown_until=cooldown_until
        )


def _get_risk_manager() -> RiskManager:
    """Singleton для RiskManager"""
    if not hasattr(_get_risk_manager, '_instance'):
        _get_risk_manager._instance = RiskManager()
    return _get_risk_manager._instance

