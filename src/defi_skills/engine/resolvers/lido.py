"""Lido resolvers: withdrawal request IDs, checkpoint hints."""

from typing import Any, List

from eth_utils import to_checksum_address

from defi_skills.engine.resolvers import common
from defi_skills.engine.resolvers.common import ResolveContext


def resolve_lido_withdrawal_requests(value: Any, ctx: ResolveContext, **kwargs) -> List:
    """Query Lido WithdrawalQueue for pending withdrawal request IDs."""
    wq_address = kwargs.get("withdrawal_queue_address")
    if not wq_address:
        raise ValueError("resolve_lido_withdrawal_requests: withdrawal_queue_address not provided by playbook")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3 or not ctx.from_address:
        raise ValueError("resolve_lido_withdrawal_requests: no web3 or from_address")

    from_addr = to_checksum_address(ctx.from_address)
    wq_addr = to_checksum_address(wq_address)

    (request_ids,) = common.raw_eth_call(
        w3, wq_addr,
        "getWithdrawalRequests(address)", ["address"], [from_addr], ["uint256[]"]
    )

    if not request_ids:
        raise ValueError("resolve_lido_withdrawal_requests: no pending withdrawal requests")

    return list(request_ids)


def resolve_lido_checkpoint_hints(value: Any, ctx: ResolveContext, **kwargs) -> List:
    """Query Lido WithdrawalQueue for checkpoint hints matching resolved request IDs."""
    wq_address = kwargs.get("withdrawal_queue_address")
    if not wq_address:
        raise ValueError("resolve_lido_checkpoint_hints: withdrawal_queue_address not provided by playbook")
    w3 = ctx.token_resolver.w3 if ctx.token_resolver else None
    if not w3:
        raise ValueError("resolve_lido_checkpoint_hints: no web3 instance")

    wq_addr = to_checksum_address(wq_address)
    request_ids = ctx.resolved.get("_requestIds")
    if not request_ids:
        raise ValueError("resolve_lido_checkpoint_hints: _requestIds not resolved")

    (last_index,) = common.raw_eth_call(w3, wq_addr, "getLastCheckpointIndex()", [], [], ["uint256"])

    (hints,) = common.raw_eth_call(
        w3, wq_addr,
        "findCheckpointHints(uint256[],uint256,uint256)",
        ["uint256[]", "uint256", "uint256"],
        [request_ids, 1, last_index],
        ["uint256[]"]
    )

    return list(hints)
