#!/usr/bin/env python3
"""portfolio_report - build reports/PORTFOLIO.md from Vanguard holdings.

The fund is VALL, the Vanguard FTSE Global All-Cap UCITS ETF USD Acc
(ISIN IE000VAHT5T0). The report lists every equity line in the fund with its
latest one-day move, grouped by region and sector.

Data flow:

    Vanguard GraphQL holdings (5 pages, re-fetched every run)
        -> resolve each line to a Yahoo symbol (cache: data/portfolio_symbols.json)
        -> Yahoo quote endpoint (2,000 symbols per request, gzip)
        -> reports/PORTFOLIO.md

Vanguard changes the holdings file from time to time. The report always uses
the current file: new lines are resolved on the run they appear, removed lines
simply stop appearing. The previous holdings snapshot is kept in
data/portfolio_holdings.json so the report can show what changed.

The Yahoo symbol and Hong Kong country caches are keyed by SEDOL (falling back
to country + ticker) and are additive. A line that disappears for a while and
returns later keeps its resolved symbol. Negative search results are cached for
30 days so a long tail of unpriced small caps does not trigger a search on every
build.

Commands:
    build   fetch, resolve, quote and write reports/PORTFOLIO.md
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import difflib
import gzip
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Configuration ----------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_PATH = ROOT / "reports" / "PORTFOLIO.md"
HOLDINGS_PATH = DATA_DIR / "portfolio_holdings.json"
SYMBOLS_PATH = DATA_DIR / "portfolio_symbols.json"
HK_COUNTRIES_PATH = DATA_DIR / "portfolio_hk_countries.json"

VANGUARD_URL = "https://www.vanguardinvestor.co.uk/gpx/graphql"
VANGUARD_ORIGIN = "https://www.vanguardinvestor.co.uk"
PORT_ID = "E161"

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
YAHOO_PROFILE_URL = (
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    "?modules=assetProfile&crumb={crumb}"
)
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

USER_AGENT = os.environ.get(
    "VALLMON_USER_AGENT", "Mozilla/5.0 (compatible; vallmon-portfolio/1.0)"
)

HOLDINGS_LIMIT = 1500          # Vanguard's maximum page size
QUOTE_BATCH = 2000             # tested; falls back to halves if the URL is rejected
SEARCH_WORKERS = 5
PROFILE_WORKERS = 4
NEGATIVE_CACHE_DAYS = 30
HTTP_ATTEMPTS = 3
HTTP_BACKOFF = 1.0

# Market-cap bands, in USD. These are the common large/mid/small cutoffs; the
# report states them explicitly so a reader can see what the labels mean.
CAP_LARGE = 10_000_000_000
CAP_MID = 2_000_000_000
CAP_ORDER = ["Large", "Mid", "Small", "Unknown"]

EQUITY_TYPES = {"EQ.STOCK", "EQ.REIT", "EQ.PREF", "EQ.DRCPT", "EQ.FSH"}

# Minimal fields kept in the previous-holdings snapshot. The report only needs
# enough to identify a line and show it in the changes table.
SNAPSHOT_FIELDS = (
    "issuerName",
    "securityLongDescription",
    "securityType",
    "ticker",
    "sedol1",
    "bloombergIsoCountry",
    "marketValuePercentage",
)

# Yahoo exchange suffixes. The suffix is the listing venue, not the country of
# risk; the holdings feed's country is the starting point and Hong Kong-listed
# issuers get a profile lookup (see below).
SUFFIXES = {
    "CA": [".TO", ".V"],
    "GB": [".L"],
    "JP": [".T"],
    "FR": [".PA"],
    "DE": [".DE"],
    "CH": [".SW"],
    "SE": [".ST"],
    "NL": [".AS"],
    "IT": [".MI"],
    "ES": [".MC"],
    "BR": [".SA"],
    "ZA": [".JO"],
    "SG": [".SI"],
    "MX": [".MX"],
    "ID": [".JK"],
    "TH": [".BK"],
    "MY": [".KL"],
    "PH": [".PS"],
    "TR": [".IS"],
    "PL": [".WA"],
    "IL": [".TA"],
    "NO": [".OL"],
    "FI": [".HE"],
    "BE": [".BR"],
    "AT": [".VI"],
    "IE": [".IR", ".L"],
    "NZ": [".NZ"],
    "PT": [".LS"],
    "GR": [".AT"],
    "AE": [".AE", ".DU"],
    "QA": [".QA"],
    "KW": [".KW"],
    "CL": [".SN"],
    "CO": [".CL"],
    "HU": [".BD"],
    "CZ": [".PR"],
    "EG": [".CA"],
    "VN": [".VN"],
    "TW": [".TW", ".TWO"],
    "KR": [".KS", ".KQ"],
    "AU": [".AX"],
    "IS": [".IC"],
    "RO": [".RO"],
    "SA": [".SR"],
    "DK": [".CO"],
    "IN": [".NS", ".BO"],
    "CN": [".SS", ".SZ"],
    "HK": [".HK"],
    "US": [""],
}

# Bloomberg tickers that Yahoo does not expose under the same code. Keep this
# list small and specific: a wrong symbol is worse than a missing quote.
MANUAL_SYMBOLS = {
    ("SG", "OCBC"): "O39.SI",
    ("SG", "UOB"): "U11.SI",
    ("SG", "ST"): "Z74.SI",
    ("SG", "SIA"): "C6L.SI",
    ("SG", "SGX"): "S68.SI",
    ("SG", "HKL"): "H78.SI",
    ("SG", "JM"): "J36.SI",
    ("SG", "SGREIT"): "P40U.SI",
    ("SG", "CICT"): "C38U.SI",
    ("SG", "SIE"): "S59.SI",
    ("SG", "WIL"): "F34.SI",
    ("SG", "IHH"): "Q0F.SI",
    ("MY", "PBK"): "1295.KL",
    ("MY", "MAY"): "1155.KL",
    ("MY", "CIMB"): "1023.KL",
    ("MY", "TNB"): "5347.KL",
    ("MY", "PMAH"): "8869.KL",
    ("MY", "GAM"): "5398.KL",
    ("MY", "SDG"): "5285.KL",
    ("MY", "MISC"): "3816.KL",
    ("MY", "SPTOTO"): "1562.KL",
    ("MY", "YTLP"): "6742.KL",
    ("MY", "ITMAX"): "5309.KL",
    ("CA", "TECK/B"): "TECK-B.TO",
    ("SA", "SNB"): "1180.SR",
    ("IN", "BAF"): "BAJFINANCE.NS",
    ("IN", "SUNP"): "SUNPHARMA.NS",
    ("IN", "SHFL"): "SHRIRAMFIN.NS",
    ("IN", "DRRD"): "DRREDDY.NS",
    ("IN", "ADSEZ"): "ADANIPORTS.NS",
    ("IN", "APHS"): "APOLLOHOSP.NS",
    ("IN", "PWGR"): "POWERGRID.NS",
    ("IN", "CIFC"): "CHOLAFIN.NS",
    ("IN", "TTAN"): "TITAN.NS",
    ("IN", "MOTHERSO"): "MOTHERSON.NS",
    ("IN", "TATACONS"): "TATACONSUM.NS",
    ("IN", "IH"): "INDHOTEL.NS",
    ("IN", "MAXHEALT"): "MAXHEALTH.NS",
    ("IN", "LAURUS"): "LAURUSLABS.NS",
    ("IN", "NTPCGREE"): "NTPC.NS",
    ("IN", "ADANIENS"): "ADANIENSOL.NS",
    ("IN", "TPWR"): "TATAPOWER.NS",
    ("IN", "POLICYBZ"): "POLICYBZR.NS",
    ("IN", "MAZDOCKS"): "MAZDOCK.NS",
    ("IN", "WLCO"): "WELCORP.NS",
    ("IN", "JINDALST"): "JINDALSTEL.NS",
    ("IN", "JKCE"): "JKCEMENT.NS",
    ("IN", "CBOI"): "CENTRALBK.NS",
    ("IN", "RITE"): "RITES.NS",
    ("IN", "MOFS"): "MOTILALOFS.NS",
    ("CL", "FALAB"): "FALABELLA.SN",
    ("CL", "SQM/B"): "SQM-B.SN",
    ("MX", "FIBRAPL"): "FIBRAPL14.MX",
}

HOLDINGS_QUERY = """
query FundsHoldingsQuery($portIds: [String!], $lastItemKey: String) {
  borHoldings(portIds: $portIds) {
    holdings(limit: 1500, lastItemKey: $lastItemKey) {
      items {
        issuerName
        securityLongDescription
        icbIndustryDescription
        marketValuePercentage
        sedol1
        ticker
        securityType
        effectiveDate
        bloombergIsoCountry
      }
      totalHoldings
      lastItemKey
    }
  }
}
"""

ALLOCATION_QUERY = """
query MarketAllocationGqlQuery($portIds: [String!]!) {
  funds(portIds: $portIds) {
    marketAllocation {
      countryCode
      countryName
      regionName
      holdingStatCode
      fundMktPercent
    }
  }
}
"""


class FetchError(RuntimeError):
    """Raised when a remote endpoint cannot be read."""


# --- HTTP helpers -----------------------------------------------------------


def _read_body(response: Any) -> bytes:
    raw = response.read()
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def _http_json(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if headers:
        request_headers.update(headers)
    for attempt in range(HTTP_ATTEMPTS):
        request = urllib.request.Request(url, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(_read_body(response))
        except urllib.error.HTTPError as exc:
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < HTTP_ATTEMPTS - 1:
                time.sleep(HTTP_BACKOFF * (2**attempt))
                continue
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise FetchError("HTTP %d for %s: %s" % (exc.code, url, detail)) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < HTTP_ATTEMPTS - 1:
                time.sleep(HTTP_BACKOFF * (2**attempt))
                continue
            raise FetchError("request failed for %s: %s" % (url, exc)) from exc
    raise FetchError("request failed for %s" % url)


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    payload = _http_json(
        VANGUARD_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "origin": VANGUARD_ORIGIN,
            "referer": VANGUARD_ORIGIN + "/",
        },
    )
    if payload.get("errors"):
        raise FetchError("Vanguard GraphQL error: %s" % payload["errors"])
    return payload.get("data") or {}


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


# --- Vanguard ---------------------------------------------------------------


def fetch_holdings() -> dict[str, Any]:
    """Fetch every holdings page. The list is re-read on every build."""
    items: list[dict[str, Any]] = []
    total = 0
    key: Any = None
    seen: set[str] = set()
    while True:
        data = _graphql(HOLDINGS_QUERY, {"portIds": [PORT_ID], "lastItemKey": key})
        block = data["borHoldings"][0]["holdings"]
        page = block.get("items") or []
        items.extend(page)
        total = block.get("totalHoldings") or total
        key = block.get("lastItemKey")
        marker = json.dumps(key, sort_keys=True) if key else ""
        if not key or not page or marker in seen:
            break
        seen.add(marker)
        if len(items) >= total:
            break
    if total and len(items) != total:
        raise FetchError("Vanguard returned %d of %d holdings" % (len(items), total))
    as_of = next((item.get("effectiveDate") for item in items if item.get("effectiveDate")), None)
    return {"as_of": as_of, "total": total or len(items), "items": items}


def fetch_regions() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return country-code -> region, country-name -> region, and code -> name."""
    data = _graphql(ALLOCATION_QUERY, {"portIds": [PORT_ID]})
    rows = data["funds"][0]["marketAllocation"]
    ftse = [row for row in rows if row.get("holdingStatCode") == "FTCTYATPCS"] or rows
    by_code = {}
    by_name = {}
    names = {}
    for row in ftse:
        code = row.get("countryCode")
        name = row.get("countryName")
        region = row.get("regionName")
        if code and region:
            by_code[code] = region
        if name and region:
            by_name[name] = region
        if code and name:
            names[code] = name
    return by_code, by_name, names


def equity_lines(holdings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in holdings["items"] if item.get("securityType") in EQUITY_TYPES
    ]


# --- Symbol and country caches ---------------------------------------------


def security_key(item: dict[str, Any]) -> str:
    """Stable cache key: SEDOL when present, otherwise country + ticker."""
    sedol = (item.get("sedol1") or "").strip()
    if sedol:
        return "sedol:" + sedol
    country = item.get("bloombergIsoCountry") or ""
    ticker = (item.get("ticker") or "").strip().upper()
    return "ticker:%s|%s" % (country, ticker)


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        print("warning: ignoring unreadable %s (%s)" % (path, exc), file=sys.stderr)
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".tmp")
    scratch.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(scratch, path)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _recent_negative(entry: dict[str, Any] | None) -> bool:
    if not entry or entry.get("symbol"):
        return False
    attempted = entry.get("attempted") or entry.get("updated")
    if not attempted:
        return False
    try:
        when = datetime.fromisoformat(attempted).date()
    except ValueError:
        return False
    return (datetime.now(timezone.utc).date() - when).days < NEGATIVE_CACHE_DAYS


# --- Symbol candidates ------------------------------------------------------


def _clean_ticker(ticker: str | None) -> str:
    return (ticker or "").strip().upper().replace("*", "")


_NAME_STOP_WORDS = {
    "the",
    "of",
    "and",
    "for",
    "de",
    "la",
    "el",
    "und",
    "et",
    "y",
    "e",
    "inc",
    "incorporated",
    "ltd",
    "limited",
    "plc",
    "corp",
    "corporation",
    "co",
    "company",
    "sa",
    "ag",
    "nv",
    "se",
    "spa",
    "ab",
    "asa",
    "oyj",
    "bhd",
    "tbk",
    "pt",
    "pjsc",
    "psc",
    "qpsc",
    "kscp",
    "jsc",
    "spc",
    "reit",
}

# Tokens that are too common to prove two companies are the same. A match that
# shares only these needs a much stronger whole-name similarity.
_GENERIC_TOKENS = {
    "bank",
    "banking",
    "group",
    "holdings",
    "holding",
    "international",
    "investment",
    "investments",
    "financial",
    "capital",
    "global",
    "industries",
    "industrial",
    "services",
    "technology",
    "technologies",
    "systems",
    "company",
    "corporation",
    "fund",
    "trust",
    "real",
    "estate",
    "energy",
    "resources",
    "power",
    "electric",
    "properties",
    "property",
    "pharma",
    "pharmaceutical",
    "health",
    "care",
    "media",
    "telecom",
    "telecommunications",
    "motors",
    "motor",
    "mining",
    "gold",
    "silver",
    "oil",
    "gas",
    "petroleum",
    "chemical",
    "chemicals",
    "food",
    "foods",
    "retail",
    "stores",
    "insurance",
    "asset",
    "management",
    "partners",
    "ventures",
    "new",
    "issuer",
    "id",
    "bb",
    "china",
    "japan",
    "india",
    "korea",
    "asia",
    "europe",
    "america",
    "american",
    "emerging",
    "markets",
}


def _normalise_name(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [token for token in text.split() if token and token not in _NAME_STOP_WORDS]
    return " ".join(tokens)


def name_score(left: str | None, right: str | None) -> float:
    """Loose similarity between a Vanguard issuer name and a Yahoo quote name."""
    left_norm = _normalise_name(left)
    right_norm = _normalise_name(right)
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(overlap, sequence)


def name_matches(left: str | None, right: str | None, threshold: float = 0.4) -> bool:
    """True when two names share a distinctive token, or are close enough."""
    left_norm = _normalise_name(left)
    right_norm = _normalise_name(right)
    if not left_norm or not right_norm:
        return False
    shared = set(left_norm.split()) & set(right_norm.split())
    if shared - _GENERIC_TOKENS:
        return True
    return name_score(left_norm, right_norm) >= max(threshold, 0.8)


def native_candidates(item: dict[str, Any]) -> list[str]:
    """Yahoo symbols implied by the holding's ticker and listing country."""
    country = item.get("bloombergIsoCountry")
    ticker = _clean_ticker(item.get("ticker"))
    if not ticker:
        return []
    manual = MANUAL_SYMBOLS.get((country or "", ticker))
    if manual:
        return [manual]
    candidates: list[str] = []
    if country == "US":
        candidates = [ticker.replace("/", "-").replace(".", "-")]
    elif country == "CN":
        code = ticker.split(".")[0]
        first = code + (".SS" if code.startswith("6") else ".SZ")
        candidates = [first, code + (".SZ" if first.endswith(".SS") else ".SS")]
    elif country == "HK":
        code = ticker.split(".")[0]
        candidates = [(code.zfill(4) if code.isdigit() else code) + ".HK"]
    elif country == "IN":
        candidates = [ticker + ".NS", ticker + ".BO"]
    elif country == "KR":
        code = ticker.split(".")[0]
        candidates = [code + ".KS", code + ".KQ"]
    else:
        base = ticker.split("/")[0]
        suffix = (SUFFIXES.get(country or "") or [""])[0]
        if (
            country in ("SE", "DK")
            and len(base) > 1
            and base[-1] in "ABC"
            and base[-2].isalpha()
        ):
            candidates = [base[:-1] + "-" + base[-1] + suffix, base + suffix]
        elif suffix:
            candidates = [base + suffix]
        if country == "IE":
            candidates.append(base + ".L")
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _expected_suffixes(country: str | None) -> list[str]:
    return [suffix for suffix in SUFFIXES.get(country or "", []) if suffix]


# --- Yahoo client -----------------------------------------------------------

_AUTH_LOCK = threading.Lock()


class YahooClient:
    """Small wrapper around the Yahoo quote, search and profile endpoints."""

    def __init__(self) -> None:
        self.cookie = ""
        self.crumb = ""
        self._authenticate()

    def _authenticate(self) -> None:
        with _AUTH_LOCK:
            cookie = self._fetch_cookie()
            crumb = self._fetch_crumb(cookie)
            self.cookie = cookie
            self.crumb = crumb

    @staticmethod
    def _fetch_cookie() -> str:
        request = urllib.request.Request(YAHOO_COOKIE_URL, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                headers = response.headers
        except urllib.error.HTTPError as exc:
            headers = exc.headers
        for value in headers.get_all("Set-Cookie") or []:
            match = re.match(r"(A3=[^;]+)", value)
            if match:
                return match.group(1)
        raise FetchError("Yahoo did not return an A3 cookie")

    @staticmethod
    def _fetch_crumb(cookie: str) -> str:
        request = urllib.request.Request(
            YAHOO_CRUMB_URL,
            headers={"User-Agent": USER_AGENT, "Cookie": cookie},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return _read_body(response).decode().strip()
        except urllib.error.HTTPError as exc:
            raise FetchError("Yahoo crumb request failed: HTTP %d" % exc.code) from exc

    def _get_json(self, url: str, *, attempts: int = 3) -> dict[str, Any]:
        refreshed = False
        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "Cookie": self.cookie,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(_read_body(response))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and not refreshed:
                    self._authenticate()
                    refreshed = True
                    continue
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < attempts - 1:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                detail = exc.read()[:300].decode("utf-8", "replace")
                raise FetchError("HTTP %d for %s: %s" % (exc.code, url, detail)) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                time.sleep(1.0 * (attempt + 1))
                if attempt == attempts - 1:
                    raise FetchError("request failed for %s: %s" % (url, exc)) from exc
        raise FetchError("request failed for %s" % url)

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        unique = list(dict.fromkeys(symbols))
        for batch in _chunks(unique, QUOTE_BATCH):
            result.update(self._quote_batch(batch))
        return result

    def _quote_batch(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        query = urllib.parse.quote(",".join(symbols))
        url = "%s?symbols=%s&crumb=%s" % (
            YAHOO_QUOTE_URL,
            query,
            urllib.parse.quote(self.crumb),
        )
        try:
            payload = self._get_json(url)
        except FetchError:
            if len(symbols) > 500:
                middle = len(symbols) // 2
                merged = self._quote_batch(symbols[:middle])
                merged.update(self._quote_batch(symbols[middle:]))
                return merged
            raise
        rows = (payload.get("quoteResponse") or {}).get("result") or []
        quotes = {}
        for row in rows:
            symbol = row.get("symbol")
            change = row.get("regularMarketChangePercent")
            price = row.get("regularMarketPrice")
            if not symbol or change is None or price is None:
                continue
            quotes[symbol] = {
                "chg": change,
                "price": price,
                "timestamp": row.get("regularMarketTime"),
                "currency": row.get("currency"),
                "exchange": row.get("fullExchangeName"),
                "market_state": row.get("marketState"),
                "market_cap": row.get("marketCap"),
                "name": row.get("longName") or row.get("shortName"),
            }
        return quotes

    def spark_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Fallback for the few symbols the quote endpoint omits.

        Spark has no currency or market cap, so these rows are classified as
        Unknown unless another quote already supplied those fields.
        """
        quotes = {}
        for batch in _chunks(list(dict.fromkeys(symbols)), 20):
            url = "%s?symbols=%s&range=1d&interval=1d" % (
                YAHOO_SPARK_URL,
                urllib.parse.quote(",".join(batch)),
            )
            try:
                payload = self._get_json(url)
            except FetchError:
                continue
            for symbol, row in payload.items():
                change = row.get("fulldayChangePercent")
                price = row.get("fulldayPrice")
                if change is None or price is None:
                    continue
                timestamps = row.get("timestamp") or []
                quotes[symbol] = {
                    "chg": change,
                    "price": price,
                    "timestamp": timestamps[-1] if timestamps else None,
                    "currency": None,
                    "exchange": None,
                    "market_state": None,
                    "market_cap": None,
                    "name": None,
                }
        return quotes

    def search(self, item: dict[str, Any]) -> str | None:
        query = (
            item.get("issuerName")
            or item.get("securityLongDescription")
            or item.get("ticker")
            or ""
        ).strip()
        if not query:
            return None
        url = "%s?q=%s&quotesCount=10&newsCount=0" % (
            YAHOO_SEARCH_URL,
            urllib.parse.quote(query),
        )
        payload = self._get_json(url)
        expected = _expected_suffixes(item.get("bloombergIsoCountry"))
        issuer = item.get("issuerName") or item.get("securityLongDescription")
        ranked = []
        for quote in payload.get("quotes") or []:
            quote_type = quote.get("quoteType")
            if quote_type and quote_type not in ("EQUITY", "ETF"):
                continue
            symbol = quote.get("symbol") or ""
            rank: int | None
            if item.get("bloombergIsoCountry") == "US":
                if "." in symbol:
                    continue
                rank = 0
            else:
                rank = next(
                    (index for index, suffix in enumerate(expected) if symbol.endswith(suffix)),
                    None,
                )
                if rank is None:
                    continue
            result_name = quote.get("longname") or quote.get("shortname")
            if (
                result_name
                and name_matches(issuer, result_name, threshold=0.5)
                and name_score(issuer, result_name) >= 0.5
            ):
                score = name_score(issuer, result_name)
                ranked.append((-score, rank, len(symbol), symbol))
        return min(ranked)[3] if ranked else None

    def profile_country(self, symbol: str) -> str | None:
        url = YAHOO_PROFILE_URL.format(
            symbol=urllib.parse.quote(symbol),
            crumb=urllib.parse.quote(self.crumb),
        )
        try:
            payload = self._get_json(url)
        except FetchError:
            return None
        result = (payload.get("quoteSummary") or {}).get("result") or []
        if not result:
            return None
        return (result[0].get("assetProfile") or {}).get("country")


# --- Resolution -------------------------------------------------------------


def _cap_factor(
    lines: list[dict[str, Any]],
    candidates: dict[str, list[str]],
    quotes: dict[str, dict[str, Any]],
) -> float | None:
    """Median market cap per 1% of fund weight, used to reject bad symbols."""
    ratios = []
    for item in lines:
        weight = float(item.get("marketValuePercentage") or 0)
        if weight < 0.0005:
            continue
        for symbol in candidates.get(security_key(item), []):
            quote = quotes.get(symbol)
            market_cap = (quote or {}).get("market_cap")
            if market_cap and market_cap > 0:
                ratios.append(float(market_cap) / weight)
    return statistics.median(ratios) if ratios else None


def _pick_symbol(
    item: dict[str, Any],
    options: list[str],
    quotes: dict[str, dict[str, Any]],
    cap_factor: float | None = None,
) -> str | None:
    issuer = item.get("issuerName") or item.get("securityLongDescription")
    country = item.get("bloombergIsoCountry") or ""
    ticker = _clean_ticker(item.get("ticker"))
    weight = float(item.get("marketValuePercentage") or 0)
    ranked = []
    for order, symbol in enumerate(options):
        quote = quotes.get(symbol)
        if not quote:
            continue
        if MANUAL_SYMBOLS.get((country, ticker)) == symbol:
            score = 2.0
        else:
            market_cap = quote.get("market_cap")
            cap_ok = False
            if market_cap and weight >= 0.0005 and cap_factor:
                ratio = float(market_cap) / weight
                cap_ok = cap_factor / 50 <= ratio <= cap_factor * 50
            quote_name = quote.get("name")
            if not cap_ok and quote_name and not name_matches(issuer, quote_name):
                continue
            score = 1.0 if cap_ok else (name_score(issuer, quote_name) if quote_name else 0.5)
        ranked.append((-score, order, symbol))
    return min(ranked)[2] if ranked else None


def resolve_symbols(
    lines: list[dict[str, Any]],
    symbols: dict[str, Any],
    client: YahooClient,
    *,
    refresh: bool,
    search: bool,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Pick a working Yahoo symbol for each current holding.

    Returns the chosen symbols keyed by security key, plus all quotes fetched.
    """
    cached = symbols.setdefault("symbols", {})
    candidates: dict[str, list[str]] = {}
    items_by_key: dict[str, dict[str, Any]] = {}
    for item in lines:
        key = security_key(item)
        items_by_key[key] = item
        options: list[str] = []
        entry = cached.get(key)
        if entry and entry.get("symbol"):
            options.append(entry["symbol"])
        options.extend(native_candidates(item))
        candidates[key] = list(dict.fromkeys(options))

    all_options = [symbol for options in candidates.values() for symbol in options]
    quotes = client.quotes(all_options)
    factor = _cap_factor(lines, candidates, quotes)
    chosen: dict[str, str] = {}
    for key, options in candidates.items():
        symbol = _pick_symbol(items_by_key[key], options, quotes, factor)
        if symbol:
            chosen[key] = symbol

    # Only retry/fallback for lines that are still unresolved. The quote
    # endpoint omits a long tail of alternative exchange codes; fetching all of
    # them through spark would cost far more than the handful actually needed.
    unresolved = [key for key in candidates if key not in chosen]
    retry_symbols = [
        symbol
        for key in unresolved
        for symbol in candidates[key]
        if symbol not in quotes
    ]
    if retry_symbols:
        print(f"  {len(retry_symbols):,} fallback quotes", file=sys.stderr)
        quotes.update(client.quotes(retry_symbols))
        still_missing = [symbol for symbol in retry_symbols if symbol not in quotes]
        if still_missing:
            quotes.update(client.spark_quotes(still_missing))
        factor = _cap_factor(lines, candidates, quotes)
        for key in unresolved:
            symbol = _pick_symbol(items_by_key[key], candidates[key], quotes, factor)
            if symbol:
                chosen[key] = symbol

    search_attempted: set[str] = set()
    search_failed: set[str] = set()
    if search:
        missing = [
            item
            for item in lines
            if security_key(item) not in chosen
            and (
                refresh
                or not _recent_negative(cached.get(security_key(item)))
                or (cached.get(security_key(item)) or {}).get("ticker") != item.get("ticker")
            )
        ]
        if missing:
            print(f"  {len(missing):,} name lookups", file=sys.stderr)
        found: dict[str, str] = {}
        if missing:
            with concurrent.futures.ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
                futures = {pool.submit(client.search, item): item for item in missing}
                for future in concurrent.futures.as_completed(futures):
                    item = futures[future]
                    key = security_key(item)
                    search_attempted.add(key)
                    try:
                        symbol = future.result()
                    except Exception:  # noqa: BLE001 - one bad search must not stop the build
                        search_failed.add(key)
                        symbol = None
                    if symbol:
                        found[key] = symbol
            if search_failed:
                print(
                    f"  {len(search_failed):,} name lookups failed (will retry)",
                    file=sys.stderr,
                )
        if found:
            found_symbols = sorted(set(found.values()))
            new_quotes = client.quotes(found_symbols)
            missing_found = [symbol for symbol in found_symbols if symbol not in new_quotes]
            if missing_found:
                new_quotes.update(client.spark_quotes(missing_found))
            quotes.update(new_quotes)
            for key, symbol in found.items():
                picked = _pick_symbol(items_by_key[key], [symbol], quotes, factor)
                if picked:
                    chosen[key] = picked

    today = _today()
    for item in lines:
        key = security_key(item)
        symbol = chosen.get(key)
        entry = cached.get(key) or {}
        if symbol:
            source = entry.get("source") or (
                "manual"
                if MANUAL_SYMBOLS.get(
                    ((item.get("bloombergIsoCountry") or ""), _clean_ticker(item.get("ticker")))
                )
                == symbol
                else "native"
                if symbol in native_candidates(item)
                else "search"
            )
            cached[key] = {
                "symbol": symbol,
                "source": source,
                "ticker": item.get("ticker"),
                "updated": today,
            }
        elif key in search_failed:
            # A timeout/429/5xx is not evidence that the line has no symbol.
            # Drop a previous negative entry and try again next build.
            if not (cached.get(key) or {}).get("symbol"):
                cached.pop(key, None)
            continue
        elif key in search_attempted:
            cached[key] = {
                "symbol": None,
                "source": "search",
                "ticker": item.get("ticker"),
                "updated": today,
                "attempted": today,
            }
    symbols["updated"] = today
    return chosen, quotes


def resolve_hk_countries(
    lines: list[dict[str, Any]],
    chosen: dict[str, str],
    cache: dict[str, Any],
    client: YahooClient,
    *,
    refresh: bool,
) -> dict[str, str]:
    """Return security key -> issuer country for Hong Kong-listed lines."""
    countries = cache.setdefault("countries", {})
    targets = [
        item
        for item in lines
        if item.get("bloombergIsoCountry") == "HK"
        and chosen.get(security_key(item))
        and (refresh or not (countries.get(security_key(item)) or {}).get("country"))
    ]
    if targets:
        print(f"  {len(targets):,} HK profile lookups", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=PROFILE_WORKERS) as pool:
            futures = {
                pool.submit(client.profile_country, chosen[security_key(item)]): item
                for item in targets
            }
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    country = future.result()
                except Exception:  # noqa: BLE001 - a failed profile falls back to Hong Kong
                    country = None
                if country:
                    countries[security_key(item)] = {
                        "country": country,
                        "updated": _today(),
                    }
    cache["updated"] = _today()
    return {
        key: entry["country"]
        for key, entry in countries.items()
        if entry.get("country")
    }


# --- Changes ----------------------------------------------------------------


def diff_holdings(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any] | None:
    if not previous:
        return None
    old = {security_key(item): item for item in equity_lines(previous)}
    new = {security_key(item): item for item in equity_lines(current)}
    added = [item for key, item in new.items() if key not in old]
    removed = [item for key, item in old.items() if key not in new]
    added.sort(key=lambda item: -float(item.get("marketValuePercentage") or 0))
    removed.sort(key=lambda item: -float(item.get("marketValuePercentage") or 0))
    return {
        "previous_as_of": previous.get("as_of"),
        "current_as_of": current.get("as_of"),
        "added": added,
        "removed": removed,
    }


# --- Report -----------------------------------------------------------------


def _esc(value: Any) -> str:
    text = str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ").strip()
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("`", "\\`")


def _pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def _signed(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:+.{digits}f}%"


def _major_currency(currency: str | None) -> str | None:
    """Yahoo quotes some markets in minor units; marketCap is in the major one."""
    if not currency:
        return None
    currency = currency.strip().upper()
    return {
        "GBX": "GBP",
        "ZAC": "ZAR",
        "ZAR": "ZAR",
        "ILA": "ILS",
        "ILS": "ILS",
        "CNH": "CNY",
        "RMB": "CNY",
    }.get(currency, currency)


def cap_group(market_cap_usd: float | None) -> str:
    if market_cap_usd is None or market_cap_usd <= 0:
        return "Unknown"
    if market_cap_usd >= CAP_LARGE:
        return "Large"
    if market_cap_usd >= CAP_MID:
        return "Mid"
    return "Small"


def _cap_label(cap: str) -> str:
    return "—" if cap == "Unknown" else cap


def _weight(value: float) -> str:
    if value >= 0.1:
        return f"{value:.2f}%"
    if value >= 0.001:
        return f"{value:.3f}%"
    return "<0.001%"


def _day(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) < 0.005:
        return '0.00% <img src="../docs/assets/arrow-flat.svg" alt="flat" width="12" height="12">'
    icon = "arrow-up.svg" if value > 0 else "arrow-down.svg"
    alt = "gain" if value > 0 else "loss"
    return (
        f'{value:+.2f}% <img src="../docs/assets/{icon}" '
        f'alt="{alt}" width="12" height="12">'
    )


def _stats(group: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [row for row in group if row["chg"] is not None]
    weight = sum(row["weight"] for row in group)
    priced_weight = sum(row["weight"] for row in priced)
    return {
        "n": len(group),
        "np": len(priced),
        "w": weight,
        "pw": priced_weight,
        "up": sum(1 for row in priced if row["chg"] > 0.005),
        "dn": sum(1 for row in priced if row["chg"] < -0.005),
        "fl": sum(1 for row in priced if abs(row["chg"]) <= 0.005),
        "median": statistics.median([row["chg"] for row in priced]) if priced else None,
        "wavg": (
            sum(row["weight"] * row["chg"] for row in priced) / priced_weight
            if priced_weight
            else None
        ),
    }


def fetch_fx_rates(
    client: YahooClient,
    quotes: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """USD value of one unit of each quote currency, via Yahoo FX pairs."""
    currencies = {
        major
        for quote in quotes.values()
        if quote.get("currency")
        for major in [_major_currency(quote.get("currency"))]
        if major and major != "USD"
    }
    if not currencies:
        return {"USD": 1.0}
    symbols = [f"{currency}USD=X" for currency in sorted(currencies)]
    fx_quotes = client.quotes(symbols)
    rates = {"USD": 1.0}
    for currency in currencies:
        quote = fx_quotes.get(f"{currency}USD=X")
        if quote and quote.get("price"):
            rates[currency] = float(quote["price"])
    return rates


def build_rows(
    lines: list[dict[str, Any]],
    chosen: dict[str, str],
    quotes: dict[str, dict[str, Any]],
    hk_countries: dict[str, str],
    regions_by_code: dict[str, str],
    regions_by_name: dict[str, str],
    country_names: dict[str, str],
    fx_rates: dict[str, float],
) -> list[dict[str, Any]]:
    fallback_regions = {
        "Macau": "Emerging Markets",
        "Mongolia": "Emerging Markets",
        "Luxembourg": "Europe",
    }
    rows = []
    for item in lines:
        key = security_key(item)
        symbol = chosen.get(key)
        quote = quotes.get(symbol) if symbol else None
        code = item.get("bloombergIsoCountry") or ""
        country = country_names.get(code, code or "Unknown")
        region = regions_by_code.get(code, "Unclassified")
        if code == "HK" and hk_countries.get(key):
            country = hk_countries[key]
            region = regions_by_name.get(country, fallback_regions.get(country, "Unclassified"))
        currency = (quote or {}).get("currency")
        market_cap = (quote or {}).get("market_cap")
        usd_rate = fx_rates.get(_major_currency(currency) or "")
        market_cap_usd = (
            float(market_cap) * usd_rate
            if market_cap is not None and usd_rate is not None
            else None
        )
        rows.append(
            {
                "name": item.get("issuerName")
                or item.get("securityLongDescription")
                or item.get("ticker"),
                "ticker": item.get("ticker"),
                "sym": symbol,
                "country": country,
                "region": region,
                "sector": item.get("icbIndustryDescription") or "Unclassified",
                "cap": cap_group(market_cap_usd),
                "market_cap_usd": market_cap_usd,
                "weight": float(item.get("marketValuePercentage") or 0),
                "chg": (quote or {}).get("chg"),
                "price": (quote or {}).get("price"),
                "currency": currency,
                "market_state": (quote or {}).get("market_state"),
                "quote_time": (quote or {}).get("timestamp"),
            }
        )
    return rows


def write_report(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    holdings_as_of: str | None,
    changes: dict[str, Any] | None,
) -> None:
    price_asof = max((row["quote_time"] or 0 for row in rows), default=0)
    price_asof_text = (
        datetime.fromtimestamp(price_asof, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if price_asof
        else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    all_stats = _stats(rows)
    regions = collections.defaultdict(list)
    sectors = collections.defaultdict(list)
    for row in rows:
        regions[row["region"]].append(row)
        sectors[row["sector"]].append(row)
    region_order = sorted(regions, key=lambda key: -sum(row["weight"] for row in regions[key]))
    sector_order = sorted(sectors, key=lambda key: -sum(row["weight"] for row in sectors[key]))
    caps = collections.defaultdict(list)
    for row in rows:
        caps[row["cap"]].append(row)
    cap_order = [cap for cap in CAP_ORDER if cap in caps]

    lines: list[str] = []
    add = lines.append
    add("# VALL portfolio — daily moves")
    add("")
    add(
        f"**Snapshot:** {price_asof_text} · **Holdings as of:** {holdings_as_of or '—'} · "
        f"**Fund:** Vanguard FTSE Global All-Cap UCITS ETF USD Acc "
        "(`VALL.L`, ISIN `IE000VAHT5T0`)"
    )
    add("")
    add(
        "Every equity line in the fund and its latest one-day price move, grouped by "
        "region and sector. This is the full portfolio — all stock, REIT, preferred and "
        "depositary-receipt lines — not just the top ten holdings."
    )
    add("")
    add(
        "> The move is the latest local-currency price change from Yahoo Finance, before "
        "dividends and before FX. Weights are the fund weights reported by Vanguard. "
        "The sign carries the meaning; the arrow is decoration. Up/down is a market "
        "observation, not a recommendation."
    )
    add("")
    add(
        "[At a glance](#at-a-glance) · [Holdings changes](#holdings-changes) · "
        "[By region](#by-region) · [By sector](#by-sector) · "
        "[By market cap](#by-market-cap) · [Largest holdings](#largest-holdings) · "
        "[Largest moves](#largest-moves) · [Full holdings](#holdings-by-region) · "
        "[Notes](#notes-and-method)"
    )
    add("")

    # At a glance
    add("## At a glance")
    add("")
    add("| Metric | Value |")
    add("|--------|-------|")
    add(f"| Equity lines | {all_stats['n']:,} |")
    add(f"| Equity weight | {_pct(all_stats['w'])} of fund |")
    add(
        f"| Priced | {all_stats['np']:,} lines · {_pct(all_stats['pw'])} of fund "
        f"({_pct(all_stats['pw'] / all_stats['w'] * 100)} of equity weight) |"
    )
    add(
        f"| Unpriced | {all_stats['n'] - all_stats['np']:,} lines · "
        f"{_pct(all_stats['w'] - all_stats['pw'])} of fund |"
    )
    add(f"| Advancers | {all_stats['up']:,} |")
    add(f"| Decliners | {all_stats['dn']:,} |")
    add(f"| Flat | {all_stats['fl']:,} |")
    add(f"| Median move | {_signed(all_stats['median'])} |")
    add(f"| Weighted move | {_signed(all_stats['wavg'])} |")
    add("")
    add(
        f"The {_pct(100 - all_stats['w'])} not covered by these lines is cash, FX, "
        "futures and other derivatives. The weighted move is the fund-weight-weighted "
        "average of the priced equity lines; it is not the ETF’s NAV return."
    )
    add("")

    # Holdings changes
    add("## Holdings changes")
    add("")
    if changes is None:
        add(
            "First snapshot — no previous holdings file to compare. The current "
            "holdings are saved so the next build can show additions and removals."
        )
    else:
        previous = changes["previous_as_of"] or "—"
        current = changes["current_as_of"] or "—"
        if previous != current:
            add(f"Vanguard published a new holdings file: **{previous} → {current}**.")
        else:
            add(f"Compared with the previous snapshot, also as of **{current}**.")
        add("")
        add(
            f"**{len(changes['added']):,} added · {len(changes['removed']):,} removed.** "
            "New lines are resolved automatically; removed lines stop being quoted."
        )
        add("")
        if changes["added"] or changes["removed"]:
            add("| Change | Stock | Ticker | Country | Weight |")
            add("|--------|-------|--------|---------|-------:|")
            for marker, items in (("Added", changes["added"]), ("Removed", changes["removed"])):
                for item in items[:10]:
                    name = item.get("issuerName") or item.get("securityLongDescription")
                    country = (
                        item.get("countryName")
                        or item.get("bloombergIsoCountry")
                        or "—"
                    )
                    add(
                        f"| {marker} | {_esc(name)} | "
                        f"`{_esc(item.get('ticker'))}` | "
                        f"{_esc(country)} | "
                        f"{_weight(float(item.get('marketValuePercentage') or 0))} |"
                    )
            add("")
        else:
            add("No equity lines were added or removed.")
    add("")

    # Region and sector summaries
    add("## By region")
    add("")
    add("| Region | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |")
    add("|--------|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|")
    for name in region_order:
        stats = _stats(regions[name])
        add(
            f"| {_esc(name)} | {stats['n']:,} | {_pct(stats['w'])} | {stats['np']:,} | "
            f"{stats['up']:,} | {stats['dn']:,} | {stats['fl']:,} | "
            f"{_signed(stats['median'])} | {_signed(stats['wavg'])} |"
        )
    add(
        f"| **Total** | **{all_stats['n']:,}** | **{_pct(all_stats['w'])}** | "
        f"**{all_stats['np']:,}** | **{all_stats['up']:,}** | **{all_stats['dn']:,}** | "
        f"**{all_stats['fl']:,}** | **{_signed(all_stats['median'])}** | "
        f"**{_signed(all_stats['wavg'])}** |"
    )
    add("")
    add("## By sector")
    add("")
    add("| Sector | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |")
    add("|--------|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|")
    for name in sector_order:
        stats = _stats(sectors[name])
        add(
            f"| {_esc(name)} | {stats['n']:,} | {_pct(stats['w'])} | {stats['np']:,} | "
            f"{stats['up']:,} | {stats['dn']:,} | {stats['fl']:,} | "
            f"{_signed(stats['median'])} | {_signed(stats['wavg'])} |"
        )
    add("")

    # Market cap summary
    add("## By market cap")
    add("")
    add(
        f"_Large ≥ ${CAP_LARGE / 1e9:.0f}B · Mid ${CAP_MID / 1e9:.0f}–"
        f"${CAP_LARGE / 1e9:.0f}B · Small < ${CAP_MID / 1e9:.0f}B. "
        "Market cap is converted to USD using Yahoo FX rates._"
    )
    add("")
    add("| Cap | Holdings | Weight | Priced | Up | Down | Flat | Median | Weighted |")
    add("|-----|-------:|-------:|-------:|---:|-----:|-----:|-------:|---------:|")
    for cap in cap_order:
        stats = _stats(caps[cap])
        add(
            f"| {cap} | {stats['n']:,} | {_pct(stats['w'])} | {stats['np']:,} | "
            f"{stats['up']:,} | {stats['dn']:,} | {stats['fl']:,} | "
            f"{_signed(stats['median'])} | {_signed(stats['wavg'])} |"
        )
    add(
        f"| **Total** | **{all_stats['n']:,}** | **{_pct(all_stats['w'])}** | "
        f"**{all_stats['np']:,}** | **{all_stats['up']:,}** | **{all_stats['dn']:,}** | "
        f"**{all_stats['fl']:,}** | **{_signed(all_stats['median'])}** | "
        f"**{_signed(all_stats['wavg'])}** |"
    )
    add("")

    add("## Region × market cap")
    add("")
    add("_Weighted one-day move. A dash means the region has no priced line in that cap band._")
    add("")
    add("| Region | " + " | ".join(cap_order) + " | Total |")
    add("|--------|" + "|".join(["------:"] * (len(cap_order) + 1)) + "|")
    for region in region_order:
        cells = []
        for cap in cap_order:
            group = [
                row
                for row in rows
                if row["region"] == region and row["cap"] == cap and row["chg"] is not None
            ]
            priced_weight = sum(row["weight"] for row in group)
            cells.append(
                _signed(sum(row["weight"] * row["chg"] for row in group) / priced_weight)
                if priced_weight
                else "—"
            )
        stats = _stats(regions[region])
        add(
            f"| {_esc(region)} | " + " | ".join(cells) + f" | **{_signed(stats['wavg'])}** |"
        )
    add("")

    # Region x sector
    add("## Region × sector")
    add("")
    add("_Weighted one-day move. A dash means the region has no priced line in that sector._")
    add("")
    add("| Region | " + " | ".join(_esc(name) for name in sector_order) + " | Total |")
    add("|--------|" + "|".join(["------:"] * (len(sector_order) + 1)) + "|")
    for region in region_order:
        cells = []
        for sector in sector_order:
            group = [
                row
                for row in rows
                if row["region"] == region and row["sector"] == sector and row["chg"] is not None
            ]
            priced_weight = sum(row["weight"] for row in group)
            cells.append(
                _signed(sum(row["weight"] * row["chg"] for row in group) / priced_weight)
                if priced_weight
                else "—"
            )
        stats = _stats(regions[region])
        add(f"| {_esc(region)} | " + " | ".join(cells) + f" | **{_signed(stats['wavg'])}** |")
    add("")
    add("_Column order follows each sector’s weight in the fund._")
    add("")

    # Largest holdings
    top = sorted(rows, key=lambda row: -row["weight"])[:20]
    add("## Largest holdings")
    add("")
    add("| Stock | Ticker | Cap | Region | Sector | Weight | Day % |")
    add("|-------|--------|-----|--------|--------|-------:|------:|")
    for row in top:
        add(
            f"| {_esc(row['name'])} | `{_esc(row['sym'] or row['ticker'])}` | "
            f"{_cap_label(row['cap'])} | {_esc(row['region'])} | {_esc(row['sector'])} | "
            f"{_weight(row['weight'])} | {_day(row['chg'])} |"
        )
    add("")

    # Top holdings in each cap band
    add("## Largest holdings by market cap")
    add("")
    for cap in cap_order:
        if cap == "Unknown" or not caps[cap]:
            continue
        add(f"### {cap} cap")
        add("")
        add("| Stock | Ticker | Region | Sector | Weight | Day % |")
        add("|-------|--------|--------|--------|-------:|------:|")
        for row in sorted(caps[cap], key=lambda row: -row["weight"])[:10]:
            add(
                f"| {_esc(row['name'])} | `{_esc(row['sym'] or row['ticker'])}` | "
                f"{_esc(row['region'])} | {_esc(row['sector'])} | "
                f"{_weight(row['weight'])} | {_day(row['chg'])} |"
            )
        add("")

    # Movers
    movers = [row for row in rows if row["chg"] is not None and row["weight"] >= 0.01]
    add("## Largest moves")
    add("")
    add(
        "_Among holdings worth at least 0.01% of the fund, to keep the long tail of "
        "micro-caps out of the extremes._"
    )
    add("")
    for title, ordered in (
        ("Up", sorted(movers, key=lambda row: -row["chg"])[:15]),
        ("Down", sorted(movers, key=lambda row: row["chg"])[:15]),
    ):
        add(f"### {title}")
        add("")
        add("| Stock | Ticker | Cap | Region | Sector | Weight | Day % |")
        add("|-------|--------|-----|--------|--------|-------:|------:|")
        for row in ordered:
            add(
                f"| {_esc(row['name'])} | `{_esc(row['sym'] or row['ticker'])}` | "
                f"{_cap_label(row['cap'])} | {_esc(row['region'])} | {_esc(row['sector'])} | "
                f"{_weight(row['weight'])} | {_day(row['chg'])} |"
            )
        add("")

    # Full list
    add("## Holdings by region")
    add("")
    add(
        "_Within each sector, lines are sorted by fund weight. Ticker is the Yahoo "
        "Finance symbol used for the quote; where no quote was found, it is the "
        "Vanguard ticker. “—” means no matching quote._"
    )
    add("")
    for region in region_order:
        stats = _stats(regions[region])
        add(f"### {_esc(region)}")
        add("")
        add(
            f"_{stats['n']:,} lines · {_pct(stats['w'])} of fund · {stats['up']:,} up / "
            f"{stats['dn']:,} down / {stats['fl']:,} flat · median "
            f"{_signed(stats['median'])} · weighted {_signed(stats['wavg'])}_"
        )
        add("")
        sector_groups = collections.defaultdict(list)
        for row in regions[region]:
            sector_groups[row["sector"]].append(row)
        for sector in sorted(
            sector_groups, key=lambda key: -sum(row["weight"] for row in sector_groups[key])
        ):
            sector_stats = _stats(sector_groups[sector])
            add(f"#### {_esc(sector)}")
            add("")
            add(
                f"_{sector_stats['n']:,} lines · {_pct(sector_stats['w'])} of fund · "
                f"{sector_stats['up']:,} up / {sector_stats['dn']:,} down / "
                f"{sector_stats['fl']:,} flat · median {_signed(sector_stats['median'])} · "
                f"weighted {_signed(sector_stats['wavg'])}_"
            )
            add("")
            add("| Stock | Ticker | Country | Cap | Weight | Day % |")
            add("|-------|--------|---------|-----|-------:|------:|")
            for row in sorted(
                sector_groups[sector], key=lambda row: (-row["weight"], row["name"] or "")
            ):
                add(
                    f"| {_esc(row['name'])} | `{_esc(row['sym'] or row['ticker'])}` | "
                    f"{_esc(row['country'])} | {_cap_label(row['cap'])} | "
                    f"{_weight(row['weight'])} | {_day(row['chg'])} |"
                )
            add("")

    # Notes
    missing_by_country = collections.Counter(
        row["country"] for row in rows if row["chg"] is None
    )
    missing_bits = ", ".join(
        f"{country} ({count})" for country, count in missing_by_country.most_common(8)
    )
    add("## Notes and method")
    add("")
    add(
        f"- **Universe.** All {all_stats['n']:,} equity lines in Vanguard’s full holdings "
        f"file for {holdings_as_of or '—'}: ordinary shares, REITs, preferred shares, "
        "depositary receipts and foreign shares. Cash, FX, futures, swaps and rights are "
        "excluded."
    )
    add(
        f"- **Prices.** Latest available session from Yahoo Finance’s quote endpoint, "
        f"captured {price_asof_text}. Markets close at different times; Japan was closed "
        "on 2026-11-03, for example, so its lines show the previous session. The move is "
        "in the stock’s local currency."
    )
    add(
        f"- **Coverage.** {all_stats['np']:,} of {all_stats['n']:,} lines have a quote "
        f"({_pct(all_stats['pw'])} of the fund, {_pct(all_stats['pw'] / all_stats['w'] * 100)} "
        f"of equity weight). The {all_stats['n'] - all_stats['np']:,} unpriced lines are "
        f"{_pct(all_stats['w'] - all_stats['pw'])} of the fund and are mostly small caps "
        f"in {missing_bits}. Every line is kept, including those below 0.001%."
    )
    add(
        f"- **Market cap.** Yahoo market cap converted to USD using Yahoo FX pairs. "
        f"Large ≥ ${CAP_LARGE / 1e9:.0f}B, Mid ${CAP_MID / 1e9:.0f}–"
        f"${CAP_LARGE / 1e9:.0f}B, Small < ${CAP_MID / 1e9:.0f}B. Unknown means the quote "
        "did not carry a market cap or an FX rate."
    )
    add(
        "- **Holdings changes.** The holdings file is re-fetched on every build. New "
        "lines are resolved automatically; removed lines are dropped from the quote set. "
        "The symbol and Hong Kong country caches are additive and keyed by SEDOL, so a "
        "returning line keeps its old mapping."
    )
    add(
        "- **Symbol matching.** Yahoo symbols are checked against the issuer name and, "
        "where available, a market-cap plausibility test. An ambiguous match is left as "
        "“—” rather than quoted under the wrong ticker."
    )
    add(
        "- **Region.** FTSE region from Vanguard’s country allocation. Hong Kong-listed "
        "lines are reassigned to the issuer’s country (China, Hong Kong, etc.) using "
        "Yahoo Finance profile data, so mainland Chinese issuers listed in Hong Kong sit "
        "under China rather than Pacific."
    )
    add(
        "- **Sector.** ICB industry as supplied by Vanguard. ICB puts some names in "
        "different buckets than GICS — payment networks, for example, sit in Industrials, "
        "and Samsung Electronics sits in Telecommunications."
    )
    add(
        "- **Weights.** Fund weights as reported by Vanguard; they sum to 96.35% because "
        "the rest of the fund is cash and derivatives. They are not position sizes in any "
        "particular portfolio. Vanguard’s published region and sector charts cover the "
        "whole fund, so their percentages are slightly higher than the equity-only weights "
        "here."
    )
    add(
        "- **Not advice.** A one-day move is noise more often than signal. This report "
        "describes the portfolio; it does not say what to do with it."
    )
    add("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- Build ------------------------------------------------------------------


def build(
    report_path: Path = REPORT_PATH,
    *,
    refresh_symbols: bool = False,
    search: bool = True,
) -> None:
    clock = time.perf_counter()

    def mark(label: str) -> None:
        print(f"  [{time.perf_counter() - clock:5.1f}s] {label}", file=sys.stderr)

    print("Fetching Vanguard holdings ...", file=sys.stderr)
    holdings = fetch_holdings()
    print(
        f"  {holdings['total']:,} lines, as of {holdings.get('as_of') or '—'}",
        file=sys.stderr,
    )
    mark("holdings fetched")
    regions_by_code, regions_by_name, country_names = fetch_regions()
    lines = equity_lines(holdings)
    print(f"  {len(lines):,} equity lines", file=sys.stderr)
    mark("region allocation fetched")

    previous = load_json(HOLDINGS_PATH, None)
    changes = diff_holdings(previous, holdings)

    symbols = load_json(SYMBOLS_PATH, {"version": 1, "symbols": {}})
    hk_cache = load_json(HK_COUNTRIES_PATH, {"version": 1, "countries": {}})
    client = YahooClient()

    print("Resolving symbols ...", file=sys.stderr)
    chosen, quotes = resolve_symbols(
        lines, symbols, client, refresh=refresh_symbols, search=search
    )
    print(f"  {len(chosen):,} resolved, {len(quotes):,} quoted", file=sys.stderr)
    mark("symbols resolved")

    print("Fetching FX rates ...", file=sys.stderr)
    fx_rates = fetch_fx_rates(client, quotes)
    mark("FX rates fetched")

    print("Resolving Hong Kong issuer countries ...", file=sys.stderr)
    hk_countries = resolve_hk_countries(
        lines, chosen, hk_cache, client, refresh=refresh_symbols
    )
    mark("Hong Kong countries resolved")

    rows = build_rows(
        lines,
        chosen,
        quotes,
        hk_countries,
        regions_by_code,
        regions_by_name,
        country_names,
        fx_rates,
    )
    write_report(
        report_path,
        rows,
        holdings_as_of=holdings.get("as_of"),
        changes=changes,
    )
    mark("report written")

    snapshot_items = []
    for item in equity_lines(holdings):
        snapshot = {field: item.get(field) for field in SNAPSHOT_FIELDS}
        code = item.get("bloombergIsoCountry") or ""
        snapshot["countryName"] = country_names.get(code, code or "Unknown")
        snapshot_items.append(snapshot)
    save_json(
        HOLDINGS_PATH,
        {
            "as_of": holdings.get("as_of"),
            "total": holdings.get("total"),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": sorted(snapshot_items, key=security_key),
        },
    )
    save_json(SYMBOLS_PATH, symbols)
    save_json(HK_COUNTRIES_PATH, hk_cache)
    mark("caches written")

    priced = sum(1 for row in rows if row["chg"] is not None)
    print(
        f"Wrote {report_path} — {len(rows):,} lines, {priced:,} priced "
        f"({len(rows) - priced:,} missing)",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    build_parser = sub.add_parser("build", help="fetch and write reports/PORTFOLIO.md")
    build_parser.add_argument(
        "--report",
        type=Path,
        default=REPORT_PATH,
        help="output path (default: reports/PORTFOLIO.md)",
    )
    build_parser.add_argument(
        "--refresh-symbols",
        action="store_true",
        help="re-run Yahoo search and Hong Kong profile lookups for every line",
    )
    build_parser.add_argument(
        "--no-search",
        action="store_true",
        help="skip Yahoo name search; unresolved lines show a dash",
    )
    args = parser.parse_args(argv)
    if args.command not in (None, "build"):
        parser.error("unknown command: %s" % args.command)
    build(
        getattr(args, "report", REPORT_PATH),
        refresh_symbols=getattr(args, "refresh_symbols", False),
        search=not getattr(args, "no_search", False),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
