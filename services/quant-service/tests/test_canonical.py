"""Canonical hashing: the contract FinPilot re-computes independently."""
from stockquant_bank.canonical import (
    canonical_json,
    normalise_nav_series,
    series_source_hash,
    sha256_json,
)


def test_hash_ignores_point_order():
    a = [{"date": "2026-01-02", "value": 1.0}, {"date": "2026-01-03", "value": 1.01}]
    b = list(reversed(a))
    assert series_source_hash(a) == series_source_hash(b)


def test_hash_ignores_float_representation_noise():
    a = [{"date": "2026-01-02", "value": 1.0}]
    b = [{"date": "2026-01-02", "value": 1.0000000001}]
    assert series_source_hash(a) == series_source_hash(b)


def test_hash_changes_when_a_value_changes():
    a = [{"date": "2026-01-02", "value": 1.0}]
    b = [{"date": "2026-01-02", "value": 1.0001}]
    assert series_source_hash(a) != series_source_hash(b)


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert sha256_json({"b": 1, "a": 2}) == sha256_json({"a": 2, "b": 1})


def test_normalise_rounds_before_hashing():
    rows = normalise_nav_series([{"date": "2026-01-02", "value": 1.23456789}])
    assert rows[0]["value"] == 1.234568