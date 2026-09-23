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

    def get_tracking_data(self, network: str | None = None) -> dict:
        """Return the user's tracking_data doc for ``network``.

        Schema::

            {"validators":  [{"address": "0x…", "label": "…"}, …],
             "delegations": [{"delegator": "0x…", "staker": "0x…", "label": "…"}, …],
             "networks":    {"sepolia": {"validators": […], "delegations": […]}}}

        The default network keeps the top level (that's what the bot has
        always read); any other network is a sub-document under
        ``networks``. Passing ``None`` means the default network, so
        every pre-existing call site is unaffected.

        No migration from the older ``data_pair`` / pool-keyed format is
        performed — the project chose to wipe the DB on that breaking
        change.
        """
        from data.contracts import DEFAULT_NETWORK

        empty = {"validators": [], "delegations": []}
        if not self.tracking_data:
            return empty
        try:
            data = json.loads(self.tracking_data)
        except json.JSONDecodeError:
            return empty
        if not isinstance(data, dict):
            return empty
        if network is not None and network != DEFAULT_NETWORK:
            sub = (data.get("networks") or {}).get(network)
            data = sub if isinstance(sub, dict) else {}
        data.setdefault("validators", [])
        data.setdefault("delegations", [])
        return data

    # Keys tracked separately for every network. ``usd_threshold`` is
    # deliberately NOT among them: a USD threshold needs a price, and the
    # tokens outside mainnet have no market. Token-amount thresholds do
    # work anywhere — you can ask to hear about 10 test-STRK — so those
    # are per-network like everything else.
    _PER_NETWORK_KEYS = (
        "token_thresholds",
        "attestation_alerts_for",
        "_attestation_state",
        "operator_balance_min_strk",
        "_operator_balance_was_below",
    )

    def get_notification_config(self, network: str | None = None) -> dict:
        """Return the notification config dict for ``network``.

        Schema (default network — the top level of the column)::

            {
              "usd_threshold": float,
              "token_thresholds": {sym: float},
              "attestation_alerts_for": [staker_addr, …],  # Bug 5: per-validator opt-in
              "attestation_alerts": bool,                  # legacy global flag (read-only)
              "_attestation_state": {staker: int},         # last-seen missed_epochs
              "operator_balance_min_strk": float,
              "_operator_balance_was_below": {staker: True},
              "networks": {"sepolia": {…the four per-network keys…}},
            }

        For a non-default network the same key names are returned, read
        out of ``networks[<name>]``, with the reward thresholds forced to
        "off" — see :data:`_PER_NETWORK_KEYS`.

        Falls back to the legacy ``claim_reward_msg`` (treated as STRK
        threshold) when no explicit config is stored. Always returns a fresh
        dict — mutating it does NOT persist anything.
        """
        from data.contracts import DEFAULT_NETWORK

        if network is not None and network != DEFAULT_NETWORK:
            return self._network_notification_config(network)

        if self.notification_config:
            try:
                cfg = json.loads(self.notification_config)
                cfg.setdefault("usd_threshold", 0.0)
                cfg.setdefault("token_thresholds", {})
                cfg.setdefault("attestation_alerts_for", [])
                cfg.setdefault("attestation_alerts", False)
                cfg.setdefault("_attestation_state", {})
                # Operator low-balance: 0 means "alert disabled". Alerts
                # are now sent at most once per epoch (only when the epoch
                # boundary ticks). ``_operator_balance_was_below`` is the
                # snapshot from the last boundary so we can pick the right
                # message (down-cross / recovered / silent) when the next
                # boundary fires.
                #
                # Migration: the legacy ``_operator_balance_state`` (1=below,
                # 0=above; flip-triggered) is converted in-place to the new
                # shape so existing rows keep working without a one-shot
                # SQL migration. Subsequent set_notification_config persists
                # the cleaner shape.
                cfg.setdefault("operator_balance_min_strk", 0.0)
                if "_operator_balance_was_below" not in cfg:
                    legacy = cfg.get("_operator_balance_state") or {}
                    cfg["_operator_balance_was_below"] = {
                        str(k): True for k, v in legacy.items() if v
                    }
                cfg.pop("_operator_balance_state", None)
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
            "_operator_balance_was_below": {},
        }

    def _raw_notification_config(self) -> dict:
        """The stored JSON as-is (``{}`` when absent or corrupt)."""
        if not self.notification_config:
            return {}
        try:
            raw = json.loads(self.notification_config)
        except (TypeError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _network_notification_config(self, network: str) -> dict:
        """Read the per-network slice, shaped like the top-level config."""
        sub = (self._raw_notification_config().get("networks") or {}).get(network)
        sub = sub if isinstance(sub, dict) else {}
        return {
            # A USD threshold needs a price and the tokens here have none,
            # so it is reported as off — the Settings screen hides the USD
            # mode rather than arming something the notifier would skip.
            "usd_threshold": 0.0,
            "token_thresholds": {
                str(sym): float(amt)
                for sym, amt in (sub.get("token_thresholds") or {}).items()
                if amt and float(amt) > 0
            },
            "attestation_alerts_for": list(sub.get("attestation_alerts_for") or []),
            "attestation_alerts": False,
            "_attestation_state": dict(sub.get("_attestation_state") or {}),
            "operator_balance_min_strk": float(
                sub.get("operator_balance_min_strk") or 0.0
            ),
            "_operator_balance_was_below": {
                str(k): True
                for k, v in (sub.get("_operator_balance_was_below") or {}).items()
                if v
            },
        }

    def set_notification_config(self, cfg: dict, network: str | None = None) -> None:
        """Persist a new config. Pass ``{}`` to disable everything.

        With a non-default ``network`` only that network's slice is
        rewritten; the mainnet settings and the other networks are read
        back off the column and preserved.
        """
        from data.contracts import DEFAULT_NETWORK

        if network is not None and network != DEFAULT_NETWORK:
            self._set_network_notification_config(network, cfg)
            return

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
            # Per-staker "was below threshold at last epoch boundary" flag.
            # Read on the next boundary tick to decide whether to fire
            # ``low-balance`` (still below) or ``recovered`` (now above)
            # alerts. Only stored when True — absence == "above" so the
            # JSON stays lean.
            "_operator_balance_was_below": {
                str(k): True
                for k, v in (cfg.get("_operator_balance_was_below") or {}).items()
                if v
            },
        }
        # Other networks are edited through their own code path; carry
        # whatever is already stored so a mainnet save can't wipe the
        # testnet subscriptions.
        networks = cfg.get("networks")
        if not isinstance(networks, dict):
            networks = self._raw_notification_config().get("networks")
        networks = {
            str(name): self._clean_network_slice(sub)
            for name, sub in (networks or {}).items()
            if isinstance(sub, dict)
        }
        networks = {name: sub for name, sub in networks.items() if sub}
        if networks:
            clean["networks"] = networks

        # If everything is off and no state is being tracked, store NULL so
        # a future migration to a typed column doesn't have to filter out
        # empty dicts.
        if (
            clean["usd_threshold"] <= 0
            and not clean["token_thresholds"]
            and not clean["attestation_alerts_for"]
            and not clean["_attestation_state"]
            and clean["operator_balance_min_strk"] <= 0
            and not clean["_operator_balance_was_below"]
            and not networks
        ):
            self.notification_config = None
        else:
            self.notification_config = json.dumps(clean)

    @staticmethod
    def _clean_network_slice(sub: dict) -> dict:
        """Normalize one per-network slice; ``{}`` when nothing is set."""
        out = {
            "token_thresholds": {
                str(sym): float(amt)
                for sym, amt in (sub.get("token_thresholds") or {}).items()
                if amt and float(amt) > 0
            },
            "attestation_alerts_for": sorted({
                str(a).lower() for a in (sub.get("attestation_alerts_for") or []) if a
            }),
            "_attestation_state": {
                str(k): int(v)
                for k, v in (sub.get("_attestation_state") or {}).items()
            },
            "operator_balance_min_strk": float(
                sub.get("operator_balance_min_strk") or 0.0
            ),
            "_operator_balance_was_below": {
                str(k): True
                for k, v in (sub.get("_operator_balance_was_below") or {}).items()
                if v
            },
        }
        if (
            not out["token_thresholds"]
            and not out["attestation_alerts_for"]
            and not out["_attestation_state"]
            and out["operator_balance_min_strk"] <= 0
            and not out["_operator_balance_was_below"]
        ):
            return {}
        return out

    def _set_network_notification_config(self, network: str, cfg: dict) -> None:
        """Replace one network's slice, leaving every other setting alone."""
        raw = self._raw_notification_config()
        networks = dict(raw.get("networks") or {})
        slice_ = self._clean_network_slice(cfg)
        if slice_:
            networks[network] = slice_
        else:
            networks.pop(network, None)

        # Re-serialize through the default-network path so the top-level
        # shape stays canonical and the "everything off → NULL" rule is
        # applied in exactly one place.
        base = self.get_notification_config()
        base["networks"] = networks
        self.set_notification_config(base)
