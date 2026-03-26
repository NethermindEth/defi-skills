"""defi-skills CLI — translate natural language into Ethereum transactions."""

import json

import click
from dotenv import load_dotenv

from defi_skills import __version__
from defi_skills.cli import config as cfg
from defi_skills.cli.utils import fmt_amount

# ANSI colors
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def init_engine():
    """Initialize PlaybookEngine + resolvers."""
    from defi_skills.engine.token_resolver import TokenResolver
    from defi_skills.engine.ens_resolver import ENSResolver
    from defi_skills.engine.playbook_engine import PlaybookEngine

    tr = TokenResolver()
    er = ENSResolver(w3=tr.w3)
    engine = PlaybookEngine(token_resolver=tr, ens_resolver=er)
    return engine, tr, er


def build_tx(engine, action, arguments, from_address, chain_id):
    """Delegate to the engine's build_transactions pipeline."""
    llm_output = {"action": action, "arguments": arguments}
    return engine.build_transactions(llm_output, chain_id=chain_id, from_address=from_address)


def print_tx_result(result):
    """Print a build result in human-readable format."""
    if not result.get("success"):
        click.echo(f"\n  {RED}Error: {result.get('error')}{RESET}", err=True)
        return

    transactions = result.get("transactions", [])
    total = len(transactions)

    for i, tx in enumerate(transactions):
        tx_type = tx.get("type", "unknown")
        raw = tx.get("raw_tx", {})

        if tx_type == "approval":
            click.echo(f"\n  {YELLOW}{BOLD}Step {i + 1}/{total}: Approve{RESET}")
            click.echo(f"  {DIM}{'─' * 45}{RESET}")
            click.echo(f"  Token:    {tx.get('token')}")
            click.echo(f"  Spender:  {tx.get('spender')}")
        else:
            action_display = tx.get('action', '')
            if hasattr(action_display, 'value'):
                action_display = action_display.value
            click.echo(f"\n  {GREEN}{BOLD}Step {i + 1}/{total}: {action_display}{RESET}")
            click.echo(f"  {DIM}{'─' * 45}{RESET}")

        click.echo(f"  To:       {raw.get('to')}")
        click.echo(f"  Value:    {raw.get('value', '0')}")
        data_hex = raw.get("data", "")
        if len(data_hex) > 66:
            data_hex = data_hex[:66] + "..."
        click.echo(f"  Data:     {data_hex}")
        click.echo(f"  Chain ID: {raw.get('chain_id', 1)}")

    click.echo()


def print_sim_result(result):
    """Print simulation results in human-readable format."""
    if not result.get("success") and "simulation" not in result:
        click.echo(f"\n  {RED}Simulation Error: {result.get('error')}{RESET}", err=True)
        return

    current_plan_step = None
    for r in result.get("simulation", []):
        # Show plan step header when it changes (multi-step mode)
        ps = r.get("plan_step")
        if ps and ps != current_plan_step:
            current_plan_step = ps
            pa = r.get("plan_action", "")
            click.echo(f"\n  {MAGENTA}{BOLD}[{ps}] {pa}{RESET}")

        color = GREEN if r["status"] == "success" else RED
        click.echo(f"  {color}  Step {r['step']}: {r['type']} — {r['status']}{RESET}")
        if r.get("gas_used"):
            click.echo(f"      Gas used: {r['gas_used']:,}")
        if r.get("error"):
            click.echo(f"      {RED}{r['error']}{RESET}")

        # Show token transfers for this step
        for t in r.get("transfers", []):
            amt = fmt_amount(t["amount"])
            click.echo(
                f"      {CYAN}{amt} {t['symbol']}{RESET}"
                f"  {t['from']} {DIM}->{RESET} {t['to']}"
            )

    # Show net balance changes across all steps
    balance_changes = result.get("balance_changes", {})
    if balance_changes:
        click.echo(f"\n  {BOLD}Balance Changes:{RESET}")
        for symbol, change in balance_changes.items():
            sign = "+" if change > 0 else "-"
            color = GREEN if change > 0 else RED
            click.echo(f"    {color}{sign}{fmt_amount(change)} {symbol}{RESET}")

    ok = result.get("success")
    n = len(result.get("simulation", []))
    click.echo(f"\n  {GREEN if ok else RED}{BOLD}{'Simulation passed' if ok else 'Simulation failed'}. {n} transaction(s).{RESET}\n")


def describe_field(llm_field, source, arg_spec):
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
            return 'slippage tolerance as percentage (e.g. "0.5" for 0.5%, default: 0.5%)'
        return "value (passed as-is)"
    return "value"


# Sources resolved automatically — not user-facing
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

        hint = describe_field(llm_field, source, arg_spec)

        if arg_spec.get("optional") or arg_spec.get("context_field") or source in ("resolve_fee_tier", "resolve_interest_rate_mode"):
            optional[llm_field] = hint
        else:
            required[llm_field] = hint

    return required, optional


# CLI group

@click.group()
@click.version_option(version=__version__, prog_name="defi-skills")
def main():
    """defi-skills — translate natural language into Ethereum transactions."""
    load_dotenv()
    cfg.inject_config_env()


# Config subcommands

@main.group()
def config():
    """Manage CLI configuration."""
    pass


@config.command("set-wallet")
@click.argument("address")
def config_set_wallet(address):
    """Set your Ethereum wallet address."""
    from eth_utils import is_address
    if not is_address(address):
        click.echo(f"{RED}Error: invalid Ethereum address.{RESET}", err=True)
        raise SystemExit(1)
    cfg.set_value("wallet_address", address)
    click.echo(f"Wallet address set: {address}")


@config.command("set-model")
@click.argument("model")
def config_set_model(model):
    """Set the default LLM model (used only for NL mode)."""
    cfg.set_value("model", model)
    click.echo(f"Model set: {model}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a configuration value.  e.g.  defi-skills config set alchemy_api_key <KEY>"""
    cfg.set_value(key, value)
    display = cfg.mask_key(value) if "key" in key.lower() else value
    click.echo(f"{key} set: {display}")


@config.command("show")
def config_show():
    """Show current configuration."""
    all_cfg = cfg.get_all()

    # Core settings
    click.echo(f"\n  {BOLD}defi-skills Configuration{RESET}")
    click.echo(f"  {DIM}{'─' * 40}{RESET}")
    for key in ["wallet_address", "model"]:
        info = all_cfg[key]
        val = info["value"]
        src = info["source"]
        display = val if val is not None else f"{RED}not set{RESET}"
        click.echo(f"  {key:20s} {display}  {DIM}({src}){RESET}")

    # API keys
    click.echo(f"\n  {BOLD}API Keys{RESET}")
    click.echo(f"  {DIM}{'─' * 40}{RESET}")
    for config_key in cfg.API_KEY_FIELDS:
        info = all_cfg[config_key]
        val = info["value"]
        src = info["source"]
        if val is not None:
            click.echo(f"  {config_key:20s} {val}  {DIM}({src}){RESET}")
        else:
            click.echo(f"  {config_key:20s} {RED}not set{RESET}")
    click.echo()


@config.command("setup")
def config_setup():
    """Interactive guided setup for defi-skills."""
    click.echo(f"\n  {BOLD}defi-skills Setup{RESET}")
    click.echo(f"  {DIM}{'─' * 40}{RESET}")
    click.echo(f"  This will configure your CLI settings.")
    click.echo(f"  Config is stored at: {cfg.CONFIG_FILE}\n")

    updates = {}

    # Wallet address
    from eth_utils import is_address

    while True:
        address = click.prompt("  Ethereum wallet address (0x...)")
        if is_address(address):
            updates["wallet_address"] = address
            break
        click.echo(f"  {RED}Invalid Ethereum address. Try again.{RESET}")

    # NL mode
    click.echo(f"\n  {BOLD}Natural Language Mode{RESET}")
    click.echo(f"  {DIM}Needed for: defi-skills build \"Send 0.5 ETH to vitalik.eth\"{RESET}")
    click.echo(f"  {DIM}Not needed for: --action + --args deterministic mode{RESET}\n")

    setup_nl = click.confirm("  Set up an LLM API key for natural language mode?", default=True)
    if setup_nl:
        provider = click.prompt(
            "  LLM provider",
            type=click.Choice(["anthropic", "openai"], case_sensitive=False),
            default="anthropic",
        )

        if provider == "anthropic":
            key = click.prompt("  ANTHROPIC_API_KEY", hide_input=True)
            updates["anthropic_api_key"] = key
            default_model = "claude-sonnet-4-6"
        else:
            key = click.prompt("  OPENAI_API_KEY", hide_input=True)
            updates["openai_api_key"] = key
            default_model = "gpt-5.4"

        model = click.prompt("  LLM model", default=default_model)
        updates["model"] = model

    # Optional API keys
    click.echo(f"\n  {BOLD}Optional API Keys{RESET}")
    click.echo(f"  {DIM}Used for on-chain token/ENS resolution and ABI fetching.{RESET}")
    click.echo(f"  {DIM}Press Enter to skip any of these.{RESET}\n")

    for config_key, env_var in [
        ("alchemy_api_key", "ALCHEMY_API_KEY"),
        ("etherscan_api_key", "ETHERSCAN_API_KEY"),
        ("oneinch_api_key", "ONEINCH_API_KEY"),
    ]:
        val = click.prompt(f"  {env_var}", default="", show_default=False)
        if val:
            updates[config_key] = val

    # Save
    existing = cfg.load_config()
    existing.update(updates)
    cfg.save_config(existing)
    cfg.inject_config_env()

    # Summary
    click.echo(f"\n  {GREEN}{BOLD}Setup complete!{RESET}")
    click.echo(f"  Config saved to: {cfg.CONFIG_FILE}\n")

    # Show what was set
    click.echo(f"  {BOLD}Summary:{RESET}")
    click.echo(f"  {DIM}{'─' * 40}{RESET}")
    click.echo(f"  wallet_address      {updates.get('wallet_address', '')}")
    if "model" in updates:
        click.echo(f"  model               {updates['model']}")
    for config_key in cfg.API_KEY_FIELDS:
        if config_key in updates:
            click.echo(f"  {config_key:20s} {cfg.mask_key(updates[config_key])}")
    click.echo()


# Actions discovery

@main.command()
@click.argument("action_name", required=False)
@click.option("--json", "-j", "json_output", is_flag=True, help="Output raw JSON.")
def actions(action_name, json_output):
    """List supported actions, or show details for a specific action.

    \b
    Examples:
      defi-skills actions                 # list all
      defi-skills actions aave_supply     # show params for aave_supply
      defi-skills actions --json          # list all as JSON
    """
    engine, _, _ = init_engine()

    if action_name:
        spec = engine.playbooks.get(action_name)
        if not spec:
            click.echo(f"{RED}Unknown action: '{action_name}'{RESET}", err=True)
            click.echo(f"Run 'defi-skills actions' to see all supported actions.", err=True)
            raise SystemExit(1)

        pb = engine.playbook_meta.get(action_name, {})
        required, optional = get_action_params(spec)

        if json_output:
            click.echo(json.dumps({
                "action": action_name,
                "protocol": pb.get("protocol", "unknown"),
                "description": spec.get("description", ""),
                "required": required,
                "optional": optional,
                "valid_tokens": spec.get("valid_tokens"),
            }, indent=2))
        else:
            click.echo(f"\n  {BOLD}{action_name}{RESET} — {spec.get('description', '')}")
            click.echo(f"  Protocol: {pb.get('protocol', 'unknown')}")
            vt = spec.get("valid_tokens")
            if vt:
                click.echo(f"  Valid tokens: {', '.join(vt)}")
            click.echo(f"\n  {BOLD}Required:{RESET}")
            for k, v in required.items():
                click.echo(f"    {k:20s} {DIM}{v}{RESET}")
            if not required:
                click.echo(f"    {DIM}(none){RESET}")
            click.echo(f"\n  {BOLD}Optional:{RESET}")
            for k, v in optional.items():
                click.echo(f"    {k:20s} {DIM}{v}{RESET}")
            if not optional:
                click.echo(f"    {DIM}(none){RESET}")
            click.echo()
    else:
        by_protocol = engine.get_actions_by_protocol()

        if json_output:
            click.echo(json.dumps({
                "by_protocol": by_protocol,
                "count": sum(len(v) for v in by_protocol.values()),
            }, indent=2))
        else:
            total = sum(len(v) for v in by_protocol.values())
            click.echo(f"\n  {BOLD}Supported Actions ({total}){RESET}")
            click.echo(f"  {DIM}{'─' * 45}{RESET}")
            for protocol, acts in sorted(by_protocol.items()):
                click.echo(f"\n  {CYAN}{protocol}{RESET}")
                for a in acts:
                    click.echo(f"    • {a['action']:35s} {DIM}{a['description']}{RESET}")
            click.echo()


# Build command

@main.command()
@click.option("--action", "-a", "action_name", required=True, help="Action name (e.g. aave_supply).")
@click.option("--args", "-A", "args_json", default=None, help="JSON arguments (e.g. '{\"asset\":\"USDC\",\"amount\":\"500\"}').")
@click.option("--json", "-j", "json_output", is_flag=True, help="Output raw JSON.")
@click.option("--wallet", "-w", default=None, help="Override wallet address.")
@click.option("--chain", "-c", "chain_id", type=int, default=1,
              help="Chain ID (1=mainnet, 11155111=sepolia)")
def build(action_name, args_json, json_output, wallet, chain_id):
    """Build an unsigned Ethereum transaction (deterministic, no LLM).

    \b
    Examples:
      defi-skills build --action aave_supply --args '{"asset":"USDC","amount":"500"}'
      defi-skills build --action transfer_native --args '{"to":"vitalik.eth","amount":"0.5"}' --json
    """

    wallet_addr = wallet or cfg.get_value("wallet_address")
    if not wallet_addr:
        click.echo(f"{RED}Error: wallet address not set. Run: defi-skills config setup{RESET}", err=True)
        raise SystemExit(1)

    if args_json:
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as e:
            click.echo(f"{RED}Error: invalid JSON in --args: {e}{RESET}", err=True)
            raise SystemExit(1)
    else:
        arguments = {}

    if not json_output:
        click.echo(f"  {DIM}Loading engine...{RESET}", err=True)

    engine, _, _ = init_engine()
    result = build_tx(engine, action_name, arguments, wallet_addr, chain_id)

    if json_output:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        print_tx_result(result)

    if not result.get("success"):
        raise SystemExit(1)


# Chat command

@main.command()
@click.option("--model", "-m", default=None, help="LLM model (e.g. claude-sonnet-4-6, gpt-5.4).")
@click.option("--wallet", "-w", default=None, help="Override wallet address.")
@click.option("--stream/--no-stream", default=None, help="Stream text live (raw) or wait for formatted output.")
@click.option("--chain", "-c", "chain_id", type=int, default=1,
              help="Chain ID (1=mainnet, 11155111=sepolia)")
def chat(model, wallet, stream, chain_id):
    """Interactive DeFi transaction assistant (uses LLM with tool calling).

    \b
    Example:
      defi-skills chat
      defi-skills chat --model gpt-5.1
      defi-skills chat --no-stream
    """

    wallet_addr = wallet or cfg.get_value("wallet_address")
    if not wallet_addr:
        click.echo(f"{RED}Error: wallet address not set. Run: defi-skills config setup{RESET}", err=True)
        raise SystemExit(1)

    model_name = model or cfg.get_value("model") or "claude-sonnet-4-6"

    if stream is None:
        stream_cfg = cfg.get_value("stream_responses")
        stream = stream_cfg.lower() == "true" if stream_cfg else False

    click.echo(f"  {DIM}Loading engine...{RESET}", err=True)
    engine, _, _ = init_engine()

    from defi_skills.cli.chat import run_chat
    run_chat(engine, wallet_addr, chain_id, model_name, stream=stream)

@main.command()
@click.option("--action", "-a", "action_name", default=None, help="Action name (e.g. aave_supply).")
@click.option("--args", "-A", "args_json", default=None, help="JSON arguments.")
@click.option("--multi-step", "-m", "multi_step_json", default=None,
              help='JSON array of steps: \'[{"action":"...","args":{...}}, ...]\'')
@click.option("--json", "-j", "json_output", is_flag=True, help="Output raw JSON.")
@click.option("--wallet", "-w", default=None, help="Override wallet address.")
@click.option("--chain", "-c", "chain_id", type=int, default=1,
              help="Chain ID (1=mainnet, 11155111=sepolia)")
def simulate(action_name, args_json, multi_step_json, json_output, wallet, chain_id):
    """Build and simulate transactions on a local Anvil fork.

    \b
    Single action:
      defi-skills simulate --action aave_supply --args '{"asset":"USDC","amount":"500"}'

    \b
    Multi-step (all steps share one fork, state carries forward):
      defi-skills simulate --multi-step '[
        {"action":"lido_stake","args":{"amount":"10"}},
        {"action":"eigenlayer_deposit","args":{"asset":"stETH","amount":"10"}}
      ]'
    """
    from defi_skills.cli.simulate import run_simulation

    wallet_addr = wallet or cfg.get_value("wallet_address")
    if not wallet_addr:
        click.echo(f"{RED}Error: wallet address not set. Run: defi-skills config setup{RESET}", err=True)
        raise SystemExit(1)

    # Validate: exactly one of --action or --multi-step
    if action_name and multi_step_json:
        click.echo(f"{RED}Error: use either --action or --multi-step, not both.{RESET}", err=True)
        raise SystemExit(1)
    if not action_name and not multi_step_json:
        click.echo(f"{RED}Error: provide either --action or --multi-step.{RESET}", err=True)
        raise SystemExit(1)

    if not json_output:
        click.echo(f"  {DIM}Loading engine...{RESET}", err=True)

    engine, _, _ = init_engine()

    if action_name:
        # Single action mode (existing behavior)
        try:
            arguments = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError as e:
            click.echo(f"{RED}Error: invalid JSON in --args: {e}{RESET}", err=True)
            raise SystemExit(1)

        if not json_output:
            click.echo(f"  {DIM}Building transaction...{RESET}", err=True)

        build_result = build_tx(engine, action_name, arguments, wallet_addr, chain_id)

        if not build_result.get("success"):
            if json_output:
                click.echo(json.dumps(build_result, indent=2, default=str))
            else:
                print_tx_result(build_result)
            raise SystemExit(1)

    else:
        # Multi-step mode
        try:
            steps = json.loads(multi_step_json)
        except json.JSONDecodeError as e:
            click.echo(f"{RED}Error: invalid JSON in --multi-step: {e}{RESET}", err=True)
            raise SystemExit(1)

        if not isinstance(steps, list) or not steps:
            click.echo(f"{RED}Error: --multi-step must be a non-empty JSON array.{RESET}", err=True)
            raise SystemExit(1)

        all_transactions = []
        for i, step in enumerate(steps):
            step_action = step.get("action")
            step_args = step.get("args", {})
            if not step_action:
                click.echo(f"{RED}Error: step {i + 1} missing 'action' field.{RESET}", err=True)
                raise SystemExit(1)

            if not json_output:
                click.echo(f"  {DIM}Building step {i + 1}/{len(steps)}: {step_action}...{RESET}", err=True)

            step_result = build_tx(engine, step_action, step_args, wallet_addr, chain_id)
            if not step_result.get("success"):
                err = step_result.get("error", "unknown error")
                if json_output:
                    click.echo(json.dumps({"success": False, "error": f"Step {i + 1} ({step_action}): {err}"}, indent=2))
                else:
                    click.echo(f"\n  {RED}Step {i + 1} ({step_action}) failed: {err}{RESET}", err=True)
                raise SystemExit(1)

            # Tag each transaction with its plan step
            for tx in step_result.get("transactions", []):
                tx["plan_step"] = i + 1
                tx["plan_action"] = step_action
                all_transactions.append(tx)

        build_result = {"success": True, "transactions": all_transactions}

    if not json_output:
        click.echo(f"  {DIM}Simulating on fork...{RESET}", err=True)

    sim_result = run_simulation(build_result, wallet_addr)

    if json_output:
        click.echo(json.dumps(sim_result, indent=2, default=str))
    else:
        print_sim_result(sim_result)

    if not sim_result.get("success"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
