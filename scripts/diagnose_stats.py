#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Диагностика статистики сигналов
Проверяет правильность сохранения и отображения сигналов для всех пользователей
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

# Добавляем корневую папку в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import _db_conn, db_enabled, db_list_users


def diagnose_user_signals(uid: int) -> Dict[str, Any]:
    """Диагностика сигналов конкретного пользователя"""
    if not db_enabled():
        return {"error": "Database not enabled"}
    
    with _db_conn() as c, c.cursor() as cur:
        # Получаем информацию о пользователе
        cur.execute("""
            SELECT user_id, username, created_at, timezone
            FROM users
            WHERE user_id = %s
        """, (uid,))
        user_row = cur.fetchone()
        
        if not user_row:
            return {"error": f"User {uid} not found"}
        
        user_info = {
            "user_id": user_row[0],
            "username": user_row[1],
            "created_at": user_row[2],
            "timezone": user_row[3]
        }
        
        # Общая статистика сигналов
        cur.execute("""
            SELECT 
                COUNT(*) as total_signals,
                MIN(bar_ts) as first_signal,
                MAX(bar_ts) as last_signal,
                COUNT(DISTINCT symbol) as unique_symbols,
                COUNT(DISTINCT timeframe) as unique_timeframes
            FROM signals_log
            WHERE user_id = %s
        """, (uid,))
        total_stats = cur.fetchone()
        
        # Сигналы по временным периодам
        now = datetime.now(timezone.utc)
        periods = {
            "last_hour": now - timedelta(hours=1),
            "last_24h": now - timedelta(hours=24),
            "last_7d": now - timedelta(days=7),
            "last_30d": now - timedelta(days=30)
        }
        
        period_stats = {}
        for period_name, since_dt in periods.items():
            since_ms = int(since_dt.timestamp() * 1000)
            
            cur.execute("""
                SELECT COUNT(*) as count
                FROM signals_log
                WHERE user_id = %s 
                  AND bar_ts >= to_timestamp(%s/1000.0)
            """, (uid, since_ms))
            count = cur.fetchone()[0]
            period_stats[period_name] = count
        
        # Последние 10 сигналов с деталями
        cur.execute("""
            SELECT 
                id,
                symbol,
                timeframe,
                side,
                bar_ts,
                sent_at,
                entry,
                sl,
                tp1,
                tp2,
                outcome,
                outcome_v2,
                mode,
                trigger_type,
                EXTRACT(EPOCH FROM bar_ts)*1000 as bar_ts_ms,
                EXTRACT(EPOCH FROM sent_at)*1000 as sent_at_ms
            FROM signals_log
            WHERE user_id = %s
            ORDER BY bar_ts DESC
            LIMIT 10
        """, (uid,))
        
        recent_signals = []
        for row in cur.fetchall():
            recent_signals.append({
                "id": row[0],
                "symbol": row[1],
                "timeframe": row[2],
                "side": row[3],
                "bar_ts": row[4],
                "sent_at": row[5],
                "entry": float(row[6]) if row[6] else None,
                "sl": float(row[7]) if row[7] else None,
                "tp1": float(row[8]) if row[8] else None,
                "tp2": float(row[9]) if row[9] else None,
                "outcome": row[10],
                "outcome_v2": row[11],
                "mode": row[12],
                "trigger_type": row[13],
                "bar_ts_ms": int(row[14]) if row[14] else None,
                "sent_at_ms": int(row[15]) if row[15] else None
            })
        
        # Проверка на дубликаты (по уникальному индексу)
        cur.execute("""
            SELECT symbol, timeframe, bar_ts, COUNT(*) as dup_count
            FROM signals_log
            WHERE user_id = %s
            GROUP BY symbol, timeframe, bar_ts
            HAVING COUNT(*) > 1
        """, (uid,))
        duplicates = []
        for row in cur.fetchall():
            duplicates.append({
                "symbol": row[0],
                "timeframe": row[1],
                "bar_ts": row[2],
                "count": row[3]
            })
        
        # Проверка временных меток (bar_ts vs sent_at)
        cur.execute("""
            SELECT 
                COUNT(*) as count,
                COUNT(CASE WHEN bar_ts > sent_at THEN 1 END) as bar_after_sent,
                COUNT(CASE WHEN bar_ts < sent_at - INTERVAL '1 hour' THEN 1 END) as bar_too_old,
                COUNT(CASE WHEN bar_ts > NOW() THEN 1 END) as bar_in_future,
                COUNT(CASE WHEN sent_at > NOW() THEN 1 END) as sent_in_future
            FROM signals_log
            WHERE user_id = %s
        """, (uid,))
        timestamp_issues = cur.fetchone()
        
        # Проверка outcome_v2 (новый формат статистики)
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(outcome_v2) as with_outcome_v2,
                COUNT(outcome) as with_outcome,
                COUNT(CASE WHEN outcome_v2 IS NULL AND outcome IS NOT NULL THEN 1 END) as only_old_outcome
            FROM signals_log
            WHERE user_id = %s
        """, (uid,))
        outcome_stats = cur.fetchone()
        
        # Сигналы по символам
        cur.execute("""
            SELECT symbol, COUNT(*) as count
            FROM signals_log
            WHERE user_id = %s
            GROUP BY symbol
            ORDER BY count DESC
        """, (uid,))
        by_symbol = [{"symbol": row[0], "count": row[1]} for row in cur.fetchall()]
        
        # Сигналы по таймфреймам
        cur.execute("""
            SELECT timeframe, COUNT(*) as count
            FROM signals_log
            WHERE user_id = %s
            GROUP BY timeframe
            ORDER BY count DESC
        """, (uid,))
        by_timeframe = [{"timeframe": row[0], "count": row[1]} for row in cur.fetchall()]
        
        return {
            "user": user_info,
            "total_signals": total_stats[0],
            "first_signal_ts": total_stats[1],
            "last_signal_ts": total_stats[2],
            "unique_symbols": total_stats[3],
            "unique_timeframes": total_stats[4],
            "period_stats": period_stats,
            "recent_signals": recent_signals,
            "duplicates": duplicates,
            "timestamp_issues": {
                "total": timestamp_issues[0],
                "bar_after_sent": timestamp_issues[1],
                "bar_too_old": timestamp_issues[2],
                "bar_in_future": timestamp_issues[3],
                "sent_in_future": timestamp_issues[4]
            },
            "outcome_stats": {
                "total": outcome_stats[0],
                "with_outcome_v2": outcome_stats[1],
                "with_outcome": outcome_stats[2],
                "only_old_outcome": outcome_stats[3]
            },
            "by_symbol": by_symbol,
            "by_timeframe": by_timeframe
        }


def diagnose_all_users() -> List[Dict[str, Any]]:
    """Диагностика всех пользователей"""
    if not db_enabled():
        return [{"error": "Database not enabled"}]
    
    users = db_list_users()
    results = []
    
    for uid in users:
        try:
            result = diagnose_user_signals(uid)
            results.append(result)
        except Exception as e:
            results.append({
                "user_id": uid,
                "error": str(e)
            })
    
    return results


def print_diagnosis(diagnosis: Dict[str, Any]):
    """Красивый вывод диагностики"""
    if "error" in diagnosis:
        print(f"❌ ERROR: {diagnosis['error']}")
        return
    
    user = diagnosis["user"]
    print("=" * 80)
    print(f"👤 USER: {user['username']} (ID: {user['user_id']})")
    print(f"   Created: {user['created_at']} | Timezone: {user['timezone']}")
    print("-" * 80)
    
    print(f"📊 TOTAL SIGNALS: {diagnosis['total_signals']}")
    if diagnosis['total_signals'] > 0:
        print(f"   First: {diagnosis['first_signal_ts']}")
        print(f"   Last:  {diagnosis['last_signal_ts']}")
        print(f"   Unique symbols: {diagnosis['unique_symbols']}")
        print(f"   Unique timeframes: {diagnosis['unique_timeframes']}")
    
    print("\n📅 PERIOD STATS:")
    for period, count in diagnosis["period_stats"].items():
        print(f"   {period:12s}: {count:4d} signals")
    
    # PROBLEMS CHECK
    problems = []
    
    if diagnosis["duplicates"]:
        problems.append(f"⚠️  {len(diagnosis['duplicates'])} duplicate groups found")
        print("\n⚠️  DUPLICATES FOUND:")
        for dup in diagnosis["duplicates"][:5]:  # Показываем первые 5
            print(f"   {dup['symbol']} {dup['timeframe']} {dup['bar_ts']} (x{dup['count']})")
    
    ts_issues = diagnosis["timestamp_issues"]
    if ts_issues["bar_after_sent"] > 0:
        problems.append(f"⚠️  {ts_issues['bar_after_sent']} signals with bar_ts > sent_at")
    if ts_issues["bar_too_old"] > 0:
        problems.append(f"⚠️  {ts_issues['bar_too_old']} signals with bar_ts too old")
    if ts_issues["bar_in_future"] > 0:
        problems.append(f"⚠️  {ts_issues['bar_in_future']} signals with bar_ts in future")
    if ts_issues["sent_in_future"] > 0:
        problems.append(f"⚠️  {ts_issues['sent_in_future']} signals with sent_at in future")
    
    outcome = diagnosis["outcome_stats"]
    if outcome["only_old_outcome"] > 0:
        ratio = outcome["only_old_outcome"] / outcome["total"] * 100 if outcome["total"] > 0 else 0
        if ratio > 10:
            problems.append(f"⚠️  {outcome['only_old_outcome']} signals ({ratio:.1f}%) without outcome_v2")
    
    if problems:
        print("\n🚨 PROBLEMS DETECTED:")
        for prob in problems:
            print(f"   {prob}")
    else:
        print("\n✅ No problems detected")
    
    print("\n📈 BY SYMBOL:")
    for item in diagnosis["by_symbol"][:10]:  # Top 10
        print(f"   {item['symbol']:15s}: {item['count']:4d} signals")
    
    print("\n⏰ BY TIMEFRAME:")
    for item in diagnosis["by_timeframe"]:
        print(f"   {item['timeframe']:5s}: {item['count']:4d} signals")
    
    print("\n🔍 RECENT SIGNALS (last 10):")
    for sig in diagnosis["recent_signals"][:5]:  # Показываем первые 5
        bar_dt = sig["bar_ts"].strftime("%Y-%m-%d %H:%M:%S") if sig["bar_ts"] else "N/A"
        print(f"   ID {sig['id']:6d} | {sig['symbol']:10s} {sig['timeframe']:3s} {sig['side']:5s} | {bar_dt}")
        print(f"              Entry: {sig['entry']:.4f} | SL: {sig['sl']:.4f} | TP1: {sig['tp1']:.4f} | TP2: {sig['tp2']:.4f}")
        if sig['outcome_v2']:
            print(f"              Outcome_v2: {sig['outcome_v2']}")
        elif sig['outcome']:
            print(f"              Outcome: {sig['outcome']}")
        print()
    
    print("=" * 80)
    print()


def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Diagnose signal statistics")
    parser.add_argument("--user", "-u", type=int, help="User ID to diagnose (or all if not specified)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    args = parser.parse_args()
    
    if not db_enabled():
        print("❌ Database not enabled (DATABASE_URL not set)")
        return 1
    
    if args.user:
        # Single user
        diagnosis = diagnose_user_signals(args.user)
        
        if args.json:
            import json
            print(json.dumps(diagnosis, indent=2, default=str))
        else:
            print_diagnosis(diagnosis)
    else:
        # All users
        results = diagnose_all_users()
        
        if args.json:
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            for diagnosis in results:
                if "error" in diagnosis and "user_id" not in diagnosis.get("user", {}):
                    continue  # Skip db connection error
                print_diagnosis(diagnosis)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

