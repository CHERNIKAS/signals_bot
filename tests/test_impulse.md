# Test Cases: Impulse Module

## Test 1: PathScore граничные значения

### Scenario A: Минимальный PathScore
**Данные:**
- N = 12 баров
- Закрытия: линейное движение вверх на 1.5% total
- TrueRange sum: 2.0%
- PathScore = 1.5 / 2.0 = 0.75

**Режим:** Scalp (порог 0.85)

**Ожидание:** ❌ BAN `IMPULSE_LOW_PATHSCORE`

---

### Scenario B: Пороговый PathScore
**Данные:**
- PathScore = 0.850 (ровно порог)

**Режим:** Scalp

**Ожидание:** ✅ PASS (≥ порога)

---

### Scenario C: Сильный PathScore
**Данные:**
- PathScore = 1.20

**Режим:** Intraday (порог 0.80)

**Ожидание:** ✅ PASS

---

## Test 2: Anti-chop

### Scenario A: Высокий chop
**Данные:**
- Бычий импульс: +3%
- Максимальный откат внутри окна: 1.5%
- ATR = 2%
- Anti-chop = 1.5 / 2.0 = 0.75×ATR

**Режим:** Scalp (порог ≤0.60)

**Ожидание:** ❌ BAN `IMPULSE_HIGH_CHOP` (0.75 > 0.60)

---

### Scenario B: Низкий chop
**Данные:**
- Anti-chop = 0.45×ATR

**Режим:** Scalp

**Ожидание:** ✅ PASS

---

## Test 3: Монотоничность

### Scenario A: Низкий body_share
**Данные:**
- N = 15 баров
- Бычьих (close > open): 8
- Медвежьих: 7
- Body share = 8 / 15 = 53.3%

**Режим:** Scalp (порог 60%)

**Ожидание:** ❌ BAN `IMPULSE_LOW_MONOTONICITY`

---

### Scenario B: Низкий body_ratio
**Данные:**
- Сумма тел: 1.2%
- Сумма теней: 0.8%
- Body ratio = 1.2 / 0.8 = 1.5

**Режим:** Scalp (порог 1.7)

**Ожидание:** ❌ BAN `IMPULSE_LOW_MONOTONICITY`

---

### Scenario C: Высокая монотоничность
**Данные:**
- Body share = 73%
- Body ratio = 2.1

**Режим:** Scalp

**Ожидание:** ✅ PASS

---

## Test 4: N-bar window bounds

### Scenario A: Минимальный N
**Данные:**
- TF = 1h
- base_minutes = 180
- N_calc = 180 / 60 = 3
- clamp(3, 3, 36) = 3

**Ожидание:** N = 3

---

### Scenario B: Максимальный N
**Данные:**
- TF = 5m
- N_calc = 180 / 5 = 36
- clamp(36, 3, 36) = 36

**Ожидание:** N = 36

---

### Scenario C: Средний N
**Данные:**
- TF = 15m
- N_calc = 180 / 15 = 12
- clamp(12, 3, 36) = 12

**Ожидание:** N = 12

---

## Test 5: Медвежий импульс

### Scenario: Медвежий PathScore
**Данные:**
- Close_now = 100
- Close_N = 105 (цена упала на 5%)
- TrueRange sum = 4%
- PathScore = (100 - 105) / 4 = -1.25

**Режим:** Swing (порог |PS| ≥ 0.90)

**Ожидание:** ✅ PASS (|-1.25| = 1.25 ≥ 0.90)

---

## Implementation Notes

- Все значения PathScore, anti-chop, mono должны логироваться в `SignalLog`
- При BAN должна записываться точная причина с числами
- N-bar window должен быть в пределах [3, 36]

