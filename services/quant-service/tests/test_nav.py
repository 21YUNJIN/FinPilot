"""Data-quality gates: a bad series must fail loudly, not silently."""
import pytest

from stockquant_bank.errors import UnprocessableData
from stockquant_bank.nav import validate_series


def points(*rows):
    return [{"date": d, "value": v} for d, v in rows]


def test_valid_series_sorts_and_hashes():
    series = validate_series("P1", points(("2026-01-03", 1.01), ("2026-01-02", 1.0)), as_of="2026-01-04")
    assert series.dates == ["2026-01-02", "2026-01-03"]
    assert len(series.source_hash) == 64


def test_duplicate_date_is_rejected():
    with pytest.raises(UnprocessableData) as exc:
        validate_series("P1", points(("2026-01-02", 1.0), ("2026-01-02", 1.01)), as_of="2026-01-04")
    assert "多条净值" in exc.value.safe_message


def test_zero_or_negative_nav_is_rejected():
    with pytest.raises(UnprocessableData):
        validate_series("P1", points(("2026-01-02", 0.0)), as_of="2026-01-04")


def test_future_data_is_rejected():
    with pytest.raises(UnprocessableData) as exc:
        validate_series("P1", points(("2026-01-05", 1.0)), as_of="2026-01-04")
    assert "未来" in exc.value.safe_message


def test_bad_date_format_is_rejected():
    with pytest.raises(UnprocessableData):
        validate_series("P1", points(("02/01/2026", 1.0)), as_of="2026-01-04")


def test_stale_series_warns_but_still_computes():
    series = validate_series("P1", points(("2025-12-01", 1.0), ("2025-12-02", 1.01)), as_of="2026-01-15")
    assert any("距 as_of" in warning for warning in series.quality["warnings"])


def test_long_gap_warns():
    series = validate_series(
        "P1",
        points(("2025-01-02", 1.0), ("2025-03-03", 1.02)),
        as_of="2025-03-04",
    )
    assert series.quality["gaps"]
    assert any("缺口" in warning for warning in series.quality["warnings"])


def test_too_many_points_is_rejected():
    rows = [{"date": f"2020-{1 + i // 280:02d}-{1 + i % 28:02d}", "value": 1.0 + i / 1e6} for i in range(5001)]
    with pytest.raises(UnprocessableData):
        validate_series("P1", rows, as_of="2026-01-01")