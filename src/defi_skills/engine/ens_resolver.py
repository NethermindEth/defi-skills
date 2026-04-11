"""ENS name resolver - live forward lookup, best-effort reverse lookup."""

import os
from typing import Any, Optional


class ENSResolver:
    """Live-only ENS resolver. No persistent cache."""

    def __init__(self, w3=None, chain_id: int = 1):
        from defi_skills.engine.chains import get_chain_config, get_rpc_url

        self.chain_id = chain_id
        self.chain_config = get_chain_config(chain_id)

        self.w3: Any = w3
        if self.w3 is None:
            api_key = os.getenv("ALCHEMY_API_KEY")
            if api_key:
                try:
                    from web3 import Web3
                    rpc_url = get_rpc_url(chain_id)
                    self.w3 = Web3(Web3.HTTPProvider(rpc_url))
                except Exception:
                    self.w3 = None

    def resolve(self, name: str) -> str:
        """Forward lookup: ENS name -> checksummed address. Always live, never cached."""
        if not name:
            raise ValueError("resolve: empty ENS name")

        if not self.chain_config.ens_supported:
            raise ValueError(
                f"ENS is not available on {self.chain_config.name}. "
                f"Use a hex address (0x...) instead of '{name}'."
            )

        key = name.strip().lower()
        if not key.endswith(".eth"):
            key = key + ".eth"

        if self.w3 is None:
            raise ValueError(
                f"Cannot resolve ENS name '{key}': no RPC provider available. "
                f"Set ALCHEMY_API_KEY or use a hex address instead."
            )

        try:
            address = self.w3.ens.address(key)
        except Exception as e:
            raise ValueError(
                f"Cannot resolve ENS name '{key}': RPC call failed ({e}). "
                f"Use a hex address instead."
            ) from e

        if address is None:
            raise ValueError(
                f"ENS name '{key}' does not resolve to any address."
            )

        return str(address)

    def reverse(self, address: str) -> Optional[str]:
        """Reverse lookup: address -> ENS name. Returns None on failure."""
        if not address or self.w3 is None:
            return None
        if not self.chain_config.ens_supported:
            return None
        try:
            name = self.w3.ens.name(address)
            return name if name else None
        except Exception:
            return None
