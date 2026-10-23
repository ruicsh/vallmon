# VALL drawdown monitor

_As of 2026-10-22 · 23 readings · 2026-09-22 → 2026-10-22_

## State

**Equity** — drawdown -3.63%, unchanged for 23 readings (since 2026-09-22).

No action. Switch to cash at -15% — a further 11.4 pp.

> Window: 23 of 756 readings. The peak is not yet a 756-day peak, so signals are provisional until ~2029-09-18.

## Signal history

_No state changes yet — the switch has been in **equity** since the first reading._

## Drawdown

| Metric | Value |
|--------|-------|
| Peak (trailing 756d) | 3.7925 (2026-10-06) |
| Current drawdown | -3.63% |
| Distance to switch | 11.4 pp |
| History seen | 23 of 756 readings |

_Rule: switch to cash at or below -15% of the trailing peak; return to equity at or above -5%._

## Volatility context

| Metric | Value |
|--------|-------|
| Δσ | 0.89 🟢 normal |
| Current σ (W=20, ann.) | 9.36% |
| Baseline μ (N≤90) | 10.47% |
| Term slope σ10/σ30 | 0.82 (contango) |
| Latest volume | 8,669,701 units |
| Δσ min / max / median | 0.80 / 1.04 / 0.97 |

**Δσ trend** `▇▆▇▇▇▅▅▅▇█▇▇▇▆▅▅▅▅▅▁▁▁▃` 1.01 → 0.89 (range 0.80–1.04, 23 readings)

**Term structure** (σ ann.): 10d 8.65% · 20d 9.36% · 30d 10.53% · 60d —

## Recent readings

| Date | Close (GBP) | Drawdown | State | Signal | Δσ | Regime |
|------|-------------|----------|-------|--------|-----|--------|
| 2026-10-22 | 3.655 | -3.63% | equity | hold | 0.89 | 🟢 normal |
| 2026-10-21 | 3.699 | -2.48% | equity | hold | 0.82 | 🔵 compressing |
| 2026-10-20 | 3.687 | -2.78% | equity | hold | 0.80 | 🔵 compressing |
| 2026-10-19 | 3.704 | -2.33% | equity | hold | 0.82 | 🔵 compressing |
| 2026-10-16 | 3.699 | -2.47% | equity | hold | 0.95 | 🟢 normal |
| 2026-10-15 | 3.719 | -1.94% | equity | hold | 0.95 | 🟢 normal |
| 2026-10-14 | 3.752 | -1.07% | equity | hold | 0.95 | 🟢 normal |
| 2026-10-13 | 3.754 | -1.02% | equity | hold | 0.97 | 🟢 normal |
| 2026-10-12 | 3.774 | -0.49% | equity | hold | 0.97 | 🟢 normal |
| 2026-10-09 | 3.764 | -0.74% | equity | hold | 1.00 | 🟢 normal |
| 2026-10-08 | 3.749 | -1.15% | equity | hold | 1.02 | 🟢 normal |
| 2026-10-07 | 3.765 | -0.71% | equity | hold | 1.02 | 🟢 normal |
| 2026-10-06 | 3.792 | 0.00% | equity | hold | 1.04 | 🟢 normal |
| 2026-10-05 | 3.779 | 0.00% | equity | hold | 1.04 | 🟢 normal |
| 2026-10-02 | 3.754 | -0.42% | equity | hold | 1.03 | 🟢 normal |
| 2026-10-01 | 3.716 | -1.45% | equity | hold | 0.96 | 🟢 normal |
| 2026-09-30 | 3.740 | -0.80% | equity | hold | 0.95 | 🟢 normal |
| 2026-09-29 | 3.740 | -0.78% | equity | hold | 0.95 | 🟢 normal |
| 2026-09-28 | 3.736 | -0.90% | equity | hold | 1.03 | 🟢 normal |
| 2026-09-25 | 3.764 | -0.16% | equity | hold | 1.03 | 🟢 normal |

_Last 20 of 23 readings. Full series: [data/delta_sigma.csv](data/delta_sigma.csv)._

## Methodology

See [README.md → Methodology](README.md#methodology).
