"""ChainResources — per-chain contract address loader.

Loads contract addresses from data/chains/{chain_id}/{protocol}.json files.
Wraps raw addresses into the {"address": addr, "abi_source": "etherscan_cache"}
format expected by PlaybookEngine.
"""

import json
from pathlib import Path
from typing import Dict, List

from defi_skills.engine.chains import CHAIN_REGISTRY

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHAINS_DIR = DATA_DIR / "chains"
_CHAINS_DIR_RESOLVED = CHAINS_DIR.resolve()

_cache: Dict[tuple, Dict[str, Dict]] = {}


def _chain_file(chain_id: int, protocol: str) -> Path:
    """Build path to a chain resource file, with path traversal protection."""
    safe_id = int(chain_id)
    if "/" in protocol or "\\" in protocol or ".." in protocol or not protocol:
        raise ValueError(f"Invalid protocol name: {protocol!r}")
    path = (CHAINS_DIR / str(safe_id) / f"{protocol}.json").resolve()
    if not path.is_relative_to(_CHAINS_DIR_RESOLVED):
        raise ValueError(f"Invalid path for chain_id={chain_id}, protocol={protocol!r}")
    return path


def load_chain_contracts(chain_id: int, protocol: str) -> Dict[str, Dict]:
    """Load contract addresses for a protocol on a specific chain.

    Returns dict of {contract_key: {"address": addr, "abi_source": "etherscan_cache"}}.
    Raises ValueError if the protocol is not available on the chain.
    """
    cache_key = (chain_id, protocol)
    if cache_key in _cache:
        return _cache[cache_key]

    path = _chain_file(chain_id, protocol)
    if not path.exists():
        supported = supported_chains_for_protocol(protocol)
        if supported:
            chain_names = []
            for cid in supported:
                cfg = CHAIN_REGISTRY.get(cid)
                name = f"{cfg.name} ({cid})" if cfg else str(cid)
                chain_names.append(name)
            raise ValueError(
                f"{protocol} is not available on chain {chain_id}. "
                f"Supported chains: {', '.join(chain_names)}"
            )
        raise ValueError(
            f"{protocol} is not available on chain {chain_id}. "
            f"No chain contract files found for this protocol."
        )

    raw = json.loads(path.read_text())

    contracts = {}
    for key, value in raw.items():
        if isinstance(value, str):
            contracts[key] = {"address": value, "abi_source": "etherscan_cache"}
        else:
            contracts[key] = value

    _cache[cache_key] = contracts
    return contracts


def protocol_available(chain_id: int, protocol: str) -> bool:
    """Check if a protocol has contract addresses for a given chain."""
    return _chain_file(chain_id, protocol).exists()


def supported_chains_for_protocol(protocol: str) -> List[int]:
    """Return all chain IDs that have contract files for a protocol."""
    if not CHAINS_DIR.exists():
        return []
    chains = []
    for chain_dir in sorted(CHAINS_DIR.iterdir()):
        if chain_dir.is_dir() and (chain_dir / f"{protocol}.json").exists():
            try:
                chains.append(int(chain_dir.name))
            except ValueError:
                continue
    return chains


def get_supported_protocols(chain_id: int) -> List[str]:
    """Return all protocols available on a given chain."""
    safe_id = int(chain_id)
    chain_dir = (CHAINS_DIR / str(safe_id)).resolve()
    if not chain_dir.is_relative_to(_CHAINS_DIR_RESOLVED):
        return []
    if not chain_dir.exists():
        return []
    return sorted(p.stem for p in chain_dir.glob("*.json"))
