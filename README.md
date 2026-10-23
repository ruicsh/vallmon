# vallmon - VALL Rolling Variance Delta (delta-sigma) monitor

Monitors volatility expansion and compression in **VALL** - the Vanguard FTSE
Global All-Cap UCITS ETF USD Acc (London Stock Exchange: `VALL.L`, ISIN
`IE000VAHT5T0`) - using the Rolling Variance Delta metric, alongside a
short-vs-long volatility term structure (σ10/σ20/σ30/σ60).

## Current reading

_Updated automatically each trading day by the GitHub Actions workflow._

<!-- REPORT:START -->
**Equity** · -3.63% from peak · **Δσ = 0.89** 🟢 normal · 9.36% vol vs 10.47% baseline · 8,669,701 units · 2026-10-22 · TS σ10/σ30 0.82

`▇▆▇▇▇▅▅▅▇█▇▇▇▆▅▅▅▅▅▁▁▁▃` · [Full report →](REPORT.md)
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

### Drawdown switch

`delta_sigma` measures dispersion, not direction, so it cannot tell you whether
to sell. The drawdown rule answers a narrower question that *is* answerable -
which asset to sell from:

```
drawdown = close / peak - 1        peak = max close over the trailing 756d
```

The switch is a two-boundary state machine rather than a single threshold. The
state carries forward from the previous reading:

| State | Entered when | Signal |
|----------------------|------------------------------|--------------------|
| **cash** | drawdown at or below -15% | **draw from cash** |
| **equity** | drawdown at or above -5% | **refill** |
| unchanged | anything in between | **hold** |

The band exists because a single threshold is crossed roughly three times a
year on long history, which is not a state a spending decision can follow; the
15% -> 5% gap is the hysteresis that keeps it stable. A new high (drawdown 0)
is inside the re-arm band, so it also returns the switch to equity.

Unlike delta-sigma this has no baseline, no fitted parameters and no forecast:
it is an observable state of the price series, so it cannot drift the way a
rolling statistic can. `python vallmon.py drawdown` prints the recent series
and the current state; `--annual-spend` additionally translates the current
cash episode into the number of months and amount of spending it covers.

The peak window is ~3 years, so until that much history exists the rule is
running on a partial window - the report states how many readings it has seen.
The window and both thresholds are **provisional defaults** (three years, 15%,
5%), not backtested parameters; treat a signal as information rather than a
validated trigger.

### Data & caveats

- **Source:** Yahoo Finance `VALL.L` daily closes (GBP). Rows with a null close
  or zero volume (new-listing placeholders) are dropped, and several bars for
  one session are collapsed to the last close with summed volume. That collapse
  is a backstop: Yahoo serves `range=max` at *hourly* granularity for this
  ticker (observed 2026-10), which would silently turn the daily returns series
  into an intraday one. The fetch therefore checks the declared granularity and
  retries with a bounded range (`range=20y`) that it does serve daily.
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
  against the primary σ, baseline, Δσ and every term-structure leg); `--strict`
  makes that warning fatal so CI cannot commit a drifted series, and
  `python vallmon.py rebuild` recomputes the whole series from source.
  `tests/test_vallmon.py` additionally re-derives every committed reading from
  its own `close` column, so a series built from a different price source fails
  the test suite rather than quietly producing readings.
- **Partial bars:** a bar captured before the 16:30 London close is incomplete,
  so `fetch_prices` drops the current session and the next run picks it up. The
  scheduled 18:00 UTC run is safely after the close. `rebuild` is consequently
  time-of-day dependent: running it at 14:00 and at 18:00 on the same day
  produces different CSVs, by design.

## Setup

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python vallmon.py run             # fetch + append + print the latest reading
python vallmon.py run --strict    # ...but refuse to append if stored rows drifted
python vallmon.py rebuild         # recompute the whole series from source (overwrites CSV)
python vallmon.py history         # recent readings with a trend sparkline
python vallmon.py drawdown        # drawdown from the trailing peak + switch state
python vallmon.py report          # regenerate the "Current reading" section above
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
