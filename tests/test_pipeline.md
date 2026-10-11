# Test Cases: Full Pipeline

## Test 1: Успешный сигнал (все гейты PASS)

### Scenario: LONG BTCUSDT 15m Scalp
**Данные:**
- Price = 65000
- ATR = 500 (0.77%)
- PathScore = 0.92, anti-chop = 0.45, mono = 68%, ratio = 2.1
- Counter level = 66000 (FVG), distance = 2.0×ATR
- OI_Flow = +800, p_flow = 72%, mag_gate = 1.5
- Funding = 0.0001, basis = -0.0002
- BTC wind: 2/3 UP (1m UP, 5m UP, 15m NEUTRAL)
- Taker buy = 58%

**Пайплайн:**
1. ✅ ATR Gate: 0.77% in [0.30, 0.70] quantile range
2. ✅ Impulse Gate: |PS|=0.92≥0.85, chop=0.45≤0.60, mono=68%≥60%, ratio=2.1≥1.7
3. ✅ Space Gate: 2.0×ATR ≥ 0.70
4. ✅ OI Flow Gate: p_flow=72%≥50%, mag=1.5≥0.8
5. ✅ Funding Gate: |0.0001|≤0.0005
6. ✅ Basis Gate: -0.0002≥-0.0005
7. ✅ BTC Wind Gate: 2/3 UP vs LONG = OK
8. ✅ Taker Gate: 58% not extreme
9. ✅ Safety: OK
10. ✅ Risk: k_pct calculated

**Trigger:** TC (Trend Continuation)

**Geometry:**
- SL = 64350 (FVG на 64500 - 0.15×ATR)
- TP1 = 66000 - 0.10×ATR = 65900
- TP2 = 65000 × (1 + 2.4 × 0.0077) = 66200
- RR1 = (65900-65000)/(65000-64350) = 1.38
- RR2 = (66200-65000)/(65000-64350) = 1.85

**Ожидание:** ✅ SIGNAL PASSED

---

## Test 2: BAN на ATR Gate

### Scenario: Низкая волатильность
**Данные:**
- atr_pct = 0.003 (0.3%)
- Режим: Scalp
- Квантили: [0.30, 0.70] = [0.005, 0.025]

**Ожидание:**
- ❌ BAN `ATR_OUT_OF_RANGE` на шаге 1
- Пайплайн остановлен
- log.status = BANNED
- log.atr_pass = False

---

## Test 3: BAN на Impulse Gate

### Scenario: Высокий chop
**Данные:**
- PathScore = 0.88 ✅
- anti_chop = 0.72 ❌ (> 0.60)

**Режим:** Scalp

**Ожидание:**
- ✅ ATR Gate passed
- ❌ BAN `IMPULSE_HIGH_CHOP` на шаге 2
- log.impulse_pass = False

---

## Test 4: BAN на Space Gate

### Scenario: Слишком близко к уровню
**Данные:**
- Counter level = 65300
- Entry = 65000
- Distance = 300 / 500 (ATR) = 0.6×ATR

**Режим:** Scalp (порог 0.70)

**Ожидание:**
- ✅ Gates 1-2 passed
- ❌ BAN `SPACE_TOO_TIGHT` на шаге 3 (0.6 < 0.70)

---

## Test 5: BAN на OI Flow Gate (аномалия)

### Scenario: ΔOI спайк
**Данные:**
- doi_sigma = 7.2 (|ΔOI| > 5σ)

**Ожидание:**
- ✅ Gates 1-3 passed
- ❌ BAN `OI_ANOMALY` на шаге 4

---

## Test 6: BAN на BTC Wind Gate

### Scenario: BTC против LONG
**Данные:**
- Side = LONG
- BTC wind: 3/3 DOWN (1m DOWN, 5m DOWN, 15m DOWN)
- direction = -1

**Ожидание:**
- ✅ Gates 1-5 passed
- ❌ BAN `BTC_WIND_OPPOSITE` на шаге 7

---

## Test 7: EPS Short-кейс (обход OI Flow)

### Scenario: Exhaustion Push Short
**Данные:**
- Side = SHORT
- PathScore = +0.91 (рост цены)
- OI_Flow = -200 (падает)
- is_short_case = True (EPS)

**Ожидание:**
- ✅ OI Flow Gate PASS (EPS разрешает SHORT с отрицательным flow)
- ✅ Остальные гейты ОК
- ✅ SIGNAL PASSED (trigger = EPS)

---

## Test 8: Multiple sides (LONG fails, SHORT passes)

### Scenario A: LONG против BTC ветра
**Данные:**
- BTC wind: DOWN
- Side = LONG

**Ожидание:**
- ❌ LONG: BAN на BTC Wind Gate

---

### Scenario B: SHORT по ветру
**Данные:**
- BTC wind: DOWN
- Side = SHORT
- Все остальные гейты ОК

**Ожидание:**
- ✅ SHORT: PASS
- Pipeline возвращает SHORT signal

---

## Test 9: Geometry от структурных уровней

### Scenario: SL от swing, TP1 от FVG
**Данные:**
- Entry = 100
- Side = LONG
- Swing на 98.5 (для SL)
- FVG на 102.0 (для TP1)
- ATR = 1.0
- Режим: Intraday (sl_buffer=0.22, sl_min_mult=[1.40,1.55])

**Geometry:**
- SL_structural = 98.5 - 0.22×1.0 = 98.28
- SL_min = 100 × (1 - 1.40 × 0.01) = 98.60
- SL_final = max(98.28, 98.60) = 98.60
- TP1 = 102.0 - 0.15×1.0 = 101.85
- TP2 = 100 × (1 + 3.0 × 0.01) = 103.0

**Ожидание:**
- log.sl_anchor_type = "swing"
- log.tp1_level = 102.0
- RR1 ≈ 1.32
- RR2 ≈ 3.14

---

## Test 10: Trigger adjustments

### Scenario: TC триггер добавляет к TP2
**Данные:**
- Trigger = TC
- Base TP2 mult = 2.4× atr_pct
- TC adjustment: +0.2× atr_pct
- Final TP2 mult = 2.6× atr_pct

**Ожидание:**
- log.trigger_adjustments["tp2_mult_add"] = 0.2
- TP2 = entry × (1 + 2.6 × atr_pct)

---

## Implementation Notes

- Pipeline обязательно должен пробовать обе стороны (LONG/SHORT)
- При BAN любого гейта — немедленный возврат с log.status=BANNED
- При PASS всех гейтов — log.status=PASSED, trigger detection, geometry
- SignalLog содержит ВСЕ метрики на каждом шаге
- `log.get_detailed_log()` показывает пройденные/заблокированные гейты

