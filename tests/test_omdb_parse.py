"""OMDb value parsing (the '1,234,567' / 'N/A' gotcha)."""

from __future__ import annotations

import pytest

from homeTheater.metadata.omdb import parse_rating, parse_votes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7.8", 7.8), ("N/A", None), ("", None), (None, None), ("bad", None)],
)
def test_parse_rating(raw: object, expected: float | None) -> None:
    assert parse_rating(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234,567", 1234567),
        ("2,500", 2500),
        ("42", 42),
        ("N/A", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_votes(raw: object, expected: int | None) -> None:
    assert parse_votes(raw) == expected
