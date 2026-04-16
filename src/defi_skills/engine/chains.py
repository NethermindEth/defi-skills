"""Chain configuration registry - single source of truth for per-chain settings."""

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional


@dataclass(frozen=True)
class ChainConfig:
    """Immutable configuration for a single EVM chain."""
    chain_id: int
    name: str
    short_name: str
    alchemy_network_slug: str
    oneinch_chain_id: Optional[str]
    ens_supported: bool
    is_testnet: bool
    native_symbol: str
    weth_address: str
    approve_reset_tokens: FrozenSet[str] = field(default_factory=frozenset)


CHAIN_REGISTRY: Dict[int, ChainConfig] = {
    1: ChainConfig(
        chain_id=1,
        name="Ethereum Mainnet",
        short_name="mainnet",
        alchemy_network_slug="eth-mainnet",
        oneinch_chain_id="1",
        ens_supported=True,
        is_testnet=False,
        native_symbol="ETH",
        weth_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        approve_reset_tokens=frozenset([
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
        ]),
    ),
    42161: ChainConfig(
        chain_id=42161,
        name="Arbitrum One",
        short_name="arbitrum",
        alchemy_network_slug="arb-mainnet",
        oneinch_chain_id="42161",
        ens_supported=False,
        is_testnet=False,
        native_symbol="ETH",
        weth_address="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        approve_reset_tokens=frozenset(),
    ),
    8453: ChainConfig(
        chain_id=8453,
        name="Base",
        short_name="base",
        alchemy_network_slug="base-mainnet",
        oneinch_chain_id="8453",
        ens_supported=False,
        is_testnet=False,
        native_symbol="ETH",
        weth_address="0x4200000000000000000000000000000000000006",
        approve_reset_tokens=frozenset(),
    ),
    10: ChainConfig(
        chain_id=10,
        name="Optimism",
        short_name="optimism",
        alchemy_network_slug="opt-mainnet",
        oneinch_chain_id="10",
        ens_supported=False,
        is_testnet=False,
        native_symbol="ETH",
        weth_address="0x4200000000000000000000000000000000000006",
        approve_reset_tokens=frozenset(),
    ),
    137: ChainConfig(
        chain_id=137,
        name="Polygon",
        short_name="polygon",
        alchemy_network_slug="polygon-mainnet",
        oneinch_chain_id="137",
        ens_supported=False,
        is_testnet=False,
        native_symbol="POL",
        weth_address="0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
        approve_reset_tokens=frozenset(),
    ),
    11155111: ChainConfig(
        chain_id=11155111,
        name="Sepolia",
        short_name="sepolia",
        alchemy_network_slug="eth-sepolia",
        oneinch_chain_id=None,
        ens_supported=False,
        is_testnet=True,
        native_symbol="ETH",
        weth_address="0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9",
        approve_reset_tokens=frozenset(),
    ),
}


def get_chain_config(chain_id: int) -> ChainConfig:
    """Get config for a chain. Raises ValueError if unsupported."""
    if chain_id not in CHAIN_REGISTRY:
        supported = ", ".join(f"{c.name} ({c.chain_id})" for c in CHAIN_REGISTRY.values())
        raise ValueError(f"Unsupported chain ID: {chain_id}. Supported: {supported}")
    return CHAIN_REGISTRY[chain_id]


def get_rpc_url(chain_id: int) -> str:
    """Build the full Alchemy RPC URL for a chain."""
    cfg = get_chain_config(chain_id)
    api_key = os.getenv("ALCHEMY_API_KEY", "")
    env_override = os.getenv(f"ALCHEMY_URL_{cfg.short_name.upper()}")
    if env_override:
        return f"{env_override}/{api_key}" if api_key else env_override
    base = os.getenv("ALCHEMY_URL") if chain_id == 1 else None
    if base:
        return f"{base}/{api_key}" if api_key else base
    return f"https://{cfg.alchemy_network_slug}.g.alchemy.com/v2/{api_key}"


def supported_chain_ids() -> List[int]:
    """Return all registered chain IDs."""
    return list(CHAIN_REGISTRY.keys())


def get_approve_reset_tokens(chain_id: int) -> FrozenSet[str]:
    """Get tokens requiring approve-reset for a chain."""
    return get_chain_config(chain_id).approve_reset_tokens


NATIVE_ETH_SENTINELS = frozenset({
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "0x0000000000000000000000000000000000000000",
})


def is_native_sentinel(addr: Optional[str]) -> bool:
    """True if addr is a known placeholder for native ETH (not a real ERC-20)."""
    return bool(addr) and addr.lower() in NATIVE_ETH_SENTINELS
