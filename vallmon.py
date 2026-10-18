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
    report    regenerate the "Current reading" section of README.md
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
import urllib.request
from datetime import datetime
from pathlib import Path
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

# Every term-structure window except the primary one. This is the single source
# of truth for both the persisted CSV columns and the per-row computation, so
# the two can never drift apart.
SUPPLEMENTARY_WINDOWS = [w for w in SIGMA_WINDOWS if w != WINDOW]

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "delta_sigma.csv"
README_PATH = ROOT / "README.md"
REPORT_PATH = ROOT / "REPORT.md"

REPORT_START = "<!-- REPORT:START -->"
REPORT_END = "<!-- REPORT:END -->"

SPARK_WIDTH = 60   # sparkline length in the full report
README_SPARK = 30  # sparkline length in the README summary

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
] + TERM_FIELDS

# Fields recomputed from source on every reading. A stored value that disagrees
# with a fresh recompute is reported as drift (see find_drift).
METRIC_FIELDS = ["sigma_annualized", "baseline_mean", "delta_sigma"] + TERM_FIELDS

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    "?range=1y&interval=1d"
)
USER_AGENT = os.environ.get("VALLMON_USER_AGENT", "Mozilla/5.0 (compatible; vallmon/1.0)")

FETCH_ATTEMPTS = 3    # total tries per fetch (1 initial + retries)
FETCH_BACKOFF = 2.0   # seconds before the first retry; doubles each retry

# A stored reading that disagrees with a fresh recompute by more than this
# (in delta_sigma units) is reported as drift - see cmd_run / cmd_rebuild.
DRIFT_TOLERANCE = 1e-4

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# --- Data fetching ----------------------------------------------------------

class FetchError(RuntimeError):
    """Raised when price data cannot be fetched, retrieved or parsed."""


def _parse_payload(payload):
    """Turn a Yahoo Finance chart payload into a sorted [(iso_date, close, volume)] list.

    Volume is the number of ETF units traded that session on the exchange.
    Rows with a null close or zero volume are discarded. Yahoo emits a couple
    of zero-volume placeholder bars at the start of a new listing which would
    otherwise corrupt the returns series. Malformed payloads raise FetchError
    rather than surfacing as a raw KeyError/IndexError.
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

    if not len(timestamps) == len(closes) == len(volumes):
        raise FetchError(
            "Yahoo Finance returned ragged arrays "
            "(timestamps=%d, closes=%d, volumes=%d)"
            % (len(timestamps), len(closes), len(volumes))
        )

    tz = ZoneInfo(EXCHANGE_TZ)
    prices = []
    for ts, close, volume in zip(timestamps, closes, volumes):
        if close is None or volume is None or volume == 0:
            continue
        day = datetime.fromtimestamp(ts, tz=tz).date().isoformat()
        prices.append((day, float(close), int(volume)))

    prices.sort(key=lambda row: row[0])
    return prices


def fetch_prices(attempts=FETCH_ATTEMPTS, backoff=FETCH_BACKOFF):
    """Fetch and parse VALL.L daily closes and volumes, retrying transient failures.

    Network/timeout/parse errors are retried with exponential backoff; a
    response that Yahoo explicitly flags as an error is raised immediately.
    """
    url = YAHOO_URL.format(ticker=TICKER)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            return _parse_payload(payload)
        except FetchError:
            raise
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
        "failed to fetch %s after %d attempts: %s" % (TICKER, attempts, last_error)
    )


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
    return rows


def regime(delta):
    if delta >= 1.5:
        return "\U0001F534 elevated"      # red circle
    if delta >= 1.15:
        return "\U0001F7E0 expanding"     # orange circle
    if delta >= 0.85:
        return "\U0001F7E2 normal"        # green circle
    if delta >= 0.7:
        return "\U0001F535 compressing"   # blue circle
    return "\u26AA compressed"            # white circle


def sparkline(values):
    if not values:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return SPARK_CHARS[3] * len(values)
    scale = (len(SPARK_CHARS) - 1) / (high - low)
    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((v - low) * scale))] for v in values)


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


def sigma_value(row, window):
    """Realized vol for `window` as a float, or None when absent/missing."""
    raw = row.get(sigma_field(window))
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
            raw = prior.get(field)
            if value is None or raw in (None, ""):
                continue
            stored_value = float(raw)
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


def cmd_run(ci=False):
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
        if row_migrated:
            migrated += 1
    save_rows(existing)

    rows = sorted_rows(existing)
    latest = rows[-1]
    delta = float(latest["delta_sigma"])
    print_reading(latest, delta)
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
            "  (backfilled term structure for %d existing reading%s)"
            % (migrated, "" if migrated == 1 else "s")
        )

    if ci:
        write_step_summary(rows)
    return 0


def print_reading(row, delta):
    slope = term_slope(row)
    print("VALL Rolling Variance Delta - %s" % row["date"])
    print("  sigma current (W=%d, ann.)   %6.2f%%" % (WINDOW, float(row["sigma_annualized"]) * 100))
    print("  baseline mean  (N<=%d)       %6.2f%%" % (BASELINE, float(row["baseline_mean"]) * 100))
    print("  volume (units, daily)       %12s" % volume_display(row))
    print("  term structure (%s)   %s"
          % ("/".join(str(w) for w in SIGMA_WINDOWS), term_structure_display(row)))
    print("  term slope (s%d/s%d)          %6s   -> %s"
          % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope)))
    print("  delta_sigma                  %6.2f   -> %s" % (delta, regime(delta)))


def cmd_history(limit):
    if limit <= 0:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python vallmon.py run` first.")
        return 0
    recent = rows[-limit:]
    print("VALL Rolling Variance Delta - last %d readings\n" % len(recent))
    print("  date         close       volume    sigma    base     d-sigma  regime")
    for row in recent:
        delta = float(row["delta_sigma"])
        print(
            "  %s  %6.3f  %12s  %6.2f%%  %6.2f%%  %6.2f   %s"
            % (
                row["date"],
                float(row["close"]),
                volume_display(row),
                float(row["sigma_annualized"]) * 100,
                float(row["baseline_mean"]) * 100,
                delta,
                regime(delta),
            )
        )
    deltas = [float(row["delta_sigma"]) for row in recent]
    print("\n  trend  %s" % sparkline(deltas))
    print("         %.2f -> %.2f" % (deltas[0], deltas[-1]))
    return 0


def summary_lines(rows):
    latest = rows[-1]
    deltas = [float(row["delta_sigma"]) for row in rows]
    delta = deltas[-1]
    slope = term_slope(latest)
    return [
        "| Metric | Value |",
        "|--------|-------|",
        "| Latest Δσ | %.2f %s (%s) |" % (delta, regime(delta), latest["date"]),
        "| Current σ (W=%d, ann.) | %.2f%% |" % (WINDOW, float(latest["sigma_annualized"]) * 100),
        "| Baseline μ (N≤%d) | %.2f%% |" % (BASELINE, float(latest["baseline_mean"]) * 100),
        "| Term slope σ%d/σ%d | %s (%s) |"
        % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope)),
        "| Latest volume | %s |" % volume_display(latest, with_units=True),
        "| Δσ min / max / median | %.2f / %.2f / %.2f |"
        % (min(deltas), max(deltas), statistics.median(deltas)),
        "| Readings | %d |" % len(rows),
    ]


def term_structure_lines(rows):
    """Latest-day σ per window, the headline slope and its sparkline."""
    latest = rows[-1]
    lines = ["| Window | σ (ann.) |", "|--------|----------|"]
    for window in SIGMA_WINDOWS:
        lines.append("| %dd | %s |" % (window, sigma_display(latest, window)))

    slope = term_slope(latest)
    lines += [
        "",
        "**Term slope σ%d/σ%d: %s (%s)**"
        % (TERM_SHORT, TERM_LONG, slope_display(slope), term_slope_label(slope)),
    ]
    slope_series = [s for s in (term_slope(row) for row in rows) if s is not None]
    if slope_series:
        lines += ["", "`%s`" % sparkline(slope_series[-SPARK_WIDTH:])]
    return lines


def history_table(rows):
    lines = [
        "| Date | Close (GBP) | Volume (units) | σ %dd (ann.) | Baseline μ | Δσ | Regime |"
        % WINDOW,
        "|------|-------------|----------------|-----------------|------------|-----|--------|",
    ]
    for row in reversed(rows):
        value = float(row["delta_sigma"])
        lines.append(
            "| %s | %.3f | %s | %.2f%% | %.2f%% | %.2f | %s |"
            % (
                row["date"],
                float(row["close"]),
                volume_display(row),
                float(row["sigma_annualized"]) * 100,
                float(row["baseline_mean"]) * 100,
                value,
                regime(value),
            )
        )
    return lines


def build_readme_section(rows):
    latest = rows[-1]
    deltas = [float(row["delta_sigma"]) for row in rows]
    delta = deltas[-1]
    slope = term_slope(latest)
    trend = " · TS σ%d/σ%d %s" % (TERM_SHORT, TERM_LONG, slope_display(slope))
    return "\n".join(
        [
            "**Δσ = %.2f** %s · %.2f%% vol vs %.2f%% baseline · %s · %s%s"
            % (
                delta,
                regime(delta),
                float(latest["sigma_annualized"]) * 100,
                float(latest["baseline_mean"]) * 100,
                volume_display(latest, with_units=True),
                latest["date"],
                trend,
            ),
            "",
            "`%s` · [Full report →](REPORT.md)" % sparkline(deltas[-README_SPARK:]),
        ]
    )


def build_report(rows):
    latest = rows[-1]
    deltas = [float(row["delta_sigma"]) for row in rows]
    lines = [
        "# VALL Rolling Variance Delta — Full Report",
        "",
        "_As of %s · %d readings · %s → %s_"
        % (latest["date"], len(rows), rows[0]["date"], latest["date"]),
        "",
        "## Summary",
        "",
    ]
    lines += summary_lines(rows)
    lines += [
        "",
        "## Trend",
        "",
        "`%s`" % sparkline(deltas[-SPARK_WIDTH:]),
        "",
        "## Volatility term structure",
        "",
    ]
    lines += term_structure_lines(rows)
    lines += [
        "",
        "## Full history",
        "",
    ]
    lines += history_table(rows)
    lines += ["", "## Methodology", "", "See [README.md → Methodology](README.md#methodology)."]
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
    README_PATH.write_text(
        "%s%s\n%s\n%s" % (head, REPORT_START, build_readme_section(rows), REPORT_END + tail)
    )
    REPORT_PATH.write_text(build_report(rows) + "\n")

    print(
        "Updated %s (concise) and %s (full) - %d readings."
        % (README_PATH.name, REPORT_PATH.name, len(rows))
    )
    return 0


def cmd_rebuild(ci=False):
    """Recompute the whole series from source prices and overwrite the CSV.

    Unlike `run`, this does not preserve existing rows - it is the manual
    escape hatch when source prices have been revised (see `run`'s drift
    warning).
    """
    try:
        computed = _fetch_computed()
    except FetchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if not computed:
        return _not_enough_history()

    rows_by_date = {row["date"]: _format(row) for row in computed}
    save_rows(rows_by_date)

    rows = sorted_rows(rows_by_date)
    print("Rebuilt %s from scratch - %d readings." % (CSV_PATH.relative_to(ROOT), len(rows)))
    print_reading(rows[-1], float(rows[-1]["delta_sigma"]))

    if ci:
        write_step_summary(rows)
    return 0


def write_step_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    latest = rows[-1]
    deltas = [float(row["delta_sigma"]) for row in rows]
    delta = deltas[-1]
    slope = term_slope(latest)
    with open(path, "a") as handle:
        handle.write("## VALL Rolling Variance Delta\n\n")
        handle.write(
            "**Δσ = %.2f** %s · %.2f%% vol vs %.2f%% baseline · %s · %s\n\n"
            % (
                delta,
                regime(delta),
                float(latest["sigma_annualized"]) * 100,
                float(latest["baseline_mean"]) * 100,
                volume_display(latest, with_units=True),
                latest["date"],
            )
        )
        handle.write("`%s`\n\n" % sparkline(deltas[-SPARK_WIDTH:]))
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

    history_parser = sub.add_parser("history", help="show recent readings")
    history_parser.add_argument(
        "-n", "--limit", type=int, default=30, help="number of readings (default 30)"
    )

    sub.add_parser("report", help="regenerate the README report section")

    rebuild_parser = sub.add_parser(
        "rebuild", help="recompute the whole series from source and overwrite the CSV"
    )
    rebuild_parser.add_argument(
        "--ci", action="store_true", help="also write the GitHub step summary"
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(ci=args.ci)
    if args.command == "rebuild":
        return cmd_rebuild(ci=args.ci)
    if args.command == "history":
        return cmd_history(args.limit)
    if args.command == "report":
        return cmd_report()
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
