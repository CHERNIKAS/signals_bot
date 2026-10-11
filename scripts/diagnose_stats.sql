-- =============================================================================
-- ДИАГНОСТИКА СТАТИСТИКИ СИГНАЛОВ
-- Скрипт для проверки правильности сохранения и отображения сигналов
-- Запустите этот скрипт в pgAdmin для анализа
-- =============================================================================

-- 1. ОБЩАЯ СТАТИСТИКА ПО ПОЛЬЗОВАТЕЛЯМ
-- =============================================================================
SELECT 
    u.user_id,
    u.username,
    u.created_at,
    COUNT(s.id) as total_signals,
    COUNT(DISTINCT s.symbol) as unique_symbols,
    COUNT(DISTINCT s.timeframe) as unique_timeframes,
    MIN(s.bar_ts) as first_signal,
    MAX(s.bar_ts) as last_signal,
    COUNT(CASE WHEN s.bar_ts >= NOW() - INTERVAL '1 hour' THEN 1 END) as signals_last_hour,
    COUNT(CASE WHEN s.bar_ts >= NOW() - INTERVAL '24 hours' THEN 1 END) as signals_last_24h,
    COUNT(CASE WHEN s.bar_ts >= NOW() - INTERVAL '7 days' THEN 1 END) as signals_last_7d,
    COUNT(CASE WHEN s.bar_ts >= NOW() - INTERVAL '30 days' THEN 1 END) as signals_last_30d
FROM users u
LEFT JOIN signals_log s ON u.user_id = s.user_id
GROUP BY u.user_id, u.username, u.created_at
ORDER BY total_signals DESC;

-- =============================================================================
-- 2. ПРОВЕРКА ВРЕМЕННЫХ МЕТОК (ПОТЕНЦИАЛЬНЫЕ ПРОБЛЕМЫ)
-- =============================================================================
SELECT 
    user_id,
    COUNT(*) as total_signals,
    COUNT(CASE WHEN bar_ts > sent_at THEN 1 END) as bar_after_sent,
    COUNT(CASE WHEN bar_ts < sent_at - INTERVAL '1 hour' THEN 1 END) as bar_too_old,
    COUNT(CASE WHEN bar_ts > NOW() THEN 1 END) as bar_in_future,
    COUNT(CASE WHEN sent_at > NOW() THEN 1 END) as sent_in_future,
    COUNT(CASE WHEN bar_ts IS NULL THEN 1 END) as bar_ts_null,
    COUNT(CASE WHEN sent_at IS NULL THEN 1 END) as sent_at_null
FROM signals_log
GROUP BY user_id
HAVING 
    COUNT(CASE WHEN bar_ts > sent_at THEN 1 END) > 0
    OR COUNT(CASE WHEN bar_ts IN_future THEN 1 END) > 0
    OR COUNT(CASE WHEN sent_at > NOW() THEN 1 END) > 0
ORDER BY user_id;

-- =============================================================================
-- 3. ПРОВЕРКА ДУБЛИКАТОВ (НЕ ДОЛЖНО БЫТЬ ИЗ-ЗА УНИКАЛЬНОГО ИНДЕКСА)
-- =============================================================================
SELECT 
    user_id,
    symbol,
    timeframe,
    bar_ts,
    COUNT(*) as duplicate_count,
    array_agg(id) as signal_ids
FROM signals_log
GROUP BY user_id, symbol, timeframe, bar_ts
HAVING COUNT(*) > 1
ORDER BY duplicate_count DESC, user_id, symbol, timeframe;

-- =============================================================================
-- 4. ПРОВЕРКА OUTCOME_V2 (НОВЫЙ ФОРМАТ СТАТИСТИКИ)
-- =============================================================================
SELECT 
    user_id,
    COUNT(*) as total_signals,
    COUNT(outcome_v2) as with_outcome_v2,
    COUNT(outcome) as with_old_outcome,
    COUNT(CASE WHEN outcome_v2 IS NULL AND outcome IS NOT NULL THEN 1 END) as only_old_outcome,
    COUNT(CASE WHEN outcome_v2 IS NULL AND outcome IS NULL THEN 1 END) as no_outcome,
    ROUND(COUNT(outcome_v2)::numeric / NULLIF(COUNT(*), 0) * 100, 2) as outcome_v2_pct
FROM signals_log
GROUP BY user_id
ORDER BY total_signals DESC;

-- =============================================================================
-- 5. ПОСЛЕДНИЕ СИГНАЛЫ КАЖДОГО ПОЛЬЗОВАТЕЛЯ (ДЛЯ ДЕТАЛЬНОЙ ПРОВЕРКИ)
-- =============================================================================
WITH ranked_signals AS (
    SELECT 
        *,
        ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY bar_ts DESC) as rn
    FROM signals_log
)
SELECT 
    user_id,
    id,
    symbol,
    timeframe,
    side,
    bar_ts,
    sent_at,
    EXTRACT(EPOCH FROM bar_ts) * 1000 as bar_ts_ms,
    EXTRACT(EPOCH FROM sent_at) * 1000 as sent_at_ms,
    entry,
    sl,
    tp1,
    tp2,
    outcome,
    outcome_v2,
    mode,
    trigger_type
FROM ranked_signals
WHERE rn <= 5  -- Последние 5 сигналов каждого пользователя
ORDER BY user_id, bar_ts DESC;

-- =============================================================================
-- 6. РАСПРЕДЕЛЕНИЕ СИГНАЛОВ ПО СИМВОЛАМ И ТАЙМФРЕЙМАМ
-- =============================================================================
SELECT 
    user_id,
    symbol,
    timeframe,
    COUNT(*) as signal_count,
    MIN(bar_ts) as first_signal,
    MAX(bar_ts) as last_signal,
    COUNT(CASE WHEN bar_ts >= NOW() - INTERVAL '24 hours' THEN 1 END) as last_24h,
    COUNT(CASE WHEN bar_ts >= NOW() - INTERVAL '7 days' THEN 1 END) as last_7d
FROM signals_log
GROUP BY user_id, symbol, timeframe
ORDER BY user_id, signal_count DESC;

-- =============================================================================
-- 7. ПРОВЕРКА ПРОПУСКОВ ВО ВРЕМЕНИ (GAPS)
-- =============================================================================
-- Этот запрос покажет промежутки между сигналами больше 2 часов для активных пар
WITH signal_gaps AS (
    SELECT 
        user_id,
        symbol,
        timeframe,
        bar_ts,
        LAG(bar_ts) OVER (PARTITION BY user_id, symbol, timeframe ORDER BY bar_ts) as prev_bar_ts,
        bar_ts - LAG(bar_ts) OVER (PARTITION BY user_id, symbol, timeframe ORDER BY bar_ts) as gap
    FROM signals_log
    WHERE bar_ts >= NOW() - INTERVAL '7 days'
)
SELECT 
    user_id,
    symbol,
    timeframe,
    prev_bar_ts,
    bar_ts,
    gap,
    EXTRACT(EPOCH FROM gap) / 3600 as gap_hours
FROM signal_gaps
WHERE gap > INTERVAL '2 hours'
  AND prev_bar_ts IS NOT NULL
ORDER BY user_id, gap DESC;

-- =============================================================================
-- 8. СРАВНЕНИЕ: СКОЛЬКО СИГНАЛОВ ДОЛЖНО БЫТЬ VS СКОЛЬКО ЕСТЬ
-- =============================================================================
-- Для каждого пользователя: активные пары и ТФ vs фактические сигналы
WITH user_config AS (
    SELECT 
        us.user_id,
        array_agg(DISTINCT us.symbol) FILTER (WHERE us.is_active) as active_symbols,
        COUNT(DISTINCT us.symbol) FILTER (WHERE us.is_active) as symbol_count
    FROM user_symbols us
    GROUP BY us.user_id
),
user_tf_config AS (
    SELECT 
        uts.user_id,
        array_agg(DISTINCT uts.timeframe) FILTER (WHERE uts.is_active) as active_tfs,
        COUNT(DISTINCT uts.timeframe) FILTER (WHERE uts.is_active) as tf_count
    FROM user_tfs_signal uts
    GROUP BY uts.user_id
),
actual_signals AS (
    SELECT 
        user_id,
        COUNT(*) as signal_count_24h,
        COUNT(DISTINCT symbol) as actual_symbols,
        COUNT(DISTINCT timeframe) as actual_tfs,
        array_agg(DISTINCT symbol) as signals_symbols,
        array_agg(DISTINCT timeframe) as signals_tfs
    FROM signals_log
    WHERE bar_ts >= NOW() - INTERVAL '24 hours'
    GROUP BY user_id
)
SELECT 
    u.user_id,
    u.username,
    uc.symbol_count as config_symbols,
    utf.tf_count as config_tfs,
    COALESCE(a.actual_symbols, 0) as actual_symbols_24h,
    COALESCE(a.actual_tfs, 0) as actual_tfs_24h,
    COALESCE(a.signal_count_24h, 0) as signals_24h,
    uc.active_symbols as configured_symbols,
    utf.active_tfs as configured_tfs,
    a.signals_symbols as actual_symbols_list,
    a.signals_tfs as actual_tfs_list,
    -- Проблема: если configured > 0, но actual = 0
    CASE 
        WHEN uc.symbol_count > 0 AND COALESCE(a.signal_count_24h, 0) = 0 THEN '⚠️  NO SIGNALS'
        WHEN COALESCE(a.actual_symbols, 0) < uc.symbol_count THEN '⚠️  FEWER SYMBOLS'
        WHEN COALESCE(a.actual_tfs, 0) < utf.tf_count THEN '⚠️  FEWER TFs'
        ELSE '✅ OK'
    END as status
FROM users u
LEFT JOIN user_config uc ON u.user_id = uc.user_id
LEFT JOIN user_tf_config utf ON u.user_id = utf.user_id
LEFT JOIN actual_signals a ON u.user_id = a.user_id
WHERE uc.symbol_count > 0  -- Только пользователи с настроенными парами
ORDER BY u.user_id;

-- =============================================================================
-- 9. ДЕТАЛЬНАЯ ПРОВЕРКА ДЛЯ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ
-- =============================================================================
-- Замените 123456789 на реальный user_id
/*
SELECT 
    id,
    symbol,
    timeframe,
    side,
    bar_ts,
    sent_at,
    bar_ts AT TIME ZONE 'UTC' as bar_ts_utc,
    sent_at AT TIME ZONE 'UTC' as sent_at_utc,
    EXTRACT(EPOCH FROM bar_ts) * 1000 as bar_ts_ms,
    EXTRACT(EPOCH FROM sent_at) * 1000 as sent_at_ms,
    entry,
    sl,
    tp1,
    tp2,
    reason,
    outcome,
    outcome_v2,
    mode,
    trigger_type,
    hit_tp1_ts,
    hit_tp2_ts,
    hit_sl_ts
FROM signals_log
WHERE user_id = 123456789  -- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ USER_ID
ORDER BY bar_ts DESC
LIMIT 50;
*/

-- =============================================================================
-- 10. ПРОВЕРКА АКТИВНОСТИ БОТА
-- =============================================================================
-- Показывает последнюю активность каждого пользователя
SELECT 
    u.user_id,
    u.username,
    us.is_running,
    (SELECT COUNT(*) FROM user_symbols WHERE user_id = u.user_id AND is_active) as active_pairs,
    (SELECT COUNT(*) FROM user_tfs_signal WHERE user_id = u.user_id AND is_active) as active_tfs,
    (SELECT MAX(bar_ts) FROM signals_log WHERE user_id = u.user_id) as last_signal,
    (SELECT COUNT(*) FROM signals_log WHERE user_id = u.user_id AND bar_ts >= NOW() - INTERVAL '24 hours') as signals_24h,
    CASE 
        WHEN us.is_running AND (SELECT MAX(bar_ts) FROM signals_log WHERE user_id = u.user_id) < NOW() - INTERVAL '2 hours' 
            THEN '⚠️  Running but no recent signals'
        WHEN NOT us.is_running 
            THEN 'Stopped'
        ELSE '✅ Active'
    END as status
FROM users u
LEFT JOIN user_settings us ON u.user_id = us.user_id
ORDER BY last_signal DESC NULLS LAST;

-- =============================================================================
-- ИТОГОВАЯ СВОДКА
-- =============================================================================
SELECT 
    'Total users' as metric,
    COUNT(DISTINCT user_id)::text as value
FROM users
UNION ALL
SELECT 
    'Users with signals',
    COUNT(DISTINCT user_id)::text
FROM signals_log
UNION ALL
SELECT 
    'Total signals',
    COUNT(*)::text
FROM signals_log
UNION ALL
SELECT 
    'Signals last 24h',
    COUNT(*)::text
FROM signals_log
WHERE bar_ts >= NOW() - INTERVAL '24 hours'
UNION ALL
SELECT 
    'Signals last 7d',
    COUNT(*)::text
FROM signals_log
WHERE bar_ts >= NOW() - INTERVAL '7 days'
UNION ALL
SELECT 
    'Signals with outcome_v2',
    COUNT(*)::text
FROM signals_log
WHERE outcome_v2 IS NOT NULL
UNION ALL
SELECT 
    'Duplicate signals (should be 0)',
    COUNT(*)::text
FROM (
    SELECT user_id, symbol, timeframe, bar_ts
    FROM signals_log
    GROUP BY user_id, symbol, timeframe, bar_ts
    HAVING COUNT(*) > 1
) dup;

