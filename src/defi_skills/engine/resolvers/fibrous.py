import logging
from typing import Any
import requests
import eth_abi

from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps

logger = logging.getLogger(__name__)

# Fibrous represents Native ETH as the EEE... address
NATIVE_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def resolve_fibrous_swap_data(value: Any, ctx: ResolveContext, **kwargs) -> dict:
    """
    Resolve Fibrous V2 swap route and decode its calldata to perfectly match eth_abi params.
    Uses the /v2/routeAndCallData endpoint.
    """
    amount = ctx.resolved.get("amount")
    asset_in = ctx.resolved.get("asset_in")
    asset_out = ctx.resolved.get("asset_out")

    if not amount or not asset_in or not asset_out:
        raise ValueError("fibrous_swap: amount, asset_in, and asset_out must be resolved first.")

    # Format Native token addresses for Fibrous (it uses 0xeee... for native currency)
    token_in = NATIVE_ADDRESS if asset_in.lower() == NATIVE_ADDRESS.lower() else asset_in
    token_out = NATIVE_ADDRESS if asset_out.lower() == NATIVE_ADDRESS.lower() else asset_out

    # Get slippage
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    slippage_percent = slippage_bps / 10000.0  # Fibrous API takes e.g. 0.01 for 1%

    # Destination address defaults to sender
    destination = ctx.from_address

    # Chain mapping logic: strictly allow Base, HyperEVM, Citrea, and Monad.
    chain_map = {
        8453: "base",
        999: "hyperevm",
        4114: "citrea",
        143: "monad"
    }

    chain_id = ctx.chain_id
    if chain_id not in chain_map:
        supported = ", ".join(f"{k} ({v})" for k, v in chain_map.items())
        raise ValueError(
            f"Fibrous V2 does not support chain_id {chain_id} in this configuration. "
            f"Supported: {supported}"
        )

    chain_path = chain_map[chain_id]

    url = f"https://api.fibrous.finance/{chain_path}/v2/routeAndCallData"
    params = {
        "amount": str(amount),
        "tokenInAddress": token_in,
        "tokenOutAddress": token_out,
        "slippage": str(slippage_percent),
        "destination": destination,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Fibrous API error: {e}. URL: {url} Params: {params}")
        raise ValueError(f"Fibrous API quote failed: {e}")

    if not data.get("success"):
        raise ValueError(f"Fibrous route failed: {data}")

    calldata = data.get("calldata", "")
    router_address = data.get("router_address", "")

    if not calldata or not router_address:
        raise ValueError("Fibrous response missing calldata or router_address.")

    # The Fibrous swap() signature is:
    # swap((address,address,uint256,uint256,uint256,address,uint8), (address,address,uint32,int24,address,uint8,bytes)[])
    # The calldata consists of the 4 byte selector + ABI encoded parameters.
    try:
        encoded_params = bytes.fromhex(calldata[10:] if calldata.startswith("0x") else calldata[8:])
        
        route_tuple, swap_params_array = eth_abi.decode(
            ['(address,address,uint256,uint256,uint256,address,uint8)', '(address,address,uint32,int24,address,uint8,bytes)[]'],
            encoded_params
        )
    except Exception as e:
        logger.error(f"Failed to decode Fibrous calldata: {e}")
        raise ValueError(f"Failed to decode Fibrous calldata: {e}")

    return {
        "route_param": route_tuple,
        "swap_params": list(swap_params_array),
        "router_address": router_address
    }

def resolve_fibrous_router(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Helper to extract router address after fibrous_data is resolved."""
    data = ctx.resolved.get("fibrous_data")
    if not data:
        raise ValueError("fibrous_data must be resolved before router address.")
    return data["router_address"]

def resolve_fibrous_msg_value(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Dynamically set msg.value if asset_in is native ETH."""
    asset_in = str(ctx.resolved.get("asset_in", "")).lower()
    native_identifiers = {
        "eth",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        NATIVE_ADDRESS.lower(),
        "0x0000000000000000000000000000000000000000"
    }
    if asset_in in native_identifiers:
        return str(ctx.resolved.get("amount", "0"))
    return "0"

