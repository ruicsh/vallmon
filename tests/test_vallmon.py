"""Tests for the vallmon delta-sigma monitor.

Run with `python -m pytest` from the repository root.
"""

from __future__ import annotations

import io
import json
import math
import statistics

import pytest

import vallmon

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
        out[j + 1] = statistics.stdev(chunk) * math.sqrt(vallmon.TRADING_DAYS_PER_YEAR)
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
    prices = vallmon._parse_payload(payload)
    assert [(close, volume) for _, close, volume in prices] == [(3.71, 1000), (3.72, 500)]
    assert prices == sorted(prices)


def test_parse_payload_keeps_volume_as_int():
    payload = _chart_payload([(1758172800, 3.71, 12345.0)])
    (day, close, volume), = vallmon._parse_payload(payload)
    assert isinstance(volume, int) and volume == 12345


def test_parse_payload_error_raises_fetch_error():
    payload = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    with pytest.raises(vallmon.FetchError):
        vallmon._parse_payload(payload)


def test_parse_payload_empty_result_raises_fetch_error():
    with pytest.raises(vallmon.FetchError):
        vallmon._parse_payload({"chart": {"result": None, "error": None}})


def test_parse_payload_ragged_arrays_raise_fetch_error():
    payload = _chart_payload([(1758172800, 3.71, 1000)])
    payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [10, 20]
    with pytest.raises(vallmon.FetchError):
        vallmon._parse_payload(payload)


# --- fetch retry ------------------------------------------------------------

def test_fetch_prices_retries_then_succeeds(monkeypatch):
    payload = _chart_payload([(1758172800, 3.71, 1000)])
    calls = {"n": 0}

    def fake_urlopen(request, timeout=30):
        calls["n"] += 1
        if calls["n"] < 2:
            raise vallmon.urllib.error.URLError("boom")
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(vallmon.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vallmon.time, "sleep", lambda _seconds: None)

    prices = vallmon.fetch_prices(attempts=3, backoff=0)
    assert calls["n"] == 2
    assert len(prices) == 1
    assert prices[0][1] == 3.71


def test_fetch_prices_gives_up_with_fetch_error(monkeypatch):
    def fake_urlopen(request, timeout=30):
        raise vallmon.urllib.error.URLError("boom")

    monkeypatch.setattr(vallmon.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vallmon.time, "sleep", lambda _seconds: None)

    with pytest.raises(vallmon.FetchError):
        vallmon.fetch_prices(attempts=2, backoff=0)


def test_fetch_prices_requests_full_history(monkeypatch):
    # `rebuild` overwrites the CSV from the fetch, so the request must cover the
    # whole available series - a fixed lookback would truncate older readings.
    seen = {}

    def fake_urlopen(request, timeout=30):
        seen["url"] = request.full_url
        return _FakeResponse(json.dumps(_chart_payload([(1758172800, 3.71, 1000)])).encode())

    monkeypatch.setattr(vallmon.urllib.request, "urlopen", fake_urlopen)
    vallmon.fetch_prices()

    assert "range=max" in seen["url"]
    assert vallmon.TICKER in seen["url"]


# --- metric -----------------------------------------------------------------

def test_compute_rows_needs_at_least_two_prices():
    assert vallmon.compute_rows([]) == []
    assert vallmon.compute_rows([("2026-01-01", 100.0, 1000)]) == []


def test_compute_rows_flat_series_yields_no_readings(monkeypatch):
    # Constant prices -> zero returns -> zero baseline, which is skipped.
    monkeypatch.setattr(vallmon, "WINDOW", 3)
    prices = [("2026-01-%02d" % (i + 1), 100.0, 1000) for i in range(6)]
    assert vallmon.compute_rows(prices) == []


def test_compute_rows_identical_windows_give_unit_delta(monkeypatch):
    # With W=2 the last two windows hold the same pair of returns, so the
    # current sigma equals the single prior sigma and delta is exactly 1.
    monkeypatch.setattr(vallmon, "WINDOW", 2)
    prices = [
        ("2026-01-01", 100.0, 1000),
        ("2026-01-02", 110.0, 2000),
        ("2026-01-03", 99.0, 3000),
        ("2026-01-04", 108.9, 4000),
    ]
    rows = vallmon.compute_rows(prices)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-01-04"
    assert rows[0]["close"] == 108.9
    assert rows[0]["volume"] == 4000
    assert rows[0]["delta_sigma"] == pytest.approx(1.0)


def test_compute_rows_matches_independent_reference(monkeypatch):
    monkeypatch.setattr(vallmon, "WINDOW", 3)
    monkeypatch.setattr(vallmon, "BASELINE", 2)
    closes = [100, 103, 99, 105, 101, 107, 102, 108]
    volumes = [1000 * (i + 1) for i in range(len(closes))]
    prices = [
        ("2026-01-%02d" % (i + 1), c, volumes[i]) for i, c in enumerate(closes)
    ]

    rows = vallmon.compute_rows(prices)
    ref = _sigma_series(closes, window=3)
    indices = sorted(ref)
    assert [row["date"] for row in rows] == [prices[i][0] for i in indices[1:]]

    for k, row in enumerate(rows, start=1):
        prior = [ref[i] for i in indices[:k]]
        baseline = statistics.fmean(prior[-2:])
        assert row["sigma_annualized"] == pytest.approx(ref[indices[k]])
        assert row["baseline_mean"] == pytest.approx(baseline)
        assert row["delta_sigma"] == pytest.approx(ref[indices[k]] / baseline)
        assert row["volume"] == volumes[indices[k]]


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
    assert label in vallmon.regime(delta)


def test_sparkline_empty():
    assert vallmon.sparkline([]) == ""


def test_sparkline_flat_uses_middle_glyph():
    assert vallmon.sparkline([1.0, 1.0, 1.0]) == vallmon.SPARK_CHARS[3] * 3


def test_sparkline_maps_low_and_high():
    line = vallmon.sparkline([0.0, 1.0])
    assert line[0] == vallmon.SPARK_CHARS[0]
    assert line[1] == vallmon.SPARK_CHARS[-1]


# --- term structure ---------------------------------------------------------

def _rising_prices(n):
    """A deterministic close series long enough to exercise several windows."""
    return [
        ("d%03d" % i, float(100 + i + (i % 3)), 1000 + i)
        for i in range(n)
    ]


def _term_formatted_row(day="2026-01-02", sigma10=0.11, sigma30=0.10, sigma60=None):
    return vallmon._format(
        {
            "date": day,
            "close": 3.71,
            "volume": 1000,
            "sigma_annualized": 0.10,
            "baseline_mean": 0.10,
            "delta_sigma": 1.0,
            "sigma_10": sigma10,
            "sigma_30": sigma30,
            "sigma_60": sigma60,
        }
    )


def test_sigma_series_matches_reference():
    closes = [100 + i + (i % 5) for i in range(30)]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    sigmas = vallmon._sigma_series(returns, 10)
    ref = _sigma_series(closes, window=10)
    assert sigmas == ref


def test_compute_rows_populates_available_windows_only():
    rows = vallmon.compute_rows(_rising_prices(35))
    assert rows
    # 10d and 20d are available for every reading; 60d is not (only 35 closes).
    assert all(row["sigma_10"] is not None for row in rows)
    assert all(row["sigma_60"] is None for row in rows)
    # 30d fills in only once 30 closes precede the date.
    assert rows[0]["sigma_30"] is None
    assert rows[-1]["sigma_30"] is not None


def test_compute_rows_term_sigmas_match_reference():
    prices = _rising_prices(35)
    closes = [c for _, c, _ in prices]
    rows = vallmon.compute_rows(prices)
    ref10 = _sigma_series(closes, window=10)
    ref30 = _sigma_series(closes, window=30)
    for row in rows:
        idx = int(row["date"][1:])
        assert row["sigma_10"] == pytest.approx(ref10[idx])
        if idx in ref30:
            assert row["sigma_30"] == pytest.approx(ref30[idx])
        else:
            assert row["sigma_30"] is None


def test_sigma_value_and_display():
    row = {"sigma_annualized": "0.1019", "sigma_10": "0.1102", "sigma_30": "", "sigma_60": None}
    assert vallmon.sigma_value(row, 20) == pytest.approx(0.1019)
    assert vallmon.sigma_value(row, 10) == pytest.approx(0.1102)
    assert vallmon.sigma_value(row, 30) is None
    assert vallmon.sigma_value(row, 60) is None
    assert vallmon.sigma_display(row, 10) == "11.02%"
    assert vallmon.sigma_display(row, 30) == "\u2014"


def test_term_structure_display_lists_every_window():
    row = {"sigma_annualized": 0.1019, "sigma_10": 0.1102, "sigma_30": 0.10, "sigma_60": ""}
    assert vallmon.term_structure_display(row) == "11.02% / 10.19% / 10.00% / \u2014"


def test_term_slope_and_label():
    assert vallmon.term_slope({"sigma_10": 0.12, "sigma_30": 0.10}) == pytest.approx(1.2)
    assert vallmon.term_slope_label(1.2) == "backwardated"
    assert vallmon.term_slope_label(1.0) == "flat"
    assert vallmon.term_slope_label(0.8) == "contango"
    assert vallmon.term_slope_label(None) == "n/a"
    assert vallmon.term_slope({"sigma_10": 0.12}) is None
    assert vallmon.term_slope({"sigma_30": 0.12}) is None
    assert vallmon.term_slope({"sigma_10": "", "sigma_30": 0.1}) is None
    assert vallmon.term_slope({"sigma_10": 0.1, "sigma_30": 0.0}) is None  # undefined ratio
    assert vallmon.term_slope({"sigma_10": 0.0, "sigma_30": 0.1}) == 0.0   # valid flat short leg
    assert vallmon.term_slope_display({}) == "\u2014"
    assert vallmon.slope_display(None) == "\u2014"
    assert vallmon.slope_display(0.0) == "0.00"


def test_format_blanks_missing_term_windows():
    row = {
        "date": "2026-01-02",
        "close": 3.71,
        "volume": 10,
        "sigma_annualized": 0.1,
        "baseline_mean": 0.09,
        "delta_sigma": 1.1,
        "sigma_10": 0.11,
        "sigma_30": None,
    }
    formatted = vallmon._format(row)
    assert formatted["sigma_10"] == "0.110000"
    assert formatted["sigma_30"] == ""
    assert formatted["sigma_60"] == ""


def test_term_structure_lines_render():
    rows = vallmon.sorted_rows({"2026-01-02": _term_formatted_row()})
    text = "\n".join(vallmon.term_structure_lines(rows))
    assert "| Window | σ (ann.) |" in text
    assert "| 10d | 11.00% |" in text
    assert "| 60d | \u2014 |" in text
    assert "Term slope σ10/σ30: 1.10 (backwardated)" in text


def test_build_readme_section_includes_term_slope():
    rows = vallmon.sorted_rows({"2026-01-02": _term_formatted_row()})
    section = vallmon.build_readme_section(rows)
    assert "TS σ10/σ30 1.10" in section


def test_build_readme_section_shows_dash_when_slope_unavailable():
    row = _term_formatted_row(sigma10=None, sigma30=None)
    rows = vallmon.sorted_rows({"2026-01-02": row})
    assert "TS σ10/σ30 \u2014" in vallmon.build_readme_section(rows)


def test_compute_rows_emits_declared_csv_fields():
    # Locks the compute_rows schema to CSV_FIELDS so the persisted columns and
    # the computed row can never drift apart.
    rows = vallmon.compute_rows(_rising_prices(35))
    assert rows
    for row in rows:
        assert set(row) == set(vallmon.CSV_FIELDS)


# --- persistence ------------------------------------------------------------

def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(vallmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "delta_sigma.csv")

    row = {
        "date": "2026-01-02",
        "close": 3.71,
        "volume": 12345,
        "sigma_annualized": 0.1,
        "baseline_mean": 0.09,
        "delta_sigma": 1.1,
    }
    vallmon.save_rows({"2026-01-02": vallmon._format(row)})

    assert not (tmp_path / "delta_sigma.csv.tmp").exists()  # atomic write cleaned up
    assert vallmon.load_rows() == {"2026-01-02": vallmon._format(row)}


def test_save_rows_tolerates_missing_volume(tmp_path, monkeypatch):
    # Rows written before the volume column existed must not crash the writer.
    monkeypatch.setattr(vallmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "delta_sigma.csv")

    stale = {
        "date": "2026-01-02",
        "close": "3.7100",
        "sigma_annualized": "0.100000",
        "baseline_mean": "0.090000",
        "delta_sigma": "1.1000",
    }
    vallmon.save_rows({"2026-01-02": stale})
    assert vallmon.load_rows()["2026-01-02"]["volume"] == ""


def test_volume_units_parses_and_tolerates_missing():
    assert vallmon.volume_units({"volume": "1234567"}) == 1234567
    assert vallmon.volume_units({"volume": 42}) == 42
    assert vallmon.volume_units({"volume": "1,234,567"}) == 1234567
    assert vallmon.volume_units({"volume": " 42 "}) == 42
    assert vallmon.volume_units({"volume": ""}) is None
    assert vallmon.volume_units({"volume": "n/a"}) is None
    assert vallmon.volume_units({}) is None


def test_volume_display_formats_and_tolerates_missing():
    assert vallmon.volume_display({"volume": "1234567"}) == "1,234,567"
    assert vallmon.volume_display({"volume": 42}) == "42"
    assert vallmon.volume_display({"volume": "1,234,567"}) == "1,234,567"
    assert vallmon.volume_display({"volume": ""}) == "\u2014"
    assert vallmon.volume_display({}) == "\u2014"
    assert vallmon.volume_display({"volume": 7137211}, with_units=True) == "7,137,211 units"
    assert vallmon.volume_display({}, with_units=True) == "\u2014"


def _formatted_row(volume=None):
    row = {
        "date": "2026-01-02",
        "close": 3.71,
        "sigma_annualized": 0.1,
        "baseline_mean": 0.09,
        "delta_sigma": 1.1,
    }
    if volume is not None:
        row["volume"] = volume
        return vallmon._format(row)
    # Simulate a row written before the volume column existed.
    return {
        "date": row["date"],
        "close": "%.4f" % row["close"],
        "sigma_annualized": "%.6f" % row["sigma_annualized"],
        "baseline_mean": "%.6f" % row["baseline_mean"],
        "delta_sigma": "%.4f" % row["delta_sigma"],
    }


def test_summary_lines_show_units_or_bare_dash():
    known = vallmon.summary_lines(vallmon.sorted_rows({"2026-01-02": _formatted_row(7137211)}))
    assert "| Latest volume | 7,137,211 units |" in known

    unknown = vallmon.summary_lines(vallmon.sorted_rows({"2026-01-02": _formatted_row()}))
    assert "| Latest volume | \u2014 |" in unknown


def test_build_readme_section_includes_units():
    rows = vallmon.sorted_rows({"2026-01-02": _formatted_row(7137211)})
    section = vallmon.build_readme_section(rows)
    assert "7,137,211 units" in section


def test_load_rows_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "nope.csv")
    assert vallmon.load_rows() == {}


def test_find_drift_flags_revisions():
    stored = {"2026-01-02": {"delta_sigma": "1.0000"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.05}]
    assert vallmon.find_drift(stored, computed) == [("2026-01-02", "delta_sigma", 1.0, 1.05)]


def test_find_drift_flags_term_structure_revisions():
    stored = {"2026-01-02": {"delta_sigma": "1.0000", "sigma_30": "0.100000"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.0, "sigma_30": 0.12}]
    assert vallmon.find_drift(stored, computed) == [("2026-01-02", "sigma_30", 0.1, 0.12)]


def test_find_drift_skips_uncaptured_fields():
    # A row written before the term-structure columns existed has nothing to
    # compare against, so it must not be reported as drift.
    stored = {"2026-01-02": {"delta_sigma": "1.0000", "sigma_30": ""}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.0, "sigma_30": 0.12}]
    assert vallmon.find_drift(stored, computed) == []


def test_find_drift_ignores_rounding_noise():
    stored = {"2026-01-02": {"delta_sigma": "1.0000"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.00005}]
    assert vallmon.find_drift(stored, computed) == []


def test_find_drift_skips_unparseable_stored_values():
    # A hand-edited/corrupted cell must degrade to "unknown", not crash the run.
    stored = {"2026-01-02": {"delta_sigma": "n/a", "baseline_mean": "0.09"}}
    computed = [{"date": "2026-01-02", "delta_sigma": 1.0, "baseline_mean": 0.09}]
    assert vallmon.find_drift(stored, computed) == []


def test_write_text_atomic_replaces_and_cleans_up(tmp_path):
    path = tmp_path / "out.txt"
    path.write_text("old")
    vallmon._write_text_atomic(path, "hello")
    assert path.read_text() == "hello"
    assert not (tmp_path / "out.txt.tmp").exists()


def test_reading_headline_matches_readme_and_step_summary(monkeypatch, tmp_path, capsys):
    rows = vallmon.sorted_rows({"2026-01-02": _term_formatted_row()})
    headline = vallmon.reading_headline(rows[-1])
    assert headline in vallmon.build_readme_section(rows)

    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    vallmon.write_step_summary(rows)
    assert headline in summary_path.read_text()


# --- commands ---------------------------------------------------------------

def _seeded(tmp_path, monkeypatch, prices):
    monkeypatch.setattr(vallmon, "ROOT", tmp_path)
    monkeypatch.setattr(vallmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "delta_sigma.csv")
    monkeypatch.setattr(vallmon, "fetch_prices", lambda *a, **k: prices)


def test_cmd_run_appends(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0, 1000),
        ("2026-01-02", 110.0, 2000),
        ("2026-01-03", 99.0, 3000),
        ("2026-01-04", 108.9, 4000),
        ("2026-01-05", 100.0, 5000),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(vallmon, "WINDOW", 2)

    assert vallmon.cmd_run() == 0
    assert len(vallmon.load_rows()) == 2
    assert "2 new readings" in capsys.readouterr().out


def test_cmd_run_backfills_volume_for_frozen_rows(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0, 1000),
        ("2026-01-02", 110.0, 2000),
        ("2026-01-03", 99.0, 3000),
        ("2026-01-04", 108.9, 4000),
        ("2026-01-05", 100.0, 5000),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(vallmon, "WINDOW", 2)

    # Simulate a pre-volume CSV: a frozen reading with no volume column.
    rows = {
        "2026-01-04": {
            "date": "2026-01-04",
            "close": "108.9000",
            "sigma_annualized": "0.100000",
            "baseline_mean": "0.090000",
            "delta_sigma": "1.1111",
        }
    }
    vallmon.save_rows(rows)

    assert vallmon.cmd_run() == 0
    stored = vallmon.load_rows()
    assert stored["2026-01-04"]["volume"] == "4000"         # backfilled from source
    assert stored["2026-01-04"]["delta_sigma"] == "1.1111"  # metric stayed frozen
    assert "backfilled volume for 1 existing reading" in capsys.readouterr().out


def test_cmd_run_backfills_term_structure(monkeypatch, tmp_path, capsys):
    _seeded(tmp_path, monkeypatch, _rising_prices(35))

    # Simulate a CSV written before the term-structure columns existed.
    vallmon.save_rows(
        {
            "d034": {
                "date": "d034",
                "close": "132.0000",
                "volume": "1034",
                "sigma_annualized": "0.000000",
                "baseline_mean": "0.000000",
                "delta_sigma": "1.0000",
            }
        }
    )

    assert vallmon.cmd_run() == 0
    stored = vallmon.load_rows()["d034"]
    assert stored["sigma_10"]                                  # populated
    assert stored["sigma_30"]                                  # populated (35 closes)
    assert stored["sigma_60"] == ""                            # 60d still unavailable
    assert stored["delta_sigma"] == "1.0000"                   # metric stayed frozen
    assert "backfilled term structure for 1 existing reading" in capsys.readouterr().out


def test_cmd_run_warns_on_drift(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0, 1000),
        ("2026-01-02", 110.0, 2000),
        ("2026-01-03", 99.0, 3000),
        ("2026-01-04", 108.9, 4000),
        ("2026-01-05", 100.0, 5000),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(vallmon, "WINDOW", 2)
    vallmon.cmd_run()
    capsys.readouterr()

    # Corrupt one frozen reading, then re-run.
    rows = vallmon.load_rows()
    rows["2026-01-04"]["delta_sigma"] = "9.0000"
    vallmon.save_rows(rows)

    assert vallmon.cmd_run() == 0
    captured = capsys.readouterr()
    assert "differ from a fresh recompute" in captured.err
    assert "0 new readings" in captured.out


def test_cmd_run_returns_error_on_fetch_failure(monkeypatch, capsys):
    def boom(*a, **k):
        raise vallmon.FetchError("no data")

    monkeypatch.setattr(vallmon, "fetch_prices", boom)
    assert vallmon.cmd_run() == 1
    assert "error: no data" in capsys.readouterr().err


def test_cmd_rebuild_overwrites(monkeypatch, tmp_path, capsys):
    prices = [
        ("2026-01-01", 100.0, 1000),
        ("2026-01-02", 110.0, 2000),
        ("2026-01-03", 99.0, 3000),
        ("2026-01-04", 108.9, 4000),
        ("2026-01-05", 100.0, 5000),
    ]
    _seeded(tmp_path, monkeypatch, prices)
    monkeypatch.setattr(vallmon, "WINDOW", 2)

    # Seed a bogus single row, then rebuild from source.
    vallmon.save_rows(
        {
            "1999-01-01": vallmon._format(
                {
                    "date": "1999-01-01",
                    "close": 1.0,
                    "volume": 1,
                    "sigma_annualized": 1.0,
                    "baseline_mean": 1.0,
                    "delta_sigma": 1.0,
                }
            )
        }
    )
    assert vallmon.cmd_rebuild() == 0
    rows = vallmon.load_rows()
    assert "1999-01-01" not in rows
    assert len(rows) == 2
    assert "Rebuilt" in capsys.readouterr().out


def test_main_history_rejects_nonpositive_limit(capsys):
    assert vallmon.main(["history", "-n", "0"]) == 2
    assert "--limit" in capsys.readouterr().err


def test_main_requires_a_command():
    with pytest.raises(SystemExit):
        vallmon.main([])


# --- report / history commands ----------------------------------------------

def _seeded_report(tmp_path, monkeypatch):
    monkeypatch.setattr(vallmon, "ROOT", tmp_path)
    monkeypatch.setattr(vallmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "delta_sigma.csv")
    monkeypatch.setattr(vallmon, "README_PATH", tmp_path / "README.md")
    monkeypatch.setattr(vallmon, "REPORT_PATH", tmp_path / "REPORT.md")


def test_cmd_report_splices_the_marker_section(tmp_path, monkeypatch, capsys):
    _seeded_report(tmp_path, monkeypatch)
    vallmon.save_rows({"2026-01-02": _term_formatted_row()})
    vallmon.README_PATH.write_text(
        "# Title\n\n%s\nold content\n%s\n\nfooter\n"
        % (vallmon.REPORT_START, vallmon.REPORT_END)
    )

    assert vallmon.cmd_report() == 0

    readme = vallmon.README_PATH.read_text()
    assert "old content" not in readme
    assert "# Title" in readme and "footer" in readme     # surroundings preserved
    assert vallmon.REPORT_START in readme and vallmon.REPORT_END in readme
    assert "**Δσ = 1.00**" in readme
    assert not (tmp_path / "README.md.tmp").exists()       # atomic write cleaned up

    report = vallmon.REPORT_PATH.read_text()
    assert "# VALL Rolling Variance Delta — Full Report" in report
    assert "## Full history" in report
    assert not (tmp_path / "REPORT.md.tmp").exists()
    assert "1 readings" in capsys.readouterr().out


def test_cmd_report_requires_markers(tmp_path, monkeypatch, capsys):
    _seeded_report(tmp_path, monkeypatch)
    vallmon.save_rows({"2026-01-02": _term_formatted_row()})
    vallmon.README_PATH.write_text("# No markers here\n")

    assert vallmon.cmd_report() == 1
    assert "markers" in capsys.readouterr().err


def test_cmd_report_without_readings_fails(tmp_path, monkeypatch, capsys):
    _seeded_report(tmp_path, monkeypatch)  # no CSV on disk

    assert vallmon.cmd_report() == 1
    assert "No readings yet" in capsys.readouterr().err


def test_cmd_history_prints_rows_and_trend(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vallmon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "delta_sigma.csv")
    vallmon.save_rows(
        {
            "2026-01-02": _term_formatted_row(day="2026-01-02"),
            "2026-01-03": _term_formatted_row(day="2026-01-03"),
        }
    )

    assert vallmon.cmd_history(2) == 0
    out = capsys.readouterr().out
    assert "last 2 readings" in out
    assert "2026-01-03" in out
    assert "trend" in out


def test_cmd_history_without_readings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vallmon, "CSV_PATH", tmp_path / "nope.csv")

    assert vallmon.cmd_history(5) == 0
    assert "No readings yet" in capsys.readouterr().out
