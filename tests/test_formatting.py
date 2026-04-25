"""Smoke tests for Telegram HTML formatters."""
from __future__ import annotations

from decimal import Decimal

from services.formatting import (
    _active_pools,
    _fmt_amount,
    _fmt_percent_bps,
    _fmt_relative,
    _short,
    render_attestation,
    render_delegator_card,
    render_validator_card,
)
from services.staking_dto import (
    AttestationStatus,
    DelegatorInfo,
    DelegatorMultiPositions,
    PoolInfoDto,
    ValidatorInfo,
)
from services.tracking_service import TrackingEntry


def test_short_addr_long() -> None:
    addr = "0x" + "a" * 64
    assert _short(addr).startswith("0xaaaaaa")
    assert _short(addr).endswith("aaaa")


def test_fmt_amount_large_uses_thousands_and_two_decimals() -> None:
    # >= 1 → 2 decimals, ',' as thousands separator.
    assert _fmt_amount(Decimal("1"), "STRK") == "1.00 STRK"
    assert _fmt_amount(Decimal("101219.340359257303"), "STRK") == "101,219.34 STRK"
    assert _fmt_amount(Decimal("2959097.23117"), "STRK") == "2,959,097.23 STRK"


def test_fmt_amount_small_keeps_six_digits() -> None:
    # < 1 → up to 6 fractional digits; trailing zeros stripped.
    assert _fmt_amount(Decimal("0.5"), "STRK") == "0.5 STRK"
    assert _fmt_amount(Decimal("0.01210429"), "WBTC") == "0.012104 WBTC"


def test_fmt_amount_zero() -> None:
    assert _fmt_amount(Decimal(0), "STRK") == "0 STRK"


def test_fmt_percent_bps_edges() -> None:
    assert _fmt_percent_bps(None) == "—"
    assert _fmt_percent_bps(1500) == "15.00%"
    assert _fmt_percent_bps(0) == "0.00%"


def test_fmt_relative_none() -> None:
    assert _fmt_relative(None) == "—"


def test_render_attestation_missed_formatting() -> None:
    st = AttestationStatus(
        last_epoch_attested=9470,
        current_epoch=9475,
        missed_epochs=4,
        is_attesting_this_epoch=False,
    )
    out = render_attestation(st, "en")
    assert "Missed 4" in out
    assert "⚠️" in out


def _mk_entry(info: ValidatorInfo, label: str = "Karnot") -> TrackingEntry:
    return TrackingEntry(
        index=0,
        kind="validator",
        address=info.staker_address,
        pool="",
        label=label,
        data=info,
    )


def test_render_validator_card_with_label_and_multi_pool() -> None:
    info = ValidatorInfo(
        staker_address="0x" + "a" * 64,
        reward_address="0x" + "b" * 64,
        operational_address="0x" + "c" * 64,
        amount_own_raw=10**23,
        amount_own_strk=Decimal("100000"),
        unclaimed_rewards_own_raw=0,
        unclaimed_rewards_own_strk=Decimal(0),
        commission_bps=1500,
        current_epoch=9474,
        pools=[
            PoolInfoDto(
                pool_contract="0x01",
                token_address="0x02",
                token_symbol="STRK",
                amount_raw=0,
                amount_decimal=Decimal("2500000"),
            ),
            PoolInfoDto(
                pool_contract="0x03",
                token_address="0x04",
                token_symbol="WBTC",
                amount_raw=0,
                amount_decimal=Decimal("0.1"),
            ),
        ],
    )
    out = render_validator_card(_mk_entry(info), "en")
    # Label in header
    assert "Karnot" in out
    # Own stake in table uses full precision
    assert "100,000.00 STRK" in out
    # Pools are now inline compressed: 2.5M not 2,500,000.00
    assert "2.50M" in out
    # Small BTC amount kept readable
    assert "0.1" in out
    assert "WBTC" in out
    # Commission bps rendered as 15.00%
    assert "15.00%" in out
    # Pools section inline label present
    assert "Pools" in out
    # Full staker address in <code> block (not short)
    assert "0x" + "a" * 64 in out


def test_render_validator_card_without_label_falls_back_to_short_addr() -> None:
    addr = "0x" + "1" * 64
    info = ValidatorInfo(
        staker_address=addr,
        reward_address=addr,
        operational_address=addr,
        amount_own_raw=0,
        amount_own_strk=Decimal("1.23"),
        unclaimed_rewards_own_raw=0,
        unclaimed_rewards_own_strk=Decimal(0),
        current_epoch=1,
    )
    out = render_validator_card(
        TrackingEntry(index=0, kind="validator", address=addr, pool="", label="", data=info),
        "en",
    )
    # No label → we inject an abbreviated address in the header.
    assert addr[:8] in out
    assert addr[-4:] in out


# ---------------------------------------------------------------------------
# Bug #1 — active pool count (single source of truth)
# ---------------------------------------------------------------------------

def test_active_pools_filters_empty() -> None:
    """_active_pools must exclude pools with zero stake."""
    pools = [
        PoolInfoDto(pool_contract="0x01", token_address="0x02", token_symbol="STRK",
                    amount_raw=100, amount_decimal=Decimal("2960000")),
        PoolInfoDto(pool_contract="0x03", token_address="0x04", token_symbol="WBTC",
                    amount_raw=0, amount_decimal=Decimal("0")),
        PoolInfoDto(pool_contract="0x05", token_address="0x06", token_symbol="LBTC",
                    amount_raw=0, amount_decimal=Decimal("0")),
    ]
    active = _active_pools(pools)
    assert len(active) == 1
    assert active[0].token_symbol == "STRK"


def test_validator_card_pool_inline_omits_empty_pools() -> None:
    """The validator card pool line must NOT mention empty pools."""
    info = ValidatorInfo(
        staker_address="0x" + "a" * 64,
        reward_address="0x" + "b" * 64,
        operational_address="0x" + "c" * 64,
        amount_own_raw=0,
        amount_own_strk=Decimal("50000"),
        unclaimed_rewards_own_raw=0,
        unclaimed_rewards_own_strk=Decimal("0"),
        commission_bps=1000,
        current_epoch=9500,
        pools=[
            PoolInfoDto(pool_contract="0x01", token_address="0x02", token_symbol="STRK",
                        amount_raw=1, amount_decimal=Decimal("2960000")),
            PoolInfoDto(pool_contract="0x03", token_address="0x04", token_symbol="WBTC",
                        amount_raw=0, amount_decimal=Decimal("0")),  # empty
            PoolInfoDto(pool_contract="0x05", token_address="0x06", token_symbol="LBTC",
                        amount_raw=0, amount_decimal=Decimal("0")),  # empty
        ],
    )
    out = render_validator_card(_mk_entry(info), "en")
    # Active pool shown
    assert "STRK" in out
    # Empty pools must NOT appear
    assert "empty" not in out
    assert "WBTC" not in out
    assert "LBTC" not in out


# ---------------------------------------------------------------------------
# Bug #2 — self-stake address collapse in delegator card
# ---------------------------------------------------------------------------

def _mk_delegator_entry(
    multi: DelegatorMultiPositions, label: str = "saniksin"
) -> TrackingEntry:
    return TrackingEntry(
        index=1,
        kind="delegator",
        address=multi.delegator_address,
        pool=multi.staker_address,
        label=label,
        data=multi,
    )


def _mk_delegator_position(
    delegator: str,
    pool: str,
    *,
    token_symbol: str = "STRK",
    amount: Decimal = Decimal("28000"),
    rewards: Decimal = Decimal("170"),
    commission_bps: int = 1500,
) -> DelegatorInfo:
    return DelegatorInfo(
        delegator_address=delegator,
        pool_contract=pool,
        token_symbol=token_symbol,
        token_address="0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d",
        reward_address=delegator,
        amount_raw=int(amount * 10**18),
        amount_decimal=amount,
        unclaimed_rewards_raw=int(rewards * 10**18),
        unclaimed_rewards_decimal=rewards,
        commission_bps=commission_bps,
    )


def test_delegator_card_self_stake_shows_one_address_with_annotation() -> None:
    """When delegator == staker (self-stake), only one address must appear
    and it must carry '(self-stake)' annotation."""
    addr = "0x" + "f" * 64
    pos = _mk_delegator_position(addr, "0xpool1")
    multi = DelegatorMultiPositions(
        delegator_address=addr,
        staker_address=addr,  # same → self-stake
        positions=[pos],
    )
    out = render_delegator_card(_mk_delegator_entry(multi), "en")
    # Address must appear (full, in <code>)
    assert addr in out
    # Self-stake annotation
    assert "self-stake" in out
    # Must NOT show the address twice (count occurrences of the raw hex)
    assert out.count(addr) == 1


def test_delegator_card_non_self_stake_shows_staker_address() -> None:
    """When delegator != staker, the staker (validator) address must be shown."""
    delegator_addr = "0x" + "1" * 64
    staker_addr = "0x" + "2" * 64
    pos = _mk_delegator_position(delegator_addr, "0xpool1")
    multi = DelegatorMultiPositions(
        delegator_address=delegator_addr,
        staker_address=staker_addr,
        positions=[pos],
    )
    out = render_delegator_card(_mk_delegator_entry(multi), "en")
    # Staker address shown
    assert staker_addr in out
    # No self-stake annotation
    assert "self-stake" not in out


def test_delegator_card_bank_uses_bank_icon() -> None:
    """A delegator whose only non-zero position is WBTC gets the 🏦 icon."""
    delegator_addr = "0x" + "3" * 64
    staker_addr = "0x" + "4" * 64
    pos = _mk_delegator_position(
        delegator_addr, "0xpool2", token_symbol="WBTC", amount=Decimal("0.012104")
    )
    multi = DelegatorMultiPositions(
        delegator_address=delegator_addr,
        staker_address=staker_addr,
        positions=[pos],
    )
    out = render_delegator_card(_mk_delegator_entry(multi, label="bank"), "en")
    assert "🏦" in out
