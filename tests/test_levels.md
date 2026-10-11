# Test Cases: Levels Module

## Test 1: Swing Detection

### Scenario A: Swing High с сильной реакцией
**Данные:**
- k = 3 (для 15m)
- high[i] = 105, high[i-1...i-3] = [102, 103, 104], high[i+1...i+3] = [103, 102, 101]
- Падение после пика: 105 → 101 = 4
- ATR = 2
- Reaction = 4 / 2 = 2.0×ATR

**Ожидание:** ✅ Детектирован swing high на 105 (reaction ≥ 0.8×ATR)

---

### Scenario B: Swing Low со слабой реакцией
**Данные:**
- low[i] = 95, окружение выше
- Рост после дна: 95 → 96 = 1
- ATR = 2
- Reaction = 1 / 2 = 0.5×ATR

**Ожидание:** ❌ НЕ детектирован (reaction < 0.8×ATR)

---

## Test 2: FVG Detection

### Scenario A: Bullish FVG выше минимума
**Данные:**
- low[i] = 103, high[i-2] = 100
- Gap width = 3
- ATR = 20
- fvg_min_width = 0.10 × 20 = 2

**Ожидание:** ✅ Детектирован FVG на mid_price = 101.5

---

### Scenario B: FVG ниже минимума
**Данные:**
- Gap width = 1.5
- fvg_min_width = 2

**Ожидание:** ❌ НЕ детектирован (width < min)

---

## Test 3: Liquidity Pools (Equal Highs)

### Scenario A: Три равных максимума
**Данные:**
- Пики: [105.0, 105.2, 104.9]
- ATR = 10
- epsilon = max(0.05 × 10, 0.5 × 0.001) = 0.5

**Ожидание:** ✅ Кластер из 3 уровней → 1 liquidity level на avg_price = 105.03

---

### Scenario B: Два далёких пика
**Данные:**
- Пики: [105.0, 106.0]
- epsilon = 0.5
- Расстояние = 1.0 > 0.5

**Ожидание:** ❌ Два отдельных уровня (не кластер)

---

## Test 4: Кластеризация

### Scenario A: Близкие уровни
**Данные:**
- Уровни: [100.0 (swing), 100.3 (FVG), 100.2 (liquidity)]
- ATR = 2
- cluster_delta = 0.25 × 2 = 0.5

**Ожидание:** ✅ Объединены в 1 кластер:
- avg_price = 100.17
- type = FVG (приоритет)
- cluster_size = 3

---

### Scenario B: Далёкие уровни
**Данные:**
- Уровни: [100.0, 101.0]
- cluster_delta = 0.5
- Расстояние = 1.0 > 0.5

**Ожидание:** ❌ Остаются 2 отдельных уровня

---

## Test 5: Ранжирование

### Scenario: Scoring разных типов
**Данные:**
- Level A: HTF swing на 105.0, давность 1h, reaction 1.5×ATR
- Level B: FVG на 105.5, давность 10m, reaction 1.0×ATR
- Level C: Liquidity на 104.0, давность 30m, reaction 2.0×ATR, cluster_size=3

**Ожидание (score calculation):**
- Level A: recency(0.96) × 0.2 + reaction(0.75) × 0.25 + type(1.0) × 0.2 + htf(1.0) × 0.15 + cluster(0.2) × 0.1 + distance(~0.5) × 0.1 ≈ 0.78
- Level B: recency(0.99) × 0.2 + reaction(0.5) × 0.25 + type(0.7) × 0.2 + htf(0.5) × 0.15 + cluster(0.2) × 0.1 + distance(~0.8) × 0.1 ≈ 0.63
- Level C: recency(0.98) × 0.2 + reaction(1.0) × 0.25 + type(0.85) × 0.2 + htf(0.5) × 0.15 + cluster(0.6) × 0.1 + distance(~0.6) × 0.1 ≈ 0.76

**Top-K=3 Ranking:** A > C > B

---

## Test 6: Counter Level Search

### Scenario A: LONG ищет встречный уровень
**Данные:**
- Entry = 100
- Levels: [98 (swing), 105 (FVG), 110 (liquidity)]
- Side = LONG

**Ожидание:** ✅ Counter level = 105 (FVG) — ближайший выше entry

---

### Scenario B: Нет встречных уровней
**Данные:**
- Entry = 110
- Levels: [98, 105]
- Side = LONG

**Ожидание:** ❌ None (все уровни ниже)

---

## Test 7: HTF Zones

### Scenario: HTF swing с зоной
**Данные:**
- HTF level = 105 (from 1h)
- ATR(1h) = 5
- Zone = 105 ± (0.2-0.3) × 5 = [104.0, 106.5]

**Ожидание:** ✅ HTF уровень 105 с зоной [104.0, 106.5]

---

## Implementation Notes

- Уровни должны иметь score для ранжирования
- Top-K по умолчанию = 12
- HTF уровни имеют приоритет при объединении кластеров
- Reaction strength должна быть ≥ 0.8×ATR для swings

