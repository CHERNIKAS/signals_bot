# audits/candle_verifier.py
"""
Candle-based verification for trade paths.
Verifies TP1/TP2/SL hits against real OHLC data with fee buffer and tie-break logic.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class VerifyCfg:
    """Configuration for path verification"""
    side: str              # "LONG"|"SHORT"
    fee_buffer: float      # 0.0002 = 2 б.п.
    tf_sec: int            # размер свечи в секундах (например, 60)
    bar_skew_ms: int = 0   # если таймстампы сдвинуты


@dataclass
class VerifyResult:
    """Result of path verification"""
    tp1_hit: bool
    tp2_hit: bool
    sl_hit: bool
    order_ok: bool                  # порядок событий корректен
    first_event: Optional[str]      # "TP1"|"TP2"|"SL"|None
    details: str


def _apply_buffer(level: float, side: str, typ: str, fee: float) -> float:
    """
    Apply fee buffer to level.
    For LONG: TP moves lower (harder to hit), SL moves higher (easier to hit)
    For SHORT: opposite
    """
    if side == "LONG":
        if typ.startswith("TP"):
            return level * (1 - fee)
        if typ == "SL":
            return level * (1 + fee)
    else:  # SHORT
        if typ.startswith("TP"):
            return level * (1 + fee)
        if typ == "SL":
            return level * (1 - fee)
    return level


def verify_path(
    candles: pd.DataFrame,
    entry_ts: pd.Timestamp,
    entry: float,
    tp1: float,
    tp2: float,
    sl: float,
    cfg: VerifyCfg
) -> VerifyResult:
    """
    Verify trade path against real candles.
    
    Args:
        candles: DataFrame with ['open_time','open','high','low','close']
        entry_ts: Entry timestamp
        entry: Entry price
        tp1: Take Profit 1 level
        tp2: Take Profit 2 level
        sl: Stop Loss level
        cfg: Verification configuration
    
    Returns:
        VerifyResult with hit flags and details
    """
    # Normalize timestamp
    ts = pd.to_datetime(entry_ts)
    fee = cfg.fee_buffer

    # Apply fee buffer to levels
    tp1_eff = _apply_buffer(tp1, cfg.side, "TP1", fee)
    tp2_eff = _apply_buffer(tp2, cfg.side, "TP2", fee)
    sl_eff = _apply_buffer(sl, cfg.side, "SL", fee)

    # Take candles starting from entry bar
    c = candles.copy()
    c["open_time"] = pd.to_datetime(c["open_time"])
    c = c[c["open_time"] >= (ts - pd.Timedelta(milliseconds=cfg.bar_skew_ms))].reset_index(drop=True)

    if c.empty:
        return VerifyResult(False, False, False, True, None, "No candles after entry")

    def hit_tp1(row):
        return (row["high"] >= tp1_eff) if cfg.side == "LONG" else (row["low"] <= tp1_eff)

    def hit_tp2(row):
        return (row["high"] >= tp2_eff) if cfg.side == "LONG" else (row["low"] <= tp2_eff)

    def hit_sl(row):
        return (row["low"] <= sl_eff) if cfg.side == "LONG" else (row["high"] >= sl_eff)

    # Iterate through bars, mark first event
    tp1_seen = False
    tp2_seen = False
    sl_seen = False
    first = None
    details = []

    for idx, row in c.iterrows():
        _tp1 = hit_tp1(row)
        _tp2 = hit_tp2(row)
        _sl = hit_sl(row)

        # All false — next candle
        if not (_tp1 or _tp2 or _sl):
            continue

        # Tie-break on same bar: conservative in favor of SL
        if _sl and (_tp1 or _tp2):
            sl_seen = True
            first = first or "SL"
            details.append(f"tie-break: SL with TP on {row['open_time']}")
            break

        # Normal order
        if _sl:
            sl_seen = True
            first = first or "SL"
            break

        if _tp1 and not tp1_seen:
            tp1_seen = True
            first = first or "TP1"

        if _tp2:
            tp2_seen = True
            # If TP2 without TP1 — also note, but it's a red flag for TP2 path
            if not tp1_seen:
                first = first or "TP2"
            break

    # Check order correctness
    order_ok = True
    if tp2_seen and not tp1_seen:
        order_ok = False
        details.append("TP2 hit without prior TP1")

    if sl_seen and tp1_seen:
        # SL caught after TP1 — this is TP1->SL case (valid),
        # but not OK for winner path
        details.append("SL after TP1 (TP1->SL path)")

    return VerifyResult(tp1_seen, tp2_seen, sl_seen, order_ok, first, "; ".join(details) or "OK")

