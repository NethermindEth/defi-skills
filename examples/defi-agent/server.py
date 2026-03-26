"""Session-based tool-calling DeFi Agent server.

Replaces the single-shot /intent endpoint with an agent that mirrors
the CLI's chat.py pattern: OpenAI function-calling tools executed in a
loop until the LLM produces a text-only response.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from litellm import completion

from defi_skills.engine.playbook_engine import PlaybookEngine
from defi_skills.engine.token_resolver import TokenResolver
from defi_skills.engine.ens_resolver import ENSResolver
from defi_skills.engine.chains import supported_chain_ids, get_chain_config

# ---------------------------------------------------------------------------
# Engine initialisation (one per supported chain)
# ---------------------------------------------------------------------------

engines: Dict[int, PlaybookEngine] = {}
for _cid in supported_chain_ids():
    _tr = TokenResolver(chain_id=_cid)
    _er = ENSResolver(w3=_tr.w3, chain_id=_cid)
    engines[_cid] = PlaybookEngine(token_resolver=_tr, ens_resolver=_er)

app = FastAPI(title="DeFi Agent", version="0.2.0")
HERE = Path(__file__).parent
logger = logging.getLogger("defi_agent")

# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

sessions: Dict[str, dict] = {}  # session_id -> {chain_id, messages, wallet_state, last_active}
MAX_SESSIONS = 50
SESSION_TTL = 1800  # 30 minutes


def _evict_sessions():
    """Remove expired sessions and cap total count."""
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s["last_active"] > SESSION_TTL]
    for sid in expired:
        del sessions[sid]
    # If still over limit, drop oldest
    while len(sessions) > MAX_SESSIONS:
        oldest = min(sessions, key=lambda k: sessions[k]["last_active"])
        del sessions[oldest]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class WalletState(BaseModel):
    address: Optional[str] = None
    chain_id: int = 1
    balances: Dict[str, str] = {}


class CreateSessionRequest(BaseModel):
    chain_id: int = 1
    wallet_state: Optional[WalletState] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    chain_id: int
    chain_name: str
    actions_count: int


class MessageRequest(BaseModel):
    message: str = Field(max_length=4000)
    wallet_state: Optional[WalletState] = None


class MessageResponse(BaseModel):
    reply: str
    transactions: List[dict]
    tool_log: List[dict]
    awaiting_tx: bool


class BuildRequest(BaseModel):
    action: str
    arguments: Dict[str, Any]
    from_address: str
    chain_id: int = 1


# ---------------------------------------------------------------------------
# Internal sources to skip when extracting user-facing params
# ---------------------------------------------------------------------------

SKIP_SOURCES = frozenset({
    "constant", "resolve_deadline", "compute_human_readable",
    "resolve_contract_address", "resolve_uniswap_quote",
    "resolve_balancer_pool_id", "resolve_balancer_limit",
    "resolve_curve_min_mint", "resolve_curve_min_amounts",
    "resolve_token_ordering", "resolve_tick_range",
    "resolve_uniswap_position", "resolve_partial_liquidity",
    "resolve_balancer_pool_tokens", "resolve_balancer_userdata",
    "resolve_eigenlayer_deposits", "resolve_eigenlayer_queued_withdrawals",
    "resolve_lido_withdrawal_requests", "resolve_lido_checkpoint_hints",
    "resolve_aave_reward_assets",
})


def _describe_field(llm_field, source, arg_spec):
    """Return a human-readable hint for a field."""
    if source == "resolve_token_address":
        return "token symbol (e.g. USDC, ETH, WETH)"
    if source in ("resolve_amount", "resolve_amount_or_max", "resolve_amount_or_balance"):
        return 'amount as string (e.g. "500"), or "max"'
    if source == "resolve_ens_or_hex":
        if arg_spec.get("context_field") == "from_address":
            return "address or ENS (defaults to sender)"
        return "address or ENS (e.g. vitalik.eth or 0x...)"
    if source == "resolve_eigenlayer_strategy":
        return "token symbol (e.g. stETH, rETH)"
    if source == "resolve_fee_tier":
        return "500, 3000, or 10000 (auto-detected if omitted)"
    if source == "resolve_interest_rate_mode":
        return '"variable" (default)'
    if source == "resolve_smart_amount":
        return "amount as string"
    if source == "llm_passthrough":
        if llm_field == "asset":
            return "token symbol (e.g. USDC)"
        if llm_field == "slippage":
            return 'slippage tolerance as percentage (e.g. "0.5" for 0.5%)'
        return "value (passed as-is)"
    return "value"


def get_action_params(spec):
    """Extract required/optional user-facing params from an action spec."""
    payload_args = spec.get("payload_args", {})
    required = {}
    optional = {}
    seen = set()

    for arg_name, arg_spec in payload_args.items():
        if arg_name == "human_readable_amount":
            continue
        source = arg_spec.get("source", "")
        if source in SKIP_SOURCES:
            continue
        llm_field = arg_spec.get("llm_field")
        if not llm_field or llm_field in seen:
            continue
        seen.add(llm_field)

        hint = _describe_field(llm_field, source, arg_spec)

        if (arg_spec.get("optional")
                or arg_spec.get("context_field")
                or source in ("resolve_fee_tier", "resolve_interest_rate_mode")):
            optional[llm_field] = hint
        else:
            required[llm_field] = hint

    return required, optional


# ---------------------------------------------------------------------------
# LLM tools (OpenAI function-calling format; litellm translates)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_actions",
            "description": "List all supported DeFi actions for the current chain, grouped by protocol.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "action_info",
            "description": (
                "Get details about a specific action: required and optional parameters, "
                "valid tokens, and description. Always call this before build_transaction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_name": {
                        "type": "string",
                        "description": "The action name, e.g. 'aave_supply'",
                    },
                },
                "required": ["action_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_transaction",
            "description": (
                "Build an unsigned Ethereum transaction for a single action. "
                "IMPORTANT: You MUST call action_info first to get the exact parameter names. "
                "All parameters go inside the 'args' object using the field names from action_info. "
                "Example: build_transaction(action='lido_stake', args={'amount': '1'})."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Exact action name from action_info.",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Key-value pairs using the exact field names returned by action_info. "
                            "Use human-readable values: token symbols like 'USDC' (not addresses), "
                            "decimal amounts like '500' or '0.5' (not Wei), ENS names or 0x addresses."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["action", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet_state",
            "description": (
                "Get the current wallet state: address, chain_id, and token balances. "
                "Useful after a user signs a transaction to get fresh balances."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name, arguments, engine, chain_id, wallet_state):
    """Execute a tool call.

    Returns (result_dict, transactions_list).
    transactions_list is non-empty only for build_transaction.
    """
    if name == "list_actions":
        by_protocol = engine.get_actions_by_protocol(chain_id)
        result = {
            "by_protocol": by_protocol,
            "total_actions": sum(len(v) for v in by_protocol.values()),
        }
        return result, []

    if name == "action_info":
        action_name = arguments.get("action_name", "")
        spec = engine.playbooks.get(chain_id, {}).get(action_name)
        if not spec:
            return {"error": f"Unknown action: '{action_name}'. Use list_actions to see available actions."}, []
        pb = engine.playbook_meta.get(chain_id, {}).get(action_name, {})
        required, optional = get_action_params(spec)
        result = {
            "action": action_name,
            "protocol": pb.get("protocol", "unknown"),
            "description": spec.get("description", ""),
            "required_params": required,
            "optional_params": optional,
            "valid_tokens": spec.get("valid_tokens"),
        }
        return result, []

    if name == "build_transaction":
        action = arguments.get("action", "")
        args = arguments.get("args", {})
        from_address = wallet_state.get("address", "0x0000000000000000000000000000000000000000")
        llm_output = {"action": action, "arguments": args}
        result = engine.build_transactions(llm_output, chain_id=chain_id, from_address=from_address)

        transactions = []
        if result.get("success"):
            for tx in result.get("transactions", []):
                transactions.append({
                    "type": tx.get("type", "action"),
                    "label": tx.get("label", action),
                    "action": action,
                    "raw_tx": tx.get("raw_tx", {}),
                })
        return result, transactions

    if name == "get_wallet_state":
        result = {
            "address": wallet_state.get("address"),
            "chain_id": wallet_state.get("chain_id", chain_id),
            "balances": wallet_state.get("balances", {}),
        }
        return result, []

    return {"error": f"Unknown tool: {name}"}, []


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(chain_id):
    """Chain-aware system prompt for the tool-calling agent."""
    chain = get_chain_config(chain_id)
    engine = engines[chain_id]
    by_protocol = engine.get_actions_by_protocol(chain_id)

    actions_lines = []
    for protocol, actions in sorted(by_protocol.items()):
        names = ", ".join(a["action"] for a in actions)
        actions_lines.append(f"  {protocol}: {names}")
    actions_section = "\n".join(actions_lines)

    ens_note = ""
    if not chain.ens_supported:
        ens_note = f"\n- ENS names (*.eth) are NOT available on {chain.name}. Always use hex addresses (0x...)."

    return f"""You are a DeFi transaction assistant for {chain.name} (chain_id {chain.chain_id}). You help users build unsigned Ethereum transactions through natural conversation.

WORKFLOW - follow this for every request:
1. Identify the action from the supported list below.
2. Call action_info to get exact parameter names and valid tokens. Never guess parameters.
3. Call build_transaction with the correct parameters.

Always call action_info before build_transaction.

MULTI-STEP OPERATIONS:
- For sequential steps where step 2 depends on step 1 output (e.g. "stake ETH then restake the stETH"):
  Build step 1 first, then STOP and tell the user to sign it. After they confirm, call get_wallet_state for fresh balances, then build step 2 using amount="max".
- For independent steps (e.g. "supply USDC to Aave and send ETH to alice.eth"):
  Build both immediately.

RULES:
- One action per build_transaction call.
- If a build fails, read the error and explain it.
- For ambiguous requests, ask clarifying questions. Do not assume.
- Be concise. No emoji.
- Use standard token symbols (USDC, ETH, WETH, stETH, DAI, etc.).
- Use human-readable amounts (e.g. "500", "0.5", "max").{ens_note}

SUPPORTED ACTIONS:
{actions_section}"""


# ---------------------------------------------------------------------------
# Agent turn
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 10


def run_agent_turn(session, user_message):
    """Execute one full agent turn (tool-calling loop).

    Returns {reply, transactions, tool_log, awaiting_tx}.
    """
    chain_id = session["chain_id"]
    engine = engines[chain_id]
    wallet_state = session["wallet_state"]
    messages = session["messages"]

    messages.append({"role": "user", "content": user_message})

    all_transactions = []
    tool_log = []

    for _ in range(MAX_ITERATIONS):
        try:
            resp = completion(
                model="claude-sonnet-4-6",
                messages=messages,
                tools=TOOLS,
                temperature=0.1,
            )
        except Exception as e:
            logger.exception("LLM call failed")
            return {
                "reply": f"LLM error: {e}",
                "transactions": [],
                "tool_log": tool_log,
                "awaiting_tx": False,
            }

        choice = resp.choices[0]
        assistant_msg = {"role": "assistant", "content": choice.message.content or None}

        tool_calls = choice.message.tool_calls
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            # Text-only response -- turn is done
            reply = (choice.message.content or "").strip()
            awaiting = len(all_transactions) > 0
            return {
                "reply": reply,
                "transactions": all_transactions,
                "tool_log": tool_log,
                "awaiting_tx": awaiting,
            }

        # Execute each tool call
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            result, txs = execute_tool(fn_name, fn_args, engine, chain_id, wallet_state)
            all_transactions.extend(txs)

            tool_log.append({
                "tool": fn_name,
                "arguments": fn_args,
                "result": result,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    # Exceeded iteration cap
    return {
        "reply": "Reached maximum tool iterations. Please try a simpler request.",
        "transactions": all_transactions,
        "tool_log": tool_log,
        "awaiting_tx": len(all_transactions) > 0,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/session", response_model=CreateSessionResponse)
def create_session(req: CreateSessionRequest):
    """Create a new agent session with a chain-aware system prompt."""
    if req.chain_id not in engines:
        supported = ", ".join(str(c) for c in engines)
        raise HTTPException(400, f"Unsupported chain_id {req.chain_id}. Supported: {supported}")

    _evict_sessions()

    session_id = uuid.uuid4().hex[:12]
    chain = get_chain_config(req.chain_id)
    engine = engines[req.chain_id]
    by_protocol = engine.get_actions_by_protocol(req.chain_id)
    actions_count = sum(len(v) for v in by_protocol.values())

    system_prompt = build_system_prompt(req.chain_id)
    wallet_state = (
        req.wallet_state.model_dump() if req.wallet_state
        else {"address": None, "chain_id": req.chain_id, "balances": {}}
    )

    sessions[session_id] = {
        "chain_id": req.chain_id,
        "messages": [{"role": "system", "content": system_prompt}],
        "wallet_state": wallet_state,
        "last_active": time.time(),
    }

    return CreateSessionResponse(
        session_id=session_id,
        chain_id=req.chain_id,
        chain_name=chain.name,
        actions_count=actions_count,
    )


@app.post("/session/{session_id}/message", response_model=MessageResponse)
def send_message(session_id: str, req: MessageRequest):
    """Send a message to an existing session and run the agent turn."""
    if session_id not in sessions:
        raise HTTPException(404, "Session not found or expired.")

    session = sessions[session_id]
    session["last_active"] = time.time()

    # Update wallet state if the frontend provides fresh data
    if req.wallet_state:
        session["wallet_state"] = req.wallet_state.model_dump()

    result = run_agent_turn(session, req.message)

    return MessageResponse(
        reply=result["reply"],
        transactions=result["transactions"],
        tool_log=result["tool_log"],
        awaiting_tx=result["awaiting_tx"],
    )


@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    """Delete an agent session."""
    if session_id not in sessions:
        raise HTTPException(404, "Session not found.")
    del sessions[session_id]
    return {"ok": True}


# ---------------------------------------------------------------------------
# Direct API endpoints (kept for non-session use)
# ---------------------------------------------------------------------------

@app.get("/actions")
def list_actions_endpoint(chain_id: int = 1):
    eng = engines.get(chain_id)
    if not eng:
        raise HTTPException(400, f"Unsupported chain_id: {chain_id}")
    by_protocol = eng.get_actions_by_protocol(chain_id)
    return {"by_protocol": by_protocol, "count": sum(len(v) for v in by_protocol.values())}


@app.post("/build")
def build(req: BuildRequest):
    eng = engines.get(req.chain_id)
    if not eng:
        raise HTTPException(400, f"Unsupported chain_id: {req.chain_id}")
    llm_output = {"action": req.action, "arguments": req.arguments}
    result = eng.build_transactions(llm_output, chain_id=req.chain_id, from_address=req.from_address)
    if not result.get("success"):
        raise HTTPException(422, result.get("error", "Build failed"))
    return result


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(HERE / "index.html")


app.mount("/", StaticFiles(directory=HERE), name="static")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    total = sum(
        sum(len(v) for v in eng.get_actions_by_protocol(cid).values())
        for cid, eng in engines.items()
    )
    chains = ", ".join(get_chain_config(c).name for c in engines)
    print(f"\n  DeFi Agent running at http://localhost:8000")
    print(f"  Chains: {chains}")
    print(f"  Total actions: {total}\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
