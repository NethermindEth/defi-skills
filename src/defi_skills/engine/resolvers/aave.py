"""Aave V3 resolvers: reward asset discovery."""

from typing import Any, List

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext


def resolve_aave_reward_assets(value: Any, ctx: ResolveContext, **kwargs) -> List:
    """Discover Aave aTokens held by user for reward claiming."""
    pool_address = kwargs.get("pool_address")
    if not pool_address:
        raise ValueError("resolve_aave_reward_assets: pool_address not provided by playbook")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3 or not ctx.from_address:
        raise ValueError("resolve_aave_reward_assets: no web3 or from_address")

    from_addr = to_checksum_address(ctx.from_address)
    pool_addr = to_checksum_address(pool_address)

    (reserves,) = common.raw_eth_call(w3, pool_addr, "getReservesList()", [], [], ["address[]"])

    reserve_data_types = [
        "uint256", "uint128", "uint128", "uint128", "uint128", "uint128",
        "uint40", "uint16",
        "address", "address", "address", "address",
        "uint128", "uint128", "uint128",
    ]

    held_atokens = []
    for reserve in reserves:
        reserve_cs = to_checksum_address(reserve)
        try:
            reserve_data = common.raw_eth_call(
                w3, pool_addr,
                "getReserveData(address)", ["address"], [reserve_cs], reserve_data_types
            )
            atoken_cs = to_checksum_address(reserve_data[8])
            (balance,) = common.raw_eth_call(
                w3, atoken_cs,
                "balanceOf(address)", ["address"], [from_addr], ["uint256"]
            )
            if balance > 0:
                held_atokens.append(atoken_cs)
        except Exception:
            continue

    if not held_atokens:
        raise ValueError("resolve_aave_reward_assets: no aTokens held")

    return held_atokens
