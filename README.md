# vallmon - VALL Rolling Variance Delta (delta-sigma) monitor

Monitors volatility expansion and compression in **VALL** - the Vanguard FTSE
Global All-Cap UCITS ETF USD Acc (London Stock Exchange: `VALL.L`, ISIN
`IE000VAHT5T0`) - using the Rolling Variance Delta metric, alongside a
short-vs-long volatility term structure (σ10/σ20/σ30/σ60).

## Current reading

_Updated automatically each trading day by the GitHub Actions workflow._

<!-- REPORT:START -->
**Δσ = 1.68** 🔴 elevated · 5.81% vol vs 3.45% baseline · 871,615 units · 2026-10-19 · TS σ10/σ30 0.49

`▁▁▁▃▆▆▅▃▂▄▄▄▄▄▄▃▄▄▄▄▄▄▄▄▄▄▄▃▃█` · [Full report →](REPORT.md)
<!-- REPORT:END -->

## Methodology

The Rolling Variance Delta measures how far current realized volatility has
expanded or compressed relative to its immediate historical baseline:

```
delta_sigma = sigma_current / mu_sigma(t - N)
```

1. **Daily log returns:** `r_t = ln(P_t / P_{t-1})`
2. **Current volatility:** `sigma_t = stdev(r_{t-W+1 .. t}) * sqrt(252)` -
   annualized realized volatility over `W = 20` trading days (sample standard
   deviation, i.e. the `n-1` denominator).
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

### Volatility term structure

The same realized-vol calculation is also evaluated over several windows -
`W ∈ {10, 20, 30, 60}` trading days - to show the *shape* of the volatility
curve. `W = 20` remains the primary window for Δσ; the other legs are
supplementary.

The headline term-structure signal is the **slope**:

```
slope = sigma_10 / sigma_30
```

- **> 1.05 -> backwardated:** short-term vol sits above longer-term vol, the
  classic stress shape (short-dated uncertainty priced above the trend).
- **0.95 - 1.05 -> flat:** the curve is broadly level.
- **< 0.95 -> contango:** short-term vol is below longer-term vol, the calm
  shape (recent markets quieter than the trailing window).

A window only produces a value once that many trading days of returns exist, so
the longer legs (30d, 60d) appear as an em dash until enough history accrues -
the same adaptive behavior as the baseline.

### Data & caveats

- **Source:** Yahoo Finance `VALL.L` daily closes (GBP). Rows with a null close
  or zero volume (new-listing placeholders) are dropped.
- **Volume:** each row also records that day's traded volume in ETF units
  (Yahoo's `volume` field) as context for the reading. It is *not* part of the
  Δσ metric, and it is a unit count, not a cash amount - turnover is roughly
  `volume × close`. Rows written before the column existed are backfilled from
  source the next time `run`/`rebuild` executes.
- **Currency:** `VALL.L` trades in GBP, so delta_sigma includes some GBP/USD FX
  noise on top of pure equity volatility.
- **Short history:** VALL's share class incepted on 18 Aug 2026, so early
  readings use a reduced baseline until ~90 trading days accumulate (~Feb 2027);
  the 60-day term-structure leg stays empty until ~mid-November 2026.
- **Reproducibility:** each row is written once and never silently rewritten. If
  Yahoo later revises a historical close, `run` prints a drift warning (checked
  against the primary σ, baseline, Δσ and every term-structure leg) and
  `python vallmon.py rebuild` recomputes the whole series from source. The fetch
  requests the full available history (`range=max`), so `rebuild` can never
  truncate readings that have aged past a fixed lookback window.
- **Partial bars:** `run`/`rebuild` executed during London trading hours capture
  an incomplete current-day bar; the scheduled 18:00 UTC run avoids this.

## Setup

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python vallmon.py run       # fetch + append + print the latest reading
python vallmon.py rebuild   # recompute the whole series from source (overwrites CSV)
python vallmon.py history   # recent readings with a trend sparkline
python vallmon.py report    # regenerate the "Current reading" section above
```

### Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && mypy && python -m pytest   # from the repository root
```

### Scheduled (GitHub Actions)

`.github/workflows/vallmon.yml` runs weekdays at 18:00 UTC (after the London
close). It appends the day's reading to `data/delta_sigma.csv`, regenerates this
report, and commits both back to the repository. Enable it by pushing the repo
to GitHub; runs can also be triggered manually from the Actions tab. Pushes and
pull requests additionally run a lint/type-check/test job; the data-update job
only runs on the schedule or a manual dispatch.

## Files

| Path | Purpose |
|------|---------|
| `vallmon.py` | fetch -> clean -> compute -> append -> report |
| `data/delta_sigma.csv` | append-only metric log (Δσ, daily volume, term-structure σ) |
| `README.md` | this file; the report section is auto-updated |
| `REPORT.md` | full auto-generated report (summary + full history) |
| `tests/test_vallmon.py` | unit tests for the metric, fetch, persistence and CLI |
| `.github/workflows/vallmon.yml` | daily scheduler + lint/type-check/test job |
| `requirements.txt` | runtime dependencies (`tzdata`) |
| `requirements-dev.txt` | development dependencies (pytest, ruff, mypy) |
| `pyproject.toml` | ruff / mypy / pytest configuration |
