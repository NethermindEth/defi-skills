"""Verify Sepolia playbooks load and build correctly."""

import pytest
from unittest.mock import MagicMock

from defi_skills.engine.playbook_engine import PlaybookEngine
from defi_skills.engine.chains import get_chain_config

SEPOLIA_CHAIN_ID = 11155111
FROM_ADDRESS = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


@pytest.fixture
def engine():
    """Engine with mocked resolvers."""
    mock_tr = MagicMock()
    mock_tr.resolve_erc20.return_value = {
        "address": "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8",
        "decimals": 6, "symbol": "USDC", "name": "USD Coin",
    }
    mock_tr.w3 = MagicMock()
    mock_er = MagicMock()
    return PlaybookEngine(token_resolver=mock_tr, ens_resolver=mock_er)


def test_sepolia_actions_loaded(engine):
    actions = engine.get_supported_actions(SEPOLIA_CHAIN_ID)
    assert len(actions) > 0


def test_sepolia_has_expected_protocols(engine):
    protocols = list(engine.get_actions_by_protocol(SEPOLIA_CHAIN_ID).keys())
    assert "aave_v3" in protocols
    assert "uniswap_v3" in protocols
    assert "weth" in protocols
    assert "transfers" in protocols


def test_mainnet_actions_still_loaded(engine):
    actions = engine.get_supported_actions(1)
    assert len(actions) >= 44


def test_mainnet_only_action_not_on_sepolia(engine):
    """Lido, EigenLayer, etc. should not exist on Sepolia."""
    sepolia_actions = engine.get_supported_actions(SEPOLIA_CHAIN_ID)
    assert "lido_stake" not in sepolia_actions
    assert "eigenlayer_deposit" not in sepolia_actions
    assert "curve_swap" not in sepolia_actions


def test_sepolia_build_payload_returns_none_for_missing_action(engine):
    """Requesting a mainnet-only action with Sepolia chain_id returns None."""
    result = engine.build_payload(
        {"action": "lido_stake", "arguments": {"amount": "1"}},
        chain_id=SEPOLIA_CHAIN_ID,
        from_address=FROM_ADDRESS,
    )
    assert result is None


def test_ens_not_supported_on_sepolia():
    cfg = get_chain_config(SEPOLIA_CHAIN_ID)
    assert cfg.ens_supported is False


def test_sepolia_is_testnet():
    cfg = get_chain_config(SEPOLIA_CHAIN_ID)
    assert cfg.is_testnet is True
    assert cfg.oneinch_chain_id is None


def test_sepolia_chain_id_in_playbooks(engine):
    """All Sepolia playbooks should report chain_id 11155111."""
    for action_name in engine.get_supported_actions(SEPOLIA_CHAIN_ID):
        pb = engine.playbook_meta[SEPOLIA_CHAIN_ID][action_name]
        assert pb.get("chain_id") == SEPOLIA_CHAIN_ID, f"{action_name} has wrong chain_id"
