"""Pydantic DTOs returned by the service layer.

These are the *only* shapes consumed by the Telegram bot handlers and the
REST API. Raw contract tuples/dicts are confined to the service modules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _utc_from_secs(seconds: int | None) -> datetime | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


class TokenInfo(BaseModel):
    """Metadata for a staking-eligible token (STRK or a BTC wrapper)."""

    address: str
    symbol: str | None = None           # "STRK" / "WBTC" / "LBTC" / "tBTC" / "SolvBTC"
    decimals: int = 18                  # STRK=18, most BTC wrappers=8
    enabled: bool = True                # `disabled` tokens keep working for existing stakes


class PoolInfoDto(BaseModel):
    """Represents one delegation pool owned by a staker (V2 multi-token).

    A V1 staker had at most one pool (STRK). After the V2 upgrade each
    staker can have up to N pools, one per active staking token.
    """

    pool_contract: str
    token_address: str
    token_symbol: str | None = None
    amount_raw: int = Field(description="Raw u128 amount.")
    amount_decimal: Decimal = Field(description="Amount with token decimals applied.")


class AttestationStatus(BaseModel):
    """Block-attestation health for a staker in V2.

    Extended in 2026-04 to also carry the block-level info the dashboard
    needs for the waiting-state banner: which block the validator was
    assigned, how wide the sign window is, and what block the chain head
    is at right now. All three are optional so the renderer can fall back
    to the legacy short banner when the extra RPCs fail.
    """

    last_epoch_attested: int
    current_epoch: int
    missed_epochs: int                  # max(0, current_epoch - 1 - last_epoch_attested)
    is_attesting_this_epoch: bool       # has the staker attested in `current_epoch`

    # ---- block-level extras (optional, populated when operational_address
    # is known and the RPC call succeeds) -----------------------------------
    target_block: int | None = Field(
        default=None,
        description=(
            "Block the validator must attest in the current epoch. "
            "Computed at epoch start from the validator's stake proof and "
            "the RNG; per-operator. None when the target isn't known yet "
            "(very early in epoch) or the RPC failed."
        ),
    )
    attestation_window_blocks: int | None = Field(
        default=None,
        description=(
            "Length of the sign window in blocks (governance-set, ~60 on "
            "mainnet at the time of writing). None when RPC failed."
        ),
    )
    current_block: int | None = Field(
        default=None,
        description="Current head block number on the chain at fetch time.",
    )

    @property
    def sign_window_open(self) -> int | None:
        """First block of the sign window (== ``target_block`` per ABI)."""
        return self.target_block

    @property
    def sign_window_close(self) -> int | None:
        """Last block of the sign window.

        Equals ``target_block + attestation_window_blocks``. We don't try
        to encode whether the window-end is inclusive vs. exclusive — the
        renderer's "blocks left" math is symmetric either way.
        """
        if self.target_block is None or self.attestation_window_blocks is None:
            return None
        return self.target_block + self.attestation_window_blocks

    @property
    def blocks_left_in_window(self) -> int | None:
        """Distance from the current head to ``sign_window_close``.

        Negative when the window has already closed in the current epoch
        — the renderer should treat negative values as "window closed,
        retry next epoch" rather than printing "-3 blocks left".
        """
        if self.current_block is None or self.sign_window_close is None:
            return None
        return self.sign_window_close - self.current_block

    @property
    def has_block_info(self) -> bool:
        """True when we have enough on-chain data to render the extended
        block-level banner. Renderers fall back to the short banner when
        this is False.
        """
        return (
            self.target_block is not None
            and self.attestation_window_blocks is not None
            and self.current_block is not None
        )


class EpochTimeline(BaseModel):
    """How the chain stands relative to the current vs. next epoch.

    Sourced from the staking contract's ``EpochInfo`` struct plus the
    chain head::

        next_epoch_starts_block = starting_block + (current_epoch + 1 - starting_epoch) * length
        blocks_left_in_epoch    = max(0, next_epoch_starts_block - current_block)
        seconds_left_in_epoch   = blocks_left_in_epoch * (epoch_duration / length)

    Attached to ``ValidatorInfo`` so the dashboard can render the same
    "next epoch in N blocks (~M min)" tail under every status state, not
    only waiting. ``None`` propagates when ``EpochInfo`` or the chain
    head couldn't be fetched — renderers omit the tail rather than show
    placeholder zeros.
    """

    current_epoch: int
    next_epoch: int
    next_epoch_block: int
    current_block: int
    blocks_left_in_epoch: int
    seconds_left_in_epoch: int

    # ``EpochInfo`` parameters preserved verbatim so callers (tests,
    # webapp) can verify our derivations or re-derive on their side.
    epoch_length_blocks: int
    epoch_duration_seconds: int

    @property
    def minutes_left_in_epoch(self) -> int:
        """Convenience accessor for renderers (always non-negative)."""
        return max(0, self.seconds_left_in_epoch // 60)


class ValidatorInfo(BaseModel):
    """Composite view of a staker, pools, and attestation health."""

    model_config = ConfigDict(populate_by_name=True)

    staker_address: str
    reward_address: str
    operational_address: str
    amount_own_raw: int
    amount_own_strk: Decimal

    unclaimed_rewards_own_raw: int
    unclaimed_rewards_own_strk: Decimal

    commission_bps: int | None = Field(
        default=None,
        description="Single commission (if the staker's pools share one) in basis points.",
    )

    unstake_time_utc: datetime | None = None
    unstake_requested: bool = False

    pools: list[PoolInfoDto] = Field(default_factory=list)

    current_epoch: int
    attestation: AttestationStatus | None = None

    # End-of-epoch timeline shared by every status state (waiting /
    # healthy / missed / exiting). Renderers append the same "next epoch
    # in N blocks (~M min)" tail using these fields. ``None`` when
    # EpochInfo / chain head fetch failed — renderers drop the tail
    # silently in that case.
    epoch_timeline: "EpochTimeline | None" = Field(
        default=None,
        description="Position of the chain inside the current epoch.",
    )

    operator_strk_balance: Decimal | None = Field(
        default=None,
        description=(
            "STRK balance of the operational wallet (the one signing "
            "attestation txs). Pulled live from the STRK ERC-20; never "
            "persisted. Used by the low-balance alert and the validator "
            "card so the user can see the gas reserve at a glance."
        ),
    )

    @property
    def unstake_eta(self) -> timedelta | None:
        if self.unstake_time_utc is None:
            return None
        return self.unstake_time_utc - datetime.now(tz=timezone.utc)


class DelegatorInfo(BaseModel):
    """One delegator's position inside a single pool."""

    delegator_address: str
    pool_contract: str
    token_address: str | None = None
    token_symbol: str | None = None

    reward_address: str
    amount_raw: int
    amount_decimal: Decimal
    unclaimed_rewards_raw: int
    unclaimed_rewards_decimal: Decimal
    commission_bps: int

    unpool_amount_raw: int = 0
    unpool_amount_decimal: Decimal = Decimal(0)
    unpool_time_utc: datetime | None = None

    @property
    def unpool_eta(self) -> timedelta | None:
        if self.unpool_time_utc is None:
            return None
        return self.unpool_time_utc - datetime.now(tz=timezone.utc)


class DelegatorMultiPositions(BaseModel):
    """Aggregated view of a delegator across every pool of one staker.

    Staking V2 lets a single validator run multiple pools (STRK plus BTC
    wrappers). A single user can have positions in several of them at the
    same time. Instead of asking the user to track each pool separately,
    we take the ``(delegator, staker)`` pair and enumerate the pools
    automatically via ``staker_pool_info(staker)``.
    """

    delegator_address: str
    staker_address: str
    positions: list[DelegatorInfo] = Field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.positions)

    @property
    def total_unclaimed_by_token(self) -> dict[str, Decimal]:
        """Sum of unclaimed rewards bucketed by token symbol.

        In Staking V2 the *delegation* (``amount``) is in the pool's token,
        but the *rewards* are always paid out in STRK regardless of which
        pool you're in. So this aggregator buckets everything under STRK.
        """
        total = sum(
            (p.unclaimed_rewards_decimal for p in self.positions),
            Decimal(0),
        )
        return {"STRK": total} if total else {}


class StakingSystemInfo(BaseModel):
    """Protocol-wide parameters; refreshed periodically."""

    network: Literal["mainnet", "sepolia"]
    staking_contract: str
    attestation_contract: str
    reward_supplier: str
    min_stake_raw: int
    min_stake_strk: Decimal
    exit_wait_window_seconds: int
    current_epoch: int
    active_token_addresses: list[str]


def raw_to_decimal(raw: int, decimals: int) -> Decimal:
    """Convert a u128 wei-style amount to Decimal with the token's scale."""
    if raw == 0:
        return Decimal(0)
    return Decimal(raw) / (Decimal(10) ** decimals)


def build_unstake_datetime(unstake_time: dict | None) -> datetime | None:
    """Convert a contract ``Option<TimeStamp>`` payload into a UTC datetime.

    starknet-py decodes ``Option::Some({'seconds': N})`` as
    ``{'seconds': N}`` and ``Option::None`` as ``None``.
    """
    if isinstance(unstake_time, dict):
        return _utc_from_secs(unstake_time.get("seconds"))
    return None
