#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RESEARCH/RECORDER.PY — исследовательский режим ("только лог")

1. record_closed_bars(): на каждой закрытой свече (с задержкой research.delay_s) для выборки
   пар × ТФ × {LONG, SHORT} прогоняет пайплайн в режиме collect_all (без остановки на первом
   бане) и пишет в strategy_candidates метрики КАЖДОГО гейта + геометрию сделки.
2. resolve_outcomes(): когда прошло research.horizon_bars баров, по закрытым свечам считает,
   что случилось бы со сделкой: что раньше — TP1 или SL, MFE/MAE в ATR, R с учётом комиссий.

Потом scripts/research_report.py сравнивает исходы тех, кто прошёл гейт, и тех, кто нет.
Пользователям ничего не отправляется.
"""

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import yaml

from binance.binance import _canonical_symbol, binance_klines
from market import TF_MS, crypto_perpetuals, top_crypto_by_volume
from strategy.features import compute_features
from strategy.logs import SignalLog, SignalStatus


with open("config/thresholds.yaml", "r", encoding="utf-8") as _f:
    _CFG = yaml.safe_load(_f) or {}

RESEARCH = _CFG.get("research", {})
TAKER_FEE = float(_CFG.get("fees", {}).get("taker", 0.0005))
TP1_CLOSE_SHARE = 0.7

_last_done: Dict[str, int] = {}
_run_lock = threading.Lock()


# =============================================================================
# DB
# =============================================================================

DDL = """
CREATE TABLE IF NOT EXISTS strategy_candidates (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    symbol        text    NOT NULL,
    timeframe     text    NOT NULL,
    side          text    NOT NULL,
    bar_close_ms  bigint  NOT NULL,
    mode          text,
    trigger_type  text,
    passed        boolean NOT NULL,
    first_ban     text,
    ban_reasons   text[],
    entry         double precision,
    sl            double precision,
    tp1           double precision,
    tp2           double precision,
    atr           double precision,
    metrics       jsonb,
    horizon_bars  int     NOT NULL,
    outcome       text,               -- TP1_FIRST / SL_FIRST / NONE / NO_GEOMETRY / NO_DATA
    ambiguous     boolean,            -- SL и TP1 в одной свече (засчитано как SL)
    hit_tp2       boolean,
    bars_to_event int,
    mfe_atr       double precision,
    mae_atr       double precision,
    r_tp1_full    double precision,   -- вся позиция закрывается на TP1/SL/горизонте
    r_policy      double precision,   -- 70% на TP1, остаток TP2/BE/горизонт
    resolved_at   timestamptz,
    UNIQUE (symbol, timeframe, side, bar_close_ms)
);
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_unresolved
    ON strategy_candidates (bar_close_ms) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_strategy_candidates_tf ON strategy_candidates (timeframe, side);
"""


def ensure_schema() -> None:
    from database.database import _db_conn
    with _db_conn() as c:
        c.cursor().execute(DDL)
        c.commit()


def _insert_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    from database.database import _db_conn
    from psycopg2.extras import execute_values
    cols = ["symbol", "timeframe", "side", "bar_close_ms", "mode", "trigger_type", "passed", "first_ban",
            "ban_reasons", "entry", "sl", "tp1", "tp2", "atr", "metrics", "horizon_bars"]
    values = [tuple(r[c] for c in cols) for r in rows]
    with _db_conn() as c:
        cur = c.cursor()
        execute_values(
            cur,
            f"INSERT INTO strategy_candidates ({', '.join(cols)}) VALUES %s "
            "ON CONFLICT (symbol, timeframe, side, bar_close_ms) DO NOTHING",
            values,
        )
        n = cur.rowcount
        c.commit()
    return n


# =============================================================================
# RECORD
# =============================================================================

def _universe() -> List[str]:
    allowed = set(crypto_perpetuals())
    syms = list(top_crypto_by_volume(int(RESEARCH.get("top_n", 40))))
    try:
        from database.database import db_enabled, db_list_all_symbols
        if db_enabled():
            for s in db_list_all_symbols():
                cs = _canonical_symbol(s)
                if cs in allowed and cs not in syms:
                    syms.append(cs)
    except Exception as e:
        print(f"[RESEARCH] user symbols failed: {e}")
    return syms


def _metrics(log: SignalLog, features: Dict[str, Any]) -> Dict[str, Any]:
    sl_dist = abs(log.entry_price - log.sl_final) if log.sl_final else 0.0
    bans = {r.value for r in log.ban_reasons}
    direction_ok = not any("импульс против" in d for d in log.ban_details)
    geometry_ok = not (bans & {"TP1_TOO_CLOSE", "RR_TOO_LOW", "RISK_LIMIT"})
    return {
        # гейты: прошёл ли каждый (в collect_all считаются все)
        "pass": {
            "atr": log.atr_pass, "impulse": log.impulse_pass, "space": log.space_pass,
            "oi_flow": log.oi_flow_pass, "funding": log.funding_pass, "basis": log.basis_pass,
            "btc": log.btc_wind_pass, "taker": log.taker_pass, "safety": getattr(log, "safety_pass", False),
            "positioning": log.positioning_pass,
            "direction": direction_ok,
            "geometry": geometry_ok,
        },
        "ban_details": log.ban_details,
        # сырые значения для подбора порогов
        "atr_pct": log.atr_pct,
        "atr_rank": getattr(log, "_atr_rank", None),
        "pathscore": log.pathscore,
        "anti_chop_atr": log.anti_chop_atr,
        "body_share": log.monotonicity_body_share,
        "body_ratio": log.monotonicity_body_ratio,
        "n_bars": log.n_bars,
        "distance_to_level_atr": log.distance_to_level_atr,
        "oi_flow": log.oi_flow,
        "oi_flow_pct": log.oi_flow_percentile,
        "oi_mag": log.oi_flow_mag_gate,
        "doi_sigma": log.doi_sigma,
        "oi_short_case": log.oi_flow_short_case,
        "funding_8h": log.funding_now,
        "funding_interval_h": features.get("funding_interval_h"),
        "funding_24h_sign": log.funding_24h_sign,
        "basis": log.basis_now,
        "btc_dir": log.btc_wind_direction,
        "taker_buy_share": log.taker_buy_share,
        "ls_top": features.get("ls_top"),
        "delta_p": features.get("price_delta_pct"),
        "delta_oi": features.get("oi_delta_pct"),
        "positioning_confirms": log.positioning_confirms,
        "positioning_vetoes": log.positioning_vetoes,
        "sl_atr": sl_dist / log.atr if log.atr else None,
        "rr_tp1": log.rr_to_sl,
        "rr_tp1_net": getattr(log, "_rr_tp1_net", None),
        "rr_tp2": log.rr_to_tp2,
        "sl_anchor": log.sl_anchor_type,
        "tp1_anchor": log.tp1_anchor_type,
    }


def _evaluate_symbol(symbol: str, tf: str, bar_close_ms: int, horizon: int) -> List[Dict[str, Any]]:
    from jobs.strategy_adapter import get_strategy_pipeline
    pipe = get_strategy_pipeline()

    features = compute_features(symbol, tf, 300)
    if int(features["bar_close_ms"]) + 1 != bar_close_ms:
        return []  # в кэше ещё старая свеча — пропускаем, запишем на следующем проходе

    mode_name = pipe.mode_manager.get_mode_for_tf(tf)
    mode_cfg = pipe.mode_manager.get_mode_config(mode_name)
    rows = []
    for side in ("LONG", "SHORT"):
        log = pipe._run_pipeline(symbol, tf, side, mode_name, mode_cfg, features, collect_all=True)
        rows.append({
            "symbol": symbol,
            "timeframe": tf,
            "side": side,
            "bar_close_ms": bar_close_ms,
            "mode": mode_name,
            "trigger_type": log.trigger_type or None,
            "passed": log.status == SignalStatus.PASSED,
            "first_ban": log.ban_reasons[0].value if log.ban_reasons else None,
            "ban_reasons": [r.value for r in log.ban_reasons],
            "entry": float(log.entry_price or features["price_now"]),
            "sl": float(log.sl_final) if log.sl_final else None,
            "tp1": float(log.tp1_final) if log.tp1_final else None,
            "tp2": float(log.tp2_final) if log.tp2_final else None,
            "atr": float(features["atr"]),
            "metrics": json.dumps(_metrics(log, features), default=str),
            "horizon_bars": horizon,
        })
    return rows


def record_closed_bars(executor: Optional[ThreadPoolExecutor] = None) -> None:
    """Вызывается раз в минуту. Пишет кандидатов для каждого ТФ, у которого закрылась новая свеча."""
    if not RESEARCH.get("enabled", False):
        return
    if not _run_lock.acquire(blocking=False):
        return
    try:
        now_ms = int(time.time() * 1000)
        delay_ms = int(RESEARCH.get("delay_s", 90)) * 1000
        horizon = int(RESEARCH.get("horizon_bars", 24))
        due = []
        for tf in RESEARCH.get("tfs", []):
            tf_ms = TF_MS[tf]
            bar_close = (now_ms // tf_ms) * tf_ms
            if now_ms - bar_close >= delay_ms and _last_done.get(tf, 0) < bar_close:
                due.append((tf, bar_close))
        if not due:
            return

        symbols = _universe()
        own_pool = executor is None
        pool = executor or ThreadPoolExecutor(max_workers=8)
        try:
            for tf, bar_close in due:
                t0 = time.time()
                futs = [pool.submit(_evaluate_symbol, s, tf, bar_close, horizon) for s in symbols]
                rows, errors, stale = [], 0, 0
                for f in futs:
                    try:
                        r = f.result(timeout=60)
                        if r:
                            rows.extend(r)
                        else:
                            stale += 1
                    except Exception as e:
                        errors += 1
                        if errors <= 3:
                            print(f"[RESEARCH] {tf} eval error: {e}")
                inserted = _insert_rows(rows)
                passed = sum(1 for r in rows if r["passed"])
                if stale == 0:
                    _last_done[tf] = bar_close
                print(f"[RESEARCH] {tf} bar {bar_close}: {len(symbols)} symbols, rows={len(rows)} "
                      f"inserted={inserted} passed={passed} stale={stale} errors={errors} in {time.time()-t0:.1f}s")
        finally:
            if own_pool:
                pool.shutdown(wait=False)
    finally:
        _run_lock.release()


# =============================================================================
# RESOLVE
# =============================================================================

def simulate_outcome(side: str, entry: float, sl: float, tp1: float, tp2: Optional[float],
                     atr: float, bars) -> Dict[str, Any]:
    """Прогон сделки по закрытым свечам (ASC). Вход по close сигнального бара.

    Консервативно: если в одной свече задеты и SL, и TP1 — засчитываем SL (ambiguous=True).
    Политика r_policy: 70% закрывается на TP1, стоп остатка → вход (BE), остаток до TP2/BE/горизонта.
    """
    sign = 1.0 if side == "LONG" else -1.0
    sl_dist = abs(entry - sl)
    if sl_dist <= 0 or (tp1 - entry) * sign <= 0 or (entry - sl) * sign <= 0:
        return {"outcome": "NO_GEOMETRY"}

    fee_r = 2 * TAKER_FEE * entry / sl_dist
    rr1 = abs(tp1 - entry) / sl_dist
    rr2 = abs(tp2 - entry) / sl_dist if tp2 else rr1

    highs = bars["high"].values.astype(float)
    lows = bars["low"].values.astype(float)
    closes = bars["close"].values.astype(float)

    fav = (highs - entry) * sign if sign > 0 else (entry - lows)
    adv = (entry - lows) if sign > 0 else (highs - entry)
    mfe_atr = float(max(0.0, fav.max())) / atr if atr > 0 else None
    mae_atr = float(max(0.0, adv.max())) / atr if atr > 0 else None

    def hit_sl(i):
        return lows[i] <= sl if sign > 0 else highs[i] >= sl

    def hit(i, price):
        return highs[i] >= price if sign > 0 else lows[i] <= price

    mtm_last = (closes[-1] - entry) * sign / sl_dist
    outcome, ambiguous, bars_to_event, tp1_bar = "NONE", False, None, None
    for i in range(len(closes)):
        s_hit, t_hit = hit_sl(i), hit(i, tp1)
        if s_hit:
            outcome, ambiguous, bars_to_event = "SL_FIRST", t_hit, i + 1
            break
        if t_hit:
            outcome, bars_to_event, tp1_bar = "TP1_FIRST", i + 1, i
            break

    hit_tp2 = False
    if outcome == "SL_FIRST":
        r_full = -1.0 - fee_r
        r_policy = -1.0 - fee_r
    elif outcome == "TP1_FIRST":
        r_full = rr1 - fee_r
        rem = (closes[-1] - entry) * sign / sl_dist  # по умолчанию — по горизонту
        for j in range(tp1_bar + 1, len(closes)):
            be_hit = lows[j] <= entry if sign > 0 else highs[j] >= entry
            if be_hit:
                rem = 0.0
                break
            if tp2 and hit(j, tp2):
                rem, hit_tp2 = rr2, True
                break
        r_policy = TP1_CLOSE_SHARE * rr1 + (1 - TP1_CLOSE_SHARE) * rem - fee_r
    else:
        r_full = mtm_last - fee_r
        r_policy = mtm_last - fee_r

    return {
        "outcome": outcome,
        "ambiguous": bool(ambiguous),
        "hit_tp2": bool(hit_tp2),
        "bars_to_event": bars_to_event,
        "mfe_atr": mfe_atr,
        "mae_atr": mae_atr,
        "r_tp1_full": float(r_full),
        "r_policy": float(r_policy),
    }


def resolve_outcomes(limit: int = 400) -> None:
    """Вызывается раз в 5 минут: досчитывает исходы кандидатов, у которых прошёл горизонт."""
    if not RESEARCH.get("enabled", False):
        return
    from database.database import _db_conn
    now_ms = int(time.time() * 1000)
    with _db_conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, symbol, timeframe, side, bar_close_ms, entry, sl, tp1, tp2, atr, horizon_bars "
            "FROM strategy_candidates WHERE outcome IS NULL ORDER BY bar_close_ms LIMIT %s", (limit * 4,))
        rows = cur.fetchall()

    # Свечи качаем один раз на (symbol, tf, bar) — LONG и SHORT делят один запрос
    klines_cache: Dict[tuple, Any] = {}
    updates = []
    for (cid, symbol, tf, side, bar_close_ms, entry, sl, tp1, tp2, atr, horizon) in rows:
        tf_ms = TF_MS.get(tf)
        if not tf_ms or bar_close_ms + horizon * tf_ms + 60_000 > now_ms:
            continue
        if len(updates) >= limit:
            break
        key = (symbol, tf, bar_close_ms, horizon)
        if key not in klines_cache:
            try:
                df = binance_klines(symbol, tf, limit=horizon, start_time=int(bar_close_ms))
                klines_cache[key] = df[df["ts"] < bar_close_ms + horizon * tf_ms]
            except Exception as e:
                print(f"[RESEARCH] klines {symbol} {tf} failed: {e}")
                klines_cache[key] = None
        bars = klines_cache[key]
        if bars is None or len(bars) < horizon:
            if now_ms - bar_close_ms > 3 * horizon * tf_ms:
                updates.append((cid, {"outcome": "NO_DATA"}))
            continue
        if sl is None or tp1 is None:
            updates.append((cid, {"outcome": "NO_GEOMETRY"}))
            continue
        updates.append((cid, simulate_outcome(side, entry, sl, tp1, tp2, atr, bars)))

    if not updates:
        return
    with _db_conn() as c:
        cur = c.cursor()
        for cid, r in updates:
            cur.execute(
                "UPDATE strategy_candidates SET outcome=%s, ambiguous=%s, hit_tp2=%s, bars_to_event=%s, "
                "mfe_atr=%s, mae_atr=%s, r_tp1_full=%s, r_policy=%s, resolved_at=now() WHERE id=%s",
                (r.get("outcome"), r.get("ambiguous"), r.get("hit_tp2"), r.get("bars_to_event"),
                 r.get("mfe_atr"), r.get("mae_atr"), r.get("r_tp1_full"), r.get("r_policy"), cid))
        c.commit()
    print(f"[RESEARCH] resolved {len(updates)} candidates")
