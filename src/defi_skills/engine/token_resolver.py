"""Token resolver — local cache, on-chain query, and 1inch API fallback."""

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from eth_utils import is_address
from platformdirs import user_data_dir

PACKAGE_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SEED_CACHE_PATH = PACKAGE_DATA_DIR / "cache" / "token_cache.json"


def _default_cache_path() -> Path:
    """Return the platform-appropriate path for the mutable token cache."""
    return Path(user_data_dir("defi-skills", ensure_exists=True)) / "token_cache.json"

# Minimal ERC-20 ABI for on-chain queries
ERC20_MINIMAL_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

# Some old contracts (e.g. MKR) return bytes32 instead of string
ERC20_BYTES32_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"},
]


class TokenResolver:
    """Live token resolver with persistent file cache and optional on-chain/API lookups."""

    def __init__(self, cache_path: Optional[str] = None, w3=None):
        if cache_path:
            self.cache_path = Path(cache_path)
        else:
            self.cache_path = _default_cache_path()
            # First run: seed from package data if user cache doesn't exist
            if not self.cache_path.exists() and SEED_CACHE_PATH.exists():
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SEED_CACHE_PATH, self.cache_path)

        # In-memory indexes
        self.erc20_by_symbol: Dict[str, Dict] = {}   # uppercase symbol -> {address, decimals, symbol, name}
        self.erc20_by_address: Dict[str, Dict] = {}   # lowercased address -> same dict
        self.erc721_collections: Dict[str, Dict] = {}  # lowercased alias -> {address, name, symbol}

        self.load_cache()

        # Initialize web3 provider (or use shared instance)
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

        self.oneinch_api_key = os.getenv("ONEINCH_API_KEY")

    # Primary API

    def get_by_address(self, address: str) -> Optional[Dict]:
        """Address -> {address, decimals, symbol, name} from cache."""
        if not address:
            return None
        return self.erc20_by_address.get(address.lower())

    def resolve_erc20(self, symbol: str) -> Optional[Dict]:
        """Symbol -> {address, decimals, symbol, name}. Tier 1 -> Tier 3 -> Tier 2."""
        if not symbol:
            return None
        key = symbol.strip().upper()

        # Tier 1: cache
        if key in self.erc20_by_symbol:
            return self.erc20_by_symbol[key]

        # Tier 3: 1inch API (symbol -> address)
        result = self.query_1inch(key)
        if result:
            # Tier 2: confirm metadata on-chain
            on_chain = self.query_on_chain(result["address"])
            if on_chain:
                result = on_chain
            self.add_erc20(result)
            return result

        return None

    def resolve_by_address(self, address: str) -> Optional[Dict]:
        """Address -> {address, decimals, symbol, name}. Tier 1 -> Tier 2."""
        if not address:
            return None
        key = address.strip().lower()

        # Tier 1: cache
        if key in self.erc20_by_address:
            return self.erc20_by_address[key]

        # Tier 2: on-chain query
        result = self.query_on_chain(address)
        if result:
            self.add_erc20(result)
            return result

        return None

    def resolve_collection(self, name_or_alias: str) -> Optional[Dict]:
        """Collection alias -> {address, name, symbol}. Cache -> Alchemy NFT API."""
        if not name_or_alias:
            return None
        s = name_or_alias.strip()

        # Direct hex address — query Alchemy for metadata
        if is_address(s):
            cached = self.collection_by_address(s)
            if cached:
                return cached
            result = self.query_nft_metadata(s)
            if result:
                self.add_collection(result)
                return result
            return {"address": s}

        # Try lowercased alias key
        key = s.lower().replace(" ", "")
        if key in self.erc721_collections:
            return self.erc721_collections[key]

        # Fuzzy match: check against collection name field
        for alias, info in self.erc721_collections.items():
            if (info.get("name") or alias).lower() == s.lower():
                return info

        # Tier 2: Alchemy NFT search API
        result = self.search_nft_collection(s)
        if result:
            self.add_collection(result)
            return result

        return None

    def collection_by_address(self, address: str) -> Optional[Dict]:
        """Reverse lookup: address -> collection info (cache only)."""
        if not address:
            return None
        addr_lower = address.lower()
        for alias, info in self.erc721_collections.items():
            if info.get("address", "").lower() == addr_lower:
                return info
        return None

    # Convenience

    def known_erc20_symbols(self) -> List[str]:
        """Return all cached ERC-20 symbols."""
        return list(self.erc20_by_symbol.keys())

    def known_collection_aliases(self) -> List[str]:
        """Return all cached ERC-721 collection aliases."""
        return list(self.erc721_collections.keys())

    def symbol_for_address(self, address: str) -> Optional[str]:
        """Reverse lookup: address -> symbol (cache only)."""
        if not address:
            return None
        info = self.erc20_by_address.get(address.lower())
        return info["symbol"] if info else None

    # Internals

    def query_on_chain(self, address: str) -> Optional[Dict]:
        """Tier 2: query decimals(), symbol(), name() on-chain via web3."""
        if self.w3 is None:
            return None
        try:
            from web3 import Web3
            addr = Web3.to_checksum_address(address)
            contract = self.w3.eth.contract(address=addr, abi=ERC20_MINIMAL_ABI)

            decimals = contract.functions.decimals().call()

            # Try string ABI first, fall back to bytes32
            try:
                symbol = contract.functions.symbol().call()
            except Exception:
                contract_b32 = self.w3.eth.contract(address=addr, abi=ERC20_BYTES32_ABI)
                raw = contract_b32.functions.symbol().call()
                symbol = raw.rstrip(b"\x00").decode("utf-8") if isinstance(raw, bytes) else str(raw)

            try:
                name = contract.functions.name().call()
            except Exception:
                contract_b32 = self.w3.eth.contract(address=addr, abi=ERC20_BYTES32_ABI)
                raw = contract_b32.functions.name().call()
                name = raw.rstrip(b"\x00").decode("utf-8") if isinstance(raw, bytes) else str(raw)

            return {
                "address": addr,
                "decimals": int(decimals),
                "symbol": symbol,
                "name": name,
            }
        except Exception:
            return None

    def query_1inch(self, symbol: str) -> Optional[Dict]:
        """Tier 3: 1inch Token API — symbol -> {address, decimals, symbol, name}."""
        if not self.oneinch_api_key:
            return None
        try:
            import requests
            base_url = os.getenv("ONEINCH_URL", "https://api.1inch.com/token/v1.4/1")
            url = f"{base_url}/search"
            headers = {"Authorization": f"Bearer {self.oneinch_api_key}"}
            params = {
                "query": symbol,
                "limit": 1,
                "only_positive_rating": "false",
            }
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                return None
            for token in data:
                if token.get("symbol", "").upper() == symbol.upper():
                    return {
                        "address": token["address"],
                        "decimals": token.get("decimals", 18),
                        "symbol": token["symbol"],
                        "name": token.get("name", token["symbol"]),
                    }
            return None
        except Exception:
            return None

    def query_nft_metadata(self, contract_address: str) -> Optional[Dict]:
        """Alchemy NFT API: get collection metadata by contract address."""
        api_key = os.getenv("ALCHEMY_API_KEY")
        if not api_key:
            return None
        try:
            import requests
            url = f"https://eth-mainnet.g.alchemy.com/nft/v3/{api_key}/getContractMetadata"
            resp = requests.get(url, params={"contractAddress": contract_address}, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            token_type = data.get("tokenType", "")
            if token_type not in ("ERC721", "ERC1155"):
                return None

            name = data.get("name", "")
            symbol = data.get("symbol", "")
            if not name and not symbol:
                return None

            return {
                "address": data.get("address", contract_address),
                "name": name,
                "symbol": symbol,
            }
        except Exception:
            return None

    def search_nft_collection(self, query: str) -> Optional[Dict]:
        """Alchemy NFT API: search collections by name."""
        api_key = os.getenv("ALCHEMY_API_KEY")
        if not api_key:
            return None
        try:
            import requests
            url = f"https://eth-mainnet.g.alchemy.com/nft/v3/{api_key}/searchContractMetadata"
            resp = requests.get(url, params={"query": query}, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            # Response format: {"contracts": [...]}
            contracts = data.get("contracts", []) if isinstance(data, dict) else data
            if not contracts or not isinstance(contracts, list):
                return None

            # Return the first ERC721 result
            for item in contracts:
                token_type = item.get("tokenType", "")
                if token_type not in ("ERC721", "ERC1155"):
                    continue
                name = item.get("name", "")
                symbol = item.get("symbol", "")
                address = item.get("address", "")
                if address:
                    return {
                        "address": address,
                        "name": name,
                        "symbol": symbol,
                    }
            return None
        except Exception:
            return None

    def add_erc20(self, info: Dict) -> None:
        """Add a new ERC-20 entry to in-memory indexes and persist cache."""
        sym = info["symbol"].upper()
        self.erc20_by_symbol[sym] = info
        self.erc20_by_address[info["address"].lower()] = info
        self.save_cache()

    def add_collection(self, info: Dict) -> None:
        """Add a new ERC-721 collection to in-memory indexes and persist cache."""
        name = info.get("name", "")
        symbol = info.get("symbol", "")
        # Store under multiple keys for fuzzy matching
        keys = set()
        if name:
            keys.add(name.lower().replace(" ", ""))
        if symbol:
            keys.add(symbol.lower())
        for key in keys:
            if key:
                self.erc721_collections[key] = info
        self.save_cache()

    def load_cache(self) -> None:
        """Load cache from JSON file and build in-memory indexes."""
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        for sym, info in data.get("erc20_tokens", {}).items():
            self.erc20_by_symbol[sym.upper()] = info
            self.erc20_by_address[info.get("address", "").lower()] = info

        for alias, info in data.get("erc721_collections", {}).items():
            self.erc721_collections[alias.lower()] = info

    def save_cache(self) -> None:
        """Persist current state to JSON file."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "erc20_tokens": {info["symbol"]: info for info in self.erc20_by_symbol.values()},
            "erc721_collections": self.erc721_collections,
        }
        self.cache_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
