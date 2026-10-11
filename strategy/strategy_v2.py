#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STRATEGY_V2.PY — Модульный пайплайн стратегии

Дирижёр всех гейтов в строгом порядке:
1. ATR-ворота
2. Импульс (PathScore, anti-chop, монотоничность)
3. Пространство (до встречного уровня)
4. OI Flow
5. Funding/Basis
6. BTC ветер
7. Taker/Safety
8. Триггер-детекция
9. Геометрия (SL/TP/BE/Trail)
10. Риск/лимиты

Возвращает SignalLog со всеми метриками или причиной BAN
"""

from typing import Dict, Any, Optional
from datetime import datetime
import yaml

from strategy.logs import SignalLog, create_signal_log, SignalStatus, BanReason
from strategy.modes import ModeManager, _get_mode_manager
from strategy.triggers import TriggerManager, _get_trigger_manager, TriggerType


class StrategyPipeline:
    """Главный пайплайн стратегии"""
    
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        """Инициализация с загрузкой конфигурации"""
        with open(config_path, 'r', encoding='utf-8') as f:
            full_config = yaml.safe_load(f)
            self.config = full_config
            self.modes_config = full_config['modes']
        
        self.mode_manager = _get_mode_manager()
        self.trigger_manager = _get_trigger_manager()
    
    def evaluate_signal(
        self,
        symbol: str,
        timeframe: str,
        features: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Главная функция оценки сигнала
        
        Args:
            symbol: Символ (напр., "BTCUSDT")
            timeframe: Таймфрейм (напр., "15m")
            features: Словарь с данными рынка:
                - price_now: текущая цена
                - atr: ATR значение
                - atr_pct: ATR в процентах
                - ohlc: DataFrame с OHLC данными
                - oi_data: данные Open Interest
                - funding: funding rate
                - basis: basis (спот - фьючерс)
                - taker_buy_share: доля покупателей
                - btc_data: данные BTC для ветрового анализа
                - levels: предрасчитанные уровни (опционально)
        
        Returns:
            {
                "signal": {side, entry, sl, tp1, tp2, ...} или None,
                "log": SignalLog,
                "reason": str
            }
        """
        # Определяем режим по TF
        mode_name = self.mode_manager.get_mode_for_tf(timeframe)
        mode_config = self.mode_manager.get_mode_config(mode_name)
        
        # Пробуем LONG и SHORT
        for side in ["LONG", "SHORT"]:
            log = self._run_pipeline(
                symbol=symbol,
                timeframe=timeframe,
                side=side,
                mode_name=mode_name,
                mode_config=mode_config,
                features=features
            )
            
            if log.status == SignalStatus.PASSED:
                # Успешно прошли все гейты!
                signal = self._build_signal_dict(log)
                return {
                    "signal": signal,
                    "log": log,
                    "reason": f"{log.trigger_type} signal"
                }
        
        # Ни одна сторона не прошла
        return {
            "signal": None,
            "log": None,
            "reason": "No valid signals for either side"
        }
    
    def _run_pipeline(
        self,
        symbol: str,
        timeframe: str,
        side: str,
        mode_name: str,
        mode_config: Any,
        features: Dict[str, Any]
    ) -> SignalLog:
        """
        Запускает полный пайплайн гейтов для одной стороны
        
        Returns:
            SignalLog с результатом (PASSED или BANNED)
        """
        log = create_signal_log(symbol, timeframe, side)
        log.mode = mode_name
        
        # Извлекаем данные из features
        price_now = features.get("price_now", 0)
        atr = features.get("atr", 0)
        atr_pct = features.get("atr_pct", 0)
        
        # ========== 1. ATR GATE ==========
        if not self._check_atr_gate(log, atr, atr_pct, mode_name):
            return log
        
        # ========== 2. IMPULSE GATE ==========
        impulse_data = self._calculate_impulse(
            ohlc=features.get("ohlc"),
            timeframe=timeframe,
            side=side,
            mode_name=mode_name,
            atr=atr
        )
        if not self._check_impulse_gate(log, impulse_data, mode_name):
            return log
        
        # ========== 3. SPACE/LEVELS GATE ==========
        levels_data = self._get_or_calculate_levels(
            precomputed_levels=features.get("levels"),
            ohlc=features.get("ohlc"),
            timeframe=timeframe,
            atr=atr,
            price_now=price_now
        )
        if not self._check_space_gate(log, price_now, levels_data, side, atr, mode_name):
            return log
        
        # ========== 4. OI FLOW GATE ==========
        flow_data = self._calculate_oi_flow(
            oi_data=features.get("oi_data"),
            timeframe=timeframe,
            mode_name=mode_name,
            pathscore=impulse_data.get("pathscore", 0)
        )
        if not self._check_oi_flow_gate(log, flow_data, side, impulse_data, mode_name):
            return log
        
        # ========== 5. FUNDING/BASIS GATE ==========
        if not self._check_funding_gate(log, features.get("funding", 0), features.get("funding_24h_sign", 0), side):
            return log
        
        if not self._check_basis_gate(log, features.get("basis", 0), side):
            return log
        
        # ========== 6. BTC WIND GATE ==========
        btc_wind = self._calculate_btc_wind(
            btc_data=features.get("btc_data"),
            mode_name=mode_name,
            atr_btc=features.get("atr_btc", 0)
        )
        if not self._check_btc_wind_gate(log, btc_wind, side):
            return log
        
        # ========== 7. TAKER/SAFETY GATES ==========
        if not self._check_taker_gate(log, features.get("taker_buy_share", 0.5), side):
            return log
        
        if not self._check_safety_gates(log):
            return log
        
        # ========== 8. TRIGGER DETECTION ==========
        trigger_type = self.trigger_manager.detect_trigger_type(
            impulse_data=impulse_data,
            levels_data=levels_data,
            flow_data=flow_data,
            price_now=price_now
        )
        log.trigger_type = trigger_type.value
        
        # ========== 9. GEOMETRY ==========
        self._calculate_geometry(
            log=log,
            entry_price=price_now,
            mode_config=mode_config,
            trigger_type=trigger_type,
            levels_data=levels_data,
            atr=atr,
            atr_pct=atr_pct
        )
        
        # ========== 9.5. GEOMETRY VALIDATION ==========
        if not self._validate_geometry(log, atr, mode_config):
            return log
        
        # ========== 10. RISK CHECK ==========
        if not self._check_risk_limits(log, mode_name, timeframe, symbol):
            return log
        
        # ========== ВСЕ ГЕЙТЫ ПРОЙДЕНЫ! ==========
        # Вычисляем Validity и Hold windows
        self._calculate_validity_hold(log, mode_config, timeframe)
        
        # Строим gates dict, levels_summary, why для карточки
        log.build_gates_dict()
        log.build_levels_summary()
        log.build_why()
        
        # Заполняем legacy metrics и life для совместимости/отладки
        self._populate_legacy_metrics(log, features, mode_config, timeframe)
        
        log.mark_passed()
        return log
    
    # ========== GATE METHODS (заглушки, нужно реализовать) ==========
    
    def _check_atr_gate(self, log: SignalLog, atr: float, atr_pct: float, mode_name: str) -> bool:
        """Проверка ATR в квантилях режима"""
        log.atr = atr
        log.atr_pct = atr_pct
        
        # Получаем квантили из конфига
        quantiles = self.config['atr_pct_quantiles'][mode_name]
        log.atr_quantile_range = tuple(quantiles)
        
        # TODO (опционально): для точных квантилей нужна история per-TF из БД
        # Сейчас используем статические пределы из YAML как fallback
        q_low, q_high = quantiles
        
        # Проверяем диапазон
        if atr_pct < q_low:
            log.add_ban_reason(BanReason.ATR_OUT_OF_RANGE, f"atr_pct={atr_pct:.4f} < Q{int(q_low*100)}={q_low:.4f}")
            return False
        
        if atr_pct > q_high:
            log.add_ban_reason(BanReason.ATR_OUT_OF_RANGE, f"atr_pct={atr_pct:.4f} > Q{int(q_high*100)}={q_high:.4f}")
            return False
        
        log.atr_pass = True
        return True
    
    def _calculate_impulse(self, ohlc, timeframe: str, side: str, mode_name: str, atr: float = 0) -> Dict[str, Any]:
        """Вычисляет импульсные метрики"""
        from strategy.impulse import _get_impulse_analyzer
        
        if ohlc is None or len(ohlc) < 10 or atr == 0:
            # Недостаточно данных - возвращаем нулевые метрики
            return {
                "pathscore": 0.0,
                "anti_chop": 999.0,  # Высокий chop = BAN
                "body_share": 0.0,
                "body_ratio": 0.0,
                "n_bars": 0
            }
        
        analyzer = _get_impulse_analyzer()
        metrics = analyzer.calculate_impulse(ohlc, timeframe, atr)
        
        return {
            "pathscore": metrics.pathscore,
            "anti_chop": metrics.anti_chop_atr,
            "body_share": metrics.body_share,
            "body_ratio": metrics.body_ratio,
            "n_bars": metrics.n_bars
        }
    
    def _check_impulse_gate(self, log: SignalLog, impulse_data: Dict, mode_name: str) -> bool:
        """Проверка импульса"""
        log.pathscore = impulse_data.get("pathscore", 0)
        log.anti_chop_atr = impulse_data.get("anti_chop", 0)
        log.n_bars = impulse_data.get("n_bars", 0)
        log.monotonicity_body_share = impulse_data.get("body_share", 0)
        log.monotonicity_body_ratio = impulse_data.get("body_ratio", 0)
        
        # Пороги из конфига
        log.pathscore_threshold = self.config['impulse']['pathscore_min'][mode_name]
        log.anti_chop_threshold = self.config['impulse']['anti_chop_atr'][mode_name]
        log.monotonicity_threshold_share = self.config['impulse']['monotonicity']['body_share_min'][mode_name]
        log.monotonicity_threshold_ratio = self.config['impulse']['monotonicity']['body_ratio_min'][mode_name]
        
        # Проверка PathScore
        if abs(log.pathscore) < log.pathscore_threshold:
            log.add_ban_reason(BanReason.IMPULSE_LOW_PATHSCORE, f"|PathScore|={abs(log.pathscore):.3f} < {log.pathscore_threshold}")
            return False
        
        # Проверка Anti-chop
        if log.anti_chop_atr > log.anti_chop_threshold:
            log.add_ban_reason(BanReason.IMPULSE_HIGH_CHOP, f"AntiChop={log.anti_chop_atr:.3f}×ATR > {log.anti_chop_threshold}")
            return False
        
        # Проверка монотоничности
        if log.monotonicity_body_share < log.monotonicity_threshold_share:
            log.add_ban_reason(BanReason.IMPULSE_LOW_MONOTONICITY, f"BodyShare={log.monotonicity_body_share:.2%} < {log.monotonicity_threshold_share:.2%}")
            return False
        
        if log.monotonicity_body_ratio < log.monotonicity_threshold_ratio:
            log.add_ban_reason(BanReason.IMPULSE_LOW_MONOTONICITY, f"BodyRatio={log.monotonicity_body_ratio:.2f} < {log.monotonicity_threshold_ratio}")
            return False
        
        log.impulse_pass = True
        return True
    
    def _get_or_calculate_levels(self, precomputed_levels, ohlc, timeframe: str, atr: float, price_now: float = 0, current_time=None) -> Dict[str, Any]:
        """Получает или вычисляет уровни"""
        from strategy.levels import _get_level_detector
        import pandas as pd
        
        if ohlc is None or len(ohlc) < 20 or atr == 0:
            return {
                "levels": [],
                "nearest_counter_level": None,
                "level_type": "",
                "level_timeframe": "",
                "distance_atr": 999.0  # Огромное расстояние = BAN
            }
        
        detector = _get_level_detector()
        
        # Если уровни не предрассчитаны - вычисляем
        if precomputed_levels is None:
            all_levels = detector.detect_all_levels(
                ohlc=ohlc,
                timeframe=timeframe,
                atr=atr,
                include_htf=False  # Пока HTF выключаем (нет загрузки данных)
            )
            
            # Кластеризуем
            clustered = detector.cluster_levels(all_levels, atr)
            
            # Ранжируем
            if current_time is None:
                current_time = pd.Timestamp.utcnow()
            
            ranked = detector.rank_levels(
                levels=clustered,
                current_price=price_now if price_now > 0 else ohlc['close'].iloc[0],
                current_time=current_time,
                top_k=12
            )
        else:
            ranked = precomputed_levels
        
        return {
            "levels": ranked,
            "nearest_counter_level": None,  # Будет найден в _check_space_gate
            "level_type": "",
            "level_timeframe": "",
            "distance_atr": 0.0
        }
    
    def _check_space_gate(self, log: SignalLog, price_now: float, levels_data: Dict, side: str, atr: float, mode_name: str) -> bool:
        """Проверка пространства до встречного уровня"""
        from strategy.levels import _get_level_detector
        
        levels = levels_data.get("levels", [])
        log.distance_threshold_atr = self.config['impulse']['distance_to_level_atr'][mode_name]
        
        # Ищем встречный уровень
        if levels:
            detector = _get_level_detector()
            counter_level = detector.find_counter_level(
                levels=levels,
                entry_price=price_now,
                side=side
            )
            
            if counter_level:
                log.nearest_counter_level = counter_level.price
                log.level_type = counter_level.level_type.value
                log.level_timeframe = counter_level.timeframe
                
                # Расстояние в ATR
                distance = abs(counter_level.price - price_now)
                log.distance_to_level_atr = distance / atr if atr > 0 else 999.0
                
                # Сохраняем топ-уровни для UI
                log.top_levels = [
                    {
                        "price": l.price,
                        "type": l.level_type.value,
                        "tf": l.timeframe,
                        "score": l.score
                    }
                    for l in levels[:5]  # Топ-5
                ]
            else:
                # Нет встречного уровня
                log.distance_to_level_atr = 999.0
        else:
            # Нет уровней вообще
            log.distance_to_level_atr = 999.0
        
        # Проверка минимального расстояния
        if log.distance_to_level_atr < log.distance_threshold_atr:
            log.add_ban_reason(BanReason.SPACE_TOO_TIGHT, f"Distance={log.distance_to_level_atr:.2f}×ATR < {log.distance_threshold_atr}")
            return False
        
        log.space_pass = True
        return True
    
    def _calculate_oi_flow(self, oi_data, timeframe: str, mode_name: str, pathscore: float = 0) -> Dict[str, Any]:
        """Вычисляет OI Flow"""
        from strategy.flow import _get_oi_flow_analyzer
        
        if oi_data is None or len(oi_data) < 30:
            # Недостаточно данных
            return {
                "oi_flow": 0.0,
                "percentile": 50.0,
                "mag_gate": 0.0,
                "doi_sigma": 0.0,
                "is_short_case": False,
                "short_case_type": None
            }
        
        analyzer = _get_oi_flow_analyzer()
        
        # Определяем размер окна N (зависит от TF)
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        n_bars = max(10, min(30, 300 // tf_minutes.get(timeframe, 60)))  # 300 мин / TFmin
        
        metrics = analyzer.calculate_oi_flow(
            oi_data=oi_data,
            n_bars=n_bars,
            pathscore=pathscore
        )
        
        return {
            "oi_flow": metrics.oi_flow,
            "percentile": metrics.percentile,
            "mag_gate": metrics.mag_gate,
            "doi_sigma": metrics.doi_sigma,
            "is_short_case": metrics.is_short_case,
            "short_case_type": metrics.short_case_type
        }
    
    def _check_oi_flow_gate(self, log: SignalLog, flow_data: Dict, side: str, impulse_data: Dict, mode_name: str) -> bool:
        """Проверка OI Flow"""
        from strategy.flow import _get_oi_flow_analyzer
        
        log.oi_flow = flow_data.get("oi_flow", 0)
        log.oi_flow_percentile = flow_data.get("percentile", 0)
        log.oi_flow_mag_gate = flow_data.get("mag_gate", 0)
        log.doi_sigma = flow_data.get("doi_sigma", 0)
        log.oi_flow_short_case = flow_data.get("is_short_case", False)
        
        # Пороги
        log.oi_flow_threshold_percentile = self.config['oi']['flow_percentile'][mode_name]
        log.oi_flow_threshold_mag = self.config['oi']['mag_gate'][mode_name]
        
        analyzer = _get_oi_flow_analyzer()
        
        # Проверка аномалий (приоритет)
        from strategy.flow import OIFlowMetrics
        metrics = OIFlowMetrics(
            oi_flow=log.oi_flow,
            percentile=log.oi_flow_percentile,
            mag_gate=log.oi_flow_mag_gate,
            doi_sigma=log.doi_sigma,
            is_short_case=log.oi_flow_short_case,
            short_case_type=flow_data.get("short_case_type"),
            n_bars=20,  # Placeholder
            history_bars=100,
            median_abs_doi=0,
            mean_doi=0,
            std_doi=0
        )
        
        if analyzer.check_anomaly(metrics):
            log.add_ban_reason(BanReason.OI_ANOMALY, f"|ΔOI| > 5σ (z={log.doi_sigma:.1f})")
            return False
        
        # Проверка flow gate
        pass_gate, reason = analyzer.check_flow_gate(metrics, mode_name, side)
        
        if not pass_gate:
            log.add_ban_reason(BanReason.OI_FLOW_WEAK, reason)
            return False
        
        log.oi_flow_pass = True
        return True
    
    def _check_funding_gate(self, log: SignalLog, funding_now: float, funding_24h_sign: int, side: str) -> bool:
        """Проверка Funding"""
        log.funding_now = funding_now
        log.funding_24h_sign = funding_24h_sign
        log.funding_now_threshold = self.config['funding']['now_abs_max']
        
        if abs(funding_now) > log.funding_now_threshold:
            log.add_ban_reason(BanReason.FUNDING_EXTREME, f"Funding={funding_now:.4%}")
            return False
        
        log.funding_pass = True
        return True
    
    def _check_basis_gate(self, log: SignalLog, basis_now: float, side: str) -> bool:
        """Проверка Basis"""
        log.basis_now = basis_now
        
        if side == "LONG" and basis_now < self.config['basis']['long_floor']:
            log.add_ban_reason(BanReason.BASIS_OUT_OF_RANGE, f"Basis={basis_now:.4%} < {self.config['basis']['long_floor']:.4%}")
            return False
        
        if side == "SHORT" and basis_now > self.config['basis']['short_ceiling']:
            log.add_ban_reason(BanReason.BASIS_OUT_OF_RANGE, f"Basis={basis_now:.4%} > {self.config['basis']['short_ceiling']:.4%}")
            return False
        
        log.basis_pass = True
        return True
    
    def _calculate_btc_wind(self, btc_data, mode_name: str, atr_btc: float) -> Dict[str, Any]:
        """Вычисляет BTC ветер"""
        from strategy.regimes import _get_btc_wind_analyzer
        
        if btc_data is None or not btc_data:
            # Нет данных BTC - пропускаем (нейтральный ветер)
            return {
                "direction": 0,
                "lock_minutes": self.modes_config[mode_name]['btc_lock_min'],
                "votes": [],
                "consensus": 0.0
            }
        
        analyzer = _get_btc_wind_analyzer()
        metrics = analyzer.analyze_btc_wind(
            btc_data=btc_data,
            mode=mode_name,
            current_btc_atr_pct=atr_btc
        )
        
        return {
            "direction": metrics.direction,
            "lock_minutes": metrics.lock_minutes,
            "votes": [
                {"tf": v.timeframe, "direction": v.direction, "strength": v.strength}
                for v in metrics.votes
            ],
            "consensus": metrics.consensus
        }
    
    def _check_btc_wind_gate(self, log: SignalLog, btc_wind: Dict, side: str) -> bool:
        """Проверка BTC ветра"""
        from strategy.regimes import _get_btc_wind_analyzer, BTCWindMetrics
        
        log.btc_wind_direction = btc_wind.get("direction", 0)
        log.btc_wind_lock_minutes = btc_wind.get("lock_minutes", 0)
        log.btc_wind_votes = btc_wind.get("votes", [])
        
        # Создаём metrics для проверки
        metrics = BTCWindMetrics(
            direction=log.btc_wind_direction,
            votes=[],  # Не используем в check
            consensus=btc_wind.get("consensus", 0.0),
            lock_minutes=log.btc_wind_lock_minutes,
            base_lock_minutes=log.btc_wind_lock_minutes,
            atr_mult=1.0,
            median_atr_pct=0.01
        )
        
        analyzer = _get_btc_wind_analyzer()
        pass_gate, reason = analyzer.check_btc_wind_gate(metrics, side)
        
        if not pass_gate:
            log.add_ban_reason(BanReason.BTC_WIND_OPPOSITE, reason)
            return False
        
        log.btc_wind_pass = True
        return True
    
    def _check_taker_gate(self, log: SignalLog, taker_buy_share: float, side: str) -> bool:
        """Проверка Taker экстремумов"""
        log.taker_buy_share = taker_buy_share
        extremes = self.config['safety']['taker_extremes']
        log.taker_extreme_threshold = (extremes['long_gt'], extremes['short_lt'])
        
        if side == "LONG" and taker_buy_share > extremes['long_gt']:
            log.add_ban_reason(BanReason.TAKER_EXTREME, f"Taker={taker_buy_share:.2%} > {extremes['long_gt']:.2%}")
            return False
        
        if side == "SHORT" and taker_buy_share < extremes['short_lt']:
            log.add_ban_reason(BanReason.TAKER_EXTREME, f"Taker={taker_buy_share:.2%} < {extremes['short_lt']:.2%}")
            return False
        
        log.taker_pass = True
        return True
    
    def _check_safety_gates(self, log: SignalLog) -> bool:
        """Проверка safety фильтров (сессии, новости)"""
        from datetime import datetime
        
        # Проверяем session_ban_utc
        session_bans = self.config.get('safety', {}).get('session_ban_utc', [])
        if session_bans:
            now_utc = datetime.utcnow()
            current_time = now_utc.hour * 60 + now_utc.minute  # Минуты с начала дня
            
            for ban_window in session_bans:
                if '-' in ban_window:
                    start_str, end_str = ban_window.split('-')
                    start_h, start_m = map(int, start_str.split(':'))
                    end_h, end_m = map(int, end_str.split(':'))
                    start_min = start_h * 60 + start_m
                    end_min = end_h * 60 + end_m
                    
                    # Проверяем попадание в окно
                    if start_min <= current_time < end_min:
                        log.add_ban_reason(BanReason.SESSION_BANNED, f"Session ban: {ban_window} UTC")
                        return False
        
        # Проверяем news_blackout_min
        # TODO (опционально): если есть календарь новостей, проверять proximity
        # Сейчас пропускаем (нет источника новостей)
        log.safety_pass = True
        return True
    
    def _calculate_geometry(self, log: SignalLog, entry_price: float, mode_config: Any, 
                           trigger_type: TriggerType, levels_data: Dict, atr: float, atr_pct: float):
        """Вычисляет геометрию сделки (SL/TP/BE/Trail)"""
        from strategy.levels import _get_level_detector
        
        log.entry_price = entry_price
        levels = levels_data.get("levels", [])
        detector = _get_level_detector()
        
        # Получаем поправки триггера
        adjustments = self.trigger_manager.get_adjustments(trigger_type)
        log.trigger_adjustments = {
            "sl_anchor": adjustments.sl_anchor,
            "sl_buffer_add": adjustments.sl_buffer_add_atr,
            "tp1_add": adjustments.tp1_add_atr_pct,
            "tp2_mult_add": adjustments.tp2_mult_add_atr_pct
        }
        
        # ========== STRUCTURAL SL ==========
        # Ищем ближайший уровень для SL (противоположное направление)
        sl_side = "SHORT" if log.side == "LONG" else "LONG"
        structural_level = detector.find_counter_level(levels, entry_price, sl_side)
        
        if structural_level:
            # Заполняем anchor поля для карточки
            log.sl_anchor_type = adjustments.sl_anchor if adjustments.sl_anchor else structural_level.level_type.value
            log.sl_anchor_tf = structural_level.timeframe
            log.sl_anchor_price = structural_level.price
            
            # Применяем буфер + поправки триггера
            total_buffer = mode_config.sl_buffer_atr + adjustments.sl_buffer_add_atr
            log.sl_buffer_atr = total_buffer
            
            sl_candidate = self.mode_manager.calculate_sl_from_structure(
                entry_price=entry_price,
                structural_level=structural_level.price,
                side=log.side,
                atr=atr,
                atr_pct=atr_pct,
                mode=mode_config
            )
        else:
            # Нет структурного уровня - используем минимум от ATR
            log.sl_anchor_type = "atr_min"
            log.sl_anchor_tf = log.timeframe
            log.sl_anchor_price = None
            sl_mult = mode_config.sl_min_mult_atr_pct[0]
            sl_candidate = entry_price * (1 - sl_mult * atr_pct) if log.side == "LONG" else entry_price * (1 + sl_mult * atr_pct)
        
        log.sl_final = sl_candidate
        log.sl_vola_floor_atr_pct = mode_config.sl_min_mult_atr_pct[0] * atr_pct
        
        # ========== TP1 ==========
        # Ищем встречный уровень для TP1
        counter_level = detector.find_counter_level(levels, entry_price, log.side)
        
        if counter_level:
            # Заполняем anchor поля
            log.tp1_anchor_type = counter_level.level_type.value
            log.tp1_anchor_tf = counter_level.timeframe
            log.tp1_anchor_price = counter_level.price
            
            tp1_offset = mode_config.tp1_offset_atr[0] + adjustments.tp1_add_atr_pct * atr_pct * entry_price
            log.tp1_offset_atr = tp1_offset / atr if atr > 0 else 0
            
            log.tp1_final = self.mode_manager.calculate_tp1_from_level(
                counter_level=counter_level.price,
                side=log.side,
                atr=atr,
                mode=mode_config
            )
        else:
            # Нет уровня - используем RR-бэкап
            log.tp1_anchor_type = "rr_backup"
            log.tp1_anchor_tf = log.timeframe
            log.tp1_anchor_price = None
            tp1_mult = mode_config.tp1_offset_atr[0]
            log.tp1_offset_atr = tp1_mult
            log.tp1_final = entry_price * (1 + tp1_mult * atr_pct) if log.side == "LONG" else entry_price * (1 - tp1_mult * atr_pct)
        
        # ========== TP2 ==========
        # Ищем следующий уровень или используем RR-бэкап
        # (упрощённо - используем RR)
        log.tp2_anchor_type = "rr_backup"
        log.tp2_anchor_tf = log.timeframe
        log.tp2_anchor_price = None
        
        tp2_mult = mode_config.tp2_mult_atr_pct[0] + adjustments.tp2_mult_add_atr_pct
        log.tp2_rr_backup_mult_atr_pct = tp2_mult
        log.tp2_offset_atr = 0.0  # Для RR-бэкапа offset не применяется
        
        log.tp2_final = self.mode_manager.calculate_tp2_backup(
            entry_price=entry_price,
            side=log.side,
            atr_pct=atr_pct,
            mode=mode_config
        )
        
        # ========== RR ==========
        if log.sl_final != entry_price:
            log.rr_to_sl = abs((log.tp1_final - entry_price) / (entry_price - log.sl_final))  # RR к SL (через TP1)
            log.rr_to_tp2 = abs((log.tp2_final - entry_price) / (entry_price - log.sl_final))  # RR к TP2
        else:
            log.rr_to_sl = 0.0
            log.rr_to_tp2 = 0.0
        
        # ========== BE/Trail (из конфига) ==========
        log.be_offset_atr = self.config.get('position', {}).get('be_offset_atr', 0.2)
        log.trail_atr = mode_config.sl_buffer_atr + adjustments.trail_add_atr
        
        # ========== LEGACY METRICS: SL/TP MOVES ==========
        # Вычисляем pct и atr_mult для совместимости с legacy UI
        side_mult = 1.0 if log.side == "LONG" else -1.0
        
        # SL move
        sl_pct = ((log.sl_final - entry_price) / entry_price) * side_mult * 100.0
        sl_atr_mult = abs(log.sl_final - entry_price) / atr if atr > 0 else 0.0
        
        # TP1 move
        tp1_pct = ((log.tp1_final - entry_price) / entry_price) * side_mult * 100.0
        tp1_atr_mult = abs(log.tp1_final - entry_price) / atr if atr > 0 else 0.0
        
        # TP2 move
        tp2_pct = ((log.tp2_final - entry_price) / entry_price) * side_mult * 100.0
        tp2_atr_mult = abs(log.tp2_final - entry_price) / atr if atr > 0 else 0.0
        
        # Сохраняем в log (будет использовано при заполнении legacy_metrics)
        log._sl_move = {"pct": round(sl_pct, 2), "atr_mult": round(sl_atr_mult, 2)}
        log._tp1_move = {"pct": round(tp1_pct, 2), "atr_mult": round(tp1_atr_mult, 2)}
        log._tp2_move = {"pct": round(tp2_pct, 2), "atr_mult": round(tp2_atr_mult, 2)}
    
    def _validate_geometry(self, log: SignalLog, atr: float, mode_config: Any) -> bool:
        """Валидация геометрии сделки"""
        
        # Проверка 1: TP1 не должен быть слишком близко к Entry
        tp1_distance = abs(log.tp1_final - log.entry_price)
        min_tp1_distance = atr * 0.15  # Минимум 0.15×ATR
        
        if tp1_distance < min_tp1_distance:
            log.add_ban_reason(
                BanReason.TP1_TOO_CLOSE,
                f"TP1 distance {tp1_distance:.4f} < 0.15×ATR ({min_tp1_distance:.4f})"
            )
            return False
        
        # Проверка 2: RR должен быть выше порога режима
        # Получаем минимальный RR из mode_config (если есть)
        min_rr = getattr(mode_config, 'min_rr_to_sl', 1.0)  # По умолчанию 1.0
        
        if log.rr_to_sl < min_rr:
            log.add_ban_reason(
                BanReason.RR_TOO_LOW,
                f"RR {log.rr_to_sl:.2f} < min {min_rr:.2f}"
            )
            return False
        
        return True
    
    def _check_risk_limits(self, log: SignalLog, mode_name: str, timeframe: str, symbol: str) -> bool:
        """Проверка риск-лимитов"""
        from strategy.risk import _get_risk_manager
        
        manager = _get_risk_manager()
        
        # Вычисляем confidence score на основе пройденных гейтов
        # (упрощённо - можно улучшить)
        conf_score = 1.0
        
        # Вычисляем k_pct
        log.k_pct = manager.calculate_k_pct(timeframe, conf_score, mode_name)
        log.conf_score = conf_score
        log.position_size = log.entry_price * log.k_pct if log.entry_price > 0 else 0
        
        # Проверяем cooldown (если есть БД)
        # TODO (интеграция с jobs.py): передавать текущие позиции из БД через features['positions']
        # Сейчас пропускаем (нет интеграции с БД в этом модуле)
        log.risk_pass = True
        
        return True
    
    def _calculate_validity_hold(self, log: SignalLog, mode_config: Any, timeframe: str):
        """Вычисляет Validity и Hold windows"""
        from datetime import timedelta
        
        # Парсим TF в минуты
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        tf_min = tf_minutes.get(timeframe, 60)
        
        # Validity window: validity_mult × TF
        validity_min = tf_min * mode_config.validity_mult[0]
        validity_max = tf_min * mode_config.validity_mult[1]
        log.validity_window_min = (validity_min, validity_max)
        log.valid_until = log.timestamp + timedelta(minutes=validity_max)
        
        # Hold window: hold_mult × TF
        hold_min = tf_min * mode_config.hold_mult[0]
        hold_max = tf_min * mode_config.hold_mult[1]
        log.hold_window_min = (hold_min, hold_max)
        log.hold_until = log.timestamp + timedelta(minutes=hold_max)
    
    def _populate_legacy_metrics(self, log: SignalLog, features: Dict[str, Any], mode_config: Any, timeframe: str):
        """Заполняет legacy_metrics и life для совместимости с legacy UI"""
        
        # ========== LIFE STRING ==========
        # Формат: "{mode} {hold_min}-{hold_max}h/m/d {emoji}"
        mode_emojis = {"scalp": "⌛️", "intraday": "⏳", "swing": "⏰"}
        emoji = mode_emojis.get(log.mode, "⏱")
        
        hold_min, hold_max = log.hold_window_min
        if hold_max >= 1440:  # >= 1 день
            hold_min_d = int(hold_min / 1440)
            hold_max_d = int(hold_max / 1440)
            log.life = f"{log.mode} {hold_min_d}-{hold_max_d}d {emoji}"
        elif hold_max >= 60:  # >= 1 часа
            hold_min_h = int(hold_min / 60)
            hold_max_h = int(hold_max / 60)
            log.life = f"{log.mode} {hold_min_h}-{hold_max_h}h {emoji}"
        else:
            log.life = f"{log.mode} {hold_min}-{hold_max}m {emoji}"
        
        # ========== LEGACY METRICS ==========
        # Собираем метрики из features и log
        log.legacy_metrics = {
            # Импульсные метрики
            "delta_p_pct": round(features.get("delta_p_pct", 0.0), 3) if features.get("delta_p_pct") is not None else None,
            "delta_oi_pct": round(features.get("delta_oi_pct", 0.0), 3) if features.get("delta_oi_pct") is not None else None,
            
            # Funding/Basis
            "funding_now_pct": round(log.funding_now * 100, 3) if log.funding_now else None,
            "basis_pct": round(log.basis_now * 100, 3) if log.basis_now else None,
            
            # LS top / Taker
            "ls_top": round(features.get("ls_top", 0.0), 3) if features.get("ls_top") is not None else None,
            "taker_buy_share": round(log.taker_buy_share, 3) if log.taker_buy_share else None,
            
            # ATR
            "atr_abs": round(log.atr, 6) if log.atr > 0 else None,
            "atr_pct": round(log.atr_pct * 100, 2) if log.atr_pct > 0 else None,
            
            # SL/TP moves (из временных полей)
            "sl_move": log._sl_move if log._sl_move else None,
            "tp1_move": log._tp1_move if log._tp1_move else None,
            "tp2_move": log._tp2_move if log._tp2_move else None,
        }
    
    def _build_signal_dict(self, log: SignalLog) -> Dict[str, Any]:
        """Строит словарь сигнала для вывода"""
        return {
            "symbol": log.symbol,
            "side": log.side,
            "entry": log.entry_price,
            "sl": log.sl_final,
            "tp1": log.tp1_final,
            "tp2": log.tp2_final,
            "mode": log.mode,
            "trigger_type": log.trigger_type,
            "rr_tp1": log.rr_tp1,
            "rr_tp2": log.rr_tp2,
            "k_pct": log.k_pct
        }


# Singleton
_pipeline_instance = None

def get_pipeline() -> StrategyPipeline:
    """Возвращает singleton инстанс пайплайна"""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = StrategyPipeline()
    return _pipeline_instance

