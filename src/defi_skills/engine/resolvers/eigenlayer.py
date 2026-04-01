"""EigenLayer resolvers: strategy lookup (live), deposits, queued withdrawals."""

import logging
from typing import Any, Dict, List, Optional

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext

logger = logging.getLogger(__name__)

# Module-level cache: verified strategies, built once per process.
_eigenlayer_strategy_cache: Optional[Dict[str, str]] = None


def resolve_eigenlayer_strategy(value: Any, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Map LST symbol to EigenLayer strategy address.

    Uses the strategy_map from the playbook (which is overridden by
    data/registry/eigenlayer.json if it exists). Verifies the specific
    strategy is still whitelisted on-chain before returning.

    The registry is the source of truth for WHICH strategies exist.
    This resolver just does the lookup + on-chain verification.
    """
    if not value:
        return None

    sm_address = ctx.playbook_contracts.get("strategy_manager", {}).get("address")
    if not sm_address:
        raise ValueError("resolve_eigenlayer_strategy: strategy_manager_address not found in playbook contracts")

    # strategy_map comes from playbook, overridden by registry if present
    strategy_map = ctx.playbook_data.get("strategy_map", {})
    if not strategy_map:
        raise ValueError("resolve_eigenlayer_strategy: no strategy_map available (run: python scripts/refresh_registry.py)")

    sym = str(value).strip().upper()
    # Case-insensitive lookup
    strat_addr = strategy_map.get(sym)
    if not strat_addr:
        for key, addr in strategy_map.items():
            if key.upper() == sym:
                strat_addr = addr
                break
    if not strat_addr:
        raise ValueError(
            f"resolve_eigenlayer_strategy: no strategy found for '{sym}'. "
            f"Available: {list(strategy_map.keys())}"
        )

    # Verify on-chain that the strategy is still whitelisted
    global _eigenlayer_strategy_cache
    if _eigenlayer_strategy_cache is None:
        _eigenlayer_strategy_cache = {}

    cache_key = strat_addr.lower()
    if cache_key in _eigenlayer_strategy_cache:
        return _eigenlayer_strategy_cache[cache_key]

    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if w3:
        try:
            sm_addr = to_checksum_address(sm_address)
            strat_cs = to_checksum_address(strat_addr)
            (whitelisted,) = common.raw_eth_call(
                w3, sm_addr,
                "strategyIsWhitelistedForDeposit(address)", ["address"], [strat_cs], ["bool"]
            )
            if not whitelisted:
                raise ValueError(
                    f"resolve_eigenlayer_strategy: strategy for {sym} ({strat_addr}) "
                    f"is no longer whitelisted — run: python scripts/refresh_registry.py"
                )
        except ValueError:
            raise
        except Exception:
            pass  # RPC error on verification — proceed with registry value

    _eigenlayer_strategy_cache[cache_key] = strat_addr
    return strat_addr


def resolve_eigenlayer_deposits(value: Any, ctx: ResolveContext, **kwargs) -> List:
    """Query EigenLayer StrategyManager for user's deposits."""
    strategy_manager = ctx.playbook_contracts.get("strategy_manager", {}).get("address")
    if not strategy_manager:
        raise ValueError("resolve_eigenlayer_deposits: strategy_manager_address not found in playbook contracts")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3 or not ctx.from_address:
        raise ValueError("resolve_eigenlayer_deposits: no web3 or from_address")

    from_addr = to_checksum_address(ctx.from_address)
    sm_addr = to_checksum_address(strategy_manager)

    (strategies, shares) = common.raw_eth_call(
        w3, sm_addr,
        "getDeposits(address)", ["address"], [from_addr], ["address[]", "uint256[]"]
    )

    if not strategies:
        raise ValueError("resolve_eigenlayer_deposits: no deposits found")

    strategy_map = ctx.playbook_data.get("strategy_map", {})
    if value and str(value).strip():
        target = None
        for sym, addr in strategy_map.items():
            if sym.upper() == str(value).strip().upper():
                target = addr.lower()
                break
        if target:
            filtered = [(s, sh) for s, sh in zip(strategies, shares) if s.lower() == target]
            if filtered:
                strategies, shares = zip(*filtered)
                strategies, shares = list(strategies), list(shares)

    return [(list(strategies), list(shares), from_addr)]


def resolve_eigenlayer_queued_withdrawals(value: Any, ctx: ResolveContext, **kwargs) -> Dict:
    """Query EigenLayer for the first queued withdrawal."""
    dm_address = ctx.playbook_contracts.get("delegation_manager", {}).get("address")
    if not dm_address:
        raise ValueError("resolve_eigenlayer_queued_withdrawals: delegation_manager_address not found in playbook contracts")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3 or not ctx.from_address:
        raise ValueError("resolve_eigenlayer_queued_withdrawals: no web3 or from_address")

    from_addr = to_checksum_address(ctx.from_address)
    dm_addr = to_checksum_address(dm_address)

    (withdrawals, shares_arrays) = common.raw_eth_call(
        w3, dm_addr,
        "getQueuedWithdrawals(address)", ["address"], [from_addr],
        ["(address,address,address,uint256,uint32,address[],uint256[])[]", "uint256[][]"]
    )

    if not withdrawals:
        raise ValueError("resolve_eigenlayer_queued_withdrawals: no queued withdrawals")

    w = withdrawals[0]
    strategies = w[5] if len(w) > 5 else []

    strategy_map = ctx.playbook_data.get("strategy_map", {})
    strategy_to_token = {}
    for sym, strat_addr in strategy_map.items():
        token_info = ctx.token_resolver.resolve_erc20(sym) if ctx.token_resolver else None
        if token_info:
            strategy_to_token[strat_addr.lower()] = token_info["address"]

    tokens = []
    for strat in strategies:
        token = strategy_to_token.get(strat.lower() if isinstance(strat, str) else to_checksum_address(strat).lower())
        tokens.append(token or strat)

    return {
        "withdrawal": tuple(w),
        "tokens": tokens,
    }
