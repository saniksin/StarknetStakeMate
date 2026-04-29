"""CLDR-style plural rules for the 8 locales the bot supports.

Picks the plural category (``"one"`` / ``"few"`` / ``"many"`` / ``"other"``)
for an integer count in a given locale, and resolves a ``(key_base, count,
locale)`` tuple to the right localized template — applying ``str.format``
with the count and any extra kwargs.

The 4 categories cover everything the project needs:

- ``en``, ``de``, ``es``: ``one`` (n == 1), ``other`` (everything else).
- ``ru``, ``ua``, ``pl``: ``one`` (n%10==1 && n%100!=11),
  ``few`` (n%10 in 2..4 && n%100 not in 12..14),
  ``many`` (everything else).
- ``ko``, ``zh``: ``other`` only — these languages don't pluralize.

Why a hand-rolled table instead of pulling in ``babel``: babel adds ~12 MB
to the runtime image and we already hand-curate every locale. The CLDR
rules for the 6 non-trivial languages here are stable since 2010 and the
test suite freezes the expected categories so a regression would be
loud.

Usage::

    from services.i18n_plural import t_n
    t_n("att_blocks", 1, "ru")    # → "1 блок"
    t_n("att_blocks", 3, "ru")    # → "3 блока"
    t_n("att_blocks", 11, "ru")   # → "11 блоков"
    t_n("att_blocks", 1, "en")    # → "1 block"
    t_n("att_blocks", 5, "en")    # → "5 blocks"

The lookup expects ``{key_base}_{category}`` keys to exist in the locale
file. Falls back to ``{key_base}_other`` and finally to ``{key_base}`` if
nothing matches; that means new locale entries can ship a single
``key_base_other`` for ko/zh while ru/ua/pl carry the full triple.
"""
from __future__ import annotations

from typing import Any, Literal

from data.languages import translate

PluralCategory = Literal["one", "few", "many", "other"]


def plural_category(n: int, locale: str) -> PluralCategory:
    """Return the CLDR plural category for ``n`` in ``locale``.

    ``n`` is treated as a non-negative integer. We don't bother with the
    fractional ``v`` / ``f`` operands because every count we render
    (blocks, minutes, seconds, epochs) is a whole number.
    """
    n = abs(int(n))
    lang = (locale or "en").lower().split("-")[0]

    # Slavic 3-way: one / few / many.
    if lang in ("ru", "ua", "uk"):
        mod10 = n % 10
        mod100 = n % 100
        if mod10 == 1 and mod100 != 11:
            return "one"
        if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            return "few"
        return "many"

    # Polish: same shape as Slavic 3-way, with a slightly different
    # boundary on the 1-form (only n==1, not "n%10==1 && n%100!=11"). For
    # whole numbers the practical effect is the same as ru/ua except n==1
    # is the sole "one" case (e.g. 21 → many, not one). We mirror CLDR
    # exactly to keep the contract obvious.
    if lang == "pl":
        if n == 1:
            return "one"
        mod10 = n % 10
        mod100 = n % 100
        if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
            return "few"
        return "many"

    # 2-way: one / other.
    if lang in ("en", "de", "es"):
        return "one" if n == 1 else "other"

    # No-plural languages.
    if lang in ("ko", "zh"):
        return "other"

    # Unknown locale → safe English-style 2-way.
    return "one" if n == 1 else "other"


def t_n(
    key_base: str,
    n: int,
    locale: str = "en",
    /,
    **format_args: Any,
) -> str:
    """Pick a pluralized translation for ``n`` and format it.

    The template is looked up under ``{key_base}_{category}``; if that
    isn't present in the locale (or the English fallback) we drop to
    ``{key_base}_other`` and finally to ``{key_base}`` — which lets ko/zh
    carry only the ``_other`` form without forcing every other locale to
    duplicate it.

    The substitution always seeds ``count=n`` and ``n=n`` placeholders
    so templates can reference either; explicit kwargs in
    ``format_args`` override them. The first three parameters are
    positional-only to free up ``count`` / ``locale`` as legitimate
    template variables.
    """
    category = plural_category(n, locale)
    candidates = [f"{key_base}_{category}"]
    if category != "other":
        candidates.append(f"{key_base}_other")
    candidates.append(key_base)

    # Default ``count`` and ``n`` to ``n``; caller-supplied kwargs win.
    merged: dict[str, Any] = {"count": n, "n": n}
    merged.update(format_args)

    # Pull translations and pick the first one that resolved to something
    # other than the raw key (which means the bundle had nothing). Use the
    # standard ``translate`` so the cross-locale English fallback still
    # applies.
    for candidate in candidates:
        rendered = translate(candidate, locale, **merged)
        if rendered != candidate:
            return rendered
    # Final fallback: just the count. Not a typical path; means the locale
    # is missing every variant of the key.
    return str(n)


__all__ = ["plural_category", "t_n", "PluralCategory"]
