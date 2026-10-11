# scripts/audit_cli.py
"""
CLI tool for deep auditing of bot statistics from CSV exports.

Usage:
    Basic: python scripts/audit_cli.py --trades ./exports/trades_7d.csv
    Deep:  python scripts/audit_cli.py --trades ./exports/trades_7d.csv \
                                       --candles ./exports/candles_m1_7d.csv \
                                       --fee-buffer 0.0002 --tf-sec 60

Output:
    - Prints summary report to console
    - Saves anomaly CSVs (if found) with specified prefix
    - Deep verification against real candles (if --candles provided)
"""
import argparse
import sys
from pathlib import Path

# Add parent directory to path to import audits module
sys.path.insert(0, str(Path(__file__).parent.parent))

from audits.audit_stats import (
    audit_from_csv,
    load_trades_csv,
    load_candles_csv,
    aggregate_paths,
    make_report,
    verify_trades_with_candles,
    redflag_zero_losses
)


def main():
    parser = argparse.ArgumentParser(
        description="Deep audit of bot stats (no strategy changes).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Basic audit
    python scripts/audit_cli.py --trades exports/trades_day.csv
    
    # Deep verification with candles
    python scripts/audit_cli.py --trades exports/trades_7d.csv \\
                                --candles exports/candles_m1_7d.csv \\
                                --fee-buffer 0.0002 \\
                                --tf-sec 60 \\
                                --out-prefix audit/audit_7d
        """
    )
    parser.add_argument(
        "--trades",
        required=True,
        help="Path to 'Сделки (CSV)' export from bot"
    )
    parser.add_argument(
        "--candles",
        required=False,
        help="Path to candles CSV (columns: symbol,open_time,open,high,low,close)"
    )
    parser.add_argument(
        "--fee-buffer",
        type=float,
        default=0.0002,
        help="Fee buffer in decimal (default: 0.0002 = 2 b.p.)"
    )
    parser.add_argument(
        "--tf-sec",
        type=int,
        default=60,
        help="Timeframe in seconds (default: 60 = 1m)"
    )
    parser.add_argument(
        "--out-prefix",
        default="audit_flags",
        help="Prefix for anomalies CSV outputs (default: audit_flags)"
    )
    args = parser.parse_args()

    # Validate input file exists
    if not Path(args.trades).exists():
        print(f"❌ Error: File not found: {args.trades}", file=sys.stderr)
        sys.exit(1)

    if args.candles and not Path(args.candles).exists():
        print(f"❌ Error: Candles file not found: {args.candles}", file=sys.stderr)
        sys.exit(1)

    # Create output directory if needed
    if args.out_prefix:
        out_dir = Path(args.out_prefix).parent
        if out_dir and not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # === BASIC AUDIT ===
        print("=" * 50)
        print("BASIC AUDIT")
        print("=" * 50)
        
        tr = load_trades_csv(args.trades)
        agg = aggregate_paths(tr)
        print(make_report(agg))
        print()

        # === RED FLAG: ZERO LOSSES ===
        if redflag_zero_losses(agg.closed, agg.n1, agg.n3, min_closed=20):
            print("🚩 RED FLAG: 0 проигрышей при закрытых ≥ 20. Нужно подтвердить по свечам.")
            print()

        # Check standard anomalies
        anomaly_types = [
            "tp2_without_tp1",
            "tp2_but_sl_before_tp1",
            "sl_before_tp1_but_tp1_first",
            "tp1_open_has_exit_times",
            "same_timestamp_tp_sl"
        ]
        
        found_anomalies = []
        for anom_type in anomaly_types:
            anom_file = Path(f"{args.out_prefix}.{anom_type}.csv")
            if anom_file.exists():
                found_anomalies.append(str(anom_file))
        
        if found_anomalies:
            print("⚠️ Standard anomalies detected! Check these files:")
            for f in found_anomalies:
                print(f"  - {f}")
            print()

        # === DEEP VERIFICATION WITH CANDLES ===
        if args.candles:
            print("=" * 50)
            print("DEEP VERIFICATION (CANDLES)")
            print("=" * 50)
            print(f"Fee buffer: {args.fee_buffer * 10000:.1f} b.p.")
            print(f"Timeframe: {args.tf_sec}s")
            print()

            candles = load_candles_csv(args.candles)
            print(f"Loaded {len(candles)} candles for {candles['symbol'].nunique()} symbols")
            print()

            flags = verify_trades_with_candles(
                tr, candles,
                fee_buffer=args.fee_buffer,
                tf_sec=args.tf_sec
            )

            if not flags:
                print("✅ Верификация по свечам: явных расхождений нет.")
            else:
                print("⚠️ Верификация по свечам: найдены расхождения — выгружаю CSV...")
                print()
                
                for key, df in flags.items():
                    out = f"{args.out_prefix}.{key}.csv"
                    cols = [
                        c for c in (
                            "id", "symbol", "side", "entry", "sl", "tp1", "tp2", "path",
                            "tp1_time", "tp2_time", "sl_time", "pnl_pct"
                        ) if c in df.columns
                    ]
                    df[cols].to_csv(out, index=False)
                    print(f"  → {out}: {len(df)} строк")
                
                print()
                print("🔍 Типы расхождений:")
                for key in flags:
                    if key == "tp2_verified_without_tp1":
                        print("  • TP2 достигнут по свечам, но TP1 не подтверждён")
                    elif key == "winner_candle_disagrees":
                        print("  • Путь показан как TP1+TP2, но свечи показывают SL раньше")
                    elif key == "open_should_be_closed":
                        print("  • Путь показан как OPEN, но свечи показывают выход (TP2/SL)")
        else:
            print("ℹ️ Для глубокой верификации добавьте --candles с CSV свечей")
            
    except Exception as e:
        print(f"❌ Audit failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

