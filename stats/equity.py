#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal

Side = Literal["long", "short"]


@dataclass
class Leg:
    type: str           # "TP1"|"TP2"|"SL"|"TRAIL"|"BE"|"MANUAL"
    price: float
    qty_frac: float
    time: float         # timestamp (seconds)


@dataclass
class Trade:
    entry_price: float
    side: Side
    legs: List[Leg]
    fee_round_pct: float = 0.0
    funding_paid_pct: float = 0.0
    close_time: float = 0.0  # seconds timestamp


def trade_return_1x(tr: Trade) -> float:
    e = float(tr.entry_price)
    if not e or e <= 0:
        return 0.0

    ret_weighted = 0.0
    for leg in tr.legs:
        exit_price = float(leg.price)
        if tr.side == "long":
            leg_ret = (exit_price - e) / e
        else:
            leg_ret = (e - exit_price) / e
        ret_weighted += float(leg.qty_frac) * leg_ret

    return float(ret_weighted) - float(tr.fee_round_pct) - float(tr.funding_paid_pct)


def day_equity_result(trades: List[Trade], start_equity: float) -> float:
    """
    Рассчитывает итоговый результат equity как простое суммирование процентов от начального капитала.
    Каждая сделка дает процент от start_equity, а не от текущего equity (не compound).
    """
    if start_equity <= 0:
        return 0.0
    
    # Простое суммирование процентов от начального капитала
    total_return = 0.0
    for tr in trades:
        r = trade_return_1x(tr)
        total_return += r
    
    return total_return


