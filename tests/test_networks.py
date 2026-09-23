"""Multi-network support: resolution, storage layout, and isolation.

The feature's whole promise is that mainnet and testnet never bleed into
each other — not in the tracking lists, not in the alert subscriptions,
and not in the caches in front of either. These tests pin that promise
down at the three layers where it could break.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.app import app
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

@pytest.fixture
def client() -> TestClient:
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


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


def test_usd_threshold_is_never_armed_off_mainnet() -> None:
    """A USD threshold needs a price and testnet tokens have no market, so
    the testnet view reports it as off rather than arming something the
    notifier would skip. Token-amount thresholds DO work there."""
    user = _user()
    user.set_notification_config({"usd_threshold": 5.0, "token_thresholds": {"STRK": 10}})
    testnet_cfg = user.get_notification_config("sepolia")
    assert testnet_cfg["usd_threshold"] == 0.0
    # Mainnet's token threshold must not leak into testnet either.
    assert testnet_cfg["token_thresholds"] == {}


def test_token_thresholds_are_per_network() -> None:
    user = _user()
    user.set_notification_config({"token_thresholds": {"STRK": 10}})
    user.set_notification_config({"token_thresholds": {"STRK": 500}}, "sepolia")
    assert user.get_notification_config()["token_thresholds"] == {"STRK": 10.0}
    assert user.get_notification_config("sepolia")["token_thresholds"] == {"STRK": 500.0}
    # And a USD-only mainnet save leaves the testnet amount alone.
    user.set_notification_config({"usd_threshold": 5.0})
    assert user.get_notification_config("sepolia")["token_thresholds"] == {"STRK": 500.0}


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


# ---------------------------------------------------------------------------
# Reward notifier across networks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reward_dm_on_testnet_is_badged_and_says_value_is_zero() -> None:
    """Token-amount thresholds fire off mainnet too, but the DM has to say
    what the number is worth: nothing."""
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from tasks import strk_notification as notifier

    user = _user()
    user.tracking_data = store_tracking(
        None,
        {"validators": [{"address": ADDR_B, "label": "Test V"}], "delegations": []},
        "sepolia",
    )
    user.set_notification_config({"token_thresholds": {"STRK": 10}}, "sepolia")

    entry = SimpleNamespace(
        kind="validator", address=ADDR_B, label="Test V",
        data=SimpleNamespace(
            unclaimed_rewards_own_strk=Decimal("42"), attestation=None,
        ),
    )

    async def _fake_entries(_tracking, network=None):
        assert network == "sepolia"
        return [entry]

    with (
        patch.object(notifier, "fetch_tracking_entries", _fake_entries),
        patch.object(
            notifier, "_unclaimed_by_symbol", lambda _e: {"STRK": Decimal("42")}
        ),
        patch.object(notifier, "_format_entry_alert", lambda _e, _l: "\nentry"),
        patch.object(notifier, "send_message", new=AsyncMock()) as mock_send,
    ):
        await notifier.start_parse_and_send_notification(user, {}, "sepolia")

    assert mock_send.await_count == 1
    body = mock_send.await_args.args[1]
    assert body.startswith("🧪")              # badge leads the message
    assert "STRK 42.00 ≥ 10.00" in body       # the threshold that fired
    # …and the last line states what that number is worth.
    assert "0" in body.rstrip().rsplit("\n", 1)[-1]


@pytest.mark.asyncio
async def test_reward_dm_on_mainnet_keeps_its_previous_shape() -> None:
    """No badge, no footer — months of muscle memory for this message."""
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from tasks import strk_notification as notifier

    user = _user()
    user.tracking_data = store_tracking(
        None, {"validators": [{"address": ADDR_A, "label": "Main V"}], "delegations": []}
    )
    user.set_notification_config({"token_thresholds": {"STRK": 10}})

    entry = SimpleNamespace(
        kind="validator", address=ADDR_A, label="Main V",
        data=SimpleNamespace(
            unclaimed_rewards_own_strk=Decimal("42"), attestation=None,
        ),
    )

    async def _fake_entries(_tracking, network=None):
        return [entry]

    with (
        patch.object(notifier, "fetch_tracking_entries", _fake_entries),
        patch.object(
            notifier, "_unclaimed_by_symbol", lambda _e: {"STRK": Decimal("42")}
        ),
        patch.object(notifier, "_format_entry_alert", lambda _e, _l: "\nentry"),
        patch.object(notifier, "send_message", new=AsyncMock()) as mock_send,
    ):
        await notifier.start_parse_and_send_notification(user, {}, None)

    body = mock_send.await_args.args[1]
    assert not body.startswith("🧪")
    # No "their monetary value is 0" footer: the message must end on the
    # threshold line, exactly as it did before testnet support existed.
    assert "value is 0" not in body
    assert body.rstrip().endswith("≥ 10.00")


@pytest.mark.asyncio
async def test_testnet_thresholds_do_not_fire_from_mainnet_config() -> None:
    """A mainnet threshold must not arm the testnet notifier."""
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from tasks import strk_notification as notifier

    user = _user()
    user.tracking_data = store_tracking(
        None,
        {"validators": [{"address": ADDR_B, "label": "Test V"}], "delegations": []},
        "sepolia",
    )
    # Threshold set on MAINNET only.
    user.set_notification_config({"token_thresholds": {"STRK": 10}})

    entry = SimpleNamespace(
        kind="validator", address=ADDR_B, label="Test V",
        data=SimpleNamespace(
            unclaimed_rewards_own_strk=Decimal("42"), attestation=None,
        ),
    )

    async def _fake_entries(_tracking, network=None):
        return [entry]

    with (
        patch.object(notifier, "fetch_tracking_entries", _fake_entries),
        patch.object(
            notifier, "_unclaimed_by_symbol", lambda _e: {"STRK": Decimal("42")}
        ),
        patch.object(notifier, "send_message", new=AsyncMock()) as mock_send,
    ):
        await notifier.start_parse_and_send_notification(user, {}, "sepolia")

    assert mock_send.await_count == 0


def test_html_shell_is_never_cached(client) -> None:
    """A stale HTML shell keeps pointing at the previous ``?v=`` asset
    version, so a deploy lands on the server while the user keeps running
    the old bundle. Observed in the wild: the Yield tab showing build-time
    APR constants hours after the on-chain source shipped."""
    for path in ("/", "/app/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers.get("content-type", ""), path
        assert "no-store" in r.headers.get("cache-control", ""), path


def test_versioned_assets_stay_cacheable(client) -> None:
    """Only the shell is no-store — versioning the assets would be
    pointless if they were uncacheable too."""
    r = client.get("/app/app.js")
    assert r.status_code == 200
    assert "no-store" not in r.headers.get("cache-control", "")
