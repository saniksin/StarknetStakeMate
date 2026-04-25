"""Unit tests for pure DTO helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from services.staking_dto import (
    PoolInfoDto,
    ValidatorInfo,
    build_unstake_datetime,
    raw_to_decimal,
)


def test_raw_to_decimal_strk_scale() -> None:
    assert raw_to_decimal(10**18, 18) == Decimal(1)
    assert raw_to_decimal(3_780_449_635_532_783_122_315, 18) == Decimal(
        "3780.449635532783122315"
    )


def test_raw_to_decimal_btc_scale() -> None:
    # WBTC uses 8 decimals.
    assert raw_to_decimal(12_345_678, 8) == Decimal("0.12345678")


def test_build_unstake_datetime_some() -> None:
    ts = build_unstake_datetime({"seconds": 1_700_000_000})
    assert ts == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_build_unstake_datetime_none() -> None:
    assert build_unstake_datetime(None) is None
    assert build_unstake_datetime({}) is None


def test_validator_eta_no_unstake() -> None:
    info = ValidatorInfo(
        staker_address="0x1",
        reward_address="0x2",
        operational_address="0x3",
        amount_own_raw=0,
        amount_own_strk=Decimal(0),
        unclaimed_rewards_own_raw=0,
        unclaimed_rewards_own_strk=Decimal(0),
        current_epoch=100,
    )
    assert info.unstake_eta is None
    assert info.pools == []


def test_pool_info_serializable() -> None:
    p = PoolInfoDto(
        pool_contract="0xabc",
        token_address="0xdef",
        token_symbol="STRK",
        amount_raw=123,
        amount_decimal=Decimal("0.000000000000000123"),
    )
    as_json = p.model_dump(mode="json")
    assert as_json["token_symbol"] == "STRK"
    assert as_json["amount_raw"] == 123
