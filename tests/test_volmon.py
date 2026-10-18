"""Tests for the vallmon delta-sigma monitor.

Run with `python -m pytest` from the repository root.
"""

from __future__ import annotations

import io
import json
import math
import statistics

import pytest

import volmon

# --- helpers ----------------------------------------------------------------

class _FakeResponse(io.BytesIO):
    """Minimal stand-in for the context-managed object urlopen() returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _chart_payload(rows):
    """Build a Yahoo chart payload from a list of (timestamp, close, volume)."""
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [r[0] for r in rows],
                    "indicators": {
                        "quote": [
                            {"close": [r[1] for r in rows], "volume": [r[2] for r in rows]}
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _sigma_series(closes, window):
    """Independent reference: price_index -> annualized sigma."""
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out = {}
    for j in range(window - 1, len(returns)):
        chunk = returns[j - window + 1 : j + 1]
        out[j + 1] = statistics.stdev(chunk) * math.sqrt(volmon.TRADING_DAYS_PER_YEAR)
    return out


# --- parsing ----------------------------------------------------------------

def test_parse_payload_filters_placeholders():
    payload = _chart_payload(
        [
            (1758000000, None, 0),      # null close -> dropped
            (1758086400, 3.70, 0),      # zero volume -> dropped
            (1758172800, 3.71, 1000),   # kept
            (1758259200, 3.72, 500),    # kept
        ]
    )
    prices = volmon._parse_payload(payload)
    assert [close for _, close in prices] == [3.71, 3.72]
    assert prices == sorted(prices)


def test_parse_payload_error_raises_fetch_error():
    payload = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    with pytest.raises(volmon.FetchError):
        volmon._parse_payload(payload)


def test_parse_payload_empty_result_raises_fetch_error():
    with pytest.raises(volmon.FetchError):
        volmon._parse_payload({"chart": {"result": None, "error": None}})


def test_parse_payload_ragged_arrays_raise_fetch_error():
    payload = _chart_payload([(1758172800, 3.71, 1000)])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [10, 20]
    with pytest.raises(volmon.FetchError):
        volmon._parse_payload(payload)


# --- fetch retry ------------------------------------------------------------

def test_fetch_prices_retries_then_succeeds(monkeypatch):
    payload = _chart_payload([(1758172800, 3.71, 1000)])
    calls = {"n": 0}

    def fake_urlopen(request, timeout=30):
        calls["n"] += 1
        if calls["n"] < 2:
            raise volmon.urllib.error.URLError("boom")
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(volmon.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(volmon.time, "sleep", lambda _seconds: None)

    prices = volmon.fetch_prices(attempts=3, backoff=0)
    assert calls["n"] == 2
    assert len(prices) == 1
    assert prices[0][1] == 3.71


def test_fetch_prices_gives_up_with_fetch_error(monkeypatch):
    def fake_urlopen(request, timeout=30):
        raise volmon.urllib.error.URLError("boom")

    monkeypatch.setattr(volmon.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(volmon.time, "sleep", lambda _seconds: None)

    with pytest.raises(volmon.FetchError):
        volmon.fetch_prices(attempts=2, backoff=0)


# --- metric -----------------------------------------------------------------

def test_compute_rows_needs_at_least_two_prices():
    assert volmon.compute_rows([]) == []
    assert volmon.compute_rows([("2026-01-01", 100.0)]) == []


def test_compute_rows_flat_series_yields_no_readings(monkeypatch):
    # Constant prices -> zero returns -> zero baseline, which is skipped.
    monkeypatch.setattr(volmon, "WINDOW", 3)
    prices = [("2026-01-%02d" % (i + 1), 100.0) for i in range(6)]
    assert volmon.compute_rows(prices) == []


def test_compute_rows_identical_windows_give_unit_delta(monkeypatch):
    # With W=2 the last two windows hold the same pair of returns, so the
    # current sigma equals the single prior sigma and delta is exactly 1.
    monkeypatch.setattr(volmon, "WINDOW", 2)
    prices = [
        ("2026-01-01", 100.0),
        ("2026-01-02", 110.0),
        ("2026-01-03", 99.0),
        ("2026-01-04", 108.9),
    ]
    rows = volmon.compute_rows(prices)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-01-04"
    assert rows[0]["close"] == 108.9
    assert rows[0]["delta_sigma"] == pytest.approx(1.0)


def test_compute_rows_matches_independent_reference(monkeypatch):
    monkeypatch.setattr(volmon, "WINDOW", 3)
    monkeypatch.setattr(volmon, "BASELINE", 2)
    closes = [100, 103, 99, 105, 101, 107, 102, 108]
    prices = [("2026-01-%02d" % (i + 1), c) for i, c in enumerate(closes)]

    rows = volmon.compute_rows(prices)
    ref = _sigma_series(closes, window=3)
    indices = sorted(ref)
    assert [row["date"] for row in rows] == [prices[i][0] for i in indices[1:]]

    for k, row in enumerate(rows, start=1):
        prior = [ref[i] for i in indices[:k]]
        baseline = statistics.fmean(prior[-2:])
        assert row["sigma_annualized"] == pytest.approx(ref[indices[k]])
        assert row["baseline_mean"] == pytest.approx(baseline)
        assert row["delta_sigma"] == pytest.approx(ref[indices[k]] / baseline)


@pytest.mark.parametrize(
    "delta,label",
    [
        (2.0, "elevated"),
        (1.5, "elevated"),
        (1.49, "expanding"),
        (1.15, "expanding"),
        (1.0, "normal"),
        (0.85, "normal"),
        (0.8, "compressing"),
        (0.7, "compressing"),
        (0.69, "compressed"),
    ],
)
def test_regime_boundaries(delta, label):
    assert label in volmon.regime(delta)


def test_sparkline_empty():
    assert volmon.sparkline([]) == ""


def test_sparkline_flat_uses_middle_glyph():
    assert volmon.sparkline([1.0, 1.0, 1.0]) == volmon.SPARK_CHARS[3] * 3


def test_sparkline_maps_low_and_high():
    line = volmon.sparkline([0.0, 1.0])
    assert line[0] == volmon.SPARK_CHARS[0]
    assert line[1] == volmon.SPARK_CHARS[-1]


# --- persistence ------------------------------------------------------------

def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(volmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(volmon, "CSV_PATH", tmp_path / "delta_sigma.csv")

    row = {
        "date": "2026-01-02",
        "close": 3.71,
        "sigma_annualized": 0.1,
        "baseline_mean": 0.09,
        "delta_sigma": 1.1,
    }
    volmon.save_rows({"2026-01-02": volmon._format(row)})

    assert not (tmp_path / "delta_sigma.csv.tmp").exists()  # atomic write cleaned up
    assert volmon.load_rows() == {"2026-01-02": volmon._format(row)}


def test_load_rows_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(volmon, "CSV_PATH", tmp_path / "nope.csv")
    assert volmon.load_rows() == {}


def test_find_drift_flags_revisions():
    stored = {"2026-01-02": {"delta_sigma": "1.0000"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.05}]
    assert volmon.find_drift(stored, computed) == [("2026-01-02", 1.0, 1.05)]


def test_find_drift_ignores_rounding_noise():
    stored = {"2026-01-02": {"delta_sigma": "1.0000"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.00005}]
    assert volmon.find_drift(stored, computed) == []


# --- commands ---------------------------------------------------------------

def _seeded(tmp_path, monkeypatch, prices):
    monkeypatch.setattr(volmon, "ROOT", tmp_path)
    monkeypatch.setattr(volmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(volmon, "CSV_PATH", tmp_path / "delta_sigma.csv")
    monkeypatch.setattr(volmon, "fetch_prices", lambda *a, **k: prices)


def test_cmd_run_appends(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0),
        ("2026-01-02", 110.0),
        ("2026-01-03", 99.0),
        ("2026-01-04", 108.9),
        ("2026-01-05", 100.0),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(volmon, "WINDOW", 2)

    assert volmon.cmd_run() == 0
    assert len(volmon.load_rows()) == 2
    assert "2 new readings" in capsys.readouterr().out


def test_cmd_run_warns_on_drift(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0),
        ("2026-01-02", 110.0),
        ("2026-01-03", 99.0),
        ("2026-01-04", 108.9),
        ("2026-01-05", 100.0),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(volmon, "WINDOW", 2)
    volmon.cmd_run()
    capsys.readouterr()

    # Corrupt one frozen reading, then re-run.
    rows = volmon.load_rows()
    rows["2026-01-04"]["delta_sigma"] = "9.0000"
    volmon.save_rows(rows)

    assert volmon.cmd_run() == 0
    captured = capsys.readouterr()
    assert "differ from a fresh recompute" in captured.err
    assert "0 new readings" in captured.out


def test_cmd_run_returns_error_on_fetch_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise volmon.FetchError("no data")

    monkeypatch.setattr(volmon, "fetch_prices", boom)
    assert volmon.cmd_run() == 1
    assert "error: no data" in capsys.readouterr().err


def test_cmd_rebuild_overwrites(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0),
        ("2026-01-02", 110.0),
        ("2026-01-03", 99.0),
        ("2026-01-04", 108.9),
        ("2026-01-05", 100.0),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(volmon, "WINDOW", 2)

    # Seed a bogus single row, then rebuild from source.
    volmon.save_rows(
        {
            "1999-01-01": volmon._format(
                {
                    "date": "1999-01-01",
                    "close": 1.0,
                    "sigma_annualized": 1.0,
                    "baseline_mean": 1.0,
                    "delta_sigma": 1.0,
                }
            )
        }
    )
    assert volmon.cmd_rebuild() == 0
    rows = volmon.load_rows()
    assert "1999-01-01" not in rows
    assert len(rows) == 2
    assert "Rebuilt" in capsys.readouterr().out


def test_main_history_rejects_nonpositive_limit(capsys):
    assert volmon.main(["history", "-n", "0"]) == 2
    assert "--limit" in capsys.readouterr().err


def test_main_requires_a_command():
    with pytest.raises(SystemExit):
        volmon.main([])
