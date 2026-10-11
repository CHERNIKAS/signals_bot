-- =============================================================================
-- СКРИПТ ДЛЯ ИСПРАВЛЕНИЯ ПРОБЛЕМ СО СТАТИСТИКОЙ
-- =============================================================================

-- =============================================
-- 1. ПРОВЕРКА И ИСПРАВЛЕНИЕ BAR_TS
-- =============================================
-- Иногда bar_ts может быть записан в неправильном формате (в секундах вместо миллисекунд)
-- Этот запрос покажет подозрительные записи

SELECT 
    id,
    user_id,
    symbol,
    timeframe,
    bar_ts,
    sent_at,
    EXTRACT(EPOCH FROM bar_ts) as bar_ts_epoch,
    EXTRACT(EPOCH FROM sent_at) as sent_at_epoch,
    CASE 
        WHEN bar_ts > NOW() + INTERVAL '1 day' THEN '⚠️  Future'
        WHEN bar_ts < '2020-01-01' THEN '⚠️  Too old'
        WHEN bar_ts > sent_at THEN '⚠️  Bar after sent'
        ELSE '✅ OK'
    END as status
FROM signals_log
WHERE 
    bar_ts > NOW() + INTERVAL '1 day'
    OR bar_ts < '2020-01-01'
    OR bar_ts > sent_at
ORDER BY bar_ts DESC;

-- =============================================
-- 2. ПЕРЕСЧЕТ СОБЫТИЙ TP1/TP2/SL
-- =============================================
-- Если у сигналов отсутствуют события (hit_tp1_ts, hit_tp2_ts, hit_sl_ts),
-- это может привести к неправильной статистике

-- Показать сигналы без событий (старше 24 часов)
SELECT 
    id,
    user_id,
    symbol,
    timeframe,
    side,
    bar_ts,
    entry,
    tp1,
    tp2,
    sl,
    outcome,
    outcome_v2,
    hit_tp1_ts,
    hit_tp2_ts,
    hit_sl_ts
FROM signals_log
WHERE 
    bar_ts < NOW() - INTERVAL '24 hours'
    AND hit_tp1_ts IS NULL
    AND hit_tp2_ts IS NULL
    AND hit_sl_ts IS NULL
    AND (outcome IS NULL OR outcome = 'OPEN')
ORDER BY bar_ts DESC
LIMIT 50;

-- =============================================
-- 3. ПРОВЕРКА OUTCOME_V2
-- =============================================
-- Показать сигналы с outcome но без outcome_v2
SELECT 
    id,
    user_id,
    symbol,
    timeframe,
    bar_ts,
    outcome,
    outcome_v2,
    hit_tp1_ts IS NOT NULL as has_tp1,
    hit_tp2_ts IS NOT NULL as has_tp2,
    hit_sl_ts IS NOT NULL as has_sl
FROM signals_log
WHERE 
    outcome IS NOT NULL 
    AND outcome != 'OPEN'
    AND outcome_v2 IS NULL
ORDER BY bar_ts DESC
LIMIT 50;

-- =============================================
-- 4. УДАЛЕНИЕ ДУБЛИКАТОВ (ЕСЛИ ЕСТЬ)
-- =============================================
-- ВНИМАНИЕ: Этот запрос удаляет дубликаты!
-- Сначала проверьте что действительно есть дубли

-- Показать дубли (НЕ удаляет, только показывает)
WITH duplicates AS (
    SELECT 
        user_id,
        symbol,
        timeframe,
        bar_ts,
        COUNT(*) as dup_count,
        array_agg(id ORDER BY id) as ids
    FROM signals_log
    GROUP BY user_id, symbol, timeframe, bar_ts
    HAVING COUNT(*) > 1
)
SELECT * FROM duplicates;

-- Удалить дубликаты (оставить только самый старый по ID)
-- РАСКОММЕНТИРУЙТЕ ТОЛЬКО ЕСЛИ УВЕРЕНЫ!
/*
WITH duplicates AS (
    SELECT 
        id,
        ROW_NUMBER() OVER (
            PARTITION BY user_id, symbol, timeframe, bar_ts 
            ORDER BY id
        ) as rn
    FROM signals_log
)
DELETE FROM signals_log
WHERE id IN (
    SELECT id FROM duplicates WHERE rn > 1
);
*/

-- =============================================
-- 5. ОБНОВЛЕНИЕ OUTCOME ДЛЯ СТАРЫХ СИГНАЛОВ
-- =============================================
-- Если сигнал старше 7 дней и всё ещё OPEN, можно закрыть его как CANCELLED

-- Показать старые открытые сигналы
SELECT 
    id,
    user_id,
    symbol,
    timeframe,
    bar_ts,
    NOW() - bar_ts as age,
    outcome
FROM signals_log
WHERE 
    (outcome IS NULL OR outcome = 'OPEN')
    AND bar_ts < NOW() - INTERVAL '7 days'
ORDER BY bar_ts;

-- Обновить outcome для старых сигналов (закрыть как CANCELLED)
-- РАСКОММЕНТИРУЙТЕ ТОЛЬКО ЕСЛИ УВЕРЕНЫ!
/*
UPDATE signals_log
SET 
    outcome = 'CANCELLED',
    outcome_v2 = '{"status": "CANCEL", "r": 0}'::jsonb
WHERE 
    (outcome IS NULL OR outcome = 'OPEN')
    AND bar_ts < NOW() - INTERVAL '7 days';
*/

-- =============================================
-- 6. ПРОВЕРКА ИНДЕКСОВ
-- =============================================
-- Убедитесь что все нужные индексы созданы
SELECT 
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE 
    tablename = 'signals_log'
ORDER BY indexname;

-- Пересоздать уникальный индекс (если нужно)
-- РАСКОММЕНТИРУЙТЕ ТОЛЬКО ЕСЛИ УВЕРЕНЫ!
/*
DROP INDEX IF EXISTS uniq_signal_per_bar;
CREATE UNIQUE INDEX uniq_signal_per_bar 
    ON signals_log(user_id, symbol, timeframe, bar_ts);
*/

-- =============================================
-- 7. VACUUM И ANALYZE
-- =============================================
-- После исправлений рекомендуется обновить статистику таблицы
-- РАСКОММЕНТИРУЙТЕ ТОЛЬКО ЕСЛИ УВЕРЕНЫ!
/*
VACUUM ANALYZE signals_log;
*/

-- =============================================
-- 8. БЫСТРАЯ ПРОВЕРКА ДЛЯ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ
-- =============================================
-- Замените 123456789 на реальный user_id

-- Проверка: сколько сигналов видит функция db_get_signals_with_events
/*
SELECT 
    COUNT(*) as total_signals,
    COUNT(*) FILTER (WHERE bar_ts >= to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') * 1000)/1000.0)) as signals_24h,
    COUNT(*) FILTER (WHERE bar_ts >= to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days') * 1000)/1000.0)) as signals_7d,
    COUNT(*) FILTER (WHERE bar_ts >= to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days') * 1000)/1000.0)) as signals_30d
FROM signals_log
WHERE user_id = 123456789;  -- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ USER_ID
*/

-- Детальный вывод последних сигналов с проверкой временных фильтров
/*
WITH time_filters AS (
    SELECT 
        to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') * 1000)/1000.0) as filter_24h,
        to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days') * 1000)/1000.0) as filter_7d,
        to_timestamp((EXTRACT(EPOCH FROM NOW() - INTERVAL '30 days') * 1000)/1000.0) as filter_30d
)
SELECT 
    s.id,
    s.symbol,
    s.timeframe,
    s.bar_ts,
    s.bar_ts >= tf.filter_24h as in_24h,
    s.bar_ts >= tf.filter_7d as in_7d,
    s.bar_ts >= tf.filter_30d as in_30d,
    s.outcome,
    s.outcome_v2
FROM signals_log s
CROSS JOIN time_filters tf
WHERE s.user_id = 123456789  -- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ USER_ID
ORDER BY s.bar_ts DESC
LIMIT 20;
*/

-- =============================================
-- 9. СРАВНЕНИЕ: ЧТО ВИДИТ БОТ VS ЧТО В БД
-- =============================================
-- Этот запрос эмулирует логику db_get_signals_with_events

-- Для периода 24 часа
/*
WITH filter_params AS (
    SELECT 
        123456789 as uid,  -- ЗАМЕНИТЕ НА РЕАЛЬНЫЙ USER_ID
        EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') * 1000 as since_ms
)
SELECT 
    s.id,
    s.user_id,
    s.symbol,
    s.timeframe,
    EXTRACT(EPOCH FROM s.bar_ts) * 1000 as bar_ts_ms,
    s.side,
    s.entry,
    s.sl,
    s.tp1,
    s.tp2,
    s.reason,
    s.outcome,
    s.outcome_price,
    s.outcome_ts,
    s.outcome_v2,
    s.hit_tp1_price,
    EXTRACT(EPOCH FROM s.hit_tp1_ts) * 1000 as hit_tp1_ts_ms,
    s.hit_tp2_price,
    EXTRACT(EPOCH FROM s.hit_tp2_ts) * 1000 as hit_tp2_ts_ms,
    s.hit_sl_price,
    EXTRACT(EPOCH FROM s.hit_sl_ts) * 1000 as hit_sl_ts_ms,
    -- Проверка условия фильтрации
    s.bar_ts >= to_timestamp(fp.since_ms/1000.0) as passes_filter
FROM signals_log s
CROSS JOIN filter_params fp
WHERE s.user_id = fp.uid
ORDER BY s.bar_ts DESC;
*/

-- =============================================
-- SUMMARY
-- =============================================
SELECT 
    '=' as sep,
    'SUMMARY' as title,
    '=' as sep2;

SELECT 'Total signals' as metric, COUNT(*)::text as value FROM signals_log
UNION ALL
SELECT 'Signals with duplicates', COUNT(*)::text FROM (
    SELECT user_id, symbol, timeframe, bar_ts
    FROM signals_log
    GROUP BY user_id, symbol, timeframe, bar_ts
    HAVING COUNT(*) > 1
) dup
UNION ALL
SELECT 'Signals with future bar_ts', COUNT(*)::text FROM signals_log WHERE bar_ts > NOW() + INTERVAL '1 day'
UNION ALL
SELECT 'Signals with bar_ts > sent_at', COUNT(*)::text FROM signals_log WHERE bar_ts > sent_at
UNION ALL
SELECT 'Old signals without outcome', COUNT(*)::text FROM signals_log 
WHERE bar_ts < NOW() - INTERVAL '7 days' AND (outcome IS NULL OR outcome = 'OPEN')
UNION ALL
SELECT 'Signals without outcome_v2', COUNT(*)::text FROM signals_log 
WHERE outcome IS NOT NULL AND outcome != 'OPEN' AND outcome_v2 IS NULL;

