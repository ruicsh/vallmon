#!/usr/bin/env python3
"""vallmon - VALL Rolling Variance Delta (delta-sigma) monitor.

Fetches daily closes for VALL - the Vanguard FTSE Global All-Cap UCITS ETF
USD Acc (London Stock Exchange: VALL.L, ISIN IE000VAHT5T0) - and computes the
Rolling Variance Delta:

    delta_sigma = sigma_current / mu_sigma(t - N)

where `sigma_current` is annualized realized volatility over a short window W
and `mu_sigma` is the trailing mean of that volatility profile over an adaptive
N-day baseline.

It also reports a volatility term structure: realized vol at 10/20/30/60 trading
days, plus the slope sigma_10 / sigma_30 (backwardated vs contango).

Commands:
    run       fetch, compute, backfill/append to data/delta_sigma.csv, print
    rebuild   fetch and recompute the whole series from scratch (overwrites CSV)
    history   show recent readings with a trend sparkline
    drawdown  show the drawdown switch state and how much cash it implies
    backtest  replay the switch over a long proxy history (read-only)
    report    regenerate the findings section of README.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

# --- Configuration ----------------------------------------------------------

TICKER = "VALL.L"
EXCHANGE_TZ = "Europe/London"
TRADING_DAYS_PER_YEAR = 252

WINDOW = 20     # W: short realized-vol window, in trading days
BASELINE = 90   # N: max baseline window, in trading days (adaptive below this)

# Windows (in trading days) for the volatility term structure, shortest first.
# WINDOW (20d) is the primary/Δσ window; the others are supplementary. A window
# only produces a value once that many trading days of returns exist, so the
# longer legs simply stay empty until enough history has accrued.
SIGMA_WINDOWS = [10, 20, 30, 60]
TERM_SHORT = 10  # numerator window for the term-structure slope
TERM_LONG = 30   # denominator window for the term-structure slope

# Drawdown switch. Unlike delta-sigma this needs no baseline and no forecast: it
# is a fixed, observable rule for deciding which asset to sell from, so it has
# no parameters fitted to history and cannot drift the way a rolling statistic
# can.
#
# The switch has two boundaries rather than one. A single threshold is crossed
# on every tick that straddles it, which is not something a spending decision
# can follow; the gap between `enter` and `exit` is the hysteresis that makes
# the state stable enough to act on.
#
# The numbers are provisional defaults, not calibrated parameters: 756 days is
# simply "about three years", 15% is a conventional correction threshold, and
# 5% is "recovered enough to re-arm". They are function arguments so the rule
# can be re-parameterised without being rewritten - but nothing here has been
# backtested, so treat a signal as information, not as a validated trigger.
DRAWDOWN_PEAK_WINDOW = 756   # trailing peak, ~3 years of trading days
DRAWDOWN_THRESHOLD = 0.15    # switch to cash at -15% from the trailing peak
DRAWDOWN_EXIT_THRESHOLD = 0.05  # return to equity once the drawdown is >= -5%

# London cash close. A bar captured before it is incomplete, so `fetch_prices`
# drops it rather than freezing a partial session into the append-only log.
SESSION_CLOSE = dt_time(16, 30)

# Every term-structure window except the primary one. This is the single source
# of truth for both the persisted CSV columns and the per-row computation, so
# the two can never drift apart.
SUPPLEMENTARY_WINDOWS = [w for w in SIGMA_WINDOWS if w != WINDOW]

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "delta_sigma.csv"
README_PATH = ROOT / "README.md"

REPORT_START = "<!-- REPORT:START -->"
REPORT_END = "<!-- REPORT:END -->"

SPARK_WIDTH = 60   # sparkline length in the report section
REPORT_READING_ROWS = 20  # recent rows kept in README; the CSV holds the rest

# Supplementary term-structure columns: the primary window keeps its historical
# name (`sigma_annualized`).
TERM_FIELDS = ["sigma_%d" % w for w in SUPPLEMENTARY_WINDOWS]

CSV_FIELDS = [
    "date",
    "close",
    "volume",
    "sigma_annualized",
    "baseline_mean",
    "delta_sigma",
] + TERM_FIELDS + ["mode"]

# Fields recomputed from source on every reading. A stored value that disagrees
# with a fresh recompute is reported as drift (see find_drift).
METRIC_FIELDS = ["sigma_annualized", "baseline_mean", "delta_sigma"] + TERM_FIELDS

# `mode` is deliberately NOT in METRIC_FIELDS. It is recorded, not derived: the
# switch state that was actually signalled when the reading was written. It
# cannot be drift-checked against a recompute because recomputing it under new
# thresholds would silently rewrite the signal history the log exists to keep.

# Ask for the full available history: `run` merges into the existing CSV but
# `rebuild` overwrites it, so a short lookback would silently truncate readings
# once the series outgrows it.
#
# `range=max` comes back at hourly granularity for some tickers regardless of
# `interval=1d` (observed on VALL.L in 2026-10), which would turn the daily
# returns series into an intraday one. `fetch_prices` detects that and retries
# with a bounded range Yahoo does serve daily - long enough to cover any ETF
# that has ever existed, so the truncation concern stays theoretical.
YAHOO_RANGE = "max"
YAHOO_DAILY_FALLBACK_RANGE = "20y"
DAILY_GRANULARITY = "1d"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    "?range={range_}&interval=1d"
)

# The backtest replays the rule over an arbitrary long-history proxy and is
# strictly read-only: it never touches the ticker's CSV. It bounds the window
# with period1/period2 rather than `range=max`, because Yahoo serves `max` at
# an unusable granularity for some symbols (see YAHOO_RANGE).
BACKTEST_DEFAULT_YEARS = 20
BACKTEST_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={start}&period2={end}&interval=1d&events=div%2Csplit"
)
USER_AGENT = os.environ.get("VALLMON_USER_AGENT", "Mozilla/5.0 (compatible; vallmon/1.0)")

FETCH_ATTEMPTS = 3    # total tries per fetch (1 initial + retries)
FETCH_BACKOFF = 2.0   # seconds before the first retry; doubles each retry

# A stored reading that disagrees with a fresh recompute by more than this
# (in delta_sigma units) is reported as drift - see cmd_run / cmd_rebuild.
DRIFT_TOLERANCE = 1e-4

# Volatility labels describe dispersion, not direction, and nothing acts on
# them - so unlike the switch state they are derived context rather than a
# recorded signal. A later change to the bands or the confirmation window is
# therefore allowed to re-render history.
#
# A band must be held for VOLATILITY_CONFIRM readings before the label moves.
# Without that, one quiet or busy session drags delta_sigma across a band edge
# and the label flips, implying a change in conditions that did not happen.
VOLATILITY_CONFIRM = 3

VOLATILITY_BANDS = (
    (1.5, "elevated", "\U0001F534"),      # red circle
    (1.15, "expanding", "\U0001F7E0"),    # orange circle
    (0.85, "normal", "\U0001F7E2"),       # green circle
    (0.7, "compressing", "\U0001F535"),   # blue circle
)
VOLATILITY_COMPRESSED = ("compressed", "\u26AA")   # white circle

# Plain-words description per band, used by the README legend.
VOLATILITY_PLAIN = {
    "compressed": "Unusually still. The market is barely twitching day to day. "
    "Peaceful \u2014 though sometimes the quiet before a storm.",
    "compressing": "Calming down. Quieter than it was a few weeks ago.",
    "normal": "Ordinary. The usual amount of daily movement.",
    "expanding": "Getting jumpier. Daily swings are growing \u2014 something is stirring.",
    "elevated": "Unusually turbulent. Big daily swings. Usually a stressful period, "
    "but also the time you'd want to be buying rather than selling.",
}

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# --- Data fetching ----------------------------------------------------------

class FetchError(RuntimeError):
    """Raised when price data cannot be fetched, retrieved or parsed."""


def _parse_payload(payload, require_volume=True):
    """Turn a Yahoo Finance chart payload into a sorted [(iso_date, close, volume)] list.

    Volume is the number of ETF units traded that session on the exchange.
    Rows with a null close or zero volume are discarded. Yahoo emits a couple
    of zero-volume placeholder bars at the start of a new listing which would
    otherwise corrupt the returns series. Malformed payloads raise FetchError
    rather than surfacing as a raw KeyError/IndexError.

    ``require_volume=False`` keeps bars with no volume, which is what indices
    and some proxies return; the backtest uses it because it only needs closes.
    """
    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise FetchError("Yahoo Finance error: %s" % error)

    results = chart.get("result")
    if not results:
        raise FetchError("Yahoo Finance returned no chart result")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    if len(timestamps) != len(closes) or (
        require_volume and len(volumes) != len(timestamps)
    ):
        raise FetchError(
            "Yahoo Finance returned ragged arrays "
            "(timestamps=%d, closes=%d, volumes=%d)"
            % (len(timestamps), len(closes), len(volumes))
        )
    if not require_volume and len(volumes) != len(timestamps):
        # Some symbols (indices, a few proxies) carry no volume array at all.
        # The backtest only needs closes, so pad instead of rejecting them.
        volumes = [None] * len(timestamps)

    tz = ZoneInfo(EXCHANGE_TZ)
    # Collapse to one bar per local date. Yahoo occasionally serves intraday
    # bars even for `interval=1d` (see YAHOO_RANGE), and a series with several
    # observations per day would turn the "daily" returns - and therefore sigma
    # - into an intraday statistic. Sorting by timestamp first makes "the last
    # bar of the session" follow the clock rather than the order the provider
    # happened to emit; volume then sums to the day's traded units. With true
    # daily bars this is a no-op.
    bars = sorted(
        (
            (ts, close, volume)
            for ts, close, volume in zip(timestamps, closes, volumes)
            if close is not None
            and (not require_volume or (volume is not None and volume != 0))
        ),
        key=lambda bar: bar[0],
    )
    by_day: dict[str, tuple[float, int]] = {}
    for ts, close, volume in bars:
        day = datetime.fromtimestamp(ts, tz=tz).date().isoformat()
        prior = by_day.get(day)
        by_day[day] = (float(close), int(volume or 0) + (prior[1] if prior else 0))

    return [(day, close, units) for day, (close, units) in sorted(by_day.items())]


def drop_partial_bar(prices, now=None):
    """Drop the current session's bar while the London market is still open.

    Yahoo returns the in-progress bar for today. Freezing it into the
    append-only log would lock a partial session in for good (a later run never
    rewrites a stored date), so it is dropped here and picked up by the run
    after the close. `now` exists so tests can pin the clock.
    """
    if not prices:
        return prices
    now = now or datetime.now(ZoneInfo(EXCHANGE_TZ))
    if now.time() >= SESSION_CLOSE:
        return prices
    today = now.date().isoformat()
    return prices[:-1] if prices[-1][0] == today else prices


def _chart_url(range_):
    return YAHOO_URL.format(ticker=TICKER, range_=range_)


def _payload_granularity(payload):
    """Yahoo's declared bar granularity (``"1d"``, ``"1h"``, ...), or None."""
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        return None
    return (results[0].get("meta") or {}).get("dataGranularity")


def _is_daily(payload):
    """True unless the payload explicitly declares a non-daily granularity.

    A payload without the metadata is assumed fine: the parser collapses bars
    by date regardless, so an unknown granularity degrades rather than fails.
    """
    granularity = _payload_granularity(payload)
    return granularity is None or granularity == DAILY_GRANULARITY


def _fetch_payload(url, attempts=FETCH_ATTEMPTS, backoff=FETCH_BACKOFF, label=TICKER):
    """GET `url` and decode the JSON, retrying transient failures."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                delay = backoff ** (attempt - 1)
                print(
                    "Fetch attempt %d/%d failed (%s); retrying in %.0fs"
                    % (attempt, attempts, exc, delay),
                    file=sys.stderr,
                )
                time.sleep(delay)
    raise FetchError(
        "failed to fetch %s after %d attempts: %s" % (label, attempts, last_error)
    )


def fetch_prices(range_=YAHOO_RANGE, attempts=FETCH_ATTEMPTS, backoff=FETCH_BACKOFF):
    """Fetch and parse VALL.L daily closes and volumes, retrying transient failures.

    Network/timeout/parse errors are retried with exponential backoff; a
    response that Yahoo explicitly flags as an error is raised immediately.

    `range=max` is requested first so a rebuild can never truncate history, but
    Yahoo sometimes serves it at hourly granularity (see YAHOO_RANGE). When the
    response says so, the request is retried with a long bounded range that it
    does serve daily. `_parse_payload` collapses bars by date either way, so a
    still-intraday response still yields a usable daily series.
    """
    payload = _fetch_payload(_chart_url(range_), attempts, backoff)
    if not _is_daily(payload):
        payload = _fetch_payload(_chart_url(YAHOO_DAILY_FALLBACK_RANGE), attempts, backoff)
        if not _is_daily(payload):
            print(
                "warning: %s served at %s granularity for range=%s; "
                "session closes are approximated by the last bar."
                % (TICKER, _payload_granularity(payload), YAHOO_DAILY_FALLBACK_RANGE),
                file=sys.stderr,
            )
    return drop_partial_bar(_parse_payload(payload))


# --- Metric -----------------------------------------------------------------

def _sigma_series(returns, window):
    """Annualized realized vol at each price index with a full `window` of returns.

    Returns {price_index: sigma}, indexed the same way as the price list (a
    value at index i is the sigma of the window of returns ending at price i).
    Sample standard deviation (n-1 denominator), the usual convention for
    realized volatility.
    """
    sigmas = {}
    for j in range(window - 1, len(returns)):
        chunk = returns[j - window + 1 : j + 1]
        sigmas[j + 1] = statistics.stdev(chunk) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return sigmas


def compute_rows(prices):
    """Compute the full delta-sigma series from a list of (date, close, volume).

    Returns a list of dicts, oldest first. The metric fields (close,
    sigma_annualized, baseline_mean, delta_sigma and the supplementary
    term-structure sigmas) are floats; volume is a unit count stored as an int.
    A term-structure sigma is absent (None) for a date that does not yet have a
    full window of returns.
    """
    if len(prices) < 2:
        return []

    days = [day for day, _, _ in prices]
    closes = [close for _, close, _ in prices]
    volumes = [volume for _, _, volume in prices]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    # Annualized realized vol per window: {window: {price_index: sigma}}.
    # Rows are keyed on the primary WINDOW so the set of readings (and thus the
    # Δσ series) is unchanged by the supplementary windows.
    windows = sorted(set(SIGMA_WINDOWS) | {WINDOW})
    sigmas = {w: _sigma_series(returns, w) for w in windows}
    primary = sigmas[WINDOW]
    primary_indices = sorted(primary)
    primary_values = [primary[i] for i in primary_indices]

    rows = []
    for k in range(1, len(primary_values)):
        price_idx = primary_indices[k]
        sigma = primary_values[k]
        prior = primary_values[:k]
        n_base = min(BASELINE, len(prior))
        baseline = statistics.fmean(prior[-n_base:])
        if baseline <= 0:
            continue
        row = {
            "date": days[price_idx],
            "close": closes[price_idx],
            "volume": volumes[price_idx],
            "sigma_annualized": sigma,
            "baseline_mean": baseline,
            "delta_sigma": sigma / baseline,
        }
        for window in SUPPLEMENTARY_WINDOWS:
            row["sigma_%d" % window] = sigmas[window].get(price_idx)
        rows.append(row)

    # Record the switch state alongside the reading. It is stored rather than
    # derived at read time, so re-parameterising the thresholds later cannot
    # rewrite the signal history the log exists to preserve.
    closes = [row["close"] for row in rows]
    peaks = trailing_peaks(closes)
    drawdowns = [close / peak - 1.0 if peak else 0.0 for close, peak in zip(closes, peaks)]
    for row, mode in zip(rows, drawdown_modes(drawdowns)):
        row["mode"] = mode
    return rows


def volatility_band(delta):
    """Index of the band `delta` falls in; len(VOLATILITY_BANDS) is compressed."""
    for index, (threshold, _, _) in enumerate(VOLATILITY_BANDS):
        if delta >= threshold:
            return index
    return len(VOLATILITY_BANDS)


def band_name(index):
    """The plain name for a band index."""
    if index < len(VOLATILITY_BANDS):
        return VOLATILITY_BANDS[index][1]
    return VOLATILITY_COMPRESSED[0]


def band_label(index):
    """The emoji + name for a band index (terminal surfaces)."""
    if index < len(VOLATILITY_BANDS):
        dot = VOLATILITY_BANDS[index][2]
    else:
        dot = VOLATILITY_COMPRESSED[1]
    return "%s %s" % (dot, band_name(index))


def volatility_label(delta):
    """The label for a single reading, with no hysteresis."""
    return band_label(volatility_band(delta))


def volatility_markdown(index):
    """The level bar alone, for the GitHub-rendered README.

    The band name lives in the alt text, so the column stays readable to a
    screen reader or when images are blocked.
    """
    name = band_name(index)
    return '<img src="docs/assets/vol-%s.svg" alt="%s" width="19" height="12">' % (
        name,
        name,
    )


def volatility_legend_lines():
    """Visible scale plus a collapsible plain-words legend, low to high."""
    indices = list(reversed(range(len(VOLATILITY_BANDS) + 1)))
    items = ["%s %s" % (volatility_markdown(index), band_name(index)) for index in indices]
    lines = [
        "**Volatility bars** \u2014 \u0394\u03c3 against its 90-day baseline. Three bars is "
        "normal; more means bigger day-to-day moves.",
        "",
        " \u00b7 ".join(items),
        "",
        "<details>",
        "<summary>What the bars mean</summary>",
        "",
        "| Bars | Meaning | In plain words |",
        "|------|---------|----------------|",
    ]
    for index in indices:
        name = band_name(index)
        lines.append(
            "| %s | %s | %s |"
            % (volatility_markdown(index), name, VOLATILITY_PLAIN[name])
        )
    lines += ["", "</details>"]
    return lines


def volatility_bands(deltas, confirm=VOLATILITY_CONFIRM):
    """Sticky band index per reading (0 = elevated, len = compressed).

    A band must hold for `confirm` consecutive readings before it moves, so
    ordinary day-to-day noise around a band edge cannot re-label the series.
    The first reading sets the starting band.
    """
    if confirm < 1:
        raise ValueError("confirm must be at least 1")
    if not deltas:
        return []
    bands = [volatility_band(delta) for delta in deltas]
    current = bands[0]
    pending = None
    streak = 0
    result = [current]
    for band in bands[1:]:
        if band == current:
            pending, streak = None, 0
        else:
            if band == pending:
                streak += 1
            else:
                pending, streak = band, 1
            if streak >= confirm:
                current, pending, streak = band, None, 0
        result.append(current)
    return result


def volatility_labels(deltas, confirm=VOLATILITY_CONFIRM):
    """Sticky emoji labels, for surfaces that cannot render images."""
    return [band_label(index) for index in volatility_bands(deltas, confirm)]


# --- Drawdown ---------------------------------------------------------------

def trailing_peaks(closes, window=DRAWDOWN_PEAK_WINDOW):
    """Trailing maximum close for each index, over at most `window` closes.

    The peak includes the current observation, so a new high resets the
    drawdown to zero. With less than `window` history the whole series is the
    window, which is why callers report how much history the rule has seen.
    """
    return [max(closes[max(0, i - window + 1) : i + 1]) for i in range(len(closes))]


def drawdown_series(closes, window=DRAWDOWN_PEAK_WINDOW):
    """Fractional drawdown from the trailing peak: 0 at a new high, negative below."""
    return [
        close / peak - 1.0 if peak else 0.0
        for close, peak in zip(closes, trailing_peaks(closes, window))
    ]


class DrawdownReading(NamedTuple):
    """One reading paired with the switch state and the transition it implies."""

    row: dict
    peak: float
    drawdown: float
    mode: str      # "equity" while the switch is armed, "cash" while in use
    signal: str    # "hold" unless this reading is a transition


def drawdown_modes(drawdowns, enter=DRAWDOWN_THRESHOLD, exit=DRAWDOWN_EXIT_THRESHOLD):
    """Hysteresis state machine over a drawdown series: "equity" or "cash".

    A single threshold flips state on every tick that straddles it - about three
    times a year on long history - which is not something a spending decision
    can follow. Two boundaries make the state stable: enter cash at or below
    ``-enter``, and return to equity only once the drawdown has recovered to
    ``-exit``. A new high (drawdown 0) is inside that band, so it also re-arms.

    The state is carried forward from the previous reading, so this is the one
    part of the drawdown rule that needs the whole series rather than a single
    close/peak pair.
    """
    if not 0 <= exit <= enter:
        raise ValueError("require 0 <= exit <= enter")
    modes = []
    mode = "equity"
    for drawdown in drawdowns:
        if mode == "equity" and drawdown <= -enter:
            mode = "cash"
        elif mode == "cash" and drawdown >= -exit:
            mode = "equity"
        modes.append(mode)
    return modes


def _recorded_modes(rows):
    """The switch states stored with the readings, or None if incomplete.

    Recorded state wins over a recompute. The log is the audit trail, and a
    later change of threshold must not rewrite what the monitor actually
    signalled at the time. Rows written before the column existed fall back to
    a recompute until `run` backfills them.
    """
    modes = [row.get("mode") or "" for row in rows]
    if modes and all(mode in ("equity", "cash") for mode in modes):
        return modes
    if any(mode in ("equity", "cash") for mode in modes):
        warnings.warn(
            "switch state is only partially recorded; recomputing it for the whole "
            "series (run `python vallmon.py run` to backfill)",
            RuntimeWarning,
            stacklevel=2,
        )
    return None


def drawdown_rows(
    rows, window=DRAWDOWN_PEAK_WINDOW, enter=DRAWDOWN_THRESHOLD, exit=DRAWDOWN_EXIT_THRESHOLD
):
    """DrawdownReading per stored row, oldest first.

    `enter`/`exit` only apply to rows that have no recorded `mode`; recorded
    state is used as-is.
    """
    closes = [float(row["close"]) for row in rows]
    peaks = trailing_peaks(closes, window)
    drawdowns = [close / peak - 1.0 if peak else 0.0 for close, peak in zip(closes, peaks)]
    modes = _recorded_modes(rows)
    if modes is None:
        modes = drawdown_modes(drawdowns, enter, exit)
    readings = []
    previous = modes[0] if modes else "equity"
    for row, peak, drawdown, mode in zip(rows, peaks, drawdowns, modes):
        if mode == previous:
            signal = "hold"
        elif mode == "cash":
            signal = "draw from cash"
        else:
            signal = "refill"
        readings.append(DrawdownReading(row, peak, drawdown, mode, signal))
        previous = mode
    return readings


def cash_episode(readings):
    """(start date, reading count) of the current cash episode, or None.

    Only a cash episode has a length worth reporting: it is the stretch during
    which spending is coming out of cash, so it is what a cash bucket has to
    cover.
    """
    if not readings or readings[-1].mode != "cash":
        return None
    start = len(readings) - 1
    while start > 0 and readings[start - 1].mode == "cash":
        start -= 1
    return readings[start].row["date"], len(readings) - start


def current_state_since(readings):
    """(start date, reading count) of the state the latest reading is in."""
    mode = readings[-1].mode
    start = len(readings) - 1
    while start > 0 and readings[start - 1].mode == mode:
        start -= 1
    return readings[start].row["date"], len(readings) - start


def transitions(readings):
    """[(change date, close, from mode, to mode, readings held before)]."""
    changes = []
    start = 0
    for index in range(1, len(readings)):
        if readings[index].mode == readings[index - 1].mode:
            continue
        changes.append(
            (
                readings[index].row["date"],
                float(readings[index].row["close"]),
                readings[index - 1].mode,
                readings[index].mode,
                index - start,
            )
        )
        start = index
    return changes


def window_full_date(rows, window=DRAWDOWN_PEAK_WINDOW):
    """The date the trailing window is finally full, or None once it is.

    Until then the reported "peak" is just the highest close seen so far, which
    is not the same thing - so the state is flagged provisional.
    """
    remaining = window - len(rows)
    if remaining <= 0:
        return None
    latest = datetime.strptime(rows[-1]["date"], "%Y-%m-%d")
    days = round(remaining * 365 / TRADING_DAYS_PER_YEAR)
    return (latest + timedelta(days=days)).date().isoformat()


def drawdown_lines(rows, window=DRAWDOWN_PEAK_WINDOW):
    """Markdown table of the drawdown inputs behind the switch."""
    readings = drawdown_rows(rows, window)
    reading = readings[-1]
    peak_day = next(
        (row["date"] for row in reversed(rows) if abs(float(row["close"]) - reading.peak) <= 1e-9),
        "unknown",
    )
    switch_at = DRAWDOWN_THRESHOLD if reading.mode == "equity" else DRAWDOWN_EXIT_THRESHOLD
    return [
        "| Metric | Value |",
        "|--------|-------|",
        "| Peak (trailing %dd) | %.4f (%s) |" % (window, reading.peak, peak_day),
        "| Current drawdown | %.2f%% |" % (reading.drawdown * 100),
        "| Distance to switch | %.1f pp |" % (abs(-switch_at - reading.drawdown) * 100),
        "| History seen | %d of %d readings |" % (len(rows), window),
        "",
        "_Rule: switch to cash at or below -%.0f%% of the trailing peak; "
        "return to equity at or above -%.0f%%._"
        % (DRAWDOWN_THRESHOLD * 100, DRAWDOWN_EXIT_THRESHOLD * 100),
    ]


def state_lines(rows):
    """The headline: what state, what would change it, how far away it is."""
    readings = drawdown_rows(rows)
    reading = readings[-1]
    since, held = current_state_since(readings)
    switch_at = DRAWDOWN_THRESHOLD if reading.mode == "equity" else DRAWDOWN_EXIT_THRESHOLD
    gap = abs(-switch_at - reading.drawdown) * 100
    if reading.mode == "equity":
        action = "No action. Switch to cash at -%.0f%% — a further %.1f pp." % (
            switch_at * 100,
            gap,
        )
    else:
        action = "Draw from cash. Return to equity at -%.0f%% — a recovery of %.1f pp." % (
            switch_at * 100,
            gap,
        )
    lines = [
        "**%s** — drawdown %.2f%%, unchanged for %d reading%s (since %s)."
        % (
            reading.mode.capitalize(),
            reading.drawdown * 100,
            held,
            "" if held == 1 else "s",
            since,
        ),
        "",
        action,
    ]
    full = window_full_date(rows)
    if full:
        lines += [
            "",
            "> Window: %d of %d readings. The peak is not yet a %d-day peak, so"
            " signals are provisional until ~%s."
            % (len(rows), DRAWDOWN_PEAK_WINDOW, DRAWDOWN_PEAK_WINDOW, full),
        ]
    return lines


def signal_history_lines(rows):
    """Every switch, newest first - the audit trail for the rule."""
    readings = drawdown_rows(rows)
    changes = transitions(readings)
    if not changes:
        return [
            "_No state changes yet — the switch has been in **%s** since the first"
            " reading._" % readings[-1].mode
        ]
    lines = [
        "| Changed | Close (GBP) | From → To | Held before |",
        "|---------|-------------|------------|-------------|",
    ]
    for changed, close, from_mode, to_mode, held in reversed(changes):
        lines.append(
            "| %s | %.3f | %s → %s | %d readings (~%.1f mo) |"
            % (changed, close, from_mode, to_mode, held, held / (TRADING_DAYS_PER_YEAR / 12))
        )
    return lines


def sparkline(values):
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_CHARS[3] * len(values)
    scale = (len(SPARK_CHARS) - 1) / (high - low)
    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((v - low) * scale))] for v in values)


def sparkline_summary(values):
    """A sparkline with endpoints and range, so it reads without the numbers."""
    if not values:
        return "—"
    return "`%s` %.2f → %.2f (range %.2f–%.2f, %d reading%s)" % (
        sparkline(values),
        values[0],
        values[-1],
        min(values),
        max(values),
        len(values),
        "" if len(values) == 1 else "s",
    )


# --- Persistence ------------------------------------------------------------

def load_rows():
    """Load the CSV as {date: row_dict_of_strings}."""
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="") as handle:
        return {row["date"]: row for row in csv.DictReader(handle)}


def _format(row):
    formatted = {
        "date": row["date"],
        "close": "%.4f" % row["close"],
        "volume": "%d" % int(row["volume"]),
        "sigma_annualized": "%.6f" % row["sigma_annualized"],
        "baseline_mean": "%.6f" % row["baseline_mean"],
        "delta_sigma": "%.4f" % row["delta_sigma"],
    }
    for field in TERM_FIELDS:
        value = row.get(field)
        formatted[field] = "" if value is None else "%.6f" % value
    formatted["mode"] = row.get("mode") or ""
    return formatted


def volume_units(row):
    """Traded units as an int, or None when the value is missing/unparseable."""
    raw = row.get("volume")
    if not raw:
        return None
    try:
        return int(float(str(raw).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def sigma_field(window):
    """CSV column holding the realized vol for `window` trading days."""
    return "sigma_annualized" if window == WINDOW else "sigma_%d" % window


def _as_float(raw):
    """Parse a stored numeric field, or None when absent/unparseable.

    Values come from our own CSV, but a hand-edited or corrupted cell must
    degrade to "unknown" rather than crash a run.
    """
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def sigma_value(row, window):
    """Realized vol for `window` as a float, or None when absent/missing."""
    return _as_float(row.get(sigma_field(window)))


def sigma_display(row, window):
    """Annualized sigma as a percentage string, or an em dash when unavailable."""
    value = sigma_value(row, window)
    return "\u2014" if value is None else "%.2f%%" % (value * 100)


def term_structure_display(row):
    """Compact per-window sigma row, e.g. ``11.02% / 10.19% / — / —``."""
    return " / ".join(sigma_display(row, window) for window in SIGMA_WINDOWS)


def term_slope(row):
    """σ_short / σ_long (10d/30d), or None when either leg is unavailable.

    A value above 1 means the short leg is above the long leg (an
    inverted/backwardated curve, the classic stress shape); below 1 is the
    normal, calm shape. A missing leg or a zero long leg (undefined ratio)
    yields None; a genuinely flat short leg (0.0) is a valid reading.
    """
    short = sigma_value(row, TERM_SHORT)
    long_ = sigma_value(row, TERM_LONG)
    if short is None or long_ is None or long_ == 0:
        return None
    return short / long_


def term_slope_label(slope):
    if slope is None:
        return "n/a"
    if slope >= 1.05:
        return "backwardated"
    if slope <= 0.95:
        return "contango"
    return "flat"


def slope_display(slope):
    """Format a precomputed slope, or an em dash when unavailable."""
    return "\u2014" if slope is None else "%.2f" % slope


def term_slope_display(row):
    return slope_display(term_slope(row))


def volume_display(row, with_units=False):
    """Human-readable volume: grouped units, or an em dash when unknown.

    Rows written before the volume column existed have no value until they are
    backfilled by `run`/`rebuild`, so reports must tolerate a missing field.
    With `with_units`, a known value gets a " units" suffix while an unknown
    one stays a bare em dash.
    """
    units = volume_units(row)
    if units is None:
        return "\u2014"
    text = "{:,}".format(units)
    return "%s units" % text if with_units else text


def delta_series(rows):
    """The Δσ value of every row, oldest first."""
    return [float(row["delta_sigma"]) for row in rows]


# GitHub strips CSS from rendered markdown, so colour in the README comes from
# small SVG assets rather than styled text. Terminals cannot render images, so
# the CLI keeps the emoji. Squares rather than the circles used for volatility
# labels: the two columns sit side by side, and green must not mean "normal" in
# one and "gain" in the next.
DAY_UP = "\U0001F7E9"      # green square (terminal)
DAY_DOWN = "\U0001F7E5"    # red square (terminal)
DAY_FLAT = "\u2B1C"        # white square (terminal)

DAY_DIRECTIONS = {
    "up": (DAY_UP, "docs/assets/arrow-up.svg", "gain"),
    "down": (DAY_DOWN, "docs/assets/arrow-down.svg", "loss"),
    "flat": (DAY_FLAT, "docs/assets/arrow-flat.svg", "unchanged"),
}


def day_direction(change):
    """`up`, `down`, `flat`, or None when there is no previous reading."""
    if change is None:
        return None
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


def day_marker(change):
    """Emoji direction marker, for surfaces that cannot render images."""
    direction = day_direction(change)
    return "" if direction is None else DAY_DIRECTIONS[direction][0]


def day_image(change):
    """Inline SVG marker, for the GitHub-rendered README."""
    direction = day_direction(change)
    if direction is None:
        return ""
    _, path, alt = DAY_DIRECTIONS[direction]
    return '<img src="%s" alt="%s" width="12" height="12">' % (path, alt)


def day_display(change):
    """Change followed by its emoji marker (terminal output).

    The marker trails the value so it lands in a fixed column: the numeric
    columns are right-aligned, so a leading marker would shift with the width
    of the number.
    """
    if change is None:
        return "\u2014"
    return "%+.2f%% %s" % (change * 100, day_marker(change))


def day_markdown(change):
    """Change followed by its SVG marker (README output)."""
    if change is None:
        return "\u2014"
    return "%+.2f%% %s" % (change * 100, day_image(change))


def daily_change_series(rows):
    """Close-to-close change of every row, oldest first; None for the first."""
    closes = [float(row["close"]) for row in rows]
    changes = [None]
    for previous, current in zip(closes, closes[1:]):
        changes.append(current / previous - 1.0 if previous else None)
    return changes


def reading_headline(row, label=None):
    """One-line summary of a reading, shared by the README and CI surfaces."""
    delta = float(row["delta_sigma"])
    return "**Δσ = %.2f** %s · %.2f%% vol vs %.2f%% baseline · %s · %s" % (
        delta,
        label or volatility_label(delta),
        float(row["sigma_annualized"]) * 100,
        float(row["baseline_mean"]) * 100,
        volume_display(row, with_units=True),
        row["date"],
    )


def save_rows(rows_by_date):
    """Write the CSV atomically, so an interrupted run cannot corrupt it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CSV_PATH.with_name(CSV_PATH.name + ".tmp")
    with tmp_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for day in sorted(rows_by_date):
            writer.writerow({field: rows_by_date[day].get(field, "") for field in CSV_FIELDS})
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, CSV_PATH)


def _write_text_atomic(path, text):
    """Write text through a temp file + atomic replace, like `save_rows`.

    Keeps an interrupted run from leaving a half-written README/REPORT behind,
    reusing the same ``*.tmp`` scratch convention (already git-ignored).
    """
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def sorted_rows(rows_by_date=None):
    rows = (rows_by_date if rows_by_date is not None else load_rows()).values()
    return sorted(rows, key=lambda row: row["date"])


def find_drift(stored, computed):
    """Return [(date, field, stored_value, computed_value)] for stale readings.

    Existing readings are frozen once written, so a later revision of the
    source prices is not reflected. Comparing a fresh recompute against the
    CSV lets `run` flag that without silently rewriting history. Fields that
    have not been captured yet (an empty/missing column) are skipped - there is
    nothing to compare against.
    """
    drifted = []
    for row in computed:
        prior = stored.get(row["date"])
        if prior is None:
            continue
        for field in METRIC_FIELDS:
            value = row.get(field)
            stored_value = _as_float(prior.get(field))
            if value is None or stored_value is None:
                continue
            if abs(stored_value - value) > DRIFT_TOLERANCE:
                drifted.append((row["date"], field, stored_value, value))
    return drifted


# --- Commands ---------------------------------------------------------------

def _fetch_computed():
    """Fetch prices and compute the full series.

    Raises FetchError if the fetch fails; returns [] if there is not yet
    enough history for a reading.
    """
    prices = fetch_prices()
    if not prices:
        raise FetchError("no price data returned")
    return compute_rows(prices)


def _not_enough_history():
    print(
        "Not enough history yet to compute a reading "
        "(need %d+ trading days)." % (WINDOW + 1)
    )
    return 0


def cmd_run(ci=False, strict=False):
    try:
        computed = _fetch_computed()
    except FetchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if not computed:
        return _not_enough_history()

    existing = load_rows()

    drifted = find_drift(existing, computed)
    if drifted:
        affected = len({entry[0] for entry in drifted})
        print(
            "warning: %d stored value(s) across %d reading(s) differ from a fresh "
            "recompute; source prices may have been revised. "
            "Run `python vallmon.py rebuild` to reconcile." % (len(drifted), affected),
            file=sys.stderr,
        )
        for date, field, stored_value, fresh_value in drifted[:5]:
            print(
                "  %s %s: stored %.6f -> recomputed %.6f"
                % (date, field, stored_value, fresh_value),
                file=sys.stderr,
            )
        if len(drifted) > 5:
            print("  ... and %d more" % (len(drifted) - 5), file=sys.stderr)
        if strict:
            print(
                "error: refusing to append to a drifted series; "
                "run `python vallmon.py rebuild` first.",
                file=sys.stderr,
            )
            return 1

    added = 0
    backfilled = 0
    migrated = 0
    for row in computed:
        prior = existing.get(row["date"])
        if prior is None:
            existing[row["date"]] = _format(row)
            added += 1
            continue
        if not prior.get("volume"):
            # Reading is frozen, but the new volume column can be populated
            # from the same source without touching the stored metric.
            prior["volume"] = "%d" % int(row["volume"])
            backfilled += 1
        # One-time migration for metric columns added after this row was first
        # written. There is no prior value to be stale against, so filling the
        # gap is safe; once present the value is frozen and drift-checked.
        row_migrated = False
        for field in TERM_FIELDS:
            if not prior.get(field) and row.get(field) is not None:
                prior[field] = "%.6f" % row[field]
                row_migrated = True
        if not prior.get("mode") and row.get("mode"):
            prior["mode"] = row["mode"]
            row_migrated = True
        if row_migrated:
            migrated += 1
    save_rows(existing)

    rows = sorted_rows(existing)
    latest = rows[-1]
    delta = float(latest["delta_sigma"])
    print_reading(latest, delta, volatility_labels(delta_series(rows))[-1])
    print_drawdown(rows)
    print(
        "  (%d new reading%s written to %s)"
        % (added, "" if added == 1 else "s", CSV_PATH.relative_to(ROOT))
    )
    if backfilled:
        print(
            "  (backfilled volume for %d existing reading%s)"
            % (backfilled, "" if backfilled == 1 else "s")
        )
    if migrated:
        print(
            "  (backfilled derived columns for %d existing reading%s)"
            % (migrated, "" if migrated == 1 else "s")
        )

    if ci:
        write_step_summary(rows)
    return 0


def print_reading(row, delta, label=None):
    slope = term_slope(row)
    print("VALL Rolling Variance Delta - %s" % row["date"])
    print("  sigma current (W=%d, ann.)   %6.2f%%" % (WINDOW, float(row["sigma_annualized"]) * 100))
    print("  baseline mean  (N<=%d)       %6.2f%%" % (BASELINE, float(row["baseline_mean"]) * 100))
    print("  volume (units, daily)       %12s" % volume_display(row))
    print("  term structure (%s)   %s"
          % ("/".join(str(w) for w in SIGMA_WINDOWS), term_structure_display(row)))
    print("  term slope (s%d/s%d)          %6s   -> %s"
          % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope)))
    print("  delta_sigma                  %6.2f   -> %s"
          % (delta, label or volatility_label(delta)))


def print_drawdown(rows):
    reading = drawdown_rows(rows)[-1]
    print("  peak (trailing %dd)        %8.4f" % (DRAWDOWN_PEAK_WINDOW, reading.peak))
    print(
        "  drawdown from peak           %6.2f%%   -> %s"
        % (reading.drawdown * 100, reading.signal)
    )
    print("  switch state                %8s" % reading.mode)


def cmd_history(limit):
    if limit <= 0:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python vallmon.py run` first.")
        return 0
    recent = rows[-limit:]
    changes = daily_change_series(rows)[-len(recent) :]
    labels = volatility_labels(delta_series(rows))[-len(recent) :]
    print("VALL Rolling Variance Delta - last %d readings\n" % len(recent))
    print(
        "  %-10s  %6s  %9s  %12s  %7s  %7s  %6s   %s"
        % ("date", "close", "day", "volume", "sigma", "base", "d-sigma", "volatility")
    )
    for row, change, label in zip(recent, changes, labels):
        delta = float(row["delta_sigma"])
        day = day_display(change)
        print(
            "  %s  %6.3f  %8s  %12s  %6.2f%%  %6.2f%%  %6.2f   %s"
            % (
                row["date"],
                float(row["close"]),
                day,
                volume_display(row),
                float(row["sigma_annualized"]) * 100,
                float(row["baseline_mean"]) * 100,
                delta,
                label,
            )
        )
    deltas = delta_series(recent)
    print("\n  trend  %s" % sparkline(deltas))
    print("         %.2f -> %.2f" % (deltas[0], deltas[-1]))
    return 0


def cmd_drawdown(limit, annual_spend=None):
    if limit <= 0:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if annual_spend is not None and annual_spend <= 0:
        print("error: --annual-spend must be a positive number", file=sys.stderr)
        return 2
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python vallmon.py run` first.")
        return 0
    readings = drawdown_rows(rows)
    recent = readings[-limit:]
    print("VALL drawdown from trailing peak - last %d readings\n" % len(recent))
    print("  date         close     peak   drawdown   mode     signal")
    for reading in recent:
        print(
            "  %s  %6.3f  %7.3f  %8.2f%%   %-7s  %s"
            % (
                reading.row["date"],
                float(reading.row["close"]),
                reading.peak,
                reading.drawdown * 100,
                reading.mode,
                reading.signal,
            )
        )
    print("\n  history seen  %d of %d readings" % (len(rows), DRAWDOWN_PEAK_WINDOW))
    print(
        "  rule          cash at <= -%.0f%%, re-arm at >= -%.0f%%"
        % (DRAWDOWN_THRESHOLD * 100, DRAWDOWN_EXIT_THRESHOLD * 100)
    )
    episode = cash_episode(readings)
    if episode:
        start, count = episode
        months = count / (TRADING_DAYS_PER_YEAR / 12)
        print("  cash episode  since %s, %d readings (~%.1f months)" % (start, count, months))
        if annual_spend is not None:
            print(
                "  spend covered %s at %s/yr"
                % (
                    format(annual_spend * months / 12, ",.0f"),
                    format(annual_spend, ",.0f"),
                )
            )
    return 0


# --- Backtest ---------------------------------------------------------------

class BacktestEpisode(NamedTuple):
    """One cash episode produced by replaying the switch."""

    start: str
    end: str
    position: int   # where it sits in the replayed series, for the partial-window note
    readings: int
    entry: float
    exit: float


class BacktestStats(NamedTuple):
    """What the switch did across a whole replayed series."""

    bars: int
    first: str
    last: str
    years: float
    max_drawdown: float
    cash_share: float
    switches: int
    episodes: list
    worst: float


def fetch_history(symbol, years=BACKTEST_DEFAULT_YEARS, now=None):
    """Daily (date, close, volume) for `symbol` over the trailing `years`.

    Read-only: it never touches the ticker's CSV, so it can be pointed at a
    long-history proxy to replay the rule. Bars are parsed without requiring
    volume, so an index also works.

    Any bar dated "today" is dropped unconditionally. Unlike `drop_partial_bar`
    this does not try to infer whether the session has closed, because the
    symbol's market is unknown here and replaying a half-finished bar is worse
    than losing one session from a multi-year sample.
    """
    now = now or datetime.now(tz=ZoneInfo(EXCHANGE_TZ))
    start = int((now - timedelta(days=round(years * 365.25))).timestamp())
    url = BACKTEST_URL.format(
        symbol=urllib.parse.quote(symbol), start=start, end=int(now.timestamp())
    )
    payload = _fetch_payload(url, label=symbol)
    if not _is_daily(payload):
        print(
            "warning: %s served at %s granularity; treating bars as daily"
            % (symbol, _payload_granularity(payload)),
            file=sys.stderr,
        )
    prices = _parse_payload(payload, require_volume=False)
    if prices and prices[-1][0] == now.date().isoformat():
        prices = prices[:-1]
    return prices


def backtest_stats(prices, enter=DRAWDOWN_THRESHOLD, exit_=DRAWDOWN_EXIT_THRESHOLD):
    """Replay the switch over a close series and summarise what it did."""
    dates = [day for day, _, _ in prices]
    closes = [close for _, close, _ in prices]
    peaks = trailing_peaks(closes)
    drawdowns = [close / peak - 1.0 if peak else 0.0 for close, peak in zip(closes, peaks)]
    modes = drawdown_modes(drawdowns, enter, exit_)

    episodes = []
    index = 0
    while index < len(modes):
        if modes[index] != "cash":
            index += 1
            continue
        end = index
        while end + 1 < len(modes) and modes[end + 1] == "cash":
            end += 1
        episodes.append(
            BacktestEpisode(
                start=dates[index],
                end=dates[end],
                position=index,
                readings=end - index + 1,
                entry=drawdowns[index],
                exit=drawdowns[end],
            )
        )
        index = end + 1

    switches = sum(1 for i in range(1, len(modes)) if modes[i] != modes[i - 1])
    return BacktestStats(
        bars=len(closes),
        first=dates[0] if dates else "",
        last=dates[-1] if dates else "",
        years=len(closes) / TRADING_DAYS_PER_YEAR,
        max_drawdown=min(drawdowns) if drawdowns else 0.0,
        cash_share=modes.count("cash") / len(modes) if modes else 0.0,
        switches=switches,
        episodes=episodes,
        worst=max((episode.readings for episode in episodes), default=0)
        / TRADING_DAYS_PER_YEAR,
    )


def cmd_backtest(symbol, years=BACKTEST_DEFAULT_YEARS, annual_spend=None, sweep=False):
    if years <= 0:
        print("error: --years must be a positive integer", file=sys.stderr)
        return 2
    if annual_spend is not None and annual_spend <= 0:
        print("error: --annual-spend must be a positive number", file=sys.stderr)
        return 2
    try:
        prices = fetch_history(symbol, years)
    except FetchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if len(prices) < 2:
        print("error: no usable history for %s" % symbol, file=sys.stderr)
        return 1

    stats = backtest_stats(prices)
    print(
        "Backtest: %s - %s -> %s (%d closes, %.1f years)\n"
        % (symbol, stats.first, stats.last, stats.bars, stats.years)
    )
    print("  index max drawdown        %6.1f%%" % (stats.max_drawdown * 100))
    print("  time in cash              %6.1f%%" % (stats.cash_share * 100))
    print(
        "  switches                  %6d  (%.2f/yr)"
        % (stats.switches, stats.switches / stats.years)
    )
    print("  cash episodes             %6d" % len(stats.episodes))
    if stats.episodes:
        print("\n  start        end           years   dd entry   dd exit")
        for episode in stats.episodes:
            print(
                "  %s  %s   %5.2f    %7.1f%%   %7.1f%%"
                % (
                    episode.start,
                    episode.end,
                    episode.readings / TRADING_DAYS_PER_YEAR,
                    episode.entry * 100,
                    episode.exit * 100,
                )
            )
        print("\n  worst episode %.2f years of spending" % stats.worst)
        if annual_spend is not None:
            print(
                "  at %s/yr that is %s"
                % (
                    format(annual_spend, ",.0f"),
                    format(annual_spend * stats.worst, ",.0f"),
                )
            )
    print(
        "\n  rule: switch to cash at -%.0f%%, re-arm at -%.0f%%"
        % (DRAWDOWN_THRESHOLD * 100, DRAWDOWN_EXIT_THRESHOLD * 100)
    )
    if any(episode.position < DRAWDOWN_PEAK_WINDOW for episode in stats.episodes):
        print(
            "  note: episodes in the first %d bars ran on a partial peak window"
            % DRAWDOWN_PEAK_WINDOW
        )
    if sweep:
        print("\n  worst episode by threshold (years)")
        print("  enter      0%         5%         10%")
        for enter in (0.10, 0.15, 0.20):
            cells = []
            for exit_ in (0.0, 0.05, 0.10):
                cells.append("%.2f" % backtest_stats(prices, enter, exit_).worst)
            print("  %3.0f%%      %-9s  %-9s  %-9s" % (enter * 100, cells[0], cells[1], cells[2]))
    print("\n  (read-only: no readings were written)")
    return 0


def summary_lines(rows):
    latest = rows[-1]
    deltas = delta_series(rows)
    delta = deltas[-1]
    slope = term_slope(latest)
    return [
        "| Metric | Value |",
        "|--------|-------|",
        "| Δσ | %.2f %s |" % (delta, volatility_markdown(volatility_bands(deltas)[-1])),
        "| Current σ (W=%d, ann.) | %.2f%% |" % (WINDOW, float(latest["sigma_annualized"]) * 100),
        "| Baseline μ (N≤%d) | %.2f%% |" % (BASELINE, float(latest["baseline_mean"]) * 100),
        "| Term slope σ%d/σ%d | %s (%s) |"
        % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope)),
        "| Latest volume | %s |" % volume_display(latest, with_units=True),
        "| Δσ min / max / median | %.2f / %.2f / %.2f |"
        % (min(deltas), max(deltas), statistics.median(deltas)),
    ]


def context_lines(rows):
    """Volatility context. Secondary to the switch: it never changes the state."""
    latest = rows[-1]
    deltas = delta_series(rows)
    term = " · ".join(
        "%dd %s" % (window, sigma_display(latest, window)) for window in SIGMA_WINDOWS
    )
    lines = summary_lines(rows)
    lines += [
        "",
        "**Δσ trend** " + sparkline_summary(deltas[-SPARK_WIDTH:]),
        "",
        "**Term structure** (σ ann.): %s" % term,
        "",
        "_Volatility labels are sticky: a band must hold for %d readings before"
        " the label changes._" % VOLATILITY_CONFIRM,
    ]
    return lines


def readings_table(rows, limit=REPORT_READING_ROWS):
    """The decision-relevant columns for the most recent readings, newest first."""
    readings = drawdown_rows(rows)[-limit:]
    changes = daily_change_series(rows)[-len(readings) :]
    bands = volatility_bands(delta_series(rows))[-len(readings) :]
    lines = [
        "| Date | Close (GBP) | Day % | Drawdown | State | Signal | Δσ | Volatility |",
        "|------|------------:|------:|---------:|-------|--------|----:|------------|",
    ]
    for reading, change, band in zip(reversed(readings), reversed(changes), reversed(bands)):
        row = reading.row
        delta = float(row["delta_sigma"])
        day = day_markdown(change)
        lines.append(
            "| %s | %.3f | %s | %.2f%% | %s | %s | %.2f | %s |"
            % (
                row["date"],
                float(row["close"]),
                day,
                reading.drawdown * 100,
                reading.mode,
                reading.signal,
                delta,
                volatility_markdown(band),
            )
        )
    return lines


def build_report_section(rows):
    """The findings spliced into README.md between the report markers."""
    latest = rows[-1]
    lines = [
        "_As of %s · %d readings · %s → %s_"
        % (latest["date"], len(rows), rows[0]["date"], latest["date"]),
        "",
        "## State",
        "",
    ]
    lines += state_lines(rows)
    lines += ["", "## Signal history", ""]
    lines += signal_history_lines(rows)
    lines += ["", "## Drawdown", ""]
    lines += drawdown_lines(rows)
    lines += ["", "## Volatility context", ""]
    lines += context_lines(rows)
    lines += ["", "## Recent readings", ""]
    lines += volatility_legend_lines()
    lines += [""]
    lines += readings_table(rows)
    lines += [
        "",
        "_Last %d of %d readings. Full series: [data/delta_sigma.csv](data/delta_sigma.csv)._"
        % (min(REPORT_READING_ROWS, len(rows)), len(rows)),
        "",
        "_How these are computed: [docs/methodology.md](docs/methodology.md)._",
    ]
    return "\n".join(lines)


def cmd_report():
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python vallmon.py run` first.", file=sys.stderr)
        return 1

    if not README_PATH.exists():
        print("README.md not found at %s" % README_PATH, file=sys.stderr)
        return 1

    text = README_PATH.read_text()
    if REPORT_START not in text or REPORT_END not in text:
        print(
            "README.md is missing the %s / %s markers." % (REPORT_START, REPORT_END),
            file=sys.stderr,
        )
        return 1

    head, remainder = text.split(REPORT_START, 1)
    _, tail = remainder.split(REPORT_END, 1)
    _write_text_atomic(
        README_PATH,
        "%s%s\n%s\n%s" % (head, REPORT_START, build_report_section(rows), REPORT_END + tail),
    )

    print(
        "Updated %s - %d reading%s."
        % (README_PATH.name, len(rows), "" if len(rows) == 1 else "s")
    )
    return 0


def cmd_rebuild(ci=False, reset_switch=False):
    """Recompute the whole series from source prices and overwrite the CSV.

    Unlike `run`, this does not preserve existing rows - it is the manual
    escape hatch when source prices have been revised (see `run`'s drift
    warning). Recorded switch states are the exception: they are a log rather
    than a metric, so they survive a rebuild unless `reset_switch` asks for a
    full re-derivation under the current thresholds.
    """
    try:
        computed = _fetch_computed()
    except FetchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if not computed:
        return _not_enough_history()

    rows_by_date = {row["date"]: _format(row) for row in computed}
    preserved = 0
    if not reset_switch:
        existing = load_rows()
        for day, row in rows_by_date.items():
            prior = existing.get(day)
            if prior and prior.get("mode") in ("equity", "cash"):
                row["mode"] = prior["mode"]
                preserved += 1
    save_rows(rows_by_date)

    rows = sorted_rows(rows_by_date)
    print("Rebuilt %s from scratch - %d readings." % (CSV_PATH.relative_to(ROOT), len(rows)))
    if preserved:
        print(
            "  (kept the recorded switch state for %d reading%s)"
            % (preserved, "" if preserved == 1 else "s")
        )
    print_reading(
        rows[-1], float(rows[-1]["delta_sigma"]), volatility_labels(delta_series(rows))[-1]
    )

    if ci:
        write_step_summary(rows)
    return 0


def write_step_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    latest = rows[-1]
    slope = term_slope(latest)
    with open(path, "a") as handle:
        handle.write("## VALL drawdown monitor\n\n")
        handle.write("\n".join(state_lines(rows)) + "\n\n")
        handle.write("%s\n\n" % reading_headline(latest, volatility_labels(delta_series(rows))[-1]))
        handle.write("%s\n\n" % sparkline_summary(delta_series(rows)[-SPARK_WIDTH:]))
        handle.write(
            "Volatility term structure (σ ann., %s d): %s\n\n"
            % ("/".join(str(w) for w in SIGMA_WINDOWS), term_structure_display(latest))
        )
        handle.write(
            "Term slope σ%d/σ%d: %s (%s)\n"
            % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope))
        )


# --- Entry point ------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="fetch, compute and append the latest reading")
    run_parser.add_argument("--ci", action="store_true", help="also write the GitHub step summary")
    run_parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if stored readings disagree with a fresh recompute",
    )

    history_parser = sub.add_parser("history", help="show recent readings")
    history_parser.add_argument(
        "-n", "--limit", type=int, default=30, help="number of readings (default 30)"
    )

    drawdown_parser = sub.add_parser(
        "drawdown", help="show drawdown from the trailing peak and the switch signal"
    )
    drawdown_parser.add_argument(
        "-n", "--limit", type=int, default=30, help="number of readings (default 30)"
    )
    drawdown_parser.add_argument(
        "--annual-spend",
        type=float,
        default=None,
        help="annual spend used to translate the current cash episode into months of cover",
    )

    backtest_parser = sub.add_parser(
        "backtest", help="replay the switch over a long history (read-only)"
    )
    backtest_parser.add_argument("symbol", help="Yahoo symbol to replay, e.g. VT")
    backtest_parser.add_argument(
        "--years",
        type=int,
        default=BACKTEST_DEFAULT_YEARS,
        help="years of history to fetch (default %d)" % BACKTEST_DEFAULT_YEARS,
    )
    backtest_parser.add_argument(
        "--annual-spend",
        type=float,
        default=None,
        help="annual spend used to translate the worst episode into an amount",
    )
    backtest_parser.add_argument(
        "--sweep", action="store_true", help="also sweep the enter/exit thresholds"
    )

    sub.add_parser("report", help="regenerate the findings section of README.md")

    rebuild_parser = sub.add_parser(
        "rebuild", help="recompute the whole series from source and overwrite the CSV"
    )
    rebuild_parser.add_argument(
        "--ci", action="store_true", help="also write the GitHub step summary"
    )
    rebuild_parser.add_argument(
        "--reset-switch",
        action="store_true",
        help="re-derive the recorded switch state under the current thresholds",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(ci=args.ci, strict=args.strict)
    if args.command == "rebuild":
        return cmd_rebuild(ci=args.ci, reset_switch=args.reset_switch)
    if args.command == "history":
        return cmd_history(args.limit)
    if args.command == "drawdown":
        return cmd_drawdown(args.limit, args.annual_spend)
    if args.command == "backtest":
        return cmd_backtest(args.symbol, args.years, args.annual_spend, args.sweep)
    if args.command == "report":
        return cmd_report()
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
