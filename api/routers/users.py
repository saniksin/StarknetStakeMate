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
from db_api.database import get_account, write_to_db
from services.tracking_service import (
    TrackingEntry,
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


class LabelUpdate(BaseModel):
    kind: Literal["validator", "delegator"]
    index: int = Field(ge=0)
    label: str = Field(max_length=40)


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
    )


@router.put("/notification-config", summary="Replace USD/per-token notification thresholds")
async def put_notification_config(
    payload: NotificationConfigPayload, user_id: int = Depends(_resolve_user_id)
) -> NotificationConfigPayload:
    user = await get_account(str(user_id))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")
    user.set_notification_config(payload.model_dump())
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
