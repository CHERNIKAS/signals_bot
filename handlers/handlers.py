#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Telegram handlers: команды, коллбэки, обработка сообщений"""

import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Импорты из других модулей
from config import CONFIG
from utils import utcnow, normalize_tf, _get_message, _fmt_price, format_qty, _get_user_symbols_cached
from database import (
    db_enabled, _db_conn, db_get_or_create_user, db_set_running, db_is_running,
    db_session_is_open, db_user_pairs, db_user_tfs_analysis, db_user_tfs_signal,
    db_get_settings, db_add_pair, db_rm_pair, db_toggle_pair, db_user_tfs,
    db_add_tf, db_rm_tf, db_set_tf_mode, db_toggle_tf_analysis, db_toggle_tf_signal,
    db_get_signals_with_events, db_update_signal_events, db_get_signals, db_clear_signals,
    db_session_start, db_session_end, db_get_user_timezone, db_set_user_timezone,
    db_get_tp_mid_config, db_set_tp_mid_config,
)
from keyboards import (
    BTN_SESS_START, BTN_SESS_END, BTN_PAIR_TOGGLE, BTN_TF_SETTINGS,
    BTN_MY_DATA, BTN_LEVELS, BTN_STATS, BTN_TRADES, BTN_U_STATS, BTN_ADMIN,
    BTN_TIMEZONE, BTN_STATS_D, BTN_STATS_W, BTN_STATS_M, BTN_STATS_BY_SYMBOL,
    BTN_STATS_BY_TF, BTN_STATS_BY_SESSION, BTN_CLEAR_STATS, BTN_STATS_CSV,
    BTN_CLEAR_DAY, BTN_CLEAR_WEEK, BTN_CLEAR_MONTH, BTN_TF_ANALYSIS,
    BTN_TF_SIGNAL, BTN_TF_MODE, BTN_TF_DONE, BTN_BACK, BTN_NOTIF_STATS,
    BTN_NOTIF_DRAWDOWN, BTN_NOTIF_WEEKLY,
    BTN_TPMID_MENU, BTN_TPMID_TOGGLE, BTN_TPMID_AUTO_TOGGLE, BTN_TPMID_SETTINGS, BTN_TPMID_RESET,
    ADMIN_USER_IDS, _kb_main, _kb_stats, _kb_clear_stats, _kb_timezone, _kb_levels_filters, _kb_tp_mid,
)
from jobs import tick_once, send_telegram
from utils import normalize_symbol
import pytz
import io
import csv
import psycopg2.extras

# Admin user IDs (можно вынести в конфиг)



# =============================================================================
# HELPER FUNCTIONS FOR STATISTICS
# =============================================================================

def _parse_tp_mid_delta(text: str) -> Optional[float]:
    """Парсинг строки вида +0.1, -0,1, +0.1% и т.п. Возвращает float процентов (0.1 -> +0.1%).
    Требуется обязательный знак + или -.
    """
    if not text:
        return None
    s = text.strip().replace('%', '').replace(',', '.')
    if not (s.startswith('+') or s.startswith('-')):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _check_price_level_hit(sig: dict, target_price: float) -> Optional[int]:
    """Проверить по свечам достижение произвольного уровня цены. Возвращает ts(ms) или None.
    Не пишет в БД. Использует те же данные, что и _resolve_all_events_for_signal.
    """
    try:
        from binance import binance_klines
        from datetime import datetime, timezone
        symbol = sig.get('symbol')
        timeframe = sig.get('timeframe')
        side = str(sig.get('side', 'LONG')).upper()
        bar_ts = int(sig.get('bar_ts', 0)) if sig.get('bar_ts') else 0
        if not (symbol and timeframe and side and bar_ts and target_price):
            return None
        klines = binance_klines(symbol, timeframe, limit=500)
        if klines is None or len(klines) == 0:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if bar_ts > now_ms:
            bar_ts = bar_ts // 1000
        target = float(target_price)
        hit_ts = None
        
        # Обрабатываем DataFrame или список
        if hasattr(klines, 'iterrows'):
            # DataFrame
            for idx, row in klines.iterrows():
                # Пытаемся использовать именованные колонки, иначе позиционный доступ через iloc
                if 'ts' in row.index:
                    ts = int(row['ts'])
                    high = float(row.get('high', 0))
                    low = float(row.get('low', 0))
                else:
                    # Позиционный доступ через iloc для совместимости
                    ts = int(row.iloc[0] if len(row) > 0 else 0)
                    high = float(row.iloc[2] if len(row) > 2 else 0)
                    low = float(row.iloc[3] if len(row) > 3 else 0)
                
                if ts < bar_ts:
                    continue
                
                if side == 'LONG':
                    if high >= target:
                        hit_ts = ts
                        break
                else:
                    if low <= target:
                        hit_ts = ts
                        break
        else:
            # Список
            for k in klines:
                ts = int(k[0])
                if ts < bar_ts:
                    continue
                high = float(k[2])
                low = float(k[3])
                if side == 'LONG':
                    if high >= target:
                        hit_ts = ts
                        break
                else:
                    if low <= target:
                        hit_ts = ts
                        break
        return hit_ts
    except Exception as e:
        print(f"[CHECK_PRICE_LEVEL] Error: {e}")
        return None

def _check_tp1_return_after_tpmid(sig: dict, tp1_price: float, tpmid_ts: int) -> Optional[int]:
    """Проверяет, вернулась ли цена к TP1 после достижения TPmid.
    Возвращает timestamp возврата к TP1 или None.
    """
    try:
        from binance import binance_klines
        from datetime import datetime, timezone
        symbol = sig.get('symbol')
        timeframe = sig.get('timeframe')
        side = str(sig.get('side', 'LONG')).upper()
        bar_ts = int(sig.get('bar_ts', 0)) if sig.get('bar_ts') else 0
        
        if not (symbol and timeframe and side and bar_ts and tp1_price and tpmid_ts):
            return None
        
        klines = binance_klines(symbol, timeframe, limit=500)
        if klines is None or len(klines) == 0:
            return None
        
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if bar_ts > now_ms:
            bar_ts = bar_ts // 1000
        
        tp1 = float(tp1_price)
        return_ts = None
        
        # Обрабатываем DataFrame или список
        if hasattr(klines, 'iterrows'):
            # DataFrame
            for idx, row in klines.iterrows():
                ts = int(row.get('ts', row.get(0, 0)))
                if ts <= tpmid_ts:  # Только после TPmid
                    continue
                high = float(row.get('high', row.get(2, 0)))
                low = float(row.get('low', row.get(3, 0)))
                
                if side == 'LONG':
                    # Для LONG: цена вернулась к TP1, если low <= tp1 (цена упала до TP1)
                    if low <= tp1:
                        return_ts = ts
                        break
                else:  # SHORT
                    # Для SHORT: цена вернулась к TP1, если high >= tp1 (цена поднялась до TP1)
                    if high >= tp1:
                        return_ts = ts
                        break
        else:
            # Список
            for k in klines:
                ts = int(k[0])
                if ts <= tpmid_ts:  # Только после TPmid
                    continue
                high = float(k[2])
                low = float(k[3])
                
                if side == 'LONG':
                    # Для LONG: цена вернулась к TP1, если low <= tp1 (цена упала до TP1)
                    if low <= tp1:
                        return_ts = ts
                        break
                else:  # SHORT
                    # Для SHORT: цена вернулась к TP1, если high >= tp1 (цена поднялась до TP1)
                    if high >= tp1:
                        return_ts = ts
                        break
        
        return return_ts
    except Exception as e:
        print(f"[CHECK_TP1_RETURN] Error: {e}")
        import traceback
        traceback.print_exc()
        return None

def _resolve_all_events_for_signal(sig: dict, tp_mid_config: Optional[dict] = None) -> dict:
    """
    Проверяет исторические данные и определяет события TP1/TP2/SL для сигнала.
    Возвращает словарь с hit_tp1_price, hit_tp1_ts, hit_tp2_price, hit_tp2_ts, hit_sl_price, hit_sl_ts.
    
    Args:
        sig: Словарь с данными сигнала
        tp_mid_config: Опциональная конфигурация TPmid (не используется в текущей реализации, но принимается для совместимости)
    """
    # Если события уже есть - возвращаем их (конвертируем Decimal в int/float)
    if sig.get("hit_tp1_ts") or sig.get("hit_tp2_ts") or sig.get("hit_sl_ts"):
        result = {
            "hit_tp1_price": float(sig.get("hit_tp1_price")) if sig.get("hit_tp1_price") else None,
            "hit_tp1_ts": int(sig.get("hit_tp1_ts")) if sig.get("hit_tp1_ts") else None,
            "hit_tp2_price": float(sig.get("hit_tp2_price")) if sig.get("hit_tp2_price") else None,
            "hit_tp2_ts": int(sig.get("hit_tp2_ts")) if sig.get("hit_tp2_ts") else None,
            "hit_sl_price": float(sig.get("hit_sl_price")) if sig.get("hit_sl_price") else None,
            "hit_sl_ts": int(sig.get("hit_sl_ts")) if sig.get("hit_sl_ts") else None,
        }
        # Добавляем TPmid если есть
        if sig.get("hit_tp_mid_ts"):
            result["hit_tp_mid_price"] = float(sig.get("hit_tp_mid_price")) if sig.get("hit_tp_mid_price") else None
            result["hit_tp_mid_ts"] = int(sig.get("hit_tp_mid_ts")) if sig.get("hit_tp_mid_ts") else None
        return result
    
    # Проверяем исторические данные
    try:
        from binance import binance_klines
        from datetime import datetime, timezone
        
        symbol = sig.get("symbol")
        timeframe = sig.get("timeframe")
        side = sig.get("side")
        entry = float(sig.get("entry", 0))
        tp1 = float(sig.get("tp1", 0))
        tp2 = float(sig.get("tp2", 0))
        sl = float(sig.get("sl", 0))
        bar_ts = int(sig.get("bar_ts", 0)) if sig.get("bar_ts") else 0  # Convert Decimal to int
        
        if not all([symbol, timeframe, side, entry, tp1, tp2, sl, bar_ts]):
            return {
                "hit_tp1_price": None,
                "hit_tp1_ts": None,
                "hit_tp2_price": None,
                "hit_tp2_ts": None,
                "hit_sl_price": None,
                "hit_sl_ts": None,
            }
        
        # Загружаем свечи
        klines = binance_klines(symbol, timeframe, limit=500)
        if klines is None or len(klines) == 0:
            return {
                "hit_tp1_price": None,
                "hit_tp1_ts": None,
                "hit_tp2_price": None,
                "hit_tp2_ts": None,
                "hit_sl_price": None,
                "hit_sl_ts": None,
            }
        
        # Фильтруем свечи после сигнала
        # Если bar_ts больше текущего времени - значит это старая запись в неправильном формате
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        
        print(f"[RESOLVE] bar_ts={bar_ts} ({datetime.fromtimestamp(bar_ts/1000, tz=timezone.utc).isoformat()})")
        print(f"[RESOLVE] now_ms={now_ms} ({datetime.fromtimestamp(now_ms/1000, tz=timezone.utc).isoformat()})")
        
        if bar_ts > now_ms:
            # Старая запись - делим на 1000
            print(f"[RESOLVE] FIXING: bar_ts > now_ms, dividing by 1000")
            bar_ts = bar_ts // 1000
        
        print(f"[RESOLVE] Total klines before filter: {len(klines)}")
        if "ts" in klines.columns:
            print(f"[RESOLVE] Klines date range: {klines['ts'].min()} to {klines['ts'].max()}")
            klines = klines[klines["ts"] >= bar_ts]
        else:
            print(f"[RESOLVE] WARNING: 'ts' column not found! Using index instead")
            print(f"[RESOLVE] Klines date range: {klines.index.min()} to {klines.index.max()}")
            klines = klines[klines.index >= bar_ts]
        
        print(f"[RESOLVE] Klines after filter (>= bar_ts): {len(klines)}")
        
        if len(klines) == 0:
            # Нет свечей после сигнала - сигнал еще открыт
            print(f"[RESOLVE] NO KLINES AFTER SIGNAL - returning OPEN")
            return {
                "hit_tp1_price": None,
                "hit_tp1_ts": None,
                "hit_tp2_price": None,
                "hit_tp2_ts": None,
                "hit_sl_price": None,
                "hit_sl_ts": None,
            }
        
        hit_tp1_price = None
        hit_tp1_ts = None
        hit_tp2_price = None
        hit_tp2_ts = None
        hit_sl_price = None
        hit_sl_ts = None
        
        # Проверяем каждую свечу
        checked_count = 0
        for idx, row in klines.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            ts = int(row["ts"])  # Берём timestamp из колонки, НЕ из индекса!
            
            checked_count += 1
            if checked_count <= 5:  # Логируем первые 5 свечей
                ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                print(f"[RESOLVE] Candle #{checked_count}: {ts_str} | HIGH={high} LOW={low}")
            
            if side == "LONG":
                # Для LONG: проверяем достижение уровней снизу вверх
                if hit_sl_ts is None and low <= sl:
                    hit_sl_price = sl
                    hit_sl_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🔴 SL HIT! {ts_str} | LOW={low} <= SL={sl}")
                
                if hit_tp1_ts is None and high >= tp1:
                    hit_tp1_price = tp1
                    hit_tp1_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🟡 TP1 HIT! {ts_str} | HIGH={high} >= TP1={tp1}")
                
                if hit_tp2_ts is None and high >= tp2:
                    hit_tp2_price = tp2
                    hit_tp2_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🟢 TP2 HIT! {ts_str} | HIGH={high} >= TP2={tp2}")
            else:  # SHORT
                # Для SHORT: проверяем достижение уровней сверху вниз
                if hit_sl_ts is None and high >= sl:
                    hit_sl_price = sl
                    hit_sl_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🔴 SL HIT! {ts_str} | HIGH={high} >= SL={sl}")
                
                if hit_tp1_ts is None and low <= tp1:
                    hit_tp1_price = tp1
                    hit_tp1_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🟡 TP1 HIT! {ts_str} | LOW={low} <= TP1={tp1}")
                
                if hit_tp2_ts is None and low <= tp2:
                    hit_tp2_price = tp2
                    hit_tp2_ts = ts
                    ts_str = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat()
                    print(f"[RESOLVE] 🟢 TP2 HIT! {ts_str} | LOW={low} <= TP2={tp2}")
        
        events = {
            "hit_tp1_price": hit_tp1_price,
            "hit_tp1_ts": hit_tp1_ts,
            "hit_tp2_price": hit_tp2_price,
            "hit_tp2_ts": hit_tp2_ts,
            "hit_sl_price": hit_sl_price,
            "hit_sl_ts": hit_sl_ts,
        }
        
        print(f"[RESOLVE] Checked {checked_count} candles")
        print(f"[RESOLVE] Results: TP1={bool(hit_tp1_ts)} TP2={bool(hit_tp2_ts)} SL={bool(hit_sl_ts)}")
        
        # Сохраняем события в БД если хоть что-то найдено
        if any([hit_tp1_ts, hit_tp2_ts, hit_sl_ts]):
            sig_id = sig.get("id")
            if sig_id:
                try:
                    db_update_signal_events(sig_id, events)
                    print(f"[RESOLVE] Saved events to DB for signal {sig_id}")
                    
                    # Вычисляем и сохраняем outcome_v2
                    outcome_v2 = _calculate_outcome_v2(sig, events)
                    if outcome_v2:
                        from database import db_update_signal_outcome
                        db_update_signal_outcome(sig_id, outcome_v2)
                        print(f"[RESOLVE] Saved outcome_v2 for signal {sig_id}: {outcome_v2.get('status')}")
                except Exception as e:
                    print(f"[WARN] Failed to save events/outcome for signal {sig_id}: {e}")
        
        return events
    
    except Exception as e:
        print(f"[WARN] Failed to resolve events for signal {sig.get('id')}: {e}")
        return {
            "hit_tp1_price": None,
            "hit_tp1_ts": None,
            "hit_tp2_price": None,
            "hit_tp2_ts": None,
            "hit_sl_price": None,
            "hit_sl_ts": None,
        }


def _calculate_outcome_v2(sig: dict, events: dict) -> dict:
    """
    Вычисляет outcome_v2 для сигнала на основе событий.
    
    Returns:
        {"status": "TP1+TP2"|"TP1→SL"|"SL"|"TP1→OPEN"|"OPEN", "r": float, "time_to_tp1_min": int}
    """
    tp1_ts = events.get("hit_tp1_ts")
    tp2_ts = events.get("hit_tp2_ts")
    sl_ts = events.get("hit_sl_ts")
    bar_ts = int(sig.get("bar_ts", 0)) if sig.get("bar_ts") else 0
    
    # Конвертируем в bool (могут быть 0 или None)
    has_tp1 = bool(tp1_ts)
    has_tp2 = bool(tp2_ts)
    has_sl = bool(sl_ts)
    
    # Определяем status
    if has_sl and not has_tp1:
        status = "SL"
    elif has_tp1 and has_tp2:
        status = "TP1+TP2"
    elif has_tp1 and has_sl:
        # Проверяем какой раньше
        try:
            if int(sl_ts) < int(tp1_ts):
                status = "SL"
            else:
                status = "TP1→SL"
        except:
            status = "TP1→SL"
    elif has_tp1 and not has_tp2 and not has_sl:
        status = "TP1→OPEN"
    else:
        # Ничего не достигнуто - еще открыт
        return None
    
    # Вычисляем r (risk-reward ratio)
    try:
        entry = float(sig.get("entry", 0))
        tp1 = float(sig.get("tp1", 0))
        tp2 = float(sig.get("tp2", 0))
        sl = float(sig.get("sl", 0))
        side = sig.get("side", "")
        
        risk = abs(entry - sl)
        if risk == 0:
            r = 0.0
        elif status == "SL":
            r = -1.0
        elif status == "TP1+TP2":
            # Учитываем частичное закрытие на TP1
            from config import CONFIG
            tp1_close_share = CONFIG.get("position", {}).get("tp1_close_share", 0.7)
            tp1_profit = abs(tp1 - entry) / risk
            tp2_profit = abs(tp2 - entry) / risk
            r = tp1_profit * tp1_close_share + tp2_profit * (1 - tp1_close_share)
        elif status == "TP1→SL":
            # Смешанный результат
            from config import CONFIG
            tp1_close_share = CONFIG.get("position", {}).get("tp1_close_share", 0.7)
            tp1_profit = abs(tp1 - entry) / risk
            r = tp1_profit * tp1_close_share - 1.0 * (1 - tp1_close_share)
        elif status == "TP1→OPEN":
            # Только TP1
            r = abs(tp1 - entry) / risk
        else:
            r = 0.0
    except Exception as e:
        print(f"[WARN] Failed to calculate r for signal {sig.get('id')}: {e}")
        r = 0.0
    
    # Вычисляем time_to_tp1_min
    time_to_tp1_min = None
    if has_tp1 and bar_ts:
        try:
            delta_ms = int(tp1_ts) - int(bar_ts)
            time_to_tp1_min = max(1, int(delta_ms / 1000 / 60))
        except Exception as e:
            print(f"[WARN] Failed to calculate time_to_tp1_min for signal {sig.get('id')}: {e}")
    
    outcome = {
        "status": status,
        "r": round(r, 2),
    }
    
    if time_to_tp1_min:
        outcome["time_to_tp1_min"] = time_to_tp1_min
    
    return outcome


def _determine_signal_path(sig: dict, tp_mid_config: Optional[dict] = None) -> str:
    """
    Определяет путь исхода сигнала на основе зафиксированных событий.
    Возвращает: 'SL_BEFORE_TP1', 'TP1_PLUS_TP2', 'TP1_TO_SL', 'TP1_TO_OPEN', 'TP1_TO_TPMID', 'TPMID_TO_TP1_BE', 'OPEN'
    """
    hit_tp1 = bool(sig.get("hit_tp1_ts"))
    hit_tp2 = bool(sig.get("hit_tp2_ts"))
    hit_sl = bool(sig.get("hit_sl_ts"))
    hit_tpmid = bool(sig.get("hit_tp_mid_ts")) if tp_mid_config and tp_mid_config.get('enabled') else False
    
    # Если ничего не достигнуто
    if not hit_tp1 and not hit_sl:
        return "OPEN"
    
    # Если SL раньше TP1
    if hit_sl and not hit_tp1:
        return "SL_BEFORE_TP1"
    
    # Если достигли и TP1 и TP2
    if hit_tp1 and hit_tp2:
        # Если TPmid включен, проверяем, был ли он достигнут между TP1 и TP2
        if hit_tpmid:
            tp1_ts = sig.get("hit_tp1_ts", 0)
            tp2_ts = sig.get("hit_tp2_ts", 0)
            tpmid_ts = sig.get("hit_tp_mid_ts", 0)
            if tp1_ts and tp2_ts and tpmid_ts:
                tp1_ts_int = int(tp1_ts)
                tp2_ts_int = int(tp2_ts)
                tpmid_ts_int = int(tpmid_ts)
                # Если TPmid был достигнут между TP1 и TP2, это TP1+TPmid+TP2
                if tp1_ts_int <= tpmid_ts_int <= tp2_ts_int:
                    return "TP1_PLUS_TP2"  # Путь остается тот же, но название будет изменено при отображении
        # Если TPmid не включен или не был достигнут, это обычный TP1+TP2
        return "TP1_PLUS_TP2"
    
    # Если TP1 и SL (но не TP2)
    if hit_tp1 and hit_sl and not hit_tp2:
        tp1_ts = sig.get("hit_tp1_ts", 0)
        sl_ts = sig.get("hit_sl_ts", 0)
        if sl_ts <= tp1_ts:  # SL до или вместо с TP1
            return "SL_BEFORE_TP1"
        else:  # TP1, затем SL
            # Проверяем, был ли TPmid между TP1 и SL
            if hit_tpmid:
                tpmid_ts = sig.get("hit_tp_mid_ts", 0)
                # Проверяем, что TPmid был достигнут между TP1 и SL
                if tp1_ts and tpmid_ts and sl_ts and int(tp1_ts) <= int(tpmid_ts) <= int(sl_ts):  # TP1 → TPmid → SL
                    # НО: если после TPmid цена вернулась к TP1, это BE, а не SL
                    tp1_price = float(sig.get("tp1", 0))
                    if tp1_price > 0:
                        tp1_return_ts = _check_tp1_return_after_tpmid(sig, tp1_price, int(tpmid_ts))
                        if tp1_return_ts and int(tp1_return_ts) < int(sl_ts):  # Вернулся к TP1 раньше SL
                            return "TPMID_TO_TP1_BE"
                    return "TP1_TO_SL"  # Не вернулся к TP1, значит SL
            return "TP1_TO_SL"
    
    # Если только TP1 (без TP2 и без SL)
    if hit_tp1 and not hit_tp2 and not hit_sl:
        # Проверяем, был ли достигнут TPmid после TP1
        if hit_tpmid:
            tp1_ts = sig.get("hit_tp1_ts", 0)
            tpmid_ts = sig.get("hit_tp_mid_ts", 0)
            # Проверяем, что TPmid был достигнут после TP1
            if tp1_ts and tpmid_ts and int(tpmid_ts) >= int(tp1_ts):  # TP1 → TPmid (но не TP2 и не SL)
                # Проверяем, вернулась ли цена к TP1 после TPmid
                tp1_price = float(sig.get("tp1", 0))
                if tp1_price > 0:
                    tp1_return_ts = _check_tp1_return_after_tpmid(sig, tp1_price, int(tpmid_ts))
                    if tp1_return_ts:  # Вернулся к TP1
                        return "TPMID_TO_TP1_BE"
                    # Не вернулся к TP1, значит открыт с TPmid
                    return "TP1_TO_TPMID"
                else:
                    # Нет цены TP1, не можем проверить возврат - считаем как открыт с TPmid
                    return "TP1_TO_TPMID"
            # TPmid был достигнут до TP1 или нет timestamp - это странно, но считаем как обычный TP1→OPEN
            return "TP1_TO_OPEN"
        # TPmid не достигнут - обычный TP1→OPEN
        return "TP1_TO_OPEN"
    
    return "OPEN"


def _calculate_outcome_pct(sig: dict, path: str) -> float | None:
    """
    Рассчитывает процент результата для закрытых путей.
    Для TP1_TO_OPEN, TP1_TO_TPMID, OPEN возвращает None.
    """
    if path == "TP1_TO_OPEN" or path == "TP1_TO_TPMID" or path == "OPEN":
        return None
    
    side = sig.get("side", "LONG")
    entry = float(sig.get("entry", 0))
    
    if entry == 0:
        return None
    
    if path == "SL_BEFORE_TP1":
        sl = float(sig.get("sl", 0))
        if side == "LONG":
            return (sl - entry) / entry * 100
        else:
            return (entry - sl) / entry * 100
    
    elif path == "TP1_PLUS_TP2":
        tp2 = float(sig.get("tp2", 0))
        if side == "LONG":
            return (tp2 - entry) / entry * 100
        else:
            return (entry - tp2) / entry * 100
    
    elif path == "TP1_TO_SL":
        # Смешанный результат: часть позиции закрыта на TP1, часть на SL
        from config import CONFIG
        tp1_share = CONFIG.get("position", {}).get("tp1_close_share", 0.5)
        
        tp1 = float(sig.get("tp1", 0))
        sl = float(sig.get("sl", 0))
        
        if side == "LONG":
            tp1_pct = (tp1 - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
        else:
            tp1_pct = (entry - tp1) / entry * 100
            sl_pct = (entry - sl) / entry * 100
        
        # Взвешенный результат
        return tp1_share * tp1_pct + (1 - tp1_share) * sl_pct
    
    elif path == "TPMID_TO_TP1_BE":
        # Безубыток: цена вернулась к TP1 после TPmid, результат = 0%
        return 0.0
    
    return None


def _calculate_stats_metrics(signals: list[dict], tp_mid_config: Optional[dict] = None) -> dict:
    """
    Рассчитывает все метрики для статистики сигналов.
    Возвращает словарь с метриками.
    
    ВАЖНО: "Рассчитано исходов" (closed) = только финальные пути (n1+n2+n3+n_tpmid_be).
    TP1→OPEN и TP1→TPMID относятся к "Открыто", т.к. это промежуточное состояние.
    """
    # Подсчитываем пути
    paths_count = {"SL_BEFORE_TP1": 0, "TP1_PLUS_TP2": 0, "TP1_TO_SL": 0, "TP1_TO_OPEN": 0, "TP1_TO_TPMID": 0, "TPMID_TO_TP1_BE": 0, "OPEN": 0}
    outcome_pcts = []
    
    # Счетчики касаний (независимо от финального исхода)
    tp1_touch_cnt = 0  # Все, кто коснулся TP1
    tp1_tpmid_touch_cnt = 0  # Все, кто коснулся TP1 и TPmid
    n_sl_before_tpmid = 0
    n_tpmid_to_sl = 0
    tp_mid_enabled = bool(tp_mid_config and tp_mid_config.get('enabled'))
    
    for sig in signals:
        path = _determine_signal_path(sig, tp_mid_config)
        paths_count[path] += 1
        
        # Счетчики касаний
        if sig.get("hit_tp1_ts"):
            tp1_touch_cnt += 1
            # Если также коснулся TPmid ПОСЛЕ TP1 (TPmid не может быть раньше TP1)
            # Считаем только сигналы, которые после TP1 дотронулись именно TPmid, а не ушли в SL или остались OPEN без TPmid
            if tp_mid_config and tp_mid_config.get('enabled') and sig.get("hit_tp_mid_ts"):
                tp1_ts = sig.get("hit_tp1_ts")
                tpmid_ts = sig.get("hit_tp_mid_ts")
                # Проверяем, что TPmid был достигнут после TP1
                if tp1_ts and tpmid_ts and int(tpmid_ts) >= int(tp1_ts):
                    # Проверяем путь: после TP1 должен быть TPmid, а не сразу SL или OPEN без TPmid
                    # Пути, где после TP1 есть TPmid: TP1_PLUS_TP2 (с TPmid), TPMID_TO_TP1_BE, TP1_TO_TPMID
                    # НЕ включаем: TP1_TO_SL (даже если там был TPmid, после TPmid не может быть SL)
                    # НЕ включаем: TP1_TO_OPEN (без TPmid после TP1)
                    if path in ("TP1_PLUS_TP2", "TPMID_TO_TP1_BE", "TP1_TO_TPMID"):
                        # Для TP1_PLUS_TP2 проверяем, что TPmid был между TP1 и TP2
                        if path == "TP1_PLUS_TP2":
                            tp2_ts = sig.get("hit_tp2_ts")
                            if tp2_ts and int(tp1_ts) <= int(tpmid_ts) <= int(tp2_ts):
                                tp1_tpmid_touch_cnt += 1
                        else:
                            # TPMID_TO_TP1_BE и TP1_TO_TPMID - точно после TP1 был TPmid
                            tp1_tpmid_touch_cnt += 1
        
        # Собираем outcome_pct для AVERAGE (только для закрытых)
        pct = _calculate_outcome_pct(sig, path)
        if pct is not None and path in ("SL_BEFORE_TP1", "TP1_PLUS_TP2", "TP1_TO_SL", "TPMID_TO_TP1_BE"):
            outcome_pcts.append(pct)

        # Подсчитываем SL-пути относительно TPmid
        if path in ("SL_BEFORE_TP1", "TP1_TO_SL"):
            sl_ts = sig.get("hit_sl_ts")
            tpmid_ts = sig.get("hit_tp_mid_ts")
            if tp_mid_enabled and tpmid_ts and sl_ts and int(tpmid_ts) <= int(sl_ts):
                n_tpmid_to_sl += 1
            else:
                n_sl_before_tpmid += 1
    
    n1 = paths_count["SL_BEFORE_TP1"]
    n2 = paths_count["TP1_PLUS_TP2"]
    n3 = paths_count["TP1_TO_SL"]
    n4 = paths_count["TP1_TO_OPEN"]
    n_tpmid_open = paths_count["TP1_TO_TPMID"]  # TP1 → TPmid → OPEN
    n_tpmid_be = paths_count["TPMID_TO_TP1_BE"]  # TP1 → TPmid → TP1 (BE)
    n_open = paths_count["OPEN"]
    
    # Проверяем, сколько сигналов с TP2 прошли через TPmid
    n2_with_tpmid = 0
    if tp_mid_config and tp_mid_config.get('enabled'):
        from handlers.message_render import _tp_mid_price_for_signal
        for sig in signals:
            if _determine_signal_path(sig, tp_mid_config) == "TP1_PLUS_TP2":
                # Проверяем, был ли достигнут TPmid между TP1 и TP2
                tp1_ts = sig.get("hit_tp1_ts")
                tp2_ts = sig.get("hit_tp2_ts")
                tpmid_ts = sig.get("hit_tp_mid_ts")
                
                # Если есть timestamp TPmid, проверяем по времени
                if tp1_ts and tp2_ts and tpmid_ts:
                    tp1_ts_int = int(tp1_ts)
                    tp2_ts_int = int(tp2_ts)
                    tpmid_ts_int = int(tpmid_ts)
                    # TPmid должен быть между TP1 и TP2 по времени
                    if tp1_ts_int <= tpmid_ts_int <= tp2_ts_int:
                        n2_with_tpmid += 1
                # Если timestamp TPmid отсутствует, но TPmid включен и находится между TP1 и TP2 по цене,
                # то логически сигнал с TP2 должен был пройти через TPmid
                elif tp1_ts and tp2_ts:
                    try:
                        entry = float(sig.get('entry') or sig.get('entry_price') or 0.0)
                        tp1 = float(sig.get('tp1') or 0.0)
                        tp2 = float(sig.get('tp2') or 0.0)
                        side = str(sig.get('side', 'LONG')).upper()
                        mid_price = _tp_mid_price_for_signal(sig, tp_mid_config)
                        
                        if entry > 0 and tp1 > 0 and tp2 > 0 and mid_price:
                            # Проверяем, находится ли TPmid между TP1 и TP2 по цене
                            if side == 'LONG':
                                tp_mid_between = tp1 < mid_price < tp2
                            else:  # SHORT
                                tp_mid_between = tp2 < mid_price < tp1
                            
                            # Если TPmid между TP1 и TP2, то сигнал с TP2 должен был пройти через TPmid
                            if tp_mid_between:
                                n2_with_tpmid += 1
                    except Exception:
                        pass
    
    # ВАЖНО: закрыты только финальные пути
    closed = n1 + n2 + n3 + n_tpmid_be
    total = closed + n4 + n_tpmid_open + n_open
    
    # Проценты путей считаем от общего количества сигналов (total)
    def safe_pct(num, denom):
        return (num / denom * 100) if denom > 0 else 0.0
    
    pct1 = safe_pct(n1, total)
    pct2 = safe_pct(n2, total)
    pct3 = safe_pct(n3, total)
    # TP1→OPEN считаем также от общего количества
    pct4 = safe_pct(n4, total)
    
    # Winrate'ы по закрытым
    wr_final = safe_pct(n2, closed)                         # чистый винрейт финальных побед
    wr_weighted = safe_pct(1.0 * n2 + 0.5 * n3, closed)     # взвешенный (TP1→SL = 0.5)
    
    # TP1 Touch: используем счетчики касаний (независимо от финального исхода)
    tp1_touch_base = total  # Все сигналы
    wr_touch = safe_pct(tp1_touch_cnt, tp1_touch_base)
    
    # TP1+TPmid Touch: счетчик касаний TP1 и TPmid
    tp1_tpmid_touch_pct = safe_pct(tp1_tpmid_touch_cnt, total)
    
    # AVERAGE (без округления)
    avg_outcome = (sum(outcome_pcts) / len(outcome_pcts)) if outcome_pcts else 0.0
    
    # n_mid: все сигналы, которые коснулись TP1 и TPmid (независимо от финального исхода)
    n_mid = tp1_tpmid_touch_cnt
    pct_mid = tp1_tpmid_touch_pct
    
    # n_mid_sl: переименовываем в n_mid_be (TPmid → TP1 = BE)
    n_mid_be = n_tpmid_be
    pct_mid_be = safe_pct(n_mid_be, total)
    pct_sl_before_tpmid = safe_pct(n_sl_before_tpmid, total)
    pct_tpmid_to_sl = safe_pct(n_tpmid_to_sl, total)
    
    # Детальная разбивка сигналов с TPmid по финальным исходам
    # Считаем ТОЛЬКО сигналы, которые сначала коснулись TP1, затем TPmid (tpmid_ts >= tp1_ts)
    # После TPmid возможны только 3 исхода: TP2, BE, или OPEN
    n_mid_in_tp2 = 0  # TP1 → TPmid → TP2 (TP1+TPmid+TP2)
    n_mid_in_be = 0  # TP1 → TPmid → TP1 = BE
    n_mid_in_open = 0  # TP1 → TPmid → OPEN (не достигли ни TP2, ни вернулись к TP1)
    
    if tp_mid_config and tp_mid_config.get('enabled'):
        for sig in signals:
            # Проверяем, коснулся ли сигнал TP1 и TPmid
            if not (sig.get("hit_tp1_ts") and sig.get("hit_tp_mid_ts")):
                continue
            
            tp1_ts = sig.get("hit_tp1_ts")
            tpmid_ts = sig.get("hit_tp_mid_ts")
            
            if not (tp1_ts and tpmid_ts):
                continue
            
            tp1_ts_int = int(tp1_ts)
            tpmid_ts_int = int(tpmid_ts)
            
            # ВАЖНО: TPmid не может быть раньше TP1, но проверяем для безопасности
            if tpmid_ts_int < tp1_ts_int:
                continue  # Пропускаем некорректные данные (не должно быть, но на всякий случай)
            
            # Теперь распределяем по финальным исходам
            path = _determine_signal_path(sig, tp_mid_config)
            
            if path == "TP1_PLUS_TP2":
                # Проверяем, что TPmid был между TP1 и TP2 (это и есть TP1+TPmid+TP2)
                tp2_ts = sig.get("hit_tp2_ts")
                if tp2_ts:
                    tp2_ts_int = int(tp2_ts)
                    if tp1_ts_int <= tpmid_ts_int <= tp2_ts_int:
                        n_mid_in_tp2 += 1
            elif path == "TPMID_TO_TP1_BE":
                # Вернулись к TP1 после TPmid = BE
                n_mid_in_be += 1
            elif path == "TP1_TO_TPMID":
                # Достигли TPmid, но не достигли ни TP2, ни вернулись к TP1 = OPEN
                n_mid_in_open += 1
            # НЕ включаем:
            # - TP1_TO_SL (после TPmid не может быть SL, только BE или TP2 или OPEN)
            # - TP1_TO_OPEN (если TPmid был до TP1, мы уже пропустили выше)
            # - SL_BEFORE_TP1 (TPmid не был после TP1)
    
    return {
        "total": total,
        "resolved": closed,  # "Рассчитано исходов"
        "open": n_open + n4 + n_tpmid_open,  # "Открыто": обычные открытые + TP1→OPEN + TP1→TPmid→OPEN
        "n1": n1, "pct1": pct1,
        "n2": n2, "pct2": pct2,
        "n3": n3, "pct3": pct3,
        "n4": n4, "pct4": pct4,
        "n_tpmid_open": n_tpmid_open,  # TP1 → TPmid → OPEN
        "pct_tpmid_open": safe_pct(n_tpmid_open, total),
        "n_mid_be": n_mid_be,  # TPmid → TP1 = BE
        "pct_mid_be": pct_mid_be,
        "n_mid": n_mid,  # Все, кто коснулся TP1 и TPmid (счетчик касаний)
        "pct_mid": pct_mid,
        "n2_with_tpmid": n2_with_tpmid,  # Сигналы TP1+TP2, которые прошли через TPmid
        "n_sl_before_tpmid": n_sl_before_tpmid,
        "pct_sl_before_tpmid": pct_sl_before_tpmid,
        "n_tpmid_to_sl": n_tpmid_to_sl,
        "pct_tpmid_to_sl": pct_tpmid_to_sl,
        # Детальная разбивка сигналов с TPmid (только TP1 → TPmid → что-то)
        "n_mid_in_tp2": n_mid_in_tp2,  # TP1+TPmid+TP2
        "n_mid_in_be": n_mid_in_be,  # TPmid → TP1 = BE
        "n_mid_in_open": n_mid_in_open,  # TP1+TPmid → OPEN
        "wr_weighted": wr_weighted,
        "wr_touch": wr_touch,
        "wr_final": wr_final,
        "tp1_touch_cnt": tp1_touch_cnt,
        "tp1_touch_base": tp1_touch_base,
        "avg_outcome": avg_outcome,
        "avg_day_outcome": (sum(outcome_pcts) / total) if (outcome_pcts and total > 0) else 0.0
    }


def _determine_session_by_hour(hour_utc: int) -> str:
    """Определяет торговую сессию по часу UTC"""
    if 0 <= hour_utc < 8:
        return "asian"
    elif 8 <= hour_utc < 16:
        return "european"
    else:
        return "american"


def _add_session_to_signals(signals: list[dict], user_tz_name: str = 'UTC'):
    """Добавляет поле session к каждому сигналу"""
    from datetime import datetime
    
    try:
        user_tz = pytz.timezone(user_tz_name)
    except:
        user_tz = pytz.UTC
    
    for sig in signals:
        bar_ts = sig.get('bar_ts', 0)
        if not bar_ts:
            sig['session'] = 'UNKNOWN'
            continue
        # Перебиндовка по сессиям делается по ЛОКАЛЬНОМУ времени пользователя
        dt_utc = datetime.fromtimestamp(float(bar_ts) / 1000, tz=pytz.UTC)
        dt_local = dt_utc.astimezone(user_tz)
        hour_local = dt_local.hour
        sig['session'] = _determine_session_by_hour(hour_local)


def _calculate_group_stats(signals: list[dict], group_by: str, tp_mid_config: Optional[dict] = None) -> list[dict]:
    """Группирует сигналы и рассчитывает метрики для каждой группы"""
    from collections import defaultdict
    
    groups = defaultdict(list)
    for sig in signals:
        if group_by == 'symbol':
            key = sig.get('symbol', 'UNKNOWN')
        elif group_by == 'timeframe':
            key = sig.get('timeframe', 'UNKNOWN')
        elif group_by == 'session':
            key = sig.get('session', 'UNKNOWN')
        else:
            key = 'UNKNOWN'
        groups[key].append(sig)
    
    results = []
    for group_value, group_sigs in groups.items():
        if not group_sigs:
            continue
        
        paths_count = {"SL_BEFORE_TP1": 0, "TP1_PLUS_TP2": 0, "TP1_TO_SL": 0, "TP1_TO_OPEN": 0, "TP1_TO_TPMID": 0, "TPMID_TO_TP1_BE": 0, "OPEN": 0}
        outcome_pcts = []
        
        # Счетчики касаний (независимо от финального исхода)
        tp1_touch_cnt = 0  # Все, кто коснулся TP1
        tp1_tpmid_touch_cnt = 0  # Все, кто коснулся TP1 и TPmid
        n_sl_before_tpmid = 0
        n_tpmid_to_sl = 0
        tp_mid_enabled = bool(tp_mid_config and tp_mid_config.get('enabled'))
        
        for sig in group_sigs:
            path = _determine_signal_path(sig, tp_mid_config)
            paths_count[path] += 1
            
            # Счетчики касаний
            if sig.get("hit_tp1_ts"):
                tp1_touch_cnt += 1
                # Если также коснулся TPmid
                if tp_mid_config and tp_mid_config.get('enabled') and sig.get("hit_tp_mid_ts"):
                    tp1_tpmid_touch_cnt += 1
            
            pct = _calculate_outcome_pct(sig, path)
            if pct is not None:
                outcome_pcts.append(pct)
            
            if path in ("SL_BEFORE_TP1", "TP1_TO_SL"):
                sl_ts = sig.get("hit_sl_ts")
                tpmid_ts = sig.get("hit_tp_mid_ts")
                if tp_mid_enabled and tpmid_ts and sl_ts and int(tpmid_ts) <= int(sl_ts):
                    n_tpmid_to_sl += 1
                else:
                    n_sl_before_tpmid += 1
        
        n1 = paths_count["SL_BEFORE_TP1"]
        n2 = paths_count["TP1_PLUS_TP2"]
        n3 = paths_count["TP1_TO_SL"]
        n4 = paths_count["TP1_TO_OPEN"]
        n_tpmid_open = paths_count["TP1_TO_TPMID"]
        n_tpmid_be = paths_count["TPMID_TO_TP1_BE"]
        n_open = paths_count["OPEN"]
        
        # Проверяем, сколько сигналов с TP2 прошли через TPmid
        n2_with_tpmid = 0
        if tp_mid_config and tp_mid_config.get('enabled'):
            from handlers.message_render import _tp_mid_price_for_signal
            for sig in group_sigs:
                if _determine_signal_path(sig, tp_mid_config) == "TP1_PLUS_TP2":
                    # Проверяем, был ли достигнут TPmid между TP1 и TP2
                    tp1_ts = sig.get("hit_tp1_ts")
                    tp2_ts = sig.get("hit_tp2_ts")
                    tpmid_ts = sig.get("hit_tp_mid_ts")
                    
                    # Если есть timestamp TPmid, проверяем по времени
                    if tp1_ts and tp2_ts and tpmid_ts:
                        tp1_ts_int = int(tp1_ts)
                        tp2_ts_int = int(tp2_ts)
                        tpmid_ts_int = int(tpmid_ts)
                        # TPmid должен быть между TP1 и TP2 по времени
                        if tp1_ts_int <= tpmid_ts_int <= tp2_ts_int:
                            n2_with_tpmid += 1
                    # Если timestamp TPmid отсутствует, но TPmid включен и находится между TP1 и TP2 по цене,
                    # то логически сигнал с TP2 должен был пройти через TPmid
                    elif tp1_ts and tp2_ts:
                        try:
                            entry = float(sig.get('entry') or sig.get('entry_price') or 0.0)
                            tp1 = float(sig.get('tp1') or 0.0)
                            tp2 = float(sig.get('tp2') or 0.0)
                            side = str(sig.get('side', 'LONG')).upper()
                            mid_price = _tp_mid_price_for_signal(sig, tp_mid_config)
                            
                            if entry > 0 and tp1 > 0 and tp2 > 0 and mid_price:
                                # Проверяем, находится ли TPmid между TP1 и TP2 по цене
                                if side == 'LONG':
                                    tp_mid_between = tp1 < mid_price < tp2
                                else:  # SHORT
                                    tp_mid_between = tp2 < mid_price < tp1
                                
                                # Если TPmid между TP1 и TP2, то сигнал с TP2 должен был пройти через TPmid
                                if tp_mid_between:
                                    n2_with_tpmid += 1
                        except Exception:
                            pass
        
        # ВАЖНО: закрыты только финальные пути
        closed = n1 + n2 + n3 + n_tpmid_be
        total = closed + n4 + n_tpmid_open + n_open
        
        def safe_pct(num, denom):
            return round(num / denom * 100, 1) if denom > 0 else 0.0
        
        # Проценты путей считаем от общего количества сигналов (total)
        pct1 = safe_pct(n1, total)
        pct2 = safe_pct(n2, total)
        pct3 = safe_pct(n3, total)
        # TP1→OPEN считаем также от общего количества
        pct4 = safe_pct(n4, total)
        
        # Winrate'ы по закрытым
        wr_final = safe_pct(n2, closed)
        wr_weighted = safe_pct(1.0 * n2 + 0.5 * n3, closed)
        
        # TP1 Touch: используем счетчики касаний (независимо от финального исхода)
        tp1_touch_base = total  # Все сигналы
        wr_touch = safe_pct(tp1_touch_cnt, tp1_touch_base)
        
        # AVERAGE только для закрытых
        closed_outcome_pcts = []
        for sig in group_sigs:
            path = _determine_signal_path(sig, tp_mid_config)
            if path in ("SL_BEFORE_TP1", "TP1_PLUS_TP2", "TP1_TO_SL", "TPMID_TO_TP1_BE"):
                pct = _calculate_outcome_pct(sig, path)
                if pct is not None:
                    closed_outcome_pcts.append(pct)
        
        avg_outcome = (sum(closed_outcome_pcts) / len(closed_outcome_pcts)) if closed_outcome_pcts else 0.0
        
        # TPmid метрики: счетчики касаний
        n_mid = tp1_tpmid_touch_cnt  # Все, кто коснулся TP1 и TPmid (независимо от финального исхода)
        pct_mid = safe_pct(n_mid, total)
        n_mid_be = n_tpmid_be  # TPmid → TP1 = BE
        pct_mid_be = safe_pct(n_mid_be, total)
        pct_sl_before_tpmid = safe_pct(n_sl_before_tpmid, total)
        pct_tpmid_to_sl = safe_pct(n_tpmid_to_sl, total)
        
        results.append({
            "group_value": group_value,
            "total": total,
            "closed": closed,
            "open": n_open + n4 + n_tpmid_open,  # "Открыто": обычные открытые + TP1→OPEN + TP1→TPmid→OPEN
            "n1": n1, "pct1": pct1,
            "n2": n2, "pct2": pct2,
            "n3": n3, "pct3": pct3,
            "n4": n4, "pct4": pct4,
            "n_tpmid_open": n_tpmid_open,
            "pct_tpmid_open": safe_pct(n_tpmid_open, total),
            "n2_with_tpmid": n2_with_tpmid,  # Сигналы TP1+TP2, которые прошли через TPmid
            "wr_weighted": wr_weighted,
            "wr_touch": wr_touch,
            "wr_final": wr_final,
            "tp1_touch_cnt": tp1_touch_cnt,
            "tp1_touch_base": tp1_touch_base,
            "avg_outcome": avg_outcome,
            "avg_day_outcome": (sum(closed_outcome_pcts) / total) if (closed_outcome_pcts and total > 0) else 0.0,
            "group_sigs": group_sigs,  # Сохраняем сигналы для расчета equity
            "n_mid": n_mid, "pct_mid": pct_mid,  # TPmid метрики (счетчик касаний)
            "n_mid_be": n_mid_be, "pct_mid_be": pct_mid_be,  # TPmid → TP1 = BE
            "n_sl_before_tpmid": n_sl_before_tpmid, "pct_sl_before_tpmid": pct_sl_before_tpmid,
            "n_tpmid_to_sl": n_tpmid_to_sl, "pct_tpmid_to_sl": pct_tpmid_to_sl,
        })
    
    results.sort(key=lambda x: x["avg_outcome"], reverse=True)
    return results


def _format_group_stats_text(group_stats: list[dict], group_type: str, period_name: str, page: int = 1, per_page: int = 10, tp_mid_config: Optional[dict] = None) -> tuple[str, int]:
    """Форматирует текст для экрана статистики по группам"""
    type_names = {
        'symbol': 'символам',
        'timeframe': 'таймфреймам',
        'session': 'сессиям'
    }
    
    type_icons = {
        'symbol': '💎',
        'timeframe': '⏰',
        'session': '🌍'
    }
    
    icon = type_icons.get(group_type, '📊')
    text = f"{icon} <b>По {type_names.get(group_type, 'группам')} — {period_name}</b>\n\n"
    
    if not group_stats:
        text += "Нет данных за выбранный период."
        return text, 1
    
    # Пагинация
    total_pages = (len(group_stats) + per_page - 1) // per_page
    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, len(group_stats))
    
    page_groups = group_stats[start_idx:end_idx]
    
    for g in page_groups:
        if group_type == 'symbol':
            header = f"💎 <b>{g['group_value']}</b>"
        elif group_type == 'timeframe':
            header = f"⏰ <b>{g['group_value']}</b>"
        elif group_type == 'session':
            session_info = {
                'asian': '🇨🇳 Азиатская (00:00-08:00 UTC)',
                'european': '🇪🇺 Европейская (08:00-16:00 UTC)',
                'american': '🇺🇸 Американская (16:00-00:00 UTC)'
            }
            session_name = session_info.get(g['group_value'], f"🌍 {g['group_value']}")
            header = f"{session_name}"
        else:
            header = f"<b>{g['group_value']}</b>"
        
        text += f"{header}\n"
        text += f"Всего сигналов: <b>{g['total']}</b>\n"
        text += f"Рассчитано исходов: <b>{g['closed']}</b>    ⏳ Открыто: <b>{g['open']}</b>\n\n"
        
        # Формат без WINRATE, округление до 2 знаков, TP1→OPEN над путями
        text += f"⚪️ TP1 → OPEN: <b>{g['n4']}</b> (<b>{g['pct4']:.2f}%</b>)\n"
        if tp_mid_config and tp_mid_config.get('enabled') and 'n_tpmid_open' in g:
            text += f"⚪️ TP1 + TPmid → OPEN: <b>{g['n_tpmid_open']}</b> (<b>{g['pct_tpmid_open']:.2f}%</b>)\n"
        text += "\n<b>Пути исходов:</b>\n\n"
        text += (
            f"🟡 TP1: <b>{g['tp1_touch_cnt']}</b> из <b>{g['tp1_touch_base']}</b> "
            f"(<b>{(g['tp1_touch_cnt']/g['total']*100 if g['total']>0 else 0):.2f}%</b>)\n"
        )
        # Добавляем TPmid если включен
        if tp_mid_config and tp_mid_config.get('enabled') and 'n_mid' in g:
            text += f"🟠 TP1+TPmid: <b>{g['n_mid']}</b> (<b>{g.get('pct_mid', 0):.2f}%</b>)\n"
        # Проверяем, были ли сигналы с TP2, которые прошли через TPmid
        if tp_mid_config and tp_mid_config.get('enabled') and g.get('n2_with_tpmid', 0) > 0:
            pct_tpmid_tp2 = (g['n2_with_tpmid']/g['total']*100) if g['total']>0 else 0.0
            text += f"🟢 TP1+TPmid+TP2: <b>{g['n2_with_tpmid']}</b> (<b>{pct_tpmid_tp2:.2f}%</b>)\n\n"
        else:
            text += f"🟢 TP1+TP2: <b>{g['n2']}</b> (<b>{g['pct2']:.2f}%</b>)\n\n"
        text += f"🔻 SL до TP1: <b>{g['n1']}</b> (<b>{g['pct1']:.2f}%</b>)\n"
        if tp_mid_config and tp_mid_config.get('enabled') and 'n_sl_before_tpmid' in g:
            text += f"🔻 SL до TPmid: <b>{g['n_sl_before_tpmid']}</b> (<b>{g.get('pct_sl_before_tpmid', 0):.2f}%</b>)\n"
        text += "\n"
        text += f"🔙 TP1 → SL: <b>{g['n3']}</b> (<b>{g['pct3']:.2f}%</b>)\n"
        if tp_mid_config and tp_mid_config.get('enabled') and 'n_tpmid_to_sl' in g:
            text += f"🔙 TPmid → SL: <b>{g['n_tpmid_to_sl']}</b> (<b>{g.get('pct_tpmid_to_sl', 0):.2f}%</b>)\n"
        # Добавляем TPmid → TP1 = BE если включен
        if tp_mid_config and tp_mid_config.get('enabled') and 'n_mid_be' in g:
            text += f"⭕️ TPmid → TP1 = BE: <b>{g['n_mid_be']}</b> (<b>{g.get('pct_mid_be', 0):.2f}%</b>)\n"
        text += "\n"
        
        # Рассчитываем equity для группы (только для symbol и timeframe)
        if group_type in ('symbol', 'timeframe') and g.get('group_sigs'):
            try:
                from config.config import CONFIG
                from stats.equity import Trade, Leg, day_equity_result
                
                stats_cfg = CONFIG.get("stats", {})
                start_equity = float(stats_cfg.get("equity_start_usd", 100.0))
                include_commissions = bool(stats_cfg.get("include_commissions", True))
                tp1_share = float(CONFIG.get("position", {}).get("tp1_close_share", 0.7))
                
                group_sigs = g['group_sigs']
                trades = []
                
                for s in group_sigs:
                    side = str(s.get("side", "long")).lower()
                    entry = float(s.get("entry_price") or s.get("entry") or 0.0)
                    tp1 = float(s.get("tp1") or 0.0)
                    tp2 = float(s.get("tp2") or 0.0)
                    sl = float(s.get("sl") or 0.0)
                    path = _determine_signal_path(s, tp_mid_config)
                    legs = []
                    close_ts = 0.0
                    
                    if path == "TP1_PLUS_TP2":
                        if tp1 and tp2 and entry:
                            legs.append(Leg(type="TP1", price=tp1, qty_frac=tp1_share, time=float(s.get("hit_tp1_ts") or 0.0)))
                            legs.append(Leg(type="TP2", price=tp2, qty_frac=1.0 - tp1_share, time=float(s.get("hit_tp2_ts") or 0.0)))
                            close_ts = float(s.get("hit_tp2_ts") or 0.0)
                    elif path == "TP1_TO_SL":
                        if tp1 and sl and entry:
                            # Проверяем TPmid
                            tp_mid_price = None
                            if s.get('hit_tp_mid_ts'):
                                tp_mid_price = float(s.get('hit_tp_mid_price') or tp1)
                            price1 = tp_mid_price if tp_mid_price else tp1
                            legs.append(Leg(type="TP1", price=price1, qty_frac=tp1_share, time=float(s.get("hit_tp1_ts") or 0.0)))
                            legs.append(Leg(type="SL", price=sl, qty_frac=1.0 - tp1_share, time=float(s.get("hit_sl_ts") or 0.0)))
                            close_ts = float(s.get("hit_sl_ts") or 0.0)
                    elif path == "TPMID_TO_SL":
                        if sl and entry:
                            tp_mid_price = float(s.get('hit_tp_mid_price') or tp1)
                            legs.append(Leg(type="TP1", price=tp_mid_price, qty_frac=tp1_share, time=float(s.get("hit_tp_mid_ts") or 0.0)))
                            legs.append(Leg(type="SL", price=sl, qty_frac=1.0 - tp1_share, time=float(s.get("hit_sl_ts") or 0.0)))
                            close_ts = float(s.get("hit_sl_ts") or 0.0)
                    elif path == "SL_BEFORE_TP1":
                        if sl and entry:
                            legs.append(Leg(type="SL", price=sl, qty_frac=1.0, time=float(s.get("hit_sl_ts") or 0.0)))
                            close_ts = float(s.get("hit_sl_ts") or 0.0)
                    else:
                        # OPEN, TP1→OPEN, TP1→TPMID пропускаем
                        continue
                    
                    if not legs or entry <= 0:
                        continue
                    
                    fee_pct = 0.0
                    if include_commissions:
                        fee_pct = 0.0  # синтетически комиссии можно настроить отдельно
                    
                    trades.append(
                        Trade(
                            entry_price=entry,
                            side=side if side in ("long", "short") else "long",
                            legs=legs,
                            fee_round_pct=fee_pct,
                            funding_paid_pct=0.0,
                            close_time=close_ts,
                        )
                    )
                
                if trades:
                    trades.sort(key=lambda tr: tr.close_time)
                    r_day = day_equity_result(trades, start_equity)
                    text += f"🟣 Итог дня (Equity от ${int(start_equity)}, 1×): <b>{r_day*100:+.2f}%</b>\n\n"
            except Exception as e:
                print(f"[GROUP_STATS] Error calculating equity for {g['group_value']}: {e}")
        
        text += "\n" + "_" * 25 + "\n\n"
    
    return text, total_pages


def _build_group_stats_csv(group_stats: list[dict], group_type: str, period_name: str):
    """Генерирует CSV для статистики по группам"""
    from datetime import datetime
    
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "group_type", "group_value", "total", "closed", "open",
        "n_sl_before_tp1", "n_tp1_plus_tp2", "n_tp1_to_sl", "n_tp1_to_open",
        "n_sl_before_tpmid", "pct_sl_before_tpmid",
        "n_tpmid_to_sl", "pct_tpmid_to_sl",
        "wr_weighted", "wr_touch", "wr_final",
        "tp1_touch_cnt", "tp1_touch_base",
        "average_pct", "ev_pct"
    ])
    
    for g in group_stats:
        w.writerow([
            group_type, g["group_value"], g["total"], g["closed"], g["open"],
            g["n1"], g["n2"], g["n3"], g["n4"],
            g.get("n_sl_before_tpmid", 0), g.get("pct_sl_before_tpmid", 0),
            g.get("n_tpmid_to_sl", 0), g.get("pct_tpmid_to_sl", 0),
            g["wr_weighted"], g["wr_touch"], g["wr_final"],
            g["tp1_touch_cnt"], g["tp1_touch_base"],
            g["avg_outcome"], 0  # ev_pct отсутствует в новой версии
        ])
    
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    
    period_slug = period_name.lower().replace("день", "day").replace("неделя", "week").replace("месяц", "30d") if period_name else "report"
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    filename = f"stats_{group_type}_{period_slug}_{date_str}.csv"
    
    return data, filename


def _build_signals_csv(sigs: list[dict], period_name: str = ""):
    """Генерирует CSV с сигналами (новая версия с путями исходов)"""
    from datetime import datetime, timezone
    
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "ts", "symbol", "timeframe", "side", "entry", "tp1", "tp2", "sl",
        "path", "outcome_pct", "latency_tp1_s", "latency_tp2_s", "latency_sl_s"
    ])
    
    for s in sigs:
        ts = int(s["bar_ts"])
        t_utc = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        path = _determine_signal_path(s)
        outcome_pct = _calculate_outcome_pct(s, path)
        outcome_pct_str = f"{outcome_pct:.2f}" if outcome_pct is not None else ""
        
        bar_ts = s.get("bar_ts", 0)
        
        latency_tp1 = ""
        if s.get("hit_tp1_ts"):
            latency_tp1 = (s["hit_tp1_ts"] - bar_ts) / 1000
            
        latency_tp2 = ""
        if s.get("hit_tp2_ts"):
            latency_tp2 = (s["hit_tp2_ts"] - bar_ts) / 1000
            
        latency_sl = ""
        if s.get("hit_sl_ts"):
            latency_sl = (s["hit_sl_ts"] - bar_ts) / 1000
        
        w.writerow([
            t_utc, s["symbol"], s["timeframe"], s["side"],
            s["entry"], s["tp1"], s["tp2"], s["sl"],
            path, outcome_pct_str,
            latency_tp1, latency_tp2, latency_sl
        ])

    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    
    period_slug = period_name.lower().replace("день", "day").replace("неделя", "week").replace("месяц", "30d") if period_name else "report"
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    filename = f"signals_{period_slug}_{date_str}.csv"
    return data, filename


def _build_trades_csv(uid: int, since_ms: int, period_name: str = ""):
    """Генерирует CSV с пользовательскими сделками"""
    from datetime import datetime, timezone
    
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT 
                ut.id as trade_id,
                ut.symbol, ut.timeframe, ut.side,
                ut.base_qty as qty,
                ut.entry_price_user as entry_price,
                0 as fills_tp1,
                0 as fills_tp2, 
                0 as fills_sl,
                ut.pnl_net_total as pnl_net,
                ut.pnl_pct,
                ut.opened_at,
                ut.closed_at
            FROM user_trades ut
            WHERE ut.user_id = %s 
              AND ut.opened_at >= to_timestamp(%s/1000.0)
            ORDER BY ut.opened_at DESC
        """, (uid, since_ms))
        rows = [dict(r) for r in cur.fetchall()]
    
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "trade_id", "symbol", "timeframe", "side", "qty", "entry_price",
        "fills_tp1", "fills_tp2", "fills_sl", "pnl_net", "pnl_pct",
        "opened_at", "closed_at"
    ])
    
    for r in rows:
        opened_at_str = r["opened_at"].strftime('%Y-%m-%d %H:%M:%S') if r.get("opened_at") else ""
        closed_at_str = r["closed_at"].strftime('%Y-%m-%d %H:%M:%S') if r.get("closed_at") else ""
        
        w.writerow([
            r["trade_id"], r["symbol"], r["timeframe"], r["side"],
            r["qty"] if r["qty"] else "",
            r["entry_price"] if r["entry_price"] else "",
            r["fills_tp1"], r["fills_tp2"], r["fills_sl"],
            r["pnl_net"] if r["pnl_net"] else "",
            r["pnl_pct"] if r["pnl_pct"] else "",
            opened_at_str, closed_at_str
        ])
    
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    
    period_slug = period_name.lower().replace("день", "day").replace("неделя", "week").replace("месяц", "30d") if period_name else "report"
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    filename = f"trades_{period_slug}_{date_str}.csv"
    
    return data, filename


# =============================================================================
# AUTH
# =============================================================================

def _auth(update: Update) -> bool:
    """Проверка прав доступа"""
    env_chat = os.getenv("TELEGRAM_CHAT_ID")
    ch = update.effective_chat.id if update.effective_chat else None
    if env_chat:
        # Жёсткая привязка к одному чату
        return str(ch) == str(env_chat)
    if db_enabled():
        # Автосоздание пользователя при первом сообщении
        try:
            db_get_or_create_user(
                ch,
                update.effective_user.username if update.effective_user else None
            )
        except Exception:
            pass
        return True
    return True


# =============================================================================
# TF HELPERS
# =============================================================================

def normalize_tf_any(tf_str: str) -> Optional[str]:
    """Normalize any timeframe string"""
    return normalize_tf(tf_str)


async def _enter_tf_menu(update: Update):
    """Показать меню настройки таймфреймов"""
    text = (
        "<b>Настройка таймфреймов</b>\n\n"
        "• <b>ТФ для анализа</b> — на них считаются метрики/контекст.\n"
        "• <b>ТФ для сигналов</b> — с них реально уходят сигналы.\n\n"
        "Выбери раздел или вернись назад."
    )
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_TF_ANALYSIS), KeyboardButton(BTN_TF_SIGNAL)],
            [KeyboardButton(BTN_TF_MODE), KeyboardButton(BTN_TF_DONE)],
            [KeyboardButton(BTN_BACK)]
        ],
        resize_keyboard=True, one_time_keyboard=False
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def _tf_master_enter(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    """Вход в мастер настройки ТФ"""
    ch = update.effective_chat.id
    current = db_user_tfs_analysis(ch) if kind == "analysis" else db_user_tfs_signal(ch)
    title = "АНАЛИЗА" if kind == "analysis" else "СИГНАЛА"
    current_list = ", ".join([f"<code>{tf}</code>" for tf in current]) if current else "пока пусто"
    explain = (
        f"<b>ТФ для {title}</b>\n"
        f"Сейчас: {current_list}\n\n"
        "Пиши таймфреймы текстом (можно списком через пробел/запятую).\n"
        "Уже существующие — <b>удалю</b>, новые — <b>добавлю</b>.\n"
        f"Примеры: <code>1ч</code> <code>15м</code> <code>4h</code>, <code>60m</code>, <code>1h</code>, <code>1d</code>.\n\n"
        f"Внизу есть «{BTN_TF_DONE}» и «{BTN_BACK}»."
    )
    context.user_data["tf_kind"] = kind
    context.user_data["state"] = "await_tf_flow"
    await update.effective_message.reply_text(explain, parse_mode=ParseMode.HTML)


BTN_SET = {
    BTN_TF_SETTINGS, BTN_TF_ANALYSIS, BTN_TF_SIGNAL, BTN_TF_MODE, BTN_TF_DONE,
    BTN_BACK, BTN_MY_DATA, BTN_PAIR_TOGGLE 
}


async def _tf_flow_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода ТФ в мастере"""
    raw = (update.message.text or "").strip()
    if raw in BTN_SET:
        return  # это кнопка; её уже обработал handle_text

    ch = update.effective_chat.id
    kind = context.user_data.get("tf_kind")
    if kind not in {"analysis", "signal"}:
        context.user_data.pop("state", None)
        return

    parts = [p for p in re.split(r"[\s,;]+", raw) if p]
    add, rm, bad = [], [], []
    for p in parts:
        tf = normalize_tf_any(p)
        if not tf:
            bad.append(p)
            continue
        new_state = db_toggle_tf_analysis(ch, tf) if kind == "analysis" else db_toggle_tf_signal(ch, tf)
        (add if new_state else rm).append(tf)

    rep = (
        (f"Добавлено: <code>{', '.join(add)}</code>\n" if add else "") +
        (f"Удалено: <code>{', '.join(rm)}</code>\n" if rm else "") +
        (f"Не понял: <code>{', '.join(bad)}</code>\n" if bad else "") +
        f"Продолжай писать ТФ или нажми «{BTN_TF_DONE}»."
    )
    await update.effective_message.reply_text(rep or "Ок.", parse_mode=ParseMode.HTML)


# =============================================================================
# COMMANDS
# =============================================================================

async def cmd_recalc_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /recalc_stats - пересчитывает события для всех сигналов"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Пересчитываю события для всех сигналов...")
    
    try:
        from datetime import timedelta
        # Получаем ВСЕ сигналы пользователя (за последние 30 дней)
        since_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
        sigs = db_get_signals_with_events(uid, since_ms)
        
        total = len(sigs)
        updated = 0
        
        for s in sigs:
            # Сбрасываем старые события
            s["hit_tp1_ts"] = None
            s["hit_tp2_ts"] = None
            s["hit_sl_ts"] = None
            
            # Пересчитываем
            events = _resolve_all_events_for_signal(s)
            
            if any([events.get("hit_tp1_ts"), events.get("hit_tp2_ts"), events.get("hit_sl_ts")]):
                updated += 1
        
        await update.message.reply_text(
            f"✅ Готово!\n\n"
            f"Всего сигналов: {total}\n"
            f"Обновлено событий: {updated}\n"
            f"Открыто: {total - updated}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_recent_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /recent_signals - показать последние 10 сигналов с ID"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    
    try:
        from database import _db_conn
        
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, timeframe, 
                       extract(epoch from bar_ts)*1000 as bar_ts,
                       side, entry,
                       hit_tp1_ts, hit_tp2_ts, hit_sl_ts
                FROM signals_log
                WHERE user_id = %s
                ORDER BY bar_ts DESC
                LIMIT 10
            """, (uid,))
            
            rows = cur.fetchall()
            
            if not rows:
                await update.message.reply_text("❌ Сигналов не найдено")
                return
            
            lines = []
            lines.append("📋 <b>Последние 10 сигналов:</b>\n")
            
            for row in rows:
                sig_id = int(row[0])
                symbol = row[1]
                timeframe = row[2]
                bar_ts = int(row[3]) if row[3] else 0
                side = row[4]
                entry = float(row[5]) if row[5] else 0
                hit_tp1 = int(row[6]) if row[6] else None
                hit_tp2 = int(row[7]) if row[7] else None
                hit_sl = int(row[8]) if row[8] else None
                
                time_str = datetime.fromtimestamp(bar_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
                
                # Определяем статус
                if hit_tp2:
                    status = "✅ TP2"
                elif hit_tp1 and hit_sl:
                    status = "🟠 TP1→SL"
                elif hit_sl:
                    status = "🔴 SL"
                elif hit_tp1:
                    status = "🟡 TP1"
                else:
                    status = "⏳ OPEN"
                
                lines.append(
                    f"<code>#{sig_id:3d}</code> | {symbol:8s} | {timeframe:3s} | {side:5s} | {entry:.2f} | {status}"
                )
            
            lines.append(f"\n💡 Используй <code>/check_signal ID</code> для детальной проверки")
            
            text = "\n".join(lines)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


async def cmd_check_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /check_signal <id> - детальная проверка конкретного сигнала"""
    if not _auth(update):
        return
    
    # Получаем ID сигнала из аргументов
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "❌ Укажите ID сигнала:\n"
            "/check_signal 123"
        )
        return
    
    try:
        signal_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    uid = update.effective_user.id
    await update.message.reply_text(f"🔍 Проверяю сигнал #{signal_id}...")
    
    try:
        from database import _db_conn
        
        # Получаем сигнал из БД
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, timeframe, 
                       extract(epoch from bar_ts)*1000 as bar_ts,
                       side, entry, sl, tp1, tp2,
                       hit_tp1_price, hit_tp1_ts,
                       hit_tp2_price, hit_tp2_ts,
                       hit_sl_price, hit_sl_ts
                FROM signals_log
                WHERE id = %s AND user_id = %s
            """, (signal_id, uid))
            
            row = cur.fetchone()
            if not row:
                await update.message.reply_text(f"❌ Сигнал #{signal_id} не найден")
                return
            
            sig = {
                "id": int(row[0]),
                "symbol": row[1],
                "timeframe": row[2],
                "bar_ts": int(row[3]) if row[3] else 0,
                "side": row[4],
                "entry": float(row[5]) if row[5] else 0,
                "sl": float(row[6]) if row[6] else 0,
                "tp1": float(row[7]) if row[7] else 0,
                "tp2": float(row[8]) if row[8] else 0,
                "hit_tp1_price": float(row[9]) if row[9] else None,
                "hit_tp1_ts": int(row[10]) if row[10] else None,
                "hit_tp2_price": float(row[11]) if row[11] else None,
                "hit_tp2_ts": int(row[12]) if row[12] else None,
                "hit_sl_price": float(row[13]) if row[13] else None,
                "hit_sl_ts": int(row[14]) if row[14] else None,
            }
        
        # Форматируем информацию
        lines = []
        lines.append(f"🔍 <b>Сигнал #{sig['id']}</b>\n")
        lines.append(f"<b>Символ:</b> {sig['symbol']}")
        lines.append(f"<b>Таймфрейм:</b> {sig['timeframe']}")
        lines.append(f"<b>Сторона:</b> {sig['side']}")
        lines.append(f"<b>Время бара:</b> {datetime.fromtimestamp(sig['bar_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        lines.append(f"<b>Entry:</b> {sig['entry']}")
        lines.append(f"<b>TP1:</b> {sig['tp1']}")
        lines.append(f"<b>TP2:</b> {sig['tp2']}")
        lines.append(f"<b>SL:</b> {sig['sl']}\n")
        
        # События
        lines.append(f"<b>События (сохранено в БД):</b>")
        if sig['hit_tp1_ts']:
            tp1_time = datetime.fromtimestamp(sig['hit_tp1_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines.append(f"✅ TP1: {sig['hit_tp1_price']} @ {tp1_time}")
        else:
            lines.append(f"❌ TP1: не достигнут")
            
        if sig['hit_tp2_ts']:
            tp2_time = datetime.fromtimestamp(sig['hit_tp2_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines.append(f"✅ TP2: {sig['hit_tp2_price']} @ {tp2_time}")
        else:
            lines.append(f"❌ TP2: не достигнут")
            
        if sig['hit_sl_ts']:
            sl_time = datetime.fromtimestamp(sig['hit_sl_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines.append(f"✅ SL: {sig['hit_sl_price']} @ {sl_time}")
        else:
            lines.append(f"❌ SL: не достигнут")
        
        # Определяем путь
        path = _determine_signal_path(sig)
        lines.append(f"\n<b>Путь:</b> {path}")
        
        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        
        # Теперь ПЕРЕСЧИТЫВАЕМ с логированием
        await update.message.reply_text(
            "🔄 Пересчитываю события по свечам...\n"
            "(смотри логи Railway для деталей)"
        )
        
        print(f"\n{'='*60}")
        print(f"[CHECK_SIGNAL] Checking signal #{signal_id}")
        print(f"{'='*60}")
        
        events = _resolve_all_events_for_signal(sig)
        
        print(f"{'='*60}\n")
        
        # Результат пересчёта
        lines2 = []
        lines2.append(f"<b>Результат пересчёта:</b>\n")
        
        if events.get("hit_tp1_ts"):
            tp1_time = datetime.fromtimestamp(events['hit_tp1_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines2.append(f"✅ TP1: {events['hit_tp1_price']} @ {tp1_time}")
        else:
            lines2.append(f"❌ TP1: не найден")
            
        if events.get("hit_tp2_ts"):
            tp2_time = datetime.fromtimestamp(events['hit_tp2_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines2.append(f"✅ TP2: {events['hit_tp2_price']} @ {tp2_time}")
        else:
            lines2.append(f"❌ TP2: не найден")
            
        if events.get("hit_sl_ts"):
            sl_time = datetime.fromtimestamp(events['hit_sl_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            lines2.append(f"✅ SL: {events['hit_sl_price']} @ {sl_time}")
        else:
            lines2.append(f"❌ SL: не найден")
        
        # Сравнение
        lines2.append(f"\n<b>Сравнение:</b>")
        
        changed = False
        if bool(events.get("hit_tp1_ts")) != bool(sig['hit_tp1_ts']):
            lines2.append(f"⚠️ TP1: изменилось!")
            changed = True
        if bool(events.get("hit_tp2_ts")) != bool(sig['hit_tp2_ts']):
            lines2.append(f"⚠️ TP2: изменилось!")
            changed = True
        if bool(events.get("hit_sl_ts")) != bool(sig['hit_sl_ts']):
            lines2.append(f"⚠️ SL: изменилось!")
            changed = True
        
        if not changed:
            lines2.append("✅ События совпадают")
        
        text2 = "\n".join(lines2)
        await update.message.reply_text(text2, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /audit - проверка корректности статистики"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Генерирую аудит статистики...")
    
    try:
        from datetime import timedelta
        import tempfile
        import os
        from audits.audit_stats import audit_from_csv, load_trades_csv, aggregate_paths, find_anomalies
        
        # Получаем сигналы за последние 7 дней
        since_ms = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
        sigs = db_get_signals_with_events(uid, since_ms)
        
        if not sigs:
            await update.message.reply_text("❌ Нет данных за последние 7 дней")
            return
        
        # Резолвим события если нужно
        for s in sigs:
            has_events = bool(s.get("hit_tp1_ts")) or bool(s.get("hit_tp2_ts")) or bool(s.get("hit_sl_ts"))
            if not has_events:
                events = _resolve_all_events_for_signal(s)
                s.update(events)
        
        # Добавляем пути к сигналам
        for s in sigs:
            s["path"] = _determine_signal_path(s)
        
        # Создаём временный CSV
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            # Генерируем CSV вручную
            f.write("id,symbol,side,entry,sl,tp1,tp2,path,tp1_time,tp2_time,sl_time,pnl_pct\n")
            for s in sigs:
                sig_id = s.get("id", "")
                symbol = s.get("symbol", "")
                side = s.get("side", "")
                entry = s.get("entry", "")
                sl = s.get("sl", "")
                tp1 = s.get("tp1", "")
                tp2 = s.get("tp2", "")
                path = s.get("path", "OPEN")
                
                # Timestamps (если есть)
                tp1_ts = s.get("hit_tp1_ts")
                tp2_ts = s.get("hit_tp2_ts")
                sl_ts = s.get("hit_sl_ts")
                
                tp1_time = datetime.fromtimestamp(tp1_ts / 1000, tz=timezone.utc).isoformat() if tp1_ts else ""
                tp2_time = datetime.fromtimestamp(tp2_ts / 1000, tz=timezone.utc).isoformat() if tp2_ts else ""
                sl_time = datetime.fromtimestamp(sl_ts / 1000, tz=timezone.utc).isoformat() if sl_ts else ""
                
                # PnL
                pnl_pct = _calculate_outcome_pct(s, path) or 0.0
                
                f.write(f"{sig_id},{symbol},{side},{entry},{sl},{tp1},{tp2},{path},{tp1_time},{tp2_time},{sl_time},{pnl_pct}\n")
            
            temp_csv_path = f.name
        
        # Загружаем и анализируем
        tr = load_trades_csv(temp_csv_path)
        agg = aggregate_paths(tr)
        flags = find_anomalies(tr)
        
        # Формируем отчёт
        lines = []
        lines.append("📊 <b>АУДИТ СТАТИСТИКИ (7 дней)</b>\n")
        lines.append(f"Всего записей: <b>{agg.total}</b>")
        lines.append(f"Закрыто: <b>{agg.closed}</b> | Открыто: <b>{agg.open}</b>\n")
        lines.append(f"<b>Пути:</b>")
        lines.append(f"🔴 SL до TP1: <b>{agg.n1}</b>")
        lines.append(f"🟠 TP1 → SL: <b>{agg.n3}</b>")
        lines.append(f"🟢 TP1+TP2: <b>{agg.n2}</b>")
        lines.append(f"⚪️ TP1 → OPEN: <b>{agg.n4}</b>\n")
        lines.append(f"<b>WINRATE:</b>")
        lines.append(f"🟢 Final: <b>{agg.final}%</b>")
        lines.append(f"🟣 Weighted: <b>{agg.weighted}%</b>")
        lines.append(f"🟡 Touch: <b>{agg.touch}%</b> ({agg.tp1_touch_cnt} из {agg.tp1_touch_base})")
        
        if agg.final >= 95.0 and agg.open > 0 and agg.closed >= 20:
            lines.append("\n⚠️ <b>SANITY:</b> финальный WR ≥95% при наличии открытых — проверь тай-брейк/буфер.")
        
        if flags:
            lines.append(f"\n⚠️ <b>Обнаружено аномалий: {len(flags)}</b>")
            for key in flags:
                lines.append(f"  • {key}: {len(flags[key])} записей")
        else:
            lines.append("\n✅ Аномалий не обнаружено!")
        
        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        
        # Удаляем временный файл
        try:
            os.unlink(temp_csv_path)
        except Exception:
            pass
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    ch = update.effective_chat.id if update.effective_chat else None
    username = update.effective_user.username if update.effective_user else None
    env_chat = os.getenv("TELEGRAM_CHAT_ID")

    if env_chat and str(ch) != str(env_chat):
        await update.effective_message.reply_text("Этот бот привязан к другому чату.")
        return

    if db_enabled():
        db_get_or_create_user(ch, username)
        db_set_running(ch, True)

    text = (
        "Привет! Я сигнальный бот.\n\n"
        "Ты можешь управлять мной кнопками снизу или просто текстом:\n"
        "• «добавь пару SOL», «пары»,\n"
        "• «добавь тф 1h», «убери тф 4h», «режим any|stacked»,\n"
        "• «уровни по SOL 4h: 5.12, 5.05 4.88», «покажи уровни по SOL 4h», «удали уровни по SOL 4h」.\n"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_main(update.effective_user.id))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status"""
    if not _auth(update):
        return
    ch = update.effective_chat.id

    running = db_is_running(ch) if db_enabled() else True
    sess = db_session_is_open(ch) if db_enabled() else True

    if db_enabled():
        pairs = db_user_pairs(ch) or ["BTCUSDT", "ETHUSDT"]
        tfa = db_user_tfs_analysis(ch) or ["1h", "4h", "1d"]
        tfs = db_user_tfs_signal(ch) or ["1h", "4h", "1d"]
        mode = (db_get_settings(ch).get("tf_mode") or "stacked").upper()
        
        # Получаем настройки уведомлений
        notif = db_get_notifications(ch)
        notif_text = []
        if notif["stats_notifications"]:
            notif_text.append("📊 Статистика")
        if notif["drawdown_alerts"]:
            notif_text.append("📉 Просадка")
        if notif["weekly_reports"]:
            notif_text.append("📅 Еженедельные")

        # Форматируем пары для вывода
        pairs_block = "</code>\n<code>".join(pairs) if pairs else "—"
        
        text = (
            "<b>Статус</b>\n\n"
            f"<b>Running:</b> <code>{'ON' if running else 'OFF'}</code>\n"
            f"<b>Session:</b> <code>{'OPEN' if sess else 'CLOSED'}</code>\n\n"
            f"Пары:\n<code>{pairs_block}</code>\n"
            f"ТФ (анализ): <b>{', '.join(tfa) or '—'}</b>\n"
            f"ТФ (сигналы): <b>{', '.join(tfs) or '—'}</b>\n"
            f"Режим сигналов: <b>{mode}</b>\n"
            f"Schedule: <code>{db_get_settings(ch).get('schedule_minutes')}</code> мин\n"
            f"Уведомления: <b>{', '.join(notif_text) if notif_text else '—'}</b>\n"
        )
    else:
        pairs = ["BTCUSDT", "ETHUSDT"]
        tfa = ["1h", "4h", "1d"]
        tfs = ["1h", "4h", "1d"]
        mode = "STACKED"
        
        # Форматируем пары для вывода
        pairs_block = "</code>\n<code>".join(pairs)

        text = (
            "<b>Статус</b>\n\n"
            f"<b>Running:</b> <code>{'ON' if running else 'OFF'}</code>\n"
            f"<b>Session:</b> <code>{'OPEN' if sess else 'CLOSED'}</code>\n\n"
            f"Пары:\n<code>{pairs_block}</code>\n"
            f"ТФ (анализ): <b>{', '.join(tfa)}</b>\n"
            f"ТФ (сигналы): <b>{', '.join(tfs)}</b>\n"
            f"Режим сигналов: <b>{mode}</b>\n"
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_main(update.effective_user.id))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop"""
    if not _auth(update):
        return
    ch = update.effective_chat.id
    if db_enabled():
        db_set_running(ch, False)
    await update.effective_message.reply_text("⏹️ Сигналы: ВЫКЛЮЧЕНО", reply_markup=_kb_main(update.effective_user.id))


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /pairs"""
    if not _auth(update):
        return
    ch = update.effective_chat.id
    if db_enabled():
        pairs = db_user_pairs(ch)
    else:
        pairs = ["BTCUSDT", "ETHUSDT"]
    
    text = "Введи символ пары (например: BTCUSDT или BTC/USDT).\n"
    text += "Если пара уже есть — удалю, если нет — добавлю.\n\n"
    if pairs:
        text += "<b>Текущие пары:</b> " + ", ".join([f"<code>{p}</code>" for p in pairs])
    else:
        text += "<b>Текущие пары:</b> —"
    
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("✅ НАСТРОЙКА ЗАВЕРШЕНА")], [KeyboardButton("🔙 НАЗАД")]],
            resize_keyboard=True
        )
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add"""
    if not _auth(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Используй: /add SYMBOL (например, /add SOLUSDT)")
        return
    sym = context.args[0].upper()
    if not sym.endswith("USDT"):
        await update.effective_message.reply_text("Символ должен заканчиваться на USDT.")
        return
    ch = update.effective_chat.id
    if db_enabled():
        db_add_pair(ch, sym)
        await update.effective_message.reply_text(f"Добавил {sym}.")
    else:
        await update.effective_message.reply_text("В режиме без БД пары не сохраняются.")


async def cmd_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /rm"""
    if not _auth(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Используй: /rm SYMBOL")
        return
    sym = context.args[0].upper()
    ch = update.effective_chat.id
    if db_enabled():
        ok = db_rm_pair(ch, sym)
        await update.effective_message.reply_text(("Удалил " + sym) if ok else (sym + " не найден"))
    else:
        await update.effective_message.reply_text("В режиме без БД пары не сохраняются.")


# TF commands
async def cmd_tfs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tfs"""
    if not _auth(update):
        return
    ch = update.effective_chat.id
    if db_enabled():
        tfs = db_user_tfs(ch)
        st = db_get_settings(ch)
        await update.effective_message.reply_text(
            f"Твои ТФ: {', '.join(tfs) or '—'}\nРежим: {st.get('tf_mode','stacked')}"
        )
    else:
        await update.effective_message.reply_text("Без БД: дефолтные ТФ 1h,4h,1d (локально).")


async def cmd_tf_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tf_add"""
    if not _auth(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Используй: /tf_add 1h")
        return
    tf = context.args[0]
    ch = update.effective_chat.id
    if db_enabled():
        db_add_tf(ch, tf)
        await update.effective_message.reply_text(f"Добавил ТФ {tf}")
    else:
        await update.effective_message.reply_text("Без БД не сохраняется.")


async def cmd_tf_rm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tf_rm"""
    if not _auth(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Используй: /tf_rm 4h")
        return
    tf = context.args[0]
    ch = update.effective_chat.id
    if db_enabled():
        ok = db_rm_tf(ch, tf)
        await update.effective_message.reply_text(("Убрал ТФ " + tf) if ok else (tf + " не найден"))
    else:
        await update.effective_message.reply_text("Без БД не сохраняется.")


async def cmd_tf_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tf_mode"""
    if not _auth(update):
        return
    if not context.args or context.args[0] not in ("stacked", "any"):
        await update.effective_message.reply_text("Используй: /tf_mode stacked|any")
        return
    ch = update.effective_chat.id
    if db_enabled():
        db_set_tf_mode(ch, context.args[0])
        await update.effective_message.reply_text(f"Режим ТФ: {context.args[0]}")
    else:
        await update.effective_message.reply_text("Без БД режим не сохраняется.")


# =============================================================================
# CALLBACK HANDLER (PLACEHOLDER - нужно перенести полный on_cb из signal_bot.py)
# =============================================================================

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-запросов (inline кнопки)"""
    if not _auth(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data
    
    # ===== ENTRY FLOW CALLBACKS =====
    if data.startswith("entry:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        
        if action == "mode":
            # entry:mode:qty:123 or entry:mode:usdt:123
            mode = parts[2] if len(parts) > 2 else "USDT"
            signal_id = int(parts[3]) if len(parts) > 3 else 0
            
            context.user_data["entry_context"] = {
                "signal_id": signal_id,
                "entry_mode": mode.upper()
            }
            context.user_data["await_entry_value"] = True
            
            if mode.upper() == "QTY":
                prompt = "🪙 Введи количество монет (например: 0.05):"
            else:
                prompt = "💵 Введи сумму в USDT (например: 50, 100, 500):"
            
            try:
                await update.callback_query.answer()
                await update.effective_message.reply_text(
                    prompt,
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton("50"), KeyboardButton("100"), KeyboardButton("250")],
                                  [KeyboardButton("500"), KeyboardButton("1000"), KeyboardButton("2000")],
                                  [KeyboardButton(BTN_BACK)]],
                        resize_keyboard=True,
                        one_time_keyboard=True
                    )
                )
            except Exception as e:
                print(f"[ERROR] entry:mode failed: {e}")
            return
        
        elif action == "confirm":
            entry_ctx = context.user_data.get("entry_context", {})
            signal_id = entry_ctx.get("signal_id")
            entry_mode = entry_ctx.get("entry_mode")
            entry_value = entry_ctx.get("entry_value")
            entry_price = entry_ctx.get("entry_price")
            entry_qty = entry_ctx.get("entry_qty")
            entry_notional = entry_ctx.get("entry_notional")
            leverage = entry_ctx.get("leverage", 10)
            
            if not all([signal_id, entry_mode, entry_value, entry_price, entry_qty, entry_notional]):
                await update.callback_query.answer("Ошибка: неполные данные", show_alert=True)
                return
            
            uid = update.effective_user.id
            with _db_conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT id, symbol, timeframe, side, entry, sl, tp1, tp2
                    FROM signals_log WHERE id=%s
                """, (signal_id,))
                row = cur.fetchone()
                if not row:
                    await update.callback_query.answer("Сигнал не найден", show_alert=True)
                    return
                sig = {
                    "id": row[0], "symbol": row[1], "timeframe": row[2],
                    "side": row[3], "entry": row[4], "sl": row[5], "tp1": row[6], "tp2": row[7]
                }
                
                fee_open = entry_notional * 5 / 10000
                
                try:
                    cur.execute("""
                        INSERT INTO user_trades(
                            user_id, signal_id, amount_usdt, side,
                            entry_price, entry_price_user, entry_price_signal,
                            tp1, tp2, sl, timeframe, symbol,
                            base_qty, remaining_qty, fee_rate_bps, fee_open,
                            entry_mode, entry_request_value, entry_qty, entry_notional_usdt
                        ) VALUES (
                            %s,%s,%s,%s,
                            %s,%s,%s,
                            %s,%s,%s,%s,%s,
                            %s,%s,%s,%s,
                            %s,%s,%s,%s
                        )
                        ON CONFLICT (user_id, signal_id) DO NOTHING
                        RETURNING id
                    """, (
                        uid, sig["id"], entry_notional, sig["side"],
                        entry_price, entry_price, sig["entry"],
                        sig["tp1"], sig["tp2"], sig["sl"], sig["timeframe"], sig["symbol"],
                        entry_qty, entry_qty, 5, fee_open,
                        entry_mode, entry_value, entry_qty, entry_notional
                    ))
                    inserted = cur.fetchone()
                    c.commit()
                except Exception as e:
                    print(f"[ERROR] Insert user_trade failed: {e}")
                    await update.callback_query.answer("Ошибка при создании сделки", show_alert=True)
                    return
                
                if not inserted:
                    await update.callback_query.answer("Вход уже зафиксирован для этого сигнала", show_alert=True)
                    return
                
                context.user_data.pop("entry_context", None)
                
                side_icon = '🟢' if sig['side'] == 'LONG' else '🔴'
                filters = get_symbol_filters(sig["symbol"])
                step_size = filters.get("stepSize", 0.001)
                qty_formatted = format_qty(entry_qty, step_size)
                
                msg = (
                    "Вход зафиксирован ✅\n\n"
                    f"{side_icon} <code>{sig['symbol']}</code> {sig['side']} • TF <code>{sig['timeframe']}</code>\n\n"
                )
                
                if entry_mode == "USDT":
                    msg += f"💵 Режим: По сумме (USDT)\n"
                    msg += f"Запрос: {entry_value:.2f} USDT\n"
                else:
                    msg += f"🪙 Режим: По количеству\n"
                    msg += f"Запрос: {qty_formatted} монет\n"
                
                msg += f"Плечо: {leverage}x\n"
                msg += f"Цена входа: <code>{entry_price:.4f}</code>\n\n"
                msg += f"→ Количество: <code>{qty_formatted}</code> монет\n"
                msg += f"→ Стоимость: <code>{entry_notional:.2f}</code> USDT\n"
                
                await update.callback_query.answer("Сделка создана ✅")
                await update.effective_message.reply_text(
                    msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_kb_main(uid)
                )
            return
        
        elif action == "cancel":
            context.user_data.pop("entry_context", None)
            context.user_data.pop("await_entry_value", None)
            context.user_data.pop("await_entry_leverage", None)
            context.user_data.pop("await_entry_price_new", None)
            
            await update.callback_query.answer("Отменено")
            try:
                await update.effective_message.reply_text(
                    "Ввод отменён.",
                    reply_markup=_kb_main(update.effective_user.id)
                )
            except Exception:
                pass
            return
    
    # ===== STATS CSV CALLBACKS =====
    if data.startswith("stats:csv="):
        csv_type = data.split("=", 1)[1]
        uid = update.effective_user.id
        since_ms = context.user_data.get("stats_since_ms")
        period_name = context.user_data.get("stats_period_name", "")
        
        if not since_ms:
            await q.answer("Сначала выбери период статистики", show_alert=True)
            return
        
        if not db_enabled():
            await q.answer("CSV доступен только с БД", show_alert=True)
            return
        
        try:
            if csv_type == "signals":
                sigs = db_get_signals_with_events(uid, since_ms)
                if not sigs:
                    await q.answer("Нет сигналов за выбранный период", show_alert=True)
                    return
                
                data_file, filename = _build_signals_csv(sigs, period_name)
                await context.bot.send_document(
                    chat_id=uid,
                    document=InputFile(data_file, filename=filename),
                    caption=f"CSV отчёт по сигналам — {period_name}"
                )
                await q.answer("CSV отправлен")
                
            elif csv_type == "trades":
                data_file, filename = _build_trades_csv(uid, since_ms, period_name)
                await context.bot.send_document(
                    chat_id=uid,
                    document=InputFile(data_file, filename=filename),
                    caption=f"CSV отчёт по сделкам — {period_name}"
                )
                await q.answer("CSV отправлен")
            else:
                await q.answer("Неизвестный тип CSV", show_alert=True)
                
        except Exception as e:
            print(f"[ERROR] CSV generation failed: {e}")
            await q.answer("Не удалось сформировать CSV, повтори позже", show_alert=True)
        
        return
    
    # ===== STATS GROUP PAGINATION =====
    if data.startswith("stats_group:page="):
        page_str = data.split("=", 1)[1]
        try:
            page = int(page_str)
        except:
            await q.answer("Некорректная страница", show_alert=True)
            return
        
        group_stats = context.user_data.get("group_stats")
        group_type = context.user_data.get("group_type")
        period_name = context.user_data.get("stats_period_name", "")
        tp_mid_cfg = context.user_data.get("group_tp_mid_config")
        
        if not group_stats or not group_type:
            await q.answer("Сначала просмотри статистику по группам", show_alert=True)
            return
        
        context.user_data["group_page"] = page
        
        text, total_pages = _format_group_stats_text(group_stats, group_type, period_name, page=page, per_page=10, tp_mid_config=tp_mid_cfg if tp_mid_cfg and tp_mid_cfg.get('enabled') else None)
        
        keyboard = []
        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append(InlineKeyboardButton("◀️", callback_data=f"stats_group:page={page-1}"))
            nav_row.append(InlineKeyboardButton(f"Стр. {page}/{total_pages}", callback_data="stats_group:noop"))
            if page < total_pages:
                nav_row.append(InlineKeyboardButton("▶️", callback_data=f"stats_group:page={page+1}"))
            keyboard.append(nav_row)
        
        keyboard.append([InlineKeyboardButton("📥 CSV", callback_data=f"stats_group:csv={group_type}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        await q.answer()
        return
    
    if data == "stats_group:noop":
        await q.answer()
        return
    
    # ===== STATS GROUP CSV =====
    if data.startswith("stats_group:csv="):
        group_type = data.split("=", 1)[1]
        uid = update.effective_user.id
        group_stats = context.user_data.get("group_stats")
        period_name = context.user_data.get("stats_period_name", "")
        
        if not group_stats:
            await q.answer("Сначала просмотри статистику по группам", show_alert=True)
            return
        
        try:
            data_file, filename = _build_group_stats_csv(group_stats, group_type, period_name)
            
            type_names = {
                'symbol': 'символам',
                'timeframe': 'таймфреймам',
                'session': 'сессиям'
            }
            
            await context.bot.send_document(
                chat_id=uid,
                document=InputFile(data_file, filename=filename),
                caption=f"CSV отчёт по {type_names.get(group_type, 'группам')} — {period_name}"
            )
            await q.answer("CSV отправлен")
            
        except Exception as e:
            print(f"[ERROR] Group CSV generation failed: {e}")
            await q.answer("Не удалось сформировать CSV, повтори позже", show_alert=True)
        
        return
    
    # ===== LEVELS CALLBACKS =====
    if data.startswith("levels:"):
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        uid = update.effective_user.id
        
        if action == "symbol" and len(parts) >= 3:
            direction = parts[2]
            available_symbols = _get_user_symbols(uid)
            current_symbol = _get_levels_current_symbol(uid, context)
            current_tf = _get_levels_current_timeframe(uid, context)
            
            if direction == "prev":
                current_idx = available_symbols.index(current_symbol) if current_symbol in available_symbols else 0
                new_symbol = available_symbols[current_idx - 1] if current_idx > 0 else available_symbols[-1]
            elif direction == "next":
                current_idx = available_symbols.index(current_symbol) if current_symbol in available_symbols else 0
                new_symbol = available_symbols[current_idx + 1] if current_idx < len(available_symbols) - 1 else available_symbols[0]
            else:
                await q.answer("Неизвестное направление", show_alert=True)
                return
            
            await _show_levels_panel(update, context, new_symbol, current_tf, 1)
            return
        
        elif action == "cfg":
            if len(parts) == 2:
                # Show config panel
                await _show_levels_cfg(update, context)
            elif len(parts) >= 3:
                sub = parts[2]
                if sub == "toggle" and len(parts) >= 4:
                    field = parts[3]
                    cfg = db_levels_get_config(uid)
                    if field == "use":
                        cfg["use_levels"] = not bool(cfg.get("use_levels", True))
                    elif field == "block":
                        cfg["block_against_level"] = not bool(cfg.get("block_against_level", True))
                    db_levels_set_config(uid, cfg)
                    await _show_levels_cfg(update, context)
                elif sub == "thresh" and len(parts) >= 4:
                    op = parts[3]
                    cfg = db_levels_get_config(uid)
                    th = int(cfg.get("score_threshold", 3))
                    th = max(0, th - 1) if op == "-" else (th + 1)
                    cfg["score_threshold"] = th
                    db_levels_set_config(uid, cfg)
                    await _show_levels_cfg(update, context)
                elif sub == "info":
                    info = (
                        "<b>Пояснения к настройкам уровней</b>\n\n"
                        "• <b>use_levels</b>: если выключено — уровни никак не влияют на сигналы.\n"
                        "• <b>block_against_level</b>: жёсткий запрет отправлять сигнал, если он 'в уровень'.\n"
                        "• <b>score_threshold</b>: минимальный балл confluence.\n"
                        "• <b>near_tight/near_loose</b>: пороги близости в долях ATR.\n"
                        "• <b>adjust_sl_tp</b>: при включении (пока не активно) SL/TP корректируются."
                    )
                    await update.effective_message.reply_text(info, parse_mode=ParseMode.HTML)
            return
        
        elif action == "export":
            # Export levels to CSV
            current_symbol = _get_levels_current_symbol(uid, context)
            current_tf = _get_levels_current_timeframe(uid, context)
            await _levels_export_csv(update, context, current_symbol, current_tf)
            return
        
        elif action == "import":
            # Import levels from CSV
            current_symbol = _get_levels_current_symbol(uid, context)
            current_tf = _get_levels_current_timeframe(uid, context)
            context.user_data["levels_import_ctx"] = {"symbol": current_symbol, "tf": current_tf}
            await update.effective_message.reply_text(
                "Пришли CSV-текст (вставкой) формата: symbol,timeframe,price[,note] — по одной строке на уровень.",
                reply_markup=_kb_main(uid)
            )
            return
        
        elif action == "add":
            # Add levels manually
            current_symbol = _get_levels_current_symbol(uid, context)
            current_tf = _get_levels_current_timeframe(uid, context)
            context.user_data["levels_add_ctx"] = {"symbol": current_symbol, "tf": current_tf}
            await update.effective_message.reply_text(
                "Пришли уровни числами через запятую/пробел (можно несколько): например: 65000, 64550 64000",
                reply_markup=_kb_main(uid)
            )
            return
        
        elif action == "del":
            # Delete all levels for current symbol/tf
            current_symbol = _get_levels_current_symbol(uid, context)
            current_tf = _get_levels_current_timeframe(uid, context)
            n = db_clear_levels(uid, current_symbol, current_tf)
            await update.effective_message.reply_text(f"Удалил {n} уровней по {current_symbol} {current_tf}.")
            await _show_levels_panel(update, context, current_symbol, current_tf, 1)
            return
        
        elif action == "rm_one" and len(parts) >= 3:
            # Remove one level by price
            try:
                price_to_remove = float(parts[2])
                current_symbol = _get_levels_current_symbol(uid, context)
                current_tf = _get_levels_current_timeframe(uid, context)
                db_remove_level(uid, current_symbol, current_tf, price_to_remove)
                await q.answer("Уровень удалён")
                await _show_levels_panel(update, context, current_symbol, current_tf, 1)
            except Exception as e:
                print(f"[ERROR] levels:rm_one: {e}")
                await q.answer("Ошибка удаления", show_alert=True)
            return
        
        await q.answer("Callback обработан")
        return
    
    # ===== TRADES CALLBACKS =====
    if data.startswith("trades:"):
        await cb_trades_router(update, context, data)
        return
    
    # ===== TRADE CLOSE CALLBACKS =====
    if data.startswith("trade:") and ":close:" in data:
        # Handle partial close callbacks: trade:{id}:close:{type}
        parts = data.split(":")
        if len(parts) >= 4:
            trade_id = int(parts[1])
            close_type = parts[3]
            await _handle_close_callback(update, context, trade_id, close_type, parts[4:] if len(parts) > 4 else [])
        return
    
    # ===== ADMIN CALLBACKS =====
    if data.startswith("admin:"):
        from admin import handle_admin_callback
        await handle_admin_callback(update, context, data)
        return
    
    # Заглушка для остальных callbacks
    await q.answer("Callback обработан")


# =============================================================================
# TRADE CLOSE CALLBACK HANDLER
# =============================================================================

async def _handle_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id: int, close_type: str, extra_parts: list):
    """Handle close trade callbacks"""
    from panels import _show_close_size_selection, _execute_partial_close
    
    uid = update.effective_user.id
    
    if close_type in ["tp1", "tp2", "sl"]:
        # Get exit price from trade data
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT tp1, tp2, sl FROM user_trades 
                WHERE id=%s AND user_id=%s AND status='ACTIVE'
            """, (trade_id, uid))
            trade = cur.fetchone()
        
        if not trade:
            await update.effective_message.reply_text("Сделка не найдена.")
            return
        
        tp1, tp2, sl = trade
        exit_price = None
        
        if close_type == "tp1" and tp1:
            exit_price = float(tp1)
        elif close_type == "tp2" and tp2:
            exit_price = float(tp2)
        elif close_type == "sl" and sl:
            exit_price = float(sl)
        
        if exit_price is None:
            await update.effective_message.reply_text(f"Уровень {close_type.upper()} не установлен для этой сделки.")
            return
        
        await _show_close_size_selection(update, context, trade_id, close_type, exit_price)
        
    elif close_type == "full":
        # Close entire position - ask for exit price
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, remaining_qty, entry_price_user, fee_rate_bps FROM user_trades 
                WHERE id=%s AND user_id=%s AND status='ACTIVE'
            """, (trade_id, uid))
            trade = cur.fetchone()
        
        if not trade:
            await update.effective_message.reply_text("Сделка не найдена.")
            return
        
        symbol, side, remaining_qty, entry_price, fee_rate_bps = trade
        remaining_amount_usdt = float(remaining_qty or 0) * float(entry_price or 0)
        context.user_data["close_trade_context"] = {
            "trade_id": trade_id,
            "close_type": "full",
            "exit_price": None,
            "symbol": symbol,
            "side": side,
            "remaining_qty": remaining_qty,
            "entry_price": entry_price,
            "remaining_amount_usdt": remaining_amount_usdt,
            "fee_rate_bps": fee_rate_bps
        }
        
        await update.effective_message.reply_text(
            "Введите цену закрытия для полного закрытия позиции:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        
    elif close_type == "custom":
        # Custom price - ask for price first
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, remaining_qty, entry_price_user, fee_rate_bps FROM user_trades 
                WHERE id=%s AND user_id=%s AND status='ACTIVE'
            """, (trade_id, uid))
            trade = cur.fetchone()
        
        if not trade:
            await update.effective_message.reply_text("Сделка не найдена.")
            return
        
        symbol, side, remaining_qty, entry_price, fee_rate_bps = trade
        remaining_amount_usdt = float(remaining_qty or 0) * float(entry_price or 0)
        context.user_data["close_trade_context"] = {
            "trade_id": trade_id,
            "close_type": "custom",
            "exit_price": None,
            "symbol": symbol,
            "side": side,
            "remaining_qty": remaining_qty,
            "entry_price": entry_price,
            "remaining_amount_usdt": remaining_amount_usdt,
            "fee_rate_bps": fee_rate_bps
        }
        
        await update.effective_message.reply_text(
            "Введите цену закрытия:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        
    elif close_type == "mode" and len(extra_parts) >= 1:
        # Size mode selection: trade:{id}:close:mode:{amount|qty}
        size_mode = extra_parts[0]
        ctx = context.user_data.get("close_trade_context", {})
        
        if not ctx:
            await update.effective_message.reply_text("Ошибка: контекст закрытия не найден.")
            return
        
        # Store size mode and ask for value
        ctx["size_mode"] = size_mode
        context.user_data["close_trade_context"] = ctx
        
        if size_mode == "amount":
            await update.effective_message.reply_text(
                "Введите сумму в USDT для закрытия:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(BTN_BACK)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
            )
        else:  # qty
            await update.effective_message.reply_text(
                f"Введите количество {ctx.get('symbol', '').replace('USDT', '')} для закрытия:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(BTN_BACK)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
            )
            
    elif close_type == "confirm":
        # Execute the close
        await _execute_partial_close(update, context)
        
    elif close_type == "edit_price":
        # Ask user to input new price
        ctx = context.user_data.get("close_trade_context", {})
        if not ctx:
            await update.effective_message.reply_text("Ошибка: контекст закрытия не найден.")
            return
        await update.effective_message.reply_text(
            "Введите новую цену закрытия:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        # Mark exit_price None to route next text as price
        ctx["exit_price"] = None
        context.user_data["close_trade_context"] = ctx
        
    elif close_type == "edit_fee":
        # Ask user to input new fee in bps
        ctx = context.user_data.get("close_trade_context", {})
        if not ctx:
            await update.effective_message.reply_text("Ошибка: контекст закрытия не найден.")
            return
        context.user_data["await_edit_fee_bps"] = True
        await update.effective_message.reply_text(
            "Введите комиссию в bps (например 5 = 0.05%):",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )


# =============================================================================
# TEXT HANDLER (PLACEHOLDER - нужно перенести полный handle_text из signal_bot.py)
# =============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений и кнопок"""
    if not _auth(update):
        return

    ch = update.effective_chat.id
    txt_raw = (update.message.text or "").strip()
    txt = txt_raw
    # Режим ввода дельты TPmid
    if context.user_data.get("state") == "tp_mid_delta_input":
        uid = update.effective_user.id
        val = _parse_tp_mid_delta(txt)
        if val is None:
            await update.message.reply_text(
                "Неверный формат. Пример: +0.1 или -0,1 или +0.1%",
                reply_markup=_kb_tp_mid()
            )
            return
        if not db_enabled():
            await update.message.reply_text("Сохранение доступно только с БД.", reply_markup=_kb_tp_mid())
            context.user_data.pop("state", None)
            return
        db_set_tp_mid_config(uid, {"delta_pct": val})
        context.user_data.pop("state", None)
        await update.message.reply_text(f"Δ TPmid установлена: {val:+.2f}%", reply_markup=_kb_tp_mid())
        return

    # Обработка кнопок сессий
    if txt == BTN_SESS_START:
        # Сбрасываем состояния
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        if not db_enabled():
            await update.message.reply_text("Сессии работают только с БД.", reply_markup=_kb_main(update.effective_user.id))
            return
        sid = db_session_start(ch)
        await update.message.reply_text("▶️ Сессия открыта.", reply_markup=_kb_main(update.effective_user.id))
        return

    if txt == BTN_SESS_END:
        # Сбрасываем состояния
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        if not db_enabled():
            await update.message.reply_text("Сессии работают только с БД.", reply_markup=_kb_main(update.effective_user.id))
            return
        sid = db_session_end(ch)
        if sid:
            await update.message.reply_text("⏹️ Сессия закрыта.", reply_markup=_kb_main(update.effective_user.id))
        else:
            await update.message.reply_text("Нет открытой сессии.", reply_markup=_kb_main(update.effective_user.id))
        return
    
    # Обработка кнопки BTN_MY_DATA
    if txt == BTN_MY_DATA:
        # Сбрасываем состояния при переходе
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        uid = update.effective_user.id
        pairs = db_user_pairs(uid) if db_enabled() else ["BTCUSDT", "ETHUSDT"]
        tfa = db_user_tfs_analysis(uid) if db_enabled() else ["1h", "4h", "1d"]
        tfs = db_user_tfs_signal(uid) if db_enabled() else ["1h", "4h", "1d"]
        mode = (db_get_settings(uid).get("tf_mode") if db_enabled() else "stacked").upper()
        
        # Форматируем пары для вывода
        pairs_block = "</code>\n<code>".join(pairs) if pairs else "—"
        
        text = (
            "<b>Твои данные</b>\n"
            f"Пары:\n<code>{pairs_block}</code>\n"
            f"ТФ (анализ): <b>{', '.join(tfa) or '—'}</b>\n"
            f"ТФ (сигналы): <b>{', '.join(tfs) or '—'}</b>\n"
            f"Режим сигналов: <b>{mode}</b>\n\n"
        )
        # Добавим отображение текущего TPmid конфига
        try:
            cfg = db_get_tp_mid_config(uid) if db_enabled() else {"enabled": False, "auto": True, "delta_pct": 0.1}
            text += (
                f"TPmid: <b>{'Включен' if cfg.get('enabled') else 'Выключен'}</b> · "
                f"Режим: <b>{'Авто' if cfg.get('auto') else 'Ручной'}</b> · "
                f"Δ: <b>{cfg.get('delta_pct', 0.1):+.2f}%</b>\n\n"
            )
        except Exception:
            pass
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_tp_mid())
        return
    # Подменю TPmid
    if txt == BTN_TPMID_TOGGLE:
        if not db_enabled():
            await update.message.reply_text("Настройки TPmid работают только с БД.", reply_markup=_kb_tp_mid())
            return
        uid = update.effective_user.id
        cfg = db_get_tp_mid_config(uid)
        new_cfg = db_set_tp_mid_config(uid, {"enabled": not bool(cfg.get("enabled", False))})
        await update.message.reply_text(
            f"TPmid теперь: <b>{'Включен' if new_cfg.get('enabled') else 'Выключен'}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_tp_mid()
        )
        return

    if txt == BTN_TPMID_AUTO_TOGGLE:
        if not db_enabled():
            await update.message.reply_text("Настройки TPmid работают только с БД.", reply_markup=_kb_tp_mid())
            return
        uid = update.effective_user.id
        cfg = db_get_tp_mid_config(uid)
        new_cfg = db_set_tp_mid_config(uid, {"auto": not bool(cfg.get("auto", True))})
        await update.message.reply_text(
            f"Авто-режим: <b>{'Включен' if new_cfg.get('auto') else 'Выключен'}</b> (ручная Δ игнорируется при авто)",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_tp_mid()
        )
        return

    if txt == BTN_TPMID_SETTINGS:
        # Входим в режим ввода дельты
        context.user_data["state"] = "tp_mid_delta_input"
        guide = (
            "Введи дельту относительно TP1 в процентах.\n\n"
            "Формат: <code>+0.1</code> или <code>-0.1</code> (можно с запятой, можно с %).\n"
            "Примеры: <code>+0,1</code>, <code>+0.1%</code>, <code>-0,25%</code>.\n"
            "Важно: всегда указывай знак + или -."
        )
        await update.message.reply_text(guide, parse_mode=ParseMode.HTML, reply_markup=_kb_tp_mid())
        return

    if txt == BTN_TPMID_RESET:
        if not db_enabled():
            await update.message.reply_text("Настройки TPmid работают только с БД.", reply_markup=_kb_tp_mid())
            return
        uid = update.effective_user.id
        new_cfg = db_set_tp_mid_config(uid, {"enabled": False, "auto": True, "delta_pct": 0.1})
        await update.message.reply_text(
            "Настройки TPmid сброшены: выключен, авто-режим, Δ=+0.10%",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_tp_mid()
        )
        return
    
    # Обработка кнопки BTN_TIMEZONE
    if txt == BTN_TIMEZONE:
        # Сбрасываем состояния при переходе
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        if not db_enabled():
            await update.message.reply_text("Часовые пояса работают только с БД.", reply_markup=_kb_main(update.effective_user.id))
            return
        
        current_tz = db_get_user_timezone(ch)
        text = f"🌍 Текущий часовой пояс: <b>{current_tz}</b>\n\nВыберите новый часовой пояс:"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_timezone())
        return
    
    # Обработка кнопки BTN_TF_SETTINGS
    if txt == BTN_TF_SETTINGS:
        await _enter_tf_menu(update)
        return
    
    # Обработка кнопки BTN_PAIR_TOGGLE
    if txt == BTN_PAIR_TOGGLE:
        if not db_enabled():
            await update.message.reply_text("Управление парами работает только с БД.", reply_markup=_kb_main(update.effective_user.id))
            return
        context.user_data["state"] = "managing_pairs"
        await cmd_pairs(update, context)
        return
    
    # Обработка кнопок таймфреймов в меню настройки ТФ
    if txt == BTN_TF_ANALYSIS:
        await _tf_master_enter(update, context, "analysis")
        return
    
    if txt == BTN_TF_SIGNAL:
        await _tf_master_enter(update, context, "signal")
        return
    
    if txt == BTN_TF_MODE:
        if not db_enabled():
            await update.effective_message.reply_text("Режим сигналов работает только с БД.")
            return
        uid = update.effective_user.id
        current = db_get_settings(uid).get("tf_mode", "stacked")
        new_mode = "independent" if current == "stacked" else "stacked"
        db_set_tf_mode(uid, new_mode)
        mode_ru = "независимый" if new_mode == "independent" else "объединённый"
        await update.effective_message.reply_text(f"Режим изменён на: {mode_ru}")
        return
    
    # Обработка кнопки BTN_STATS
    if txt == BTN_STATS:
        # Сбрасываем все активные состояния
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        context.user_data.pop("stats_scope", None)
        await update.message.reply_text("Выбери период:", reply_markup=_kb_stats())
        return
    
    # Обработка кнопки BTN_TRADES
    if txt == BTN_TRADES:
        # Сбрасываем состояния
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        try:
            await _show_trades_panel(update, context)
        except Exception as e:
            print(f"[ERROR] _show_trades_panel failed: {e}")
            await update.message.reply_text(f"Ошибка: {e}")
        return
    
    # Обработка кнопки BTN_LEVELS
    if txt == BTN_LEVELS:
        # Сбрасываем состояния
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        try:
            await _show_levels_panel(update, context)
        except Exception as e:
            print(f"[ERROR] _show_levels_panel failed: {e}")
            await update.message.reply_text(f"Ошибка: {e}")
        return
    
    # Обработка кнопки BTN_ADMIN
    if txt == BTN_ADMIN:
        if update.effective_user.id not in ADMIN_USER_IDS:
            await update.message.reply_text("Доступ запрещён.")
            return
        try:
            from admin import show_admin_panel
            await show_admin_panel(update, context)
        except Exception as e:
            print(f"[ERROR] show_admin_panel failed: {e}")
            await update.message.reply_text(f"Ошибка: {e}")
        return
    
    # Обработка состояния "await_tf_flow"
    if context.user_data.get("state") == "await_tf_flow":
        # Проверяем, не является ли это кнопкой главного меню
        # Кнопки главного меню должны обрабатываться всегда, независимо от состояния
        main_menu_buttons = (BTN_TF_DONE, BTN_BACK, BTN_MY_DATA, BTN_STATS, BTN_TRADES, 
                            BTN_LEVELS, BTN_SESS_START, BTN_SESS_END, BTN_TIMEZONE,
                            BTN_PAIR_TOGGLE, BTN_TF_SETTINGS)
        if txt not in main_menu_buttons:
            await _tf_flow_handle(update, context)
            return
        # Если это кнопка главного меню - продолжаем обработку ниже
    
    # Обработка состояния управления парами (ПЕРЕД обработкой BTN_BACK!)
    if context.user_data.get("state") == "managing_pairs":
        if txt == "✅ НАСТРОЙКА ЗАВЕРШЕНА":
            context.user_data.pop("state", None)
            await update.message.reply_text("Настройка завершена.", reply_markup=_kb_main(update.effective_user.id))
            return
        
        if txt == "🔙 НАЗАД":
            context.user_data.pop("state", None)
            await update.message.reply_text("Отменено.", reply_markup=_kb_main(update.effective_user.id))
            return
        
        # Проверяем кнопки главного меню - они должны работать всегда
        main_menu_buttons = (BTN_MY_DATA, BTN_STATS, BTN_TRADES, BTN_LEVELS, 
                            BTN_SESS_START, BTN_SESS_END, BTN_TIMEZONE, BTN_TF_SETTINGS)
        if txt in main_menu_buttons:
            # Сбрасываем состояние и продолжаем обработку кнопки ниже
            context.user_data.pop("state", None)
            # Обработка кнопки будет ниже по коду (не возвращаемся, продолжаем)
        else:
            # Обработка ввода символа пары
            sym = txt.replace("/", "").upper().strip()
            # Автоматически добавляем USDT если не указано
            if not sym.endswith("USDT"):
                sym = sym + "USDT"
            
            uid = update.effective_user.id
            pairs = db_user_pairs(uid) if db_enabled() else []
            
            if sym in pairs:
                db_rm_pair(uid, sym)
                pairs = db_user_pairs(uid)
                text = f"Удалил {sym}.\n\n"
            else:
                db_add_pair(uid, sym)
                pairs = db_user_pairs(uid)
                text = f"Добавил {sym}.\n\n"
            
            if pairs:
                text += "<b>Текущие пары:</b> " + ", ".join([f"<code>{p}</code>" for p in pairs])
            else:
                text += "<b>Текущие пары:</b> —"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
    
    # Обработка BTN_BACK (теперь ПОСЛЕ проверки состояний)
    if txt == BTN_BACK:
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        await update.effective_message.reply_text(
            "Ок, вернул в главное меню.",
            reply_markup=_kb_main(update.effective_user.id)
        )
        return
    
    # Обработка BTN_TF_DONE
    if txt == BTN_TF_DONE:
        context.user_data.pop("state", None)
        context.user_data.pop("tf_kind", None)
        await update.effective_message.reply_text("Настройка сохранена.", reply_markup=_kb_main(update.effective_user.id))
        return
    
    # ===== СТАТИСТИКА ПО ПЕРИОДАМ =====
    if txt in (BTN_STATS_D, BTN_STATS_W, BTN_STATS_M):
        ch = update.effective_chat.id
        
        # Получаем локальное время пользователя
        user_tz_name = db_get_user_timezone(ch)
        try:
            user_tz = pytz.timezone(user_tz_name)
            now_local = datetime.now(user_tz)
        except:
            user_tz = pytz.UTC
            now_local = datetime.now(user_tz)
        
        if txt == BTN_STATS_D:
            start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            since_ms = int(start_of_day.timestamp() * 1000)
        elif txt == BTN_STATS_W:
            days_since_monday = now_local.weekday()
            start_of_week = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
            since_ms = int(start_of_week.timestamp() * 1000)
        else:
            start_of_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            since_ms = int(start_of_month.timestamp() * 1000)

        scope = context.user_data.get("stats_scope")
        sigs = db_get_signals_with_events(ch, since_ms) if (db_enabled() and scope != "user") else []
        
        if db_enabled() and scope == "user":
            # User trades statistics
            with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    select 
                        count(*) filter (where status='CLOSED') as closed_total,
                        count(*) filter (where status='CLOSED' and outcome='TP') as tp_count,
                        count(*) filter (where status='CLOSED' and outcome='SL') as sl_count,
                        count(*) filter (where status='CLOSED' and outcome='MANUAL') as manual_count,
                        coalesce(sum(pnl_abs),0) as pnl_sum,
                        coalesce(avg(pnl_abs),0) as pnl_avg,
                        count(*) filter (where status!='CANCELLED') as total_ex_cancelled
                      from user_trades
                     where user_id=%s 
                       and status != 'CANCELLED'
                       and coalesce(closed_at, opened_at) >= to_timestamp(%s/1000.0)
                """, (ch, since_ms))
                agg = cur.fetchone()
            
            closed_total = int(agg["closed_total"] or 0)
            tp = int(agg["tp_count"] or 0)
            sl = int(agg["sl_count"] or 0)
            manual_count = int(agg["manual_count"] or 0)
            pnl_sum = float(agg["pnl_sum"] or 0)
            pnl_avg = float(agg["pnl_avg"] or 0)
            total_ex_cancelled = int(agg["total_ex_cancelled"] or 0)
            
            tp_sl_total = tp + sl
            if tp_sl_total > 0:
                winrate = round(100.0 * tp / tp_sl_total, 2)
                winrate_str = f"Winrate (TP/SL): <b>{winrate}%</b>"
            else:
                winrate_str = "Winrate (TP/SL): <b>—</b>"
            
            parts = [
                f"<b>Ваши сделки — {txt.lower()}</b>",
                f"Сделок (без CANCELLED): <b>{total_ex_cancelled}</b>",
                f"🟢 TP: <b>{tp}</b>   🔴 SL: <b>{sl}</b>   🟡 MANUAL: <b>{manual_count}</b>",
                winrate_str,
                f"PnL суммарно: <b>{pnl_sum:.2f} USDT</b>",
                f"PnL средний/сделку: <b>{pnl_avg:.2f} USDT</b>",
            ]
            text = "\n".join(parts)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_stats())
            context.user_data["stats_since_ms"] = since_ms
            context.user_data["stats_period_name"] = txt
            return
        
        if not sigs:
            await update.message.reply_text("За период сигналов нет.", reply_markup=_kb_stats())
            return

        # Обновляем события для всех сигналов
        for s in sigs:
            has_events = bool(s.get("hit_tp1_ts")) or bool(s.get("hit_tp2_ts")) or bool(s.get("hit_sl_ts"))
            if not has_events:
                events = _resolve_all_events_for_signal(s)
                s.update(events)
        # TPmid: использовать из БД, если есть, иначе рассчитать при необходимости (перс.-настройки)
        uid = update.effective_user.id
        tp_mid_cfg = db_get_tp_mid_config(uid) if db_enabled() else {"enabled": False, "auto": True, "delta_pct": 0.1}
        
        # Проверяем наличие колонок TPmid в БД
        has_tp_mid_columns = False
        if tp_mid_cfg.get('enabled') and db_enabled():
            try:
                from database import _db_conn
                with _db_conn() as c, c.cursor() as cur:
                    cur.execute("""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name = 'signals_log' 
                          AND column_name = 'hit_tp_mid_price'
                    """)
                    has_tp_mid_columns = cur.fetchone() is not None
            except Exception:
                has_tp_mid_columns = False
        
        if tp_mid_cfg.get('enabled') and has_tp_mid_columns:
            from handlers.message_render import _tp_mid_price_for_signal
            for s in sigs:
                try:
                    # Если TPmid уже есть в БД, используем его
                    if s.get('hit_tp_mid_ts') and s.get('hit_tp_mid_price'):
                        continue
                    
                    mid_price = _tp_mid_price_for_signal(s, tp_mid_cfg)
                    if not mid_price:
                        continue
                    
                    # Оптимизация: если сигнал достиг TP2, то TPmid (между TP1 и TP2) точно был достигнут
                    tp1_ts = s.get('hit_tp1_ts')
                    tp2_ts = s.get('hit_tp2_ts')
                    entry = float(s.get('entry') or s.get('entry_price') or 0.0)
                    tp1 = float(s.get('tp1') or 0.0)
                    tp2 = float(s.get('tp2') or 0.0)
                    side = str(s.get('side', 'LONG')).upper()
                    
                    mid_ts = None
                    
                    # Проверяем, находится ли TPmid между TP1 и TP2
                    if entry > 0 and tp1 > 0 and tp2 > 0:
                        if side == 'LONG':
                            tp_mid_between = tp1 < mid_price < tp2
                        else:  # SHORT
                            tp_mid_between = tp2 < mid_price < tp1
                        
                        # Если TP2 достигнут и TPmid между TP1 и TP2, то TPmid был достигнут
                        if tp2_ts and tp1_ts and tp_mid_between:
                            # Сначала пытаемся найти точный момент через свечи
                            mid_ts = _check_price_level_hit(s, mid_price)
                            # Если не нашли через свечи, но TP2 достигнут, значит TPmid был достигнут
                            if not mid_ts:
                                # TPmid был достигнут между TP1 и TP2, используем среднее время
                                tp1_ts_int = int(tp1_ts)
                                tp2_ts_int = int(tp2_ts)
                                mid_ts = (tp1_ts_int + tp2_ts_int) // 2  # Среднее время между TP1 и TP2
                        elif tp1_ts:
                            # Если TP1 достигнут, но не TP2, проверяем через свечи
                            mid_ts = _check_price_level_hit(s, mid_price)
                        else:
                            # Если TP1 не достигнут, проверяем через свечи
                            mid_ts = _check_price_level_hit(s, mid_price)
                    else:
                        # Обычная проверка через свечи
                        mid_ts = _check_price_level_hit(s, mid_price)
                    
                    # Сохраняем только если нашли
                    if mid_ts:
                        s['hit_tp_mid_price'] = mid_price
                        s['hit_tp_mid_ts'] = mid_ts
                except Exception as e:
                    print(f"[TPMID] Error calculating TPmid for signal {s.get('id')}: {e}")
                    pass

        metrics = _calculate_stats_metrics(sigs, tp_mid_cfg if tp_mid_cfg.get('enabled') and has_tp_mid_columns else None)
        
        # TPmid метрики теперь считаются через пути в _calculate_stats_metrics
        # n_tpmid_open = TP1 → TPmid → OPEN
        # n_mid_be = TPmid → TP1 = BE
        # n_mid = счетчик касаний TP1 и TPmid (независимо от финального исхода)
        # Метрики уже рассчитаны в _calculate_stats_metrics

        # Расчёт Итога дня (Equity от $N, 1×)
        equity_line = None
        try:
            from config.config import CONFIG
            from database.database import db_enabled as _db_enabled
            if _db_enabled():
                from database.database import db_get_closed_trades_with_fills
                from stats.equity import Trade, Leg, day_equity_result

                stats_cfg = CONFIG.get("stats", {})
                start_equity = float(stats_cfg.get("equity_start_usd", 100.0))
                include_commissions = bool(stats_cfg.get("include_commissions", True))
                include_funding = bool(stats_cfg.get("include_funding", False))

                raw_trades = db_get_closed_trades_with_fills(ch, since_ms)
                trades: list[Trade] = []
                for t in raw_trades:
                    legs = [
                        Leg(
                            type=lg.get("type", ""),
                            price=float(lg.get("price", 0.0)),
                            qty_frac=float(lg.get("qty_frac", 0.0)),
                            time=float(lg.get("time", 0.0)),
                        )
                        for lg in t.get("legs", [])
                        if float(lg.get("qty_frac", 0.0)) > 0
                    ]
                    fee_pct = float(t.get("fee_round_pct", 0.0)) if include_commissions else 0.0
                    funding_pct = 0.0  # пока не учитываем
                    if include_funding:
                        funding_pct = float(t.get("funding_paid_pct", 0.0) or 0.0)
                    trades.append(
                        Trade(
                            entry_price=float(t.get("entry_price", 0.0)),
                            side=str(t.get("side", "long")),
                            legs=legs,
                            fee_round_pct=fee_pct,
                            funding_paid_pct=funding_pct,
                            close_time=float(t.get("close_time", 0.0)),
                        )
                    )

                # Если реальных сделок нет — синтетика по сигналам
                if not trades:
                    from stats.equity import Trade, Leg
                    tp1_share = float(CONFIG.get("position", {}).get("tp1_close_share", 0.7))
                    # Построим ножки по итогам сигналов
                    for s in sigs:
                        side = str(s.get("side", "long")).lower()
                        entry = float(s.get("entry_price") or s.get("entry") or 0.0)
                        tp1 = float(s.get("tp1") or 0.0)
                        tp2 = float(s.get("tp2") or 0.0)
                        sl  = float(s.get("sl") or 0.0)
                        path = _determine_signal_path(s, tp_mid_cfg if tp_mid_cfg.get('enabled') and has_tp_mid_columns else None)
                        legs: list[Leg] = []
                        close_ts = 0.0
                        if path == "TP1_PLUS_TP2":
                            if tp1 and tp2 and entry:
                                legs.append(Leg(type="TP1", price=tp1, qty_frac=tp1_share, time=float(s.get("hit_tp1_ts") or 0.0)))
                                legs.append(Leg(type="TP2", price=tp2, qty_frac=1.0 - tp1_share, time=float(s.get("hit_tp2_ts") or 0.0)))
                                close_ts = float(s.get("hit_tp2_ts") or 0.0)
                        elif path == "TP1_TO_SL":
                            if tp1 and sl and entry:
                                price1 = tp1
                                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and s.get('hit_tp_mid_ts'):
                                    price1 = float(s.get('hit_tp_mid_price') or tp1)
                                legs.append(Leg(type="TP1", price=price1, qty_frac=tp1_share, time=float(s.get("hit_tp1_ts") or 0.0)))
                                legs.append(Leg(type="SL",  price=sl,  qty_frac=1.0 - tp1_share, time=float(s.get("hit_sl_ts") or 0.0)))
                                close_ts = float(s.get("hit_sl_ts") or 0.0)
                        elif path == "TPMID_TO_TP1_BE":
                            if tp1 and entry:
                                # Безубыток: закрываем по TP1 (цена вернулась к TP1 после TPmid)
                                legs.append(Leg(type="TP1", price=tp1, qty_frac=1.0, time=float(s.get("hit_tp1_ts") or 0.0)))
                                close_ts = float(s.get("hit_tp1_ts") or 0.0)
                        elif path == "SL_BEFORE_TP1":
                            if sl and entry:
                                legs.append(Leg(type="SL", price=sl, qty_frac=1.0, time=float(s.get("hit_sl_ts") or 0.0)))
                                close_ts = float(s.get("hit_sl_ts") or 0.0)
                        else:
                            # OPEN, TP1→OPEN, TP1→TPMID пропускаем
                            continue

                        if not legs or entry <= 0:
                            continue

                        fee_pct = 0.0
                        if include_commissions:
                            fee_pct = 0.0  # синтетически комиссии можно настроить отдельно

                        trades.append(
                            Trade(
                                entry_price=entry,
                                side=side if side in ("long", "short") else "long",
                                legs=legs,
                                fee_round_pct=fee_pct,
                                funding_paid_pct=0.0,
                                close_time=close_ts,
                            )
                        )

                # сортировка по close_time
                trades.sort(key=lambda tr: tr.close_time)
                r_day = day_equity_result(trades, start_equity)
                
                # Определяем название периода для equity
                if txt == BTN_STATS_D:
                    period_equity = "дня"
                elif txt == BTN_STATS_W:
                    period_equity = "недели"
                elif txt == BTN_STATS_M:
                    period_equity = "месяца"
                else:
                    period_equity = "периода"
                
                equity_line = f"🟣 Итог {period_equity} (Equity от ${int(start_equity)}, 1×): <b>{r_day*100:+.2f}%</b>"
        except Exception as e:
            equity_line = None
        
        period_name = txt.replace("День", "День").replace("Неделя", "Неделя").replace("Месяц", "Месяц")
        
        # Получаем EOD-статистику для дня (только для дневной статистики)
        eod_stats = None
        if txt == BTN_STATS_D and db_enabled():
            try:
                from database import db_get_eod_closed_stats
                # Рассчитываем границы дня
                day_end = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
                day_end_ts = int(day_end.timestamp() * 1000)
                eod_stats = db_get_eod_closed_stats(ch, since_ms, day_end_ts)
            except Exception as e:
                print(f"[STATS] Error getting EOD stats: {e}")
                eod_stats = None
        
        # Формируем заголовок с датой для дня
        if txt == BTN_STATS_D:
            date_str = now_local.strftime("%d.%m.%Y")
            header = f"📊 <b>Статистика — {period_name}</b> ({date_str})"
        else:
            header = f"📊 <b>Статистика — {period_name}</b>"
        
        text_parts = [
            header,
            "",
            f"Всего сигналов: <b>{metrics['total']}</b>",
            f"Рассчитано исходов: <b>{metrics['resolved']}</b>    ⏳ Открыто: <b>{metrics['open']}</b>",
            "",
            f"⚪️ TP1 → OPEN: <b>{metrics['n4']}</b> (<b>{metrics['pct4']:.2f}%</b>)",
            *(
                [f"⚪️ TP1 + TPmid → OPEN: <b>{metrics['n_tpmid_open']}</b> (<b>{metrics['pct_tpmid_open']:.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and 'n_tpmid_open' in metrics else []
            ),
            "",
            "Пути исходов:",
            "",
            (
                f"🟡 TP1: <b>{metrics['tp1_touch_cnt']}</b> "
                f"(<b>{(metrics['tp1_touch_cnt']/metrics['total']*100 if metrics['total']>0 else 0):.2f}%</b>)"
            ),
            *(
                [f"🟠 TP1+TPmid: <b>{metrics['n_mid']}</b> (<b>{metrics['pct_mid']:.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and 'n_mid' in metrics else []
            ),
            *(
                [f"🟢 TP1+TPmid+TP2: <b>{metrics['n2_with_tpmid']}</b> (<b>{(metrics['n2_with_tpmid']/metrics['total']*100 if metrics['total']>0 else 0):.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and metrics.get('n2_with_tpmid', 0) > 0 else []
            ),
            *(
                [f"🟢 TP1+TP2: <b>{metrics['n2']}</b> (<b>{metrics['pct2']:.2f}%</b>)"]
                if not (tp_mid_cfg.get('enabled') and has_tp_mid_columns and metrics.get('n2_with_tpmid', 0) > 0) else []
            ),
            "",
            f"🔻 SL до TP1: <b>{metrics['n1']}</b> (<b>{metrics['pct1']:.2f}%</b>)",
            *(
                [f"🔻 SL до TPmid: <b>{metrics['n_sl_before_tpmid']}</b> (<b>{metrics['pct_sl_before_tpmid']:.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and 'n_sl_before_tpmid' in metrics else []
            ),
            "",
            f"🔙 TP1 → SL: <b>{metrics['n3']}</b> (<b>{metrics['pct3']:.2f}%</b>)",
            *(
                [f"⭕️ TPmid → TP1 = BE: <b>{metrics['n_mid_be']}</b> (<b>{metrics['pct_mid_be']:.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and 'n_mid_be' in metrics else []
            ),
            *(
                [f"🔙 TPmid → SL: <b>{metrics['n_tpmid_to_sl']}</b> (<b>{metrics['pct_tpmid_to_sl']:.2f}%</b>)"]
                if tp_mid_cfg.get('enabled') and has_tp_mid_columns and 'n_tpmid_to_sl' in metrics else []
            ),
        ]
        
        # Добавляем блок "Закрыли сделки" для дневной статистики
        if txt == BTN_STATS_D and eod_stats and (eod_stats.get('n_be', 0) > 0 or eod_stats.get('n_below_entry', 0) > 0):
            total_eod = eod_stats.get('n_be', 0) + eod_stats.get('n_below_entry', 0)
            text_parts.extend([
                "",
                "Закрыли сделки:",
                "",
            ])
            if eod_stats.get('n_be', 0) > 0:
                pct_be = (eod_stats['n_be'] / metrics['total'] * 100) if metrics['total'] > 0 else 0.0
                text_parts.append(f"◻️ BE: <b>{eod_stats['n_be']}</b> (<b>{pct_be:.2f}%</b>)")
            if eod_stats.get('n_below_entry', 0) > 0:
                pct_below = (eod_stats['n_below_entry'] / metrics['total'] * 100) if metrics['total'] > 0 else 0.0
                text_parts.append(f"🔺 Ниже входа: <b>{eod_stats['n_below_entry']}</b> (<b>{pct_below:.2f}%</b>)")
        
        text_parts.append("")
        
        if equity_line:
            text_parts.append(equity_line)
        text = "\n".join(text_parts)
        
        keyboard = [
            [
                InlineKeyboardButton("📥 Сигналы (CSV)", callback_data="stats:csv=signals"),
                InlineKeyboardButton("📥 Сделки (CSV)", callback_data="stats:csv=trades")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        context.user_data["stats_since_ms"] = since_ms
        context.user_data["stats_period_name"] = period_name
        return

    # ===== СТАТИСТИКА ПО СИМВОЛАМ =====
    if txt == BTN_STATS_BY_SYMBOL:
        if not db_enabled():
            await update.message.reply_text("Анализ доступен только с БД.", reply_markup=_kb_stats())
            return
        
        since_ms = context.user_data.get("stats_since_ms")
        period_name = context.user_data.get("stats_period_name", "")
        if not since_ms:
            await update.message.reply_text("Сначала выбери период (День/Неделя/Месяц).", reply_markup=_kb_stats())
            return
        
        scope = context.user_data.get("stats_scope")
        if scope == "user":
            with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT symbol,
                           COUNT(*) FILTER (WHERE status!='CANCELLED') total,
                           COUNT(*) FILTER (WHERE outcome='TP') tp_count,
                           COUNT(*) FILTER (WHERE outcome='SL') sl_count
                      FROM user_trades
                     WHERE user_id=%s AND opened_at >= to_timestamp(%s/1000.0)
                     GROUP BY symbol ORDER BY total DESC
                """, (ch, since_ms))
                stats = [dict(r) for r in cur.fetchall()]
            
            text = "📊 <b>Ваши сделки по символам</b>\n\n"
            for stat in stats:
                total = stat['tp_count'] + stat['sl_count']
                winrate = (stat['tp_count'] / total * 100) if total > 0 else 0
                text += f"{stat['symbol']}: WR {winrate:.1f}% (TP/SL {stat['tp_count']}/{total})\n"
            
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_stats())
            return
        
        sigs = db_get_signals_with_events(ch, since_ms)
        if not sigs:
            await update.message.reply_text("За период сигналов нет.", reply_markup=_kb_stats())
            return
        
        # Получаем настройки TPmid
        tp_mid_cfg = db_get_tp_mid_config(ch) if db_enabled() else {"enabled": False, "auto": True, "delta_pct": 0.1}
        
        for s in sigs:
            has_events = bool(s.get("hit_tp1_ts")) or bool(s.get("hit_tp2_ts")) or bool(s.get("hit_sl_ts"))
            if not has_events:
                events = _resolve_all_events_for_signal(s, tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
                db_update_signal_events(s["id"], events)
                s.update(events)
        
        # Рассчитываем TPmid события если включен
        if tp_mid_cfg.get('enabled'):
            from handlers.message_render import _tp_mid_price_for_signal
            for s in sigs:
                try:
                    mid_price = _tp_mid_price_for_signal(s, tp_mid_cfg)
                    if mid_price:
                        mid_ts = _check_price_level_hit(s, mid_price)
                        s['hit_tp_mid_price'] = mid_price
                        s['hit_tp_mid_ts'] = mid_ts
                except Exception:
                    pass
        
        group_stats = _calculate_group_stats(sigs, 'symbol', tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
        context.user_data["group_stats"] = group_stats
        context.user_data["group_type"] = "symbol"
        context.user_data["group_page"] = 1
        context.user_data["group_tp_mid_config"] = tp_mid_cfg
        
        text, total_pages = _format_group_stats_text(group_stats, 'symbol', period_name, page=1, per_page=10, tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
        
        keyboard = []
        if total_pages > 1:
            nav_row = []
            if 1 < total_pages:
                nav_row.append(InlineKeyboardButton("▶️", callback_data="stats_group:page=2"))
            nav_row.append(InlineKeyboardButton(f"Стр. 1/{total_pages}", callback_data="stats_group:noop"))
            keyboard.append(nav_row)
        
        keyboard.append([InlineKeyboardButton("📥 CSV", callback_data="stats_group:csv=symbol")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return

    # ===== СТАТИСТИКА ПО ТАЙМФРЕЙМАМ =====
    if txt == BTN_STATS_BY_TF:
        if not db_enabled():
            await update.message.reply_text("Анализ доступен только с БД.", reply_markup=_kb_stats())
            return
        
        since_ms = context.user_data.get("stats_since_ms")
        period_name = context.user_data.get("stats_period_name", "")
        if not since_ms:
            await update.message.reply_text("Сначала выбери период (День/Неделя/Месяц).", reply_markup=_kb_stats())
            return
        
        scope = context.user_data.get("stats_scope")
        if scope == "user":
            with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT timeframe,
                           COUNT(*) FILTER (WHERE status!='CANCELLED') total,
                           COUNT(*) FILTER (WHERE outcome='TP') tp_count,
                           COUNT(*) FILTER (WHERE outcome='SL') sl_count
                      FROM user_trades
                     WHERE user_id=%s AND opened_at >= to_timestamp(%s/1000.0)
                     GROUP BY timeframe ORDER BY total DESC
                """, (ch, since_ms))
                stats = [dict(r) for r in cur.fetchall()]
            
            text = "⏰ <b>Ваши сделки по таймфреймам</b>\n\n"
            for stat in stats:
                total = stat['tp_count'] + stat['sl_count']
                winrate = (stat['tp_count'] / total * 100) if total > 0 else 0
                text += f"{stat['timeframe']}: WR {winrate:.1f}% (TP/SL {stat['tp_count']}/{total})\n"
            
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_kb_stats())
            return
        
        sigs = db_get_signals_with_events(ch, since_ms)
        if not sigs:
            await update.message.reply_text("За период сигналов нет.", reply_markup=_kb_stats())
            return
        
        # Получаем настройки TPmid
        tp_mid_cfg = db_get_tp_mid_config(ch) if db_enabled() else {"enabled": False, "auto": True, "delta_pct": 0.1}
        
        for s in sigs:
            has_events = bool(s.get("hit_tp1_ts")) or bool(s.get("hit_tp2_ts")) or bool(s.get("hit_sl_ts"))
            if not has_events:
                events = _resolve_all_events_for_signal(s, tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
                db_update_signal_events(s["id"], events)
                s.update(events)
        
        # Рассчитываем TPmid события если включен
        if tp_mid_cfg.get('enabled'):
            from handlers.message_render import _tp_mid_price_for_signal
            for s in sigs:
                try:
                    mid_price = _tp_mid_price_for_signal(s, tp_mid_cfg)
                    if mid_price:
                        mid_ts = _check_price_level_hit(s, mid_price)
                        s['hit_tp_mid_price'] = mid_price
                        s['hit_tp_mid_ts'] = mid_ts
                except Exception:
                    pass
        
        group_stats = _calculate_group_stats(sigs, 'timeframe', tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
        context.user_data["group_stats"] = group_stats
        context.user_data["group_type"] = "timeframe"
        context.user_data["group_page"] = 1
        context.user_data["group_tp_mid_config"] = tp_mid_cfg
        
        text, total_pages = _format_group_stats_text(group_stats, 'timeframe', period_name, page=1, per_page=10, tp_mid_config=tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
        
        keyboard = []
        if total_pages > 1:
            nav_row = []
            if 1 < total_pages:
                nav_row.append(InlineKeyboardButton("▶️", callback_data="stats_group:page=2"))
            nav_row.append(InlineKeyboardButton(f"Стр. 1/{total_pages}", callback_data="stats_group:noop"))
            keyboard.append(nav_row)
        
        keyboard.append([InlineKeyboardButton("📥 CSV", callback_data="stats_group:csv=timeframe")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return

    # ===== СТАТИСТИКА ПО СЕССИЯМ =====
    if txt == BTN_STATS_BY_SESSION:
        if not db_enabled():
            await update.message.reply_text("Анализ доступен только с БД.", reply_markup=_kb_stats())
            return
        
        since_ms = context.user_data.get("stats_since_ms")
        period_name = context.user_data.get("stats_period_name", "")
        if not since_ms:
            await update.message.reply_text("Сначала выбери период (День/Неделя/Месяц).", reply_markup=_kb_stats())
            return
        
        sigs = db_get_signals_with_events(ch, since_ms)
        if not sigs:
            await update.message.reply_text("За период сигналов нет.", reply_markup=_kb_stats())
            return
        
        for s in sigs:
            if s.get("hit_tp1_ts") is None and s.get("hit_tp2_ts") is None and s.get("hit_sl_ts") is None:
                events = _resolve_all_events_for_signal(s)
                db_update_signal_events(s["id"], events)
                s.update(events)
        
        user_tz_name = db_get_user_timezone(ch)
        _add_session_to_signals(sigs, user_tz_name)
        
        group_stats = _calculate_group_stats(sigs, 'session')
        text, total_pages = _format_group_stats_text(group_stats, 'session', period_name)
        
        keyboard = [[InlineKeyboardButton("📥 CSV", callback_data="stats_group:csv=session")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        context.user_data["group_stats"] = group_stats
        context.user_data["group_type"] = "session"
        
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return

    # ===== ОЧИСТКА СТАТИСТИКИ =====
    if txt == BTN_CLEAR_STATS:
        await update.message.reply_text("Выбери период для очистки:", reply_markup=_kb_clear_stats())
        return

    if txt in (BTN_CLEAR_DAY, BTN_CLEAR_WEEK, BTN_CLEAR_MONTH):
        if not db_enabled():
            await update.message.reply_text("Очистка доступна только с БД.", reply_markup=_kb_clear_stats())
            return
        
        user_tz_name = db_get_user_timezone(ch)
        try:
            user_tz = pytz.timezone(user_tz_name)
            now_local = datetime.now(user_tz)
        except:
            user_tz = pytz.UTC
            now_local = datetime.now(user_tz)
        
        if txt == BTN_CLEAR_DAY:
            start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            since_ms = int(start_of_day.timestamp() * 1000)
            period_name = "день"
        elif txt == BTN_CLEAR_WEEK:
            days_since_monday = now_local.weekday()
            start_of_week = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
            since_ms = int(start_of_week.timestamp() * 1000)
            period_name = "неделя"
        else:
            start_of_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            since_ms = int(start_of_month.timestamp() * 1000)
            period_name = "месяц"
        
        db_clear_signals(ch, since_ms)
        await update.message.reply_text(f"Статистика за {period_name} очищена.", reply_markup=_kb_main(update.effective_user.id))
        return

    # ===== ОБРАБОТКА СОСТОЯНИЙ ВВОДА =====
    
    # Ожидание нового значения по умолчанию для entry_value
    if context.user_data.get("await_entry_value_default"):
        raw_value = txt_raw.replace(" ", "").replace(",", ".")
        try:
            value = float(raw_value)
        except Exception:
            await update.message.reply_text("Введи число (например: 50 или 0.05)")
            return
        if not (value > 0):
            await update.message.reply_text("Значение должно быть > 0.")
            return
        
        uid = update.effective_user.id
        db_set_entry_mode_prefs(uid, value=value)
        context.user_data.pop("await_entry_value_default", None)
        
        await update.message.reply_text(
            f"Значение по умолчанию изменено на {value} ✅",
            reply_markup=_kb_main(uid)
        )
        return
    
    # NEW ENTRY FLOW: ожидание значения (сумма или количество)
    if context.user_data.get("await_entry_value"):
        raw_value = txt_raw.replace(" ", "").replace(",", ".")
        try:
            value = float(raw_value)
        except Exception:
            await update.message.reply_text("Введи число (например: 50 или 0.05)")
            return
        if not (value > 0):
            await update.message.reply_text("Значение должно быть > 0.")
            return
        
        entry_ctx = context.user_data.get("entry_context", {})
        entry_ctx["entry_value"] = value
        context.user_data["entry_context"] = entry_ctx
        context.user_data.pop("await_entry_value", None)
        
        # Теперь запрашиваем ПЛЕЧО
        context.user_data["await_entry_leverage"] = True
        await update.message.reply_text(
            "Какое плечо используешь? (например: 10, 20, 50):",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton("10"), KeyboardButton("20"), KeyboardButton("50")],
                          [KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return
    
    # NEW ENTRY FLOW: ожидание плеча
    if context.user_data.get("await_entry_leverage"):
        raw_lev = txt_raw.replace(" ", "").replace(",", ".")
        try:
            leverage = int(float(raw_lev))
        except Exception:
            await update.message.reply_text("Введи число для плеча (например: 10, 20, 50)")
            return
        if not (leverage > 0 and leverage <= 125):
            await update.message.reply_text("Плечо должно быть от 1 до 125.")
            return
        
        entry_ctx = context.user_data.get("entry_context", {})
        entry_ctx["leverage"] = leverage
        context.user_data["entry_context"] = entry_ctx
        context.user_data.pop("await_entry_leverage", None)
        
        # Теперь запрашиваем цену входа
        context.user_data["await_entry_price_new"] = True
        await update.message.reply_text(
            "Введи точную цену входа (например: 4519.30):",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(BTN_BACK)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return
    
    # NEW ENTRY FLOW: ожидание цены входа
    if context.user_data.get("await_entry_price_new"):
        raw_price = txt_raw.replace(" ", "").replace(",", ".")
        try:
            price = float(raw_price)
        except Exception:
            await update.message.reply_text("Введи число для цены входа (например: 4519.30)")
            return
        if not (price > 0):
            await update.message.reply_text("Цена должна быть > 0.")
            return
        
        entry_ctx = context.user_data.get("entry_context", {})
        entry_ctx["entry_price"] = price
        
        # Рассчитываем entry_qty и entry_notional
        entry_mode = entry_ctx.get("entry_mode", "USDT")
        entry_value = entry_ctx.get("entry_value", 50)
        
        if entry_mode == "USDT":
            # Режим по сумме: entry_value - это USDT
            entry_notional = entry_value
            entry_qty = entry_notional / price
        else:
            # Режим по количеству: entry_value - это количество монет
            entry_qty = entry_value
            entry_notional = entry_qty * price
        
        entry_ctx["entry_qty"] = entry_qty
        entry_ctx["entry_notional"] = entry_notional
        context.user_data["entry_context"] = entry_ctx
        context.user_data.pop("await_entry_price_new", None)
        
        # Показываем предпросмотр (кнопка подтверждения - callback entry:confirm)
        signal_id = entry_ctx.get("signal_id")
        leverage = entry_ctx.get("leverage", 10)
        
        text = (
            "Проверь данные перед входом:\n\n"
            f"Режим: {'💵 По сумме (USDT)' if entry_mode == 'USDT' else '🪙 По количеству'}\n"
            f"Запрошено: {entry_value}\n"
            f"Плечо: {leverage}x\n"
            f"Цена входа: {price:.4f}\n\n"
            f"→ Количество: {entry_qty:.8f} монет\n"
            f"→ Стоимость: {entry_notional:.2f} USDT\n\n"
            "Подтверди вход или отмени:"
        )
        
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить вход", callback_data=f"entry:confirm")],
            [InlineKeyboardButton("❌ Отменить", callback_data=f"entry:cancel")]
        ]
        
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ===== ОБРАБОТКА ЧАСТИЧНОГО ЗАКРЫТИЯ =====
    
    # Обработка ввода цены закрытия для частичного закрытия
    close_ctx = context.user_data.get("close_trade_context")
    if close_ctx and close_ctx.get("exit_price") is None:
        # User is entering exit price
        raw_price = txt_raw.replace(" ", "").replace(",", ".")
        try:
            exit_price = float(raw_price)
        except Exception:
            await update.message.reply_text("Введите число для цены закрытия, например: 4486.00")
            return
        if not (exit_price > 0):
            await update.message.reply_text("Цена должна быть > 0.")
            return
        
        # Update context with exit price
        close_ctx["exit_price"] = exit_price
        context.user_data["close_trade_context"] = close_ctx
        
        # If it's full close, go directly to confirmation
        if close_ctx["close_type"] == "full":
            from panels import _show_close_confirmation
            
            remaining_qty = close_ctx["remaining_qty"]
            entry_price = close_ctx["entry_price"]
            
            await _show_close_confirmation(update, context, close_ctx["trade_id"], "full", exit_price, "qty", remaining_qty)
        else:
            # For custom price, ask for size selection
            from panels import _show_close_size_selection
            await _show_close_size_selection(update, context, close_ctx["trade_id"], close_ctx["close_type"], exit_price)
        return
    
    # Обработка ввода размера закрытия
    if close_ctx and "size_mode" in close_ctx:
        from panels import _show_close_confirmation
        
        size_mode = close_ctx["size_mode"]
        raw_value = txt_raw.replace(" ", "").replace(",", ".")
        try:
            size_value = float(raw_value)
        except Exception:
            if size_mode == "amount":
                await update.message.reply_text("Введите сумму в USDT, например: 500")
            else:
                await update.message.reply_text("Введите количество, например: 0.1")
            return
        
        if not (size_value > 0):
            await update.message.reply_text("Размер должен быть > 0.")
            return
        
        # Validate size against remaining
        remaining_qty = close_ctx["remaining_qty"]
        entry_price = close_ctx["entry_price"]
        remaining_amount_usdt = remaining_qty * entry_price
        
        if size_mode == "amount":
            if size_value > remaining_amount_usdt:
                await update.message.reply_text(f"Недостаточно средств. Доступно: {remaining_amount_usdt:,.2f} USDT")
                return
        else:  # qty
            if size_value > remaining_qty:
                await update.message.reply_text(f"Недостаточно количества. Доступно: {remaining_qty:.8f}")
                return
        
        # Show confirmation
        await _show_close_confirmation(update, context, close_ctx["trade_id"], close_ctx["close_type"], close_ctx["exit_price"], size_mode, size_value)
        return

    # ===== LEVELS ADD/IMPORT =====
    
    # Добавление уровней вручную
    if context.user_data.get("levels_add_ctx"):
        ctx = context.user_data.pop("levels_add_ctx")
        symbol = ctx["symbol"]
        tf = ctx["tf"]
        import re
        raw = re.split(r"[\s,]+", txt_raw.replace("\n", " ").strip())
        levels = []
        for token in raw:
            if not token:
                continue
            token = token.replace(",", ".")
            try:
                levels.append(float(token))
            except Exception:
                pass
        if not levels:
            await update.message.reply_text("Не нашёл числовых уровней.", reply_markup=_kb_main(ch))
            return
        db_add_levels(ch, symbol, tf, levels, None)
        await update.message.reply_text(f"Сохранил уровни по {symbol} {tf}: " + ", ".join(f"{lv:.8f}" for lv in levels))
        await _show_levels_panel(update, context, symbol, tf, 1)
        return
    
    # Обработка выбора часового пояса
    TIMEZONE_MAP = {
        "🇹🇷 Turkey (UTC+3)": "Europe/Istanbul",
        "🇩🇪 Germany (UTC+1)": "Europe/Berlin",
        "🇷🇺 Moscow (UTC+3)": "Europe/Moscow",
        "🇺🇸 New York (UTC-5)": "America/New_York",
        "🇬🇧 London (UTC+0)": "Europe/London",
        "🇯🇵 Tokyo (UTC+9)": "Asia/Tokyo",
        "🇦🇺 Sydney (UTC+10)": "Australia/Sydney",
        "🇨🇳 Beijing (UTC+8)": "Asia/Shanghai",
    }
    
    if txt in TIMEZONE_MAP:
        if not db_enabled():
            await update.message.reply_text("Часовые пояса работают только с БД.", reply_markup=_kb_main(update.effective_user.id))
            return
        
        tz_name = TIMEZONE_MAP[txt]
        db_set_user_timezone(ch, tz_name)
        await update.message.reply_text(
            f"✅ Часовой пояс изменён на <b>{tz_name}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_main(update.effective_user.id)
        )
        return
    
    # Импорт уровней из CSV
    if context.user_data.get("levels_import_ctx"):
        ctx = context.user_data.pop("levels_import_ctx")
        default_symbol = ctx["symbol"]
        default_tf = ctx["tf"]
        # ожидаем строки CSV без заголовка: symbol,timeframe,price[,note]
        # если symbol/timeframe не указаны в CSV - используем текущие
        lines = [ln.strip() for ln in txt_raw.splitlines() if ln.strip()]
        imported = 0
        for ln in lines:
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 1:
                continue
            
            # Определяем формат: может быть price[,note] или symbol,timeframe,price[,note]
            if len(parts) >= 3:
                # Полный формат: symbol,timeframe,price[,note]
                sym, tfr, price = parts[:3]
                note = parts[3] if len(parts) > 3 else None
                sym = normalize_symbol(sym)
                from utils import normalize_tf
                tfr = normalize_tf(tfr)
            else:
                # Короткий формат: price[,note] - используем текущие symbol+tf
                sym = default_symbol
                tfr = default_tf
                price = parts[0]
                note = parts[1] if len(parts) > 1 else None
            
            try:
                price_f = float(price.replace(" ", "").replace(",", "."))
            except Exception:
                continue
            
            if sym and tfr and price_f:
                db_add_levels(ch, sym, tfr, [price_f], note)
                imported += 1
        await update.message.reply_text(f"Импортировано: {imported}")
        await _show_levels_panel(update, context, default_symbol, default_tf, 1)
        return


# =============================================================================
# HELPERS FOR LEVELS
# =============================================================================

def _get_user_symbols(uid: int) -> list:
    """Get user's available symbols sorted"""
    if not db_enabled():
        return ["BTCUSDT", "ETHUSDT"]
    symbols = db_user_pairs(uid)
    return sorted(list(set(symbols))) if symbols else ["BTCUSDT"]


def _get_user_timeframes(uid: int) -> list:
    """Get user's available timeframes sorted"""
    if not db_enabled():
        return ["1h", "4h", "1d"]
    tfs = db_user_tfs_analysis(uid)
    return sorted(list(set(tfs))) if tfs else ["1h"]


def _get_levels_current_symbol(uid: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Get current symbol for levels panel"""
    current = context.user_data.get("levels_current_symbol")
    if current:
        return current
    symbols = _get_user_symbols(uid)
    default = symbols[0] if symbols else "BTCUSDT"
    context.user_data["levels_current_symbol"] = default
    return default


def _get_levels_current_timeframe(uid: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Get current timeframe for levels panel"""
    current = context.user_data.get("levels_current_tf")
    if current:
        return current
    tfs = _get_user_timeframes(uid)
    default = tfs[0] if tfs else "1h"
    context.user_data["levels_current_tf"] = default
    return default


async def _show_levels_cfg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show levels configuration panel"""
    uid = update.effective_user.id
    cfg = db_levels_get_config(uid) if db_enabled() else {}
    
    text = (
        "⚙️ <b>Настройки уровней</b>\n\n"
        f"use_levels: <b>{cfg.get('use_levels', True)}</b>\n"
        f"block_against_level: <b>{cfg.get('block_against_level', True)}</b>\n"
        f"score_threshold: <b>{cfg.get('score_threshold', 3)}</b>\n"
        f"near_tight×ATR: <b>{cfg.get('near_tight', 0.25)}</b> · near_loose×ATR: <b>{cfg.get('near_loose', 0.5)}</b>\n"
    )
    
    rows = [
        [InlineKeyboardButton("use_levels", callback_data="levels:cfg:toggle:use"),
         InlineKeyboardButton("block", callback_data="levels:cfg:toggle:block")],
        [InlineKeyboardButton("threshold -", callback_data="levels:cfg:thresh:-"),
         InlineKeyboardButton("threshold +", callback_data="levels:cfg:thresh:+")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data="levels:cfg:info")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:main")],
    ]
    
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def _levels_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, timeframe: str):
    """Export levels to CSV file"""
    from telegram import InputFile
    import io
    import csv
    
    uid = update.effective_user.id
    rows = db_list_levels(uid, symbol, timeframe) if db_enabled() else []
    
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["symbol", "timeframe", "type", "price_low", "price_high", "strength", "note"])
    
    for lv, note in rows:
        # старый формат — только price_low и note; тип считаем support, strength=1
        w.writerow([symbol, timeframe, "support", f"{float(lv):.8f}", "", 1, (note or "")])
    
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    
    await update.effective_message.reply_document(
        document=InputFile(data, filename=f"levels_{symbol}_{timeframe}.csv"),
        caption="Экспорт уровней"
    )


def _set_levels_current_symbol(uid: int, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    """Set current symbol for levels panel"""
    context.user_data["levels_current_symbol"] = symbol


def _set_levels_current_timeframe(uid: int, context: ContextTypes.DEFAULT_TYPE, timeframe: str):
    """Set current timeframe for levels panel"""
    context.user_data["levels_current_tf"] = timeframe


async def _show_levels_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str = None, timeframe: str = None, page: int = 1):
    """Показать панель уровней"""
    uid = update.effective_user.id
    
    if symbol is None:
        symbol = _get_levels_current_symbol(uid, context)
    else:
        _set_levels_current_symbol(uid, context, symbol)
        
    if timeframe is None:
        timeframe = _get_levels_current_timeframe(uid, context)
    else:
        _set_levels_current_timeframe(uid, context, timeframe)
    
    available_symbols = _get_user_symbols(uid)
    available_timeframes = _get_user_timeframes(uid)
    
    levels_ui = context.user_data.get("levels_ui", {})
    levels_ui.update({
        "mode": "panel",
        "symbol": symbol,
        "tf": timeframe,
        "panel_page": page
    })
    context.user_data["levels_ui"] = levels_ui
    
    limit = 10
    offset = (page-1)*limit
    rows = db_list_levels(uid, symbol, timeframe) if db_enabled() else []
    total = len(rows)
    page_rows = rows[offset:offset+limit]
    has_next = (offset+limit) < total
    
    symbol_idx = available_symbols.index(symbol) if symbol in available_symbols else 0
    prev_symbol = available_symbols[symbol_idx - 1] if symbol_idx > 0 else (available_symbols[-1] if len(available_symbols) > 1 else None)
    next_symbol = available_symbols[symbol_idx + 1] if symbol_idx < len(available_symbols) - 1 else (available_symbols[0] if len(available_symbols) > 1 else None)
    
    text = (
        f"🧮 Уровни — {symbol} • {timeframe} • Стр. {page}/{max(1,(total+limit-1)//limit)}\n\n"
    )
    
    if not page_rows:
        text += "Пока нет уровней. Нажми «➕ Добавить»."
    else:
        lines = []
        buttons = []
        for i,(lv, note) in enumerate(page_rows, start=offset+1):
            lines.append(f"{i}. <b>{float(lv):.8f}</b>" + (f" — {note}" if note else ""))
            buttons.append([InlineKeyboardButton(f"🗑 Удалить {i}", callback_data=f"levels:rm_one:{symbol}:{timeframe}:{float(lv):.8f}")])
        text += "\n".join(lines) + "\n\nВыбери, какой удалить:"
        kb_base = _kb_levels_filters(symbol, timeframe, page, has_next, prev_symbol, next_symbol)
        base_rows = list(kb_base.inline_keyboard) if hasattr(kb_base, 'inline_keyboard') else []
        kb = InlineKeyboardMarkup(buttons + base_rows)
        await _get_message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    
    kb = _kb_levels_filters(symbol, timeframe, page, has_next, prev_symbol, next_symbol)
    await _get_message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# =============================================================================
# СТАТИСТИКА (КАТЕГОРИЙНАЯ) — НОВЫЕ КОМАНДЫ
# =============================================================================

async def cmd_stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общая сводка за день"""
    from handlers.stats_render import render_summary_stats
    from database import db_get_signals_with_events
    
    uid = update.effective_user.id
    
    # Получаем сигналы за последние 30 дней
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    signals = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    
    # Конвертируем в список dict
    logs = [s for s in signals]
    
    # Определяем период (по умолчанию день, или из аргументов)
    period = "day"
    if context.args and len(context.args) > 0:
        arg = context.args[0].lower()
        if arg in ["week", "month"]:
            period = arg
    
    text = render_summary_stats(logs, period=period)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбивка по режимам"""
    from handlers.stats_render import render_mode_breakdown
    from database import db_get_signals_with_events
    
    uid = update.effective_user.id
    
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    signals = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    logs = [s for s in signals]
    
    text = render_mode_breakdown(logs)
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode=ParseMode.HTML)


async def cmd_stat_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбивка по триггерам"""
    from handlers.stats_render import render_trigger_breakdown
    from database import db_get_signals_with_events
    
    uid = update.effective_user.id
    
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    signals = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    logs = [s for s in signals]
    
    text = render_trigger_breakdown(logs)
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode=ParseMode.HTML)


async def cmd_stat_tf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбивка по таймфреймам"""
    from handlers.stats_render import render_tf_breakdown
    from database import db_get_signals_with_events
    
    uid = update.effective_user.id
    
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    signals = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    logs = [s for s in signals]
    
    text = render_tf_breakdown(logs)
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode=ParseMode.HTML)


async def cmd_stat_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика фильтров и банов"""
    from handlers.stats_render import render_filters_stats
    from database import db_get_signals_with_events, db_get_ban_logs
    
    uid = update.effective_user.id
    
    # Получаем passed и ban логи за последние 30 дней
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    passed_logs = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    ban_logs = db_get_ban_logs(uid, since_ms) if db_enabled() else []
    
    text = render_filters_stats(passed_logs, ban_logs)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stat_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика и авто-советы"""
    from handlers.stats_render import render_diagnostics
    from database import db_get_signals_with_events
    
    uid = update.effective_user.id
    
    since_ms = int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)
    signals = db_get_signals_with_events(uid, since_ms) if db_enabled() else []
    logs = [s for s in signals]
    
    text = render_diagnostics(logs)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =============================================================================
# REGISTER HANDLERS
# =============================================================================

async def cmd_fix_outcomes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /fix_outcomes - пересчитать все outcome по свечам Binance"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    await update.message.reply_text("🔧 Пересчитываю outcome для всех сигналов...\n⏳ Это займёт 2-3 минуты...")
    
    try:
        # Импортируем функцию из handlers
        signals_fixed = 0
        signals_total = 0
        
        # Берём все сигналы за последние 7 дней
        # (для старых сигналов binance_klines не вернёт достаточно свечей)
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT 
                    id, symbol, timeframe,
                    extract(epoch from bar_ts)*1000 as bar_ts,
                    side, entry, sl, tp1, tp2
                FROM signals_log
                WHERE user_id = %s
                  AND bar_ts >= NOW() - INTERVAL '7 days'
                ORDER BY bar_ts ASC
            """, (uid,))
            
            signals = []
            for row in cur.fetchall():
                signals.append({
                    "id": int(row[0]),
                    "symbol": row[1],
                    "timeframe": row[2],
                    "bar_ts": int(row[3]) if row[3] else 0,
                    "side": row[4],
                    "entry": float(row[5]) if row[5] else 0,
                    "sl": float(row[6]) if row[6] else 0,
                    "tp1": float(row[7]) if row[7] else 0,
                    "tp2": float(row[8]) if row[8] else 0,
                })
        
        signals_total = len(signals)
        
        if signals_total == 0:
            await update.message.reply_text("❌ Нет сигналов для пересчёта")
            return
        
        # Сначала ОЧИЩАЕМ старые события и outcome_v2 для всех сигналов
        with _db_conn() as c, c.cursor() as cur:
            sig_ids = [s["id"] for s in signals]
            cur.execute("""
                UPDATE signals_log
                SET hit_tp1_ts = NULL,
                    hit_tp1_price = NULL,
                    hit_tp2_ts = NULL,
                    hit_tp2_price = NULL,
                    hit_sl_ts = NULL,
                    hit_sl_price = NULL,
                    outcome_v2 = NULL
                WHERE id = ANY(%s)
            """, (sig_ids,))
            c.commit()
        
        # Теперь пересчитываем каждый
        from binance.binance import binance_klines
        from datetime import datetime, timezone
        
        for sig in signals:
            try:
                # Загружаем свечи от bar_ts до текущего момента
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                klines = binance_klines(
                    sig["symbol"], 
                    sig["timeframe"], 
                    limit=1500,
                    start_time=sig["bar_ts"],
                    end_time=now_ms
                )
                
                if klines is None or klines.empty:
                    continue
                
                # Пересчитываем события вручную (не через _resolve_signal_events_live)
                hit_tp1_ts = None
                hit_tp2_ts = None
                hit_sl_ts = None
                
                for idx, row in klines.iterrows():
                    ts = int(row["ts"])
                    if ts <= sig["bar_ts"]:
                        continue
                    
                    high = float(row["high"])
                    low = float(row["low"])
                    
                    if sig["side"] == "LONG":
                        if hit_sl_ts is None and low <= sig["sl"]:
                            hit_sl_ts = ts
                        if hit_tp1_ts is None and high >= sig["tp1"]:
                            hit_tp1_ts = ts
                        if hit_tp2_ts is None and high >= sig["tp2"]:
                            hit_tp2_ts = ts
                    else:  # SHORT
                        if hit_sl_ts is None and high >= sig["sl"]:
                            hit_sl_ts = ts
                        if hit_tp1_ts is None and low <= sig["tp1"]:
                            hit_tp1_ts = ts
                        if hit_tp2_ts is None and low <= sig["tp2"]:
                            hit_tp2_ts = ts
                
                # Сохраняем события
                if any([hit_tp1_ts, hit_tp2_ts, hit_sl_ts]):
                    from database import db_update_signal_events, db_update_signal_outcome
                    
                    events = {
                        "hit_tp1_price": sig["tp1"] if hit_tp1_ts else None,
                        "hit_tp1_ts": hit_tp1_ts,
                        "hit_tp2_price": sig["tp2"] if hit_tp2_ts else None,
                        "hit_tp2_ts": hit_tp2_ts,
                        "hit_sl_price": sig["sl"] if hit_sl_ts else None,
                        "hit_sl_ts": hit_sl_ts,
                    }
                    db_update_signal_events(sig["id"], events)
                    
                    # Вычисляем outcome_v2
                    outcome_v2 = _calculate_outcome_v2(sig, events)
                    if outcome_v2:
                        db_update_signal_outcome(sig["id"], outcome_v2)
                        signals_fixed += 1
            
            except Exception as e:
                print(f"[FIX_OUTCOMES] Error for signal {sig['id']}: {e}")
        
        await update.message.reply_text(
            f"✅ Пересчёт завершён!\n\n"
            f"📊 Всего сигналов: {signals_total}\n"
            f"🔧 Обновлено: {signals_fixed}\n\n"
            f"Теперь запусти /verify_stats чтобы проверить корректность."
        )
    
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка пересчёта: {e}")
        print(f"[FIX_OUTCOMES] Error: {e}")


async def cmd_recalc_tpmid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /recalc_tpmid - пересчитывает TPmid события и пути сигналов для всех сигналов"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    await update.message.reply_text("⏳ Пересчитываю TPmid события и пути сигналов для всех сигналов...")
    
    try:
        from datetime import timedelta
        # Получаем настройки TPmid
        tp_mid_cfg = db_get_tp_mid_config(uid) if db_enabled() else {"enabled": False, "auto": True, "delta_pct": 0.1}
        
        if not tp_mid_cfg.get('enabled'):
            await update.message.reply_text("❌ TPmid выключен. Включите его в настройках сначала.")
            return
        
        # Получаем ВСЕ сигналы пользователя (за последние 30 дней)
        since_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
        sigs = db_get_signals_with_events(uid, since_ms)
        
        total = len(sigs)
        updated = 0
        tp2_count = 0
        tp2_with_tpmid = 0
        paths_updated = 0
        
        from handlers.message_render import _tp_mid_price_for_signal
        
        for s in sigs:
            try:
                # Сбрасываем старое TPmid событие
                s["hit_tp_mid_ts"] = None
                s["hit_tp_mid_price"] = None
                
                # Рассчитываем TPmid цену
                mid_price = _tp_mid_price_for_signal(s, tp_mid_cfg)
                if not mid_price:
                    continue
                
                # Оптимизация: если сигнал достиг TP2, то TPmid (между TP1 и TP2) точно был достигнут
                tp1_ts = s.get('hit_tp1_ts')
                tp2_ts = s.get('hit_tp2_ts')
                entry = float(s.get('entry') or s.get('entry_price') or 0.0)
                tp1 = float(s.get('tp1') or 0.0)
                tp2 = float(s.get('tp2') or 0.0)
                side = str(s.get('side', 'LONG')).upper()
                
                mid_ts = None
                
                # Проверяем, находится ли TPmid между TP1 и TP2
                if entry > 0 and tp1 > 0 and tp2 > 0:
                    if side == 'LONG':
                        tp_mid_between = tp1 < mid_price < tp2
                    else:  # SHORT
                        tp_mid_between = tp2 < mid_price < tp1
                    
                    # Если TP2 достигнут и TPmid между TP1 и TP2, то TPmid был достигнут
                    if tp2_ts and tp1_ts:
                        tp2_count += 1
                        if tp_mid_between:
                            # Сначала пытаемся найти точный момент через свечи
                            mid_ts = _check_price_level_hit(s, mid_price)
                            # Если не нашли через свечи, но TP2 достигнут, значит TPmid был достигнут
                            if not mid_ts:
                                # TPmid был достигнут между TP1 и TP2, используем среднее время
                                tp1_ts_int = int(tp1_ts)
                                tp2_ts_int = int(tp2_ts)
                                mid_ts = (tp1_ts_int + tp2_ts_int) // 2  # Среднее время между TP1 и TP2
                                print(f"[RECALC_TPMID] Signal {s.get('id')}: TP2 reached, using avg time for TPmid: {mid_ts}")
                            if mid_ts:
                                tp2_with_tpmid += 1
                        else:
                            print(f"[RECALC_TPMID] Signal {s.get('id')}: TP2 reached but TPmid ({mid_price}) not between TP1 ({tp1}) and TP2 ({tp2})")
                    elif tp1_ts:
                        # Если TP1 достигнут, но не TP2, проверяем через свечи
                        mid_ts = _check_price_level_hit(s, mid_price)
                    else:
                        # Если TP1 не достигнут, проверяем через свечи
                        mid_ts = _check_price_level_hit(s, mid_price)
                else:
                    # Обычная проверка через свечи
                    mid_ts = _check_price_level_hit(s, mid_price)
                
                if mid_ts:
                    s['hit_tp_mid_price'] = mid_price
                    s['hit_tp_mid_ts'] = mid_ts
                    updated += 1
                    
                    # Сохраняем в БД
                    try:
                        from database import db_update_signal_events, db_update_signal_outcome
                        events_dict = {
                            "hit_tp_mid_price": mid_price,
                            "hit_tp_mid_ts": mid_ts
                        }
                        db_update_signal_events(s.get('id'), events_dict)
                        
                        # Обновляем outcome_v2 с учетом новой логики
                        # Сначала получаем все события сигнала
                        all_events = {
                            "hit_tp1_price": s.get('hit_tp1_price'),
                            "hit_tp1_ts": s.get('hit_tp1_ts'),
                            "hit_tp2_price": s.get('hit_tp2_price'),
                            "hit_tp2_ts": s.get('hit_tp2_ts'),
                            "hit_sl_price": s.get('hit_sl_price'),
                            "hit_sl_ts": s.get('hit_sl_ts'),
                            "hit_tp_mid_price": mid_price,
                            "hit_tp_mid_ts": mid_ts,
                        }
                        
                        # Определяем путь используя новую логику
                        path = _determine_signal_path(s, tp_mid_cfg)
                        
                        # Преобразуем путь в старый формат статуса для outcome_v2
                        status_map = {
                            "SL_BEFORE_TP1": "SL",
                            "TP1_PLUS_TP2": "TP1+TP2",
                            "TP1_TO_SL": "TP1→SL",
                            "TP1_TO_OPEN": "TP1→OPEN",
                            "TP1_TO_TPMID": "TP1→OPEN",  # Временный статус
                            "TPMID_TO_TP1_BE": "TP1→OPEN",  # Безубыток = TP1→OPEN для outcome_v2
                            "OPEN": None
                        }
                        status = status_map.get(path)
                        
                        # Вычисляем новый outcome_v2
                        if status:
                            outcome_v2 = _calculate_outcome_v2(s, all_events)
                            if outcome_v2:
                                # Обновляем статус если путь изменился
                                if outcome_v2.get('status') != status:
                                    outcome_v2['status'] = status
                                    # Для TPMID_TO_TP1_BE r = 0 (безубыток)
                                    if path == "TPMID_TO_TP1_BE":
                                        outcome_v2['r'] = 0.0
                                db_update_signal_outcome(s.get('id'), outcome_v2)
                                paths_updated += 1
                    except Exception as db_err:
                        print(f"[RECALC_TPMID] DB update error for signal {s.get('id')}: {db_err}")
                    
            except Exception as e:
                print(f"[RECALC_TPMID] Error for signal {s.get('id')}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        # Также пересчитываем пути для всех сигналов (даже без TPmid)
        # чтобы обновить outcome_v2 с учетом новой логики
        for s in sigs:
            try:
                # Получаем все события сигнала
                all_events = {
                    "hit_tp1_price": s.get('hit_tp1_price'),
                    "hit_tp1_ts": s.get('hit_tp1_ts'),
                    "hit_tp2_price": s.get('hit_tp2_price'),
                    "hit_tp2_ts": s.get('hit_tp2_ts'),
                    "hit_sl_price": s.get('hit_sl_price'),
                    "hit_sl_ts": s.get('hit_sl_ts'),
                    "hit_tp_mid_price": s.get('hit_tp_mid_price'),
                    "hit_tp_mid_ts": s.get('hit_tp_mid_ts'),
                }
                
                # Определяем путь используя новую логику
                path = _determine_signal_path(s, tp_mid_cfg if tp_mid_cfg.get('enabled') else None)
                
                # Обновляем outcome_v2 если нужно
                if path and path != "OPEN":
                    outcome_v2 = _calculate_outcome_v2(s, all_events)
                    if outcome_v2:
                        from database import db_update_signal_outcome
                        # Для TPMID_TO_TP1_BE r = 0 (безубыток)
                        if path == "TPMID_TO_TP1_BE":
                            outcome_v2['r'] = 0.0
                            outcome_v2['status'] = "TP1→OPEN"  # Для совместимости
                        db_update_signal_outcome(s.get('id'), outcome_v2)
                        paths_updated += 1
            except Exception as e:
                print(f"[RECALC_TPMID] Error updating path for signal {s.get('id')}: {e}")
                continue
        
        await update.message.reply_text(
            f"✅ Пересчёт завершён!\n\n"
            f"📊 Всего сигналов: {total}\n"
            f"🔄 TPmid событий найдено: {updated}\n"
            f"📈 Сигналов с TP2: {tp2_count}\n"
            f"📊 Из них с TPmid: {tp2_with_tpmid}\n"
            f"🔄 Путей обновлено: {paths_updated}\n\n"
            f"✅ Все пути пересчитаны с учётом новой логики (возврат к TP1 после TPmid).\n\n"
            f"Теперь проверь статистику - всё должно считаться правильно."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


async def cmd_verify_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /verify_stats - автоматическая проверка корректности outcome_v2"""
    if not _auth(update):
        return
    
    uid = update.effective_user.id
    await update.message.reply_text("🔍 Проверяю корректность outcome_v2...\n⏳ Это займёт 1-2 минуты...")
    
    try:
        from binance.binance import binance_klines
        
        # Берём 10 случайных сигналов с outcome_v2
        with _db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT 
                    id, symbol, timeframe,
                    extract(epoch from bar_ts)*1000 as bar_ts,
                    side, entry, sl, tp1, tp2,
                    outcome_v2
                FROM signals_log
                WHERE user_id = %s
                  AND outcome_v2 IS NOT NULL
                  AND bar_ts >= NOW() - INTERVAL '7 days'
                ORDER BY RANDOM()
                LIMIT 10
            """, (uid,))
            
            signals = []
            for row in cur.fetchall():
                signals.append({
                    "id": int(row[0]),
                    "symbol": row[1],
                    "timeframe": row[2],
                    "bar_ts": int(row[3]) if row[3] else 0,
                    "side": row[4],
                    "entry": float(row[5]) if row[5] else 0,
                    "sl": float(row[6]) if row[6] else 0,
                    "tp1": float(row[7]) if row[7] else 0,
                    "tp2": float(row[8]) if row[8] else 0,
                    "outcome_v2": row[9],
                })
        
        if not signals:
            await update.message.reply_text("❌ Нет закрытых сигналов для проверки")
            return
        
        # Проверяем каждый сигнал
        correct = 0
        incorrect = 0
        errors = 0
        details = []
        
        for sig in signals:
            try:
                # Загружаем свечи
                klines_df = binance_klines(sig["symbol"], sig["timeframe"], limit=500)
                
                if klines_df is None or klines_df.empty:
                    errors += 1
                    continue
                
                # Пересчитываем события
                hit_tp1_ts = None
                hit_tp2_ts = None
                hit_sl_ts = None
                
                for idx, row in klines_df.iterrows():
                    ts = int(row["ts"])
                    if ts <= sig["bar_ts"]:
                        continue
                    
                    high = float(row["high"])
                    low = float(row["low"])
                    
                    if sig["side"] == "LONG":
                        if hit_sl_ts is None and low <= sig["sl"]:
                            hit_sl_ts = ts
                        if hit_tp1_ts is None and high >= sig["tp1"]:
                            hit_tp1_ts = ts
                        if hit_tp2_ts is None and high >= sig["tp2"]:
                            hit_tp2_ts = ts
                    else:  # SHORT
                        if hit_sl_ts is None and high >= sig["sl"]:
                            hit_sl_ts = ts
                        if hit_tp1_ts is None and low <= sig["tp1"]:
                            hit_tp1_ts = ts
                        if hit_tp2_ts is None and low <= sig["tp2"]:
                            hit_tp2_ts = ts
                
                # Определяем outcome
                has_tp1 = bool(hit_tp1_ts)
                has_tp2 = bool(hit_tp2_ts)
                has_sl = bool(hit_sl_ts)
                
                if has_sl and not has_tp1:
                    calculated = "SL"
                elif has_tp1 and has_tp2:
                    calculated = "TP1+TP2"
                elif has_tp1 and has_sl:
                    if int(hit_sl_ts) < int(hit_tp1_ts):
                        calculated = "SL"
                    else:
                        calculated = "TP1→SL"
                elif has_tp1 and not has_tp2 and not has_sl:
                    calculated = "TP1→OPEN"
                else:
                    calculated = None
                
                # Сравниваем с БД
                db_status = sig["outcome_v2"].get("status") if isinstance(sig["outcome_v2"], dict) else None
                
                # Нормализация (старый vs новый формат)
                mapping = {
                    "SL_BEFORE_TP1": "SL",
                    "TP1_PLUS_TP2": "TP1+TP2",
                    "TP1_TO_SL": "TP1→SL",
                    "TP1_TO_OPEN": "TP1→OPEN",
                }
                db_status_normalized = mapping.get(db_status, db_status)
                calculated_normalized = mapping.get(calculated, calculated)
                
                if db_status_normalized == calculated_normalized:
                    correct += 1
                else:
                    incorrect += 1
                    details.append(f"#{sig['id']} {sig['symbol']}: БД={db_status} ≠ Расчёт={calculated}")
            
            except Exception as e:
                errors += 1
                print(f"[VERIFY] Error checking signal {sig['id']}: {e}")
        
        # Формируем отчёт
        total = correct + incorrect
        accuracy = (correct * 100 / total) if total > 0 else 0
        
        lines = []
        lines.append("📊 <b>ПРОВЕРКА КОРРЕКТНОСТИ OUTCOME</b>\n")
        lines.append(f"✅ Совпало: {correct}")
        lines.append(f"❌ Не совпало: {incorrect}")
        lines.append(f"⚠️ Ошибки загрузки: {errors}")
        lines.append(f"\n📈 <b>Точность: {accuracy:.1f}%</b>\n")
        
        if accuracy >= 95:
            lines.append("✅ <b>Outcome считается ПРАВИЛЬНО!</b>")
            lines.append("Статистика корректна, можно доверять.")
        elif accuracy >= 80:
            lines.append("⚠️ <b>Есть небольшие расхождения</b>")
            lines.append("Возможно краевые случаи или задержки Binance.")
        else:
            lines.append("🚨 <b>ПРОБЛЕМА! Много ошибок!</b>")
            lines.append("Нужно проверять логику расчёта outcome.")
        
        if details:
            lines.append("\n<b>Детали несовпадений:</b>")
            for d in details[:5]:  # Первые 5
                lines.append(f"• {d}")
            if len(details) > 5:
                lines.append(f"... и ещё {len(details) - 5}")
        
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка проверки: {e}")
        print(f"[VERIFY] Error: {e}")


def register(application: Application):
    """Регистрация всех обработчиков"""
    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pairs", cmd_pairs))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("rm", cmd_rm))
    application.add_handler(CommandHandler("tfs", cmd_tfs))
    application.add_handler(CommandHandler("tf_add", cmd_tf_add))
    application.add_handler(CommandHandler("tf_rm", cmd_tf_rm))
    application.add_handler(CommandHandler("tf_mode", cmd_tf_mode))
    application.add_handler(CommandHandler("recalc_stats", cmd_recalc_stats))
    application.add_handler(CommandHandler("recalc_tpmid", cmd_recalc_tpmid))
    application.add_handler(CommandHandler("audit", cmd_audit))
    application.add_handler(CommandHandler("recent_signals", cmd_recent_signals))
    application.add_handler(CommandHandler("check_signal", cmd_check_signal))
    application.add_handler(CommandHandler("fix_outcomes", cmd_fix_outcomes))
    application.add_handler(CommandHandler("verify_stats", cmd_verify_stats))
    
    # Новые команды статистики (категорийная)
    application.add_handler(CommandHandler("stat", cmd_stat))
    application.add_handler(CommandHandler("stat_mode", cmd_stat_mode))
    application.add_handler(CommandHandler("stat_trigger", cmd_stat_trigger))
    application.add_handler(CommandHandler("stat_tf", cmd_stat_tf))
    application.add_handler(CommandHandler("stat_filters", cmd_stat_filters))
    application.add_handler(CommandHandler("stat_diag", cmd_stat_diag))
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(on_cb))
    
    # Текстовые сообщения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Периодические задачи (job_queue)
    from jobs import _job_tick, _job_update_outcomes, _job_eod_close_and_digest
    
    # 1. Генерация сигналов (часто)
    schedule_minutes = CONFIG.get("schedule_minutes", 1)
    application.job_queue.run_repeating(
        _job_tick,
        interval=schedule_minutes * 60,  # конвертируем минуты в секунды
        first=10  # первый запуск через 10 секунд после старта
    )
    
    # 2. Обновление исходов сделок (реже)
    application.job_queue.run_repeating(
        _job_update_outcomes,
        interval=5 * 60,  # каждые 5 минут
        first=60  # первый запуск через 1 минуту после старта
    )
    
    # 3. EOD-закрытие сделок и отправка дневной статистики
    # Проверяет каждую минуту, кому пора закрывать (в 23:59 по их TZ)
    application.job_queue.run_repeating(
        _job_eod_close_and_digest,
        interval=60,  # каждую минуту
        first=120  # первый запуск через 2 минуты после старта
    )
    
    print("[HANDLERS] All handlers registered")
    print(f"[JOB] Signal generation scheduled every {schedule_minutes} minute(s)")
    print("[JOB] Outcome updates scheduled every 5 minutes")
