#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Периодические задачи: генерация сигналов, дайджесты"""

import asyncio
import time
import os
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Импорты из других модулей
from config import CONFIG, FRESH_ONLY, _TICK_TIMEOUT, MAX_WORKERS
from utils import normalize_tf, within_news_blackout, utcnow, _TF_TO_SEC, MAX_DELAY_SEC, _fmt_price
from binance import FEATURE_CACHE, LAST_SIGNAL
from strategy import compute_features
from handlers.message_render import render_signal_card_short
from strategy.logs import SignalLog

def _evaluate_signal_unified(symbol: str, timeframe: str, features: Dict[str, Any], uid: int = None) -> Dict[str, Any]:
    """Оценка сигнала пайплайном стратегии. Возвращает {} или {signal, log, reason, symbol, features}."""
    # ленивый импорт: адаптер тянет handlers.message_render → handlers → jobs (цикл)
    from jobs.strategy_adapter import evaluate_signal_v2
    result = evaluate_signal_v2(symbol=symbol, timeframe=timeframe, features=features, uid=uid)
    if not result or not result.get("signal"):
        return {}
    result["symbol"] = symbol
    # apply_levels_influence ждёт res["features"]["ATR"]
    result["features"] = {"ATR": float(features.get("atr", 0.0))}
    return result


from database import (
    db_enabled,
    db_list_users,
    db_user_pairs,
    db_user_tfs_analysis,
    db_user_tfs_signal,
    db_get_settings,
    db_is_running,
    db_session_is_open,
    db_log_signal,
    db_set_signal_message_id,
    db_levels_get_config,
    db_list_levels,
)

# Thread executor для параллельных вычислений
EXEC = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Блокировка тика
_tick_lock = asyncio.Lock()

# UI flags (импортируем из config если нужно)
SHOW_TRAIL_AS_ATR_HINT = True
SHOW_CONFLUENCE_BLOCK = True
PCT_DECIMALS = 2
XATR_DECIMALS = 2
FULL_CONFLUENCE_DETAILS = True


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _compute_cached(symbol: str, tf: str) -> Dict[str, Any]:
    """Получить фичи из кэша или вычислить заново"""
    k = (symbol, tf)
    if k in FEATURE_CACHE:
        return FEATURE_CACHE[k]
    feat = compute_features(symbol, tf, CONFIG["lookback"])
    FEATURE_CACHE[k] = feat
    return feat


def _cooldown_pass(uid: int, symbol: str, side: str) -> bool:
    """Проверка cooldown между сигналами"""
    cd_min = CONFIG.get("cooldown_minutes", 10)
    now = utcnow()
    key = (uid, symbol, side)
    if key in LAST_SIGNAL:
        dt = (now - LAST_SIGNAL[key]).total_seconds() / 60.0
        if dt < cd_min:
            return False
    LAST_SIGNAL[key] = now
    return True


def _levels_fetch_near(uid: int, symbol: str, timeframe: str, center_price: float, atr_abs: float) -> list[tuple[float, str | None]]:
    """Получить ближайшие уровни поддержки/сопротивления"""
    rows = db_list_levels(uid, symbol, timeframe) if db_enabled() else []
    if not rows:
        return []
    window = atr_abs if atr_abs and atr_abs > 0 else max(1e-6, center_price * 0.01)
    return [(lv, note) for (lv, note) in rows if abs(float(lv) - center_price) <= window]


def format_signal_message(res: Dict[str, Any], tf: str, uid: Optional[int] = None):
    """Форматирование сообщения с сигналом (НОВАЯ КАРТОЧКА с Gates/Mode/Trigger)"""
    symbol = res["symbol"]
    tv_sym = symbol.replace("USDT", "USDT.P")
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{tv_sym}"
    
    from jobs.strategy_adapter import format_signal_message_v2
    text = format_signal_message_v2(res["log"], tv_url=tv_url, uid=uid)
    
    # Клавиатура (базовая: только график, кнопка "Зайти в позицию" добавляется позже)
    kb_rows = [
        [InlineKeyboardButton("📈 Открыть график (TV)", url=tv_url)]
    ]
    keyboard = InlineKeyboardMarkup(kb_rows)
    
    return text, keyboard


def apply_levels_influence(uid: int, symbol: str, tf: str, res: Dict[str, Any]) -> tuple[bool, str, InlineKeyboardMarkup]:
    """Применить влияние уровней поддержки/сопротивления на сигнал"""
    s = res["signal"]
    f = res["features"]
    text, kb = format_signal_message(res, tf, uid=uid)
    cfg = db_levels_get_config(uid) if db_enabled() else {"use_levels": False}
    
    if not cfg.get("use_levels", True):
        return True, text, kb

    entry = float(s["entry"])
    atr_abs = float(f.get("ATR", 0) or 0)
    near = _levels_fetch_near(uid, symbol, tf, entry, atr_abs)
    
    if not near:
        return True, text, kb

    # Простой скоринг
    near_tight = float(cfg.get("near_tight", 0.25))
    near_loose = float(cfg.get("near_loose", 0.5))
    w_near_tight = int(cfg.get("w_near_tight", 2))
    w_near_loose = int(cfg.get("w_near_loose", 1))
    w_dir = int(cfg.get("w_dir", 2))
    
    score_best = 0
    d_best = None
    details: list[tuple[float, float, str | None, int, int]] = []
    tight_cnt = 0
    loose_cnt = 0
    
    for lv, _note in near:
        D = abs(entry - float(lv))
        d_atr = (D / atr_abs) if atr_abs > 0 else 9e9
        near_pts = w_near_tight if d_atr <= near_tight else (w_near_loose if d_atr <= near_loose else 0)
        
        if d_atr <= near_tight:
            tight_cnt += 1
        elif d_atr <= near_loose:
            loose_cnt += 1
        
        side = (s["side"] or "").upper()
        dir_pts = 0
        if side == "LONG" and float(lv) <= entry:
            dir_pts = w_dir
        if side == "SHORT" and float(lv) >= entry:
            dir_pts = w_dir
        
        score = near_pts + dir_pts + 1
        if score > score_best:
            score_best = score
            d_best = d_atr
        details.append((float(lv), d_atr, _note, near_pts, dir_pts))

    # Блокировка «в уровень»
    if cfg.get("block_against_level", True):
        is_against = (
            (s["side"].upper()=="LONG" and any(float(lv) >= entry and ((abs(entry-float(lv))/max(atr_abs,1e-6)) <= near_tight) for lv,_ in near)) or
            (s["side"].upper()=="SHORT" and any(float(lv) <= entry and ((abs(entry-float(lv))/max(atr_abs,1e-6)) <= near_tight) for lv,_ in near))
        )
        if is_against:
            return False, text, kb

    threshold = int(cfg.get("score_threshold", 3))
    if score_best < threshold:
        return False, text, kb

    if d_best is not None and SHOW_CONFLUENCE_BLOCK:
        details.sort(key=lambda x: x[1])
        if FULL_CONFLUENCE_DETAILS:
            lines = [
                "\n\n🧮 <b>Confluence (уровни)</b>",
                f"• Найдено рядом: {tight_cnt} (≤ {near_tight}×ATR), {loose_cnt} (≤ {near_loose}×ATR)",
                f"• Балл: <b>{score_best}</b> (порог {int(cfg.get('score_threshold', 3))})",
                f"• Ближайший: {d_best:.2f}×ATR",
            ]
            for lv, d_atr, note, n_pts, d_pts in details[:5]:
                note_part = f" — {note}" if note else ""
                lines.append(f"  · {lv:.8f} ({d_atr:.2f}×ATR, near+{n_pts}, dir+{d_pts}){note_part}")
            text += "\n".join(lines)
        else:
            text += f"\n\nConfluence: +{score_best}; ближ. {d_best:.2f}×ATR."
    
    return True, text, kb


# =============================================================================
# TELEGRAM SEND
# =============================================================================

async def tg_send_html(text: str, keyboard=None, chat_id: Optional[int]=None, reply_to_message_id: Optional[int]=None) -> Optional[int]:
    """Отправка HTML сообщения в Telegram"""
    if not CONFIG["telegram"]["enabled"]:
        return
    
    token = os.getenv(CONFIG["telegram"]["token_env"])
    if not token:
        print("Telegram disabled: no token")
        return
    
    if chat_id is None:
        env_chat = os.getenv(CONFIG["telegram"]["chat_id_env"])
        if not env_chat:
            print("Telegram: no chat target")
            return
        chat_id = int(env_chat)
    
    bot = Bot(token=token)
    try:
        m = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
            reply_to_message_id=reply_to_message_id
        )
        return m.message_id if m else None
    except Exception as e:
        print(f"[WARN] send to {chat_id} failed: {e}")
        return None


def send_telegram(text: str, keyboard=None, chat_id: Optional[int]=None, reply_to_message_id: Optional[int]=None) -> Optional[int]:
    """
    Send message in any context.
    - If no running event loop: run coroutine and return message id.
    - If called inside running loop: schedule background task and return None.
    """
    try:
        loop = asyncio.get_running_loop()
        # We're inside an async loop; schedule without awaiting
        asyncio.create_task(tg_send_html(text, keyboard=keyboard, chat_id=chat_id, reply_to_message_id=reply_to_message_id))
        return None
    except RuntimeError:
        # No running loop; safe to run synchronously
        return asyncio.run(tg_send_html(text, keyboard=keyboard, chat_id=chat_id, reply_to_message_id=reply_to_message_id))


# =============================================================================
# MAIN TICK ENGINE
# =============================================================================

def tick_once_for_user(uid: int):
    """
    Главная функция генерации сигналов для одного пользователя.
    
    Порядок:
      1) Считаем/кэшируем фичи на ТФ АНАЛИЗА (контекст)
      2) На ТФ СИГНАЛА считаем кандидатов
      3) Для каждого кандидата: контекст-гейт + свежесть + cooldown → отправка
    """
    FEATURE_CACHE.clear()
    now = utcnow()

    # Blackouts по новостям
    reason = within_news_blackout(now, CONFIG["news_blackouts"])
    if reason:
        print(f"[{now}] Blackout by news: {reason}")
        return

    # Стоп-краны: /start|/stop + открытая сессия
    if db_enabled():
        if not db_is_running(uid):
            return
        if not db_session_is_open(uid):
            return

    # Наборы пользователя
    if db_enabled():
        pairs = db_user_pairs(uid) or ["BTCUSDT", "ETHUSDT"]
        tfs_a = db_user_tfs_analysis(uid) or ["1h", "4h", "1d"]  # анализ
        tfs_s = db_user_tfs_signal(uid) or ["1h", "4h", "1d"]    # сигналы
        mode = (db_get_settings(uid).get("tf_mode", "stacked") or "stacked").lower()
        print(f"[TICK] User {uid}: pairs={pairs}, tfs_analysis={tfs_a}, tfs_signal={tfs_s}, mode={mode}")
    else:
        pairs = ["BTCUSDT", "ETHUSDT"]
        tfs_a = ["1h", "4h", "1d"]
        tfs_s = ["1h", "4h", "1d"]
        mode = "stacked"
        print(f"[TICK] Fallback mode: pairs={pairs}, tfs_analysis={tfs_a}, tfs_signal={tfs_s}")

    # ---------- (1) КОНТЕКСТ: фичи на analysis-ТФ ----------
    futs = []
    for s in set(pairs):
        for tf in set(tfs_a):
            futs.append(EXEC.submit(lambda sym=s, t=tf: (sym, t, compute_features(sym, t, CONFIG["lookback"]))))

    total_timeout = max(5, CONFIG.get("EVAL_TIMEOUT_S", 20) * max(1, len(futs)))
    
    for fut in as_completed(futs, timeout=total_timeout):
        try:
            s_, tf_, feat_ = fut.result(timeout=CONFIG.get("EVAL_TIMEOUT_S", 20))
            FEATURE_CACHE[(s_, tf_)] = feat_
        except Exception:
            continue

    # ---------- (2) КАНДИДАТЫ: сигналы на signal-ТФ ----------
    print(f"[TICK] Computing signals for {len(set(pairs))} pairs × {len(set(tfs_s))} TFs = {len(set(pairs)) * len(set(tfs_s))} candidates")
    evaluated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    futs = []
    for s in set(pairs):
        for tf in set(tfs_s):
            futs.append(EXEC.submit(
                lambda sym=s, t=tf: (sym, t, _evaluate_signal_unified(sym, t, FEATURE_CACHE.get((sym, t)) or compute_features(sym, t, CONFIG["lookback"]), uid=uid))
            ))

    total_timeout = max(5, CONFIG.get("EVAL_TIMEOUT_S", 20) * max(1, len(futs)))
    for fut in as_completed(futs, timeout=total_timeout):
        try:
            s_, tf_, res_ = fut.result(timeout=CONFIG.get("EVAL_TIMEOUT_S", 20))
            evaluated[(s_, tf_)] = res_
            if (res_.get("signal") or {}).get("side"):
                print(f"[TICK] Signal candidate: {s_} {tf_} → {res_['signal']['side']}")
        except Exception as e:
            print(f"[TICK] evaluate_signal failed: {e}")
            continue
    
    print(f"[TICK] Found {len([r for r in evaluated.values() if (r.get('signal') or {}).get('side')])} signal candidates")

    # Helper: собрать контекст-фичи по символу
    def _ctx_feats(symbol: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for tf in tfs_a:
            k = (symbol, tf)
            if k in FEATURE_CACHE:
                out[tf] = FEATURE_CACHE[k]
        return out

    # Helper: свежесть бара
    def _fresh_enough(tf: str) -> Tuple[int, bool]:
        now_sec = int(time.time())
        tf_sec = _TF_TO_SEC.get(tf, 60)
        bar_close_ts = ((now_sec // tf_sec) * tf_sec) * 1000  # В миллисекундах!
        print(f"[FRESH] now_sec={now_sec}, bar_close_ts={bar_close_ts} (ms)")
        if FRESH_ONLY:
            bar_close_sec = bar_close_ts // 1000
            delay = now_sec - bar_close_sec
            max_delay = MAX_DELAY_SEC.get(tf, 10)
            if delay > max_delay:
                print(f"[skip stale] {tf} delay={delay}s > {max_delay}s")
                return bar_close_ts, False
        return bar_close_ts, True

    # ---------- (3) Отправка по режиму ----------
    if mode == "any":
        for s in pairs:
            ctx = _ctx_feats(s)
            for tf in tfs_s:
                res = evaluated.get((s, tf))
                if not res or res.get("signal") is None:
                    continue
                side = res["signal"]["side"]
                
                bar_ts, ok = _fresh_enough(tf)
                if not ok:
                    continue
                if not _cooldown_pass(uid, s, side):
                    continue
                
                # Влияние уровней
                use_msg, msg, kb = apply_levels_influence(uid, s, tf, res)
                if not use_msg:
                    continue
                
                # Всегда добавляем кнопку "Зайти в позицию"
                tv_row = kb.inline_keyboard[0] if hasattr(kb, 'inline_keyboard') else []
                rows = [tv_row] if tv_row else []
                
                if db_enabled():
                    sig = res["signal"]
                    # Извлекаем mode и trigger_type для v2
                    mode = res["log"].mode if res.get("log") else None
                    trigger = res["log"].trigger_type if res.get("log") else None
                    sig_id = db_log_signal(uid, s, tf, bar_ts, side, sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], res["reason"], mode=mode, trigger_type=trigger)
                    if sig_id:
                        rows.append([InlineKeyboardButton("✅ Зайти в позицию", callback_data=f"enter:{sig_id}")])
                        kb2 = InlineKeyboardMarkup(rows)
                        mid = send_telegram(msg, keyboard=kb2, chat_id=uid)
                        try:
                            mid_val = mid.result().message_id if hasattr(mid, 'result') else mid
                        except Exception:
                            mid_val = mid
                        if mid_val:
                            db_set_signal_message_id(sig_id, int(mid_val))
                    else:
                        # sig_id is None - сигнал уже существует (дубликат)
                        # НЕ отправляем в Telegram чтобы избежать дублей
                        print(f"[TICK] Signal duplicate for {s} {tf} - skipping Telegram send")
                else:
                    # БД отключена, добавляем кнопку без callback
                    rows.append([InlineKeyboardButton("✅ Зайти в позицию", callback_data="enter:0")])
                    kb2 = InlineKeyboardMarkup(rows)
                    send_telegram(msg, keyboard=kb2, chat_id=uid)
        return

    # STACKED: большинство среди сигнальных ТФ
    for s in pairs:
        sides: List[str] = []
        for tf in tfs_s:
            r = evaluated.get((s, tf))
            if r and r.get("signal"):
                sides.append(r["signal"]["side"])
        
        if not sides:
            continue
        
        side = "LONG" if sides.count("LONG") >= sides.count("SHORT") else "SHORT"

        # Берём первый подходящий результат
        chosen_tf, chosen_res = None, None
        for tf in tfs_s:
            r = evaluated.get((s, tf))
            if r and r.get("signal") and r["signal"]["side"] == side:
                chosen_tf, chosen_res = tf, r
                break
        
        if not chosen_res:
            continue

        bar_ts, ok = _fresh_enough(chosen_tf)
        if not ok:
            continue
        if not _cooldown_pass(uid, s, side):
            continue

        use_msg, msg, kb = apply_levels_influence(uid, s, chosen_tf, chosen_res)
        if not use_msg:
            continue
        
        # Всегда добавляем кнопку "Зайти в позицию"
        tv_row = kb.inline_keyboard[0] if hasattr(kb, 'inline_keyboard') else []
        rows = [tv_row] if tv_row else []
        
        if db_enabled():
            sig = chosen_res["signal"]
            # Извлекаем mode и trigger_type для v2
            mode = chosen_res["log"].mode if chosen_res.get("log") else None
            trigger = chosen_res["log"].trigger_type if chosen_res.get("log") else None
            sig_id = db_log_signal(uid, s, chosen_tf, bar_ts, side, sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], chosen_res["reason"], mode=mode, trigger_type=trigger)
            if sig_id:
                rows.append([InlineKeyboardButton("✅ Зайти в позицию", callback_data=f"enter:{sig_id}")])
                kb2 = InlineKeyboardMarkup(rows)
                mid = send_telegram(msg, keyboard=kb2, chat_id=uid)
                try:
                    mid_val = mid.result().message_id if hasattr(mid, 'result') else mid
                except Exception:
                    mid_val = mid
                if mid_val:
                    db_set_signal_message_id(sig_id, int(mid_val))
            else:
                # sig_id is None - сигнал уже существует (дубликат)
                # НЕ отправляем в Telegram чтобы избежать дублей
                print(f"[TICK] Signal duplicate for {s} {chosen_tf} - skipping Telegram send")
        else:
            # БД отключена, добавляем кнопку без callback
            rows.append([InlineKeyboardButton("✅ Зайти в позицию", callback_data="enter:0")])
            kb2 = InlineKeyboardMarkup(rows)
            send_telegram(msg, keyboard=kb2, chat_id=uid)


def tick_once():
    """Глобальный тик по всем пользователям (или по одному чату без БД)."""
    if db_enabled():
        for uid in db_list_users():
            try:
                tick_once_for_user(uid)
            except Exception as e:
                print(f"[ERR] user {uid}: {e}")
    else:
        # Fallback: один чат из TELEGRAM_CHAT_ID
        env_chat = os.getenv("TELEGRAM_CHAT_ID")
        uid = int(env_chat) if env_chat else None
        if uid is None:
            print("No DATABASE_URL and no TELEGRAM_CHAT_ID — nowhere to send.")
            return
        try:
            tick_once_for_user(uid)
        except Exception as e:
            print(f"[ERR] fallback user: {e}")


# =============================================================================
# JOB SCHEDULERS
# =============================================================================

# =============================================================================
# EOD CLOSING FUNCTIONS
# =============================================================================

def _close_user_trades_eod(uid: int) -> dict:
    """
    Закрывает все открытые сделки пользователя в конце дня.
    
    Returns:
        {
            'n_be': int,
            'n_below_entry': int,
            'n_closed': int,
            'errors': int
        }
    """
    from database import (
        db_get_open_trades, db_close_trade_eod, db_get_tp_mid_config
    )
    from binance import get_mark_price_cached
    
    stats = {
        'n_be': 0,
        'n_below_entry': 0,
        'n_closed': 0,
        'errors': 0
    }
    
    # Получаем настройки TPmid
    tp_mid_cfg = db_get_tp_mid_config(uid)
    tp_mid_enabled = tp_mid_cfg.get('enabled', False)
    
    # Получаем открытые сделки
    open_trades = db_get_open_trades(uid)
    
    if not open_trades:
        return stats
    
    print(f"[EOD] User {uid}: found {len(open_trades)} open trades")
    
    for trade in open_trades:
        try:
            trade_id = trade['id']
            symbol = trade['symbol']
            side = str(trade['side']).upper()
            entry = float(trade['entry_price_user'] or 0)
            tp1 = float(trade['tp1'] or 0) if trade.get('tp1') else 0
            tp2 = float(trade['tp2'] or 0) if trade.get('tp2') else 0
            sl = float(trade['sl'] or 0) if trade.get('sl') else 0
            
            if entry <= 0:
                stats['errors'] += 1
                continue
            
            # Получаем текущую цену
            current_price = get_mark_price_cached(symbol)
            if current_price <= 0:
                print(f"[EOD] Failed to get price for {symbol}")
                stats['errors'] += 1
                continue
            
            # Вычисляем TPmid если включен
            tp_mid = None
            if tp_mid_enabled and tp1 > 0 and tp2 > 0:
                if tp_mid_cfg.get('auto', True):
                    tp_mid = (tp1 + tp2) / 2.0
                else:
                    # Manual delta
                    delta_pct = float(tp_mid_cfg.get('delta_pct', 0.1)) / 100.0
                    if side == 'LONG':
                        tp_mid = tp1 + entry * delta_pct
                    else:
                        tp_mid = tp1 - entry * delta_pct
            
            # Определяем тип закрытия по логике
            close_type = None
            
            # Если цена ниже SL - должна была закрыться по SL, пропускаем
            if sl > 0 and ((side == 'LONG' and current_price <= sl) or (side == 'SHORT' and current_price >= sl)):
                print(f"[EOD] Trade {trade_id}: price {current_price} <= SL {sl}, skipping")
                continue
            
            # Если цена выше/равна TP2 - должна была закрыться как TP1+TP2, пропускаем
            if tp2 > 0 and ((side == 'LONG' and current_price >= tp2) or (side == 'SHORT' and current_price <= tp2)):
                print(f"[EOD] Trade {trade_id}: price {current_price} >= TP2 {tp2}, skipping")
                continue
            
            # Определяем тип закрытия
            if side == 'LONG':
                # LONG логика
                if entry <= current_price < tp1:
                    close_type = 'EOD_BE'
                elif tp1 > 0 and tp_mid and tp1 <= current_price < tp_mid:
                    close_type = 'EOD_TP1'  # Технически это TP1, но для статистики не считаем
                    continue  # Пропускаем, не считаем в EOD-статистике
                elif tp_mid and tp_mid <= current_price < tp2:
                    close_type = 'EOD_TPMID'  # Технически это TPmid, но для статистики не считаем
                    continue  # Пропускаем, не считаем в EOD-статистике
                elif sl < current_price < entry:
                    close_type = 'EOD_BELOW_ENTRY'
                else:
                    # Не подходит под категории, пропускаем
                    continue
            else:
                # SHORT логика (обратная)
                if entry >= current_price > tp1:
                    close_type = 'EOD_BE'
                elif tp1 > 0 and tp_mid and tp1 >= current_price > tp_mid:
                    close_type = 'EOD_TP1'
                    continue
                elif tp_mid and tp_mid >= current_price > tp2:
                    close_type = 'EOD_TPMID'
                    continue
                elif sl > current_price > entry:
                    close_type = 'EOD_BELOW_ENTRY'
                else:
                    continue
            
            # Закрываем сделку
            success = db_close_trade_eod(uid, trade_id, current_price, close_type)
            
            if success:
                stats['n_closed'] += 1
                if close_type == 'EOD_BE':
                    stats['n_be'] += 1
                elif close_type == 'EOD_BELOW_ENTRY':
                    stats['n_below_entry'] += 1
                print(f"[EOD] Closed trade {trade_id} ({symbol}) at {current_price} as {close_type}")
            else:
                stats['errors'] += 1
                print(f"[EOD] Failed to close trade {trade_id}")
                
        except Exception as e:
            print(f"[EOD] Error processing trade {trade.get('id', 'unknown')}: {e}")
            stats['errors'] += 1
            continue
    
    return stats


async def _job_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Периодическая задача генерации сигналов"""
    # Если предыдущий тик ещё идёт — мягко скипаем
    if _tick_lock.locked():
        print("[tickjob] previous tick is still running → skip")
        return

    async with _tick_lock:
        t0 = time.time()
        try:
            # Запускаем tick_once без жёсткого таймаута
            await asyncio.to_thread(tick_once)
        except Exception as e:
            print(f"[tickjob] ERROR: {e}")
        finally:
            print(f"[tickjob] done in {time.time()-t0:.2f}s")


# =============================================================================
# RESEARCH (режим «только лог»: все гейты по выборке пар + исходы)
# =============================================================================

async def _job_research_record(ctx: ContextTypes.DEFAULT_TYPE):
    """Раз в минуту: если по какому-то ТФ закрылась новая свеча — пишем кандидатов."""
    from research import record_closed_bars
    try:
        await asyncio.to_thread(record_closed_bars, EXEC)
    except Exception as e:
        print(f"[RESEARCH] record ERROR: {e}")


async def _job_research_resolve(ctx: ContextTypes.DEFAULT_TYPE):
    """Раз в 5 минут: досчитываем исходы кандидатов, у которых прошёл горизонт."""
    from research import resolve_outcomes
    try:
        await asyncio.to_thread(resolve_outcomes)
    except Exception as e:
        print(f"[RESEARCH] resolve ERROR: {e}")


# =============================================================================
# OUTCOME UPDATER
# =============================================================================

def _update_signal_outcome_v2(sig: dict, current_price: float, current_ts_ms: int):
    """
    Обновляет outcome_v2 для одного сигнала на основе текущей цены.
    
    Логика:
    - Если уже закрыт (есть outcome_v2.status != 'OPEN') — пропускаем
    - Проверяем достижение TP1/TP2/SL по текущей цене
    - Обновляем events в БД (hit_tp1_ts, hit_tp2_ts, hit_sl_ts)
    - Вычисляем outcome_v2: {status, r, time_to_tp1_min, ...}
    """
    from database import db_update_signal_events, db_update_signal_outcome
    
    sig_id = sig['id']
    symbol = sig['symbol']
    side = sig['side']
    entry = float(sig['entry'])
    sl = float(sig['sl'])
    tp1 = float(sig['tp1'])
    tp2 = float(sig['tp2'])
    bar_ts = sig['bar_ts']  # миллисекунды
    
    # Проверяем outcome_v2
    outcome_v2 = sig.get('outcome_v2')
    # Пропускаем только ПОЛНОСТЬЮ закрытые сигналы (TP1+TP2, TP1→SL, SL)
    # НЕ пропускаем 'TP1' и 'TP1→OPEN' - они могут ещё дойти до TP2 или SL
    if outcome_v2:
        status = outcome_v2.get('status')
        if status in ('TP1+TP2', 'TP1→SL', 'SL'):
            # Эти статусы финальные, дальше обновлять не нужно
            return
        # 'TP1' и 'TP1→OPEN' и 'OPEN' продолжаем обновлять
    
    # События (из БД)
    hit_tp1_ts = sig.get('hit_tp1_ts')
    hit_tp2_ts = sig.get('hit_tp2_ts')
    hit_sl_ts = sig.get('hit_sl_ts')
    
    # Проверяем достижение уровней
    events_updated = False
    
    if side == 'LONG':
        # TP1
        if not hit_tp1_ts and current_price >= tp1:
            hit_tp1_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: TP1 HIT at {current_price}")
        
        # TP2
        if hit_tp1_ts and not hit_tp2_ts and current_price >= tp2:
            hit_tp2_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: TP2 HIT at {current_price}")
        
        # SL (только если не закрыт)
        if not hit_sl_ts and current_price <= sl:
            hit_sl_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: SL HIT at {current_price}")
    
    else:  # SHORT
        # TP1
        if not hit_tp1_ts and current_price <= tp1:
            hit_tp1_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: TP1 HIT at {current_price}")
        
        # TP2
        if hit_tp1_ts and not hit_tp2_ts and current_price <= tp2:
            hit_tp2_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: TP2 HIT at {current_price}")
        
        # SL (только если не закрыт)
        if not hit_sl_ts and current_price >= sl:
            hit_sl_ts = current_ts_ms
            events_updated = True
            print(f"[OUTCOME] Signal {sig_id}: SL HIT at {current_price}")
    
    # Обновляем events в БД
    if events_updated:
        db_update_signal_events(sig_id, {
            'hit_tp1_price': current_price if hit_tp1_ts else None,
            'hit_tp1_ts': hit_tp1_ts,
            'hit_tp2_price': current_price if hit_tp2_ts else None,
            'hit_tp2_ts': hit_tp2_ts,
            'hit_sl_price': current_price if hit_sl_ts else None,
            'hit_sl_ts': hit_sl_ts
        })
    
    # Определяем outcome_v2
    status = 'OPEN'
    r_multiple = 0.0
    time_to_tp1_min = None
    
    risk = abs(entry - sl)
    
    if hit_tp2_ts:
        # TP1+TP2
        status = 'TP1+TP2'
        # Упрощённо: 0.7×1R + 0.3×(TP2-Entry)/Risk
        reward_tp2 = abs(tp2 - entry)
        r_multiple = 0.7 * 1.0 + 0.3 * (reward_tp2 / risk) if risk > 0 else 0
        time_to_tp1_min = int((hit_tp1_ts - bar_ts) / 60000) if hit_tp1_ts else None
    
    elif hit_tp1_ts and hit_sl_ts:
        # TP1→SL
        status = 'TP1→SL'
        # Упрощённо: 0.7×1R - 0.3×1R = 0.4R
        r_multiple = 0.4
        time_to_tp1_min = int((hit_tp1_ts - bar_ts) / 60000)
    
    elif hit_tp1_ts:
        # TP1 (частичный)
        status = 'TP1'
        r_multiple = 0.7  # 70% позиции закрыто на TP1
        time_to_tp1_min = int((hit_tp1_ts - bar_ts) / 60000)
    
    elif hit_sl_ts:
        # SL до TP1
        status = 'SL'
        r_multiple = -1.0
    
    else:
        # Ещё открыт
        status = 'OPEN'
        r_multiple = 0.0
    
    # Обновляем outcome_v2 в БД
    outcome_obj = {
        'status': status,
        'r': round(r_multiple, 2),
        'time_to_tp1_min': time_to_tp1_min
    }
    
    db_update_signal_outcome(sig_id, outcome_obj)
    
    print(f"[OUTCOME] Signal {sig_id}: status={status}, r={r_multiple:.2f}")


async def _job_update_outcomes(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая задача обновления исходов сделок (TP1/TP2/SL).
    
    Запускается реже чем тик генерации сигналов (раз в 5-15 минут).
    """
    from database import db_enabled, db_list_users, db_get_signals_with_events
    from binance import get_mark_price_cached
    from datetime import datetime, timedelta
    import time
    
    if not db_enabled():
        return
    
    print("[OUTCOME_JOB] Starting outcome update...")
    t0 = time.time()
    
    # Получаем всех пользователей
    users = db_list_users()
    
    for uid in users:
        try:
            # Берём сигналы за последние 7 дней
            since_ms = int((datetime.utcnow() - timedelta(days=7)).timestamp() * 1000)
            signals = db_get_signals_with_events(uid, since_ms)
            
            if not signals:
                continue
            
            print(f"[OUTCOME_JOB] User {uid}: {len(signals)} signals to check")
            
            # Группируем по символам для оптимизации
            by_symbol: dict[str, list] = {}
            for sig in signals:
                symbol = sig['symbol']
                if symbol not in by_symbol:
                    by_symbol[symbol] = []
                by_symbol[symbol].append(sig)
            
            # Обновляем по символам
            for symbol, sigs in by_symbol.items():
                try:
                    # Получаем текущую цену
                    current_price = get_mark_price_cached(symbol, ttl_seconds=30)
                    if current_price == 0:
                        print(f"[OUTCOME_JOB] Failed to get price for {symbol}, skipping")
                        continue
                    
                    current_ts_ms = int(datetime.utcnow().timestamp() * 1000)
                    
                    # Обновляем каждый сигнал
                    for sig in sigs:
                        _update_signal_outcome_v2(sig, current_price, current_ts_ms)
                
                except Exception as e:
                    print(f"[OUTCOME_JOB] Error updating {symbol}: {e}")
                    continue
        
        except Exception as e:
            print(f"[OUTCOME_JOB] Error for user {uid}: {e}")
            continue
    
    elapsed = time.time() - t0
    print(f"[OUTCOME_JOB] Done in {elapsed:.2f}s")


# =============================================================================
# EOD CLOSING AND DAILY DIGEST
# =============================================================================

async def _job_eod_close_and_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая задача для EOD-закрытия сделок и отправки дневной статистики.
    Проверяет каждые 1 минуту, кому пора закрывать (в 23:59 по их TZ).
    """
    from database import (
        db_enabled, db_list_users, db_get_user_timezone,
        db_get_signals_with_events, db_get_eod_closed_stats
    )
    from datetime import datetime, timedelta
    import pytz
    
    if not db_enabled():
        return
    
    print("[EOD_JOB] Checking users for EOD closing...")
    
    try:
        users = db_list_users()
        if not users:
            return
        
        now_utc = datetime.now(pytz.UTC)
        processed_count = 0
        
        for uid in users:
            try:
                # Получаем TZ пользователя
                user_tz_name = db_get_user_timezone(uid)
                try:
                    user_tz = pytz.timezone(user_tz_name)
                except:
                    user_tz = pytz.UTC
                
                now_local = now_utc.astimezone(user_tz)
                
                # Проверяем время - выполняем если 23:59 (в течение этой минуты)
                # Используем проверку на 23:59, чтобы сработало в течение всей минуты
                if now_local.hour == 23 and now_local.minute == 59:
                    print(f"[EOD] Processing user {uid} (TZ: {user_tz_name}, time: {now_local.strftime('%H:%M')})")
                    
                    # Закрываем открытые сделки
                    eod_result = _close_user_trades_eod(uid)
                    print(f"[EOD] User {uid}: closed {eod_result['n_closed']} trades (BE: {eod_result['n_be']}, Below: {eod_result['n_below_entry']})")
                    
                    # Формируем и отправляем статистику
                    await _send_daily_digest(ctx, uid, now_local)
                    
                    processed_count += 1
                    
                    # Небольшая пауза между пользователями
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                print(f"[EOD] Error for user {uid}: {e}")
                continue
        
        if processed_count > 0:
            print(f"[EOD_JOB] Processed {processed_count} user(s)")
            
    except Exception as e:
        print(f"[EOD_JOB] Job failed: {e}")


async def _send_daily_digest(ctx: ContextTypes.DEFAULT_TYPE, uid: int, now_local: datetime):
    """
    Отправляет дневную статистику пользователю.
    
    Args:
        ctx: Context бота
        uid: ID пользователя
        now_local: Локальное время пользователя
    """
    from database import (
        db_get_signals_with_events, db_get_eod_closed_stats,
        db_get_tp_mid_config, _db_conn
    )
    from handlers.handlers import (
        _calculate_stats_metrics, _resolve_all_events_for_signal,
        _determine_signal_path, _check_price_level_hit
    )
    from handlers.message_render import _tp_mid_price_for_signal
    import pytz
    
    try:
        # Рассчитываем границы дня
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        day_start_utc = day_start.astimezone(pytz.UTC)
        day_end_utc = day_end.astimezone(pytz.UTC)
        day_start_ts = int(day_start_utc.timestamp() * 1000)
        day_end_ts = int(day_end_utc.timestamp() * 1000)
        
        # Получаем сигналы за день
        sigs = db_get_signals_with_events(uid, day_start_ts)
        sigs = [s for s in sigs if s.get("sent_at", 0) <= day_end_ts]
        
        if not sigs:
            # Если нет сигналов, всё равно можем отправить сообщение о закрытых сделках
            eod_stats = db_get_eod_closed_stats(uid, day_start_ts, day_end_ts)
            if eod_stats.get('n_be', 0) > 0 or eod_stats.get('n_below_entry', 0) > 0:
                text = (
                    f"📊 <b>Статистика — День</b> (сегодня {now_local.strftime('%d.%m.%Y')})\n\n"
                    f"Всего сигналов: <b>0</b>\n"
                    f"Рассчитано исходов: <b>0</b>\n\n"
                    f"Закрыли сделки:\n\n"
                )
                if eod_stats.get('n_be', 0) > 0:
                    text += f"◻️ BE: <b>{eod_stats['n_be']}</b>\n"
                if eod_stats.get('n_below_entry', 0) > 0:
                    text += f"🔺 Ниже входа: <b>{eod_stats['n_below_entry']}</b>\n"
                await ctx.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            return
        
        # Обновляем события для всех сигналов
        tp_mid_cfg = db_get_tp_mid_config(uid)
        tp_mid_enabled = tp_mid_cfg.get('enabled', False)

        has_tp_mid_columns = False
        if tp_mid_enabled:
            try:
                with _db_conn() as c, c.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1
                          FROM information_schema.columns
                         WHERE table_name = 'signals_log'
                           AND column_name = 'hit_tp_mid_price'
                        """
                    )
                    has_tp_mid_columns = cur.fetchone() is not None
            except Exception as e:
                has_tp_mid_columns = False
                print(f"[EOD] Failed to detect TPmid columns: {e}")
        tp_mid_active = tp_mid_enabled and has_tp_mid_columns
        
        for s in sigs:
            has_events = bool(s.get("hit_tp1_ts")) or bool(s.get("hit_tp2_ts")) or bool(s.get("hit_sl_ts"))
            if not has_events:
                events = _resolve_all_events_for_signal(s, tp_mid_config=tp_mid_cfg if tp_mid_active else None)
                s.update(events)
        
        # TPmid: рассчитать при необходимости
        if tp_mid_active:
            for s in sigs:
                try:
                    mid_price = _tp_mid_price_for_signal(s, tp_mid_cfg)
                    if mid_price:
                        mid_ts = _check_price_level_hit(s, mid_price)
                        s['hit_tp_mid_price'] = mid_price
                        s['hit_tp_mid_ts'] = mid_ts
                except Exception:
                    pass
        
        # Рассчитываем метрики
        metrics = _calculate_stats_metrics(sigs, tp_mid_config=tp_mid_cfg if tp_mid_active else None)
        
        # TPmid метрики уже рассчитаны в _calculate_stats_metrics
        # Не нужно пересчитывать здесь
        
        # Получаем EOD-статистику
        eod_stats = db_get_eod_closed_stats(uid, day_start_ts, day_end_ts)
        
        # Формируем сообщение (EOD формат - отличается от основного)
        date_str = now_local.strftime("%d.%m.%Y")
        text_parts = [
            f"📊 <b>Статистика — День</b> ({date_str})",
            "",
            f"Всего сигналов: <b>{metrics['total']}</b>",
            f"Рассчитано исходов: <b>{metrics['resolved']}</b>",
            "",
            "Пути исходов:",
            "",
            (
                f"🟡 TP1: <b>{metrics['tp1_touch_cnt']}</b> "
                f"(<b>{(metrics['tp1_touch_cnt']/metrics['total']*100 if metrics['total']>0 else 0):.2f}%</b>)"
            ),
        ]
        
        if tp_mid_active and 'n_mid' in metrics:
            text_parts.append(f"🟠 TP1+TPmid: <b>{metrics['n_mid']}</b> (<b>{metrics['pct_mid']:.2f}%</b>)")
        
        # Проверяем, были ли сигналы с TP2, которые прошли через TPmid
        if tp_mid_active and metrics.get('n2_with_tpmid', 0) > 0:
            pct_tpmid_tp2 = (metrics['n2_with_tpmid']/metrics['total']*100) if metrics['total']>0 else 0.0
            text_parts.append(f"🟢 TP1+TPmid+TP2: <b>{metrics['n2_with_tpmid']}</b> (<b>{pct_tpmid_tp2:.2f}%</b>)")
        else:
            text_parts.append(f"🟢 TP1+TP2: <b>{metrics['n2']}</b> (<b>{metrics['pct2']:.2f}%</b>)")
        
        text_parts.append("")
        text_parts.append(f"🔻 SL до TP1: <b>{metrics['n1']}</b> (<b>{metrics['pct1']:.2f}%</b>)")
        if tp_mid_active and 'n_sl_before_tpmid' in metrics:
            text_parts.append(f"🔻 SL до TPmid: <b>{metrics['n_sl_before_tpmid']}</b> (<b>{metrics['pct_sl_before_tpmid']:.2f}%</b>)")
        text_parts.append("")
        text_parts.append(f"🔙 TP1 → SL: <b>{metrics['n3']}</b> (<b>{metrics['pct3']:.2f}%</b>)")
        if tp_mid_active and 'n_mid_be' in metrics:
            text_parts.append(f"⭕️ TPmid → TP1 = BE: <b>{metrics['n_mid_be']}</b> (<b>{metrics['pct_mid_be']:.2f}%</b>)")
        if tp_mid_active and 'n_tpmid_to_sl' in metrics:
            text_parts.append(f"🔙 TPmid → SL: <b>{metrics['n_tpmid_to_sl']}</b> (<b>{metrics['pct_tpmid_to_sl']:.2f}%</b>)")
        
        # Добавляем блок "Закрыли сделки" (всегда показываем, даже если 0)
        text_parts.extend([
            "",
            "Закрыли сделки:",
            "",
        ])
        n_be = eod_stats.get('n_be', 0)
        n_below = eod_stats.get('n_below_entry', 0)
        pct_be = (n_be / metrics['total'] * 100) if metrics['total'] > 0 else 0.0
        pct_below = (n_below / metrics['total'] * 100) if metrics['total'] > 0 else 0.0
        text_parts.append(f"◻️ BE: <b>{n_be}</b> (<b>{pct_be:.2f}%</b>)")
        text_parts.append(f"🔺 Ниже входа: <b>{n_below}</b> (<b>{pct_below:.2f}%</b>)")
        
        text_parts.append("")
        
        # Итог дня (Equity) - не включаем EOD-закрытия
        try:
            from config.config import CONFIG
            from database.database import db_get_closed_trades_with_fills
            from stats.equity import Trade, Leg, day_equity_result
            
            stats_cfg = CONFIG.get("stats", {})
            start_equity = float(stats_cfg.get("equity_start_usd", 100.0))
            include_commissions = bool(stats_cfg.get("include_commissions", True))
            
            raw_trades = db_get_closed_trades_with_fills(uid, day_start_ts)
            trades = []
            for t in raw_trades:
                # Пропускаем EOD-закрытия (они не должны входить в equity)
                legs = t.get("legs", [])
                if any(lg.get("type", "").startswith("EOD_") for lg in legs):
                    continue
                
                legs_obj = [
                    Leg(
                        type=lg.get("type", ""),
                        price=float(lg.get("price", 0.0)),
                        qty_frac=float(lg.get("qty_frac", 0.0)),
                        time=float(lg.get("time", 0.0)),
                    )
                    for lg in legs
                    if float(lg.get("qty_frac", 0.0)) > 0
                ]
                
                if not legs_obj:
                    continue
                
                fee_pct = float(t.get("fee_round_pct", 0.0)) if include_commissions else 0.0
                trades.append(
                    Trade(
                        entry_price=float(t.get("entry_price", 0.0)),
                        side=str(t.get("side", "long")),
                        legs=legs_obj,
                        fee_round_pct=fee_pct,
                        funding_paid_pct=0.0,
                        close_time=float(t.get("close_time", 0.0)),
                    )
                )
            
            if trades:
                trades.sort(key=lambda tr: tr.close_time)
                r_day = day_equity_result(trades, start_equity)
                text_parts.append(f"🟣 Итог дня (Equity от ${int(start_equity)}, 1×): <b>{r_day*100:+.2f}%</b>")
        except Exception as e:
            print(f"[EOD] Error calculating equity: {e}")
        
        text = "\n".join(text_parts)
        
        # Отправляем сообщение
        await ctx.bot.send_message(
            chat_id=uid,
            text=text,
            parse_mode="HTML"
        )
        
        print(f"[EOD] Sent daily digest to user {uid}")
        
    except Exception as e:
        error_str = str(e).lower()
        if '403' in error_str or 'forbidden' in error_str or 'blocked' in error_str:
            print(f"[EOD] User {uid} blocked bot")
        else:
            print(f"[EOD] Failed to send digest to {uid}: {e}")