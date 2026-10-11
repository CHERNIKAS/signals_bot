# Test Cases: OI Flow Module

## Test 1: OI_Flow Calculation

### Scenario A: Бычий flow
**Данные:**
- N = 20 bars
- ΔOI за окно: [+100, +50, +30, -10, +20, ... ]
- Σ(ΔOI_positive) = 500
- Σ(ΔOI_negative) = -100
- OI_Flow = 500 - 100 = 400

**Ожидание:** OI_Flow = +400 (бычий)

---

### Scenario B: Медвежий flow
**Данные:**
- Σ(ΔOI_positive) = 200
- Σ(ΔOI_negative) = -600
- OI_Flow = 200 - 600 = -400

**Ожидание:** OI_Flow = -400 (медвежий)

---

## Test 2: Перцентиль

### Scenario: 65-й перцентиль
**Данные:**
- История H = 100 окон
- OI_Flow values: [-500, -300, 0, +100, +200, ..., +800]
- Текущий OI_Flow = +450
- Сортировка: [..., +400, +450, +500, ...]
- Позиция: 65/100

**Ожидание:** p_flow = 65%

---

## Test 3: Magnitude Gate

### Scenario: Сильный flow
**Данные:**
- OI_Flow = +400
- median(|ΔOI|) = 30
- mag_gate = 400 / 30 = 13.3

**Режим:** Intraday (порог ≥1.0)

**Ожидание:** ✅ PASS (13.3 > 1.0)

---

## Test 4: Short-кейсы (EPS)

### Scenario: Exhaustion Push Short
**Данные:**
- PathScore = +0.90 (сильный рост цены)
- OI_Flow = -50 (OI падает)

**Ожидание:**
- is_short_case = True
- short_case_type = "EPS"
- Для SHORT: ✅ разрешён (несмотря на отрицательный flow)

---

## Test 5: Short-кейсы (LQS)

### Scenario: Liquidity Squeeze
**Данные:**
- PathScore = -0.92 (сильное падение цены)
- OI_Flow = +300 (OI растёт)

**Ожидание:**
- is_short_case = True
- short_case_type = "LQS"
- Для SHORT: ✅ разрешён

---

## Test 6: Аномалии ΔOI

### Scenario: Спайк OI
**Данные:**
- История ΔOI: mean = 20, std = 10
- Последний ΔOI = 80
- Z-score = (80 - 20) / 10 = 6.0

**Порог:** doi_sigma = 5

**Ожидание:** ❌ BAN `OI_ANOMALY` (6.0 > 5.0)

---

## Test 7: Flow Gate для LONG

### Scenario A: Слабый перцентиль
**Данные:**
- Side = LONG
- OI_Flow = +100 (положительный)
- p_flow = 45%
- mag_gate = 1.2

**Режим:** Scalp (порог percentile ≥50%)

**Ожидание:** ❌ BAN `OI_FLOW_WEAK` (45% < 50%)

---

### Scenario B: Слабая magnitude
**Данные:**
- Side = LONG
- OI_Flow = +100
- p_flow = 60%
- mag_gate = 0.7

**Режим:** Scalp (порог mag ≥0.8)

**Ожидание:** ❌ BAN `OI_FLOW_WEAK` (0.7 < 0.8)

---

### Scenario C: Всё ОК
**Данные:**
- Side = LONG
- OI_Flow = +300
- p_flow = 70%
- mag_gate = 1.5

**Режим:** Scalp

**Ожидание:** ✅ PASS

---

## Test 8: Flow Gate для SHORT

### Scenario A: EPS разрешает SHORT
**Данные:**
- Side = SHORT
- OI_Flow = -50 (отрицательный)
- PathScore = +0.90
- is_short_case = True (EPS)

**Ожидание:** ✅ PASS (EPS игнорирует обычные правила)

---

### Scenario B: Обычный SHORT с положительным flow
**Данные:**
- Side = SHORT
- OI_Flow = +100
- is_short_case = False

**Ожидание:** ❌ BAN `OI_FLOW_OPPOSITE` (flow > 0 для SHORT)

---

### Scenario C: Обычный SHORT с правильным flow
**Данные:**
- Side = SHORT
- OI_Flow = -300
- inv_p_flow = 100 - 35 = 65%
- mag_gate = 1.2

**Режим:** Intraday (порог ≥60%)

**Ожидание:** ✅ PASS

---

## Implementation Notes

- История H = max(30, 5×N)
- Перцентили вычисляются по скользящему окну
- Short-кейсы (EPS/LQS) обходят обычные правила для SHORT
- Аномалии (|ΔOI| > 5σ) блокируют обе стороны

