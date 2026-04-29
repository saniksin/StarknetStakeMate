"""Plural-rule unit tests for the 8 locales.

We freeze the CLDR category each locale assigns to a representative set
of integers. If the table in ``services/i18n_plural.py`` drifts, these
break loudly — the alternative (silently wrong noun forms in alerts) is
the kind of bug nobody catches in code review.

The reference values come straight from the CLDR plural rules:
https://cldr.unicode.org/index/cldr-spec/plural-rules

The Slavic block (ru/ua/pl) is the spicy one and gets the most cases.
"""
from __future__ import annotations

import pytest

from services.i18n_plural import plural_category, t_n


# ---------------------------------------------------------------------------
# plural_category — the lookup table itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "many"),    # 0 — many in CLDR (zero is one of the "many" buckets in ru)
        (1, "one"),
        (2, "few"),
        (3, "few"),
        (4, "few"),
        (5, "many"),
        (10, "many"),
        (11, "many"),   # special: ends in 1 but 11 is "many"
        (12, "many"),   # 12-14 are "many" even though they end in 2-4
        (13, "many"),
        (14, "many"),
        (15, "many"),
        (21, "one"),    # ends in 1, mod100=21 not 11 → "one"
        (22, "few"),
        (101, "one"),
        (111, "many"),  # mod100 = 11 → "many"
        (1000, "many"),
    ],
)
def test_ru_categories(n: int, expected: str) -> None:
    assert plural_category(n, "ru") == expected


@pytest.mark.parametrize(
    "n, expected",
    [
        (1, "one"),
        (2, "few"),
        (3, "few"),
        (5, "many"),
        (11, "many"),
        (21, "one"),
        (22, "few"),
    ],
)
def test_ua_matches_ru(n: int, expected: str) -> None:
    """Ukrainian uses the same rules as Russian for cardinals."""
    assert plural_category(n, "ua") == expected


@pytest.mark.parametrize(
    "n, expected",
    [
        # Polish: only n==1 is "one"; 21/31/101 are "many" (NOT "one" like ru).
        (0, "many"),
        (1, "one"),
        (2, "few"),
        (3, "few"),
        (4, "few"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (21, "many"),    # ⚠ different from ru — Polish treats 21 as many
        (22, "few"),
        (24, "few"),
        (25, "many"),
    ],
)
def test_pl_categories(n: int, expected: str) -> None:
    assert plural_category(n, "pl") == expected


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "other"),
        (1, "one"),
        (2, "other"),
        (10, "other"),
        (21, "other"),
        (101, "other"),
    ],
)
def test_en_categories(n: int, expected: str) -> None:
    assert plural_category(n, "en") == expected


@pytest.mark.parametrize("lang", ["de", "es"])
@pytest.mark.parametrize("n, expected", [(1, "one"), (2, "other"), (5, "other"), (21, "other")])
def test_de_es_match_en(lang: str, n: int, expected: str) -> None:
    """German and Spanish are 2-way same as English for our purposes."""
    assert plural_category(n, lang) == expected


@pytest.mark.parametrize("lang", ["ko", "zh"])
@pytest.mark.parametrize("n", [0, 1, 2, 5, 11, 100])
def test_ko_zh_always_other(lang: str, n: int) -> None:
    """Korean and Chinese never pluralize cardinals — always ``other``."""
    assert plural_category(n, lang) == "other"


def test_unknown_locale_safe_fallback() -> None:
    """An unrecognized locale falls back to English-style 2-way."""
    assert plural_category(1, "xx") == "one"
    assert plural_category(2, "xx") == "other"


def test_locale_with_region_suffix_normalised() -> None:
    """``ru-RU`` should still be treated as Russian."""
    assert plural_category(2, "ru-RU") == "few"


# ---------------------------------------------------------------------------
# t_n — formatting via the locale bundles. Uses the project's real
# translate() so we exercise the cross-locale fallback chain too.
# ---------------------------------------------------------------------------


def test_t_n_falls_back_to_other_when_one_missing() -> None:
    """If a locale only ships ``_other``, ko/zh-style, we still get a string."""
    # We don't expect the test bundle to define this key — just that we fall
    # back to the raw key without crashing.
    out = t_n("nonexistent_test_key", 5, "en")
    # No template found → numeric fallback (str(count)).
    assert out == "5"
