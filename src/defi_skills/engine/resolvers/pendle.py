"""Pendle V2 resolvers: market lookup and swap quote via Pendle public API."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from eth_utils import to_checksum_address

from defi_skills.engine.resolvers.common import (
    ResolveContext,
    resolve_slippage_bps,
)

PENDLE_API = "https://api-v2.pendle.finance/core/v1"
PENDLE_SDK_API = "https://api-v2.pendle.finance/core/v3/sdk"


# --- Market resolver (API) ---

def resolve_pendle_market(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Resolve a Pendle market address from a token name/symbol via Pendle API.

    Searches active markets on the chain, matches by underlying asset name.
    Stores PT, YT, SY addresses in ctx.resolved for downstream use.
    """
    chain_id = ctx.chain_id
    query = str(value).strip().lower() if value else ""
    if not query:
        raise ValueError("resolve_pendle_market: no token/market name provided")

    # If it already looks like an address, pass through
    if query.startswith("0x") and len(query) == 42:
        return to_checksum_address(query)

    try:
        resp = requests.get(
            f"{PENDLE_API}/{chain_id}/markets/active",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            markets = data.get("markets") or data.get("results") or []
        else:
            markets = data
    except Exception as e:
        raise ValueError(f"resolve_pendle_market: API call failed ({e})")

    if not isinstance(markets, list):
        raise ValueError("resolve_pendle_market: unexpected API response format")

    # Search by market name or underlying token symbol
    best_match = None
    for m in markets:
        name = (m.get("name") or m.get("proName") or "").lower()
        # Strip chain prefix from addresses (e.g., "1-0x...")
        underlying = (m.get("underlyingAsset") or "").lower()
        if underlying.startswith(f"{chain_id}-"):
            underlying = underlying[len(f"{chain_id}-"):]

        if query == name or query in name:
            # Prefer exact match, accept substring
            if best_match is None or query == name:
                best_match = m
                if query == name:
                    break

    if not best_match:
        available = [m.get("name", "?") for m in markets[:20]]
        raise ValueError(
            f"resolve_pendle_market: no market found for '{value}'. "
            f"Available: {', '.join(available)}..."
        )

    # Reject expired markets
    expiry = best_match.get("expiry")
    if expiry:
        try:
            expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiry_dt < datetime.now(timezone.utc):
                raise ValueError(
                    f"resolve_pendle_market: market '{best_match.get('name')}' expired on "
                    f"{expiry_dt.strftime('%Y-%m-%d')}. No active market found for '{value}'."
                )
        except ValueError:
            raise
        except Exception:
            pass  # If expiry parsing fails, proceed — better than blocking

    market_addr = best_match.get("address") or best_match.get("market")
    if not market_addr:
        raise ValueError(f"resolve_pendle_market: no address in market data for '{value}'")

    # Strip chain prefix if present
    if market_addr.startswith(f"{chain_id}-"):
        market_addr = market_addr[len(f"{chain_id}-"):]

    # Store related addresses for downstream resolvers
    for field in ("pt", "yt", "sy"):
        addr = best_match.get(field, "")
        if isinstance(addr, str) and addr.startswith(f"{chain_id}-"):
            addr = addr[len(f"{chain_id}-"):]
        if addr:
            ctx.resolved[f"_pendle_{field}"] = addr

    return to_checksum_address(market_addr)


# --- YT address resolver (from market context) ---

def resolve_pendle_yt(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Resolve Pendle YT address from the market resolver's stored context.

    If the user provides a raw address, pass it through.
    Otherwise, use _pendle_yt stored by resolve_pendle_market.
    """
    # User provided a raw address
    if value and str(value).startswith("0x") and len(str(value)) == 42:
        return to_checksum_address(str(value))

    # Pull from market resolver context
    yt = ctx.resolved.get("_pendle_yt")
    if yt:
        return to_checksum_address(yt)

    raise ValueError(
        "resolve_pendle_yt: no YT address available. "
        "Provide a 'market' argument so the market resolver can look it up."
    )


# --- Quote resolver (Pendle API) ---

# Map playbook function_name to the rate field from swapping-prices API
_RATE_KEYS = {
    "swapExactTokenForPt": "underlyingTokenToPtRate",
    "swapExactPtForToken": "ptToUnderlyingTokenRate",
    "swapExactTokenForYt": "underlyingTokenToYtRate",
    "swapExactYtForToken": "ytToUnderlyingTokenRate",
}

# Actions that need the convert API for accurate quotes
_CONVERT_ACTIONS = {
    "addLiquiditySingleToken",
    "removeLiquiditySingleToken",
    "mintPyFromToken",
    "redeemPyToToken",
}


def _quote_via_convert(
    chain_id: int, receiver: str, token_in: str, amount_in: int,
    output_tokens: list,
) -> int:
    """Call Pendle's convert API to get exact expected output.

    Returns the amount of the first output token.
    For mint-py, pass both [PT, YT] as output_tokens.
    """
    resp = requests.post(
        f"{PENDLE_SDK_API}/{chain_id}/convert",
        json={
            "receiver": receiver,
            "slippage": 0.5,  # high slippage for quote — we apply our own
            "inputs": [{"token": token_in, "amount": str(amount_in)}],
            "outputs": output_tokens,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract output amount from first route
    routes = data.get("routes", [])
    if routes:
        outputs = routes[0].get("outputs", [])
        if outputs:
            return int(outputs[0]["amount"])

    raise ValueError("resolve_pendle_min_out: no output in convert API response")


def resolve_pendle_min_out(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Quote a Pendle action and apply slippage protection.

    For swaps: uses market spot rates from swapping-prices API (fast, no auth).
    For mint/redeem/liquidity: uses convert API for accurate quotes.
    Slippage is applied on top of the quoted amount.
    """
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    function_name = kwargs.get("function_name")
    if not function_name:
        raise ValueError("resolve_pendle_min_out: function_name not specified in playbook")

    market = ctx.resolved.get("market")
    amount_in = int(ctx.resolved.get("amount", 0))
    chain_id = ctx.chain_id

    if not market:
        raise ValueError("resolve_pendle_min_out: market not yet resolved")
    if not amount_in:
        raise ValueError("resolve_pendle_min_out: amount is 0 or not resolved")

    if function_name in _CONVERT_ACTIONS:
        # Use convert API for accurate quotes on complex operations
        if not ctx.from_address:
            raise ValueError("resolve_pendle_min_out: from_address required for convert API quote")
        receiver = ctx.from_address

        # Determine input/output tokens for the convert API
        pt = ctx.resolved.get("_pendle_pt", "")
        yt = ctx.resolved.get("_pendle_yt") or ctx.resolved.get("yt_address", "")

        if function_name == "addLiquiditySingleToken":
            token_in = ctx.resolved.get("asset", "")
            output_tokens = [market]  # LP token = market address
        elif function_name == "removeLiquiditySingleToken":
            token_in = market  # LP token
            output_tokens = [ctx.resolved.get("asset_out", "")]
        elif function_name == "mintPyFromToken":
            token_in = ctx.resolved.get("asset", "")
            output_tokens = [pt, yt]  # both PT + YT for mint-py detection
        elif function_name == "redeemPyToToken":
            token_in = pt or yt
            output_tokens = [ctx.resolved.get("asset_out", "")]
        else:
            raise ValueError(f"resolve_pendle_min_out: unsupported convert action '{function_name}'")

        if not token_in or not all(output_tokens):
            raise ValueError(f"resolve_pendle_min_out: missing token addresses for {function_name}")

        try:
            quoted = _quote_via_convert(chain_id, receiver, token_in, amount_in, output_tokens)
        except Exception as e:
            raise ValueError(f"resolve_pendle_min_out: convert API failed ({e})")
    else:
        # Swap actions: use spot rates (faster, simpler)
        rate_key = _RATE_KEYS.get(function_name)
        if not rate_key:
            raise ValueError(f"resolve_pendle_min_out: unsupported function '{function_name}'")

        try:
            resp = requests.get(
                f"{PENDLE_API}/sdk/{chain_id}/markets/{market}/swapping-prices",
                timeout=10,
            )
            resp.raise_for_status()
            prices = resp.json()
        except Exception as e:
            raise ValueError(f"resolve_pendle_min_out: API call failed ({e})")

        rate = prices.get(rate_key)
        if rate is None:
            raise ValueError(f"resolve_pendle_min_out: rate '{rate_key}' not in API response")

        quoted = int(Decimal(amount_in) * Decimal(str(rate)))

    min_out = quoted * (10000 - slippage_bps) // 10000
    return str(min_out)
