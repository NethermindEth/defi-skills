"""Fetch verified ABIs from Etherscan and cache them locally."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


ETHERSCAN_V2 = os.getenv("ETHERSCAN_URL", "https://api.etherscan.io/v2/api")
CACHE_DIR = Path(__file__).parent / "abi_cache"
CACHE_DIR.mkdir(exist_ok=True)


def etherscan_api_key() -> str:
    key = os.getenv("ETHERSCAN_API_KEY", "")
    if not key:
        print("Error: ETHERSCAN_API_KEY not set in .env")
        sys.exit(1)
    return key


def etherscan_get(params: Dict) -> Dict:
    """Make an Etherscan V2 API call with rate limiting."""
    params["apikey"] = etherscan_api_key()
    params["chainid"] = "1"
    resp = requests.get(ETHERSCAN_V2, params=params, timeout=15)
    time.sleep(0.25)  # Rate limit: 5 calls/sec on free tier
    return resp.json()


def get_implementation_address(address: str) -> Optional[str]:
    """Check if a contract is a proxy and return its implementation address."""
    data = etherscan_get({
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    })
    if data.get("status") == "1" and data.get("result"):
        result = data["result"][0]
        if result.get("Proxy") == "1" and result.get("Implementation"):
            return result["Implementation"]
    return None


def detect_multi_facet_proxy(address: str) -> bool:
    """Check source code for multi-facet proxy patterns (selectorToFacet, etc.)."""
    data = etherscan_get({
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    })
    if data.get("status") != "1" or not data.get("result"):
        return False
    source = data["result"][0].get("SourceCode", "")
    patterns = ["selectorToFacet", "selectorToAddress", "_facets", "FacetCut"]
    return any(p in source for p in patterns)


def get_diamond_facets(address: str) -> Optional[List[str]]:
    """Detect EIP-2535 Diamond proxy by calling facetAddresses() and return facet addresses."""
    # facetAddresses() selector = 0x52ef6b2c
    data = etherscan_get({
        "module": "proxy",
        "action": "eth_call",
        "to": address,
        "data": "0x52ef6b2c",
        "tag": "latest",
    })
    result = data.get("result", "0x")
    if not result or result == "0x" or len(result) < 130:
        return None

    try:
        hex_data = result[2:]
        # ABI-encoded address[]: first 32 bytes = offset, then length, then addresses
        offset = int(hex_data[0:64], 16) * 2  # byte offset → hex char offset
        length = int(hex_data[offset:offset + 64], 16)
        if length == 0 or length > 50:  # sanity check
            return None

        addresses = []
        for i in range(length):
            start = offset + 64 + (i * 64)
            addr_hex = hex_data[start:start + 64]
            addr = "0x" + addr_hex[24:]  # last 20 bytes of 32-byte slot
            if addr != "0x" + "0" * 40:  # skip zero address
                addresses.append(addr)

        return addresses if len(addresses) > 1 else None
    except (ValueError, IndexError):
        return None


def fetch_abi(address: str) -> Optional[List[Dict]]:
    """Fetch verified ABI from Etherscan V2."""
    data = etherscan_get({
        "module": "contract",
        "action": "getabi",
        "address": address,
    })
    if data.get("status") == "1" and data.get("result"):
        return json.loads(data["result"])
    return None


def fetch_and_cache(
    name: str, address: str, extra_facets: Optional[List[str]] = None,
) -> Optional[List[Dict]]:
    """
    Fetch ABI for a contract, handling proxies and Diamond proxies automatically.

    For standard EIP-2535 Diamonds, facets are detected via facetAddresses().
    For custom multi-facet proxies, pass extra_facets=[addr1, addr2, ...].

    Caches the merged result in abi_cache/{address}.json.
    """
    cache_file = CACHE_DIR / f"{address.lower()}.json"
    if cache_file.exists() and not extra_facets:
        abi = json.loads(cache_file.read_text())
        funcs = [e["name"] for e in abi if e.get("type") == "function"]
        print(f"  [cached] {name:25s} {address[:14]}... ({len(funcs)} functions)")
        return abi

    print(f"  Fetching {name:25s} {address[:14]}...", end="", flush=True)

    # Check if proxy
    impl_addr = get_implementation_address(address)
    if impl_addr:
        print(f" proxy → {impl_addr[:14]}...", end="", flush=True)
        abi = fetch_abi(impl_addr)
    else:
        abi = fetch_abi(address)

    if not abi:
        print(f" ✗ (not found)")
        return None

    # Collect facet addresses to merge from (standard Diamond + user-provided)
    facet_addrs: List[str] = []

    # 1. Try standard EIP-2535 Diamond Loupe
    diamond_facets = get_diamond_facets(address)
    if diamond_facets:
        print(f" diamond ({len(diamond_facets)} facets)...", end="", flush=True)
        facet_addrs.extend(diamond_facets)

    # 2. User-provided extra facets (for custom multi-facet proxies)
    if extra_facets:
        print(f" +{len(extra_facets)} extra facets...", end="", flush=True)
        facet_addrs.extend(extra_facets)

    # Merge facet ABIs
    if facet_addrs:
        seen_names = {e.get("name") for e in abi if e.get("type") == "function"}
        merged_count = 0
        for facet_addr in facet_addrs:
            if impl_addr and facet_addr.lower() == impl_addr.lower():
                continue  # already fetched
            facet_abi = fetch_abi(facet_addr)
            if facet_abi:
                facet_cache = CACHE_DIR / f"{facet_addr.lower()}.json"
                if not facet_cache.exists():
                    facet_cache.write_text(json.dumps(facet_abi, indent=2))
                for entry in facet_abi:
                    fname = entry.get("name", "")
                    if entry.get("type") == "function" and fname not in seen_names:
                        abi.append(entry)
                        seen_names.add(fname)
                        merged_count += 1
        if merged_count:
            print(f" +{merged_count} merged...", end="", flush=True)
    elif impl_addr and detect_multi_facet_proxy(address):
        # Warn: source code suggests multi-facet but we couldn't auto-detect facets
        print(f"\n  WARNING: {name} looks like a multi-facet proxy but facets "
              f"could not be auto-detected. Use --facets to provide facet addresses.", flush=True)

    funcs = [e["name"] for e in abi if e.get("type") == "function"]
    cache_file.write_text(json.dumps(abi, indent=2))
    print(f" ✓ ({len(funcs)} functions)")
    return abi


def find_function_in_abi(abi: List[Dict], func_name: str) -> Optional[Dict]:
    """Find a function entry by name in an ABI."""
    for entry in abi:
        if entry.get("type") == "function" and entry.get("name") == func_name:
            return entry
    return None


def main():
    playbooks_dir = Path(__file__).parent / "playbooks"

    print("ABI Bootstrap — Fetching from Etherscan V2")
    print("=" * 60)

    # Collect all unique contract addresses from playbook JSONs
    contracts: Dict[str, str] = {}  # address -> name
    playbooks = []
    for pb_file in sorted(playbooks_dir.glob("*.json")):
        pb = json.loads(pb_file.read_text())
        playbooks.append(pb)
        protocol = pb.get("protocol", pb_file.stem)
        for key, contract_info in pb.get("contracts", {}).items():
            addr = contract_info.get("address", "")
            if addr:
                contracts[addr] = f"{protocol}/{key}"

    print(f"Contracts to fetch: {len(contracts)}")
    print()

    # Fetch ABIs
    abi_map: Dict[str, List[Dict]] = {}  # address -> ABI
    for addr, name in sorted(contracts.items(), key=lambda x: x[1]):
        abi = fetch_and_cache(name, addr)
        if abi:
            abi_map[addr.lower()] = abi

    print()

    # Verify all playbook actions have their functions in the fetched ABIs
    print("Verifying action → function mapping:")
    print("-" * 60)

    all_ok = True
    for pb in playbooks:
        contracts_map = pb.get("contracts", {})
        for action_name, action_spec in pb.get("actions", {}).items():
            func_name = action_spec.get("function_name")
            target_key = action_spec.get("target_contract")
            if not func_name or not target_key:
                continue

            contract_info = contracts_map.get(target_key, {})
            target_addr = contract_info.get("address", "")
            abi = abi_map.get(target_addr.lower())

            if not abi:
                print(f"  ✗ {action_name:25s} — no ABI for {target_addr[:14]}...")
                all_ok = False
                continue

            func_entry = find_function_in_abi(abi, func_name)
            if func_entry:
                types = [i["type"] for i in func_entry.get("inputs", [])]
                sig = f"{func_name}({','.join(types)})"
                print(f"  ✓ {action_name:25s} — {sig}")
            else:
                print(f"  ✗ {action_name:25s} — '{func_name}' not found in ABI")
                all_ok = False

    print()
    if all_ok:
        print("All actions verified against Etherscan ABIs!")
    else:
        print("Some actions could not be verified — check errors above.")

    print(f"\nABI cache: {CACHE_DIR}")


if __name__ == "__main__":
    main()