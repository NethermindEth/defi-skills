"""Fibrous DEX aggregator resolvers — route fetching, calldata decoding, native ETH handling."""

import logging
from typing import Any, Optional

import requests
import eth_abi

from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps, sanitize_error

logger = logging.getLogger(__name__)

NATIVE_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Fibrous API chain path mapping — scoped to Base for now.
_CHAIN_PATH = {
    8453: "base",
}


def resolve_fibrous_token(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Resolve token for Fibrous swaps, mapping native ETH to the 0xeee... sentinel."""
    if not value:
        return None
    s = value.strip()

    # Native ETH → Fibrous sentinel address (not a real ERC-20)
    if s.upper() == "ETH":
        ctx.decimals_cache[NATIVE_ADDRESS] = 18
        return NATIVE_ADDRESS

    # Everything else goes through the standard token resolver
    from defi_skills.engine.resolvers.core import resolve_token_address
    return resolve_token_address(s, ctx, **kwargs)


def resolve_fibrous_swap_data(value: Any, ctx: ResolveContext, **kwargs) -> dict:
    """Fetch route from Fibrous API and ABI-decode the calldata into swap parameters."""
    amount = ctx.resolved.get("amount")
    asset_in = ctx.resolved.get("asset_in")
    asset_out = ctx.resolved.get("asset_out")

    if not amount or not asset_in or not asset_out:
        raise ValueError("resolve_fibrous_swap_data: amount, asset_in, and asset_out must be resolved first.")

    chain_path = _CHAIN_PATH.get(ctx.chain_id)
    if not chain_path:
        supported = ", ".join(f"{cid} ({name})" for cid, name in _CHAIN_PATH.items())
        raise ValueError(
            f"resolve_fibrous_swap_data: chain_id {ctx.chain_id} is not supported. "
            f"Supported: {supported}"
        )

    # Fibrous uses 0xeee... for native currency
    token_in = NATIVE_ADDRESS if asset_in.lower() == NATIVE_ADDRESS.lower() else asset_in
    token_out = NATIVE_ADDRESS if asset_out.lower() == NATIVE_ADDRESS.lower() else asset_out

    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    slippage_percent = slippage_bps / 10000.0

    url = f"https://api.fibrous.finance/{chain_path}/v2/routeAndCallData"
    params = {
        "amount": str(amount),
        "tokenInAddress": token_in,
        "tokenOutAddress": token_out,
        "slippage": str(slippage_percent),
        "destination": ctx.from_address,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("Fibrous API error: %s", sanitize_error(str(e)))
        raise ValueError(f"Fibrous API quote failed: {sanitize_error(str(e))}")

    if not data.get("success"):
        raise ValueError(f"Fibrous route failed: {sanitize_error(str(data))}")

    calldata = data.get("calldata", "")
    if not calldata:
        raise ValueError("Fibrous response missing calldata.")

    # swap() selector (4 bytes) + ABI-encoded params
    try:
        raw_hex = calldata[10:] if calldata.startswith("0x") else calldata[8:]
        encoded_params = bytes.fromhex(raw_hex)

        route_tuple, swap_params_array = eth_abi.decode(
            [
                '(address,address,uint256,uint256,uint256,address,uint8)',
                '(address,address,uint32,int24,address,uint8,bytes)[]',
            ],
            encoded_params,
        )
    except Exception as e:
        logger.error("Failed to decode Fibrous calldata: %s", sanitize_error(str(e)))
        raise ValueError(f"Failed to decode Fibrous calldata: {sanitize_error(str(e))}")

    return {
        "route_param": route_tuple,
        "swap_params": list(swap_params_array),
    }


def resolve_fibrous_msg_value(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Set msg.value when selling native ETH, zero otherwise."""
    asset_in = str(ctx.resolved.get("asset_in", "")).lower()
    native_sentinels = {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0000000000000000000000000000000000000000",
    }
    if asset_in in native_sentinels:
        return str(ctx.resolved.get("amount", "0"))
    return "0"
