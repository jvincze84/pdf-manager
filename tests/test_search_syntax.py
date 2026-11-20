"""Unit tests for the search-string -> FTS5 MATCH translation (no database needed)."""
import pytest

from app.search import build_match


@pytest.mark.parametrize("query, expected", [
    ("invoice", '"invoice"*'),
    ("invoice 2024", '"invoice"* AND "2024"*'),
    ('"exact phrase"', '"exact phrase"'),
    ('foo "exact phrase" bar', '"foo"* AND "exact phrase" AND "bar"*'),
    ("invoice -draft", '("invoice"*) NOT ("draft"*)'),
    ('invoice -"old version"', '("invoice"*) NOT ("old version")'),
    ("tax OR vat", '("tax"* OR "vat"*)'),
    ("e-mail", '"e mail"*'),
    ('say "he said ""hi"""', None),  # must not raise; exact output irrelevant
])
def test_build_match(query, expected):
    result = build_match(query)
    if expected is not None:
        assert result == expected


@pytest.mark.parametrize("query", ["", "   ", '"', '""', "-", "-onlyneg", "OR", "!!!", "( )"])
def test_build_match_nothing_to_search(query):
    assert build_match(query) is None


def test_unclosed_quote_is_phrase():
    assert build_match('"unclosed phrase') == '"unclosed phrase"'
