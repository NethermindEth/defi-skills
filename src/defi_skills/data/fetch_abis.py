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


def fetch_and_cache(name: str, address: str) -> Optional[List[Dict]]:
    """
    Fetch ABI for a contract, handling proxies automatically.
    Caches the result in abi_cache/{address}.json.
    """
    cache_file = CACHE_DIR / f"{address.lower()}.json"
    if cache_file.exists():
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

    if abi:
        funcs = [e["name"] for e in abi if e.get("type") == "function"]
        # Save to cache
        cache_file.write_text(json.dumps(abi, indent=2))
        print(f" ✓ ({len(funcs)} functions)")
        return abi
    else:
        print(f" ✗ (not found)")
        return None


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