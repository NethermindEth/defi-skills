"""Balancer V2 resolvers: pool ID lookup, swap limit, pool tokens, userData encoding."""

import json
import os
import urllib.request
import urllib.error
from typing import Any, List

from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps


def resolve_balancer_pool_id(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Query The Graph for a Balancer V2 pool containing both tokens."""
    token_in = ctx.resolved.get("__token_in_address")
    token_out = ctx.resolved.get("__token_out_address")
    if not token_in or not token_out:
        raise ValueError("resolve_balancer_pool_id: missing token addresses")

    api_key = os.getenv("THEGRAPH_API_KEY")
    if not api_key:
        raise ValueError("resolve_balancer_pool_id: THEGRAPH_API_KEY not set")

    subgraph_id = kwargs.get("subgraph_id", "C4ayEZP2yTXRAB8vSaTrgN4m9anTe9Mdm2ViyiAuV9TV")
    endpoint = f"https://gateway.thegraph.com/api/subgraphs/id/{subgraph_id}"

    addr_a = token_in.lower()
    addr_b = token_out.lower()

    query = {
        "query": '{ pools(where: {tokensList_contains: ["%s", "%s"], totalLiquidity_gt: "0"}, orderBy: totalLiquidity, orderDirection: desc, first: 1) { id } }' % (addr_a, addr_b)
    }

    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(query).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "defi-skills/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        pools = data.get("data", {}).get("pools", [])
        if pools:
            pool_id = pools[0]["id"]
            if pool_id.startswith("0x") and len(pool_id) == 66:
                return pool_id
            raise ValueError(f"resolve_balancer_pool_id: unexpected pool ID format: {pool_id}")

        raise ValueError("resolve_balancer_pool_id: no Balancer V2 pool found for this token pair")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"resolve_balancer_pool_id: subgraph query failed ({e})")


def resolve_balancer_limit(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Simulate Balancer V2 swap via queryBatchSwap, apply slippage."""
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    vault_address = kwargs.get("vault_address")
    if not vault_address:
        raise ValueError("resolve_balancer_limit: vault_address not provided by playbook")

    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if w3 is None:
        raise ValueError("resolve_balancer_limit: no web3 instance — cannot quote without RPC")

    pool_id = ctx.resolved.get("poolId")
    amount = ctx.resolved.get("amount")
    token_in = ctx.resolved.get("__token_in_address")
    token_out = ctx.resolved.get("__token_out_address")
    sender = ctx.resolved.get("sender") or ctx.from_address

    if not pool_id or pool_id == "0x" + "00" * 32:
        raise ValueError("resolve_balancer_limit: poolId is zero — pool lookup failed")
    if not all([amount, token_in, token_out]):
        raise ValueError("resolve_balancer_limit: missing dependencies (amount, token_in, or token_out)")

    try:
        vault_cs = to_checksum_address(vault_address)
        token_in_cs = to_checksum_address(token_in)
        token_out_cs = to_checksum_address(token_out)
        sender_cs = to_checksum_address(sender) if sender else to_checksum_address("0x" + "00" * 20)

        pool_id_bytes = bytes.fromhex(pool_id[2:]) if pool_id.startswith("0x") else bytes.fromhex(pool_id)
        swap_step = (pool_id_bytes, 0, 1, int(amount), b"")
        funds = (sender_cs, False, sender_cs, False)

        sig = "queryBatchSwap(uint8,(bytes32,uint256,uint256,uint256,bytes)[],address[],(address,bool,address,bool))"
        types = [
            "uint8",
            "(bytes32,uint256,uint256,uint256,bytes)[]",
            "address[]",
            "(address,bool,address,bool)",
        ]
        values = [0, [swap_step], [token_in_cs, token_out_cs], funds]

        (deltas,) = common.raw_eth_call(w3, vault_cs, sig, types, values, ["int256[]"])

        if len(deltas) >= 2:
            expected_out = abs(deltas[1])
            min_out = expected_out * (10000 - slippage_bps) // 10000
            return str(min_out)

        raise ValueError("resolve_balancer_limit: unexpected deltas format from queryBatchSwap")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"resolve_balancer_limit: on-chain call failed ({e})")


def resolve_balancer_pool_tokens(value: Any, ctx: ResolveContext, **kwargs) -> List:
    """Query Balancer Vault.getPoolTokens(poolId) for the pool's asset list."""
    vault_address = kwargs.get("vault_address")
    if not vault_address:
        raise ValueError("resolve_balancer_pool_tokens: vault_address not provided by playbook")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3:
        raise ValueError("resolve_balancer_pool_tokens: no web3 instance")

    pool_id = ctx.resolved.get("poolId")
    if not pool_id:
        raise ValueError("resolve_balancer_pool_tokens: poolId not resolved")

    vault_cs = to_checksum_address(vault_address)
    pool_id_bytes = bytes.fromhex(pool_id[2:]) if isinstance(pool_id, str) and pool_id.startswith("0x") else pool_id

    (tokens, _balances, _last_block) = common.raw_eth_call(
        w3, vault_cs,
        "getPoolTokens(bytes32)", ["bytes32"], [pool_id_bytes],
        ["address[]", "uint256[]", "uint256"]
    )

    return [to_checksum_address(t) for t in tokens]


def resolve_balancer_userdata(value: Any, ctx: ResolveContext, **kwargs) -> bytes:
    """ABI-encode userData for Balancer joinPool or exitPool."""
    mode = kwargs.get("mode", "join")

    if mode == "join":
        pool_tokens = ctx.resolved.get("__pool_tokens", [])
        amount = ctx.resolved.get("amount", "0")
        token_addr = ctx.resolved.get("__token_in_address", "")

        amounts = []
        for t in pool_tokens:
            if t.lower() == token_addr.lower():
                amounts.append(int(amount))
            else:
                amounts.append(0)

        ctx.resolved["__maxAmountsIn"] = amounts
        return abi_encode(["uint256", "uint256[]", "uint256"], [1, amounts, 0])

    elif mode == "exit":
        bpt_amount = int(ctx.resolved.get("amount", "0"))
        pool_tokens = ctx.resolved.get("__pool_tokens", [])
        exit_token = ctx.resolved.get("__token_out_address", "")

        token_index = 0
        for i, t in enumerate(pool_tokens):
            if t.lower() == exit_token.lower():
                token_index = i
                break

        return abi_encode(["uint256", "uint256", "uint256"], [0, bpt_amount, token_index])

    return b""
