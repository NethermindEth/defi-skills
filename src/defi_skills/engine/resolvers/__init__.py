"""Resolver package — re-exports for backward compatibility.

External code should import from here:
    from defi_skills.engine.resolvers import RESOLVER_REGISTRY, ResolveContext
"""

from defi_skills.engine.resolvers.common import (
    ResolveContext,
    DecimalsResolutionError,
    raw_eth_call,
    resolve_decimals,
    resolve_slippage_bps,
    UINT256_MAX,
    DEFAULT_SLIPPAGE_BPS,
)

from defi_skills.engine.resolvers.core import (
    resolve_token_address,
    resolve_collection_address,
    resolve_amount,
    resolve_amount_or_max,
    resolve_amount_or_balance,
    resolve_ens_or_hex,
    resolve_fee_tier,
    resolve_deadline,
    resolve_interest_rate_mode,
    resolve_smart_amount,
    wrap_in_array,
    build_fixed_array,
    resolve_constant,
    llm_passthrough,
    compute_human_readable,
    resolve_contract_address,
)

from defi_skills.engine.resolvers.uniswap import (
    resolve_uniswap_quote,
    resolve_token_ordering,
    resolve_tick_range,
    resolve_uniswap_position,
    resolve_partial_liquidity,
)

from defi_skills.engine.resolvers.balancer import (
    resolve_balancer_pool_id,
    resolve_balancer_limit,
    resolve_balancer_pool_tokens,
    resolve_balancer_userdata,
)

from defi_skills.engine.resolvers.curve import (
    resolve_curve_min_mint,
    resolve_curve_min_amounts,
)

from defi_skills.engine.resolvers.eigenlayer import (
    resolve_eigenlayer_strategy,
    resolve_eigenlayer_deposits,
    resolve_eigenlayer_queued_withdrawals,
)

from defi_skills.engine.resolvers.lido import (
    resolve_lido_withdrawal_requests,
    resolve_lido_checkpoint_hints,
)

from defi_skills.engine.resolvers.aave import (
    resolve_aave_reward_assets,
)

from defi_skills.engine.resolvers.pendle import (
    resolve_pendle_market,
    resolve_pendle_min_out,
    resolve_pendle_yt,
)


RESOLVER_REGISTRY = {
    "resolve_token_address": resolve_token_address,
    "resolve_collection_address": resolve_collection_address,
    "resolve_amount": resolve_amount,
    "resolve_amount_or_max": resolve_amount_or_max,
    "resolve_amount_or_balance": resolve_amount_or_balance,
    "resolve_ens_or_hex": resolve_ens_or_hex,
    "resolve_fee_tier": resolve_fee_tier,
    "resolve_deadline": resolve_deadline,
    "resolve_smart_amount": resolve_smart_amount,
    "wrap_in_array": wrap_in_array,
    "build_fixed_array": build_fixed_array,
    "constant": resolve_constant,
    "llm_passthrough": llm_passthrough,
    "compute_human_readable": compute_human_readable,
    "resolve_contract_address": resolve_contract_address,
    "resolve_eigenlayer_strategy": resolve_eigenlayer_strategy,
    "resolve_interest_rate_mode": resolve_interest_rate_mode,
    "resolve_uniswap_quote": resolve_uniswap_quote,
    "resolve_balancer_pool_id": resolve_balancer_pool_id,
    "resolve_balancer_limit": resolve_balancer_limit,
    "resolve_curve_min_mint": resolve_curve_min_mint,
    "resolve_curve_min_amounts": resolve_curve_min_amounts,
    "resolve_eigenlayer_deposits": resolve_eigenlayer_deposits,
    "resolve_lido_withdrawal_requests": resolve_lido_withdrawal_requests,
    "resolve_lido_checkpoint_hints": resolve_lido_checkpoint_hints,
    "resolve_aave_reward_assets": resolve_aave_reward_assets,
    "resolve_token_ordering": resolve_token_ordering,
    "resolve_tick_range": resolve_tick_range,
    "resolve_uniswap_position": resolve_uniswap_position,
    "resolve_partial_liquidity": resolve_partial_liquidity,
    "resolve_balancer_pool_tokens": resolve_balancer_pool_tokens,
    "resolve_balancer_userdata": resolve_balancer_userdata,
    "resolve_eigenlayer_queued_withdrawals": resolve_eigenlayer_queued_withdrawals,
    "resolve_pendle_market": resolve_pendle_market,
    "resolve_pendle_min_out": resolve_pendle_min_out,
    "resolve_pendle_yt": resolve_pendle_yt,
}
