"""Core resolvers: token, collection, amount, ENS, fee, deadline, and simple passthrough."""

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, List, Optional

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext, UINT256_MAX, resolve_decimals, is_valid_eth_address


def resolve_token_address(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Symbol -> ERC-20 contract address. Rejects ETH unless eth_alias is set."""
    if not value:
        return None
    s = value.strip()
    if s.startswith("0x"):
        if not is_valid_eth_address(s):
            raise ValueError(f"resolve_token_address: invalid address '{s}'")
        if ctx.token_resolver:
            info = ctx.token_resolver.resolve_by_address(s)
            if info:
                ctx.decimals_cache[s] = info["decimals"]
                return info["address"]
        return s
    if s.upper() == "ETH":
        eth_alias = kwargs.get("eth_alias")
        if eth_alias is None:
            raise ValueError(
                "ETH (native) is not supported for this action. "
                "Use WETH, or wrap first with weth_wrap."
            )
        s = eth_alias
        # If the alias is a literal hex address (like Fibonacci's 0xeee... sentinel),
        # return it immediately and cache 18 decimals if it's a native sentinel.
        if s.startswith("0x") and is_valid_eth_address(s):
            if s.lower() in (
                "0x0000000000000000000000000000000000000000",
                "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            ):
                ctx.decimals_cache[s] = 18
            return s
    if ctx.token_resolver:
        info = ctx.token_resolver.resolve_erc20(s)
        if info:
            addr = info["address"]
            ctx.decimals_cache[addr] = info["decimals"]
            ctx.decimals_cache[s.upper()] = info["decimals"]
            return addr
    raise ValueError(f"resolve_token_address: unknown token '{value}'")


def resolve_collection_address(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Collection name -> ERC-721 contract address."""
    if not value:
        return None
    s = value.strip()
    if s.startswith("0x"):
        if not is_valid_eth_address(s):
            raise ValueError(f"resolve_collection_address: invalid address '{s}'")
        return s
    if ctx.token_resolver:
        info = ctx.token_resolver.resolve_collection(s)
        if info:
            return info.get("address")
    raise ValueError(f"resolve_collection_address: unknown collection '{value}'")


def resolve_amount(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Human-readable amount -> base units string."""
    if value is None:
        return None
    if str(value).strip().lower() == "max":
        raise ValueError(
            f"resolve_amount received 'max' for action '{ctx.action}'. "
            f"Use resolve_amount_or_max or resolve_amount_or_balance instead."
        )
    try:
        d = Decimal(str(value))
    except Exception:
        raise ValueError(f"resolve_amount: invalid amount '{value}' - expected a number")
    if d < 0:
        raise ValueError(f"resolve_amount: negative amount '{value}' is not allowed")
    decimals = resolve_decimals(kwargs.get("decimals_from"), ctx)
    scale = Decimal(10) ** decimals
    base = (d * scale).to_integral_value(rounding=ROUND_DOWN)
    return str(int(base))


def resolve_amount_or_max(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Like resolve_amount but 'max' -> uint256.max (Aave, Compound sentinel)."""
    if value is None:
        return None
    if str(value).strip().lower() == "max":
        return UINT256_MAX
    return resolve_amount(value, ctx, **kwargs)


def resolve_amount_or_balance(value: str, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Like resolve_amount but 'max' -> actual on-chain balance."""
    if value is None:
        return None
    if str(value).strip().lower() != "max":
        return resolve_amount(value, ctx, **kwargs)

    balance_of = kwargs.get("balance_of")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not balance_of or not w3 or not ctx.from_address:
        raise ValueError("resolve_amount_or_balance: 'max' requested but cannot query balance")

    from_addr = to_checksum_address(ctx.from_address)

    if balance_of == "$native":
        return str(w3.eth.get_balance(from_addr))

    token_addr = balance_of
    if token_addr.startswith("$"):
        key = token_addr[1:]
        if "." in key:
            parent, child = key.split(".", 1)
            parent_val = ctx.resolved.get(parent)
            token_addr = parent_val.get(child) if isinstance(parent_val, dict) else None
        else:
            token_addr = ctx.resolved.get(key)

    if not token_addr:
        raise ValueError(f"resolve_amount_or_balance: could not resolve token address from '{balance_of}'")

    try:
        token_cs = to_checksum_address(token_addr)
        (balance,) = common.raw_eth_call(
            w3, token_cs,
            "balanceOf(address)", ["address"], [from_addr], ["uint256"]
        )
        return str(balance)
    except Exception as e:
        raise ValueError(f"resolve_amount_or_balance: balanceOf query failed ({e})")


def resolve_ens_or_hex(value: Optional[str], ctx: ResolveContext, **kwargs) -> Optional[str]:
    """ENS name -> address, or pass through hex address."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.startswith("0x"):
        if not is_valid_eth_address(s):
            raise ValueError(f"resolve_ens_or_hex: invalid address '{s}'")
        return s
    ens_key = s.lower()
    if not ens_key.endswith(".eth"):
        ens_key = ens_key + ".eth"
    # resolve() is live-only and raises ValueError on failure
    return ctx.ens_resolver.resolve(ens_key)


def resolve_fee_tier(value: Any, ctx: ResolveContext, **kwargs) -> int:
    """Resolve Uniswap V3 fee tier. Uses LLM value if provided, else heuristic."""
    if value is not None and str(value) != "":
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
    heuristic = kwargs.get("fee_heuristic", {})
    stable_tokens = set(heuristic.get("stable_tokens", []))
    major_tokens = set(heuristic.get("major_tokens", ["WETH", "ETH"]))
    sym_in = (ctx.raw_args.get(kwargs.get("input_symbol_field", "asset_in")) or "").upper()
    sym_out = (ctx.raw_args.get(kwargs.get("output_symbol_field", "asset_out")) or "").upper()
    if sym_in in stable_tokens and sym_out in stable_tokens:
        return heuristic.get("stable_stable_fee", 100)
    if (sym_in in major_tokens and sym_out in stable_tokens) or \
       (sym_out in major_tokens and sym_in in stable_tokens):
        return heuristic.get("major_stable_fee", 500)
    return heuristic.get("default_fee", 3000)


def resolve_deadline(value: Any, ctx: ResolveContext, **kwargs) -> int:
    """Current timestamp + buffer seconds."""
    buffer = kwargs.get("buffer_seconds", 1200)
    return int(time.time()) + buffer


def resolve_interest_rate_mode(value: Any, ctx: ResolveContext, **kwargs) -> int:
    """Map LLM text to Aave V3 interest rate mode enum."""
    default = kwargs.get("default", 2)
    if value is None or str(value).strip() == "":
        return default
    s = str(value).strip().lower()
    if s in ("1", "stable", "stable rate", "fixed"):
        raise ValueError("Stable rate borrowing is deprecated on Aave V3 mainnet (disabled by governance). Use variable rate instead.")
    if s in ("2", "variable", "variable rate", "floating"):
        return 2
    try:
        v = int(s)
        if v == 1:
            raise ValueError("Stable rate borrowing is deprecated on Aave V3 mainnet (disabled by governance). Use variable rate instead.")
        if v == 2:
            return 2
    except (ValueError, TypeError):
        pass
    return default


def resolve_smart_amount(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Try to parse as base-unit integer first; fall back to token_to_base."""
    if value is None or str(value).strip() == "":
        return kwargs.get("fallback", "0")
    s = str(value).strip()
    if s == "0":
        return "0"
    try:
        int(s)
        return s
    except (ValueError, TypeError):
        return resolve_amount(s, ctx, **kwargs)


def wrap_in_array(value: str, ctx: ResolveContext, **kwargs) -> List[str]:
    """Resolve a single amount and wrap in a list."""
    if value is None:
        raise ValueError("wrap_in_array: amount is required but not provided")
    if str(value).strip().lower() == "max":
        balance_of = kwargs.get("balance_of")
        if balance_of:
            resolved = resolve_amount_or_balance("max", ctx, **kwargs)
            return [resolved] if resolved else ["0"]
        raise ValueError("wrap_in_array: 'max' requested but no balance_of configured")
    try:
        d = Decimal(str(value))
    except Exception:
        raise ValueError(f"wrap_in_array: invalid amount '{value}' — expected a number")
    decimals_from = kwargs.get("decimals_from")
    if decimals_from:
        decimals = resolve_decimals(decimals_from, ctx)
    else:
        decimals = kwargs.get("decimals", 18)
    scale = Decimal(10) ** decimals
    base = (d * scale).to_integral_value(rounding=ROUND_DOWN)
    return [str(int(base))]


def build_fixed_array(value: Any, ctx: ResolveContext, **kwargs) -> List[str]:
    """Build a fixed-size array with one non-zero slot (e.g. Curve 3pool)."""
    array_size = kwargs.get("array_size", 3)
    fill_value = kwargs.get("fill_value", "0")
    arr = [fill_value] * array_size

    index_map = kwargs.get("index_map", {})
    asset_field = kwargs.get("asset_field", "asset")
    asset_sym = (ctx.raw_args.get(asset_field) or "USDC").upper()
    idx = index_map.get(asset_sym, 0)

    amount_val = value
    if amount_val is not None:
        decimals = resolve_decimals(kwargs.get("decimals_from"), ctx, asset_sym=asset_sym)
        d = Decimal(str(amount_val))
        scale = Decimal(10) ** decimals
        base = (d * scale).to_integral_value(rounding=ROUND_DOWN)
        arr[idx] = str(int(base))

    return arr


def resolve_constant(value: Any, ctx: ResolveContext, **kwargs) -> Any:
    """Return a constant value from the playbook spec."""
    return kwargs.get("value", value)


def llm_passthrough(value: Any, ctx: ResolveContext, **kwargs) -> Any:
    """Return the LLM field value as-is."""
    return value


def compute_human_readable(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Render a template string using LLM args."""
    existing = ctx.raw_args.get("human_readable_amount")
    if existing:
        return existing
    template = kwargs.get("template", "")
    merged = {**ctx.raw_args}
    try:
        return template.format(**merged)
    except (KeyError, IndexError):
        return ""


def resolve_contract_address(value: Any, ctx: ResolveContext, **kwargs) -> Optional[str]:
    """Look up address from the playbook's contracts map."""
    contract_key = kwargs.get("contract_key", "")
    contract = ctx.playbook_contracts.get(contract_key, {})
    return contract.get("address")
