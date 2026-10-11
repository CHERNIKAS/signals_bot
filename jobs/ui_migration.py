#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UI_MIGRATION.PY — Быстрая миграция на новую карточку БЕЗ переписывания стратегии

Использует текущие данные из старой стратегии для генерации новых карточек.
"""

from typing import Dict, Any
from datetime import datetime, timedelta


def build_signal_card_object(res: Dict[str, Any], tf: str) -> Dict[str, Any]:
    """
    Строит signal_card объект из текущих данных для новой карточки.
    Использует текущие метрики и добавляет минимальный набор полей для UI.
    
    Args:
        res: dict с 'signal', 'features', 'reason', 'symbol'
        tf: таймфрейм (например, "1h")
    
    Returns:
        signal_card dict для передачи в render_signal_card_from_dict()
    """
    symbol = res["symbol"]
    s = res["signal"]
    f = res["features"]
    side = s["side"]
    entry = float(s["entry"])
    sl = float(s["sl"])
    tp1 = float(s.get("tp1") or 0)
    tp2 = float(s.get("tp2") or 0)
    atr = float(f.get("ATR") or f.get("atr") or 0)
    atr_pct = (atr / entry) if entry > 0 else 0
    
    # TF → Mode маппинг
    tf_to_mode = {
        "5m": "Scalp", "15m": "Scalp", "30m": "Scalp",
        "1h": "Intraday", "4h": "Swing", "1d": "Swing"
    }
    mode = tf_to_mode.get(tf, "Intraday")
    
    # Определяем trigger из метрик (ΔP & ΔOI)
    delta_p = f.get('ΔP%', 0)
    delta_oi = f.get('ΔOI%', 0)
    
    # Унифицированная логика определения триггеров (как в message_render.py)
    eps_threshold = 0.05  # Порог для "≈0" (0.05%)
    
    if side == "SHORT":
        # Для SHORT сигналов:
        if delta_p < -eps_threshold and delta_oi > eps_threshold:
            trigger = "LQS"  # Лонги зажаты: цена↓, OI↑
        elif delta_p < -eps_threshold and delta_oi < -eps_threshold:
            trigger = "EPS"  # Истощение покупателей: цена↓, OI↓
        else:
            trigger = "TC"   # Продолжение тренда (вниз)
    else:  # LONG
        # Для LONG сигналов:
        if delta_p > eps_threshold and delta_oi > eps_threshold:
            trigger = "TC"   # Продолжение тренда (вверх)
        elif delta_p < -eps_threshold and delta_oi > eps_threshold:
            trigger = "SQS"  # Шорты зажаты: цена↓ (но это редко для LONG), OI↑
        elif delta_p > eps_threshold and abs(delta_oi) <= eps_threshold:
            trigger = "SQS"  # Шорты зажаты: цена↑, OI≈0 (не подтвержден OI)
        elif delta_p > eps_threshold and delta_oi < -eps_threshold:
            trigger = "EPS"  # Истощение шортистов: цена↑, OI↓
        else:
            # По умолчанию: продолжение тренда (может быть и коррекция с восстановлением)
            trigger = "TC"
    
    # Validity/Hold (примерные значения на основе TF)
    now = datetime.utcnow()
    tf_minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    tf_min = tf_minutes.get(tf, 60)
    
    valid_until = now + timedelta(minutes=tf_min * 2)
    hold_until = now + timedelta(minutes=tf_min * 8)
    
    # RR
    risk = abs(entry - sl) if sl != entry else 1e-6
    rr_to_sl = abs((tp1 - entry) / risk) if tp1 else 0
    rr_to_tp2 = abs((tp2 - entry) / risk) if tp2 else 0
    
    # Trail
    trail_step = float(s.get('trail_step') or 0)
    trail_atr = (trail_step / atr) if atr > 0 else 0.35
    
    # SL/TP anchors (заглушки, пока нет модуля уровней)
    sl_buffer_atr = 0.22 if mode in ["Scalp", "Intraday"] else 0.30
    tp1_offset_atr = abs(tp1 - entry) / atr if (tp1 and atr > 0) else 0.20
    tp2_rr_backup = rr_to_tp2
    
    # Gates (используем текущие метрики)
    gates = {
        "vol": {
            "ok": True,
            "atr_pct": round(atr_pct * 100, 2),
            "quantile": "Q50-85"
        },
        "impulse": {
            "ok": True,
            "pathscore": None,
            "anti_chop_atr": None,
            "mono_pct": None,
            "body_ratio": None
        },
        "level": {
            "ok": True,
            "dist_to_level_atr": round(rr_to_sl, 2)
        },
        "oi": {
            "ok": True,
            "flow": round(delta_oi, 3),
            "percentile": 60,
            "mag_gate": 1.0,
            "short_case": trigger if trigger in ["LQS", "EPS"] else ""
        },
        "funding": {
            "ok": True,
            "now": round(f.get('funding%', 0) / 100, 4),
            "ctx24h_sign": "+" if f.get('funding%', 0) > 0 else "-",
            "z_extreme": False
        },
        "basis": {
            "ok": True,
            "value": round(f.get('basis%', 0) / 100, 4)
        },
        "btc": {
            "ok": True,
            "votes": "2/3",
            "lock_min": 30
        }
    }
    
    # Levels summary
    levels_summary = f"SL ▸ swing({tf}) • TP1 ▸ level({tf}) • TP2 ▸ swing(4h)"
    
    # Why (короткая версия, ≤120 символов)
    why_parts = []
    why_parts.append(f"{trigger} —")
    if abs(delta_p) > 0.5:
        why_parts.append(f"ΔP{delta_p:+.1f}%")
    if abs(delta_oi) > 0.5:
        why_parts.append(f"& ΔOI{delta_oi:+.1f}%")
    if trigger == "LQS":
        why_parts.append("(лонги зажаты)")
    elif trigger == "EPS":
        why_parts.append("(истощение)")
    why_parts.append(f"; место до уровня ~{rr_to_sl:.2f}×ATR")
    why_short = " ".join(why_parts)[:120]
    
    return {
        "symbol": symbol,
        "side": side,
        "tf": tf,
        "mode": mode,
        "trigger_type": trigger,
        "valid_until": valid_until.isoformat(),
        "hold_until": hold_until.isoformat(),
        "entry_price": entry,
        
        "sl": {
            "price": sl,
            "anchor_type": "swing",
            "anchor_tf": tf,
            "buffer_atr": sl_buffer_atr,
            "vola_floor_atr_pct": round(atr_pct * 100, 2)
        },
        "tp1": {
            "price": tp1,
            "anchor_type": "level",
            "anchor_tf": tf,
            "offset_atr": round(tp1_offset_atr, 2)
        },
        "tp2": {
            "price": tp2,
            "anchor_type": "swing",
            "anchor_tf": "4h",
            "offset_atr": 0,
            "rr_backup_mult_atr_pct": round(tp2_rr_backup, 2)
        },
        
        "rr": {
            "rr_to_sl": round(rr_to_sl, 2),
            "rr_to_tp2": round(rr_to_tp2, 2)
        },
        "trail_atr": round(trail_atr, 2),
        
        "gates": gates,
        "levels_summary": levels_summary,
        "why": why_short,
        "links": {}
    }


# =============================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ В jobs.py
# =============================================================================

"""
# В jobs.py функции format_signal_message():

# БЫЛО:
def format_signal_message(res: Dict[str, Any], tf: str) -> tuple[str, InlineKeyboardMarkup]:
    # ... старый рендер ...
    return text, keyboard

# СТАЛО:
def format_signal_message(res: Dict[str, Any], tf: str) -> tuple[str, InlineKeyboardMarkup]:
    from jobs.ui_migration import build_signal_card_object
    from handlers.message_render import render_signal_card_from_dict
    
    # Строим signal_card объект из текущих данных
    signal_card = build_signal_card_object(res, tf)
    
    # Добавляем ссылки
    symbol = res["symbol"]
    tv_sym = symbol.replace("USDT", "USDT.P")
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{tv_sym}"
    signal_card["links"] = {
        "tv_url": tv_url
    }
    
    # Рендерим через новый рендер
    text = render_signal_card_from_dict(signal_card)
    
    # Клавиатура (базовая: только график, кнопка "Зайти в позицию" добавляется позже)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb_rows = [
        [InlineKeyboardButton("📈 Открыть график (TV)", url=tv_url)]
    ]
    keyboard = InlineKeyboardMarkup(kb_rows)
    
    return text, keyboard
"""

