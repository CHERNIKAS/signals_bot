# Strategy V2 — Модульная Архитектура

## 🎯 СПРИНТЫ 1-5 ЗАВЕРШЕНЫ (Полная реализация)

### ✅ Созданные файлы:

1. **`config/thresholds.yaml`** — Полная конфигурация всех порогов
   - Режимы (Scalp/Intraday/Swing)
   - ATR квантили
   - Импульс пороги
   - Уровни настройки
   - OI Flow параметры
   - Funding/Basis/BTC правила
   - Триггеры и поправки
   - Риск менеджмент
   - Safety фильтры

2. **`strategy/modes.py`** — Управление режимами
   - `ModeConfig`: датакласс конфигурации режима
   - `ModeManager`: загрузка из YAML, маппинг TF→Mode
   - Расчёт Validity/Hold windows
   - Расчёт SL/TP от структурных уровней

3. **`strategy/triggers.py`** — Триггеры и поправки
   - `TriggerType`: enum (TC/BR/FVR/LSR/EPS/LQS)
   - `TriggerAdjustments`: датакласс поправок
   - `TriggerManager`: детекция типа триггера
   - Поправки к геометрии по типам

4. **`strategy/logs.py`** — Единый лог-объект
   - `SignalLog`: полный лог со всеми метриками (~100 полей)
   - Статусы (PENDING/PASSED/BANNED)
   - Ban Reasons (enum с причинами блокировки)
   - Методы: `add_ban_reason`, `mark_passed`, `to_dict`
   - UI форматирование: `get_ui_summary`, `get_detailed_log`

5. **`strategy/strategy_v2.py`** — Главный пайплайн
   - `StrategyPipeline`: дирижёр всех гейтов
   - `evaluate_signal`: главная функция оценки
   - `_run_pipeline`: последовательный проход гейтов
   - Методы для каждого гейта (пока заглушки)

---

## 📋 РЕАЛИЗОВАННЫЕ СПРИНТЫ:

### ✅ СПРИНТ 2: Уровни (`levels.py` - 550 строк)
- [x] Детекция swings (N-bar pivots) с реакцией ≥0.8×ATR
- [x] Детекция FVG (Fair Value Gaps) с минимальной шириной
- [x] Детекция liquidity pools (equal highs/lows) с epsilon
- [x] HTF уровни (интерфейс готов, требует загрузки HTF данных)
- [x] Кластеризация (cluster_delta_atr)
- [x] Ранжирование (Top-K по weighted score)
- [x] Поиск "встречного" уровня

### ✅ СПРИНТ 3: OI/Funding/Basis/BTC
- [x] `strategy/flow.py` (240 строк)
  - Расчёт OI_Flow (Σpos - Σneg)
  - Перцентили (p_flow за историю H=5×N)
  - Magnitude gate (|OI_Flow| / median)
  - Short-кейсы (EPS/LQS детекция)
  - Аномалии (|ΔOI| > 5σ → BAN)

- [x] `strategy/regimes.py` (220 строк)
  - BTC ветровой анализ (опорные TF, 2/3 голосов)
  - Динамический lock (× волатильность BTC)
  - Направление и consensus

### ✅ СПРИНТ 4: Риск/Лимиты (`risk.py` - 300 строк)
- [x] k_pct адаптивный (k_base × √(TFmin/60) × conf_score)
- [x] Max concurrent (по режимам: 5/3/2)
- [x] Per-symbol caps (2× k_base)
- [x] Cooldowns:
  - between_signals_min (20 мин)
  - after_1sl_min (90/180/480 мин по режимам)
  - after_2sl_hours (12/12/24 ч по режимам)

### ✅ СПРИНТ 5: Импульс (`impulse.py` - 290 строк)
- [x] N-bar window (clamp к n_bounds: 3-36)
- [x] PathScore: |ΔClose_N| / Σ(TrueRange)
- [x] Anti-chop: макс откат в ATR
- [x] Монотоничность:
  - body_share (доля направленных баров)
  - body_ratio (тела / тени)

### ⏳ СПРИНТ 6: Интеграция (СЛЕДУЮЩИЙ)
- [ ] Интегрировать модули в strategy_v2.py (заменить заглушки)
- [ ] Обновить `jobs/jobs.py` для вызова strategy_v2
- [ ] Обновить `handlers/handlers.py` для UI логов (SignalLog)
- [ ] Создать тесты (tests/*.md)
- [ ] A/B testing (14 дней)

---

## 📊 СТАТИСТИКА РЕАЛИЗАЦИИ:

| Файл | Строк | Статус |
|------|-------|--------|
| `config/thresholds.yaml` | 230 | ✅ Полностью |
| `strategy/modes.py` | 200 | ✅ Полностью |
| `strategy/triggers.py` | 160 | ✅ Полностью |
| `strategy/logs.py` | 380 | ✅ Полностью |
| `strategy/levels.py` | 550 | ✅ Полностью |
| `strategy/flow.py` | 240 | ✅ Полностью |
| `strategy/regimes.py` | 220 | ✅ Полностью |
| `strategy/risk.py` | 300 | ✅ Полностью |
| `strategy/impulse.py` | 290 | ✅ Полностью |
| `strategy/strategy_v2.py` | 460 | ⚠️ Заглушки |
| **ИТОГО** | **~3000 строк** | **90% готово** |

---

## 🚀 КАК ИСПОЛЬЗОВАТЬ СЕЙЧАС:

```python
from strategy.strategy_v2 import get_pipeline

# Получить пайплайн
pipeline = get_pipeline()

# Подготовить features
features = {
    "price_now": 65000.0,
    "atr": 500.0,
    "atr_pct": 0.0077,
    "ohlc": df_ohlc,           # DataFrame с OHLC
    "oi_data": oi_dict,        # OI данные
    "funding": 0.0001,         # Funding rate
    "basis": -0.0002,          # Basis
    "taker_buy_share": 0.55,   # Доля покупателей
    "btc_data": btc_dict,      # BTC данные
    "levels": None             # Опционально, иначе рассчитаются автоматически
}

# Оценить сигнал
result = pipeline.evaluate_signal(
    symbol="BTCUSDT",
    timeframe="15m",
    features=features
)

# Результат
if result["signal"]:
    print(f"✅ SIGNAL: {result['signal']}")
    print(result['log'].get_ui_summary())
else:
    print(f"❌ NO SIGNAL: {result['reason']}")
```

---

## 📝 ПРИМЕЧАНИЯ:

1. **Заглушки**: Многие методы в `strategy_v2.py` пока возвращают фиктивные данные
2. **TODO**: Нужно реализовать:
   - `_calculate_impulse` → `impulse.py`
   - `_get_or_calculate_levels` → `levels.py`
   - `_calculate_oi_flow` → `flow.py`
   - `_calculate_btc_wind` → `regimes.py`
   - `_check_risk_limits` → `risk.py`

3. **Конфиг**: Все пороги в `thresholds.yaml` можно менять без изменения кода

4. **Логирование**: `SignalLog` содержит ВСЕ метрики на каждом шаге

---

## 🎯 СЛЕДУЮЩИЕ ШАГИ (Спринт 6 - Интеграция):

### 1. Интеграция модулей в `strategy_v2.py`

Заменить заглушки в методах:

```python
# В strategy_v2.py

def _calculate_impulse(self, ohlc, timeframe, side, mode_name):
    # БЫЛО: return {"pathscore": 0.90, ...}
    # СТАЛО:
    from strategy.impulse import _get_impulse_analyzer
    analyzer = _get_impulse_analyzer()
    metrics = analyzer.calculate_impulse(ohlc, timeframe, atr)
    pass_gate, reason = analyzer.check_impulse_gate(metrics, mode_name)
    return metrics.__dict__

# Аналогично для:
# - _get_or_calculate_levels → levels.py
# - _calculate_oi_flow → flow.py
# - _calculate_btc_wind → regimes.py
# - _check_risk_limits → risk.py
```

### 2. Обновить `jobs/jobs.py`

Заменить вызов `strategy.evaluate_signal` на `strategy_v2.get_pipeline().evaluate_signal`:

```python
# jobs/jobs.py

from strategy.strategy_v2 import get_pipeline

def evaluate_signal(...):
    pipeline = get_pipeline()
    
    features = {
        "price_now": price,
        "atr": atr,
        "atr_pct": atr_pct,
        "ohlc": df_ohlc,
        "oi_data": oi_df,
        "funding": funding_rate,
        "basis": basis,
        "taker_buy_share": taker_share,
        "btc_data": btc_dict  # {timeframe: ohlc_df}
    }
    
    result = pipeline.evaluate_signal(symbol, timeframe, features)
    
    if result["signal"]:
        log = result["log"]
        # Отправить в Telegram с детальными метриками
        send_signal(log.get_ui_summary())
        # Сохранить полный лог в БД
        db_save_signal_log(log.to_dict())
```

### 3. Обновить UI (`handlers/handlers.py`)

Показывать детальные метрики из `SignalLog`:

```python
# handlers/handlers.py

def cmd_signals(update, context):
    logs = db_get_recent_signal_logs(uid, limit=10)
    
    for log in logs:
        text = log.get_ui_summary()  # Краткая сводка
        # Или log.get_detailed_log() для полного отчёта
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
```

### 4. Создать тесты

См. `tests/*.md` для сценариев:
- `test_impulse.md`: граничные случаи PathScore, anti-chop
- `test_levels.md`: кластеризация, ранжирование
- `test_flow.md`: short-кейсы, аномалии
- `test_pipeline.md`: end-to-end сценарии

### 5. A/B Testing (14 дней)

- **Группа A**: Текущая стратегия (`strategy.py`)
- **Группа B**: Новая стратегия (`strategy_v2.py`)

Метрики сравнения:
- TP1 touch rate
- TP1→SL rate (≤ 12%)
- TP1+TP2 rate (≥ 10-15%)
- EV/сделку
- Block-Rate (≥ 20%)

---

**ГОТОВО К ИНТЕГРАЦИИ!** 🚀 **~3000 строк кода, 10 гейтов, адаптивные пороги.**

