"""Smoke tests for Telegram HTML formatters."""
from __future__ import annotations

from decimal import Decimal

import pytest

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
from services.i18n_plural import t_n
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


# ---------------------------------------------------------------------------
# Extended attestation banner — block window + epoch tail. The renderer
# is shared between the four status branches (waiting / healthy / missed
# / exiting) so every state gets the same "next epoch in N blocks" tail.
# ---------------------------------------------------------------------------

from services.staking_dto import EpochTimeline  # noqa: E402


def _mk_timeline(
    blocks_left: int = 810, seconds_left: int = 35 * 60,
    current_epoch: int = 9590,
) -> EpochTimeline:
    return EpochTimeline(
        current_epoch=current_epoch,
        next_epoch=current_epoch + 1,
        next_epoch_block=9_283_224 + blocks_left,
        current_block=9_283_224,
        blocks_left_in_epoch=blocks_left,
        seconds_left_in_epoch=seconds_left,
        epoch_length_blocks=1389,
        epoch_duration_seconds=3600,
    )


def test_attestation_waiting_renders_block_window_en() -> None:
    """B.2 from the UX catalogue — waiting state with full block info."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=False,
        target_block=9_283_500,
        attestation_window_blocks=60,
        current_block=9_283_540,
    )
    out = render_attestation(att, "en", timeline=_mk_timeline())
    assert "Current block" in out
    assert "9_283_540" in out
    assert "Assigned block" in out
    assert "9_283_500" in out
    assert "9_283_560" in out  # window close
    # Tail with next epoch
    assert "9591" in out
    # Plural noun forms came from t_n (en: "blocks" for 810).
    assert "blocks" in out


def test_attestation_waiting_renders_block_window_ru_with_plurals() -> None:
    """B.1 — Russian waiting message uses correct plural noun forms."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=False,
        target_block=9_283_500,
        attestation_window_blocks=60,
        current_block=9_283_540,
    )
    out = render_attestation(att, "ru", timeline=_mk_timeline())
    assert "Текущий блок" in out
    assert "Целевой блок" in out
    assert "Окно подписи" in out
    # 810 → "many" form in ru ("блоков")
    assert "810 блоков" in out
    # Tail in ru
    assert "До эпохи 9591" in out


def test_attestation_attested_renders_tail_only() -> None:
    """B.4 — already attested: short message + epoch tail in every locale."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=True,
        target_block=None,  # not relevant in attested state
        attestation_window_blocks=60,
        current_block=9_283_540,
    )
    out = render_attestation(att, "en", timeline=_mk_timeline())
    assert "Already attested" in out
    # Block-window detail must NOT appear in attested state.
    assert "Sign window" not in out
    assert "Current block" not in out
    # But the epoch tail must.
    assert "9591" in out
    assert "blocks" in out


def test_attestation_missed_renders_tail_in_ru() -> None:
    """B.5 — missed state: ru pluralization on missed count + tail."""
    att = AttestationStatus(
        last_epoch_attested=9588,
        current_epoch=9590,
        missed_epochs=1,
        is_attesting_this_epoch=False,
        target_block=None,
        attestation_window_blocks=60,
        current_block=9_283_540,
    )
    out = render_attestation(att, "ru", timeline=_mk_timeline())
    assert "9588" in out
    assert "До эпохи 9591" in out


def test_attestation_no_timeline_drops_tail() -> None:
    """When EpochInfo / chain head fetch fails, no tail appears."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=False,
        target_block=9_283_500,
        attestation_window_blocks=60,
        current_block=9_283_540,
    )
    out = render_attestation(att, "en", timeline=None)
    # Block window still rendered (those fields are present)
    assert "Current block" in out
    # But no "next epoch" tail
    assert "Next epoch" not in out


def test_attestation_window_closed_branch() -> None:
    """When current_block > sign_window_close → "window closed" message."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=False,
        target_block=9_283_500,
        attestation_window_blocks=60,
        current_block=9_283_700,  # past target+60
    )
    out = render_attestation(att, "en", timeline=_mk_timeline())
    assert "Window closed" in out


def test_attestation_no_block_info_falls_back_to_simple_waiting() -> None:
    """Without target_block we degrade to the single-line waiting template."""
    att = AttestationStatus(
        last_epoch_attested=9589,
        current_epoch=9590,
        missed_epochs=0,
        is_attesting_this_epoch=False,
        # target_block / window / current_block all None
    )
    out = render_attestation(att, "en", timeline=_mk_timeline())
    assert "Awaiting attestation in epoch 9590" in out
    # No block-level rows
    assert "Current block" not in out


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


# ---------------------------------------------------------------------------
# Plural noun forms — ``attestation_missed`` / ``attestation_alert_missed``
# / ``webapp_status_missed_t`` / ``confirm_delete_all_prompt``. The
# previous renderer printed "Missed 3 epoch(s)" verbatim. The plural
# table now picks the correct noun form per CLDR category in each
# locale; these tests freeze the expected wording so a regression
# (silently dropping a category from the locale file, or a JSON typo)
# would break the build instead of shipping ungrammatical alerts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, expected",
    [
        (1, "1 epoch since last attestation"),
        (3, "3 epochs since last attestation"),
        (5, "5 epochs since last attestation"),
    ],
)
def test_attestation_missed_plural_en(n: int, expected: str) -> None:
    out = t_n("attestation_missed", n, "en")
    assert expected in out


@pytest.mark.parametrize(
    "n, expected_noun",
    [
        (1, "эпоху"),    # one
        (3, "эпохи"),    # few
        (5, "эпох"),     # many
        (11, "эпох"),    # many (special: 11 ends in 1 but mod100=11 → many)
        (21, "эпоху"),   # one (21 mod 100 = 21, mod 10 = 1)
    ],
)
def test_attestation_missed_plural_ru(n: int, expected_noun: str) -> None:
    out = t_n("attestation_missed", n, "ru")
    assert f"{n} {expected_noun}" in out


@pytest.mark.parametrize(
    "n, expected_noun",
    [
        (1, "epokę"),    # one (Polish: only n==1)
        (3, "epoki"),    # few
        (5, "epok"),     # many
        (21, "epok"),    # many (Polish 21 != one)
    ],
)
def test_attestation_missed_plural_pl(n: int, expected_noun: str) -> None:
    out = t_n("attestation_missed", n, "pl")
    assert f"{n} {expected_noun}" in out


def test_attestation_missed_singleform_ko() -> None:
    """Korean has no cardinal pluralization — single template covers all n."""
    for n in (1, 3, 5, 11):
        out = t_n("attestation_missed", n, "ko")
        # Just check the {count} got injected.
        assert str(n) in out


def test_attestation_alert_missed_plural_ru() -> None:
    """Push-notification text picks correct ru noun forms."""
    label = "Karnot"
    epoch = 9590
    one = t_n("attestation_alert_missed", 1, "ru", label=label, epoch=epoch)
    few = t_n("attestation_alert_missed", 3, "ru", label=label, epoch=epoch)
    many = t_n("attestation_alert_missed", 5, "ru", label=label, epoch=epoch)
    assert "1</b> эпоха" in one or "1 эпоха" in one
    assert "3</b> эпохи" in few or "3 эпохи" in few
    assert "5</b> эпох" in many or "5 эпох" in many


def test_webapp_status_missed_t_plural_ru() -> None:
    """Webapp banner title gets the correct ru noun case."""
    assert "1 аттестацию" in t_n("webapp_status_missed_t", 1, "ru")
    assert "3 аттестации" in t_n("webapp_status_missed_t", 3, "ru")
    assert "5 аттестаций" in t_n("webapp_status_missed_t", 5, "ru")


def test_webapp_status_missed_t_plural_en() -> None:
    """Banner title plural in en."""
    assert "missed 1 attestation" in t_n("webapp_status_missed_t", 1, "en")
    assert "missed 3 attestations" in t_n("webapp_status_missed_t", 3, "en")


def test_confirm_delete_all_prompt_plural_ru() -> None:
    """Delete-all confirmation: ru must use the right noun case for n addresses."""
    assert "1 отслеживаемый адрес" in t_n("confirm_delete_all_prompt", 1, "ru")
    assert "3 отслеживаемых адреса" in t_n("confirm_delete_all_prompt", 3, "ru")
    assert "5 отслеживаемых адресов" in t_n("confirm_delete_all_prompt", 5, "ru")


def test_render_attestation_uses_plural_for_missed_count() -> None:
    """End-to-end through the renderer: ru bot card with missed=3 should
    print "эпохи" (few), not "эпох" (many) or "эпох(а)" (legacy hack)."""
    att = AttestationStatus(
        last_epoch_attested=9587,
        current_epoch=9590,
        missed_epochs=3,
        is_attesting_this_epoch=False,
    )
    out = render_attestation(att, "ru")
    assert "3 эпохи" in out
    assert "эпох(" not in out  # no legacy "(s)" / "(a)" hack remnants

    out_en = render_attestation(att, "en")
    assert "3 epochs" in out_en
    assert "epoch(s)" not in out_en
