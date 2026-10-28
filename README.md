# vallmon - VALL drawdown switch monitor

Tracks **VALL** - the Vanguard FTSE Global All-Cap UCITS ETF USD Acc (London
Stock Exchange: `VALL.L`, ISIN `IE000VAHT5T0`) - and answers one question: in a
drawdown, should spending come from cash or from the fund?

It is a two-threshold switch whose state is recorded alongside every reading.
The Rolling Variance Delta and the volatility term structure are reported as
context: they describe dispersion, not direction, and never change the switch.
[How the numbers work →](docs/methodology.md)

_Updated automatically each trading day by the GitHub Actions workflow._

<!-- REPORT:START -->
_As of 2026-10-28 · 28 readings · 2026-09-21 → 2026-10-28_

## State

**Equity** — drawdown -2.23%, unchanged for 28 readings (since 2026-09-21).

No action. Switch to cash at -15% — a further 12.8 pp.

> Window: 28 of 756 readings. The peak is not yet a 756-day peak, so signals are provisional until ~2029-09-16.

## Signal history

_No state changes yet — the switch has been in **equity** since the first reading._

## Drawdown

| Metric | Value |
|--------|-------|
| Peak (trailing 756d) | 3.7925 (2026-10-06) |
| Current drawdown | -2.23% |
| Distance to switch | 12.8 pp |
| History seen | 28 of 756 readings |

_Rule: switch to cash at or below -15% of the trailing peak; return to equity at or above -5%._

## Volatility context

| Metric | Value |
|--------|-------|
| Δσ | 0.94 <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| Current σ (W=20, ann.) | 9.69% |
| Baseline μ (N≤90) | 10.29% |
| Term slope σ10/σ30 | 1.00 (flat) |
| Latest volume | 7,097,166 units |
| Δσ min / max / median | 0.81 / 1.10 / 0.97 |

**Δσ trend** `█▆▅▆▆▆▄▄▄▆▆▆▆▆▅▅▅▄▄▄▁▁▁▃▂▂▄▄` 1.10 → 0.94 (range 0.81–1.10, 28 readings)

**Term structure** (σ ann.): 10d 10.18% · 20d 9.69% · 30d 10.23% · 60d —

_Volatility labels are sticky: a band must hold for 3 readings before the label changes._

## Recent readings

| Date | Close (GBP) | Day % | Drawdown | State | Signal | Δσ | Volatility |
|------|------------:|------:|---------:|-------|--------|----:|------------|
| 2026-10-28 | 3.708 | +0.15% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -2.23% | equity | hold | 0.94 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-27 | 3.703 | +0.90% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -2.37% | equity | hold | 0.94 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-26 | 3.670 | +0.47% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -3.24% | equity | hold | 0.87 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-23 | 3.652 | -0.07% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -3.69% | equity | hold | 0.87 | <img src="docs/assets/vol-compressing.svg" alt="compressing" width="19" height="12"> |
| 2026-10-22 | 3.655 | -1.18% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -3.63% | equity | hold | 0.90 | <img src="docs/assets/vol-compressing.svg" alt="compressing" width="19" height="12"> |
| 2026-10-21 | 3.699 | +0.31% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -2.48% | equity | hold | 0.83 | <img src="docs/assets/vol-compressing.svg" alt="compressing" width="19" height="12"> |
| 2026-10-20 | 3.687 | -0.46% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -2.78% | equity | hold | 0.81 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-19 | 3.704 | +0.14% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -2.33% | equity | hold | 0.83 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-16 | 3.699 | -0.54% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -2.47% | equity | hold | 0.95 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-15 | 3.719 | -0.88% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -1.94% | equity | hold | 0.95 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-14 | 3.752 | -0.05% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -1.07% | equity | hold | 0.96 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-13 | 3.754 | -0.53% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -1.02% | equity | hold | 0.98 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-12 | 3.774 | +0.25% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -0.49% | equity | hold | 0.98 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-09 | 3.764 | +0.41% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -0.74% | equity | hold | 1.01 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-08 | 3.749 | -0.44% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -1.15% | equity | hold | 1.02 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-07 | 3.765 | -0.71% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -0.71% | equity | hold | 1.02 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-06 | 3.792 | +0.37% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | 0.00% | equity | hold | 1.04 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-05 | 3.779 | +0.65% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | 0.00% | equity | hold | 1.05 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-02 | 3.754 | +1.04% <img src="docs/assets/arrow-up.svg" alt="gain" width="12" height="12"> | -0.42% | equity | hold | 1.03 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |
| 2026-10-01 | 3.716 | -0.66% <img src="docs/assets/arrow-down.svg" alt="loss" width="12" height="12"> | -1.45% | equity | hold | 0.97 | <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12"> |

_Last 20 of 28 readings. Full series: [data/delta_sigma.csv](data/delta_sigma.csv)._

_How these are computed: [docs/methodology.md](docs/methodology.md)._
<!-- REPORT:END -->

## Legend

### Reading columns

What each column in the readings table above means.

| Column | Values | In plain words |
|--------|--------|----------------|
| Date | a trading day | The London session the row describes. Weekends and holidays are absent; a bar taken before the 16:30 close is picked up the next day. |
| Close (GBP) | a price | What one VALL unit cost at that day's London close, in pounds. |
| Day % | `+x.xx%` ▲ / `-x.xx%` ▼ | The move since the previous reading. ▲ means up and ▼ means down; the sign carries the meaning, so the arrow is decoration, not a signal. |
| Drawdown | `0.00%` to `-100%` | How far below its highest close of the past ~3 years the price sits. `0.00%` is a fresh high; `-15%` is where spending switches to cash. |
| State | `equity` / `cash` | Which asset to spend from: `equity` sells from the fund, `cash` spends the cash bucket. |
| Signal | `hold` / `draw from cash` / `refill` | What changed at this reading: `hold` = nothing, `draw from cash` = just switched into cash, `refill` = just switched back to equity. |
| Δσ | a ratio centred on 1 | The last 20 days' volatility divided by its 90-day baseline. `1.00` = as calm as usual; above = bigger daily moves, below = smaller. |
| Volatility | a bar | Δσ as a five-step bar. The key below explains the steps; it describes the size of moves, never their direction. |

### Volatility bars

Δσ against its 90-day baseline. Three bars is normal; more means bigger day-to-day moves.

| Bars                                                                                 | Meaning     | In plain words                                                                                                                    |
| ------------------------------------------------------------------------------------ | ----------- | --------------------------------------------------------------------------------------------------------------------------------- |
| <img src="docs/assets/vol-compressed.svg" alt="compressed" width="19" height="12">   | compressed  | Unusually still. The market is barely twitching day to day. Peaceful — though sometimes the quiet before a storm.                 |
| <img src="docs/assets/vol-compressing.svg" alt="compressing" width="19" height="12"> | compressing | Calming down. Quieter than it was a few weeks ago.                                                                                |
| <img src="docs/assets/vol-normal.svg" alt="normal" width="19" height="12">           | normal      | Ordinary. The usual amount of daily movement.                                                                                     |
| <img src="docs/assets/vol-expanding.svg" alt="expanding" width="19" height="12">     | expanding   | Getting jumpier. Daily swings are growing — something is stirring.                                                                |
| <img src="docs/assets/vol-elevated.svg" alt="elevated" width="19" height="12">       | elevated    | Unusually turbulent. Big daily swings. Usually a stressful period, but also the time you'd want to be buying rather than selling. |

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
python vallmon.py backtest VT     # replay the switch over a long history (read-only)
python vallmon.py report          # regenerate the findings section above
```

### Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && mypy && python -m pytest   # from the repository root
```

### Scheduled (GitHub Actions)

`.github/workflows/vallmon.yml` runs weekdays at 18:00 UTC (after the London
close). It appends the day's reading to `data/delta_sigma.csv`, regenerates the
findings above, and commits both back to the repository. Enable it by pushing
the repo to GitHub; runs can also be triggered manually from the Actions tab.
Pushes and pull requests additionally run a lint/type-check/test job; the
data-update job only runs on the schedule or a manual dispatch.

## Files

| Path                            | Purpose                                                                   |
| ------------------------------- | ------------------------------------------------------------------------- |
| `vallmon.py`                    | fetch -> clean -> compute -> append -> report                             |
| `data/delta_sigma.csv`          | append-only metric log (Δσ, daily volume, switch state, term-structure σ) |
| `README.md`                     | this file; the findings section is auto-updated                           |
| `docs/methodology.md`           | how the metrics are computed and what they mean                           |
| `docs/backtest.md`              | recorded backtests across global, regional and long-history proxies       |
| `tests/test_vallmon.py`         | unit tests for the metric, fetch, persistence and CLI                     |
| `.github/workflows/vallmon.yml` | daily scheduler + lint/type-check/test job                                |
| `requirements.txt`              | runtime dependencies (`tzdata`)                                           |
| `requirements-dev.txt`          | development dependencies (pytest, ruff, mypy)                             |
| `pyproject.toml`                | ruff / mypy / pytest configuration                                        |
