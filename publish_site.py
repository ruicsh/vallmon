#!/usr/bin/env python3
"""publish_site - publish vallmon's reports into the ruicsh.github.io site.

Reads the daily drawdown monitor (README.md), a portfolio report, the
methodology and backtest notes, and the raw readings CSV. Writes the Astro
page, one structured payload and static assets for /projects/vallmon.

The portfolio report is an input, not an artifact: the publish workflow builds
it into a temporary path with portfolio_report.py and passes it in. Nothing
from this pipeline is committed to vallmon except the data caches.

The site repo is a separate checkout. The publish workflow checks it out with
a token and passes its path:

    python publish_site.py --target ../ruicsh.github.io \
        --portfolio /tmp/PORTFOLIO.md
"""

from __future__ import annotations

import argparse
import collections
import csv
import html
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
README_PATH = ROOT / "README.md"
PORTFOLIO_PATH = ROOT / "reports" / "PORTFOLIO.md"
CSV_PATH = ROOT / "data" / "delta_sigma.csv"
METHODOLOGY_PATH = ROOT / "docs" / "methodology.md"
BACKTEST_PATH = ROOT / "docs" / "backtest.md"
ASSETS_DIR = ROOT / "docs" / "assets"

SITE_DATA_PATH = Path("src") / "data" / "vallmon.json"
SITE_PAGE_PATH = Path("src") / "pages" / "projects" / "vallmon.astro"
SITE_PUBLIC_DIR = Path("public") / "projects" / "vallmon"
PAGE_TEMPLATE_PATH = ROOT / "site" / "vallmon.astro"

REPORT_START = "<!-- REPORT:START -->"
REPORT_END = "<!-- REPORT:END -->"

IMG_RE = re.compile(r"<img[^>]*>")
IMG_ALT_RE = re.compile(r'<img[^>]*alt="([^"]*)"[^>]*>')
CODE_RE = re.compile(r"`([^`]*)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITALIC_RE = re.compile(r"(?<!\w)_([^_]+)_(?!\w)")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
TABLE_LINE_RE = re.compile(r"^\|[\s:|-]+\|$")


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(text: str, heading: str) -> str:
    """Return the body of a ``## heading`` section."""
    match = re.search(r"^## " + re.escape(heading) + r"\s*$", text, re.M)
    if match is None:
        return ""
    start = match.end()
    nxt = re.search(r"^#{1,2} ", text[start:], re.M)
    return text[start : start + nxt.start()] if nxt else text[start:]


def _subblock(text: str, heading: str, level: int = 3) -> str:
    """Return the body of a ``###``/``####`` heading inside a section."""
    marker = "#" * level
    match = re.search(r"^" + marker + r" " + re.escape(heading) + r"\s*$", text, re.M)
    if match is None:
        return ""
    start = match.end()
    nxt = re.search(r"^#{1,%d} " % level, text[start:], re.M)
    return text[start : start + nxt.start()] if nxt else text[start:]


def plain(value: str) -> str:
    """Reduce a report table cell to readable text."""
    value = IMG_RE.sub("", value)
    value = CODE_RE.sub(r"\1", value)
    value = BOLD_RE.sub(r"\1", value)
    value = LINK_RE.sub(r"\1", value)
    value = ITALIC_RE.sub(r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return html.unescape(value)


def _table_rows(block: str) -> list[list[str]]:
    lines = [line.strip() for line in block.splitlines() if line.strip().startswith("|")]
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        if index == 1 and TABLE_LINE_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def table(block: str) -> dict[str, Any]:
    """Parse the first Markdown table in ``block`` into headers/plain rows."""
    rows = _table_rows(block)
    if not rows:
        return {"headers": [], "rows": []}
    headers = [plain(cell) for cell in rows[0]]
    body = [[plain(cell) for cell in row] for row in rows[1:]]
    return {"headers": headers, "rows": body}


def table_records(block: str) -> list[dict[str, str]]:
    parsed = table(block)
    headers = parsed["headers"]
    return [dict(zip(headers, row)) for row in parsed["rows"]]


def direction(cell: str) -> str | None:
    if "arrow-up" in cell or 'alt="gain"' in cell:
        return "up"
    if "arrow-down" in cell or 'alt="loss"' in cell:
        return "down"
    if "arrow-flat" in cell or 'alt="flat"' in cell:
        return "flat"
    return None


def number(value: str) -> float | None:
    match = NUMBER_RE.search(value.replace(",", ""))
    return float(match.group()) if match else None


def md_to_html(text: str, strip_title: bool = False) -> str:
    """Render report prose to HTML and point relative links at the site."""
    import markdown

    rendered = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
    )
    if strip_title:
        rendered = re.sub(r"^<h1>.*?</h1>\s*", "", rendered, count=1, flags=re.S)
    rendered = re.sub(
        r'href="(?:\.\./)?docs/assets/([^"]+)"',
        r'href="/projects/vallmon/assets/\1"',
        rendered,
    )
    csv_href = 'href="/projects/vallmon/data/delta_sigma.csv"'
    rendered = rendered.replace('href="data/delta_sigma.csv"', csv_href)
    rendered = rendered.replace('href="../data/delta_sigma.csv"', csv_href)
    rendered = rendered.replace('href="backtest.md"', 'href="#backtests"')
    rendered = rendered.replace('href="docs/backtest.md"', 'href="#backtests"')
    rendered = rendered.replace('href="methodology.md"', 'href="#method"')
    rendered = rendered.replace('href="docs/methodology.md"', 'href="#method"')
    rendered = rendered.replace(">backtest.md</a>", ">Backtests</a>")
    rendered = rendered.replace(">methodology.md</a>", ">Method</a>")
    return rendered


# ---------------------------------------------------------------------------
# Drawdown monitor
# ---------------------------------------------------------------------------


def _report_block(readme: str) -> str:
    start = readme.find(REPORT_START)
    end = readme.find(REPORT_END)
    if start == -1 or end == -1:
        return readme
    return readme[start + len(REPORT_START) : end]


def parse_series(path: Path) -> list[dict[str, Any]]:
    """Read the append-only CSV and derive a rolling 756-reading drawdown."""
    series: list[dict[str, Any]] = []
    closes: collections.deque[float] = collections.deque(maxlen=756)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            close = number(row.get("close", ""))
            if close is None:
                continue
            closes.append(close)
            peak = max(closes)
            delta_sigma = number(row.get("delta_sigma", ""))
            series.append(
                {
                    "date": row["date"],
                    "close": close,
                    "drawdown": round((close / peak - 1) * 100, 2),
                    "delta_sigma": delta_sigma,
                    "mode": row.get("mode", ""),
                }
            )
    return series


def parse_monitor(readme: str, series: list[dict[str, Any]]) -> dict[str, Any]:
    block = _report_block(readme)

    as_of = re.search(
        r"_As of ([\d-]+) · (\d+) readings · ([\d-]+) → ([\d-]+)_", block
    )
    state = re.search(
        r"\*\*(\w+)\*\* — drawdown ([+-]?[\d.]+)%, unchanged for (\d+) readings "
        r"\(since ([\d-]+)\)\.",
        block,
    )
    state_body = _block(block, "State")
    action = ""
    for line in state_body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("**") or stripped.startswith(">"):
            continue
        action = plain(stripped)
        break
    window_match = re.search(r"^> Window: (.+)$", block, re.M)

    drawdown_rows = {
        row["Metric"]: row["Value"] for row in table_records(_block(block, "Drawdown"))
    }

    volatility_rows = {
        row["Metric"]: row["Value"]
        for row in table_records(_block(block, "Volatility context"))
    }
    delta_cell = _raw_metric(_block(block, "Volatility context"), "Δσ")
    delta_sigma = number(volatility_rows.get("Δσ", ""))
    vol_label = None
    match = IMG_ALT_RE.search(delta_cell)
    if match:
        vol_label = match.group(1)

    peak_match = re.search(r"([\d.]+) \(([\d-]+)\)", drawdown_rows.get("Peak (trailing 756d)", ""))
    sigma_match = re.search(r"([\d.]+)%", volatility_rows.get("Current σ (W=20, ann.)", ""))
    baseline_match = re.search(r"([\d.]+)%", volatility_rows.get("Baseline μ (N≤90)", ""))
    slope_match = re.search(
        r"([\d.]+) \((\w+)\)", volatility_rows.get("Term slope σ10/σ30", "")
    )
    ds_stats = [
        number(part)
        for part in re.split(r"[/·]", volatility_rows.get("Δσ min / max / median", ""))
    ]

    trend = re.search(
        r"\*\*Δσ trend\*\* `([^`]*)` ([\d.]+) → ([\d.]+) \(range ([^,]+), (\d+) readings\)",
        block,
    )
    terms = re.findall(r"(\d+)d ([\d.]+%|—)", block)

    recent: list[dict[str, Any]] = []
    for row in _table_rows(_block(block, "Recent readings")):
        if len(row) < 8 or row[0] == "Date":
            continue
        volatility_match = IMG_ALT_RE.search(row[7])
        recent.append(
            {
                "date": plain(row[0]),
                "close": number(row[1]),
                "day": number(row[2]),
                "direction": direction(row[2]),
                "drawdown": number(row[3]),
                "state": plain(row[4]),
                "signal": plain(row[5]),
                "delta_sigma": number(row[6]),
                "volatility": volatility_match.group(1) if volatility_match else None,
            }
        )

    return {
        "as_of": as_of.group(1) if as_of else None,
        "readings": int(as_of.group(2)) if as_of else None,
        "range_start": as_of.group(3) if as_of else None,
        "range_end": as_of.group(4) if as_of else None,
        "state": state.group(1) if state else None,
        "drawdown": float(state.group(2)) if state else None,
        "unchanged_for": int(state.group(3)) if state else None,
        "since": state.group(4) if state else None,
        "action": action,
        "window": window_match.group(1).strip() if window_match else "",
        "peak": {"value": float(peak_match.group(1)), "date": peak_match.group(2)}
        if peak_match
        else None,
        "distance": plain(drawdown_rows.get("Distance to switch", "")),
        "history": plain(drawdown_rows.get("History seen", "")),
        "delta_sigma": delta_sigma,
        "volatility": vol_label,
        "sigma": float(sigma_match.group(1)) if sigma_match else None,
        "baseline": float(baseline_match.group(1)) if baseline_match else None,
        "slope": float(slope_match.group(1)) if slope_match else None,
        "slope_label": slope_match.group(2) if slope_match else None,
        "volume": plain(volatility_rows.get("Latest volume", "")),
        "delta_sigma_min": ds_stats[0] if len(ds_stats) > 0 else None,
        "delta_sigma_max": ds_stats[1] if len(ds_stats) > 1 else None,
        "delta_sigma_median": ds_stats[2] if len(ds_stats) > 2 else None,
        "trend": {
            "spark": trend.group(1) if trend else "",
            "from": float(trend.group(2)) if trend else None,
            "to": float(trend.group(3)) if trend else None,
            "range": trend.group(4) if trend else None,
            "readings": int(trend.group(5)) if trend else None,
        },
        "terms": [
            {"window": int(window), "sigma": float(value.rstrip("%")) if value != "—" else None}
            for window, value in terms
        ],
        "signal_history_html": md_to_html(_block(block, "Signal history")),
        "recent": recent,
        "series": series,
    }


def _raw_metric(block: str, metric: str) -> str:
    for row in _table_rows(block):
        if len(row) >= 2 and plain(row[0]) == metric:
            return row[1]
    return ""


# ---------------------------------------------------------------------------
# Portfolio report
# ---------------------------------------------------------------------------


def _portfolio_header(text: str) -> dict[str, str | None]:
    snapshot = re.search(
        r"\*\*Snapshot:\*\* (.*?) · \*\*Holdings as of:\*\* (.*?) · \*\*Fund:\*\* (.*)",
        text,
    )
    if snapshot is None:
        return {"snapshot": None, "holdings_as_of": None, "fund": None}
    return {
        "snapshot": plain(snapshot.group(1)),
        "holdings_as_of": plain(snapshot.group(2)),
        "fund": plain(snapshot.group(3)),
    }


def parse_portfolio(text: str) -> dict[str, Any]:
    header = _portfolio_header(text)
    at_a_glance = table_records(_block(text, "At a glance"))

    largest = _simple_stock_table(_block(text, "Largest holdings"))
    by_cap_block = _block(text, "Largest holdings by market cap")
    by_cap = {
        "Large": _simple_stock_table(_subblock(by_cap_block, "Large cap")),
        "Mid": _simple_stock_table(_subblock(by_cap_block, "Mid cap")),
        "Small": _simple_stock_table(_subblock(by_cap_block, "Small cap")),
    }

    moves_block = _block(text, "Largest moves")
    moves_up = _simple_stock_table(_subblock(moves_block, "Up"))
    moves_down = _simple_stock_table(_subblock(moves_block, "Down"))

    return {
        **header,
        "title": plain(text.splitlines()[0].lstrip("# ").strip()) if text else "",
        "at_a_glance": at_a_glance,
        "holdings_changes_html": md_to_html(_block(text, "Holdings changes")),
        "region": table(_block(text, "By region")),
        "sector": table(_block(text, "By sector")),
        "market_cap": table(_block(text, "By market cap")),
        "region_market_cap": table(_block(text, "Region × market cap")),
        "region_sector": table(_block(text, "Region × sector")),
        "largest": largest,
        "by_cap": by_cap,
        "moves_up": moves_up,
        "moves_down": moves_down,
        "notes_html": md_to_html(_block(text, "Notes and method")),
    }


def _simple_stock_table(block: str) -> list[dict[str, Any]]:
    rows = _table_rows(block)
    if not rows:
        return []
    headers = [plain(header) for header in rows[0]]
    records = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        record: dict[str, Any] = {header: plain(cell) for header, cell in zip(headers, row)}
        record["stock"] = plain(row[0])
        record["ticker"] = plain(row[1])
        day_cell = row[-1]
        record["day"] = number(day_cell)
        record["direction"] = direction(day_cell)
        records.append(record)
    return records


def parse_holdings(text: str) -> list[dict[str, Any]]:
    """Parse the full region -> sector -> line hierarchy."""
    body = _block(text, "Holdings by region")
    regions: list[dict[str, Any]] = []

    region_matches = list(re.finditer(r"^### (.+)$", body, re.M))
    for index, region_match in enumerate(region_matches):
        region_end = (
            region_matches[index + 1].start()
            if index + 1 < len(region_matches)
            else len(body)
        )
        region_chunk = body[region_match.end() : region_end]
        region: dict[str, Any] = {"name": plain(region_match.group(1)), "sectors": []}

        sector_matches = list(re.finditer(r"^#### (.+)$", region_chunk, re.M))
        for sector_index, sector_match in enumerate(sector_matches):
            sector_end = (
                sector_matches[sector_index + 1].start()
                if sector_index + 1 < len(sector_matches)
                else len(region_chunk)
            )
            sector_chunk = region_chunk[sector_match.end() : sector_end]
            summary = ""
            for line in sector_chunk.splitlines():
                if line.strip().startswith("_") and line.strip().endswith("_"):
                    summary = plain(line.strip())
                    break

            rows: list[dict[str, Any]] = []
            for row in _table_rows(sector_chunk):
                if len(row) < 6 or row[0] == "Stock":
                    continue
                day_cell = row[5]
                rows.append(
                    {
                        "stock": plain(row[0]),
                        "ticker": plain(row[1]),
                        "country": plain(row[2]),
                        "cap": plain(row[3]),
                        "weight": plain(row[4]),
                        "weight_value": number(row[4]) or 0.0,
                        "day": number(day_cell),
                        "direction": direction(day_cell),
                    }
                )

            region["sectors"].append(
                {
                    "name": plain(sector_match.group(1)),
                    "summary": summary,
                    "rows": rows,
                }
            )

        regions.append(region)

    return regions


# ---------------------------------------------------------------------------
# Payload + output
# ---------------------------------------------------------------------------


def build_payload(portfolio_path: Path = PORTFOLIO_PATH) -> tuple[dict[str, Any], dict[str, Any]]:
    readme = _read(README_PATH)
    portfolio_text = _read(portfolio_path)
    series = parse_series(CSV_PATH)
    holdings = parse_holdings(portfolio_text)
    monitor = parse_monitor(readme, series)
    portfolio = parse_portfolio(portfolio_text)

    if not monitor["state"]:
        raise SystemExit("could not parse the monitor state from README.md")
    if not portfolio["region"]["rows"]:
        raise SystemExit(f"could not parse the portfolio region table from {portfolio_path}")

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "monitor": monitor,
        "portfolio": portfolio,
        "methodology_html": md_to_html(_read(METHODOLOGY_PATH), strip_title=True),
        "backtests_html": md_to_html(_read(BACKTEST_PATH), strip_title=True),
    }
    holdings_payload = {
        "generated_at": payload["generated_at"],
        "regions": holdings,
    }
    return payload, holdings_payload


def write_outputs(target: Path, payload: dict[str, Any], holdings: dict[str, Any]) -> None:
    target = target.resolve()
    if not (target / "astro.config.mjs").is_file():
        raise SystemExit(f"{target} does not look like the Astro site repository")

    data_path = target / SITE_DATA_PATH
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    page_path = target / SITE_PAGE_PATH
    page_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PAGE_TEMPLATE_PATH, page_path)

    public = target / SITE_PUBLIC_DIR
    stale_reports = public / "reports"
    if stale_reports.exists():
        shutil.rmtree(stale_reports)

    assets = public / "assets"
    data_dir = public / "data"
    for directory in (assets, data_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for asset in sorted(ASSETS_DIR.glob("*.svg")):
        shutil.copy2(asset, assets / asset.name)
    shutil.copy2(CSV_PATH, data_dir / "delta_sigma.csv")

    (public / "holdings.json").write_text(
        json.dumps(holdings, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    lines = sum(
        len(sector["rows"])
        for region in holdings["regions"]
        for sector in region["sectors"]
    )
    print(
        f"Published {lines:,} holdings, {len(payload['monitor']['series']):,} readings "
        f"to {data_path}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="path to the ruicsh.github.io checkout",
    )
    parser.add_argument(
        "--portfolio",
        type=Path,
        default=PORTFOLIO_PATH,
        help="portfolio report input (default: reports/PORTFOLIO.md)",
    )
    args = parser.parse_args(argv)
    if not args.portfolio.is_file():
        raise SystemExit(f"portfolio report is missing: {args.portfolio}")
    if not PAGE_TEMPLATE_PATH.is_file():
        raise SystemExit(f"site page template is missing: {PAGE_TEMPLATE_PATH}")
    payload, holdings = build_payload(args.portfolio)
    write_outputs(args.target, payload, holdings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
