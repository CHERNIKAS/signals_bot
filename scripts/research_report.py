#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Отчёт по исследовательскому режиму (таблица strategy_candidates).

Запуск в контейнере:
    docker compose exec bot python scripts/research_report.py [--tf 1h] [--side LONG] [--days 14]

Что показывает (только кандидаты с посчитанным исходом и валидной геометрией):
  1. База: все кандидаты vs прошедшие все гейты.
  2. По каждому гейту: исход у прошедших гейт vs не прошедших.
     Если у не прошедших R не хуже — гейт режет количество, а не убытки.
  3. "Единственный бан": кандидаты, которых отсёк ровно один гейт. Если их R > 0 —
     этот гейт выкидывает прибыльные сделки, его порог стоит ослабить.
  4. Квинтили сырых метрик → средний R. Видно, где на самом деле проходит граница.

R — в единицах риска (дистанции SL), комиссии учтены. r_policy: 70% на TP1, остаток TP2/BE.
"""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GATES = ["atr", "impulse", "direction", "space", "oi_flow", "funding", "basis", "btc", "taker",
         "positioning", "geometry"]
NUMERIC = ["atr_rank", "pathscore", "anti_chop_atr", "body_share", "distance_to_level_atr",
           "oi_flow_pct", "oi_mag", "funding_8h", "basis", "taker_buy_share", "ls_top",
           "delta_p", "delta_oi", "rr_tp1_net", "sl_atr"]


def load(days: int, tf: str = None, side: str = None) -> pd.DataFrame:
    from dotenv import load_dotenv
    load_dotenv(".env")
    from database.database import _db_conn
    q = ("SELECT symbol, timeframe, side, bar_close_ms, passed, first_ban, ban_reasons, metrics, "
         "outcome, ambiguous, hit_tp2, mfe_atr, mae_atr, r_tp1_full, r_policy "
         "FROM strategy_candidates WHERE outcome IN ('TP1_FIRST','SL_FIRST','NONE') "
         "AND bar_close_ms >= %s")
    params = [int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).timestamp() * 1000)]
    if tf:
        q += " AND timeframe = %s"; params.append(tf)
    if side:
        q += " AND side = %s"; params.append(side)
    with _db_conn() as c:
        df = pd.read_sql(q, c, params=params)
    if df.empty:
        return df
    m = df["metrics"].apply(lambda x: x if isinstance(x, dict) else json.loads(x))
    for g in GATES:
        df[f"pass_{g}"] = m.apply(lambda d: bool(d.get("pass", {}).get(g)))
    for k in NUMERIC:
        df[k] = pd.to_numeric(m.apply(lambda d: d.get(k)), errors="coerce")
    df["abs_pathscore"] = df["pathscore"].abs()
    df["win"] = df["outcome"] == "TP1_FIRST"
    return df


def summary(d: pd.DataFrame) -> str:
    if d.empty:
        return "n=0"
    return (f"n={len(d):5d}  TP1-first={d['win'].mean():6.1%}  "
            f"R(policy)={d['r_policy'].mean():+.3f}  R(full)={d['r_tp1_full'].mean():+.3f}  "
            f"MFE={d['mfe_atr'].mean():.2f}ATR MAE={d['mae_atr'].mean():.2f}ATR")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tf")
    ap.add_argument("--side", choices=["LONG", "SHORT"])
    a = ap.parse_args()

    df = load(a.days, a.tf, a.side)
    if df.empty:
        print("Нет кандидатов с посчитанным исходом. Подождите, пока пройдёт горизонт (research.horizon_bars).")
        return

    print(f"=== Кандидаты за {a.days} дн. tf={a.tf or 'все'} side={a.side or 'обе'} ===")
    print(f"Все                 {summary(df)}")
    print(f"Прошли все гейты    {summary(df[df['passed']])}")
    print(f"(неоднозначные свечи, засчитаны как SL: {df['ambiguous'].fillna(False).mean():.1%})")

    print("\n=== По гейтам: прошли vs не прошли ===")
    for g in GATES:
        col = f"pass_{g}"
        print(f"{g:12s} ✅ {summary(df[df[col]])}")
        print(f"{'':12s} ❌ {summary(df[~df[col]])}")

    print("\n=== Отсечены ровно одним гейтом (что бы дал этот гейт, если его убрать) ===")
    single = df[df["ban_reasons"].apply(lambda r: r is not None and len(r) == 1)]
    for reason, grp in single.groupby(single["ban_reasons"].apply(lambda r: r[0])):
        print(f"{reason:26s} {summary(grp)}")

    print("\n=== Квинтили метрик → средний R(policy) ===")
    for k in ["abs_pathscore"] + [x for x in NUMERIC if x != "pathscore"]:
        s = df[[k, "r_policy", "win"]].dropna()
        if s[k].nunique() < 5:
            continue
        try:
            s["q"] = pd.qcut(s[k], 5, duplicates="drop")
        except ValueError:
            continue
        g = s.groupby("q", observed=True).agg(n=("r_policy", "size"), R=("r_policy", "mean"), win=("win", "mean"))
        cells = "  ".join(f"[{iv.left:.3g};{iv.right:.3g}] R={r.R:+.2f} w={r.win:.0%}" for iv, r in g.iterrows())
        print(f"{k:22s} {cells}")

    print("\n=== По ТФ и стороне ===")
    for (tf, side), grp in df.groupby(["timeframe", "side"]):
        print(f"{tf:4s} {side:5s} все: {summary(grp)}")


if __name__ == "__main__":
    main()
