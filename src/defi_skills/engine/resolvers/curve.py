"""Curve resolvers: min LP mint amount, proportional min withdrawal amounts."""

from typing import Any

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps


def resolve_curve_min_mint(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Call Curve 3pool calc_token_amount on-chain, apply slippage."""
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    pool_address = kwargs.get("pool_address")
    if not pool_address:
        raise ValueError("resolve_curve_min_mint: pool_address not provided by playbook")

    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if w3 is None:
        raise ValueError("resolve_curve_min_mint: no web3 instance — cannot calculate min mint without RPC")

    amounts_raw = ctx.resolved.get("amounts")
    if not amounts_raw or not isinstance(amounts_raw, list) or len(amounts_raw) != 3:
        raise ValueError("resolve_curve_min_mint: missing or invalid amounts array (expected 3 elements)")

    amounts_int = [int(a) for a in amounts_raw]
    if all(a == 0 for a in amounts_int):
        raise ValueError("resolve_curve_min_mint: all amounts are zero")

    try:
        pool_cs = to_checksum_address(pool_address)
        sig = "calc_token_amount(uint256[3],bool)"
        (expected_lp,) = common.raw_eth_call(w3, pool_cs, sig, ["uint256[3]", "bool"], [amounts_int, True], ["uint256"])
        min_mint = expected_lp * (10000 - slippage_bps) // 10000
        return str(min_mint)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"resolve_curve_min_mint: on-chain call failed ({e})")


def resolve_curve_min_amounts(value: Any, ctx: ResolveContext, **kwargs) -> list:
    """Calculate proportional min_amounts for Curve 3pool remove_liquidity."""
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    pool_address = kwargs.get("pool_address")
    lp_token_address = kwargs.get("lp_token_address")
    if not pool_address:
        raise ValueError("resolve_curve_min_amounts: pool_address not provided by playbook")
    if not lp_token_address:
        raise ValueError("resolve_curve_min_amounts: lp_token_address not provided by playbook")

    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if w3 is None:
        raise ValueError("resolve_curve_min_amounts: no web3 instance available")

    lp_amount_raw = ctx.resolved.get("amount")
    if not lp_amount_raw:
        raise ValueError("resolve_curve_min_amounts: missing LP amount")

    lp_amount = int(lp_amount_raw)
    if lp_amount == 0:
        raise ValueError("resolve_curve_min_amounts: LP amount is zero")

    try:
        pool_cs = to_checksum_address(pool_address)
        lp_cs = to_checksum_address(lp_token_address)

        balances = []
        for i in range(3):
            (bal,) = common.raw_eth_call(w3, pool_cs, "balances(uint256)", ["uint256"], [i], ["uint256"])
            balances.append(bal)

        (total_supply,) = common.raw_eth_call(w3, lp_cs, "totalSupply()", [], [], ["uint256"])

        if total_supply == 0:
            raise ValueError("resolve_curve_min_amounts: pool totalSupply is 0")

        min_amounts = []
        for bal in balances:
            expected = lp_amount * bal // total_supply
            min_amt = expected * (10000 - slippage_bps) // 10000
            min_amounts.append(str(min_amt))

        return min_amounts
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"resolve_curve_min_amounts: on-chain call failed ({e})")
