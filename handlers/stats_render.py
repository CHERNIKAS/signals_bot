#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STATS_RENDER.PY — Категорийная статистика и диагностика

Функции для:
- Сводки (день/неделя/месяц) §4.1
- Разбивок по режимам/триггерам/TF/парам §4.2
- Фильтров/банов §4.3
- Диагностики и авто-советов §4.4
"""

from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import statistics


def render_summary_stats(logs: List[Dict], period: str = "day") -> str:
    """
    Рендерит сводную статистику (§4.1)
    
    Args:
        logs: Список SignalLog.to_dict()
        period: "day", "week", "month"
    
    Returns:
        HTML-форматированная статистика
    """
    # Фильтруем по периоду
    now = datetime.utcnow()
    if period == "day":
        cutoff = now - timedelta(days=1)
        period_name = "День"
    elif period == "week":
        cutoff = now - timedelta(days=7)
        period_name = "Неделя"
    else:  # month
        cutoff = now - timedelta(days=30)
        period_name = "Месяц"
    
    filtered = [
        log for log in logs
        if datetime.fromisoformat(log['timestamp']) >= cutoff
    ]
    
    if not filtered:
        return f"📊 Статистика — {period_name}\n\nНет данных за этот период."
    
    # Вычисляем метрики
    total = len(filtered)
    outcomes = _extract_outcomes(filtered)
    
    closed = outcomes['sl_before'] + outcomes['tp1_sl'] + outcomes['tp1_tp2']
    open_count = outcomes['open'] + outcomes['tp1_open']
    
    # Winrates
    wr_final = (outcomes['tp1_tp2'] / closed * 100) if closed > 0 else 0
    wr_touch = ((outcomes['tp1_tp2'] + outcomes['tp1_sl'] + outcomes['tp1_open']) / (closed + outcomes['tp1_open']) * 100) if (closed + outcomes['tp1_open']) > 0 else 0
    wr_weighted = ((outcomes['tp1_tp2'] + 0.5 * outcomes['tp1_sl']) / closed * 100) if closed > 0 else 0
    
    # EV/сделку
    ev_per_trade = _calculate_ev_per_trade(filtered, outcomes)
    
    # Средний RR побед
    avg_rr_wins = _calculate_avg_rr_wins(filtered, outcomes)
    
    # Медиана t→TP1
    median_t_tp1 = _calculate_median_time_to_tp1(filtered)
    
    # Строим отчёт
    lines = [
        f"📊 <b>Статистика — {period_name}</b>",
        f"Всего сигналов: <b>{total}</b>    Рассчитано исходов: <b>{closed}</b>    ⏳ Открыто: <b>{open_count}</b>",
        "",
        "<b>WINRATE:</b>",
        f"🎯 Финальный: <b>{wr_final}%</b>    "
        f"🟡 Touch TP1: <b>{wr_touch}%</b>    "
        f"🟣 Взвешенный: <b>{wr_weighted}%</b>",
        "",
        "<b>Пути исходов:</b>",
        f"🔴 SL до TP1: <b>{outcomes['sl_before']}</b> ({outcomes['sl_before']/total*100 if total > 0 else 0}%)    "
        f"🟠 TP1→SL: <b>{outcomes['tp1_sl']}</b> ({outcomes['tp1_sl']/total*100 if total > 0 else 0}%)",
        f"🟢 TP1+TP2: <b>{outcomes['tp1_tp2']}</b> ({outcomes['tp1_tp2']/total*100 if total > 0 else 0}%)      "
        f"⚪️ TP1→OPEN: <b>{outcomes['tp1_open']}</b> ({outcomes['tp1_open']/total*100 if total > 0 else 0}%)",
        "",
        f"💰 EV/сделку: <b>{ev_per_trade}R</b>    "
        f"📈 Средний RR побед: <b>{avg_rr_wins}R</b>    "
        f"⏱ Медиана t→TP1: <b>{median_t_tp1}м</b>"
    ]
    
    return "\n".join(lines)


def render_mode_breakdown(logs: List[Dict]) -> str:
    """Рендерит разбивку по режимам (§4.2)"""
    # Группируем по mode
    by_mode = defaultdict(list)
    for log in logs:
        mode = log.get('mode', 'unknown')
        by_mode[mode].append(log)
    
    if not by_mode:
        return "<b>По режимам:</b>\nНет данных"
    
    lines = ["<b>📊 По режимам:</b>"]
    lines.append("")
    lines.append("Режим     │ Сигналы │ Win  │ SL→TP1 │ TP1→SL │ TP1+TP2 │ TP1 Hit% │ EV/сделку")
    lines.append("─────────┼─────────┼──────┼────────┼────────┼─────────┼──────────┼──────────")
    
    for mode in sorted(by_mode.keys()):
        mode_logs = by_mode[mode]
        stats = _calculate_group_stats(mode_logs)
        
        lines.append(
            f"{mode:9s} │ {stats['total']:7d} │ {stats['wr_final']:4.1f}% │ "
            f"{stats['sl_before']:6d} │ {stats['tp1_sl']:6d} │ {stats['tp1_tp2']:7d} │ "
            f"{stats['wr_touch']:8.1f}% │ {stats['ev']:+9.2f}R"
        )
    
    return "\n".join(lines)


def render_trigger_breakdown(logs: List[Dict]) -> str:
    """Рендерит разбивку по триггерам (§4.2)"""
    # Группируем по trigger_type
    by_trigger = defaultdict(list)
    for log in logs:
        trigger = log.get('trigger_type', 'unknown')
        by_trigger[trigger].append(log)
    
    if not by_trigger:
        return "<b>По триггерам:</b>\nНет данных"
    
    lines = ["<b>📊 По типам триггеров:</b>"]
    lines.append("")
    lines.append("Тип │ Сигналы │ Win  │ SL→TP1 │ TP1→SL │ TP1+TP2 │ TP1 Hit% │ EV/сделку")
    lines.append("────┼─────────┼──────┼────────┼────────┼─────────┼──────────┼──────────")
    
    for trigger in sorted(by_trigger.keys()):
        trigger_logs = by_trigger[trigger]
        stats = _calculate_group_stats(trigger_logs)
        
        lines.append(
            f"{trigger:3s} │ {stats['total']:7d} │ {stats['wr_final']:4.1f}% │ "
            f"{stats['sl_before']:6d} │ {stats['tp1_sl']:6d} │ {stats['tp1_tp2']:7d} │ "
            f"{stats['wr_touch']:8.1f}% │ {stats['ev']:+9.2f}R"
        )
    
    return "\n".join(lines)


def render_tf_breakdown(logs: List[Dict]) -> str:
    """Рендерит разбивку по таймфреймам (§4.2)"""
    # Группируем по timeframe
    by_tf = defaultdict(list)
    for log in logs:
        tf = log.get('timeframe', 'unknown')
        by_tf[tf].append(log)
    
    if not by_tf:
        return "<b>По TF:</b>\nНет данных"
    
    lines = ["<b>📊 По таймфреймам:</b>"]
    lines.append("")
    lines.append("TF  │ Сигналы │ Win  │ SL→TP1 │ TP1→SL │ TP1+TP2 │ EV/сделку │ t→TP1 мед.")
    lines.append("────┼─────────┼──────┼────────┼────────┼─────────┼───────────┼───────────")
    
    tf_order = ["5m", "15m", "30m", "1h", "4h", "1d"]
    for tf in tf_order:
        if tf not in by_tf:
            continue
        
        tf_logs = by_tf[tf]
        stats = _calculate_group_stats(tf_logs)
        median_t = _calculate_median_time_to_tp1(tf_logs)
        
        lines.append(
            f"{tf:3s} │ {stats['total']:7d} │ {stats['wr_final']:4.1f}% │ "
            f"{stats['sl_before']:6d} │ {stats['tp1_sl']:6d} │ {stats['tp1_tp2']:7d} │ "
            f"{stats['ev']:+10.2f}R │ {median_t:10d}м"
        )
    
    return "\n".join(lines)


def render_filters_stats(logs: List[Dict], ban_logs: List[Dict]) -> str:
    """Рендерит статистику фильтров/банов (§4.3)"""
    if not ban_logs:
        return "<b>Фильтры:</b>\nНет данных о банах"
    
    # Подсчитываем баны по типам
    ban_counts = defaultdict(int)
    for log in ban_logs:
        reasons = log.get('ban_reasons', [])
        for reason in reasons:
            ban_counts[reason] += 1
    
    total_candidates = len(logs) + len(ban_logs)
    
    lines = ["<b>📋 Фильтры:</b>"]
    
    # Dist-to-Level BAN
    space_bans = ban_counts.get('SPACE_TOO_TIGHT', 0)
    if space_bans > 0:
        lines.append(f"• Dist-to-Level BAN: <b>{space_bans/total_candidates*100:.0f}%</b> кандидатов ({space_bans} из {total_candidates})")
    
    # Funding/Basis BAN
    funding_bans = ban_counts.get('FUNDING_EXTREME', 0) + ban_counts.get('FUNDING_AGAINST', 0)
    basis_bans = ban_counts.get('BASIS_OUT_OF_RANGE', 0)
    if funding_bans + basis_bans > 0:
        lines.append(f"• Funding/Basis BAN: <b>{funding_bans + basis_bans}</b> кейс(а)")
    
    # BTC Lock
    btc_bans = ban_counts.get('BTC_WIND_OPPOSITE', 0) + ban_counts.get('BTC_LOCK_ACTIVE', 0)
    if btc_bans > 0:
        lines.append(f"• BTC Lock: <b>{btc_bans}</b> кейс(а)")
    
    # ΔOI-глитч
    oi_bans = ban_counts.get('OI_ANOMALY', 0)
    if oi_bans > 0:
        lines.append(f"• ΔOI-глитч: <b>{oi_bans}</b> кейс")
    
    # Impulse
    impulse_bans = (
        ban_counts.get('IMPULSE_LOW_PATHSCORE', 0) +
        ban_counts.get('IMPULSE_HIGH_CHOP', 0) +
        ban_counts.get('IMPULSE_LOW_MONOTONICITY', 0)
    )
    if impulse_bans > 0:
        lines.append(f"• Impulse BAN: <b>{impulse_bans}</b> кейс(а)")
    
    return "\n".join(lines)


def render_diagnostics(logs: List[Dict]) -> str:
    """Рендерит диагностику и авто-советы (§4.4)"""
    if not logs:
        return "<b>Диагностика:</b>\nНедостаточно данных"
    
    lines = ["<b>🔧 Диагностика:</b>"]
    
    # Анализ по режимам
    by_mode = defaultdict(list)
    for log in logs:
        mode = log.get('mode', '')
        by_mode[mode].append(log)
    
    for mode, mode_logs in by_mode.items():
        outcomes = _extract_outcomes(mode_logs)
        closed = outcomes['sl_before'] + outcomes['tp1_sl'] + outcomes['tp1_tp2']
        
        if closed < 5:
            continue  # Мало данных
        
        tp1_sl_pct = (outcomes['tp1_sl'] / closed * 100) if closed > 0 else 0
        ev = _calculate_ev_per_trade(mode_logs, outcomes)
        
        # Scalp: TP1→SL > 40%
        if mode == "scalp" and tp1_sl_pct > 40:
            lines.append(
                f"• <b>Scalp:</b> TP1→SL = {tp1_sl_pct:.0f}% → "
                f"увеличить tp1_offset_atr на +0.05 и снизить trail_atr на −0.05"
            )
    
    # Анализ по триггерам
    by_trigger = defaultdict(list)
    for log in logs:
        trigger = log.get('trigger_type', '')
        by_trigger[trigger].append(log)
    
    for trigger, trigger_logs in by_trigger.items():
        outcomes = _extract_outcomes(trigger_logs)
        closed = outcomes['sl_before'] + outcomes['tp1_sl'] + outcomes['tp1_tp2']
        
        if closed < 5:
            continue
        
        ev = _calculate_ev_per_trade(trigger_logs, outcomes)
        
        # FVR EV<0
        if trigger == "FVR" and ev < 0:
            lines.append(
                f"• <b>FVR:</b> EV = {ev:.2f}R < 0 → "
                f"поднять fvg_min_width_atr до 0.12 и Dist-to-Level +0.1 ATR"
            )
        
        # BR EV<0
        if trigger == "BR" and ev < 0:
            lines.append(
                f"• <b>BR:</b> EV = {ev:.2f}R < 0 → "
                f"требовать Dist-to-next-level ≥ 0.9×ATR"
            )
    
    if len(lines) == 1:
        lines.append("✅ Все метрики в норме, корректировки не требуются")
    
    return "\n".join(lines)


# ========== HELPER FUNCTIONS ==========

def _extract_outcomes(logs: List[Dict]) -> Dict[str, int]:
    """Извлекает и подсчитывает исходы (использует outcome_v2 если есть, иначе fallback)"""
    outcomes = {
        'sl_before': 0,      # SL до TP1
        'tp1_sl': 0,         # TP1 → SL
        'tp1_tp2': 0,        # TP1 + TP2
        'tp1_open': 0,       # TP1 → OPEN
        'open': 0            # Обычные OPEN
    }
    
    for log in logs:
        # Приоритет: outcome_v2 (новый формат) → outcome (старый)
        outcome_v2 = log.get('outcome_v2')
        if outcome_v2:
            status = outcome_v2.get('status', 'OPEN')
        else:
            # Fallback: пытаемся определить по событиям
            status = _determine_path_from_events(log)
        
        if status == 'SL' or status == 'SL_BEFORE_TP1':
            outcomes['sl_before'] += 1
        elif status == 'TP1→SL' or status == 'TP1_TO_SL':
            outcomes['tp1_sl'] += 1
        elif status == 'TP1+TP2' or status == 'TP1_PLUS_TP2':
            outcomes['tp1_tp2'] += 1
        elif status == 'TP1→OPEN' or status == 'TP1_TO_OPEN':
            outcomes['tp1_open'] += 1
        else:  # OPEN, CANCEL, etc.
            outcomes['open'] += 1
    
    return outcomes


def _determine_path_from_events(log: Dict) -> str:
    """Определяет путь из событий hit_tp1_ts/hit_tp2_ts/hit_sl_ts"""
    tp1_ts = log.get('hit_tp1_ts')
    tp2_ts = log.get('hit_tp2_ts')
    sl_ts = log.get('hit_sl_ts')
    
    # Конвертируем в bool (могут быть Decimal 0 или None)
    has_tp1 = bool(tp1_ts)
    has_tp2 = bool(tp2_ts)
    has_sl = bool(sl_ts)
    
    if has_sl and not has_tp1:
        return 'SL'
    elif has_tp1 and has_tp2:
        return 'TP1+TP2'
    elif has_tp1 and has_sl:
        # Проверяем какой раньше
        try:
            if float(sl_ts) < float(tp1_ts):
                return 'SL'
            else:
                return 'TP1→SL'
        except:
            return 'TP1→SL'
    elif has_tp1 and not has_tp2 and not has_sl:
        return 'TP1→OPEN'
    else:
        return 'OPEN'


def _calculate_ev_per_trade(logs: List[Dict], outcomes: Dict[str, int]) -> float:
    """Вычисляет EV/сделку"""
    closed = outcomes['sl_before'] + outcomes['tp1_sl'] + outcomes['tp1_tp2']
    
    if closed == 0:
        return 0.0
    
    # Упрощённый расчёт EV (предполагаем фиксированные RR)
    # SL = -1R, TP1→SL = -0.3R (с учётом частичного close на TP1), TP1+TP2 = +2.5R (avg)
    ev_total = (
        outcomes['sl_before'] * (-1.0) +
        outcomes['tp1_sl'] * (-0.3) +
        outcomes['tp1_tp2'] * 2.5
    )
    
    return ev_total / closed


def _calculate_avg_rr_wins(logs: List[Dict], outcomes: Dict[str, int]) -> float:
    """Вычисляет средний RR побед (используя outcome_v2.r если есть)"""
    wins = []
    
    for log in logs:
        outcome_v2 = log.get('outcome_v2')
        if outcome_v2:
            status = outcome_v2.get('status', '')
            if status in ['TP1+TP2', 'TP1_PLUS_TP2']:
                # Используем 'r' из outcome_v2 если есть
                r_value = outcome_v2.get('r')
                if r_value:
                    wins.append(float(r_value))
        else:
            # Fallback: проверяем по событиям
            path = _determine_path_from_events(log)
            if path in ['TP1+TP2', 'TP1_PLUS_TP2']:
                # Пытаемся рассчитать RR из цен
                try:
                    entry = float(log.get('entry', 0))
                    tp2 = float(log.get('tp2', 0))
                    sl = float(log.get('sl', 0))
                    if entry and tp2 and sl and entry != sl:
                        r = abs((tp2 - entry) / (entry - sl))
                        wins.append(r)
                except:
                    pass
    
    if not wins:
        return 0.0
    
    return statistics.mean(wins)


def _calculate_median_time_to_tp1(logs: List[Dict]) -> int:
    """Вычисляет медиану времени до TP1 (в минутах, используя outcome_v2)"""
    times = []
    
    for log in logs:
        outcome_v2 = log.get('outcome_v2')
        if outcome_v2:
            time_val = outcome_v2.get('time_to_tp1_min')
            if time_val:
                try:
                    times.append(int(time_val))
                except:
                    pass
        else:
            # Fallback: вычисляем из hit_tp1_ts и bar_ts
            try:
                tp1_ts = log.get('hit_tp1_ts')
                bar_ts = log.get('bar_ts')
                if tp1_ts and bar_ts:
                    delta_min = int((float(tp1_ts) - float(bar_ts)) / 1000 / 60)
                    if delta_min > 0:
                        times.append(delta_min)
            except:
                pass
    
    if not times:
        return 0
    
    return int(statistics.median(times))


def _calculate_group_stats(logs: List[Dict]) -> Dict[str, Any]:
    """Вычисляет статистику для группы логов"""
    outcomes = _extract_outcomes(logs)
    closed = outcomes['sl_before'] + outcomes['tp1_sl'] + outcomes['tp1_tp2']
    
    wr_final = (outcomes['tp1_tp2'] / closed * 100) if closed > 0 else 0
    wr_touch = ((outcomes['tp1_tp2'] + outcomes['tp1_sl'] + outcomes['tp1_open']) / (closed + outcomes['tp1_open']) * 100) if (closed + outcomes['tp1_open']) > 0 else 0
    ev = _calculate_ev_per_trade(logs, outcomes)
    
    return {
        'total': len(logs),
        'closed': closed,
        'open': outcomes['open'] + outcomes['tp1_open'],
        'sl_before': outcomes['sl_before'],
        'tp1_sl': outcomes['tp1_sl'],
        'tp1_tp2': outcomes['tp1_tp2'],
        'tp1_open': outcomes['tp1_open'],
        'wr_final': wr_final,
        'wr_touch': wr_touch,
        'ev': ev
    }

