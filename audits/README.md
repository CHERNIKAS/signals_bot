# Statistics Audit Module

Система аудита для проверки корректности статистики бота без изменения логики стратегии/резолвера.

## Цель

- Пересчитать Final/Weighted/Touch WR вне бота и сверить с данными бота
- Проверить баланс путей и логику времени (tp1/sl/tp2)
- Выявить сделки, которые искажают winrate (одна свеча, перепутанный путь и т.п.)
- **НОВОЕ:** Верифицировать пути по реальным свечам (OHLC) с учётом буфера и тай-брейков

## Структура

```
audits/
├── __init__.py          # Экспорты модуля
├── audit_stats.py       # Основная логика аудита
├── candle_verifier.py   # Верификация по свечам (OHLC)
└── README.md           # Эта документация

scripts/
└── audit_cli.py        # CLI инструмент для запуска аудита
```

## Использование

### 1. CLI (рекомендуется)

```bash
# Базовое использование (без свечей)
python scripts/audit_cli.py --trades exports/trades_day.csv

# С сохранением аномалий
python scripts/audit_cli.py --trades exports/trades_week.csv --out-prefix audit/week_audit

# ГЛУБОКАЯ ВЕРИФИКАЦИЯ по свечам (рекомендуется!)
python scripts/audit_cli.py --trades exports/trades_7d.csv \
                            --candles exports/candles_m1_7d.csv \
                            --fee-buffer 0.0002 \
                            --tf-sec 60 \
                            --out-prefix audit/audit_7d
```

**Вывод:**
```
=== AUDIT SUMMARY ===
Всего записей: 42
Закрыто: 36 | Открыто: 6
Пути: SLдоTP1=0, TP1→SL=0, TP1+TP2=36, TP1→OPEN=0
WINRATE: Final=100.0%, Weighted=100.0%, Touch=100.0% (36 из 36)

✅ No anomalies detected!
```

### 2. Команда в боте

```
/audit
```

Бот автоматически:
- Загрузит сделки за последние 7 дней
- Пересчитает метрики
- Найдёт аномалии
- Вернёт отчёт

### 3. Программное использование

```python
from audits.audit_stats import audit_from_csv

# Простой аудит
report = audit_from_csv("trades.csv")
print(report)

# С сохранением аномалий
report = audit_from_csv("trades.csv", "audit_output")
# Создаст файлы: audit_output.tp2_without_tp1.csv и т.д.
```

## Типы аномалий

### Стандартные (на основе timestamps)

| Тип | Описание |
|-----|----------|
| `tp2_without_tp1` | Путь TP1+TP2, но нет tp1_time |
| `tp2_but_sl_before_tp1` | Путь TP1+TP2, но SL раньше/одновременно с TP1 |
| `sl_before_tp1_but_tp1_first` | Путь SL до TP1, но tp1_time ≤ sl_time |
| `tp1_open_has_exit_times` | Путь TP1→OPEN, но есть tp2_time/sl_time |
| `same_timestamp_tp_sl` | TP1/TP2 и SL в одно и то же время (тай-брейк) |

### Глубокие (на основе свечей OHLC)

| Тип | Описание |
|-----|----------|
| `tp2_verified_without_tp1` | TP2 достигнут по свечам, но TP1 не подтверждён |
| `winner_candle_disagrees` | Путь показан как TP1+TP2, но свечи показывают SL раньше |
| `open_should_be_closed` | Путь показан как OPEN, но свечи показывают выход (TP2/SL) |

### Красные флаги

- **🚩 RED FLAG: Zero losses** — 0 проигрышей (n1+n3=0) при closed ≥ 20. Требует обязательной верификации по свечам.

## Формат CSV

### Trades CSV (обязательно)

Ожидаемые колонки:
- `id` - ID сигнала
- `symbol` - Символ (BTCUSDT)
- `side` - Сторона (LONG/SHORT)
- `entry` - Цена входа
- `sl` - Стоп-лосс
- `tp1` - Тейк-профит 1
- `tp2` - Тейк-профит 2
- `path` - Путь исхода (SL_BEFORE_TP1, TP1_PLUS_TP2, TP1_TO_SL, TP1_TO_OPEN, OPEN)
- `tp1_time` - Время достижения TP1 (ISO format)
- `tp2_time` - Время достижения TP2 (ISO format)
- `sl_time` - Время достижения SL (ISO format)
- `pnl_pct` - Процент результата
- `time_open` / `open_time` - Время входа в сделку (опционально)

### Candles CSV (для глубокой верификации)

Ожидаемые колонки:
- `symbol` - Символ (BTCUSDT)
- `open_time` - Время открытия свечи (ISO format или timestamp)
- `open` - Цена открытия
- `high` - Максимум
- `low` - Минимум
- `close` - Цена закрытия

**Рекомендации:**
- Используйте M1 (1-минутные) свечи для максимальной точности
- Период свечей должен покрывать все сделки из trades CSV
- Включите все символы, присутствующие в trades CSV

## Критерии приёмки

✅ **Скрипт запускается без ошибок** на последних выгрузках

✅ **Пересчитанные Final/Weighted/Touch совпадают с ботом** (±0.1 п.п.)

✅ **При наличии аномалий создаются CSV** с ≥1 строкой

✅ **Sanity-предупреждение** печатается, если final ≥95% при open>0 и closed≥20

## Примеры

### Пример 1: Чистые данные

```bash
$ python scripts/audit_cli.py --trades exports/trades_day.csv

=== AUDIT SUMMARY ===
Всего записей: 42
Закрыто: 36 | Открыто: 6
Пути: SLдоTP1=10, TP1→SL=5, TP1+TP2=21, TP1→OPEN=0
WINRATE: Final=58.3%, Weighted=65.3%, Touch=72.2% (26 из 36)

✅ No anomalies detected!
```

### Пример 2: Найдены аномалии

```bash
$ python scripts/audit_cli.py --trades exports/trades_week.csv --out-prefix audit/week

=== AUDIT SUMMARY ===
Всего записей: 150
Закрыто: 142 | Открыто: 8
Пути: SLдоTP1=30, TP1→SL=15, TP1+TP2=97, TP1→OPEN=2
WINRATE: Final=68.3%, Weighted=73.6%, Touch=80.6% (114 из 144)

⚠️ Anomalies detected! Check these files:
  - audit/week.same_timestamp_tp_sl.csv
```

### Пример 3: Sanity warning

```bash
$ python scripts/audit_cli.py --trades exports/suspicious.csv

=== AUDIT SUMMARY ===
Всего записей: 50
Закрыто: 45 | Открыто: 5
Пути: SLдоTP1=1, TP1→SL=0, TP1+TP2=44, TP1→OPEN=0
WINRATE: Final=97.8%, Weighted=97.8%, Touch=97.8% (44 из 45)
⚠️ SANITY: финальный винрейт ≥95% при наличии открытых — проверь тай-брейк/буфер.

✅ No anomalies detected!
```

## Интеграция с CI/CD

Можно добавить в GitHub Actions или Railway cron:

```yaml
# .github/workflows/audit.yml
name: Weekly Stats Audit
on:
  schedule:
    - cron: '0 0 * * 0'  # Каждое воскресенье
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Setup Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.12'
      - name: Install deps
        run: pip install pandas
      - name: Run audit
        run: python scripts/audit_cli.py --trades exports/trades_week.csv
```

## Troubleshooting

**Q: `FileNotFoundError: trades.csv`**  
A: Убедись, что путь к файлу корректен. Используй относительный или абсолютный путь.

**Q: `ValueError: CSV must contain 'path' column`**  
A: CSV должен содержать колонку `path`. Используй экспорт "Сделки (CSV)" из бота.

**Q: Метрики не совпадают с ботом**  
A: Проверь, что:
- Используешь тот же период (день/неделя/месяц)
- Запустил `/recalc_stats` перед экспортом
- Пути нормализуются корректно (регистронезависимо)

**Q: Много аномалий `same_timestamp_tp_sl`**  
A: Это нормально для минутных/5-минутных таймфреймов. Бот использует консервативную логику (SL имеет приоритет при одинаковом времени).

