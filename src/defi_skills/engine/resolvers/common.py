"""Shared helpers, types, and constants used by all resolver modules."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from eth_abi import encode as abi_encode, decode as abi_decode
from eth_utils import keccak, is_address

from defi_skills.engine.ens_resolver import ENSResolver

logger = logging.getLogger(__name__)

UINT256_MAX = str(2**256 - 1)
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%

# Pattern: /v2/KEY or /v3/KEY at end of URLs, or api_key=KEY params
_SECRET_URL_RE = re.compile(r'(/v[0-9]/)[A-Za-z0-9_-]{10,}')
_SECRET_PARAM_RE = re.compile(r'(api[_-]?key=)[A-Za-z0-9_-]+', re.IGNORECASE)


def sanitize_error(msg: str) -> str:
    """Strip API keys/secrets from error messages before logging."""
    msg = _SECRET_URL_RE.sub(r'\1***', msg)
    msg = _SECRET_PARAM_RE.sub(r'\1***', msg)
    return msg


# Address validation

def is_valid_eth_address(value: str) -> bool:
    """Check if a string is a valid Ethereum address (proper hex, checksum-safe)."""
    return isinstance(value, str) and is_address(value)


# Resolve context

@dataclass
class ResolveContext:
    """Immutable context passed to all resolvers."""
    token_resolver: Any  # TokenResolver instance (or None)
    ens_resolver: ENSResolver
    from_address: Optional[str]
    chain_id: int
    action: str
    raw_args: Dict[str, Any]
    resolved: Dict[str, Any] = field(default_factory=dict)
    decimals_cache: Dict[str, int] = field(default_factory=dict)

    def get_decimals_for(self, key: str) -> int:
        if key in self.decimals_cache:
            return self.decimals_cache[key]
        raise KeyError(f"Decimals not found for '{key}'. Token must be resolved before accessing decimals.")


# Errors

class DecimalsResolutionError(ValueError):
    """Raised when token decimals cannot be determined."""
    pass


# On-chain call helper

def raw_eth_call(w3, to: str, selector_sig: str, types: list, values: list, output_types: list) -> tuple:
    """Encode an eth_call from scratch and decode the result."""
    selector = keccak(selector_sig.encode())[:4]
    if types and values:
        calldata = selector + abi_encode(types, values)
    else:
        calldata = selector
    result = w3.eth.call({"to": to, "data": "0x" + calldata.hex()})
    return abi_decode(output_types, result)


# Decimals resolution

def resolve_decimals(spec: Any, ctx: ResolveContext, asset_sym: str = None) -> int:
    """Resolve a decimals_from spec to an integer.

    "$asset" -> look up from raw_args.asset symbol
    "$native" -> 18
    "$tokenIn" / "$tokenOut" -> look up from resolved token address
    int -> literal
    """
    if spec is None:
        raise DecimalsResolutionError("decimals_from not specified in playbook")
    if isinstance(spec, int):
        return spec
    s = str(spec)
    if s == "$native":
        return 18
    if s.startswith("$"):
        ref_key = s[1:]
        if ref_key in ctx.decimals_cache:
            return ctx.decimals_cache[ref_key]
        sym = asset_sym or ctx.raw_args.get(ref_key, "")
        if isinstance(sym, str) and not sym.startswith("0x"):
            if sym.upper() == "ETH":
                return 18
            if ctx.token_resolver:
                info = ctx.token_resolver.resolve_erc20(sym)
                if info:
                    ctx.decimals_cache[ref_key] = info["decimals"]
                    return info["decimals"]
        # Support nested keys: $__position.token0
        if "." in ref_key:
            parent_key, child_key = ref_key.split(".", 1)
            parent_val = ctx.resolved.get(parent_key)
            resolved_addr = parent_val.get(child_key) if isinstance(parent_val, dict) else None
        else:
            resolved_addr = ctx.resolved.get(ref_key)
        if resolved_addr and isinstance(resolved_addr, str):
            if resolved_addr in ctx.decimals_cache:
                return ctx.decimals_cache[resolved_addr]
            # Look up decimals from token resolver by address
            if ctx.token_resolver:
                info = ctx.token_resolver.get_by_address(resolved_addr)
                if info:
                    ctx.decimals_cache[resolved_addr] = info["decimals"]
                    return info["decimals"]
        raise DecimalsResolutionError(f"Cannot determine decimals for {spec} (symbol: {sym or 'unknown'})")
    try:
        return int(s)
    except ValueError:
        raise DecimalsResolutionError(f"Invalid decimals_from value: {s}")


# Slippage helper

def resolve_slippage_bps(ctx: ResolveContext, kwargs: dict) -> int:
    """Determine slippage in basis points.

    Priority: user override > playbook default > global default.
    """
    user_slippage = ctx.raw_args.get("slippage")
    if user_slippage is not None:
        try:
            pct = float(user_slippage)
            if 0 < pct <= 100:
                return int(pct * 100)
        except (ValueError, TypeError):
            pass
    return int(kwargs.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))
