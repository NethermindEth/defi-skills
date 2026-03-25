"""Minimal self-contained server for the DeFi Agent demo."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from litellm import completion

from defi_skills.engine.playbook_engine import PlaybookEngine
from defi_skills.engine.token_resolver import TokenResolver
from defi_skills.engine.ens_resolver import ENSResolver

# Engine init

tr = TokenResolver()
er = ENSResolver(w3=tr.w3)
engine = PlaybookEngine(token_resolver=tr, ens_resolver=er)

app = FastAPI(title="DeFi Agent", version="0.1.0")

HERE = Path(__file__).parent


# Request/response models

class BuildRequest(BaseModel):
    action: str
    arguments: Dict[str, Any]
    from_address: str
    chain_id: int = 1


class IntentRequest(BaseModel):
    message: str
    wallet_address: str
    balances: Dict[str, str] = {}
    history: List[Dict[str, str]] = []


class IntentGoal(BaseModel):
    id: str
    action: str
    arguments: Dict[str, Any]
    description: str
    depends_on: str | None = None


class IntentResponse(BaseModel):
    reply: str
    goals: List[IntentGoal] = []


# Action discovery

@app.get("/actions")
def list_actions():
    by_protocol = engine.get_actions_by_protocol()
    return {"by_protocol": by_protocol, "count": sum(len(v) for v in by_protocol.values())}


@app.get("/actions/{action_name}")
def get_action(action_name: str):
    spec = engine.playbooks.get(action_name)
    if not spec:
        raise HTTPException(404, f"Unknown action: {action_name}")
    pb = engine.playbook_meta.get(action_name, {})
    return {
        "action": action_name,
        "protocol": pb.get("protocol", "unknown"),
        "description": spec.get("description", ""),
        "valid_tokens": spec.get("valid_tokens"),
    }


# Transaction building

@app.post("/build")
def build(req: BuildRequest):
    llm_output = {"action": req.action, "arguments": req.arguments}
    result = engine.build_transactions(llm_output, chain_id=req.chain_id, from_address=req.from_address)
    if not result.get("success"):
        raise HTTPException(422, result.get("error", "Build failed"))
    return result


# Intent parsing

def build_action_context():
    """Build a concise action list for the LLM system prompt."""
    lines = []
    for name in sorted(engine.get_supported_actions()):
        spec = engine.playbooks.get(name, {})
        desc = spec.get("description", "")
        tokens = spec.get("valid_tokens")
        payload_args = spec.get("payload_args", {})

        # Extract user-facing params
        params = []
        skip = {"constant", "resolve_deadline", "compute_human_readable",
                "resolve_contract_address", "resolve_uniswap_quote",
                "resolve_balancer_pool_id", "resolve_balancer_limit",
                "resolve_curve_min_mint", "resolve_curve_min_amounts",
                "resolve_token_ordering", "resolve_tick_range",
                "resolve_uniswap_position", "resolve_partial_liquidity",
                "resolve_balancer_pool_tokens", "resolve_balancer_userdata",
                "resolve_eigenlayer_deposits", "resolve_eigenlayer_queued_withdrawals",
                "resolve_lido_withdrawal_requests", "resolve_lido_checkpoint_hints",
                "resolve_aave_reward_assets"}
        seen = set()
        for arg_spec in payload_args.values():
            source = arg_spec.get("source", "")
            llm_field = arg_spec.get("llm_field")
            if source in skip or not llm_field or llm_field in seen:
                continue
            seen.add(llm_field)
            opt = " (optional)" if arg_spec.get("optional") or arg_spec.get("context_field") else ""
            params.append(f"{llm_field}{opt}")

        token_str = f" [tokens: {', '.join(tokens)}]" if tokens else ""
        param_str = f" params: {', '.join(params)}" if params else ""
        lines.append(f"- {name}: {desc}{token_str}{param_str}")
    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = (
    "You are a DeFi transaction assistant powered by the defi-skills engine. "
    "You help users build Ethereum mainnet transactions through natural conversation.\n\n"
    "When the user expresses a clear DeFi intent (swap, stake, supply, borrow, transfer, etc.), "
    "respond with a JSON block containing goals to execute. "
    "When the user is chatting, asking questions, or being ambiguous, respond conversationally — "
    "ask clarifying questions, explain options, or greet them.\n\n"
    "IMPORTANT: When you identify actionable DeFi goals, include a JSON block in your response like this:\n"
    "```json\n"
    '{"goals": [{"id": "g1", "action": "action_name", "arguments": {"key": "value"}, "description": "what this does", "depends_on": null}]}\n'
    "```\n\n"
    "GOAL DEPENDENCIES — this is critical for correct execution:\n"
    "- Each goal has a unique id (g1, g2, g3, ...).\n"
    "- If a goal's inputs depend on the output of a previous goal, set depends_on to that goal's id.\n"
    "- For dependent goals, use \"max\" as the amount — the engine will read the actual on-chain balance at execution time.\n"
    "- If goals are independent (no output dependency), set depends_on to null.\n\n"
    "Examples:\n"
    "  Sequential: \"Stake 1 ETH on Lido then send the stETH to vitalik.eth\"\n"
    '  → g1: lido_stake amount=1, g2: transfer_erc20 asset=stETH amount=max to=vitalik.eth depends_on=g1\n'
    "  Independent: \"Supply 500 USDC to Aave and send 0.5 ETH to vitalik.eth\"\n"
    '  → g1: aave_supply amount=500, g2: transfer_eth amount=0.5 to=vitalik.eth depends_on=null\n'
    "  Chained: \"Swap 1000 USDC to WETH, then stake the WETH on Lido\"\n"
    '  → g1: uniswap_swap asset_in=USDC asset_out=WETH amount=1000, g2: lido_stake amount=max depends_on=g1\n\n'
    "Rules:\n"
    "- action must be an exact name from the supported actions list\n"
    "- arguments must use the parameter names listed for each action\n"
    "- Use standard token symbols (USDC, ETH, WETH, stETH, DAI, etc.)\n"
    "- Use human-readable amounts (e.g. \"500\", \"0.5\", \"max\")\n"
    "- If balance is insufficient, mention it but still provide the goals\n"
    "- Keep the reply concise — no tables or emoji\n\n"
    "Supported actions:\n"
)


@app.post("/intent", response_model=IntentResponse)
def parse_intent(req: IntentRequest):
    actions_ctx = build_action_context()
    system = SYSTEM_PROMPT_TEMPLATE + actions_ctx

    balance_str = ", ".join(f"{k}: {v}" for k, v in req.balances.items()) if req.balances else "unknown"

    # Build message history for multi-turn conversation
    messages = [{"role": "system", "content": system}]
    for msg in req.history:
        messages.append(msg)
    messages.append({
        "role": "user",
        "content": f"[Wallet: {req.wallet_address} | Balances: {balance_str}]\n\n{req.message}",
    })

    try:
        resp = completion(
            model="claude-sonnet-4-6",
            messages=messages,
            temperature=0.2,
        )
        raw = (resp.choices[0].message.content or "").strip()

        # Extract JSON block if present (```json ... ```)
        goals = []
        if "```json" in raw:
            try:
                json_str = raw.split("```json")[1].split("```")[0].strip()
                parsed = json.loads(json_str)
                raw_goals = parsed.get("goals", parsed.get("steps", []))
                for i, g in enumerate(raw_goals):
                    if "id" not in g:
                        g["id"] = f"g{i + 1}"
                    goals.append(IntentGoal(**g))
            except (json.JSONDecodeError, IndexError, KeyError):
                pass

        # Clean the reply — remove the JSON block for display
        reply = raw
        if "```json" in reply:
            parts = reply.split("```json")
            before = parts[0].strip()
            after = parts[1].split("```", 1)[1].strip() if "```" in parts[1] else ""
            reply = f"{before}\n{after}".strip()

        return IntentResponse(reply=reply, goals=goals)

    except Exception as e:
        logging.exception("Intent parsing failed")
        raise HTTPException(500, "Intent parsing failed. Check server logs for details.")


# UI

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(HERE / "index.html")

app.mount("/", StaticFiles(directory=HERE), name="static")


# Run

if __name__ == "__main__":
    import uvicorn
    print(f"\n  DeFi Agent running at http://localhost:8000")
    print(f"  Engine loaded: {len(list(engine.get_supported_actions()))} actions\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)
