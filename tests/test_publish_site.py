from __future__ import annotations

import json

from publish_site import (
    _report_block,
    md_to_html,
    parse_holdings,
    parse_series,
    plain,
    table,
)

MONITOR_FIXTURE = """
<!-- REPORT:START -->
_As of 2026-11-03 · 2 readings · 2026-11-02 → 2026-11-03_

## State

**Equity** — drawdown -4.52%, unchanged for 2 readings (since 2026-11-02).

No action. Switch to cash at -15% — a further 10.5 pp.

> Window: 2 of 756 readings. Provisional.

## Drawdown

| Metric | Value |
|--------|-------|
| Peak (trailing 756d) | 3.7925 (2026-10-06) |
| Distance to switch | 10.5 pp |
<!-- REPORT:END -->
"""

PORTFOLIO_FIXTURE = """
# VALL portfolio — daily moves

**Snapshot:** 2026-11-03 21:16 UTC · **Holdings as of:** 2026-09-30 · **Fund:** VALL.L

## At a glance

| Metric | Value |
|--------|-------|
| Equity lines | 2 |
| Equity weight | 1.00% of fund |

## Holdings changes

Compared with the previous snapshot, also as of **2026-09-30**.

**0 added · 0 removed.**

## By region

| Region | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |
|--------|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|
| North America | 2 | 1.00% | 2 | 1 | 1 | 0 | +0.35% | +0.35% |
| **Total** | **2** | **1.00%** | **2** | **1** | **1** | **0** | **+0.35%** | **+0.35%** |

## By sector

| Sector | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |
|--------|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|
| Technology | 2 | 1.00% | 2 | 1 | 1 | 0 | +0.35% | +0.35% |
| **Total** | **2** | **1.00%** | **2** | **1** | **1** | **0** | **+0.35%** | **+0.35%** |

## By market cap

| Cap | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |
|-----|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|
| Large | 2 | 1.00% | 2 | 1 | 1 | 0 | +0.35% | +0.35% |
| **Total** | **2** | **1.00%** | **2** | **1** | **1** | **0** | **+0.35%** | **+0.35%** |

## Region × market cap

| Region | Large | Total |
|--------|------:|------:|
| North America | +0.35% | **+0.35%** |

## Region × sector

| Region | Technology | Total |
|--------|------:|------:|
| North America | +0.35% | **+0.35%** |

## Largest holdings

| Stock | Ticker | Cap | Region | Sector | Weight | Day % |
|-------|--------|-----|--------|--------|-------:|------:|
| Alpha Corp | `AAA` | Large | North America | Technology | 0.60% | +1.20% <img alt="gain"> |
| Beta Ltd | `BBB` | Small | North America | Technology | 0.40% | -0.50% <img alt="loss"> |

## Largest holdings by market cap

### Large cap

| Stock | Ticker | Region | Sector | Weight | Day % |
|-------|--------|--------|--------|-------:|------:|
| Alpha Corp | `AAA` | North America | Technology | 0.60% | +1.20% <img alt="gain"> |

### Mid cap

| Stock | Ticker | Region | Sector | Weight | Day % |
|-------|--------|--------|--------|-------:|------:|

### Small cap

| Stock | Ticker | Region | Sector | Weight | Day % |
|-------|--------|--------|--------|-------:|------:|
| Beta Ltd | `BBB` | North America | Technology | 0.40% | -0.50% <img alt="loss"> |

## Largest moves

### Up

| Stock | Ticker | Cap | Region | Sector | Weight | Day % |
|-------|--------|-----|--------|--------|-------:|------:|
| Alpha Corp | `AAA` | Large | North America | Technology | 0.60% | +1.20% <img alt="gain"> |

### Down

| Stock | Ticker | Cap | Region | Sector | Weight | Day % |
|-------|--------|-----|--------|--------|-------:|------:|
| Beta Ltd | `BBB` | Small | North America | Technology | 0.40% | -0.50% <img alt="loss"> |

## Notes and method

- **Universe.** Two lines.
- **Not advice.** A one-day move is noise.
"""

HOLDINGS_FIXTURE = """
## Holdings by region

### North America

#### Technology

_2 lines · 1.00% of fund · 1 up / 1 down / 0 flat · median +0.00% · weighted +0.10%_

| Stock | Ticker | Country | Cap | Weight | Day % |
|-------|--------|---------|-----|-------:|------:|
| Alpha Corp | `AAA` | United States | Large | 0.60% | +1.20% <img alt="gain"> |
| Beta Ltd | `BBB` | Canada | Small | <0.001% | -0.50% <img alt="loss"> |

#### Financials

_1 lines · 0.20% of fund · 1 up / 0 down / 0 flat · median +2.00% · weighted +2.00%_

| Stock | Ticker | Country | Cap | Weight | Day % |
|-------|--------|---------|-----|-------:|------:|
| Gamma Bank | `CCC` | United States | Large | 0.20% | +2.00% <img alt="gain"> |

### Europe

#### Technology

_0 lines · 0.00% of fund · 0 up / 0 down / 0 flat · median — · weighted —_

| Stock | Ticker | Country | Cap | Weight | Day % |
|-------|--------|---------|-----|-------:|------:|
"""

PORTFOLIO_FIXTURE += HOLDINGS_FIXTURE


def test_plain_strips_report_markup() -> None:
    assert plain("**Total**") == "Total"
    assert plain("`NVDA`") == "NVDA"
    assert plain("[backtest.md](backtest.md)") == "backtest.md"
    assert plain('_2 lines_') == "2 lines"
    assert (
        plain('+2.29% <img src="../docs/assets/arrow-up.svg" alt="gain" width="12">')
        == "+2.29%"
    )


def test_table_parses_headers_and_rows() -> None:
    parsed = table(
        """
| Metric | Value |
|--------|------:|
| Lines | **6,980** |
| Priced | 6,715 |
"""
    )
    assert parsed["headers"] == ["Metric", "Value"]
    assert parsed["rows"] == [["Lines", "6,980"], ["Priced", "6,715"]]


def test_report_block_extracts_marked_section() -> None:
    assert "_As of 2026-11-03" in _report_block(MONITOR_FIXTURE)
    assert "<!-- REPORT" not in _report_block(MONITOR_FIXTURE)


def test_md_to_html_rewrites_site_links_and_strips_title() -> None:
    rendered = md_to_html(
        "# Methodology\n\nSee [backtest.md](backtest.md) and the "
        "[CSV](data/delta_sigma.csv).\n",
        strip_title=True,
    )
    assert "<h1>" not in rendered
    assert 'href="#backtests"' in rendered
    assert ">Backtests</a>" in rendered
    assert 'href="/projects/vallmon/data/delta_sigma.csv"' in rendered


def test_parse_series_derives_rolling_drawdown(tmp_path) -> None:
    source = tmp_path / "series.csv"
    source.write_text(
        "date,close,volume,sigma_annualized,baseline_mean,delta_sigma,"
        "sigma_10,sigma_30,sigma_60,mode\n"
        "2026-11-01,10.00,1,0.1,0.1,1.0,,,,\n"
        "2026-11-02,12.00,1,0.1,0.1,1.0,,,,\n"
        "2026-11-03,9.00,1,0.1,0.1,1.0,,,,\n",
        encoding="utf-8",
    )
    series = parse_series(source)
    assert [row["date"] for row in series] == [
        "2026-11-01",
        "2026-11-02",
        "2026-11-03",
    ]
    assert series[1]["drawdown"] == 0.0
    assert series[2]["drawdown"] == -25.0
    assert series[0]["delta_sigma"] == 1.0


def test_parse_holdings_walks_region_and_sector() -> None:
    holdings = parse_holdings(HOLDINGS_FIXTURE)
    assert [region["name"] for region in holdings] == ["North America", "Europe"]
    assert [sector["name"] for sector in holdings[0]["sectors"]] == [
        "Technology",
        "Financials",
    ]
    assert "2 lines" in holdings[0]["sectors"][0]["summary"]
    assert holdings[0]["sectors"][0]["rows"][0] == {
        "stock": "Alpha Corp",
        "ticker": "AAA",
        "country": "United States",
        "cap": "Large",
        "weight": "0.60%",
        "weight_value": 0.6,
        "day": 1.2,
        "direction": "up",
    }
    assert holdings[0]["sectors"][0]["rows"][1]["weight_value"] == 0.001
    assert holdings[1]["sectors"][0]["rows"] == []


def test_write_outputs_copies_the_astro_page_and_data(tmp_path) -> None:
    from publish_site import write_outputs

    (tmp_path / "astro.config.mjs").write_text("", encoding="utf-8")
    payload = {"monitor": {"series": [{"date": "2026-11-03"}]}}
    holdings = {"regions": [{"sectors": [{"rows": [{"stock": "Alpha Corp"}]}]}]}
    write_outputs(tmp_path, payload, holdings)

    page = tmp_path / "src" / "pages" / "projects" / "vallmon.astro"
    assert page.is_file()
    assert "VALL Monitor" in page.read_text(encoding="utf-8")
    assert (tmp_path / "src" / "data" / "vallmon.json").is_file()
    assert (tmp_path / "public" / "projects" / "vallmon" / "holdings.json").is_file()
    assert not (tmp_path / "public" / "projects" / "vallmon" / "reports").exists()


def test_build_payload_uses_the_supplied_portfolio_report(tmp_path) -> None:
    """A temporary portfolio report is the only portfolio input."""
    from publish_site import build_payload

    portfolio = tmp_path / "PORTFOLIO.md"
    portfolio.write_text(PORTFOLIO_FIXTURE, encoding="utf-8")

    payload, holdings = build_payload(portfolio)
    assert payload["monitor"]["state"] in {"Equity", "Cash"}
    assert payload["monitor"]["series"]
    assert payload["portfolio"]["snapshot"] == "2026-11-03 21:16 UTC"
    assert payload["portfolio"]["region"]["rows"]
    assert payload["portfolio"]["largest"]
    assert payload["portfolio"]["by_cap"]["Large"]
    assert payload["portfolio"]["moves_up"]
    assert "<table>" in payload["methodology_html"]
    assert holdings["regions"]
    lines = sum(
        len(sector["rows"]) for region in holdings["regions"] for sector in region["sectors"]
    )
    assert lines == 3

    # Round-trip the payload the way the workflow writes it.
    encoded = json.dumps(payload)
    assert json.loads(encoded)["monitor"]["as_of"] == payload["monitor"]["as_of"]
