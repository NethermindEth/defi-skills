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


def etherscan_get(params: Dict, chain_id: int = 1) -> Dict:
    """Make an Etherscan V2 API call with rate limiting."""
    params["apikey"] = etherscan_api_key()
    params["chainid"] = str(chain_id)
    resp = requests.get(ETHERSCAN_V2, params=params, timeout=15)
    time.sleep(0.25)  # Rate limit: 5 calls/sec on free tier
    return resp.json()


def get_implementation_address(address: str, chain_id: int = 1) -> Optional[str]:
    """Check if a contract is a proxy and return its implementation address."""
    data = etherscan_get({
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    }, chain_id=chain_id)
    if data.get("status") == "1" and data.get("result"):
        result = data["result"][0]
        if result.get("Proxy") == "1" and result.get("Implementation"):
            return result["Implementation"]
    return None


def detect_multi_facet_proxy(address: str, chain_id: int = 1) -> bool:
    """Check source code for multi-facet proxy patterns (selectorToFacet, etc.)."""
    data = etherscan_get({
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    }, chain_id=chain_id)
    if data.get("status") != "1" or not data.get("result"):
        return False
    source = data["result"][0].get("SourceCode", "")
    patterns = ["selectorToFacet", "selectorToAddress", "_facets", "FacetCut"]
    return any(p in source for p in patterns)


def get_diamond_facets(address: str, chain_id: int = 1) -> Optional[List[str]]:
    """Detect EIP-2535 Diamond proxy by calling facetAddresses() and return facet addresses."""
    # facetAddresses() selector = 0x52ef6b2c
    data = etherscan_get({
        "module": "proxy",
        "action": "eth_call",
        "to": address,
        "data": "0x52ef6b2c",
        "tag": "latest",
    }, chain_id=chain_id)
    result = data.get("result", "0x")
    if not result or result == "0x" or len(result) < 130:
        return None

    try:
        hex_data = result[2:]
        # ABI-encoded address[]: first 32 bytes = offset, then length, then addresses
        offset = int(hex_data[0:64], 16) * 2  # byte offset -> hex char offset
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


def fetch_abi(address: str, chain_id: int = 1) -> Optional[List[Dict]]:
    """Fetch verified ABI from Etherscan V2."""
    data = etherscan_get({
        "module": "contract",
        "action": "getabi",
        "address": address,
    }, chain_id=chain_id)
    if data.get("status") == "1" and data.get("result"):
        return json.loads(data["result"])
    return None


def fetch_and_cache(
    name: str, address: str, extra_facets: Optional[List[str]] = None,
    chain_id: int = 1,
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
    impl_addr = get_implementation_address(address, chain_id=chain_id)
    if impl_addr:
        print(f" proxy → {impl_addr[:14]}...", end="", flush=True)
        abi = fetch_abi(impl_addr, chain_id=chain_id)
    else:
        abi = fetch_abi(address, chain_id=chain_id)

    if not abi:
        print(f" ✗ (not found)")
        return None

    # Collect facet addresses to merge from (standard Diamond + user-provided)
    facet_addrs: List[str] = []

    # 1. Try standard EIP-2535 Diamond Loupe
    diamond_facets = get_diamond_facets(address, chain_id=chain_id)
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
            facet_abi = fetch_abi(facet_addr, chain_id=chain_id)
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
    elif impl_addr and detect_multi_facet_proxy(address, chain_id=chain_id):
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


def collect_chain_resource_contracts(data_dir: Path) -> Dict[str, tuple]:
    """Scan data/chains/{chain_id}/{protocol}.json for contract addresses."""
    contracts: Dict[str, tuple] = {}
    chains_dir = data_dir / "chains"
    if not chains_dir.exists():
        return contracts
    for chain_dir in sorted(chains_dir.iterdir()):
        if not chain_dir.is_dir():
            continue
        try:
            chain_id = int(chain_dir.name)
        except ValueError:
            continue
        for resource_file in sorted(chain_dir.glob("*.json")):
            protocol = resource_file.stem
            resource = json.loads(resource_file.read_text())
            for key, value in resource.items():
                # Skip non-address keys (action_overrides, token_overrides, etc.)
                if not isinstance(value, str) or not value.startswith("0x"):
                    continue
                name = f"{protocol}/{key}"
                # Don't overwrite if already seen (dedup by address)
                if value not in contracts:
                    contracts[value] = (name, chain_id)
    return contracts


def main():
    data_dir = Path(__file__).parent
    playbooks_dir = data_dir / "playbooks"

    print("ABI Bootstrap — Fetching from Etherscan V2")
    print("=" * 60)

    # Collect contract addresses from ChainResources (data/chains/)
    contracts: Dict[str, tuple] = collect_chain_resource_contracts(data_dir)

    print(f"Contracts to fetch: {len(contracts)}")
    print()

    # Fetch ABIs
    abi_map: Dict[str, List[Dict]] = {}  # address -> ABI
    for addr, (name, chain_id) in sorted(contracts.items(), key=lambda x: x[1][0]):
        abi = fetch_and_cache(name, addr, chain_id=chain_id)
        if abi:
            abi_map[addr.lower()] = abi

    print()

    # Verify: for each playbook action, check the target contract has the function in its ABI
    print("Verifying action → function mapping:")
    print("-" * 60)

    all_ok = True
    for pb_file in sorted(playbooks_dir.glob("*.json")):
        pb = json.loads(pb_file.read_text())
        protocol = pb.get("protocol", pb_file.stem)
        for action_name, action_spec in pb.get("actions", {}).items():
            func_name = action_spec.get("function_name")
            target_key = action_spec.get("target_contract")
            if not func_name or not target_key:
                continue

            # Find any chain resource that has this target_key for this protocol
            verified = False
            for chain_dir in sorted((data_dir / "chains").iterdir()):
                if not chain_dir.is_dir():
                    continue
                resource_file = chain_dir / f"{protocol}.json"
                if not resource_file.exists():
                    continue
                resource = json.loads(resource_file.read_text())
                target_addr = resource.get(target_key, "")
                if not target_addr or not isinstance(target_addr, str):
                    continue
                abi = abi_map.get(target_addr.lower())
                if not abi:
                    continue
                func_entry = find_function_in_abi(abi, func_name)
                if func_entry:
                    types = [i["type"] for i in func_entry.get("inputs", [])]
                    sig = f"{func_name}({','.join(types)})"
                    print(f"  ✓ {action_name:25s} — {sig}")
                    verified = True
                    break

            if not verified:
                print(f"  ✗ {action_name:25s} — '{func_name}' not found in any chain ABI")
                all_ok = False

    print()
    if all_ok:
        print("All actions verified against Etherscan ABIs!")
    else:
        print("Some actions could not be verified — check errors above.")

    print(f"\nABI cache: {CACHE_DIR}")


if __name__ == "__main__":
    main()