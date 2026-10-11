# audits/audit_stats.py
"""
Audit module for validating bot statistics without changing strategy/resolver logic.

Usage:
    from audits.audit_stats import audit_from_csv
    report = audit_from_csv("trades_2025-10-25.csv", "audit_2025-10-25")
    print(report)
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass

PATH_SL_BEFORE = {"SL_BEFORE_TP1", "SLдоTP1", "SL_BEFORE"}
PATH_TP1_SL    = {"TP1_TO_SL", "TP1→SL", "TP1_SL"}
PATH_TP1_TP2   = {"TP1_PLUS_TP2", "TP1+TP2", "TP1_TP2"}
PATH_TP1_OPEN  = {"TP1_TO_OPEN", "TP1→OPEN", "TP1_OPEN"}


def _canon(s: str) -> str:
    """Normalize path name for comparison"""
    return (str(s) or "").upper().replace(" ", "").replace("→", "")


def _isin(v: str, opts: set[str]) -> bool:
    """Check if value matches any option (case-insensitive, normalized)"""
    return _canon(v) in {_canon(o) for o in opts}


def _to_ts(x):
    """Convert value to timestamp, return NaT on failure"""
    try:
        return pd.to_datetime(x)
    except Exception:
        return pd.NaT


@dataclass
class AuditAgg:
    """Aggregated statistics for audit report"""
    total: int
    closed: int
    open: int
    n1: int  # SL_BEFORE_TP1
    n2: int  # TP1_PLUS_TP2
    n3: int  # TP1_TO_SL
    n4: int  # TP1_TO_OPEN
    final: float
    weighted: float
    touch: float
    tp1_touch_cnt: int
    tp1_touch_base: int


def load_trades_csv(path: str) -> pd.DataFrame:
    """
    Load trades CSV and normalize column names.
    Expected columns: id, symbol, side, entry, sl, tp1, tp2, path, 
                      tp1_time, tp2_time, sl_time, pnl_pct
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    
    # Ensure time columns exist
    for col in ("tp1_time", "tp2_time", "sl_time"):
        if col not in df.columns:
            df[col] = pd.NaT
        else:
            df[col] = df[col].apply(_to_ts)
    
    if "path" not in df.columns:
        raise ValueError("CSV must contain 'path' column")
    
    return df


def aggregate_paths(tr: pd.DataFrame) -> AuditAgg:
    """
    Calculate statistics from trades DataFrame.
    Returns AuditAgg with all metrics matching bot's calculation logic.
    """
    n1 = tr["path"].apply(lambda v: _isin(v, PATH_SL_BEFORE)).sum()
    n3 = tr["path"].apply(lambda v: _isin(v, PATH_TP1_SL)).sum()
    n2 = tr["path"].apply(lambda v: _isin(v, PATH_TP1_TP2)).sum()
    n4 = tr["path"].apply(lambda v: _isin(v, PATH_TP1_OPEN)).sum()

    closed = n1 + n2 + n3
    open_ = n4 + (len(tr) - (n1 + n2 + n3 + n4))
    total = len(tr)

    def pct(a, b):
        return round(100 * a / b, 1) if b > 0 else 0.0

    final = pct(n2, closed)
    weighted = pct(n2 + 0.5 * n3, closed)
    tp1_touch_cnt = n2 + n3 + n4
    tp1_touch_base = closed + n4
    touch = pct(tp1_touch_cnt, tp1_touch_base)

    return AuditAgg(
        total, closed, open_, n1, n2, n3, n4, 
        final, weighted, touch,
        tp1_touch_cnt, tp1_touch_base
    )


def find_anomalies(tr: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Detect data inconsistencies and edge cases.
    Returns dict of anomaly_type -> DataFrame with problematic rows.
    """
    flags = {}

    # 1) TP1+TP2 без tp1_time
    m_tp2 = tr["path"].apply(lambda v: _isin(v, PATH_TP1_TP2))
    f = tr[m_tp2 & tr["tp1_time"].isna()]
    if not f.empty:
        flags["tp2_without_tp1"] = f

    # 2) TP1+TP2, но SL раньше/одновременно с TP1
    # Создаём маску для валидных строк, затем сравниваем
    mask_valid = m_tp2 & tr["sl_time"].notna() & tr["tp1_time"].notna()
    if mask_valid.any():
        # Используем loc для безопасного сравнения
        valid_rows = tr[mask_valid].copy()
        compare_result = valid_rows["sl_time"] <= valid_rows["tp1_time"]
        f = valid_rows[compare_result]
        if not f.empty:
            flags["tp2_but_sl_before_tp1"] = f

    # 3) SL до TP1, но tp1_time ≤ sl_time
    m_sl = tr["path"].apply(lambda v: _isin(v, PATH_SL_BEFORE))
    mask_valid = m_sl & tr["tp1_time"].notna() & tr["sl_time"].notna()
    if mask_valid.any():
        valid_rows = tr[mask_valid].copy()
        compare_result = valid_rows["tp1_time"] <= valid_rows["sl_time"]
        f = valid_rows[compare_result]
        if not f.empty:
            flags["sl_before_tp1_but_tp1_first"] = f

    # 4) TP1→OPEN, но есть tp2_time/sl_time
    m_open = tr["path"].apply(lambda v: _isin(v, PATH_TP1_OPEN))
    f = tr[m_open & (tr["tp2_time"].notna() | tr["sl_time"].notna())]
    if not f.empty:
        flags["tp1_open_has_exit_times"] = f

    # 5) Одно и то же время (тай-брейк)
    # Проверяем tp1_time == sl_time
    mask1_valid = tr["tp1_time"].notna() & tr["sl_time"].notna()
    if mask1_valid.any():
        valid_rows1 = tr[mask1_valid].copy()
        compare1 = valid_rows1["tp1_time"] == valid_rows1["sl_time"]
        matched1 = valid_rows1[compare1]
    else:
        matched1 = pd.DataFrame()
    
    # Проверяем tp2_time == sl_time
    mask2_valid = tr["tp2_time"].notna() & tr["sl_time"].notna()
    if mask2_valid.any():
        valid_rows2 = tr[mask2_valid].copy()
        compare2 = valid_rows2["tp2_time"] == valid_rows2["sl_time"]
        matched2 = valid_rows2[compare2]
    else:
        matched2 = pd.DataFrame()
    
    # Объединяем результаты
    if not matched1.empty or not matched2.empty:
        f = pd.concat([matched1, matched2]).drop_duplicates()
        if not f.empty:
            flags["same_timestamp_tp_sl"] = f

    return flags


def make_report(agg: AuditAgg) -> str:
    """Generate human-readable audit report"""
    lines = []
    lines.append("=== AUDIT SUMMARY ===")
    lines.append(f"Всего записей: {agg.total}")
    lines.append(f"Закрыто: {agg.closed} | Открыто: {agg.open}")
    lines.append(f"Пути: SLдоTP1={agg.n1}, TP1→SL={agg.n3}, TP1+TP2={agg.n2}, TP1→OPEN={agg.n4}")
    lines.append(
        f"WINRATE: Final={agg.final}%, Weighted={agg.weighted}%, "
        f"Touch={agg.touch}% ({agg.tp1_touch_cnt} из {agg.tp1_touch_base})"
    )
    
    # Sanity check
    if agg.final >= 95.0 and agg.open > 0 and agg.closed >= 20:
        lines.append("⚠️ SANITY: финальный винрейт ≥95% при наличии открытых — проверь тай-брейк/буфер.")
    
    return "\n".join(lines)


def audit_from_csv(trades_csv: str, anomalies_out_prefix: str | None = None) -> str:
    """
    Main audit function: load CSV, calculate stats, find anomalies, generate report.
    
    Args:
        trades_csv: Path to trades CSV export from bot
        anomalies_out_prefix: If provided, save anomalies to CSV files with this prefix
    
    Returns:
        Text report with statistics summary
    """
    tr = load_trades_csv(trades_csv)
    agg = aggregate_paths(tr)
    flags = find_anomalies(tr)
    
    if anomalies_out_prefix:
        for key, df in flags.items():
            cols = [
                c for c in (
                    "id", "symbol", "side", "entry", "sl", "tp1", "tp2", "path",
                    "tp1_time", "tp2_time", "sl_time", "pnl_pct"
                ) if c in df.columns
            ]
            df[cols].to_csv(f"{anomalies_out_prefix}.{key}.csv", index=False)
    
    return make_report(agg)


def load_candles_csv(path: str) -> pd.DataFrame:
    """
    Load candles CSV for verification.
    Expected columns: symbol, open_time, open, high, low, close
    """
    c = pd.read_csv(path)
    c.columns = [x.strip().lower() for x in c.columns]
    c["open_time"] = pd.to_datetime(c["open_time"])
    return c


def verify_trades_with_candles(
    tr: pd.DataFrame,
    candles: pd.DataFrame,
    fee_buffer: float = 0.0002,
    tf_sec: int = 60
) -> dict[str, pd.DataFrame]:
    """
    Verify trades against real candles to detect discrepancies.
    
    Args:
        tr: Trades DataFrame from load_trades_csv
        candles: Candles DataFrame from load_candles_csv
        fee_buffer: Fee buffer in decimal (0.0002 = 2 b.p.)
        tf_sec: Timeframe in seconds (60 = 1m)
    
    Returns:
        Dict of anomaly_type -> DataFrame with problematic trades
    """
    from audits.candle_verifier import VerifyCfg, verify_path
    
    flags = {}
    rows_bad_tp2_no_tp1 = []
    rows_winner_but_sl = []
    rows_open_but_hit = []

    for _, t in tr.iterrows():
        sym = t.get("symbol")
        side = str(t.get("side", "LONG")).upper()
        
        # Try to get entry timestamp from various columns
        entry_ts = t.get("time_open") or t.get("open_time") or t.get("tp1_time") or t.get("sl_time")
        if pd.isna(entry_ts):
            continue

        # Filter candles for this symbol
        cs = candles[candles["symbol"] == sym].sort_values("open_time")
        if cs.empty:
            continue

        # Verify path
        cfg = VerifyCfg(side=side, fee_buffer=fee_buffer, tf_sec=tf_sec)
        try:
            res = verify_path(
                cs, entry_ts,
                float(t["entry"]), float(t["tp1"]), float(t["tp2"]), float(t["sl"]),
                cfg
            )
        except Exception as e:
            # Skip trades with invalid data
            continue

        # Check path from CSV
        path = str(t.get("path", "")).upper().replace(" ", "")
        is_winner = "TP1+TP2" in path or "TP1_PLU" in path or "TP1TP2" in path
        is_open = "OPEN" in path

        # Anomaly 1: TP2 hit without TP1
        if res.tp2_hit and (not res.tp1_hit):
            rows_bad_tp2_no_tp1.append(t)

        # Anomaly 2: Winner path but candles show SL (not TP2)
        if is_winner and (res.sl_hit and (not res.tp2_hit)):
            rows_winner_but_sl.append(t)

        # Anomaly 3: OPEN path but candles show exit (SL or TP2)
        if is_open and (res.sl_hit or res.tp2_hit):
            rows_open_but_hit.append(t)

    if rows_bad_tp2_no_tp1:
        flags["tp2_verified_without_tp1"] = pd.DataFrame(rows_bad_tp2_no_tp1)
    if rows_winner_but_sl:
        flags["winner_candle_disagrees"] = pd.DataFrame(rows_winner_but_sl)
    if rows_open_but_hit:
        flags["open_should_be_closed"] = pd.DataFrame(rows_open_but_hit)

    return flags


def redflag_zero_losses(closed: int, n1: int, n3: int, min_closed: int = 20) -> bool:
    """
    Check if results are "too perfect" — red flag for verification.
    
    Returns True if closed >= min_closed and n1+n3 == 0 (zero losses)
    """
    return closed >= min_closed and (n1 + n3 == 0)

