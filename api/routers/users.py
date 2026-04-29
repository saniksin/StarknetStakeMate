"""``/api/v1/users/me/*`` — tracking data, reward threshold, digest.

Labels are first-class in the schema. The Mini App and the local dashboard
use the same endpoints; only the auth mode differs.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.auth import TelegramUser, telegram_user_from_header
from data.contracts import get_network_addresses
from db_api.database import (
    add_tracking_entry,
    get_account,
    reorder_tracking_entries,
    write_to_db,
)
from services.tracking_service import (
    AddTrackingError,
    TrackingEntry,
    add_delegator_to_tracking,
    add_validator_to_tracking,
    dump_tracking,
    fetch_tracking_entries,
    load_tracking,
    render_dashboard_summary,
    render_user_tracking,
)
from utils.cache import clear_user_cache
from utils.check_valid_addresses import is_valid_starknet_address

router = APIRouter(prefix="/api/v1/users/me", tags=["users"])


class ValidatorPayload(BaseModel):
    address: str = Field(description="Staker ContractAddress (hex)")
    label: str = Field(default="", max_length=40)


class DelegationPayload(BaseModel):
    delegator: str = Field(description="Delegator ContractAddress (hex)")
    staker: str = Field(description="Staker/validator ContractAddress (hex)")
    label: str = Field(default="", max_length=40)


class TrackingDoc(BaseModel):
    validators: list[ValidatorPayload] = Field(default_factory=list)
    delegations: list[DelegationPayload] = Field(default_factory=list)


class ThresholdPayload(BaseModel):
    threshold_strk: float = Field(ge=0)


class NotificationConfigPayload(BaseModel):
    """Bug 4: USD aggregate + per-token thresholds.

    Either / both can be zero or empty to disable that mode. The bot's worker
    fires an alert as soon as *any* configured threshold is crossed.

    Also carries the operator-wallet low-balance threshold (STRK). 0 means
    "alerts disabled" — the attestation watcher checks every cycle and fires
    one DM the first time the live balance dips below this number.
    """

    usd_threshold: float = Field(default=0.0, ge=0)
    token_thresholds: dict[str, float] = Field(default_factory=dict)
    operator_balance_min_strk: float = Field(default=0.0, ge=0)
    # Read-only on PUT: server stays the source of truth for which validators
    # the user opted into; we expose it so the Mini App can decide whether
    # the operator-balance input should be active (only allowed when at
    # least one validator is enrolled in attestation alerts).
    attestation_alerts_for: list[str] = Field(default_factory=list)


class LabelUpdate(BaseModel):
    kind: Literal["validator", "delegator"]
    index: int = Field(ge=0)
    label: str = Field(max_length=40)


class LanguagePayload(BaseModel):
    """Single-field payload for PUT /me/language.

    Restricted to the locale prefixes the bot actually ships translations
    for so we never persist garbage that would just fall back to English.
    """
    language: Literal["en", "ru", "ua", "zh", "ko", "es", "de", "pl"]


class ProfilePayload(BaseModel):
    user_id: int
    user_name: str | None = None
    language: str = "en"


class ReorderPayload(BaseModel):
    """New order for the user's tracking lists, by identity key.

    Each list is optional — ``None`` means "leave that side untouched"
    so a future per-section reorder can ship without breaking the API
    shape. The service layer applies the reorder permissively: unknown
    keys are ignored, missing keys are appended at the end in their
    original relative order. That keeps the endpoint idempotent against
    a concurrent add (the new entry just lands at the bottom).
    """

    validators: list[str] | None = Field(
        default=None,
        description="Validator addresses in the desired order.",
    )
    delegations: list[tuple[str, str]] | None = Field(
        default=None,
        description="Delegation (delegator, staker) pairs in the desired order.",
    )


async def _resolve_user_id(
    tg_user: TelegramUser | None = Depends(telegram_user_from_header),
    tg_id: int | None = Query(default=None, description="Explicit Telegram ID (local auth)"),
) -> int:
    if tg_user is not None:
        return tg_user.id
    if tg_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="tg_id query param required in local auth"
        )
    return tg_id


@router.get("/tracking", summary="List tracked validators and delegator positions")
async def list_tracking(user_id: int = Depends(_resolve_user_id)) -> TrackingDoc:
    user = await get_account(str(user_id))
    if user is None:
        return TrackingDoc()
    doc = load_tracking(user.tracking_data)
    return TrackingDoc(
        validators=[ValidatorPayload(**v) for v in doc.get("validators", [])],
        delegations=[DelegationPayload(**d) for d in doc.get("delegations", [])],
    )


@router.put("/tracking", summary="Replace the user's tracking list")
async def put_tracking(
    payload: TrackingDoc, user_id: int = Depends(_resolve_user_id)
) -> TrackingDoc:
    for v in payload.validators:
        if not is_valid_starknet_address(v.address):
            raise HTTPException(400, detail=f"invalid validator address {v.address}")
    for d in payload.delegations:
        if not (is_valid_starknet_address(d.delegator) and is_valid_starknet_address(d.staker)):
            raise HTTPException(400, detail="invalid address in delegations")

    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    user.tracking_data = dump_tracking(payload.model_dump())
    await write_to_db(user)
    await clear_user_cache(user_id)
    return payload


def _add_error_to_http(exc: AddTrackingError) -> HTTPException:
    """Map a service-layer add-error code to the right HTTP status.

    Codes are documented on :class:`AddTrackingError`. The Mini App reads
    both ``status_code`` and the ``code`` field in the JSON body, so it
    can show a localized message without parsing the free-form ``detail``.
    """
    code = exc.code
    if code == "invalid_address":
        status_code = status.HTTP_400_BAD_REQUEST
    elif code in ("limit_reached", "duplicate"):
        status_code = status.HTTP_409_CONFLICT
    elif code in ("not_a_staker", "not_a_delegator"):
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": exc.detail or code},
    )


@router.post(
    "/tracking/validators",
    status_code=status.HTTP_201_CREATED,
    summary="Add a validator to the user's tracking list",
)
async def post_validator(
    payload: ValidatorPayload, user_id: int = Depends(_resolve_user_id)
) -> ValidatorPayload:
    """Append a validator entry. Validates format + on-chain presence,
    enforces the 10-entry cap, rejects duplicates (case-insensitive on
    address). All checks happen both at the service layer (fail-fast,
    pre-DB) and re-checked atomically inside the DB transaction so two
    concurrent Mini-App tabs can't both win the last slot.
    """
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")

    # Pre-flight at the service layer: format + on-chain + capacity +
    # duplicate against the current snapshot. Cheaper than going to the
    # DB just to bounce on a malformed address.
    doc = load_tracking(user.tracking_data)
    try:
        _, entry = await add_validator_to_tracking(
            doc, address=payload.address, label=payload.label
        )
    except AddTrackingError as exc:
        raise _add_error_to_http(exc) from exc

    # Atomic DB write — re-validates capacity + duplicate inside the
    # transaction so we don't lose a race against another tab.
    try:
        await add_tracking_entry(user_id, kind="validator", payload=entry)
    except AddTrackingError as exc:
        raise _add_error_to_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await clear_user_cache(user_id)
    return ValidatorPayload(**entry)


@router.post(
    "/tracking/delegations",
    status_code=status.HTTP_201_CREATED,
    summary="Add a delegation to the user's tracking list",
)
async def post_delegation(
    payload: DelegationPayload, user_id: int = Depends(_resolve_user_id)
) -> DelegationPayload:
    """Append a delegation entry. Validates both addresses, requires the
    delegator to actually have a position in at least one of the staker's
    pools, dedupes on the ``(delegator, staker)`` pair (pools are
    auto-discovered downstream).
    """
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")

    doc = load_tracking(user.tracking_data)
    try:
        _, entry = await add_delegator_to_tracking(
            doc,
            delegator=payload.delegator,
            staker=payload.staker,
            label=payload.label,
        )
    except AddTrackingError as exc:
        raise _add_error_to_http(exc) from exc

    try:
        await add_tracking_entry(user_id, kind="delegator", payload=entry)
    except AddTrackingError as exc:
        raise _add_error_to_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await clear_user_cache(user_id)
    return DelegationPayload(**entry)


@router.put(
    "/tracking/order",
    summary="Reorder validators and/or delegations within tracking_data",
)
async def put_tracking_order(
    payload: ReorderPayload, user_id: int = Depends(_resolve_user_id)
) -> TrackingDoc:
    """Apply a new order to the user's tracking lists.

    The Mini App calls this once when the user taps Done on the
    drag-and-drop reorder mode. Reordering a single side without
    touching the other works too — pass ``null`` for the side you
    don't want to disturb.
    """
    try:
        new_doc = await reorder_tracking_entries(
            user_id,
            validators_order=payload.validators,
            delegations_order=payload.delegations,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await clear_user_cache(user_id)

    return TrackingDoc(
        validators=[ValidatorPayload(**v) for v in new_doc.get("validators", [])],
        delegations=[DelegationPayload(**d) for d in new_doc.get("delegations", [])],
    )


@router.patch("/tracking/label", summary="Rename a single tracked entry")
async def patch_label(
    update: LabelUpdate, user_id: int = Depends(_resolve_user_id)
) -> TrackingDoc:
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    doc = load_tracking(user.tracking_data)
    lst_key = "validators" if update.kind == "validator" else "delegations"
    try:
        doc[lst_key][update.index]["label"] = update.label
    except (IndexError, KeyError) as exc:
        raise HTTPException(404, detail="entry not found") from exc
    user.tracking_data = dump_tracking(doc)
    await write_to_db(user)
    await clear_user_cache(user_id)
    return TrackingDoc(
        validators=[ValidatorPayload(**v) for v in doc["validators"]],
        delegations=[DelegationPayload(**d) for d in doc["delegations"]],
    )


@router.get("/digest", summary="Render the tracking digest (same as bot /get_full_info)")
async def user_digest(
    mode: Literal["full", "reward"] = Query("full"),
    user_id: int = Depends(_resolve_user_id),
) -> dict[str, str]:
    user = await get_account(str(user_id))
    locale = user.user_language if user else "en"
    tracking = user.tracking_data if user else None
    html = await render_user_tracking(tracking, locale, mode=mode)
    return {"html": html, "mode": mode, "locale": locale}


@router.get("/dashboard", summary="Compact summary suitable for a header card")
async def user_dashboard(user_id: int = Depends(_resolve_user_id)) -> dict:
    user = await get_account(str(user_id))
    locale = user.user_language if user else "en"
    tracking = user.tracking_data if user else None
    entries = await fetch_tracking_entries(tracking)
    html = render_dashboard_summary(entries, locale)
    return {
        "html": html,
        "entries": [
            {"index": e.index, "kind": e.kind, "label": e.label, "address": e.address}
            for e in entries
        ],
    }


@router.put("/threshold", summary="Update STRK reward notification threshold (legacy)")
async def set_threshold(
    payload: ThresholdPayload, user_id: int = Depends(_resolve_user_id)
) -> ThresholdPayload:
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    user.claim_reward_msg = payload.threshold_strk
    await write_to_db(user)
    return payload


@router.get("/notification-config", summary="Get USD/per-token notification thresholds")
async def get_notification_config(
    user_id: int = Depends(_resolve_user_id),
) -> NotificationConfigPayload:
    user = await get_account(str(user_id))
    if user is None:
        return NotificationConfigPayload()
    cfg = user.get_notification_config()
    return NotificationConfigPayload(
        usd_threshold=cfg.get("usd_threshold", 0.0),
        token_thresholds=cfg.get("token_thresholds", {}),
        operator_balance_min_strk=cfg.get("operator_balance_min_strk", 0.0),
        attestation_alerts_for=list(cfg.get("attestation_alerts_for") or []),
    )


@router.put("/notification-config", summary="Replace USD/per-token notification thresholds")
async def put_notification_config(
    payload: NotificationConfigPayload, user_id: int = Depends(_resolve_user_id)
) -> NotificationConfigPayload:
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    # Merge with the existing config so the Mini App can update the
    # threshold-related fields without having to round-trip the
    # attestation_alerts_for set (which it shouldn't be modifying through
    # this endpoint — that's a separate per-validator opt-in flow). If
    # we let the empty default propagate we'd silently disable every
    # attestation subscription on every Settings save.
    existing = user.get_notification_config()
    incoming = payload.model_dump()
    existing_subs = existing.get("attestation_alerts_for") or []
    incoming["attestation_alerts_for"] = existing_subs
    # Server-side guard: refuse to arm the operator-balance alert when
    # the user has no attestation subscriptions. The alert task only
    # checks balances for validators inside attestation_alerts_for, so
    # accepting a positive threshold here would silently produce no
    # effect and surface as the kind of "I configured something and
    # nothing happens" surprise we want to avoid.
    if payload.operator_balance_min_strk > 0 and not existing_subs:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "Enable attestation alerts for at least one validator first. "
                "The operator-wallet alert is part of the same per-validator subscription set."
            ),
        )
    user.set_notification_config(incoming)
    user.claim_reward_msg = 0  # writes consolidate into the JSON config
    await write_to_db(user)
    return payload


@router.get("/entries", summary="Return typed entries (validator/delegator DTOs)")
async def typed_entries(user_id: int = Depends(_resolve_user_id)) -> list[dict]:
    user = await get_account(str(user_id))
    if user is None:
        return []
    entries: list[TrackingEntry] = await fetch_tracking_entries(user.tracking_data)
    return [
        {
            "index": e.index,
            "kind": e.kind,
            "address": e.address,
            "pool": e.pool,
            "label": e.label,
            "data": e.data.model_dump(mode="json") if e.data is not None else None,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# Profile + language (Mini App i18n)
# ---------------------------------------------------------------------------

@router.get("/profile", summary="Current user profile (id, name, language)")
async def get_profile(user_id: int = Depends(_resolve_user_id)) -> ProfilePayload:
    """Return the user's basic profile.

    Used by the Mini App on boot to pick the locale bundle. New users that
    haven't hit the bot yet get the default language ``en`` — the Mini App
    can offer a picker to override it without going to the bot first.
    """
    user = await get_account(str(user_id))
    if user is None:
        return ProfilePayload(user_id=user_id, language="en")
    return ProfilePayload(
        user_id=int(user.user_id),
        user_name=user.user_name,
        language=(user.user_language or "en"),
    )


@router.put("/language", summary="Update the user's UI language")
async def put_language(
    payload: LanguagePayload, user_id: int = Depends(_resolve_user_id)
) -> ProfilePayload:
    """Persist a new UI language. Same column the bot reads, so changing
    here also flips the bot's replies on the user's next interaction."""
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    user.user_language = payload.language
    await write_to_db(user)
    return ProfilePayload(
        user_id=int(user.user_id),
        user_name=user.user_name,
        language=payload.language,
    )
