"""Per-validator attestation alert subscriptions.

The bot submenu and the Mini App settings screen edit the very same
``attestation_alerts_for`` set, so the read / normalise / persist rules
live here rather than inside either front end. The state trimming on
unsubscribe is subtle enough that two copies of it would drift.
"""
from __future__ import annotations

from db_api.database import Users, write_to_db


def read_subscriptions(cfg: dict) -> set[str]:
    """Return the opted-in staker addresses, lower-cased.

    Carries a tiny migration: the old schema had a single boolean
    ``attestation_alerts`` (all-or-nothing). When only that is present we
    return the ``"*"`` sentinel, meaning "everything currently tracked" —
    callers resolve it against the live validator list via `resolve`.
    """
    raw = cfg.get("attestation_alerts_for")
    if isinstance(raw, list):
        return {str(a).lower() for a in raw if a}
    if cfg.get("attestation_alerts"):
        return {"*"}
    return set()


def resolve(subscribed: set[str], tracked: set[str]) -> set[str]:
    """Expand the legacy ``"*"`` sentinel into concrete addresses.

    Anything that edits individual validators must resolve first, or the
    first per-validator toggle would silently drop every other one.
    """
    if "*" in subscribed:
        return set(tracked)
    return set(subscribed)


async def persist_subscriptions(
    user: Users, cfg: dict, new_set: set[str], network: str | None = None
) -> None:
    """Write the subscription set and forget state for dropped stakers.

    Clearing ``_attestation_state`` for someone the user just unsubscribed
    from means a later re-enable starts with a fresh missed-epoch counter
    instead of firing on a gap that accumulated while alerts were off.

    ``cfg`` must be the config for ``network`` (i.e. what
    ``user.get_notification_config(network)`` returned) — the two travel
    together so a testnet save can never be written into the mainnet slot.
    """
    cfg["attestation_alerts_for"] = sorted(new_set)
    cfg.pop("attestation_alerts", None)  # drop the legacy bool
    state = dict(cfg.get("_attestation_state") or {})
    for staker in list(state.keys()):
        if staker.lower() not in new_set:
            state.pop(staker, None)
    cfg["_attestation_state"] = state
    user.set_notification_config(cfg, network)
    await write_to_db(user)
