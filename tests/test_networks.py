"""Multi-network support: resolution, storage layout, and isolation.

The feature's whole promise is that mainnet and testnet never bleed into
each other — not in the tracking lists, not in the alert subscriptions,
and not in the caches in front of either. These tests pin that promise
down at the three layers where it could break.
"""
from __future__ import annotations

import json

import pytest

from data.contracts import (
    DEFAULT_NETWORK,
    UnknownNetworkError,
    available_networks,
    get_network_addresses,
    get_rpc_url,
    is_network_available,
    resolve_network,
)
from db_api.models import Users
from services.tracking_service import (
    dump_tracking,
    load_tracking,
    store_tracking,
    total_tracked,
)

ADDR_A = "0x" + "a" * 63
ADDR_B = "0x" + "b" * 63
ADDR_C = "0x" + "c" * 63


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, DEFAULT_NETWORK),
        ("", DEFAULT_NETWORK),
        ("mainnet", "mainnet"),
        ("MAINNET", "mainnet"),
        ("  sepolia ", "sepolia"),
        # The UI says "testnet"; the protocol says "sepolia".
        ("testnet", "sepolia"),
        ("TestNet", "sepolia"),
    ],
)
def test_resolve_network_accepts_aliases(value, expected) -> None:
    assert resolve_network(value) == expected


def test_resolve_network_rejects_unknown() -> None:
    # Falling back to mainnet here would render mainnet numbers under
    # whatever heading the caller asked for — the one failure mode this
    # feature must not have.
    with pytest.raises(UnknownNetworkError):
        resolve_network("goerli")


def test_both_networks_configured_in_tests() -> None:
    nets = available_networks()
    assert nets[0] == DEFAULT_NETWORK, "default network must be listed first"
    assert set(nets) == {"mainnet", "sepolia"}
    assert is_network_available("sepolia")
    assert get_rpc_url("mainnet") != get_rpc_url("sepolia")


def test_network_addresses_differ_per_network() -> None:
    main = get_network_addresses("mainnet")
    sep = get_network_addresses("sepolia")
    assert main.staking_contract != sep.staking_contract
    assert main.attestation_contract != sep.attestation_contract
    assert main.chain_id_hex != sep.chain_id_hex


# ---------------------------------------------------------------------------
# tracking_data layout
# ---------------------------------------------------------------------------

def test_mainnet_keeps_the_top_level() -> None:
    """The bot reads these keys directly — they must not move."""
    stored = store_tracking(
        None, {"validators": [{"address": ADDR_A, "label": "V"}], "delegations": []}
    )
    raw = json.loads(stored)
    assert raw["validators"][0]["address"] == ADDR_A
    assert "networks" not in raw


def test_testnet_lives_in_its_own_subdocument() -> None:
    stored = store_tracking(
        None,
        {"validators": [{"address": ADDR_B, "label": "T"}], "delegations": []},
        "sepolia",
    )
    raw = json.loads(stored)
    # Mainnet stays empty; the testnet entry is nested.
    assert raw["validators"] == []
    assert raw["networks"]["sepolia"]["validators"][0]["address"] == ADDR_B


def test_writing_one_network_preserves_the_other() -> None:
    doc = store_tracking(
        None, {"validators": [{"address": ADDR_A, "label": "main"}], "delegations": []}
    )
    doc = store_tracking(
        doc,
        {"validators": [{"address": ADDR_B, "label": "test"}], "delegations": []},
        "sepolia",
    )
    # Now overwrite mainnet again — the classic way to lose the other list.
    doc = store_tracking(
        doc,
        {"validators": [{"address": ADDR_C, "label": "main2"}], "delegations": []},
    )

    mainnet = load_tracking(doc, "mainnet")
    testnet = load_tracking(doc, "sepolia")
    assert [v["address"] for v in mainnet["validators"]] == [ADDR_C]
    assert [v["address"] for v in testnet["validators"]] == [ADDR_B]


def test_emptying_testnet_drops_the_subdocument() -> None:
    doc = store_tracking(
        None,
        {"validators": [{"address": ADDR_B, "label": "T"}], "delegations": []},
        "sepolia",
    )
    doc = store_tracking(doc, {"validators": [], "delegations": []}, "sepolia")
    assert "networks" not in json.loads(doc)


def test_dump_tracking_round_trip_keeps_networks() -> None:
    """Legacy bot write paths go through ``dump_tracking``; they must not
    wipe the testnet list as a side effect."""
    doc = store_tracking(
        None,
        {"validators": [{"address": ADDR_B, "label": "T"}], "delegations": []},
        "sepolia",
    )
    mainnet_doc = load_tracking(doc, "mainnet")
    mainnet_doc["validators"].append({"address": ADDR_A, "label": "V"})
    round_tripped = dump_tracking(mainnet_doc)
    assert load_tracking(round_tripped, "sepolia")["validators"][0]["address"] == ADDR_B


def test_entry_limit_is_per_network() -> None:
    """Ten mainnet entries must not block adding a testnet one."""
    doc = {"validators": [{"address": ADDR_A, "label": str(i)} for i in range(10)],
           "delegations": []}
    stored = store_tracking(None, doc)
    assert total_tracked(load_tracking(stored, "mainnet")) == 10
    assert total_tracked(load_tracking(stored, "sepolia")) == 0


def test_load_tracking_of_unseen_network_is_empty() -> None:
    doc = load_tracking('{"validators": [{"address": "0x1"}]}', "sepolia")
    assert doc == {"validators": [], "delegations": []}


# ---------------------------------------------------------------------------
# notification_config layout
# ---------------------------------------------------------------------------

def _user() -> Users:
    return Users(user_id=1, user_name="u", user_language="en", registration_data="now")


def test_attestation_subscriptions_are_per_network() -> None:
    user = _user()
    user.set_notification_config({"attestation_alerts_for": [ADDR_A]})
    user.set_notification_config({"attestation_alerts_for": [ADDR_B]}, "sepolia")

    assert user.get_notification_config()["attestation_alerts_for"] == [ADDR_A]
    assert user.get_notification_config("sepolia")["attestation_alerts_for"] == [ADDR_B]


def test_mainnet_save_keeps_testnet_subscriptions() -> None:
    user = _user()
    user.set_notification_config({"attestation_alerts_for": [ADDR_B]}, "sepolia")
    # A plain Settings save on mainnet — the payload knows nothing about
    # the other network.
    user.set_notification_config({"usd_threshold": 5.0})
    assert user.get_notification_config("sepolia")["attestation_alerts_for"] == [ADDR_B]
    assert user.get_notification_config()["usd_threshold"] == 5.0


def test_reward_thresholds_report_as_off_on_testnet() -> None:
    """Reward alerts never run off mainnet, so the testnet view must not
    show an armed threshold the notifier will ignore."""
    user = _user()
    user.set_notification_config({"usd_threshold": 5.0, "token_thresholds": {"STRK": 10}})
    testnet_cfg = user.get_notification_config("sepolia")
    assert testnet_cfg["usd_threshold"] == 0.0
    assert testnet_cfg["token_thresholds"] == {}


def test_attestation_state_is_per_network() -> None:
    user = _user()
    user.set_notification_config(
        {"attestation_alerts_for": [ADDR_A], "_attestation_state": {ADDR_A: 3}}
    )
    user.set_notification_config(
        {"attestation_alerts_for": [ADDR_A], "_attestation_state": {ADDR_A: 7}},
        "sepolia",
    )
    assert user.get_notification_config()["_attestation_state"][ADDR_A] == 3
    assert user.get_notification_config("sepolia")["_attestation_state"][ADDR_A] == 7


def test_clearing_last_testnet_slice_drops_the_networks_key() -> None:
    user = _user()
    user.set_notification_config({"attestation_alerts_for": [ADDR_B]}, "sepolia")
    user.set_notification_config({"attestation_alerts_for": []}, "sepolia")
    # Nothing configured anywhere → the column goes back to NULL.
    assert user.notification_config is None


def test_get_tracking_data_is_network_aware() -> None:
    user = _user()
    user.tracking_data = store_tracking(
        store_tracking(
            None, {"validators": [{"address": ADDR_A}], "delegations": []}
        ),
        {"validators": [{"address": ADDR_B}], "delegations": []},
        "sepolia",
    )
    assert user.get_tracking_data()["validators"][0]["address"] == ADDR_A
    assert user.get_tracking_data("sepolia")["validators"][0]["address"] == ADDR_B
