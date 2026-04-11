"""Tests for ChainResources — per-chain contract address loader."""

import pytest

from defi_skills.engine.chain_resources import (
    load_chain_contracts,
    protocol_available,
    supported_chains_for_protocol,
    get_supported_protocols,
    _cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    _cache.clear()
    yield
    _cache.clear()


class TestLoadChainContracts:
    def test_loads_mainnet_aave(self):
        contracts = load_chain_contracts(1, "aave_v3")
        assert "pool" in contracts
        assert contracts["pool"]["address"] == "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
        assert contracts["pool"]["abi_source"] == "etherscan_cache"

    def test_loads_sepolia_weth(self):
        contracts = load_chain_contracts(11155111, "weth")
        assert "weth" in contracts
        assert contracts["weth"]["address"] == "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9"

    def test_missing_protocol_raises(self):
        with pytest.raises(ValueError, match="not available"):
            load_chain_contracts(1, "nonexistent_protocol")

    def test_missing_chain_raises(self):
        with pytest.raises(ValueError, match="not available"):
            load_chain_contracts(999999, "aave_v3")

    def test_error_message_includes_supported_chains(self):
        with pytest.raises(ValueError, match="Ethereum Mainnet"):
            load_chain_contracts(999999, "aave_v3")

    def test_caches_result(self):
        contracts1 = load_chain_contracts(1, "aave_v3")
        contracts2 = load_chain_contracts(1, "aave_v3")
        assert contracts1 is contracts2


class TestProtocolAvailable:
    def test_mainnet_aave_available(self):
        assert protocol_available(1, "aave_v3") is True

    def test_sepolia_aave_available(self):
        assert protocol_available(11155111, "aave_v3") is True

    def test_sepolia_lido_not_available(self):
        assert protocol_available(11155111, "lido") is False

    def test_unknown_chain(self):
        assert protocol_available(999999, "aave_v3") is False


class TestSupportedChainsForProtocol:
    def test_aave_on_mainnet_and_sepolia(self):
        chains = supported_chains_for_protocol("aave_v3")
        assert 1 in chains
        assert 11155111 in chains

    def test_lido_only_mainnet(self):
        chains = supported_chains_for_protocol("lido")
        assert chains == [1]

    def test_nonexistent_protocol(self):
        chains = supported_chains_for_protocol("nonexistent")
        assert chains == []


class TestGetSupportedProtocols:
    def test_mainnet_has_all_protocols(self):
        protocols = get_supported_protocols(1)
        assert "aave_v3" in protocols
        assert "uniswap_v3" in protocols
        assert "lido" in protocols
        assert len(protocols) == 12

    def test_sepolia_has_subset(self):
        protocols = get_supported_protocols(11155111)
        assert "aave_v3" in protocols
        assert "uniswap_v3" in protocols
        assert "weth" in protocols
        assert "lido" not in protocols
