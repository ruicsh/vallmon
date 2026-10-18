# vallmon - VALL Rolling Variance Delta (delta-sigma) monitor

Monitors volatility expansion and compression in **VALL** - the Vanguard FTSE
Global All-Cap UCITS ETF USD Acc (London Stock Exchange: `VALL.L`, ISIN
`IE000VAHT5T0`) - using the Rolling Variance Delta metric.

## Current reading

_Updated automatically each trading day by the GitHub Actions workflow._

<!-- REPORT:START -->
**Latest:** delta_sigma = **0.95** -> 🟢 normal  *(2026-10-16)*

10.19% current vol (W=20) vs 10.77% trailing baseline (N<=90).

| Date | Close (GBp) | sigma current (ann.) | Baseline mu | delta_sigma | Regime |
|------|-------------|----------------------|-------------|-------------|--------|
| 2026-10-16 | 3.699 | 10.19% | 10.77% | 0.95 | 🟢 normal |
| 2026-10-15 | 3.719 | 10.26% | 10.79% | 0.95 | 🟢 normal |
| 2026-10-14 | 3.752 | 10.32% | 10.82% | 0.95 | 🟢 normal |
| 2026-10-13 | 3.754 | 10.56% | 10.84% | 0.97 | 🟢 normal |
| 2026-10-12 | 3.774 | 10.56% | 10.86% | 0.97 | 🟢 normal |
| 2026-10-09 | 3.764 | 10.87% | 10.86% | 1.00 | 🟢 normal |
| 2026-10-08 | 3.749 | 11.05% | 10.84% | 1.02 | 🟢 normal |
| 2026-10-07 | 3.765 | 11.03% | 10.82% | 1.02 | 🟢 normal |
| 2026-10-06 | 3.792 | 11.19% | 10.79% | 1.04 | 🟢 normal |
| 2026-10-05 | 3.779 | 11.20% | 10.75% | 1.04 | 🟢 normal |
| 2026-10-02 | 3.754 | 11.01% | 10.72% | 1.03 | 🟢 normal |
| 2026-10-01 | 3.716 | 10.37% | 10.77% | 0.96 | 🟢 normal |
| 2026-09-30 | 3.740 | 10.31% | 10.83% | 0.95 | 🟢 normal |
| 2026-09-29 | 3.740 | 10.36% | 10.91% | 0.95 | 🟢 normal |
| 2026-09-28 | 3.736 | 11.21% | 10.85% | 1.03 | 🟢 normal |
| 2026-09-25 | 3.764 | 11.09% | 10.79% | 1.03 | 🟢 normal |
| 2026-09-24 | 3.745 | 10.99% | 10.72% | 1.03 | 🟢 normal |
| 2026-09-23 | 3.768 | 10.70% | 10.73% | 1.00 | 🟢 normal |
| 2026-09-22 | 3.770 | 10.79% | 10.67% | 1.01 | 🟢 normal |
<!-- REPORT:END -->

## Methodology

The Rolling Variance Delta measures how far current realized volatility has
expanded or compressed relative to its immediate historical baseline:

```
delta_sigma = sigma_current / mu_sigma(t - N)
```

1. **Daily log returns:** `r_t = ln(P_t / P_{t-1})`
2. **Current volatility:** `sigma_t = stdev(r_{t-W+1 .. t}) * sqrt(252)` -
   annualized realized volatility over `W = 20` trading days.
3. **Baseline:** `mu = mean(sigma_{t-N .. t-1})` - the trailing mean of the
   volatility profile over `N = min(90, available)` days (adaptive; grows into a
   full 90-day baseline as history accrues).
4. **Reading:** `delta_sigma = sigma_t / mu` - a value above 1 means volatility
   is expanding, below 1 means it is compressing.

### Regime labels

| delta_sigma | Label |
|-------------|-------|
| >= 1.5 | elevated |
| 1.15 - 1.5 | expanding |
| 0.85 - 1.15 | normal |
| 0.7 - 0.85 | compressing |
| < 0.7 | compressed |

### Data & caveats

- **Source:** Yahoo Finance `VALL.L` daily closes (GBP). Rows with a null close
  or zero volume (new-listing placeholders) are dropped.
- **Currency:** `VALL.L` trades in GBP, so delta_sigma includes some GBP/USD FX
  noise on top of pure equity volatility.
- **Short history:** VALL's share class incepted on 18 Aug 2026, so early
  readings use a reduced baseline until ~90 trading days accumulate (~Feb 2027).

## Setup

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python volmon.py run       # fetch + append + print the latest reading
python volmon.py history   # recent readings with a trend sparkline
python volmon.py report    # regenerate the "Current reading" section above
```

### Scheduled (GitHub Actions)

`.github/workflows/volmon.yml` runs weekdays at 18:00 UTC (after the London
close). It appends the day's reading to `data/delta_sigma.csv`, regenerates this
report, and commits both back to the repository. Enable it by pushing the repo
to GitHub; runs can also be triggered manually from the Actions tab.

## Files

| Path | Purpose |
|------|---------|
| `volmon.py` | fetch -> clean -> compute -> append -> report |
| `data/delta_sigma.csv` | append-only metric log |
| `README.md` | this file; the report section is auto-updated |
| `.github/workflows/volmon.yml` | daily scheduler |
| `requirements.txt` | runtime dependencies (`tzdata`) |
