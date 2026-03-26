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
