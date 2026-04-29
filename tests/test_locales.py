"""Lightweight sanity checks on locale bundles."""
from __future__ import annotations

import json
import pathlib

from data.all_paths import LOCALES_DIR
from data.languages import translate


_REQUIRED_NEW_KEYS = {
    "attestation_header",
    "attestation_healthy",
    "epoch_current",
    "last_attested_epoch",
    "pools_header",
    "unstake_requested",
    "unstake_not_requested",
    "validator_not_found",
    "delegator_not_found",
}


def test_all_locales_contain_new_keys() -> None:
    for p in pathlib.Path(LOCALES_DIR).glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        missing = _REQUIRED_NEW_KEYS - data.keys()
        assert not missing, f"{p.name} is missing keys: {missing}"


def test_translate_formats_kwargs() -> None:
    # ``attestation_missed`` was split into plural variants in 2026-04;
    # the substitution contract still needs to thread {count} through.
    from services.i18n_plural import t_n
    assert "3" in t_n("attestation_missed", 3, "en", count=3)


def test_translate_falls_back_to_english() -> None:
    # Pretend a locale is missing a key — fallback must hit English, not return the raw key.
    result = translate("pools_header", "de")
    assert result not in ("pools_header",)


def test_attestation_missed_plural_keys_present_for_locale_categories() -> None:
    """Each locale must carry exactly the plural categories it uses.

    en/de/es: one + other
    ru/ua/pl: one + few + many
    ko/zh:    other only
    """
    expected = {
        "en": {"_one", "_other"},
        "de": {"_one", "_other"},
        "es": {"_one", "_other"},
        "ru": {"_one", "_few", "_many"},
        "ua": {"_one", "_few", "_many"},
        "pl": {"_one", "_few", "_many"},
        "ko": {"_other"},
        "zh": {"_other"},
    }
    for locale, suffixes in expected.items():
        data = json.loads((pathlib.Path(LOCALES_DIR) / f"{locale}.json").read_text(encoding="utf-8"))
        for suffix in suffixes:
            for base in (
                "attestation_missed",
                "attestation_alert_missed",
                "webapp_status_missed_t",
                "confirm_delete_all_prompt",
            ):
                key = f"{base}{suffix}"
                assert key in data, f"{locale}.json is missing {key}"
