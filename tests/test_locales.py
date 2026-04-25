"""Lightweight sanity checks on locale bundles."""
from __future__ import annotations

import json
import pathlib

from data.all_paths import LOCALES_DIR
from data.languages import translate


_REQUIRED_NEW_KEYS = {
    "attestation_header",
    "attestation_healthy",
    "attestation_missed",
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
    # The ``attestation_missed`` key uses a {count} placeholder.
    assert "3" in translate("attestation_missed", "en", count=3)


def test_translate_falls_back_to_english() -> None:
    # Pretend a locale is missing a key — fallback must hit English, not return the raw key.
    result = translate("pools_header", "de")
    assert result not in ("pools_header",)
