"""Uniswap V3 resolvers: swap quote, LP position, token ordering, tick range."""

from decimal import Decimal
from typing import Any, Dict

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps

TICK_SPACING = {100: 1, 500: 10, 3000: 60, 10000: 200}
MAX_TICK = 887272


def resolve_uniswap_quote(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Quote amountOutMinimum via Uniswap V3 Quoter on-chain, apply slippage."""
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    quoter_address = kwargs.get("quoter_address")
    if not quoter_address:
        raise ValueError("resolve_uniswap_quote: quoter_address not provided by playbook")

    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if w3 is None:
        raise ValueError("resolve_uniswap_quote: no web3 instance — cannot quote without RPC")

    token_in = ctx.resolved.get("tokenIn")
    token_out = ctx.resolved.get("tokenOut")
    fee = ctx.resolved.get("fee")
    amount_in = ctx.resolved.get("amountIn")

    if not all([token_in, token_out, fee, amount_in]):
        raise ValueError("resolve_uniswap_quote: missing dependencies (tokenIn, tokenOut, fee, or amountIn)")

    try:
        quoter_cs = to_checksum_address(quoter_address)
        sig = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
        types = ["(address,address,uint256,uint24,uint160)"]
        values = [(
            to_checksum_address(token_in),
            to_checksum_address(token_out),
            int(amount_in),
            int(fee),
            0,
        )]
        result = common.raw_eth_call(w3, quoter_cs, sig, types, values,
                                     ["uint256", "uint160", "uint32", "uint256"])
        quoted_amount = result[0]
        min_out = quoted_amount * (10000 - slippage_bps) // 10000
        return str(min_out)
    except Exception as e:
        raise ValueError(f"resolve_uniswap_quote: on-chain quote failed ({e})")


def resolve_token_ordering(value: Any, ctx: ResolveContext, **kwargs) -> Dict:
    """Order two tokens canonically (token0 < token1) for Uniswap V3."""
    tokenA_key = kwargs.get("tokenA_key", "__tokenA_address")
    tokenB_key = kwargs.get("tokenB_key", "__tokenB_address")
    amountA_key = kwargs.get("amountA_key", "__amountA")
    amountB_key = kwargs.get("amountB_key", "__amountB")

    tokenA = ctx.resolved.get(tokenA_key, "")
    tokenB = ctx.resolved.get(tokenB_key, "")
    amountA = ctx.resolved.get(amountA_key, "0")
    amountB = ctx.resolved.get(amountB_key, "0")

    if not tokenA or not tokenB:
        raise ValueError("resolve_token_ordering: missing token addresses")

    if int(tokenA, 16) < int(tokenB, 16):
        return {"token0": tokenA, "token1": tokenB, "amount0": str(amountA), "amount1": str(amountB)}
    else:
        return {"token0": tokenB, "token1": tokenA, "amount0": str(amountB), "amount1": str(amountA)}


def resolve_tick_range(value: Any, ctx: ResolveContext, **kwargs) -> Dict:
    """Compute tickLower/tickUpper for Uniswap V3 LP. Defaults to full range."""
    fee = ctx.resolved.get("fee")
    if fee is None:
        fee = 3000
    fee = int(fee)
    spacing = TICK_SPACING.get(fee, 60)
    tick_lower = -(MAX_TICK // spacing) * spacing
    tick_upper = (MAX_TICK // spacing) * spacing
    return {"tickLower": tick_lower, "tickUpper": tick_upper}


def resolve_uniswap_position(value: Any, ctx: ResolveContext, **kwargs) -> Dict:
    """Query NonfungiblePositionManager.positions(tokenId)."""
    nfpm = kwargs.get("nfpm_address")
    if not nfpm:
        raise ValueError("resolve_uniswap_position: nfpm_address not provided by playbook")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3:
        raise ValueError("resolve_uniswap_position: no web3 instance")

    token_id = int(value) if value is not None else 0
    nfpm_addr = to_checksum_address(nfpm)

    result = common.raw_eth_call(
        w3, nfpm_addr,
        "positions(uint256)", ["uint256"], [token_id],
        ["uint96", "address", "address", "address", "uint24", "int24", "int24",
         "uint128", "uint256", "uint256", "uint128", "uint128"]
    )

    return {
        "token0": result[2], "token1": result[3], "fee": result[4],
        "tickLower": result[5], "tickUpper": result[6], "liquidity": result[7],
        "tokensOwed0": result[10], "tokensOwed1": result[11],
    }


def resolve_partial_liquidity(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Compute liquidity amount from percentage or 'max'."""
    position = ctx.resolved.get("__position")
    if not position or not isinstance(position, dict):
        raise ValueError("resolve_partial_liquidity: __position not resolved")

    total = int(position.get("liquidity", 0))
    if not total:
        raise ValueError("resolve_partial_liquidity: position has 0 liquidity")

    if value is None or str(value).strip().lower() == "max":
        return str(total)

    pct = Decimal(str(value).strip())
    return str(int(Decimal(total) * pct / 100))
