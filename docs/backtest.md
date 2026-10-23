# Backtests

`python vallmon.py backtest <symbol> --years <n>` replays the drawdown switch
over any Yahoo close series. It is read-only: nothing is written to the ticker's
CSV. This page records the runs used to sanity-check the rule, not a promise
about future returns.

## Method

The replayed rule is exactly the live one:

- peak = highest close over the trailing 756 trading days (~3 years);
- switch to cash at or below **-15%** of that peak;
- return to equity at or above **-5%**;
- the state carries forward in between (the 15% -> 5% gap is the hysteresis).

Each run fetches daily closes for the requested window, drops today's bar, and
reports the series' max drawdown, the share of readings spent in cash, the switch
count, and every cash episode.

The thresholds are provisional defaults, not fitted parameters. These runs
illustrate how the rule behaves; they do not select the thresholds. `--sweep`
replays one series across nearby enter/exit pairs (reporting the worst episode
length) if you want to see how sensitive the result is.

## What it does not model

- **Total return.** Yahoo's `close` is split-adjusted but not dividend-adjusted.
  For distributing funds (VWRL.L, VUSA.L, ISF.L, VT, SPY) dividends are missing,
  so drawdowns are slightly overstated and recoveries slightly understated.
  Accumulating share classes (VWRP.L, VWCE.DE, IWDA.L) have no distribution to
  lose, so their close is close to the total-return series.
- **Currency.** Each series is in its own listing currency (GBP for the `.L`
  lines, EUR for VWCE.DE, USD for the US lines). No FX conversion is applied;
  VALL.L itself is GBP.
- **Spending and refills.** The backtest reports *which asset the switch would
  use*, not a portfolio simulation. It does not model selling, refilling the
  cash bucket, taxes, or dealing costs.
- **Survivorship.** Only series still available on Yahoo are tested; funds that
  closed are absent.
- **The rolling peak.** The peak is a rolling ~3-year high, so an old high
  eventually rolls out of the window. In a long decline that both re-arms the
  switch earlier and keeps it in cash while the price sits below a rolling
  peak - see the 2000-04 Nasdaq episode.
- **Window length.** The requested window changes the answer. VT fetched over
  18 years (starting in late 2008, after the pre-crash peak) shows -34.5% max
  drawdown and a 1.72 year worst episode; over 20 years, which includes the
  whole 2008 decline, it shows -50.6% and 2.13 years. Compare runs only over the
  same window.
- **Partial peak window.** Every run uses a rolling 756-close peak, so an episode
  that begins in the first 756 bars of a series is measured against a peak that
  is still forming. That understates the peak, which delays the -15% entry and
  makes the entry drawdown look shallower than the rule would have produced with
  a full window. This is not limited to the young 2019 funds: it also covers the
  GFC entries for VT, SPY, QQQ, VWO and ISF.L, the 2011 episodes for IWDA.L,
  VWCE.DE's 2022 episode, the 1992-94 episodes for ^FTSE, and the 1992 episode
  for ^IXIC. The 35-year indices are the only runs where the 2000-02 and 2008
  episodes are measured on a full window. Affected runs and episodes are marked
  † below.
- **Short series.** VWRP.L and VWCE.DE only start in 2019, so they contain no GFC
  or dot-com episode at all, on top of the partial-window effect above.

## Symbols

Chosen to cover VALL's mandate and the stress cases, not to be exhaustive.

| Symbol | Series | Why it is here |
|--------|--------|----------------|
| VWRP.L | Vanguard FTSE All-World UCITS Acc (GBP) | Closest tradable proxy: same index family, GBP, accumulating. |
| VWRL.L | Vanguard FTSE All-World UCITS Dist (GBP) | Same index with a longer GBP history; distributing. |
| VWCE.DE | Vanguard FTSE All-World UCITS Acc (EUR) | Same index in EUR: a currency stress case. |
| IWDA.L | iShares MSCI World UCITS Acc (GBP) | Developed-world, GBP, accumulating, longer history than VWRP. |
| VUSA.L | Vanguard S&P 500 UCITS Dist (GBP) | US large-cap in GBP. |
| ISF.L | iShares Core FTSE 100 (GBP) | UK large-cap; dividend-heavy and a long sideways market. |
| VT | Vanguard Total World Stock ETF (USD) | Long global history (2008 onwards), USD. |
| SPY | SPDR S&P 500 ETF (USD) | Long US large-cap history. |
| QQQ | Invesco QQQ (USD) | High-volatility US growth. |
| VWO | Vanguard FTSE Emerging Markets ETF (USD) | Emerging-market drawdowns. |
| ^GSPC | S&P 500 index | 35-year price series. |
| ^FTSE | FTSE 100 index | 35-year price series; sideways secular market. |
| ^IXIC | Nasdaq Composite | 35-year price series; dot-com bust. |

VWCE.DE and VWRP.L track the same index, but the EUR line shows a deeper COVID
drawdown (-33.4% vs -25.1%, both on 2020-03-23). That is a currency effect: GBP
fell further against the dollar than EUR did in March 2020, which cushioned the
GBP line. VALL.L is GBP, so the GBP line is the relevant one.

## Summary

20-year requests where the series allows it; index rows ask for 35 years. Latest
close 2026-10-22, so these numbers move as history extends. `Starts` is Yahoo's
series start, not fund inception; † marks a run with at least one episode on a
partial peak window (see above).

| Symbol | Starts | Years | Max drawdown | Time in cash | Switches | Cash episodes | Worst episode |
|--------|--------|------:|-------------:|-------------:|---------:|--------------:|--------------:|
| VWRP.L† | 2019-07-25 | 7.3 | -25.1% | 5.7% | 4 | 2 | 0.24y |
| VWRL.L | 2012-05-22 | 14.5 | -25.0% | 8.8% | 8 | 4 | 0.64y |
| VWCE.DE† | 2019-07-29 | 7.3 | -33.4% | 15.2% | 6 | 3 | 0.68y |
| IWDA.L† | 2009-09-25 | 17.1 | -34.1% | 14.9% | 12 | 6 | 1.17y |
| VUSA.L | 2012-05-22 | 14.5 | -25.5% | 6.9% | 10 | 5 | 0.30y |
| ISF.L† | 2009-01-02 | 17.8 | -37.3% | 23.3% | 12 | 6 | 1.71y |
| VT† | 2008-06-26 | 18.3 | -50.6% | 36.4% | 14 | 7 | 2.13y |
| SPY† | 2006-10-23 | 20.0 | -56.5% | 27.1% | 12 | 6 | 3.27y |
| QQQ† | 2006-10-23 | 20.0 | -53.6% | 25.6% | 14 | 7 | 2.79y |
| VWO† | 2006-10-23 | 20.0 | -68.3% | 73.8% | 16 | 8 | 3.37y |
| ^GSPC | 1991-10-23 | 35.0 | -56.8% | 26.5% | 16 | 8 | 3.68y |
| ^FTSE† | 1991-10-23 | 35.1 | -51.6% | 33.9% | 18 | 9 | 3.96y |
| ^IXIC† | 1991-10-23 | 35.0 | -77.9% | 32.5% | 24 | 12 | 4.60y |

## Episode detail

Rows marked † began before the 756-bar window had filled, so their entry
drawdowns are shallower than a full-window run would show.

### VWRP.L - the closest proxy

| Start | End | Years | Drawdown at entry | Drawdown at exit |
|-------|-----|------:|------------------:|-----------------:|
| 2020-03-09† | 2020-06-04 | 0.24 | -18.0% | -5.9% |
| 2025-04-04 | 2025-06-10 | 0.17 | -15.3% | -5.2% |

Two real corrections in 7.3 years, both brief. The series has not lived through
a prolonged bear, so it cannot say how long a cash bucket would need to last.

### VT - long global history

| Start | End | Years | Drawdown at entry | Drawdown at exit |
|-------|-----|------:|------------------:|-----------------:|
| 2008-09-17† | 2010-11-02 | 2.13 | -16.1% | -5.2% |
| 2011-08-08 | 2012-09-12 | 1.10 | -19.8% | -6.4% |
| 2015-09-28 | 2016-08-12 | 0.88 | -15.8% | -5.2% |
| 2018-12-14 | 2019-05-02 | 0.38 | -15.1% | -5.8% |
| 2020-03-09 | 2020-07-20 | 0.37 | -18.6% | -5.1% |
| 2022-05-06 | 2024-01-26 | 1.72 | -15.8% | -5.1% |
| 2025-04-07 | 2025-05-01 | 0.07 | -15.8% | -5.6% |

The 2008 episode is the important one: entering near the start of the GFC means
more than two years of spending from cash before the re-arm threshold is met.

### ^IXIC - dot-com stress

| Start | End | Years | Drawdown at entry | Drawdown at exit |
|-------|-----|------:|------------------:|-----------------:|
| 1992-06-26† | 1992-11-04 | 0.37 | -15.1% | -6.1% |
| 1996-07-15 | 1996-09-12 | 0.17 | -15.1% | -6.7% |
| 1998-08-27 | 1998-11-18 | 0.23 | -16.3% | -5.8% |
| 2000-04-03 | 2004-11-10 | 4.60 | -16.3% | -5.5% |
| 2008-01-15 | 2010-12-02 | 2.89 | -15.4% | -5.3% |
| 2011-08-08 | 2011-10-26 | 0.23 | -18.0% | -7.8% |
| 2011-11-25 | 2012-01-17 | 0.14 | -15.0% | -5.1% |
| 2016-02-05 | 2016-04-15 | 0.19 | -16.4% | -5.4% |
| 2018-12-17 | 2019-03-15 | 0.24 | -16.7% | -5.2% |
| 2020-03-09 | 2020-05-19 | 0.20 | -19.0% | -6.4% |
| 2022-01-25 | 2024-01-18 | 1.98 | -15.7% | -6.2% |
| 2025-04-03 | 2025-05-15 | 0.12 | -18.0% | -5.3% |

After the dot-com peak the switch stays in cash for 4.6 years. That is not a
bug: the rolling peak falls, but not fast enough - the price stays more than
15% below it until late 2004, so the -5% re-arm is never met.

## What the runs show

- **Signals cluster on real corrections**, not on ordinary noise: COVID (2020),
  the 2022 rate shock, the April 2025 sell-off, the GFC and the dot-com bust.
  The VWRP.L proxy produced only two episodes in 7.3 years.
- **The cash bucket needs to cover multiple years.** The worst episodes are
  4.60 years (^IXIC), 3.96 (^FTSE), 3.68 (^GSPC), 3.37 (VWO) and 3.27 (SPY).
  A one- or two-year cash reserve would have run out in 2000-04 and 2008-2011.
  The full-window index runs confirm the order of magnitude: ^GSPC spends 3.27
  years in cash through the GFC and ^FTSE 3.06.
- **Secular bears keep it in cash for a long time.** VWO spends 73.8% of the
  period in cash, ^FTSE 33.9%, VT 36.4%. VWO's eight episodes include
  2008-2011 (3.37y), 2011-2014 (2.99y), 2015-2017 (2.14y), 2018-2020 (2.44y)
  and 2021-2025 (3.37y). The rule is not a dip-buyer; it waits for a recovery to
  within 5% of a rolling 3-year peak, which in a long sideways market can take
  years.
- **The near proxy is too young to be reassuring.** VWRP.L only starts in 2019,
  so it says nothing about 2000-02 or 2008; the global and index runs are what
  cover those.
- **The thresholds remain unvalidated.** The runs show the mechanism behaves as
  described; they do not show that 15%/5% is the best pair. `--sweep` on VT or
  ^IXIC is the next check if the thresholds are up for debate.

## Reproduce

```bash
# GBP/EUR proxies and funds
for sym in VWRP.L VWRL.L VWCE.DE IWDA.L VUSA.L ISF.L VT SPY QQQ VWO; do
  python vallmon.py backtest "$sym" --years 20
done

# 35-year price indices (quote the ^ so the shell leaves it alone)
for sym in '^GSPC' '^FTSE' '^IXIC'; do
  python vallmon.py backtest "$sym" --years 35
done
```

Results are point-in-time: Yahoo revises history and the trailing window moves,
so a run today will not match this page exactly.
