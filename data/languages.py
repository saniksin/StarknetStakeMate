"""Localization helpers.

Notable behavior: ``translate`` now falls back to English when the key is
missing in the user's locale. The previous implementation returned the raw
key, which leaked identifiers like ``pools_header`` into user messages.
"""
import json
import logging
from typing import Any

from data.all_paths import LOCALES_DIR


logger = logging.getLogger(__name__)

_DEFAULT_LOCALE = "en"


def load_locales() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for file in LOCALES_DIR.glob("*.json"):
        try:
            with open(file, "r", encoding="utf-8") as f:
                result[file.stem] = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Error loading locale file {file}: {exc}")
    return result


locales = load_locales()


def translate(key: str, locale: str = _DEFAULT_LOCALE, **format_args: Any) -> str:
    """Return the localized string for ``key``.

    Lookup order: requested locale → English → the key itself (with a
    warning). ``locale`` can be passed positionally or as a keyword — the
    latter form is heavily used across legacy handlers, so we intentionally
    do NOT make it positional-only.

    If ``format_args`` contain ``{placeholder}`` matches in the template,
    they're substituted. Any stray ``locale`` key accidentally left inside
    ``format_args`` (shouldn't happen; it's a real parameter) is dropped.
    """
    bundle = locales.get(locale)
    value = bundle.get(key) if bundle else None
    if value is None:
        en_bundle = locales.get(_DEFAULT_LOCALE, {})
        value = en_bundle.get(key)
    if value is None:
        logger.warning(f"Translation key '{key}' missing (locale={locale})")
        value = key
    if format_args:
        format_args.pop("locale", None)
        try:
            return value.format(**format_args)
        except (KeyError, IndexError):
            return value
    return value


possible_prefixes = ["en", "ru", "ua", "zh", "ko", "es", "de", "pl"]
possible_language = [
    "english",
    "русский",
    "українська",
    "中文",
    "한국어",
    "español",
    "deutsch",
    "polski",
]
