#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MESSAGE_RENDER.PY — Рендер визуальных карточек сигналов

Форматирует SignalLog в HTML-карточку для Telegram согласно ТЗ §2.3
"""

from strategy.logs import SignalLog
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from utils.helpers import _fmt_price, _get_price_decimals
import pytz


def _tp_mid_price_for_signal(sig: dict, cfg: dict) -> Optional[float]:
    """Рассчитать TPmid цену по сигналу с учётом настроек пользователя.
    auto: середина между TP1 и TP2
    manual: TP1 с дельтой от entry: LONG → +entry*delta, SHORT → -entry*delta
    delta_pct задаётся в процентах (0.1 => 0.1%).
    """
    try:
        entry = float(sig.get('entry') or sig.get('entry_price') or 0.0)
        tp1 = float(sig.get('tp1') or 0.0)
        tp2 = float(sig.get('tp2') or 0.0)
        side = str(sig.get('side', 'LONG')).upper()
        if entry <= 0 or tp1 <= 0 or tp2 <= 0:
            return None
        if bool(cfg.get('auto', True)):
            return (tp1 + tp2) / 2.0
        # manual
        delta_pct = float(cfg.get('delta_pct', 0.1)) / 100.0
        if side == 'LONG':
            return tp1 + entry * delta_pct
        else:
            return tp1 - entry * delta_pct
    except Exception:
        return None


def render_signal_card(log: SignalLog, tv_url: Optional[str] = None, levels_url: Optional[str] = None, uid: Optional[int] = None) -> str:
    """
    Рендерит полную карточку сигнала
    
    Args:
        log: SignalLog с заполненными полями
        tv_url: Ссылка на TradingView (опционально)
        levels_url: Ссылка на карту уровней (опционально)
        uid: ID пользователя для проверки настроек TPmid (опционально)
    
    Returns:
        HTML-форматированная карточка для Telegram
    """
    # Обновляем links если переданы
    if tv_url:
        log.links['tv_url'] = tv_url
    if levels_url:
        log.links['levels_url'] = levels_url
    
    # Рассчитываем TPmid если включен у пользователя
    tp_mid_price = None
    if uid:
        try:
            from database import db_enabled, db_get_tp_mid_config
            if db_enabled():
                cfg = db_get_tp_mid_config(uid)
                if cfg.get('enabled'):
                    # Конвертируем SignalLog в dict для расчета
                    sig_dict = {
                        'entry': log.entry_price,
                        'entry_price': log.entry_price,
                        'tp1': log.tp1_final,
                        'tp2': log.tp2_final,
                        'side': log.side
                    }
                    tp_mid_price = _tp_mid_price_for_signal(sig_dict, cfg)
        except Exception:
            pass
    
    # Строим карточку по секциям
    header = _render_header(log)
    validity_hold = _render_validity_hold(log)
    life = _render_life(log)
    prices = _render_prices(log, tp_mid_price=tp_mid_price)
    gates = _render_gates(log)
    details = _render_details(log)
    footer = _render_footer(log)
    
    # Собираем всё вместе
    parts = [header, validity_hold]
    if life:
        parts.append(life)
    parts.append(f"\n{prices}")
    parts.append(f"\n{gates}")
    if details:
        parts.append(f"\n{details}")
    parts.append(f"\n{footer}")
    
    card = "\n".join(parts)
    
    return card


def _render_header(log: SignalLog) -> str:
    """Рендерит заголовок карточки"""
    # [SYMBOL] [LONG/SHORT] • TF [1h] | Mode: [Intraday] | Trigger: [LQS]
    
    side_emoji = "🟢" if log.side == "LONG" else "🔴"
    mode_cap = log.mode.capitalize()
    
    return (
        f"<b>{log.symbol}</b> {side_emoji} <b>{log.side}</b> • TF <code>{log.timeframe}</code> | "
        f"Mode: <b>{mode_cap}</b> | Trigger: <b>{log.trigger_type}</b>"
    )


def _render_validity_hold(log: SignalLog) -> str:
    """Рендерит Validity и Hold windows"""
    # Validity: до 02:15  •  Hold: 1–2×TF (2–8ч)
    
    # Форматируем valid_until
    if log.valid_until:
        valid_time = log.valid_until.strftime("%H:%M")
    else:
        valid_time = "—"
    
    # Форматируем hold window
    hold_min, hold_max = log.hold_window_min
    if hold_min >= 60:
        # Часы
        hold_text = f"{hold_min//60}–{hold_max//60}ч"
    else:
        # Минуты
        hold_text = f"{hold_min}–{hold_max}м"
    
    validity_mult = log.validity_window_min[1] // ({"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(log.timeframe, 60))
    hold_mult_min = log.hold_window_min[0] // ({"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(log.timeframe, 60))
    hold_mult_max = log.hold_window_min[1] // ({"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(log.timeframe, 60))
    
    return (
        f"⏱ Validity: до <b>{valid_time}</b>  •  "
        f"Hold: <b>{hold_mult_min}–{hold_mult_max}×TF</b> ({hold_text})"
    )


def _render_prices(log: SignalLog, tp_mid_price: Optional[float] = None) -> str:
    """Рендерит блок цен (Entry/SL/TP1/TPmid/TP2/RR/Trail)"""
    lines = []
    
    # Определяем максимальное количество знаков для единообразия
    prices_to_check = [
        log.entry_price,
        log.sl_final,
        log.tp1_final,
        log.tp2_final
    ]
    if tp_mid_price:
        prices_to_check.append(tp_mid_price)
    
    max_decimals = max(_get_price_decimals(p) for p in prices_to_check)
    
    # Форматируем ВСЕ цены с единым количеством знаков
    entry_str = _fmt_price(log.entry_price, log.entry_price, log.symbol, decimals=max_decimals)
    sl_str = _fmt_price(log.sl_final, log.entry_price, log.symbol, decimals=max_decimals)
    tp1_str = _fmt_price(log.tp1_final, log.entry_price, log.symbol, decimals=max_decimals)
    tp2_str = _fmt_price(log.tp2_final, log.entry_price, log.symbol, decimals=max_decimals)
    
    # Entry
    lines.append(f"📍 Entry:  <code>{entry_str}</code>")
    
    # SL
    sl_anchor = f"{log.sl_anchor_type}({log.sl_anchor_tf})" if log.sl_anchor_tf else log.sl_anchor_type
    sl_buffer_text = f"+{log.sl_buffer_atr:.2f}×ATR" if log.sl_buffer_atr > 0 else ""
    sl_floor_text = f"пол ≥{log.sl_vola_floor_atr_pct*100:.2f}% цены"
    
    lines.append(
        f"🛑 SL:     <code>{sl_str}</code>  "
        f"(anchor: <b>{sl_anchor}</b>, {sl_buffer_text}; {sl_floor_text})"
    )
    
    # TP1
    tp1_anchor = f"{log.tp1_anchor_type}({log.tp1_anchor_tf})" if log.tp1_anchor_tf else log.tp1_anchor_type
    tp1_offset_text = f", offset {log.tp1_offset_atr:.2f}×ATR" if log.tp1_offset_atr > 0 else ""
    
    lines.append(
        f"🎯 TP1:    <code>{tp1_str}</code>  "
        f"(anchor: <b>{tp1_anchor}</b>{tp1_offset_text})"
    )
    
    # TPmid (если задан)
    if tp_mid_price:
        tp_mid_str = _fmt_price(tp_mid_price, log.entry_price, log.symbol, decimals=max_decimals)
        # Рассчитываем процент и ATR-множитель для TPmid
        tp_mid_abs = abs(tp_mid_price - log.entry_price)
        tp_mid_pct = tp_mid_abs / log.entry_price * 100
        tp_mid_atr_mult = tp_mid_abs / log.atr if log.atr > 0 else 0
        lines.append(
            f"➕ TPmid:  <code>{tp_mid_str}</code>  ( {tp_mid_pct:+.2f}% · {tp_mid_atr_mult:.2f}×ATR )"
        )
    
    # TP2
    tp2_anchor = f"{log.tp2_anchor_type}({log.tp2_anchor_tf})" if log.tp2_anchor_tf else log.tp2_anchor_type
    if log.tp2_rr_backup_mult_atr_pct > 0:
        tp2_detail = f"RR backup {log.tp2_rr_backup_mult_atr_pct:.1f}×atr_pct"
    else:
        tp2_detail = f"offset {log.tp2_offset_atr:.2f}×ATR"
    
    lines.append(
        f"🎯 TP2:    <code>{tp2_str}</code>  "
        f"(anchor: <b>{tp2_anchor}</b>, {tp2_detail})"
    )
    
    # RR и Trail
    lines.append(
        f"📊 RR: <b>{log.rr_to_sl:.2f}</b> / <b>{log.rr_to_tp2:.2f}</b>   •   "
        f"Trail: <b>{log.trail_atr:.2f}×ATR</b> (≈{log.trail_atr*log.atr:.6g})"
    )
    
    return "\n".join(lines)


def _render_gates(log: SignalLog) -> str:
    """Рендерит блок Gates (светофор)"""
    gates = log.gates
    
    if not gates:
        return "Gates: —"
    
    lines = ["<b>Gates:</b>"]
    
    # Строка 1: Vol, Impulse
    vol = gates.get('vol', {})
    impulse = gates.get('impulse', {})
    
    vol_status = "✅" if vol.get('ok') else "❌"
    vol_text = f"atr_pct {vol.get('atr_pct', 0)*100:.1f}%, {vol.get('quantile', 'Q?')}"
    
    impulse_status = "✅" if impulse.get('ok') else "❌"
    impulse_text = (
        f"PS {impulse.get('pathscore', 0):.2f} / "
        f"AC {impulse.get('anti_chop_atr', 0):.2f}×ATR / "
        f"Mono {impulse.get('mono_pct', 0):.0f}%"
    )
    
    lines.append(
        f"• Vol … {vol_status} ({vol_text})   "
        f"• Impulse … {impulse_status} ({impulse_text})"
    )
    
    # Строка 2: Level, OI
    level = gates.get('level', {})
    oi = gates.get('oi', {})
    
    level_status = "✅" if level.get('ok') else "❌"
    level_text = f"dist {level.get('dist_to_level_atr', 0):.2f}×ATR"
    
    oi_status = "✅" if oi.get('ok') else "❌"
    short_case = oi.get('short_case', '')
    short_case_text = f", case {short_case}" if short_case else ""
    oi_text = (
        f"flow {oi.get('flow', 0):+.2%}, "
        f"perc {oi.get('percentile', 0)}, "
        f"mag {oi.get('mag_gate', 0):.1f}{short_case_text}"
    )
    
    lines.append(
        f"• Level … {level_status} ({level_text})     "
        f"• OI … {oi_status} ({oi_text})"
    )
    
    # Строка 3: Funding/Basis, BTC wind
    funding = gates.get('funding', {})
    basis = gates.get('basis', {})
    btc = gates.get('btc', {})
    
    funding_ok = funding.get('ok', False)
    basis_ok = basis.get('ok', False)
    fb_status = "✅" if (funding_ok and basis_ok) else "❌"
    fb_text = (
        f"{funding.get('now', 0):+.3%} / "
        f"{basis.get('value', 0):+.3%}"
    )
    
    btc_status = "✅" if btc.get('ok') else "❌"
    btc_text = f"{btc.get('votes', '?')}; lock {btc.get('lock_min', 0)}м"
    
    lines.append(
        f"• Funding/Basis … {fb_status} ({fb_text})    "
        f"• BTC wind … {btc_status} ({btc_text})"
    )

    # Строка 4: Positioning (правила v1)
    pos = gates.get('positioning')
    if pos and pos.get('mode') and pos.get('mode') != "off":
        pos_status = "✅" if pos.get('ok') else "❌"
        pos_text = ", ".join(pos.get('confirms') or []) or "—"
        if pos.get('vetoes'):
            pos_text += " | вето: " + ", ".join(pos['vetoes'])
        lines.append(f"• Positioning … {pos_status} ({pos_text})")

    return "\n".join(lines)


def _render_life(log: SignalLog) -> str:
    """Рендерит строку Life (если есть)"""
    if log.life:
        return f"⏰ Life: <b>{log.life}</b>"
    return ""


def _render_details(log: SignalLog) -> str:
    """Рендерит блок Details с legacy metrics"""
    if not log.legacy_metrics:
        return ""
    
    lm = log.legacy_metrics
    lines = ["<b>📊 Details:</b>"]
    
    # Строка 1: ΔP • ΔOI • Funding • Basis
    line1_parts = []
    if lm.get("delta_p_pct") is not None:
        line1_parts.append(f"ΔP {lm['delta_p_pct']:.2f}%")
    if lm.get("delta_oi_pct") is not None:
        line1_parts.append(f"ΔOI {lm['delta_oi_pct']:.2f}%")
    if lm.get("funding_now_pct") is not None:
        line1_parts.append(f"Funding {lm['funding_now_pct']:.3f}%")
    if lm.get("basis_pct") is not None:
        line1_parts.append(f"Basis {lm['basis_pct']:.3f}%")
    
    if line1_parts:
        lines.append(" • ".join(line1_parts))
    
    # Строка 2: LS(top) • Taker • ATR
    line2_parts = []
    if lm.get("ls_top") is not None:
        line2_parts.append(f"LS(top) {lm['ls_top']:.3f}")
    if lm.get("taker_buy_share") is not None:
        line2_parts.append(f"Taker {lm['taker_buy_share']:.3f}")
    if lm.get("atr_abs") is not None and lm.get("atr_pct") is not None:
        line2_parts.append(f"ATR {lm['atr_abs']:.6f} (≈{lm['atr_pct']:.2f}%)")
    
    if line2_parts:
        lines.append(" • ".join(line2_parts))
    
    # Строка 3: SL • TP1 • TP2 moves
    line3_parts = []
    if lm.get("sl_move"):
        sl = lm["sl_move"]
        line3_parts.append(f"SL: {sl['pct']:.2f}% · {sl['atr_mult']:.2f}×ATR")
    if lm.get("tp1_move"):
        tp1 = lm["tp1_move"]
        line3_parts.append(f"TP1: {tp1['pct']:.2f}% · {tp1['atr_mult']:.2f}×ATR")
    if lm.get("tp2_move"):
        tp2 = lm["tp2_move"]
        line3_parts.append(f"TP2: {tp2['pct']:.2f}% · {tp2['atr_mult']:.2f}×ATR")
    
    if line3_parts:
        lines.append(" • ".join(line3_parts))
    
    return "\n".join(lines) if len(lines) > 1 else ""


def _render_footer(log: SignalLog) -> str:
    """Рендерит подвал: Levels, Why, Links"""
    lines = []
    
    # Levels summary
    if log.levels_summary:
        lines.append(f"📌 <b>Levels:</b> {log.levels_summary}")
    
    # Why
    if log.why:
        lines.append(f"💡 <b>Why:</b> {log.why}")
    
    # Links
    link_parts = []
    if log.links.get('tv_url'):
        link_parts.append(f'<a href="{log.links["tv_url"]}">📈 Открыть график</a>')
    # Уровни пары убраны по запросу пользователя
    
    if link_parts:
        lines.append("   ".join(link_parts))
    
    return "\n".join(lines)


def render_signal_card_short(log: SignalLog) -> str:
    """
    Рендерит краткую версию карточки (для списков/уведомлений)
    
    Returns:
        Краткая HTML-карточка
    """
    side_emoji = "🟢" if log.side == "LONG" else "🔴"
    
    # Определяем максимальное количество знаков для единообразия
    max_decimals = max(
        _get_price_decimals(log.entry_price),
        _get_price_decimals(log.sl_final),
        _get_price_decimals(log.tp1_final),
        _get_price_decimals(log.tp2_final)
    )
    
    # Форматируем ВСЕ цены с единым количеством знаков
    entry_str = _fmt_price(log.entry_price, log.entry_price, log.symbol, decimals=max_decimals)
    sl_str = _fmt_price(log.sl_final, log.entry_price, log.symbol, decimals=max_decimals)
    tp1_str = _fmt_price(log.tp1_final, log.entry_price, log.symbol, decimals=max_decimals)
    tp2_str = _fmt_price(log.tp2_final, log.entry_price, log.symbol, decimals=max_decimals)
    
    return (
        f"{side_emoji} <b>{log.symbol}</b> {log.side} • {log.timeframe} | "
        f"{log.trigger_type}\n"
        f"Entry: <code>{entry_str}</code> | "
        f"SL: <code>{sl_str}</code> | "
        f"TP: <code>{tp1_str}</code> / <code>{tp2_str}</code>\n"
        f"RR: {log.rr_to_sl:.2f} / {log.rr_to_tp2:.2f}"
    )


def render_signal_card_from_dict(card: dict) -> str:
    """
    Рендерит карточку сигнала из dict (для UI-миграции без переписывания стратегии)
    
    Args:
        card: signal_card dict с полями symbol, side, tf, mode, trigger_type, gates, etc.
    
    Returns:
        HTML-форматированная карточка для Telegram
    """
    # Заголовок
    side_emoji = "🟢" if card["side"] == "LONG" else "🔴"
    header = (
        f"{side_emoji} <b>{card['symbol']}</b> <b>{card['side']}</b> • "
        f"TF <code>{card['tf']}</code> | "
        f"Mode: <b>{card['mode']}</b> | "
        f"Trigger: <b>{card['trigger_type']}</b>"
    )
    
    # Validity/Hold
    valid_until_dt = datetime.fromisoformat(card['valid_until'])
    hold_until_dt = datetime.fromisoformat(card['hold_until'])
    valid_time = valid_until_dt.strftime("%H:%M")
    
    # Вычисляем hold в человеческом виде
    tf_minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    tf_min = tf_minutes.get(card['tf'], 60)
    hold_delta = (hold_until_dt - valid_until_dt).total_seconds() / 60
    hold_mult_min = int((hold_delta / 2) / tf_min)
    hold_mult_max = int(hold_delta / tf_min)
    hold_hours_min = int((hold_delta / 2) / 60)
    hold_hours_max = int(hold_delta / 60)
    
    if hold_hours_max > 1:
        hold_text = f"{hold_hours_min}–{hold_hours_max}ч"
    else:
        hold_text = f"{int(hold_delta/2)}–{int(hold_delta)}м"
    
    validity = (
        f"⏱ Validity: до <b>{valid_time}</b>  •  "
        f"Hold: <b>{hold_mult_min}–{hold_mult_max}×TF</b> ({hold_text})"
    )
    
    # Цены
    sl = card['sl']
    tp1 = card['tp1']
    tp2 = card['tp2']
    
    # Определяем максимальное количество знаков для единообразия
    symbol = card['symbol']
    entry_price = card['entry_price']
    max_decimals = max(
        _get_price_decimals(entry_price),
        _get_price_decimals(sl['price']),
        _get_price_decimals(tp1['price']),
        _get_price_decimals(tp2['price'])
    )
    
    # Форматируем ВСЕ цены с единым количеством знаков
    entry_str = _fmt_price(entry_price, entry_price, symbol, decimals=max_decimals)
    sl_str = _fmt_price(sl['price'], entry_price, symbol, decimals=max_decimals)
    tp1_str = _fmt_price(tp1['price'], entry_price, symbol, decimals=max_decimals)
    tp2_str = _fmt_price(tp2['price'], entry_price, symbol, decimals=max_decimals)
    
    prices = [
        f"📍 Entry:  <code>{entry_str}</code>",
        "",
        f"🛑 SL:     <code>{sl_str}</code>  "
        f"(anchor: <b>{sl['anchor_type']}({sl['anchor_tf']})</b>, "
        f"+{sl['buffer_atr']:.2f}×ATR; пол ≥{sl['vola_floor_atr_pct']:.2f}%)",
        "",
        f"🎯 TP1:    <code>{tp1_str}</code>  "
        f"(anchor: <b>{tp1['anchor_type']}({tp1['anchor_tf']})</b>, "
        f"offset {tp1['offset_atr']:.2f}×ATR)",
        "",
        f"🎯 TP2:    <code>{tp2_str}</code>  "
        f"(anchor: <b>{tp2['anchor_type']}({tp2['anchor_tf']})</b>, "
        f"RR {tp2['rr_backup_mult_atr_pct']:.2f}×atr_pct)",
        "",
        f"📊 RR: <b>{card['rr']['rr_to_sl']:.2f}</b> / <b>{card['rr']['rr_to_tp2']:.2f}</b>   •   "
        f"Trail: <b>{card['trail_atr']:.2f}×ATR</b>"
    ]
    
    # Gates (светофор)
    gates = card['gates']
    gate_lines = ["<b>Gates:</b>"]
    
    # Строка 1: Vol, Impulse
    vol = gates['vol']
    impulse = gates['impulse']
    vol_status = "✅" if vol['ok'] else "❌"
    impulse_status = "✅" if impulse['ok'] else "❌"
    
    ps = impulse.get('pathscore')
    ac = impulse.get('anti_chop_atr')
    mono = impulse.get('mono_pct')
    
    gate_lines.append(
        f"• Vol … {vol_status} (atr_pct ≈{vol['atr_pct']:.1f}%)   "
        f"• Impulse … {impulse_status} (PS {ps if ps else '—'} / "
        f"AC {ac if ac else '—'}×ATR / Mono {mono if mono else '—'}%)"
    )
    
    # Строка 2: Level, OI
    level = gates['level']
    oi = gates['oi']
    level_status = "✅" if level['ok'] else "❌"
    oi_status = "✅" if oi['ok'] else "❌"
    
    short_case = oi.get('short_case', '')
    case_text = f", case {short_case}" if short_case else ""
    
    gate_lines.append(
        f"• Level … {level_status} (dist ~{level['dist_to_level_atr']:.2f}×ATR)     "
        f"• OI … {oi_status} (flow {oi['flow']:+.3f}%, "
        f"perc ~{oi['percentile']}, mag ~{oi['mag_gate']:.1f}{case_text})"
    )
    
    # Строка 3: Funding/Basis, BTC
    funding = gates['funding']
    basis = gates['basis']
    btc = gates['btc']
    
    fb_status = "✅" if (funding['ok'] and basis['ok']) else "❌"
    btc_status = "✅" if btc['ok'] else "❌"
    
    gate_lines.append(
        f"• Funding/Basis … {fb_status} (F {funding['now']:+.4%} / B {basis['value']:+.4%})    "
        f"• BTC wind … {btc_status} ({btc['votes']}; lock {btc['lock_min']}м)"
    )
    
    # Footer
    footer = [
        "",
        f"📌 <b>Levels:</b> {card['levels_summary']}",
        f"💡 <b>Why:</b> {card['why']}"
    ]
    
    # Собираем всё вместе
    card_lines = [header, validity] + prices + [""] + gate_lines + footer
    
    return "\n".join(card_lines)


# =============================================================================
# ПРОСТОЙ РЕНДЕР ДЛЯ СТАРОЙ СТРАТЕГИИ (strategy.py)
# =============================================================================

# Словарь триггеров (внутренний код → человеческое описание)
TRIGGER_LABELS = {
    "TC":  "Продолжение тренда",
    "LQS": "Лонги зажаты",
    "SQS": "Шорты зажаты", 
    "BO":  "Пробой уровня",
    "RV":  "Откат от уровня",
    "EPS": "Истощение противоположной стороны",
}


def render_signal_simple(res: Dict[str, Any], tf: str, uid: Optional[int] = None) -> str:
    """
    Рендерит сигнал в простом формате по ТЗ (для strategy.py).
    
    Формат эталона:
    🔴 AAVEUSDT SHORT • 15m
    
    Trigger: Продолжение тренда
    Mode: Swing   •  ⏱️ Действителен: до 29 окт 20:02   •  Hold: 3–6×TF (12–24ч)
    
    🚪 Entry: 115321.0
    ➖ SL:   116474.2   ( -1.00% · 1.22×ATR )
    ➕ TP1:  114472.4   ( +0.74% · 0.90×ATR )
    ➕ TP2:  112775.1   ( +2.21% · 2.70×ATR )
    
    RR: 0.60 / 2.00
    Trail: 0.35×ATR
    
    Context:
    • Тренд вниз продолжается
    • Лонги под давлением: цена ↓, OI ↑
    • Крупные кошельки перекошены в лонг (LS≈3.0) → шорт давит их позицию
    • Есть пространство до ближайшей поддержки (~0.6×ATR)
    • Funding/Basis не мешают шорту (F +0.01%, B -0.06%)
    
    ❓Why❓
    ΔP↓ •  & ΔOI↑ •  & Funding↑  (лонги зажаты)
    
    📊 Рынок 📊
    ΔP -0.355%  •  ΔOI +3.538%  •  Basis -0.05%  •  Funding +0.01%
    LS(top) 3.003 (лонги ≈ 3× шортов у топ-счётов)
    Taker 0.476 (покупатель не доминирует)
    ATR 0.003200 (≈1.6%)
    
    Args:
        res: dict с 'signal', 'features', 'reason', 'symbol'
        tf: таймфрейм (например, "15m")
        uid: user_id для определения часового пояса (опционально)
    
    Returns:
        HTML-форматированное сообщение для Telegram (с <b>, <code>, <u> тегами)
    """
    symbol = res["symbol"]
    sig = res["signal"]
    feat = res["features"]
    reason = res.get("reason", "")
    
    side = sig["side"]
    entry = float(sig["entry"])
    sl = float(sig["sl"])
    tp1 = float(sig.get("tp1", 0))
    tp2 = float(sig.get("tp2", 0))
    
    # Получаем метрики
    atr = float(feat.get("ATR", 0))
    atr_pct = (atr / entry * 100) if entry > 0 else 0
    delta_p = float(feat.get("ΔP%", 0))
    delta_oi = float(feat.get("ΔOI%", 0))
    basis = float(feat.get("basis%", 0))
    funding = float(feat.get("funding%", 0))
    ls_top = float(feat.get("LS_top", 1.0))
    taker = float(feat.get("TakerBuyShare", 0.5))
    
    # Определяем trigger из метрик
    # Логика основана на комбинациях ΔP и ΔOI для определения типа триггера
    eps_threshold = 0.05  # Порог для "≈0" (0.05%)
    
    if side == "SHORT":
        # Для SHORT сигналов:
        if delta_p < -eps_threshold and delta_oi > eps_threshold:
            trigger_code = "LQS"  # Лонги зажаты: цена↓, OI↑
        elif delta_p < -eps_threshold and delta_oi < -eps_threshold:
            trigger_code = "EPS"  # Истощение покупателей: цена↓, OI↓
        else:
            trigger_code = "TC"   # Продолжение тренда (вниз)
    else:  # LONG
        # Для LONG сигналов:
        if delta_p > eps_threshold and delta_oi > eps_threshold:
            trigger_code = "TC"   # Продолжение тренда (вверх)
        elif delta_p < -eps_threshold and delta_oi > eps_threshold:
            trigger_code = "SQS"  # Шорты зажаты: цена↓ (но это редко для LONG), OI↑
        elif delta_p > eps_threshold and abs(delta_oi) <= eps_threshold:
            trigger_code = "SQS"  # Шорты зажаты: цена↑, OI≈0 (не подтвержден OI)
        else:
            # По умолчанию: продолжение тренда (может быть и коррекция с восстановлением)
            trigger_code = "TC"
    
    trigger_label = TRIGGER_LABELS.get(trigger_code, trigger_code)
    
    # TF → Mode
    tf_to_mode = {
        "5m": "Scalp", "15m": "Scalp", "30m": "Scalp",
        "1h": "Intraday", "4h": "Swing", "1d": "Swing"
    }
    mode = tf_to_mode.get(tf, "Intraday")
    
    # Validity/Hold с учётом timezone пользователя
    from database import db_enabled, db_get_user_timezone
    
    now_utc = datetime.now(pytz.UTC)
    tf_minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    tf_min = tf_minutes.get(tf, 60)
    
    valid_until_utc = now_utc + timedelta(minutes=tf_min * 2)
    
    # Конвертируем в timezone пользователя
    if uid and db_enabled():
        try:
            user_tz_str = db_get_user_timezone(uid)
            user_tz = pytz.timezone(user_tz_str)
            valid_until_local = valid_until_utc.astimezone(user_tz)
        except Exception as e:
            print(f"[WARN] Failed to get user timezone: {e}, using UTC")
            valid_until_local = valid_until_utc
    else:
        valid_until_local = valid_until_utc
    
    # Форматируем с датой: "29 окт 20:02"
    months_ru = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
    day = valid_until_local.day
    month = months_ru[valid_until_local.month - 1]
    time_str = valid_until_local.strftime("%H:%M")
    valid_time = f"{day} {month} {time_str}"
    
    # Hold window с множителями TF
    if mode == "Scalp":
        hold_mult_min, hold_mult_max = 1, 3
        hold_text = "30–90м"
    elif mode == "Intraday":
        hold_mult_min, hold_mult_max = 2, 6
        hold_text = "2–6ч"
    else:  # Swing
        hold_mult_min, hold_mult_max = 3, 6
        hold_text = "12–24ч"
    
    # Эмодзи стороны
    side_emoji = "🟢" if side == "LONG" else "🔴"
    
    # Расчёт рисков и потенциалов
    risk_abs = abs(sl - entry)
    tp1_abs = abs(tp1 - entry)
    tp2_abs = abs(tp2 - entry)
    
    # Проценты (знаки показывают прибыль/убыток, не направление)
    # Для любой стороны: риск = минус (убыток), потенциал = плюс (прибыль)
    sl_pct = -risk_abs / entry * 100    # риск всегда с минусом (убыток)
    tp1_pct = tp1_abs / entry * 100     # потенциал всегда с плюсом (прибыль)
    tp2_pct = tp2_abs / entry * 100     # потенциал всегда с плюсом (прибыль)
    
    # ATR множители
    sl_atr_mult = risk_abs / atr if atr > 0 else 0
    tp1_atr_mult = tp1_abs / atr if atr > 0 else 0
    tp2_atr_mult = tp2_abs / atr if atr > 0 else 0
    
    # RR
    rr_tp1 = tp1_abs / risk_abs if risk_abs > 0 else 0
    rr_tp2 = tp2_abs / risk_abs if risk_abs > 0 else 0
    
    # Trail
    trail_step = float(sig.get("trail_step", 0))
    trail_atr_mult = trail_step / atr if atr > 0 else 0.35
    
    # ========== ЗАГОЛОВОК ==========
    header = f"{side_emoji} <b>{symbol} {side} • {tf}</b>"
    
    # ========== TRIGGER / MODE / ВРЕМЯ ==========
    mode_line = f"Trigger: <b>{trigger_label}</b>\n\nMode: <b>{mode}</b>   •  ⏱️ Действителен: до <b>{valid_time}</b>  •  Hold: <b>{hold_mult_min}–{hold_mult_max}×TF</b> ({hold_text})"
    
    # ========== УРОВНИ ==========
    # Рассчитываем TPmid если включен у пользователя
    tp_mid_price = None
    tp_mid_pct = None
    tp_mid_atr_mult = None
    if uid:
        try:
            from database import db_enabled, db_get_tp_mid_config
            if db_enabled():
                cfg = db_get_tp_mid_config(uid)
                if cfg.get('enabled'):
                    sig_dict = {
                        'entry': entry,
                        'entry_price': entry,
                        'tp1': tp1,
                        'tp2': tp2,
                        'side': side
                    }
                    tp_mid_price = _tp_mid_price_for_signal(sig_dict, cfg)
                    if tp_mid_price:
                        tp_mid_abs = abs(tp_mid_price - entry)
                        tp_mid_pct = tp_mid_abs / entry * 100
                        tp_mid_atr_mult = tp_mid_abs / atr if atr > 0 else 0
        except Exception:
            pass
    
    # Определяем максимальное количество знаков среди ВСЕХ цен для единообразия
    prices_to_check = [entry, sl, tp1, tp2]
    if tp_mid_price:
        prices_to_check.append(tp_mid_price)
    max_decimals = max(_get_price_decimals(p) for p in prices_to_check)
    
    # Форматируем ВСЕ цены с единым количеством знаков
    entry_str = _fmt_price(entry, entry, symbol, decimals=max_decimals)
    sl_str = _fmt_price(sl, entry, symbol, decimals=max_decimals)
    tp1_str = _fmt_price(tp1, entry, symbol, decimals=max_decimals)
    tp2_str = _fmt_price(tp2, entry, symbol, decimals=max_decimals)
    
    prices = [
        f"🚪 Entry: <code>{entry_str}</code>",
        f"➖ SL:   <code>{sl_str}</code>   ( {sl_pct:+.2f}% · {sl_atr_mult:.2f}×ATR )",
        f"➕ TP1:  <code>{tp1_str}</code>  ( {tp1_pct:+.2f}% · {tp1_atr_mult:.2f}×ATR )",
    ]
    
    # Добавляем TPmid если есть
    if tp_mid_price:
        tp_mid_str = _fmt_price(tp_mid_price, entry, symbol, decimals=max_decimals)
        prices.append(f"➕ TPmid: <code>{tp_mid_str}</code>  ( {tp_mid_pct:+.2f}% · {tp_mid_atr_mult:.2f}×ATR )")
    
    prices.append(f"➕ TP2:  <code>{tp2_str}</code>  ( {tp2_pct:+.2f}% · {tp2_atr_mult:.2f}×ATR )")
    
    # ========== RR И TRAIL ==========
    rr_trail = f"<b>RR:</b> {rr_tp1:.2f} / {rr_tp2:.2f}\n<b>Trail:</b> {trail_atr_mult:.2f}×ATR"
    
    # ========== CONTEXT ==========
    context_bullets = []
    
    # 1. Тренд/Давление (по триггеру и метрикам)
    if trigger_code == "TC":
        if side == "SHORT":
            context_bullets.append("• 📉 Тренд вниз продолжается")
        else:
            context_bullets.append("• 📈 Тренд вверх продолжается")
    elif trigger_code == "LQS":
        # Формируем стрелки динамически на основе реальных значений ΔP и ΔOI
        price_arrow = "↑" if delta_p > 0 else ("↓" if delta_p < 0 else "→")
        oi_arrow = "↑" if delta_oi > 0 else ("↓" if delta_oi < 0 else "→")
        context_bullets.append(f"• 🔻 Лонги под давлением: цена {price_arrow}, OI {oi_arrow}")
    elif trigger_code == "SQS":
        # Формируем стрелки динамически на основе реальных значений ΔP и ΔOI
        price_arrow = "↑" if delta_p > 0 else ("↓" if delta_p < 0 else "→")
        oi_arrow = "↑" if delta_oi > 0 else ("↓" if delta_oi < 0 else "→")
        context_bullets.append(f"• 🔺 Шорты под давлением: цена {price_arrow}, OI {oi_arrow}")
    elif trigger_code == "EPS":
        # Истощение противоположной стороны
        price_arrow = "↑" if delta_p > 0 else ("↓" if delta_p < 0 else "→")
        oi_arrow = "↑" if delta_oi > 0 else ("↓" if delta_oi < 0 else "→")
        if side == "SHORT":
            context_bullets.append(f"• ⚠️ Истощение покупателей: цена {price_arrow}, OI {oi_arrow}")
        else:
            context_bullets.append(f"• ⚠️ Истощение шортистов: цена {price_arrow}, OI {oi_arrow}")
    
    # 2. Перекос крупных счетов
    if ls_top >= 2.5:
        ls_rounded = round(ls_top, 1)
        context_bullets.append(f"• 🐋 Крупные кошельки перекошены в лонг (LS≈{ls_rounded}) → шорт давит их позицию")
    elif ls_top <= 0.4:
        ls_inv_rounded = round(1/ls_top, 1)
        context_bullets.append(f"• 🐋 Крупные кошельки перекошены в шорт (LS≈{ls_top:.1f}) → лонг давит их позицию")
    
    # 3. Пространство до уровня (заглушка, можно дополнить если есть данные уровней)
    # context_bullets.append(f"• 🎯 Есть пространство до ближайшей {'поддержки' if side == 'SHORT' else 'сопротивления'} (~0.6×ATR)")
    
    # 4. Funding/Basis
    if side == "SHORT":
        context_bullets.append(f"• ⚖️ Funding/Basis не мешают шорту (F {funding:+.2f}%, B {basis:+.2f}%)")
    else:
        context_bullets.append(f"• ⚖️ Funding/Basis не мешают лонгу (F {funding:+.2f}%, B {basis:+.2f}%)")
    
    context = "<b>Context:</b>\n\n" + "\n".join(context_bullets)
    
    # ========== WHY ==========
    # Формируем технический reason из метрик
    why_parts = []
    if abs(delta_p) > 0.1:
        why_parts.append(f"ΔP{'↓' if delta_p < 0 else '↑'}")
    if abs(delta_oi) > 0.5:
        why_parts.append(f"ΔOI{'↑' if delta_oi > 0 else '↓'}")
    if abs(funding) > 0.005:
        why_parts.append(f"Funding{'↑' if funding > 0 else '↓'}")
    
    # Добавляем интерпретацию из reason или триггера
    if trigger_code == "LQS":
        why_explain = "<b>(лонги зажаты)</b>"
    elif trigger_code == "SQS":
        why_explain = "<b>(шорты зажаты)</b>"
    elif trigger_code == "EPS":
        why_explain = "<b>(истощение противоположной стороны)</b>"
    elif reason and "(" in reason and ")" in reason:
        # Извлекаем текст в скобках из reason
        why_explain = f"<b>{reason[reason.find('('):reason.rfind(')')+1]}</b>"
    else:
        why_explain = ""
    
    why_formula = " •  & ".join(why_parts)
    why_block = f"❓<b>Why</b>❓\n\n {why_formula}  {why_explain}" if why_parts else ""
    
    # ========== 📊 РЫНОК 📊 ==========
    market_line1 = f"<i>ΔP</i> {delta_p:+.3f}%  •  <i>ΔOI</i> {delta_oi:+.3f}%  •  <i>Basis</i> {basis:+.2f}%  •  <i>Funding</i> {funding:+.2f}%"
    
    # LS(top) с пояснением
    if ls_top >= 2.5:
        ls_label = f"<i>LS(top)</i> {ls_top:.3f} <u>(лонги ≈ {round(ls_top, 1)}× шортов у топ-счётов)</u>"
    elif ls_top <= 0.4:
        ls_label = f"<i>LS(top)</i> {ls_top:.3f} <u>(шорты ≈ {round(1/ls_top, 1)}× лонгов у топ-счётов)</u>"
    else:
        ls_label = f"<i>LS(top)</i> {ls_top:.3f}"
    
    # Taker с интерпретацией
    if taker < 0.50:
        taker_label = f"<i>Taker</i> {taker:.3f} (покупатель не доминирует)"
    else:
        taker_label = f"<i>Taker</i> {taker:.3f} (покупатель доминирует)"
    
    # ATR
    atr_label = f"<i>ATR</i> {atr:.6f} (≈{atr_pct:.1f}%)"
    
    market = f"\n\n📊 <b>Рынок</b> 📊 \n\n{market_line1}\n{ls_label}\n{taker_label}\n{atr_label}"
    
    # ========== СОБИРАЕМ ВСЕМ ВМЕСТЕ ==========
    parts = [
        header,
        "",
        mode_line,
        "",
        "\n".join(prices),
        "",
        rr_trail,
        "",
        context,
    ]
    
    # Добавляем Why если есть
    if why_block:
        parts.append("")
        parts.append(why_block)
    
    # Добавляем рынок
    parts.append(market)
    
    return "\n".join(parts)

