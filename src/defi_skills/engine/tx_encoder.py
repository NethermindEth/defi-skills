"""ABI-driven transaction encoder."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address


# ABI helpers

def resolve_abi_type(inp: Dict) -> str:
    """Resolve ABI input type, expanding tuple/struct types recursively."""
    typ = inp.get("type", "")
    if typ == "tuple":
        comps = inp.get("components", [])
        inner = ",".join(resolve_abi_type(c) for c in comps)
        return f"({inner})"
    if typ == "tuple[]":
        comps = inp.get("components", [])
        inner = ",".join(resolve_abi_type(c) for c in comps)
        return f"({inner})[]"
    return typ


def compute_selector(abi_entry: Dict) -> str:
    """Compute 4-byte selector from an ABI function entry (the canonical way)."""
    name = abi_entry["name"]
    types = [resolve_abi_type(inp) for inp in abi_entry.get("inputs", [])]
    sig = f"{name}({','.join(types)})"
    return "0x" + keccak(sig.encode()).hex()[:8]


def encode_from_abi(abi_entry: Dict, values: List[Any]) -> str:
    """Encode calldata: selector + ABI-encoded params."""
    types = [resolve_abi_type(inp) for inp in abi_entry.get("inputs", [])]
    selector = compute_selector(abi_entry)
    if types:
        encoded = abi_encode(types, values)
        return selector + encoded.hex()
    return selector


# Cached ABI loader

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ABI_CACHE_DIR = DATA_DIR / "abi_cache"

abi_cache: Dict[str, List[Dict]] = {}  # address.lower() -> full ABI


def load_contract_abi(address: str) -> Optional[List[Dict]]:
    """Load a cached Etherscan ABI for a contract address."""
    key = address.lower()
    if key in abi_cache:
        return abi_cache[key]
    cache_file = ABI_CACHE_DIR / f"{key}.json"
    if not cache_file.exists():
        return None
    abi = json.loads(cache_file.read_text())
    abi_cache[key] = abi
    return abi


def find_function_in_abi(abi: List[Dict], func_name: str, selector: str = None) -> Optional[Dict]:
    """Find a function entry by name (or exact selector) in an ABI list."""
    if selector:
        selector = selector.lower()
        for entry in abi:
            if entry.get("type") != "function":
                continue
            entry_selector = compute_selector(entry).lower()
            if entry_selector == selector:
                return entry

    # Fallback: match by name
    for entry in abi:
        if entry.get("type") == "function" and entry.get("name") == func_name:
            return entry
    return None


# Address helper

def normalize_address(a: str) -> str:
    """Normalise to EIP-55 checksum address (required by eth_abi >= 5)."""
    if not a:
        raise ValueError("normalize_address received empty/None address")
    s = a if a.startswith("0x") else "0x" + a
    # Must be a full 20-byte hex address (42 chars including '0x' prefix)
    if len(s) != 42:
        raise ValueError(f"normalize_address received invalid address (length {len(s)}): {s!r}")
    return to_checksum_address(s)
