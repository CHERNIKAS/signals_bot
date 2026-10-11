#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
POSITIONING.PY — гейт позиционирования (бывшая стратегия v1)

v1 смотрела не на график, а на то, КТО входит в рынок: связка ΔP / ΔOI / funding /
basis / taker / LS(top). В v2 это стало гейтом-подтверждением после структурных
гейтов (импульс, уровни, OI flow):

  Подтверждения (нужно ≥1 в режиме "require"):
    LONG:
      NEW_LONGS     ΔP↑ & ΔOI↑ & basis ≥ floor & funding(8h) ≥ 0 & LS(top) ≤ only_short_ls
      DEMAND_TEST   ΔP↓ & объём ≥ среднего & (ΔOI↓ или funding ≤ 0)      — лонги вытряхнули
    SHORT:
      NEW_SHORTS    ΔP↓ & ΔOI↑ & LS(top) ≥ only_long_ls                   — (новое, симметрия NEW_LONGS)
      TRAP_UP       ΔP↑ & ΔOI ≤ trap_oi & basis ≤ 0                       — рост без новых денег
      EXHAUST_UP    ΔP↑ & ΔOI ≤ 0 & taker ≥ exhaust_taker                 — рыночные покупки в пустоту
      LONGS_TRAPPED ΔP↓ & ΔOI↑ & funding > 0                              — лонги зажаты

  Вето (режут сторону в любом режиме, кроме "off"):
    ONLY_SHORT    LS(top) > only_short_ls & funding > 0  → нельзя LONG (толпа в лонге)
    ONLY_LONG     LS(top) < only_long_ls & funding < 0   → нельзя SHORT
    TAKER         LONG: taker < req(session) / SHORT: taker > req(session)
    OI_SPIKE      всплеск OI ≥ k·σ без объёма → обе стороны

Все ΔP/ΔOI — доли (0.01 = 1%), funding — приведённый к 8ч.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math

import yaml


@dataclass
class PositioningResult:
    ok: bool
    confirms: List[str] = field(default_factory=list)
    vetoes: List[str] = field(default_factory=list)
    session: str = "normal"
    taker_required: float = 0.5


class PositioningAnalyzer:
    def __init__(self, config_path: str = "config/thresholds.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f).get('positioning', {})

    @property
    def mode(self) -> str:
        return self.config.get('mode', 'require')

    # ------------------------------------------------------------------ helpers

    def session_for(self, ts_ms: Optional[int]) -> str:
        """low (00–06 UTC) / high (13–20 UTC) / normal — по времени закрытия бара."""
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
        h = dt.hour
        if 0 <= h < 6:
            return "low"
        if 13 <= h < 20:
            return "high"
        return "normal"

    def taker_required(self, session: str, side: str) -> float:
        return float(self.config['taker_required'][side.lower()][session])

    @staticmethod
    def oi_spike_without_volume(features: Dict[str, Any], sigma_mult: float) -> bool:
        oi = features.get("oi")
        if oi is None or len(oi) < 20:
            return False
        mu = float(oi.rolling(20).mean().iloc[-1])
        sd = float(oi.rolling(20).std().iloc[-1])
        last = float(oi.iloc[-1])
        if any(math.isnan(x) for x in (mu, sd, last)) or sd <= 0:
            return False
        spike = (last - mu) / sd
        return spike >= sigma_mult and features.get("volume_now", 0) <= features.get("volume_mean", 0)

    # ------------------------------------------------------------------ main

    def evaluate(self, features: Dict[str, Any], side: str) -> PositioningResult:
        c = self.config
        dp = float(features.get("price_delta_pct", 0.0))
        doi = float(features.get("oi_delta_pct", 0.0))
        basis = float(features.get("basis", 0.0))
        funding = float(features.get("funding", 0.0))
        ls_top = float(features.get("ls_top", 1.0))
        taker = float(features.get("taker_buy_share", 0.5))
        vol_ok = features.get("volume_now", 0) >= features.get("volume_mean", 0)

        only_short_ls = float(c['only_short_ls'])
        only_long_ls = float(c['only_long_ls'])

        session = self.session_for(features.get("bar_close_ms"))
        req = self.taker_required(session, side)
        res = PositioningResult(ok=False, session=session, taker_required=req)

        # ---------------- подтверждения ----------------
        if side == "LONG":
            if dp > 0 and doi > 0 and basis >= float(c['basis_long_floor']) and funding >= 0 and ls_top <= only_short_ls:
                res.confirms.append("NEW_LONGS")
            if dp < 0 and vol_ok and (doi < 0 or funding <= 0):
                res.confirms.append("DEMAND_TEST")
        else:
            if dp < 0 and doi > 0 and ls_top >= only_long_ls:
                res.confirms.append("NEW_SHORTS")
            if dp > 0 and doi <= float(c['trap_oi_max']) and basis <= 0:
                res.confirms.append("TRAP_UP")
            if dp > 0 and doi <= 0 and taker >= float(c['exhaust_taker_min']):
                res.confirms.append("EXHAUST_UP")
            if dp < 0 and doi > 0 and funding > 0:
                res.confirms.append("LONGS_TRAPPED")

        # ---------------- вето ----------------
        if side == "LONG" and ls_top > only_short_ls and funding > 0:
            res.vetoes.append(f"ONLY_SHORT(LS {ls_top:.2f})")
        if side == "SHORT" and ls_top < only_long_ls and funding < 0:
            res.vetoes.append(f"ONLY_LONG(LS {ls_top:.2f})")

        if side == "LONG" and taker < req:
            res.vetoes.append(f"TAKER({taker:.3f}<{req:.2f},{session})")
        if side == "SHORT" and taker > req:
            res.vetoes.append(f"TAKER({taker:.3f}>{req:.2f},{session})")

        if self.oi_spike_without_volume(features, float(c['oi_spike_sigma'])):
            res.vetoes.append("OI_SPIKE_NO_VOLUME")

        # ---------------- итог ----------------
        if self.mode == "off":
            res.ok = True
        elif self.mode == "veto_only":
            res.ok = not res.vetoes
        else:  # require
            res.ok = bool(res.confirms) and not res.vetoes
        return res


def _get_positioning_analyzer() -> PositioningAnalyzer:
    if not hasattr(_get_positioning_analyzer, '_instance'):
        _get_positioning_analyzer._instance = PositioningAnalyzer()
    return _get_positioning_analyzer._instance
