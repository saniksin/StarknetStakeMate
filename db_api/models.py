import json
from data.models import AutoRepr
from sqlalchemy import (Column, Integer, Text, Boolean, DateTime)
from sqlalchemy.orm import declarative_base


Base = declarative_base()

class Users(Base, AutoRepr):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    # ``unique=True`` is the long-term contract. ``initialize_db`` also
    # CREATEs the equivalent UNIQUE INDEX on every boot so existing DBs
    # that were provisioned before this declaration get the constraint
    # too (SQLAlchemy's ``create_tables`` doesn't migrate columns on
    # existing tables).
    user_id = Column(Integer, unique=True, index=True)
    user_name = Column(Text)
    user_language = Column(Text)
    user_registration_data = Column(DateTime)
    user_is_blocked = Column(Boolean, default=False)
    tracking_data = Column(Text, nullable=True)
    claim_reward_msg = Column(Integer)
    request_queue = Column(Text, nullable=True)
    # Bug 4: USD-based + per-token thresholds. JSON document of the form
    # ``{"usd_threshold": 5.0, "token_thresholds": {"STRK": 10, "WBTC": 0.001}}``.
    # NULL / missing keys mean that mode is disabled.
    notification_config = Column(Text, nullable=True)
   
    def __init__(
            self,
            user_id: int,
            user_name: str,
            user_language: str,
            registration_data: str,
    ) -> None:
        self.user_id = user_id
        self.user_name = user_name
        self.user_language = user_language
        self.user_registration_data = registration_data
        self.tracking_data = json.dumps({"validators": [], "delegations": []})
        self.claim_reward_msg = 0
        self.request_queue = None
        self.notification_config = None

    def get_tracking_data(self) -> dict:
        """Return the user's tracking_data doc.

        Schema::

            {"validators":  [{"address": "0x…", "label": "…"}, …],
             "delegations": [{"delegator": "0x…", "staker": "0x…", "label": "…"}, …]}

        No migration from the older ``data_pair`` / pool-keyed format is
        performed — the project chose to wipe the DB on that breaking change.
        """
        empty = {"validators": [], "delegations": []}
        if not self.tracking_data:
            return empty
        try:
            data = json.loads(self.tracking_data)
        except json.JSONDecodeError:
            return empty
        data.setdefault("validators", [])
        data.setdefault("delegations", [])
        return data

    def get_notification_config(self) -> dict:
        """Return the full notification config dict.

        Schema::

            {
              "usd_threshold": float,
              "token_thresholds": {sym: float},
              "attestation_alerts_for": [staker_addr, …],  # Bug 5: per-validator opt-in
              "attestation_alerts": bool,                  # legacy global flag (read-only)
              "_attestation_state": {staker: int},         # last-seen missed_epochs
            }

        Falls back to the legacy ``claim_reward_msg`` (treated as STRK
        threshold) when no explicit config is stored. Always returns a fresh
        dict — mutating it does NOT persist anything.
        """
        if self.notification_config:
            try:
                cfg = json.loads(self.notification_config)
                cfg.setdefault("usd_threshold", 0.0)
                cfg.setdefault("token_thresholds", {})
                cfg.setdefault("attestation_alerts_for", [])
                cfg.setdefault("attestation_alerts", False)
                cfg.setdefault("_attestation_state", {})
                # Operator low-balance: 0 means "alert disabled". Per-validator
                # state keeps us from re-alerting every cycle for the same
                # below-threshold balance.
                cfg.setdefault("operator_balance_min_strk", 0.0)
                cfg.setdefault("_operator_balance_state", {})
                return cfg
            except (TypeError, ValueError):
                pass
        return {
            "usd_threshold": 0.0,
            "token_thresholds": (
                {"STRK": float(self.claim_reward_msg)}
                if self.claim_reward_msg
                else {}
            ),
            "attestation_alerts_for": [],
            "attestation_alerts": False,
            "_attestation_state": {},
            "operator_balance_min_strk": 0.0,
            "_operator_balance_state": {},
        }

    def set_notification_config(self, cfg: dict) -> None:
        """Persist a new config. Pass ``{}`` to disable everything."""
        attestation_for = sorted({
            str(a).lower()
            for a in (cfg.get("attestation_alerts_for") or [])
            if a
        })
        clean = {
            "usd_threshold": float(cfg.get("usd_threshold") or 0.0),
            "token_thresholds": {
                sym: float(amt)
                for sym, amt in (cfg.get("token_thresholds") or {}).items()
                if amt and float(amt) > 0
            },
            "attestation_alerts_for": attestation_for,
            "_attestation_state": {
                str(k): int(v)
                for k, v in (cfg.get("_attestation_state") or {}).items()
            },
            "operator_balance_min_strk": float(
                cfg.get("operator_balance_min_strk") or 0.0
            ),
            # Per-staker last-known "below threshold" flag (1) / above (0).
            # Used by the alert task to debounce so we send one DM per
            # downward crossing instead of every minute.
            "_operator_balance_state": {
                str(k): int(v)
                for k, v in (cfg.get("_operator_balance_state") or {}).items()
            },
        }
        # If everything is off and no state is being tracked, store NULL so
        # a future migration to a typed column doesn't have to filter out
        # empty dicts.
        if (
            clean["usd_threshold"] <= 0
            and not clean["token_thresholds"]
            and not clean["attestation_alerts_for"]
            and not clean["_attestation_state"]
            and clean["operator_balance_min_strk"] <= 0
            and not clean["_operator_balance_state"]
        ):
            self.notification_config = None
        else:
            self.notification_config = json.dumps(clean)