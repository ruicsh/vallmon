#!/usr/bin/env python3
"""vallmon - VALL Rolling Variance Delta (delta-sigma) monitor.

Fetches daily closes for VALL - the Vanguard FTSE Global All-Cap UCITS ETF
USD Acc (London Stock Exchange: VALL.L, ISIN IE000VAHT5T0) - and computes the
Rolling Variance Delta:

    delta_sigma = sigma_current / mu_sigma(t - N)

where `sigma_current` is annualized realized volatility over a short window W
and `mu_sigma` is the trailing mean of that volatility profile over an adaptive
N-day baseline.

Commands:
    run       fetch, compute, backfill/append to data/delta_sigma.csv, print
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

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "delta_sigma.csv"
README_PATH = ROOT / "README.md"
REPORT_PATH = ROOT / "REPORT.md"

REPORT_START = "<!-- REPORT:START -->"
REPORT_END = "<!-- REPORT:END -->"

SPARK_WIDTH = 60   # sparkline length in the full report
README_SPARK = 30  # sparkline length in the README summary

CSV_FIELDS = ["date", "close", "sigma_annualized", "baseline_mean", "delta_sigma"]

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    "?range=1y&interval=1d"
)
USER_AGENT = "Mozilla/5.0 (compatible; vallmon/1.0; +https://github.com)"

SPARK_CHARS = "▁▂▃▄▅▆▇█"


# --- Data fetching ----------------------------------------------------------

def fetch_prices():
    """Return a sorted list of (iso_date, close) for real trading sessions.

    Rows with a null close or zero volume are discarded. Yahoo emits a couple
    of zero-volume placeholder bars at the start of a new listing which would
    otherwise corrupt the returns series.
    """
    url = YAHOO_URL.format(ticker=TICKER)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    result = payload["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes = quote["close"]
    volumes = quote["volume"]

    tz = ZoneInfo(EXCHANGE_TZ)
    prices = []
    for ts, close, volume in zip(timestamps, closes, volumes):
        if close is None or volume is None or volume == 0:
            continue
        day = datetime.fromtimestamp(ts, tz=tz).date().isoformat()
        prices.append((day, float(close)))

    prices.sort(key=lambda row: row[0])
    return prices


# --- Metric -----------------------------------------------------------------

def compute_rows(prices):
    """Compute the full delta-sigma series from a list of (date, close).

    Returns a list of dicts with float values, oldest first.
    """
    if len(prices) < 2:
        return []

    days = [day for day, _ in prices]
    closes = [close for _, close in prices]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    # Annualized realized volatility at each point we have a full window.
    sigmas = []  # (index_into_prices, sigma)
    for j in range(WINDOW - 1, len(returns)):
        window = returns[j - WINDOW + 1 : j + 1]
        sigma = statistics.stdev(window) * math.sqrt(TRADING_DAYS_PER_YEAR)
        sigmas.append((j + 1, sigma))

    rows = []
    for k in range(1, len(sigmas)):
        price_idx, sigma = sigmas[k]
        prior = [value for _, value in sigmas[:k]]
        n_base = min(BASELINE, len(prior))
        baseline = statistics.fmean(prior[-n_base:])
        if baseline <= 0:
            continue
        rows.append(
            {
                "date": days[price_idx],
                "close": closes[price_idx],
                "sigma_annualized": sigma,
                "baseline_mean": baseline,
                "delta_sigma": sigma / baseline,
            }
        )
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
    return {
        "date": row["date"],
        "close": "%.4f" % row["close"],
        "sigma_annualized": "%.6f" % row["sigma_annualized"],
        "baseline_mean": "%.6f" % row["baseline_mean"],
        "delta_sigma": "%.4f" % row["delta_sigma"],
    }


def save_rows(rows_by_date):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for day in sorted(rows_by_date):
            writer.writerow({field: rows_by_date[day][field] for field in CSV_FIELDS})


def sorted_rows(rows_by_date=None):
    rows = (rows_by_date if rows_by_date is not None else load_rows()).values()
    return sorted(rows, key=lambda row: row["date"])


# --- Commands ---------------------------------------------------------------

def cmd_run(ci=False):
    prices = fetch_prices()
    if not prices:
        print("No price data returned.", file=sys.stderr)
        return 1

    computed = compute_rows(prices)
    if not computed:
        print(
            "Not enough history yet to compute a reading "
            "(need %d+ trading days)." % (WINDOW + 1)
        )
        return 0

    existing = load_rows()
    added = 0
    for row in computed:
        if row["date"] not in existing:
            existing[row["date"]] = _format(row)
            added += 1
    save_rows(existing)

    rows = sorted_rows(existing)
    latest = rows[-1]
    delta = float(latest["delta_sigma"])
    print_reading(latest, delta)
    print("  (%d new reading%s written to %s)" % (added, "" if added == 1 else "s", CSV_PATH.relative_to(ROOT)))

    if ci:
        write_step_summary(rows)
    return 0


def print_reading(row, delta):
    print("VALL Rolling Variance Delta - %s" % row["date"])
    print("  sigma current (W=%d, ann.)   %6.2f%%" % (WINDOW, float(row["sigma_annualized"]) * 100))
    print("  baseline mean  (N<=%d)       %6.2f%%" % (BASELINE, float(row["baseline_mean"]) * 100))
    print("  delta_sigma                  %6.2f   -> %s" % (delta, regime(delta)))


def cmd_history(limit):
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python volmon.py run` first.")
        return 0
    recent = rows[-limit:]
    print("VALL Rolling Variance Delta - last %d readings\n" % len(recent))
    print("  date         close    sigma    base     d-sigma  regime")
    for row in recent:
        delta = float(row["delta_sigma"])
        print(
            "  %s  %6.3f  %6.2f%%  %6.2f%%  %6.2f   %s"
            % (
                row["date"],
                float(row["close"]),
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
    return [
        "| Metric | Value |",
        "|--------|-------|",
        "| Latest Δσ | %.2f %s (%s) |" % (delta, regime(delta), latest["date"]),
        "| Current σ (W=%d, ann.) | %.2f%% |" % (WINDOW, float(latest["sigma_annualized"]) * 100),
        "| Baseline μ (N≤%d) | %.2f%% |" % (BASELINE, float(latest["baseline_mean"]) * 100),
        "| Δσ min / max / median | %.2f / %.2f / %.2f |"
        % (min(deltas), max(deltas), statistics.median(deltas)),
        "| Readings | %d |" % len(rows),
    ]


def history_table(rows):
    lines = [
        "| Date | Close (GBp) | σ current (ann.) | Baseline μ | Δσ | Regime |",
        "|------|-------------|------------------|------------|-----|--------|",
    ]
    for row in reversed(rows):
        value = float(row["delta_sigma"])
        lines.append(
            "| %s | %.3f | %.2f%% | %.2f%% | %.2f | %s |"
            % (
                row["date"],
                float(row["close"]),
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
    return "\n".join(
        [
            "**Δσ = %.2f** %s · %.2f%% vol vs %.2f%% baseline · %s"
            % (
                delta,
                regime(delta),
                float(latest["sigma_annualized"]) * 100,
                float(latest["baseline_mean"]) * 100,
                latest["date"],
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
        "## Full history",
        "",
    ]
    lines += history_table(rows)
    lines += ["", "## Methodology", "", "See [README.md → Methodology](README.md#methodology)."]
    return "\n".join(lines)


def cmd_report():
    rows = sorted_rows()
    if not rows:
        print("No readings yet; run `python volmon.py run` first.", file=sys.stderr)
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


def write_step_summary(rows):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    latest = rows[-1]
    deltas = [float(row["delta_sigma"]) for row in rows]
    delta = deltas[-1]
    with open(path, "a") as handle:
        handle.write("## VALL Rolling Variance Delta\n\n")
        handle.write(
            "**Δσ = %.2f** %s · %.2f%% vol vs %.2f%% baseline · %s\n\n"
            % (
                delta,
                regime(delta),
                float(latest["sigma_annualized"]) * 100,
                float(latest["baseline_mean"]) * 100,
                latest["date"],
            )
        )
        handle.write("`%s`\n" % sparkline(deltas[-SPARK_WIDTH:]))


# --- Entry point ------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="fetch, compute and append the latest reading")
    run_parser.add_argument("--ci", action="store_true", help="also write the GitHub step summary")

    history_parser = sub.add_parser("history", help="show recent readings")
    history_parser.add_argument("-n", "--limit", type=int, default=30, help="number of readings (default 30)")

    sub.add_parser("report", help="regenerate the README report section")

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(ci=args.ci)
    if args.command == "history":
        return cmd_history(args.limit)
    if args.command == "report":
        return cmd_report()
    parser.error("unknown command")


if __name__ == "__main__":
    sys.exit(main())
