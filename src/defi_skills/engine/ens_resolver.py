"""ENS name resolver — live forward lookup, best-effort reverse lookup."""

import os
from typing import Any, Optional


class ENSResolver:
    """Live-only ENS resolver. No persistent cache."""

    def __init__(self, w3=None):
        self.w3: Any = w3
        if self.w3 is None:
            api_key = os.getenv("ALCHEMY_API_KEY")
            if api_key:
                try:
                    from web3 import Web3
                    base_url = os.getenv("ALCHEMY_URL", "https://eth-mainnet.g.alchemy.com/v2")
                    provider_url = f"{base_url}/{api_key}"
                    self.w3 = Web3(Web3.HTTPProvider(provider_url))
                except Exception:
                    self.w3 = None

    def resolve(self, name: str) -> str:
        """Forward lookup: ENS name -> checksummed address. Always live, never cached."""
        if not name:
            raise ValueError("resolve: empty ENS name")
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
        try:
            name = self.w3.ens.name(address)
            return name if name else None
        except Exception:
            return None
