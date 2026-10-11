#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LOGS.PY — Единый лог-объект сигнала

Содержит ВСЕ метрики, пороги и причины BAN/PASS на каждом этапе пайплайна.
Используется для:
- Вывода в UI
- Записи в БД
- Анализа и отладки
- A/B тестирования
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum


class SignalStatus(Enum):
    """Статус сигнала"""
    PENDING = "PENDING"       # В процессе обработки
    PASSED = "PASSED"         # Прошёл все гейты
    BANNED = "BANNED"         # Заблокирован одним из гейтов


class BanReason(Enum):
    """Причины блокировки сигнала"""
    ATR_OUT_OF_RANGE = "ATR_OUT_OF_RANGE"
    IMPULSE_LOW_PATHSCORE = "IMPULSE_LOW_PATHSCORE"
    IMPULSE_HIGH_CHOP = "IMPULSE_HIGH_CHOP"
    IMPULSE_LOW_MONOTONICITY = "IMPULSE_LOW_MONOTONICITY"
    SPACE_TOO_TIGHT = "SPACE_TOO_TIGHT"
    OI_FLOW_WEAK = "OI_FLOW_WEAK"
    OI_FLOW_OPPOSITE = "OI_FLOW_OPPOSITE"
    OI_ANOMALY = "OI_ANOMALY"
    FUNDING_EXTREME = "FUNDING_EXTREME"
    FUNDING_AGAINST = "FUNDING_AGAINST"
    BASIS_OUT_OF_RANGE = "BASIS_OUT_OF_RANGE"
    BTC_WIND_OPPOSITE = "BTC_WIND_OPPOSITE"
    BTC_LOCK_ACTIVE = "BTC_LOCK_ACTIVE"
    TAKER_EXTREME = "TAKER_EXTREME"
    SESSION_BANNED = "SESSION_BANNED"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"
    RISK_LIMIT = "RISK_LIMIT"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    TP1_TOO_CLOSE = "TP1_TOO_CLOSE"
    RR_TOO_LOW = "RR_TOO_LOW"


@dataclass
class SignalLog:
    """Полный лог-объект сигнала со всеми метриками"""
    
    # ========== ОСНОВНАЯ ИНФОРМАЦИЯ (META) ==========
    timestamp: datetime = field(default_factory=datetime.utcnow)
    symbol: str = ""
    timeframe: str = ""
    side: str = ""              # "LONG" or "SHORT"
    mode: str = ""              # "scalp", "intraday", "swing"
    trigger_type: str = ""      # "TC", "BR", "FVR", "LSR", "EPS", "LQS"
    
    status: SignalStatus = SignalStatus.PENDING
    ban_reasons: List[BanReason] = field(default_factory=list)
    
    # ========== LIFECYCLE ==========
    valid_until: Optional[datetime] = None      # Validity window end
    hold_until: Optional[datetime] = None       # Hold window end
    
    # ========== ATR GATE ==========
    atr: float = 0.0
    atr_pct: float = 0.0
    atr_quantile_range: tuple = (0.0, 0.0)  # (low, high) перцентили
    atr_pass: bool = False
    
    # ========== IMPULSE GATE ==========
    n_bars: int = 0
    pathscore: float = 0.0
    pathscore_threshold: float = 0.0
    anti_chop_atr: float = 0.0
    anti_chop_threshold: float = 0.0
    monotonicity_body_share: float = 0.0
    monotonicity_body_ratio: float = 0.0
    monotonicity_threshold_share: float = 0.0
    monotonicity_threshold_ratio: float = 0.0
    impulse_pass: bool = False
    
    # ========== SPACE/LEVELS GATE ==========
    nearest_counter_level: Optional[float] = None
    level_type: str = ""        # "swing", "FVG", "liquidity", "HTF"
    level_timeframe: str = ""
    distance_to_level_atr: float = 0.0
    distance_threshold_atr: float = 0.0
    space_pass: bool = False
    
    # Топ уровни (для UI/отладки)
    top_levels: List[Dict[str, Any]] = field(default_factory=list)
    
    # ========== OI FLOW GATE ==========
    oi_flow: float = 0.0
    oi_flow_percentile: float = 0.0
    oi_flow_mag_gate: float = 0.0
    oi_flow_threshold_percentile: float = 0.0
    oi_flow_threshold_mag: float = 0.0
    oi_flow_short_case: bool = False  # EPS или LQS
    doi_sigma: float = 0.0             # Z-score для аномалий
    oi_flow_pass: bool = False
    
    # ========== FUNDING/BASIS GATE ==========
    funding_now: float = 0.0
    funding_24h_sign: int = 0          # +1, -1, 0
    funding_now_threshold: float = 0.0
    funding_pass: bool = False
    
    basis_now: float = 0.0
    basis_threshold: tuple = (0.0, 0.0)  # (long_floor, short_ceiling)
    basis_pass: bool = False
    
    # ========== BTC WIND GATE ==========
    btc_wind_votes: List[Dict[str, Any]] = field(default_factory=list)  # [{tf, direction, atr}, ...]
    btc_wind_direction: int = 0        # +1 (UP), -1 (DOWN), 0 (NEUTRAL)
    btc_wind_lock_minutes: int = 0
    btc_wind_pass: bool = False
    
    # ========== TAKER/SAFETY GATES ==========
    taker_buy_share: float = 0.0
    taker_extreme_threshold: tuple = (0.0, 0.0)  # (long_gt, short_lt)
    taker_pass: bool = False
    
    session_banned: bool = False
    news_blackout: bool = False
    
    # ========== GEOMETRY (ЕСЛИ ПРОШЁЛ) ==========
    entry_price: float = 0.0
    
    # SL (расширенные поля для карточки)
    sl_anchor_type: str = ""           # "swing", "FVG", "liquidity", "retest", "atr_min"
    sl_anchor_tf: str = ""             # Таймфрейм якоря
    sl_anchor_price: Optional[float] = None  # Цена якоря (до буфера)
    sl_buffer_atr: float = 0.0         # Буфер в ATR
    sl_vola_floor_atr_pct: float = 0.0 # Волатильный пол (sl_min_mult_atr_pct)
    sl_final: float = 0.0              # Финальный SL
    
    # TP1 (расширенные поля)
    tp1_anchor_type: str = ""          # "swing", "FVG", "liquidity", "HTF", "rr_backup"
    tp1_anchor_tf: str = ""
    tp1_anchor_price: Optional[float] = None
    tp1_offset_atr: float = 0.0
    tp1_final: float = 0.0
    
    # TP2 (расширенные поля)
    tp2_anchor_type: str = ""          # "swing", "FVG", "liquidity", "HTF", "rr_backup"
    tp2_anchor_tf: str = ""
    tp2_anchor_price: Optional[float] = None
    tp2_offset_atr: float = 0.0
    tp2_rr_backup_mult_atr_pct: float = 0.0  # Если от RR-бэкапа
    tp2_final: float = 0.0
    
    # RR
    rr_to_sl: float = 0.0              # Риск к SL
    rr_to_tp2: float = 0.0             # Риск к TP2
    
    # BE/Trail
    be_offset_atr: float = 0.0
    trail_atr: float = 0.0              # Шаг трейла в ATR
    
    # ========== RISK ==========
    k_pct: float = 0.0
    position_size: float = 0.0
    conf_score: float = 1.0            # Confidence score (0.5 - 1.2)
    
    # ========== VALIDITY/HOLD ==========
    validity_window_min: tuple = (0, 0)
    hold_window_min: tuple = (0, 0)
    
    # ========== TRIGGER ADJUSTMENTS ==========
    trigger_adjustments: Dict[str, Any] = field(default_factory=dict)
    
    # ========== UI/КАРТОЧКА ==========
    levels_summary: str = ""           # "SL ▸ swing(1h) • TP1 ▸ liq(1h) • TP2 ▸ swing(4h)"
    why: str = ""                      # Короткое объяснение (≤120 символов)
    links: Dict[str, str] = field(default_factory=dict)  # {"tv_url": "...", "levels_url": "..."}
    
    # ========== GATES (для светофора в карточке) ==========
    gates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Формат:
    # {
    #   "vol": {"ok": bool, "atr_pct": float, "quantile": "Q...", "reason": "..."},
    #   "impulse": {"ok": bool, "pathscore": float, ...},
    #   "level": {"ok": bool, "dist_to_level_atr": float, ...},
    #   "oi": {"ok": bool, "flow": float, "percentile": int, ...},
    #   "funding": {"ok": bool, "now": float, "ctx24h_sign": "+|-", ...},
    #   "basis": {"ok": bool, "value": float, ...},
    #   "btc": {"ok": bool, "votes": "2/3", "lock_min": int, ...}
    # }
    
    # ========== OUTCOME (статус после закрытия) ==========
    outcome: Dict[str, Any] = field(default_factory=dict)
    # Формат:
    # {
    #   "status": "SL|TP1|TP1→SL|TP1+TP2|OPEN|CANCEL",
    #   "r": float,  # Результат в R (risk units)
    #   "time_to_tp1_min": int,  # Время до TP1 в минутах (если достигнут)
    #   "manage_events": [...]  # История управленческих событий (BE/Trail/Close)
    # }
    
    # ========== LIFE & LEGACY METRICS (для совместимости/отладки) ==========
    life: str = ""  # "scalp 30-90m ⌛️" или "intraday 2-6h ⏳" и т.д.
    legacy_metrics: Dict[str, Any] = field(default_factory=dict)
    
    # Временные поля для вычислений (не сериализуются в to_dict)
    _sl_move: Dict[str, float] = field(default_factory=dict, repr=False)
    _tp1_move: Dict[str, float] = field(default_factory=dict, repr=False)
    _tp2_move: Dict[str, float] = field(default_factory=dict, repr=False)
    # Формат:
    # {
    #   "delta_p_pct": float,           # Δ цены за окно (%)
    #   "delta_oi_pct": float,          # Δ OI за окно (%)
    #   "funding_now_pct": float,       # Текущий funding (%)
    #   "basis_pct": float,             # Текущий basis (%)
    #   "ls_top": float,                # Long/Short топ-биржи
    #   "taker_buy_share": float,       # Доля покупателей
    #   "atr_abs": float,               # ATR абсолютный
    #   "atr_pct": float,               # ATR в % от цены
    #   "sl_move": {"pct": float, "atr_mult": float},
    #   "tp1_move": {"pct": float, "atr_mult": float},
    #   "tp2_move": {"pct": float, "atr_mult": float}
    # }
    
    def add_ban_reason(self, reason: BanReason, details: str = ""):
        """Добавляет причину блокировки"""
        self.ban_reasons.append(reason)
        self.status = SignalStatus.BANNED
        print(f"[BAN] {self.symbol} {self.timeframe} {self.side}: {reason.value} - {details}")
    
    def mark_passed(self):
        """Помечает сигнал как прошедший все гейты"""
        if self.status == SignalStatus.PENDING:
            self.status = SignalStatus.PASSED
            print(f"[PASS] {self.symbol} {self.timeframe} {self.side} - {self.trigger_type} | Mode: {self.mode}")
    
    def build_gates_dict(self):
        """Строит dict gates для карточки (светофор)"""
        self.gates = {
            "vol": {
                "ok": self.atr_pass,
                "atr_pct": self.atr_pct,
                "quantile": f"Q{int(self.atr_quantile_range[0]*100)}-{int(self.atr_quantile_range[1]*100)}",
                "reason": "" if self.atr_pass else "out_of_range"
            },
            "impulse": {
                "ok": self.impulse_pass,
                "pathscore": self.pathscore,
                "anti_chop_atr": self.anti_chop_atr,
                "mono_pct": self.monotonicity_body_share * 100,
                "body_ratio": self.monotonicity_body_ratio,
                "reason": "" if self.impulse_pass else self._get_impulse_ban_reason()
            },
            "level": {
                "ok": self.space_pass,
                "dist_to_level_atr": self.distance_to_level_atr,
                "reason": "" if self.space_pass else "too_close"
            },
            "oi": {
                "ok": self.oi_flow_pass,
                "flow": self.oi_flow,
                "percentile": int(self.oi_flow_percentile),
                "mag_gate": self.oi_flow_mag_gate,
                "short_case": self._get_short_case_type(),
                "reason": "" if self.oi_flow_pass else "weak_or_opposite"
            },
            "funding": {
                "ok": self.funding_pass,
                "now": self.funding_now,
                "ctx24h_sign": "+" if self.funding_24h_sign > 0 else "-" if self.funding_24h_sign < 0 else "0",
                "z_extreme": abs(self.funding_now) > 0.0005,
                "reason": "" if self.funding_pass else "extreme_or_against"
            },
            "basis": {
                "ok": self.basis_pass,
                "value": self.basis_now,
                "reason": "" if self.basis_pass else "out_of_range"
            },
            "btc": {
                "ok": self.btc_wind_pass,
                "votes": self._format_btc_votes(),
                "lock_min": self.btc_wind_lock_minutes,
                "reason": "" if self.btc_wind_pass else "opposite_wind"
            }
        }
    
    def build_levels_summary(self):
        """Строит levels_summary строку"""
        sl_anchor = f"{self.sl_anchor_type}({self.sl_anchor_tf})" if self.sl_anchor_tf else self.sl_anchor_type
        tp1_anchor = f"{self.tp1_anchor_type}({self.tp1_anchor_tf})" if self.tp1_anchor_tf else self.tp1_anchor_type
        tp2_anchor = f"{self.tp2_anchor_type}({self.tp2_anchor_tf})" if self.tp2_anchor_tf else self.tp2_anchor_type
        
        self.levels_summary = f"SL ▸ {sl_anchor} • TP1 ▸ {tp1_anchor} • TP2 ▸ {tp2_anchor}"
    
    def build_why(self):
        """Строит короткое объяснение why (≤120 символов)"""
        # Формат: <Trigger>: <логика>, <метрика>, <пространство>
        
        # Логика триггера
        trigger_logic = {
            "TC": "продолжение тренда",
            "BR": "ретест пробоя",
            "FVR": "ретест FVG",
            "LSR": "отскок от уровня",
            "EPS": "истощение покупателей при росте",
            "LQS": "сквиз продавцов при падении"
        }.get(self.trigger_type, "сигнал")
        
        # Ключевая метрика
        if abs(self.pathscore) > 0.9:
            metric = f"сильный импульс {abs(self.pathscore):.2f}"
        elif self.oi_flow_percentile > 70:
            metric = f"поток OI {self.oi_flow_percentile:.0f}p"
        else:
            metric = f"PS {abs(self.pathscore):.2f}"
        
        # Пространство
        space = f"пространство {self.distance_to_level_atr:.2f}×ATR до {self.level_type}"
        
        self.why = f"{self.trigger_type} — {trigger_logic}, {metric}, {space}"[:120]
    
    def _get_impulse_ban_reason(self) -> str:
        """Возвращает причину бана импульса"""
        for reason in self.ban_reasons:
            if "IMPULSE" in reason.value:
                return reason.value.lower()
        return ""
    
    def _get_short_case_type(self) -> str:
        """Возвращает тип short-кейса"""
        if not self.oi_flow_short_case:
            return ""
        
        # Определяем по знаку pathscore
        if self.pathscore > 0.85:
            return "EPS"
        elif self.pathscore < -0.85:
            return "LQS"
        return ""
    
    def _format_btc_votes(self) -> str:
        """Форматирует голоса BTC"""
        if not self.btc_wind_votes:
            return "0/0"
        
        # Подсчитываем голоса
        total = len(self.btc_wind_votes)
        up_votes = sum(1 for v in self.btc_wind_votes if v.get("direction", 0) > 0)
        down_votes = sum(1 for v in self.btc_wind_votes if v.get("direction", 0) < 0)
        
        if self.btc_wind_direction > 0:
            return f"{up_votes}/{total}"
        elif self.btc_wind_direction < 0:
            return f"{down_votes}/{total}"
        else:
            return f"0/{total}"
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертирует в словарь для JSON/БД"""
        result = asdict(self)
        result['status'] = self.status.value
        result['ban_reasons'] = [r.value for r in self.ban_reasons]
        result['timestamp'] = self.timestamp.isoformat()
        
        # Конвертируем datetime в ISO
        if self.valid_until:
            result['valid_until'] = self.valid_until.isoformat()
        if self.hold_until:
            result['hold_until'] = self.hold_until.isoformat()
        
        return result
    
    def get_ui_summary(self) -> str:
        """Формирует краткую сводку для UI"""
        if self.status == SignalStatus.BANNED:
            reasons = ", ".join([r.value for r in self.ban_reasons[:3]])
            return f"❌ BANNED: {reasons}"
        
        summary = [
            f"✅ {self.trigger_type} | {self.mode.upper()}",
            f"Entry: {self.entry_price:.4f}",
            f"SL: {self.sl_final:.4f} ({self.sl_anchor_type})",
            f"TP1: {self.tp1_final:.4f} (RR {self.rr_tp1:.2f})",
            f"TP2: {self.tp2_final:.4f} (RR {self.rr_tp2:.2f})",
            f"ATR: {self.atr_pct*100:.2f}%",
            f"PathScore: {self.pathscore:.2f}",
            f"OI Flow: {self.oi_flow_percentile:.0f}p",
            f"Space: {self.distance_to_level_atr:.2f}×ATR"
        ]
        return "\n".join(summary)
    
    def get_detailed_log(self) -> str:
        """Формирует детальный лог для отладки"""
        lines = [
            f"========== SIGNAL LOG ==========",
            f"Symbol: {self.symbol} | TF: {self.timeframe} | Side: {self.side}",
            f"Mode: {self.mode} | Trigger: {self.trigger_type}",
            f"Status: {self.status.value}",
            "",
            f"[ATR GATE] Pass: {self.atr_pass}",
            f"  atr_pct: {self.atr_pct*100:.3f}% (range: {self.atr_quantile_range})",
            "",
            f"[IMPULSE GATE] Pass: {self.impulse_pass}",
            f"  PathScore: {self.pathscore:.3f} (≥ {self.pathscore_threshold:.3f})",
            f"  Anti-chop: {self.anti_chop_atr:.3f} (≤ {self.anti_chop_threshold:.3f})",
            f"  Mono: {self.monotonicity_body_share:.2%} | Ratio: {self.monotonicity_body_ratio:.2f}",
            "",
            f"[SPACE GATE] Pass: {self.space_pass}",
            f"  Distance: {self.distance_to_level_atr:.3f}×ATR (≥ {self.distance_threshold_atr:.3f})",
            f"  Level: {self.nearest_counter_level} ({self.level_type})",
            "",
            f"[OI FLOW GATE] Pass: {self.oi_flow_pass}",
            f"  Flow: {self.oi_flow:.2f} | Percentile: {self.oi_flow_percentile:.0f}%",
            f"  Mag: {self.oi_flow_mag_gate:.2f} | Short-case: {self.oi_flow_short_case}",
            "",
            f"[FUNDING/BASIS GATE]",
            f"  Funding: {self.funding_now:.4%} | 24h sign: {self.funding_24h_sign} | Pass: {self.funding_pass}",
            f"  Basis: {self.basis_now:.4%} | Pass: {self.basis_pass}",
            "",
            f"[BTC WIND GATE] Pass: {self.btc_wind_pass}",
            f"  Direction: {self.btc_wind_direction} | Lock: {self.btc_wind_lock_minutes}m",
            "",
            f"[TAKER GATE] Pass: {self.taker_pass}",
            f"  Taker Buy Share: {self.taker_buy_share:.2%}",
            ""
        ]
        
        if self.ban_reasons:
            lines.append(f"[BAN REASONS]")
            for reason in self.ban_reasons:
                lines.append(f"  - {reason.value}")
            lines.append("")
        
        if self.status == SignalStatus.PASSED:
            lines.extend([
                f"[GEOMETRY]",
                f"  Entry: {self.entry_price:.4f}",
                f"  SL: {self.sl_final:.4f} ({self.sl_anchor_type})",
                f"  TP1: {self.tp1_final:.4f} (RR {self.rr_tp1:.2f})",
                f"  TP2: {self.tp2_final:.4f} (RR {self.rr_tp2:.2f})",
                f"  Position: {self.k_pct*100:.2f}% ({self.position_size:.4f})",
                ""
            ])
        
        lines.append("=" * 40)
        return "\n".join(lines)


def create_signal_log(symbol: str, timeframe: str, side: str) -> SignalLog:
    """Создаёт новый лог-объект сигнала"""
    return SignalLog(
        symbol=symbol,
        timeframe=timeframe,
        side=side
    )

