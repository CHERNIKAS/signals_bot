#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FLOW.PY — OI Flow анализ

Расчёты:
- ΔOI (изменение Open Interest)
- OI_Flow = Σ(ΔOI_positive) - Σ(ΔOI_negative) за окно N
- Перцентиль OI_Flow (p_flow) за историю H = history_mult × N
- Magnitude Gate: |OI_Flow| / median(|ΔOI|_N)

Short-кейсы (дивергенции):
- EPS (Exhaustion Push Short): PathScore↑ & OI_Flow ≤ 0
- LQS (Liquidity Squeeze): PathScore↓ & OI_Flow > 0

Аномалии:
- |ΔOI| > doi_sigma × σ(ΔOI) → BAN
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np
import yaml


@dataclass
class OIFlowMetrics:
    """Метрики OI Flow"""
    oi_flow: float                  # OI_Flow за окно N
    percentile: float               # Перцентиль p_flow (0-100)
    mag_gate: float                 # |OI_Flow| / median(|ΔOI|)
    doi_sigma: float                # Z-score последнего ΔOI
    is_short_case: bool             # EPS или LQS
    short_case_type: Optional[str]  # "EPS" или "LQS"
    
    # Детали
    n_bars: int                     # Размер окна
    history_bars: int               # Размер истории для перцентилей
    median_abs_doi: float           # Медиана |ΔOI|
    mean_doi: float                 # Среднее ΔOI
    std_doi: float                  # Стандартное отклонение ΔOI


class OIFlowAnalyzer:
    """Анализатор OI Flow"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Загружает конфигурацию OI"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            self.config = config['oi']
            self.anomaly_config = config['safety']['anomaly']
    
    def calculate_oi_flow(
        self,
        oi_data: pd.DataFrame,
        n_bars: int = 20,
        pathscore: float = 0.0
    ) -> OIFlowMetrics:
        """
        Вычисляет OI Flow метрики
        
        Args:
            oi_data: DataFrame с колонками ['timestamp', 'open_interest']
                     Должен быть отсортирован по timestamp DESC (новые первые)
            n_bars: Размер окна для OI_Flow (по умолчанию 20)
            pathscore: PathScore для детекции short-кейсов
        
        Returns:
            OIFlowMetrics со всеми метриками
        """
        if oi_data is None or len(oi_data) < n_bars + 1:
            # Недостаточно данных
            return self._empty_metrics(n_bars)
        
        # Берём последние n_bars + история
        history_mult = self.config['history_mult']
        history_bars = max(30, history_mult * n_bars)
        
        # Ограничиваем до доступного количества
        total_needed = n_bars + history_bars
        if len(oi_data) < total_needed:
            history_bars = len(oi_data) - n_bars
            if history_bars < n_bars:
                return self._empty_metrics(n_bars)
        
        # Извлекаем OI значения (новые первые)
        oi_values = oi_data['open_interest'].values[:total_needed]
        
        # Вычисляем ΔOI
        doi = np.diff(oi_values)  # doi[i] = oi[i] - oi[i+1] (изменение)
        doi = -doi  # Инвертируем, чтобы рост OI был положительным
        
        # Окно N (последние n_bars изменений)
        doi_window = doi[:n_bars]
        
        # История для перцентилей
        doi_history = doi[:history_bars]
        
        # 1. OI_Flow = Σ(ΔOI_positive) - |Σ(ΔOI_negative)|
        doi_pos = doi_window[doi_window > 0].sum()
        doi_neg = abs(doi_window[doi_window < 0].sum())
        oi_flow = doi_pos - doi_neg
        
        # 2. Перцентиль p_flow
        # Вычисляем OI_Flow для каждого окна в истории (скользящее окно)
        if len(doi_history) >= n_bars:
            flow_history = []
            for i in range(len(doi_history) - n_bars + 1):
                window = doi_history[i:i+n_bars]
                pos = window[window > 0].sum()
                neg = abs(window[window < 0].sum())
                flow_history.append(pos - neg)
            
            flow_history = np.array(flow_history)
            percentile = np.searchsorted(np.sort(flow_history), oi_flow) / len(flow_history) * 100
        else:
            percentile = 50.0  # Дефолт, если не хватает истории
        
        # 3. Magnitude Gate: |OI_Flow| / median(|ΔOI|)
        median_abs_doi = np.median(np.abs(doi_window))
        if median_abs_doi > 0:
            mag_gate = abs(oi_flow) / median_abs_doi
        else:
            mag_gate = 0.0
        
        # 4. Аномалия: Z-score последнего ΔOI
        if len(doi_history) > 1:
            mean_doi = np.mean(doi_history)
            std_doi = np.std(doi_history)
            
            if std_doi > 0:
                doi_sigma = abs((doi[0] - mean_doi) / std_doi)
            else:
                doi_sigma = 0.0
        else:
            mean_doi = 0.0
            std_doi = 0.0
            doi_sigma = 0.0
        
        # 5. Short-кейсы (дивергенции)
        is_short_case = False
        short_case_type = None
        
        # EPS: PathScore↑ (> 0.85) & OI_Flow ≤ 0
        if pathscore > 0.85 and oi_flow <= 0:
            is_short_case = True
            short_case_type = "EPS"
        
        # LQS: PathScore↓ (< -0.85) & OI_Flow > 0
        if pathscore < -0.85 and oi_flow > 0:
            is_short_case = True
            short_case_type = "LQS"
        
        return OIFlowMetrics(
            oi_flow=oi_flow,
            percentile=percentile,
            mag_gate=mag_gate,
            doi_sigma=doi_sigma,
            is_short_case=is_short_case,
            short_case_type=short_case_type,
            n_bars=n_bars,
            history_bars=len(doi_history),
            median_abs_doi=median_abs_doi,
            mean_doi=mean_doi,
            std_doi=std_doi
        )
    
    def check_anomaly(self, metrics: OIFlowMetrics) -> bool:
        """
        Проверяет, является ли текущее ΔOI аномалией
        
        Returns:
            True если аномалия (нужен BAN)
        """
        threshold_sigma = self.anomaly_config['doi_sigma']
        return metrics.doi_sigma > threshold_sigma
    
    def check_flow_gate(
        self,
        metrics: OIFlowMetrics,
        mode: str,
        side: str
    ) -> tuple[bool, str]:
        """
        Проверяет прохождение OI Flow гейта
        
        Args:
            metrics: OIFlowMetrics
            mode: "scalp", "intraday", "swing"
            side: "LONG" or "SHORT"
        
        Returns:
            (pass, reason) где pass=True если прошёл, reason - причина BAN
        """
        # Пороги из конфига
        percentile_threshold = self.config['flow_percentile'][mode]
        mag_gate_threshold = self.config['mag_gate'][mode]
        
        # Для short-кейсов (EPS/LQS) правила мягче
        if metrics.is_short_case and side == "SHORT":
            # EPS/LQS разрешают SHORT даже при слабом/отрицательном flow
            return True, ""
        
        # Обычная проверка
        if side == "LONG":
            # LONG требует положительный flow
            if metrics.oi_flow <= 0:
                return False, f"OI_Flow={metrics.oi_flow:.2f} ≤ 0 for LONG"
            
            if metrics.percentile < percentile_threshold:
                return False, f"p_flow={metrics.percentile:.0f}% < {percentile_threshold}%"
            
            if metrics.mag_gate < mag_gate_threshold:
                return False, f"mag_gate={metrics.mag_gate:.2f} < {mag_gate_threshold}"
        
        else:  # SHORT
            # SHORT требует отрицательный flow (если не short-case)
            if metrics.oi_flow >= 0 and not metrics.is_short_case:
                return False, f"OI_Flow={metrics.oi_flow:.2f} ≥ 0 for SHORT"
            
            # Для SHORT перцентиль инвертирован
            inv_percentile = 100 - metrics.percentile
            if inv_percentile < percentile_threshold:
                return False, f"inv_p_flow={inv_percentile:.0f}% < {percentile_threshold}%"
            
            if metrics.mag_gate < mag_gate_threshold:
                return False, f"mag_gate={metrics.mag_gate:.2f} < {mag_gate_threshold}"
        
        return True, ""
    
    def _empty_metrics(self, n_bars: int) -> OIFlowMetrics:
        """Возвращает пустые метрики (недостаточно данных)"""
        return OIFlowMetrics(
            oi_flow=0.0,
            percentile=50.0,
            mag_gate=0.0,
            doi_sigma=0.0,
            is_short_case=False,
            short_case_type=None,
            n_bars=n_bars,
            history_bars=0,
            median_abs_doi=0.0,
            mean_doi=0.0,
            std_doi=0.0
        )


def _get_oi_flow_analyzer() -> OIFlowAnalyzer:
    """Singleton для OIFlowAnalyzer"""
    if not hasattr(_get_oi_flow_analyzer, '_instance'):
        _get_oi_flow_analyzer._instance = OIFlowAnalyzer()
    return _get_oi_flow_analyzer._instance

