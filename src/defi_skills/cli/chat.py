"""Interactive chat agent for defi-skills."""

import json

import litellm
from litellm import completion
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from defi_skills import __version__
from defi_skills.cli.utils import fmt_amount

litellm.drop_params = True

# Rich console

THEME = Theme({
    "info": "dim",
    "success": "green",
    "error": "red",
    "warn": "yellow",
    "tool": "dim",
    "prompt": "bold green",
    "header": "bold green",
})

console = Console(theme=THEME, highlight=False)


# Tool definitions (OpenAI function-calling format, litellm translates)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": (
                "Get the current CLI configuration: wallet address, model, "
                "and API key status."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_config",
            "description": (
                "Set wallet address or model. "
                "Do NOT use for API keys — ask the user to run "
                "'defi-skills config set <key> <value>' manually."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Config key: 'wallet_address' or 'model'",
                    },
                    "value": {
                        "type": "string",
                        "description": "The value to set.",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_actions",
            "description": "List all supported DeFi actions grouped by protocol.",
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
                "valid tokens, and description. Use this when you need to check what "
                "arguments an action expects before building."
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
                "Example: build_transaction(action='lido_stake', args={'amount': '1'}) or "
                "build_transaction(action='aave_supply', args={'asset': 'USDC', 'amount': '500'})."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Exact action name from action_info (e.g. 'aave_supply', 'lido_stake', 'transfer_native').",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Key-value pairs using the exact field names returned by action_info. "
                            "Use human-readable values: token symbols like 'USDC' (not addresses), "
                            "decimal amounts like '500' or '0.5' (not Wei), ENS names like 'vitalik.eth' or 0x addresses. "
                            "Common fields: 'asset' (token symbol), 'amount' (decimal string), 'to' (recipient address or ENS)."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["action", "args"],
            },
        },
    },
]


# System prompt

def build_system_prompt(engine):
    """Build the system prompt with dynamic action knowledge from playbooks."""
    by_protocol = engine.get_actions_by_protocol()

    actions_lines = []
    for protocol, actions in sorted(by_protocol.items()):
        names = ", ".join(a["action"] for a in actions)
        actions_lines.append(f"**{protocol}**: {names}")
    actions_section = "\n".join(actions_lines)

    return f"""You are a DeFi transaction assistant for Ethereum mainnet. You help users build unsigned Ethereum transactions.

## Your Role
You understand the user's intent, plan multi-step operations, and use your tools to build transactions. You never sign or broadcast. You only produce unsigned payloads (to, value, data).

## How to Interact
- **Direct request** (e.g., "Send 0.5 ETH to vitalik.eth"): Build it immediately using build_transaction.
- **Multi-step request** (e.g., "Stake ETH on Lido then restake on EigenLayer"): Present your plan first. Explain each step, the tokens and amounts involved, and whether outputs are predictable. Wait for the user to confirm before building.
- **Ambiguous request** (e.g., "Earn yield on my ETH"): Ask clarifying questions. Do not assume a protocol or strategy.
- **Large amounts or "max"**: Confirm with the user. "max" means their entire wallet balance of that token.

## Workflow
For every user request, follow this sequence:
1. Identify the action name from the supported actions list below.
2. Call action_info to get the exact parameter names and valid tokens. Do NOT guess parameter names.
3. Call build_transaction with the correct parameters from step 2.

Always call action_info before build_transaction. The call is instant (local lookup) and prevents errors from wrong field names.

## Constraints
- Ethereum mainnet only (chain_id 1). All contract addresses are mainnet.
- One action per build_transaction call. For multi-step, make multiple calls.
- Always check "success" in build responses. If false, read the error and fix.
- Negative amounts are rejected.
- Wallet address is pre-configured. Use get_config to check, set_config to change.
- If a build fails due to a missing API key (e.g. "no web3 instance"), tell the user to set it manually: `defi-skills config set alchemy_api_key <KEY>`. Never handle API keys through tools.

## Multi-Step Planning
- **Predictable outputs** (stake, wrap, supply): input roughly equals output. Build all steps with the same amount.
- **Unpredictable outputs** (swaps): output depends on live prices. Build step 1 first, then ask the user for the result before building step 2.
- **"max" means entire wallet balance**, not "output of the previous step."

## Presenting Results
- After building: summarize what was built (action, tokens, amounts, number of transactions).
- Show raw transaction JSON only when the user asks for it.
- Be concise. Do not repeat information the user already knows.

## Supported Actions
{actions_section}

Use action_info to check parameters before building if unsure."""


# Tool execution

def execute_tool(name, arguments, engine, chat_state):
    """Execute a tool call and return the result as a dict."""
    from defi_skills.cli.main import build_tx, get_action_params
    from defi_skills.cli import config as cfg
    from eth_utils import is_address

    if name == "get_config":
        all_cfg = cfg.get_all()
        result = {}
        for key in ["wallet_address", "model"]:
            info = all_cfg[key]
            result[key] = {"value": info["value"], "source": info["source"]}
        result["model"] = {"value": chat_state["model"], "source": "running"}
        api_keys = {}
        for config_key in cfg.API_KEY_FIELDS:
            info = all_cfg[config_key]
            api_keys[config_key] = "configured" if info["value"] else "not set"
        result["api_keys"] = api_keys
        return result

    if name == "set_config":
        key = arguments.get("key", "")
        value = arguments.get("value", "")

        if key == "wallet_address":
            if not is_address(value):
                return {"error": f"Invalid Ethereum address: {value}"}
            cfg.set_value("wallet_address", value)
            chat_state["wallet_addr"] = value
            return {"success": True, "message": f"Wallet set to {value}"}

        if key == "model":
            cfg.set_value("model", value)
            return {"success": True, "message": f"Model set to {value} (takes effect next session)"}

        if key in cfg.API_KEY_FIELDS:
            return {
                "error": f"Cannot set API keys through chat. "
                f"Ask the user to run: defi-skills config set {key} <value>"
            }

        return {"error": f"Unknown config key: {key}. Valid keys: wallet_address, model"}

    if name == "list_actions":
        by_protocol = engine.get_actions_by_protocol()
        return {
            "by_protocol": by_protocol,
            "total_actions": sum(len(v) for v in by_protocol.values()),
        }

    if name == "action_info":
        action_name = arguments.get("action_name", "")
        spec = engine.playbooks.get(action_name)
        if not spec:
            return {"error": f"Unknown action: '{action_name}'. Use list_actions to see available actions."}
        pb = engine.playbook_meta.get(action_name, {})
        required, optional = get_action_params(spec)
        return {
            "action": action_name,
            "protocol": pb.get("protocol", "unknown"),
            "description": spec.get("description", ""),
            "required_params": required,
            "optional_params": optional,
            "valid_tokens": spec.get("valid_tokens"),
        }

    if name == "build_transaction":
        action = arguments.get("action", "")
        args = arguments.get("args", {})
        return build_tx(engine, action, args, chat_state["wallet_addr"], 1)

    if name == "simulate":
        from defi_skills.cli.simulate import run_simulation

        steps = arguments.get("steps", [])
        if not steps:
            return {"success": False, "error": "No steps provided."}

        all_transactions = []
        for i, step in enumerate(steps):
            step_action = step.get("action", "")
            step_args = step.get("args", {})
            result = build_tx(engine, step_action, step_args, chat_state["wallet_addr"], 1)
            if not result.get("success"):
                return {
                    "success": False,
                    "error": f"Step {i + 1} ({step_action}) build failed: {result.get('error')}",
                }
            for tx in result.get("transactions", []):
                tx["plan_step"] = i + 1
                tx["plan_action"] = step_action
                all_transactions.append(tx)

        build_result = {"success": True, "transactions": all_transactions}
        return run_simulation(build_result, chat_state["wallet_addr"])

    return {"error": f"Unknown tool: {name}"}


# Display helpers

def tool_start_label(name, arguments):
    """Human-readable label for a tool call in progress."""
    if name == "get_config":
        return "Reading configuration"
    if name == "set_config":
        key = arguments.get("key", "")
        return f"Setting {key}"
    if name == "list_actions":
        return "Discovering actions"
    if name == "action_info":
        return f"Checking {arguments.get('action_name', '')} parameters"
    if name == "build_transaction":
        return f"Building {arguments.get('action', '')} transaction"
    if name == "simulate":
        steps = arguments.get("steps", [])
        n = len(steps)
        if n == 1:
            return f"Simulating {steps[0].get('action', '')} on fork"
        return f"Simulating {n} steps on fork"
    return f"Running {name}"


def tool_result_line(name, result):
    """One-line summary after a tool completes. Returns (text, is_success)."""
    if name == "get_config":
        wallet = result.get("wallet_address", {}).get("value", "not set")

        return f"Wallet: {wallet}", True

    if name == "set_config":
        if "error" in result:
            return result["error"], False
        return result.get("message", "Config updated"), True

    if name == "list_actions":
        return f"Found {result.get('total_actions', 0)} actions across {len(result.get('by_protocol', {}))} protocols", True

    if name == "action_info":
        if "error" in result:
            return result["error"], False
        vt = result.get("valid_tokens")
        suffix = f" (tokens: {', '.join(vt)})" if vt else ""
        return f"{result.get('description', '')}{suffix}", True

    if name == "build_transaction":
        if not result.get("success"):
            return result.get("error", "Build failed"), False
        txs = result.get("transactions", [])
        approvals = sum(1 for t in txs if t.get("type") == "approval")
        parts = []
        if approvals:
            parts.append(f"{approvals} approval{'s' if approvals > 1 else ''}")
        parts.append(f"{len(txs) - approvals} action")
        return f"Built {len(txs)} tx ({' + '.join(parts)})", True

    if name == "simulate":
        if not result.get("success") and "simulation" not in result:
            return result.get("error", "Simulation failed"), False
        sim = result.get("simulation", [])
        ok = result.get("success", False)
        total_gas = sum(r.get("gas_used", 0) for r in sim)
        gas_str = f"gas: {total_gas:,}" if total_gas else ""

        bc = result.get("balance_changes", {})
        bc_parts = []
        for sym, change in bc.items():
            sign = "+" if change > 0 else "-"
            bc_parts.append(f"{sign}{fmt_amount(change)} {sym}")

        detail_parts = [p for p in [gas_str, ", ".join(bc_parts)] if p]
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""

        if ok:
            return f"Simulation passed{detail}", True
        failed = next((r for r in sim if r["status"] != "success"), None)
        if failed:
            return f"Simulation failed at step {failed['step']}: {failed.get('error', 'reverted')}", False
        return "Simulation failed", False

    return "Done", True


# Chat loop

def run_chat(engine, wallet_addr, _chain_id=1, model="claude-sonnet-4-6", stream=False):
    """Run the interactive chat agent loop."""
    system_prompt = build_system_prompt(engine)
    messages = [{"role": "system", "content": system_prompt}]

    chat_state = {"wallet_addr": wallet_addr, "model": model}

    by_protocol = engine.get_actions_by_protocol()
    action_count = sum(len(v) for v in by_protocol.values())
    protocol_count = len(by_protocol)

    # Welcome banner
    info_table = Table.grid(padding=(0, 2))
    info_table.add_column(style="dim", width=8)
    info_table.add_column()
    info_table.add_row("Model", model)
    info_table.add_row("Chain", "Ethereum Mainnet")
    info_table.add_row("Wallet", wallet_addr)
    info_table.add_row("Actions", f"{action_count} across {protocol_count} protocols")

    console.print()
    console.print(Panel(
        info_table,
        title=f"[bold green]defi-skills[/bold green] [dim]v{__version__}[/dim]",
        title_align="left",
        border_style="green",
        padding=(0, 1),
        width=60,
    ))
    console.print("  [dim]/help for commands, /exit to quit[/dim]")
    console.print()

    # Prompt session with history and proper line editing
    session = PromptSession(
        history=InMemoryHistory(),
        enable_history_search=True,
    )

    while True:
        # Read user input
        try:
            user_input = session.prompt(HTML("<ansigreen><b> > </b></ansigreen>")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]Goodbye![/dim]\n")
            break

        if not user_input:
            continue

        # Handle slash commands
        cmd = user_input.lower()
        if cmd in ("/exit", "/quit", "exit", "quit", "q"):
            console.print("  [dim]Goodbye![/dim]\n")
            break

        if cmd == "/help":
            help_text = Text()
            help_text.append("\n  Commands\n", style="bold")
            help_text.append("  /help     ", style="green")
            help_text.append("Show this help message\n")
            help_text.append("  /clear    ", style="green")
            help_text.append("Clear conversation history\n")
            help_text.append("  /exit     ", style="green")
            help_text.append("Exit the chat\n")
            help_text.append("\n  Tips\n", style="bold")
            help_text.append("  Ask in natural language: ", style="dim")
            help_text.append('"Supply 100 USDC to Aave"\n')
            help_text.append("  Multi-step works:        ", style="dim")
            help_text.append('"Stake 10 ETH on Lido, then restake on EigenLayer"\n')
            help_text.append("  Discover actions:        ", style="dim")
            help_text.append('"What protocols do you support?"\n')
            console.print(help_text)
            continue

        if cmd == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            console.print("  [dim]Conversation cleared.[/dim]\n")
            continue

        messages.append({"role": "user", "content": user_input})

        # Agent loop: LLM -> tool calls -> LLM -> ... -> text response
        while True:
            try:
                response = completion(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    temperature=0.1,
                    stream=True,
                )
            except Exception as e:
                err_msg = str(e)
                if "api_key" in err_msg.lower() or "auth" in err_msg.lower():
                    console.print("\n  [error]Authentication error.[/error]")
                    console.print("  [dim]Set your API key: defi-skills config set anthropic_api_key <KEY>[/dim]\n")
                else:
                    console.print(f"\n  [error]Error: {err_msg}[/error]\n")
                messages.pop()
                break

            # Accumulate streamed response
            content = ""
            tool_calls_by_idx = {}
            text_started = False
            stream_error = None

            live = Live(
                Spinner("dots", text=Text(" Thinking...", style="dim")),
                console=console,
                transient=True,
                refresh_per_second=8,
            )
            live.start()

            try:
                for chunk in response:
                    delta = chunk.choices[0].delta

                    if delta.content:
                        content += delta.content
                        if stream:
                            if not text_started:
                                live.stop()
                                text_started = True
                            print(delta.content, end="", flush=True)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_by_idx:
                                tool_calls_by_idx[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc_delta.id:
                                tool_calls_by_idx[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_calls_by_idx[idx]["function"]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_calls_by_idx[idx]["function"]["arguments"] += tc_delta.function.arguments
            except Exception as e:
                stream_error = e

            if text_started:
                print()
            if live.is_started:
                live.stop()

            # Formatted mode: render complete response with Markdown
            if not stream and content.strip():
                console.print(Markdown(content.strip()), width=min(console.width, 80))

            if stream_error:
                console.print(f"\n  [error]Stream error: {stream_error}[/error]\n")
                break

            # Build tool_calls list (sorted by index)
            tool_calls = (
                [tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx)]
                if tool_calls_by_idx
                else []
            )

            # Add assistant message to history
            assistant_msg = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                break  # Text-only response, conversation turn is done

            # Execute each tool call with visual feedback
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                label = tool_start_label(fn_name, fn_args)

                # Show spinner while tool executes
                with Live(
                    Spinner("dots", text=Text(f" {label}...", style="dim")),
                    console=console,
                    transient=True,
                    refresh_per_second=10,
                ):
                    result = execute_tool(fn_name, fn_args, engine, chat_state)

                summary, ok = tool_result_line(fn_name, result)
                if ok:
                    console.print(f"  [success]\u2713[/success] [dim]{summary}[/dim]")
                else:
                    console.print(f"  [error]\u2717[/error] [dim]{summary}[/dim]")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str),
                })

            # Loop back to send tool results to LLM
