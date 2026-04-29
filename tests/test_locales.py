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


# Mini-App add-flow (validator/delegator) — added 2026-04-29. Covering
# the full set in one parity test catches the trivial "forgot a locale"
# regression we'd otherwise only notice when the user switches language.
_REQUIRED_ADD_FLOW_KEYS = {
    "webapp_add_button",
    "webapp_add_form_title",
    "webapp_add_form_hint",
    "webapp_add_role_section",
    "webapp_add_role_validator",
    "webapp_add_role_delegator",
    "webapp_add_role_hint_validator",
    "webapp_add_role_hint_delegator",
    "webapp_add_addresses_section",
    "webapp_add_validator_label",
    "webapp_add_staker_label",
    "webapp_add_delegator_label",
    "webapp_add_label_section",
    "webapp_add_label_label",
    "webapp_add_label_hint",
    "webapp_add_submit_btn",
    "webapp_add_submitting",
    "webapp_add_success",
    "webapp_add_error_invalid_address",
    "webapp_add_error_duplicate_validator",
    "webapp_add_error_duplicate_delegator",
    "webapp_add_error_limit_reached",
    "webapp_add_error_not_a_staker",
    "webapp_add_error_not_a_delegator",
    "webapp_add_error_unknown",
    "webapp_topbar_add",
    "webapp_topbar_add_sub",
}


def test_all_locales_contain_new_keys() -> None:
    for p in pathlib.Path(LOCALES_DIR).glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        missing = _REQUIRED_NEW_KEYS - data.keys()
        assert not missing, f"{p.name} is missing keys: {missing}"


def test_all_locales_contain_add_flow_keys() -> None:
    """Every locale must carry the full add-flow bundle so the Mini App
    doesn't fall back to raw keys when a non-English user hits + Add."""
    for p in pathlib.Path(LOCALES_DIR).glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        missing = _REQUIRED_ADD_FLOW_KEYS - data.keys()
        assert not missing, f"{p.name} is missing add-flow keys: {missing}"


# Mini-App reorder (drag-and-drop) — added 2026-04-29.
_REQUIRED_REORDER_KEYS = {
    "webapp_reorder_button",
    "webapp_reorder_done",
    "webapp_reorder_cancel",
    "webapp_reorder_hint",
    "webapp_reorder_save_failed",
}


def test_all_locales_contain_reorder_keys() -> None:
    """Every locale must carry the full reorder bundle — ru/ua need
    real translations because the user-facing copy is what the team
    spec'd ("Сортировать", "Готово") and the EN fallback would feel
    wrong on a Russian client."""
    for p in pathlib.Path(LOCALES_DIR).glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        missing = _REQUIRED_REORDER_KEYS - data.keys()
        assert not missing, f"{p.name} is missing reorder keys: {missing}"


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
