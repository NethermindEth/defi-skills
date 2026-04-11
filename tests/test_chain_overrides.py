"""Tests for chain-specific action and token overrides."""

import pytest
from unittest.mock import MagicMock

from defi_skills.engine.resolvers.common import ResolveContext, resolve_decimals
from defi_skills.engine.resolvers.core import resolve_token_address, resolve_amount


class TestTokenOverrides:
    def _make_ctx(self, token_overrides=None):
        mock_tr = MagicMock()
        mock_tr.resolve_erc20.return_value = {
            "address": "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9",
            "decimals": 18, "symbol": "WETH", "name": "Wrapped Ether",
        }
        mock_tr.resolve_by_address.return_value = None
        mock_tr.query_on_chain.return_value = None
        return ResolveContext(
            token_resolver=mock_tr,
            ens_resolver=MagicMock(),
            from_address="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            chain_id=11155111,
            action="aave_supply",
            raw_args={},
            token_overrides=token_overrides or {},
        )

    def test_override_replaces_token_address(self):
        ctx = self._make_ctx(token_overrides={
            "WETH": "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c",
        })
        result = resolve_token_address("WETH", ctx)
        assert result == "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c"
        ctx.token_resolver.resolve_erc20.assert_not_called()

    def test_override_case_insensitive(self):
        ctx = self._make_ctx(token_overrides={
            "WETH": "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c",
        })
        result = resolve_token_address("weth", ctx)
        assert result == "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c"

    def test_no_override_falls_through_to_resolver(self):
        ctx = self._make_ctx(token_overrides={})
        result = resolve_token_address("WETH", ctx)
        assert result == "0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9"
        ctx.token_resolver.resolve_erc20.assert_called_once()

    def test_override_only_affects_matching_symbol(self):
        ctx = self._make_ctx(token_overrides={
            "WETH": "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c",
        })
        ctx.token_resolver.resolve_erc20.return_value = {
            "address": "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8",
            "decimals": 6, "symbol": "USDC", "name": "USD Coin",
        }
        result = resolve_token_address("USDC", ctx)
        assert result == "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8"

    def test_override_populates_decimals_cache(self):
        ctx = self._make_ctx(token_overrides={
            "WETH": "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c",
        })
        resolve_token_address("WETH", ctx)
        assert "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c" in ctx.decimals_cache

    def test_override_decimals_used_in_amount_resolution(self):
        """When an override token has different decimals than the canonical
        symbol, resolve_amount must use the override's decimals, not the
        canonical token's.

        Scenario: canonical USDC is 6 decimals, but the override address
        (Aave's testnet USDC) has 18 decimals.  Amount "1" should encode
        as 1e18, not 1e6.
        """
        mock_tr = MagicMock()
        # Canonical USDC in the token cache: 6 decimals
        mock_tr.resolve_erc20.return_value = {
            "address": "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8",
            "decimals": 6, "symbol": "USDC", "name": "USD Coin",
        }
        # Override address NOT in the token cache (resolve_by_address returns None)
        mock_tr.resolve_by_address.return_value = None
        # On-chain query for override returns 18 decimals
        mock_tr.query_on_chain.return_value = {
            "address": "0xOverrideUSDC000000000000000000000000000000",
            "decimals": 18, "symbol": "USDC", "name": "Test USDC",
        }
        ctx = ResolveContext(
            token_resolver=mock_tr,
            ens_resolver=MagicMock(),
            from_address="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            chain_id=11155111,
            action="aave_supply",
            raw_args={"asset": "USDC"},
            token_overrides={"USDC": "0xOverrideUSDC000000000000000000000000000000"},
        )
        # Step 1: resolve_token_address caches override's 18 decimals under "USDC"
        addr = resolve_token_address("USDC", ctx)
        assert addr == "0xOverrideUSDC000000000000000000000000000000"
        assert ctx.decimals_cache["USDC"] == 18

        # Step 2: resolve_decimals("$asset") must pick up 18, not 6
        decimals = resolve_decimals("$asset", ctx)
        assert decimals == 18, (
            f"Expected 18 (override) but got {decimals} (canonical). "
            f"resolve_decimals is ignoring the override decimals cache."
        )

        # Step 3: full amount resolution should use 18 decimals
        result = resolve_amount("1", ctx, decimals_from="$asset")
        assert result == "1000000000000000000", (
            f"Expected 1e18 but got {result}. Amount scaled with wrong decimals."
        )


from defi_skills.engine.playbook_engine import PlaybookEngine
from defi_skills.engine.chain_resources import _cache as chain_cache


class TestSepoliaIntegration:
    """Verify overrides produce correct outputs for Sepolia actions."""

    @pytest.fixture(autouse=True)
    def clear_chain_cache(self):
        chain_cache.clear()
        yield
        chain_cache.clear()

    @pytest.fixture
    def engine(self):
        return PlaybookEngine()

    def test_sepolia_uniswap_swap_uses_router02_selector(self, engine):
        contracts = engine.get_contracts("uniswap_swap", 11155111)
        overrides = contracts.get("action_overrides", {}).get("uniswap_swap", {})
        assert overrides.get("function_selector") == "0x04e45aaf"
        pm = overrides.get("param_mapping", [{}])[0]
        field_names = [f["name"] for f in pm.get("fields", [])]
        assert "deadline" not in field_names
        assert len(field_names) == 7

    def test_sepolia_aave_has_token_overrides(self, engine):
        contracts = engine.get_contracts("aave_supply", 11155111)
        overrides = contracts.get("token_overrides", {})
        assert overrides.get("WETH") == "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c"
        assert overrides.get("LINK") == "0xf8Fb3713D459D7C1018BD0A49D19b4C44290EBE5"

    def test_mainnet_uniswap_no_overrides(self, engine):
        contracts = engine.get_contracts("uniswap_swap", 1)
        assert "action_overrides" not in contracts

    def test_mainnet_aave_no_token_overrides(self, engine):
        contracts = engine.get_contracts("aave_supply", 1)
        assert "token_overrides" not in contracts

    def test_mainnet_swap_uses_original_selector(self, engine):
        spec = engine.playbooks["uniswap_swap"]
        assert spec.get("function_selector") == "0x414bf389"
        pm = spec.get("param_mapping", [{}])[0]
        field_names = [f["name"] for f in pm.get("fields", [])]
        assert "deadline" in field_names
        assert len(field_names) == 8
