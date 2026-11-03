"""Tests for the portfolio report's pure mapping and formatting helpers."""

from __future__ import annotations

import io
import urllib.error

import pytest

import portfolio_report as report


def _item(**overrides):
    base = {
        "issuerName": "Example Corp",
        "securityLongDescription": "Example Corp",
        "securityType": "EQ.STOCK",
        "ticker": "EX",
        "sedol1": "1234567",
        "bloombergIsoCountry": "US",
        "marketValuePercentage": 0.01,
        "icbIndustryDescription": "Technology",
    }
    base.update(overrides)
    return base


def test_security_key_prefers_sedol():
    assert report.security_key(_item()) == "sedol:1234567"
    assert (
        report.security_key(_item(sedol1=None, ticker="ABC", bloombergIsoCountry="GB"))
        == "ticker:GB|ABC"
    )


def test_native_candidates_handles_class_shares():
    assert report.native_candidates(
        _item(ticker="BRK/B", bloombergIsoCountry="US")
    ) == ["BRK-B"]
    assert report.native_candidates(
        _item(ticker="ATCOA", bloombergIsoCountry="SE")
    ) == ["ATCO-A.ST", "ATCOA.ST"]
    assert report.native_candidates(
        _item(ticker="0700", bloombergIsoCountry="HK")
    ) == ["0700.HK"]


def test_manual_override_wins():
    assert report.native_candidates(
        _item(ticker="OCBC", bloombergIsoCountry="SG")
    ) == ["O39.SI"]


def test_name_matching_tolerates_legal_suffixes_but_not_namesakes():
    assert report.name_matches("BP PLC", "BP p.l.c.")
    assert report.name_matches("Samsung Electronics Co Ltd", "Samsung Electronics Co., Ltd.")
    assert not report.name_matches(
        "JSW Steel Ltd", "Jeevan Scientific Technology Limited"
    )


def test_cap_group_boundaries():
    assert report.cap_group(None) == "Unknown"
    assert report.cap_group(10_000_000_000) == "Large"
    assert report.cap_group(2_000_000_000) == "Mid"
    assert report.cap_group(1_999_999_999) == "Small"


def test_major_currency_handles_minor_units():
    assert report._major_currency("GBp") == "GBP"
    assert report._major_currency("ZAc") == "ZAR"
    assert report._major_currency("ILA") == "ILS"
    assert report._major_currency("USD") == "USD"


def test_diff_holdings_reports_additions_and_removals():
    old_a = _item(issuerName="Old A", sedol1="1", ticker="OLD")
    keep = _item(issuerName="Keep", sedol1="2", ticker="KEEP")
    new_c = _item(issuerName="New C", sedol1="3", ticker="NEW")
    previous = {"as_of": "2026-08-31", "items": [old_a, keep]}
    current = {"as_of": "2026-09-30", "items": [keep, new_c]}

    changes = report.diff_holdings(previous, current)

    assert changes is not None
    assert [item["issuerName"] for item in changes["added"]] == ["New C"]
    assert [item["issuerName"] for item in changes["removed"]] == ["Old A"]
    assert changes["previous_as_of"] == "2026-08-31"
    assert changes["current_as_of"] == "2026-09-30"


def test_diff_holdings_returns_none_without_previous_snapshot():
    assert report.diff_holdings(None, {"items": []}) is None


def test_name_matching_rejects_namesakes_that_share_only_generic_words():
    assert not report.name_matches("Bank of China Ltd", "China Construction Bank Corp")
    assert report.name_matches("Bank of China Ltd", "Bank of China Limited")


def test_esc_escapes_table_and_html_markup():
    assert report._esc("A < B | C `D`") == "A &lt; B \\| C \\`D\\`"


def test_fetch_fx_rates_converts_each_quote_currency():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def quotes(self, symbols):
            self.calls.append(symbols)
            return {symbol: {"price": 1.25, "chg": 0} for symbol in symbols}

    client = FakeClient()
    quotes = {
        "A": {"currency": "EUR"},
        "B": {"currency": "GBp"},
        "C": {"currency": "USD"},
        "D": {"currency": None},
    }
    rates = report.fetch_fx_rates(client, quotes)

    assert rates["EUR"] == 1.25
    assert rates["GBP"] == 1.25
    assert rates["USD"] == 1.0
    assert "EURUSD=X" in client.calls[0]
    assert "GBPUSD=X" in client.calls[0]


def test_build_rows_converts_market_cap_and_region():
    item = _item(
        sedol1="1",
        ticker="ASML",
        bloombergIsoCountry="NL",
        marketValuePercentage=0.5,
    )
    quotes = {
        "ASML.AS": {
            "chg": 1.2,
            "price": 100.0,
            "currency": "EUR",
            "market_cap": 200_000_000_000,
            "name": "ASML Holding NV",
        }
    }
    rows = report.build_rows(
        [item],
        {"sedol:1": "ASML.AS"},
        quotes,
        {},
        {"NL": "Europe"},
        {},
        {"NL": "Netherlands"},
        {"USD": 1.0, "EUR": 1.1},
    )

    assert rows[0]["cap"] == "Large"
    assert rows[0]["market_cap_usd"] == pytest.approx(220_000_000_000)
    assert rows[0]["region"] == "Europe"
    assert rows[0]["country"] == "Netherlands"


def test_write_report_contains_cap_sections(tmp_path):
    rows = [
        {
            "name": "Large Co",
            "ticker": "BIG",
            "sym": "BIG",
            "country": "United States",
            "region": "North America",
            "sector": "Technology",
            "cap": "Large",
            "market_cap_usd": 20_000_000_000,
            "weight": 1.0,
            "chg": 1.0,
            "price": 10.0,
            "currency": "USD",
            "market_state": "REGULAR",
            "quote_time": 1_790_000_000,
        },
        {
            "name": "Mid Co",
            "ticker": "MID",
            "sym": "MID",
            "country": "United States",
            "region": "North America",
            "sector": "Financials",
            "cap": "Mid",
            "market_cap_usd": 5_000_000_000,
            "weight": 0.5,
            "chg": -1.0,
            "price": 20.0,
            "currency": "USD",
            "market_state": "REGULAR",
            "quote_time": 1_790_000_000,
        },
    ]
    path = tmp_path / "PORTFOLIO.md"

    report.write_report(path, rows, holdings_as_of="2026-09-30", changes=None)

    text = path.read_text()
    assert "## By market cap" in text
    assert "## Largest holdings by market cap" in text
    assert "| Large |" in text
    assert "## Holdings changes" in text


def test_write_report_changes_uses_country_names(tmp_path):
    rows = [
        {
            "name": "Large Co",
            "ticker": "BIG",
            "sym": "BIG",
            "country": "United States",
            "region": "North America",
            "sector": "Technology",
            "cap": "Large",
            "market_cap_usd": 20_000_000_000,
            "weight": 1.0,
            "chg": 1.0,
            "price": 10.0,
            "currency": "USD",
            "market_state": "REGULAR",
            "quote_time": 1_790_000_000,
        }
    ]
    changes = {
        "previous_as_of": "2026-08-31",
        "current_as_of": "2026-09-30",
        "added": [
            {
                "issuerName": "New Co",
                "ticker": "NEW",
                "countryName": "United Kingdom",
                "marketValuePercentage": 0.1,
            }
        ],
        "removed": [],
    }
    path = tmp_path / "PORTFOLIO.md"

    report.write_report(path, rows, holdings_as_of="2026-09-30", changes=changes)

    text = path.read_text()
    assert "| Added | New Co | `NEW` | United Kingdom | 0.10% |" in text


class _FakeYahoo:
    def __init__(self, quotes=None, search_result=None, search_error=None):
        self._quotes = quotes or {}
        self._search_result = search_result
        self._search_error = search_error

    def quotes(self, symbols):
        return {symbol: self._quotes[symbol] for symbol in symbols if symbol in self._quotes}

    def spark_quotes(self, symbols):
        return {}

    def search(self, item):
        if self._search_error:
            raise self._search_error
        return self._search_result


def _good_quote(name):
    return {
        "chg": 1.0,
        "price": 10.0,
        "currency": "USD",
        "market_cap": 20_000_000_000,
        "name": name,
    }


def test_pick_symbol_rejects_an_implausible_market_cap():
    item = _item(
        issuerName="JSW Steel Ltd",
        ticker="JSTL",
        bloombergIsoCountry="IN",
        marketValuePercentage=0.1,
    )
    quotes = {
        "JSTL.BO": {
            "chg": 1.0,
            "price": 1.0,
            "currency": "INR",
            "market_cap": 100_000_000,
            "name": "Jeevan Scientific Technology Limited",
        }
    }

    assert report._pick_symbol(item, ["JSTL.BO"], quotes, cap_factor=1e12) is None


def test_pick_symbol_accepts_a_rename_when_the_cap_is_plausible():
    item = _item(
        issuerName="Prc Newco Inc",
        ticker="PR",
        bloombergIsoCountry="US",
        marketValuePercentage=0.01,
    )
    quotes = {
        "PR": {
            "chg": 1.0,
            "price": 10.0,
            "currency": "USD",
            "market_cap": 20_000_000_000,
            "name": "Permian Resources Corporation",
        }
    }

    assert report._pick_symbol(item, ["PR"], quotes, cap_factor=2e12) == "PR"


def test_resolve_symbols_uses_cached_symbol():
    item = _item(sedol1="10", ticker="EX", issuerName="Example Corp")
    key = report.security_key(item)
    cache = {"symbols": {key: {"symbol": "EX", "source": "native", "ticker": "EX"}}}
    client = _FakeYahoo(quotes={"EX": _good_quote("Example Corp")})

    chosen, quotes = report.resolve_symbols(
        [item], cache, client, refresh=False, search=False
    )

    assert chosen[key] == "EX"
    assert "EX" in quotes


def test_resolve_symbols_does_not_cache_transient_search_failures():
    item = _item(sedol1="11", ticker="ZZ", issuerName="Missing Corp")
    key = report.security_key(item)
    cache = {"symbols": {}}
    client = _FakeYahoo(search_error=report.FetchError("boom"))

    chosen, _ = report.resolve_symbols([item], cache, client, refresh=False, search=True)

    assert chosen == {}
    assert key not in cache["symbols"]


def test_resolve_symbols_does_not_negative_cache_when_search_is_disabled():
    item = _item(sedol1="12", ticker="ZZ", issuerName="Missing Corp")
    key = report.security_key(item)
    cache = {"symbols": {}}
    client = _FakeYahoo()

    chosen, _ = report.resolve_symbols([item], cache, client, refresh=False, search=False)

    assert chosen == {}
    assert key not in cache["symbols"]


def test_resolve_symbols_negative_caches_a_genuine_no_match():
    item = _item(sedol1="13", ticker="ZZ", issuerName="Missing Corp")
    key = report.security_key(item)
    cache = {"symbols": {}}
    client = _FakeYahoo()

    chosen, _ = report.resolve_symbols([item], cache, client, refresh=False, search=True)

    assert chosen == {}
    assert cache["symbols"][key]["symbol"] is None
    assert cache["symbols"][key]["attempted"]


def test_resolve_symbols_accepts_a_search_hit():
    item = _item(sedol1="14", ticker="NOPE", issuerName="Missing Corp")
    key = report.security_key(item)
    cache = {"symbols": {}}
    client = _FakeYahoo(
        quotes={"ZZ": _good_quote("Missing Corp")}, search_result="ZZ"
    )

    chosen, _ = report.resolve_symbols([item], cache, client, refresh=False, search=True)

    assert chosen[key] == "ZZ"


class _Response:
    headers: dict[str, str] = {}

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_json_retries_transient_server_errors(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 500, "boom", {}, io.BytesIO(b"")
            )
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(report.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(report.time, "sleep", lambda seconds: None)

    assert report._http_json("https://example.test") == {"ok": True}
    assert len(calls) == 3


def test_yahoo_get_json_retries_server_errors(monkeypatch):
    client = report.YahooClient.__new__(report.YahooClient)
    client.cookie = ""
    client.crumb = ""
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 502, "bad gateway", {}, io.BytesIO(b"")
            )
        return _Response(b'{"quoteResponse": {"result": []}}')

    monkeypatch.setattr(report.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(report.time, "sleep", lambda seconds: None)

    assert client._get_json("https://example.test") == {"quoteResponse": {"result": []}}
    assert len(calls) == 3
