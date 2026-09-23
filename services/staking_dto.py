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


class NodeSync(BaseModel):
    """Whether the RPC node we read from has caught up with the network.

    Surfaced in the Mini App header as a block number plus a green/red
    dot: every figure on screen is only as fresh as the node behind it,
    and a node that silently lags shows plausible-looking but stale
    balances and attestation states.

    ``highest_block`` mirrors ``current_block`` once the node reports it
    is done syncing — Starknet's ``starknet_syncing`` returns ``false``
    at that point rather than a struct, so there is no separate network
    head to compare against.
    """

    current_block: int
    highest_block: int
    blocks_behind: int
    synced: bool


class ValidatorUptime(BaseModel):
    """How reliably a validator has been attesting, per Endur's index.

    The staking contracts expose ``get_last_epoch_attestation_done`` and
    nothing else — enough to say "missed N epochs since the last one it
    won", not enough for a percentage. Endur runs an indexer over the
    attestation events and publishes the aggregate as ``liveliness``;
    that is what this DTO carries.

    **This object is always returned, never ``None``.** ``status`` says
    whether the number is there and, when it isn't, why. That is
    deliberate: a block that quietly disappears when a third party
    changes their API looks exactly like a validator with nothing to
    report, and the failure would go unnoticed for as long as nobody
    happened to remember the feature existed. An explicit "couldn't
    fetch this" is the whole point.

      - ``ok``          — ``percent`` is set.
      - ``not_indexed`` — upstream answered, but doesn't know this
                          validator (normal right after registering).
      - ``unavailable`` — upstream unreachable, too slow, or answering
                          in a shape we don't recognise. ``detail``
                          carries a short reason for the tooltip.

    Two caveats are baked into the field names rather than left to the
    reader. ``percent`` is *their* number, not ours: Endur does not
    document the window it covers, so the UI attributes it instead of
    presenting it as a measurement of our own. ``measured_at`` is when
    their record was last refreshed (roughly every ten minutes, but not
    uniformly across validators) — without it a stale 100% looks exactly
    like a fresh one.
    """

    address: str
    status: Literal["ok", "not_indexed", "unavailable"]
    percent: float | None = Field(
        default=None, ge=0, le=100, description="Endur's ``liveliness``, 0–100."
    )
    is_active: bool | None = None
    is_unstaking: bool | None = None
    name: str | None = None
    logo_url: str | None = None
    active_since: datetime | None = None
    measured_at: datetime | None = Field(
        default=None, description="When the upstream record was last refreshed."
    )
    detail: str | None = Field(
        default=None, description="Short reason, only for ``unavailable``."
    )
    source: Literal["endur"] = "endur"

    @property
    def has_data(self) -> bool:
        return self.status == "ok" and self.percent is not None


class NetworkApr(BaseModel):
    """Protocol-wide staking APR, before any validator commission.

    The Yield calculator needs the *gross* rate: it applies commission
    itself, giving the validator its own stake at the full rate plus the
    commission slice of the delegated stake, and a delegator the rate net
    of commission. Feeding it a post-commission number would double-count
    the cut.

    Read straight off the staking contracts — APR is emission over stake
    and both sides are on chain. See :mod:`services.apr_service`.

    ``status`` says how much to trust the numbers:

      - ``ok``          — computed just now.
      - ``stale``       — the chain reads failed, so this is the last
                          figure we successfully computed, persisted on
                          disk and therefore surviving a restart.
                          ``measured_at`` is from that earlier reading,
                          which is what makes the staleness visible.
      - ``unavailable`` — never had a reading; the client falls back to
                          its built-in constants and says so.

    ``btc_percent`` is ``None`` rather than zero when it cannot be stated:
    BTC pools are paid in STRK against BTC collateral, so that one figure
    needs both prices. The STRK rate never depends on a price.
    """

    network: Literal["mainnet", "sepolia"]
    status: Literal["ok", "stale", "unavailable"]
    strk_percent: float | None = Field(default=None, ge=0, le=1000)
    btc_percent: float | None = Field(default=None, ge=0, le=1000)
    measured_at: datetime | None = None
    detail: str | None = None
    source: Literal["chain"] = "chain"

    @property
    def has_data(self) -> bool:
        return self.status in ("ok", "stale") and self.strk_percent is not None


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

    # Position of the chain inside the current epoch — surfaced on the
    # Mini App hero so the user sees a "next epoch in N blocks (~M min)"
    # tail next to the epoch chip without burning a per-validator RPC.
    # ``None`` when the EpochInfo / chain head fetch failed.
    epoch_timeline: "EpochTimeline | None" = Field(
        default=None,
        description="Chain head position relative to the current epoch.",
    )

    # ``None`` when the syncing probe itself failed — the header then omits
    # the indicator instead of claiming a state it does not know.
    node_sync: "NodeSync | None" = Field(
        default=None,
        description="Sync state of the RPC node serving this API.",
    )


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
