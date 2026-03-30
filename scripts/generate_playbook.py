"""Auto-generate playbook JSON from a protocol's contract ABI using LLM classification."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from eth_utils import keccak
from litellm import completion

from defi_skills.data.fetch_abis import fetch_and_cache

load_dotenv()

PLAYBOOKS_DIR = Path(__file__).resolve().parent.parent / "src" / "defi_skills" / "data" / "playbooks"


# Constants

ADMIN_FUNCTION_NAMES = {
    "initialize", "upgradeTo", "upgradeToAndCall", "renounceOwnership",
    "transferOwnership", "pause", "unpause", "setAdmin", "setGovernor",
    "setGuardian", "setConfiguration", "setReserve", "changeAdmin",
    "setPendingAdmin", "acceptAdmin", "rescueTokens", "setFeeCollector",
    "grantRole", "revokeRole", "setRoleAdmin", "setFlashLoanPremium",
    "setPoolPause", "setReservePause", "setReserveFactor",
    "initReserve", "dropReserve", "updateBridgeProtocolFee",
}

SOLIDITY_TO_COERCE = {
    "address": "address",
    "uint256": "uint256",
    "uint128": "uint256",
    "uint96": "uint256",
    "uint64": "uint256",
    "uint48": "uint256",
    "uint32": "uint256",
    "uint24": "uint24",
    "uint16": "uint256",
    "uint8": "uint256",
    "int256": "uint256",
    "int128": "uint256",
    "bool": "bool",
    "bytes32": "bytes32",
    "bytes4": "bytes32",
    "bytes": "bytes",
    "string": "bytes",
    "uint256[]": "uint256_array",
    "int256[]": "int_array",
    "address[]": "address_array",
}

# Functions that support "max"/"all" amounts — most use resolve_amount_or_balance
# (query actual balanceOf), only Aave/Compound withdraw/repay use resolve_amount_or_max
# (UINT256_MAX sentinel for interest-accruing positions).
MAX_AMOUNT_KEYWORDS = {"withdraw", "redeem", "repay", "remove", "exit", "unstake", "unwrap", "burn"}


# ABI helpers

def load_contract_abis(
    contracts: Dict[str, str],
    extra_facets: Optional[List[str]] = None,
) -> Dict[str, Tuple[str, List[Dict]]]:
    """Load ABIs for all contracts. Returns {label: (address, abi)}."""
    result = {}
    for label, address in contracts.items():
        abi = fetch_and_cache(label, address, extra_facets=extra_facets)
        if abi is None:
            print(f"  WARNING: Could not fetch ABI for {label} ({address})")
            continue
        result[label] = (address, abi)
    return result


def filter_write_functions(abi: List[Dict]) -> List[Dict]:
    """Return user-facing write functions from ABI."""
    result = []
    for entry in abi:
        if entry.get("type") != "function":
            continue
        mutability = entry.get("stateMutability", "nonpayable")
        if mutability in ("view", "pure"):
            continue
        name = entry.get("name", "")
        if name.startswith("_"):
            continue
        if name in ADMIN_FUNCTION_NAMES:
            continue
        result.append(entry)
    return result


def format_function_sig(func: Dict) -> str:
    """Format ABI function entry as human-readable signature."""
    name = func.get("name", "?")
    inputs = func.get("inputs", [])
    params = ", ".join(f"{i['type']} {i.get('name', '?')}" for i in inputs)
    mut = func.get("stateMutability", "nonpayable")
    return f"{name}({params}) [{mut}]"


def canonical_type(inp: Dict) -> str:
    """Build canonical Solidity type string, recursing into nested tuples."""
    base = inp["type"]
    if base == "tuple" and inp.get("components"):
        inner = ",".join(canonical_type(c) for c in inp["components"])
        return f"({inner})"
    if base == "tuple[]" and inp.get("components"):
        inner = ",".join(canonical_type(c) for c in inp["components"])
        return f"({inner})[]"
    return base


def compute_function_selector(func_entry: Dict) -> str:
    """Compute the 4-byte function selector from an ABI entry."""
    name = func_entry["name"]
    input_types = [canonical_type(inp) for inp in func_entry.get("inputs", [])]
    sig = f"{name}({','.join(input_types)})"
    return "0x" + keccak(sig.encode()).hex()[:8]


def solidity_type_to_coerce(sol_type: str) -> str:
    """Map Solidity type to playbook coerce type."""
    if sol_type in SOLIDITY_TO_COERCE:
        return SOLIDITY_TO_COERCE[sol_type]
    if sol_type.startswith("uint"):
        return "uint256"
    if sol_type.startswith("int"):
        return "uint256"
    if sol_type == "tuple":
        return "struct"
    return "uint256"


# Interactive function selection

def select_functions_interactive(
    functions_by_contract: Dict[str, List[Dict]],
) -> Dict[str, List[Dict]]:
    """Display write functions and let user select which to include."""
    all_funcs = []
    for label, funcs in functions_by_contract.items():
        for f in funcs:
            all_funcs.append((label, f))

    if not all_funcs:
        print("No write functions found in the provided ABIs.")
        return {}

    print(f"\nFound {len(all_funcs)} write functions:\n")
    for i, (label, f) in enumerate(all_funcs, 1):
        print(f"  {i:3d}. [{label}] {format_function_sig(f)}")

    print(f"\nEnter numbers to include (comma-separated), 'all', or 'q' to quit:")
    choice = input("> ").strip()

    if choice.lower() == "q":
        sys.exit(0)
    if choice.lower() == "all":
        selected_indices = list(range(len(all_funcs)))
    else:
        try:
            selected_indices = [int(x.strip()) - 1 for x in choice.split(",")]
        except ValueError:
            print("Invalid input.")
            sys.exit(1)

    result: Dict[str, List[Dict]] = {}
    for idx in selected_indices:
        if 0 <= idx < len(all_funcs):
            label, func = all_funcs[idx]
            result.setdefault(label, []).append(func)
    return result


# LLM parameter classification

def build_classification_prompt(
    protocol: str,
    function_entry: Dict,
    contract_label: str,
) -> str:
    """Build LLM prompt for classifying function parameters."""
    func_name = function_entry["name"]
    is_payable = function_entry.get("stateMutability") == "payable"
    inputs = function_entry.get("inputs", [])

    params_lines = []
    for inp in inputs:
        line = f"  - {inp.get('name', '?')}: {inp['type']}"
        if inp["type"] == "tuple" and inp.get("components"):
            comps = [f"{c.get('name', '?')}:{c['type']}" for c in inp["components"]]
            line += f" (components: [{', '.join(comps)}])"
        params_lines.append(line)
    params_str = "\n".join(params_lines) if params_lines else "  (none)"

    sig = ", ".join(f"{i['type']} {i.get('name', '?')}" for i in inputs)

    return f"""You are an expert Ethereum smart contract analyst. Classify each parameter of this DeFi function for an automated transaction builder.

PROTOCOL: {protocol}
CONTRACT: {contract_label}
FUNCTION: {func_name}({sig})
PAYABLE: {is_payable}

PARAMETERS:
{params_str}

For each parameter, classify its SEMANTIC ROLE from these categories:

1. "token_address" - ERC-20 token contract address (the asset being supplied/withdrawn/swapped).
   resolver: resolve_token_address, llm_field: "asset" (or "asset_in"/"asset_out" for swaps)
2. "amount" - Token amount needing decimal conversion.
   Three "max" behaviors — set extra_config.max_behavior:
     - "balance" (DEFAULT for most actions): user says "max" -> queries actual balanceOf on-chain.
       Used for: supply, swap, stake, wrap, unwrap, transfer, remove liquidity, deposit, redeem.
     - "sentinel" (ONLY for Aave/Compound withdraw/repay): "max" -> UINT256_MAX sentinel.
       Used when the protocol treats MAX as "close entire position including accrued interest".
     - "none" (for borrow): "max" makes no sense, raises error.
   Also set extra_config.balance_of to identify which token to query for "balance" mode:
     - "$<llm_field>" references another parameter BY ITS llm_field NAME (NOT the Solidity name).
       E.g. if you classified loanToken as token_address with llm_field "asset", use "$asset" not "$loanToken".
     - literal "0x..." address for known tokens
     - "$native" for ETH balance
   decimals_from: same rule — use "$<llm_field>" of the token parameter (e.g. "$asset"), NOT the Solidity name.
3. "sender_address" - Transaction sender's address (msg.sender / onBehalfOf / owner).
   resolver: resolve_ens_or_hex, context_field: "from_address"
4. "recipient_address" - Recipient/destination address that could differ from sender.
   resolver: resolve_ens_or_hex, llm_field: "to"
5. "constant" - Fixed default value (referralCode=0, interestRateMode=2, etc.).
   Specify the default value.
6. "deadline" - Timestamp deadline parameter.
   resolver: resolve_deadline
7. "fee_tier" - DEX fee tier (Uniswap-style).
   resolver: resolve_fee_tier
8. "passthrough" - Value passed from LLM as-is.
   resolver: llm_passthrough
9. "amount_native" - ETH amount for payable functions (goes to msg.value, not calldata).
   resolver: resolve_amount_or_balance, decimals_from: "$native", balance_of: "$native"

For struct/tuple parameters, classify EACH component field individually.

RULES:
- If payable with no explicit amount input, ETH amount comes from msg.value (not a parameter)
- "referralCode", "_referral" are always constants (usually 0)
- "onBehalfOf", "_owner", "owner" are typically sender_address
- "to", "receiver", "recipient" are typically recipient_address (default to from_address)
- uint256 tied to a token needs decimal conversion (amount)
- If ambiguous, use "passthrough"
- For max_behavior: use "sentinel" ONLY for protocols that explicitly treat UINT256_MAX as
  "all including accrued interest" (Aave withdraw/repay, Compound withdraw/repay). Everything
  else should use "balance" (queries actual on-chain balance).

Also provide:
- "action_name": suggested name as "{protocol}_<verb>" (e.g. "{protocol}_supply")
- "description": one-line description
- "human_readable_template": template string using LLM field names in curly braces (e.g. "{{amount}} {{asset}}")
- "approvals": list of ERC-20 approval requirements. Each entry:
    {{"token": "$field_name_or_0x_address", "spender": "target_contract"}}
  where $field_name references a token_address parameter (e.g. "$asset", "$asset_in").
  Include an approval if the function transfers ERC-20 tokens FROM the caller (supply, swap, stake, wrap, add liquidity, repay).
  Do NOT include approvals for native ETH, or functions that only send ETH or return tokens TO the caller.

OUTPUT (strict JSON, no markdown fences):
{{
  "action_name": "...",
  "description": "...",
  "function_name": "{func_name}",
  "human_readable_template": "...",
  "is_payable": {str(is_payable).lower()},
  "approvals": [],
  "parameters": [
    {{
      "name": "paramName",
      "solidity_type": "address",
      "role": "token_address",
      "llm_field": "asset",
      "extra_config": {{}}
    }},
    ...
  ]
}}

For struct parameters:
{{
  "name": "params",
  "solidity_type": "tuple",
  "role": "struct",
  "fields": [
    {{ "name": "tokenIn", "solidity_type": "address", "role": "token_address", "llm_field": "asset_in", "extra_config": {{}} }},
    ...
  ]
}}

Return ONLY valid JSON."""


def classify_function_params(
    protocol: str,
    function_entry: Dict,
    contract_label: str,
    model: str = "claude-opus-4-6",
) -> Optional[Dict]:
    """Call LLM to classify function parameters. Returns parsed JSON."""
    prompt = build_classification_prompt(protocol, function_entry, contract_label)

    for attempt in range(3):
        try:
            response = completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            text = response.choices[0].message.content.strip()
            # Strip markdown fences
            if text.startswith("```json"):
                text = text[7:]
            elif text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            result = json.loads(text.strip())
            result["function_name"] = function_entry["name"]
            return result
        except (json.JSONDecodeError, Exception) as e:
            print(f"    Attempt {attempt + 1} failed: {e}")
            time.sleep(2)

    print(f"    ERROR: Could not classify {function_entry['name']} after 3 attempts")
    return None


# Playbook assembly

def build_playbook(
    protocol: str,
    chain_id: int,
    contracts: Dict[str, str],
    classifications: List[Tuple[str, Dict, Dict]],  # (contract_label, classification, abi_entry)
    model: str,
) -> Dict:
    """Assemble the playbook JSON from classified parameters."""
    playbook: Dict[str, Any] = {
        "_review_notes": {
            "generated_by": "generate_playbook.py",
            "model": model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "review_items": [],
        },
        "protocol": protocol,
        "version": "1.0",
        "chain_id": chain_id,
        "contracts": {},
        "actions": {},
    }

    for label, address in contracts.items():
        playbook["contracts"][label] = {
            "address": address,
            "abi_source": "etherscan_cache",
        }

    review_items = []

    for contract_label, classification, abi_entry in classifications:
        action_name = classification["action_name"]
        func_name = classification["function_name"]
        is_payable = classification.get("is_payable", False)
        params = classification.get("parameters", [])
        template = classification.get("human_readable_template", "{amount}")
        llm_approvals = classification.get("approvals", [])

        # Compute function selector from ABI for unambiguous matching
        selector = compute_function_selector(abi_entry) if abi_entry else None

        action: Dict[str, Any] = {
            "description": classification.get("description", f"{func_name} on {protocol}"),
            "target_contract": contract_label,
            "function_name": func_name,
            "function_selector": selector,
            "abi_source": "etherscan_cache",
            "value_logic": {"type": "zero"},
            "required_payload_args": [],
            "payload_args": {},
            "param_mapping": [],
        }

        # Add approvals if the LLM identified any
        if llm_approvals:
            action["approvals"] = llm_approvals

        # Determine value_logic
        if is_payable:
            action["value_logic"] = {"type": "from_arg", "source_arg": "value"}
            # Payable functions: use resolve_amount_or_balance with $native
            # so user can say "max" to stake/wrap their full ETH balance
            action["payload_args"]["value"] = {
                "source": "resolve_amount_or_balance",
                "llm_field": "amount",
                "fallback_llm_fields": ["amount", "value"],
                "decimals_from": "$native",
                "balance_of": "$native",
            }
            action["required_payload_args"].append("value")

        # Process each parameter
        for param in params:
            process_param(param, action, func_name, review_items)

        # Always add human_readable_amount
        action["payload_args"]["human_readable_amount"] = {
            "source": "compute_human_readable",
            "template": template,
        }
        action["required_payload_args"].append("human_readable_amount")

        playbook["actions"][action_name] = action

    playbook["_review_notes"]["review_items"] = review_items
    return playbook


def process_param(
    param: Dict,
    action: Dict,
    func_name: str,
    review_items: List[str],
) -> None:
    """Process a single classified parameter into payload_args + param_mapping."""
    role = param.get("role", "passthrough")
    name = param.get("name", "unknown")
    sol_type = param.get("solidity_type", "uint256")
    extra = param.get("extra_config", {})

    # Handle struct
    if role == "struct":
        struct_entry: Dict[str, Any] = {"name": name, "source": "struct", "fields": []}
        for field in param.get("fields", []):
            process_struct_field(field, action, func_name, struct_entry["fields"], review_items)
        action["param_mapping"].append(struct_entry)
        return

    # Build payload_arg
    arg_key = build_payload_arg(param, action, func_name, review_items)

    # Build param_mapping entry (skip amount_native — goes to value_logic)
    if role == "amount_native":
        return

    coerce = solidity_type_to_coerce(sol_type)

    if role == "constant":
        const_val = extra.get("value", param.get("value", 0))
        action["param_mapping"].append({
            "name": name,
            "source": "constant",
            "value": const_val,
            "coerce": coerce,
        })
    elif arg_key:
        action["param_mapping"].append({
            "name": name,
            "source": "arg",
            "arg_key": arg_key,
            "coerce": coerce,
        })


def process_struct_field(
    field: Dict,
    action: Dict,
    func_name: str,
    struct_fields: List[Dict],
    review_items: List[str],
) -> None:
    """Process a single struct component field."""
    role = field.get("role", "passthrough")
    name = field.get("name", "unknown")
    sol_type = field.get("solidity_type", "uint256")
    extra = field.get("extra_config", {})
    coerce = solidity_type_to_coerce(sol_type)

    # Add to payload_args
    arg_key = build_payload_arg(field, action, func_name, review_items)

    # Add to struct fields
    if role == "constant":
        const_val = extra.get("value", field.get("value", 0))
        struct_fields.append({
            "name": name,
            "source": "constant",
            "value": const_val,
            "coerce": coerce,
        })
    elif arg_key:
        struct_fields.append({
            "name": name,
            "source": "arg",
            "arg_key": arg_key,
            "coerce": coerce,
        })


def build_payload_arg(
    param: Dict,
    action: Dict,
    func_name: str,
    review_items: List[str],
) -> Optional[str]:
    """Build a payload_args entry from a classified parameter. Returns the arg_key."""
    role = param.get("role", "passthrough")
    llm_field = param.get("llm_field", param.get("name", "unknown"))
    extra = param.get("extra_config", {})

    # Determine if this is a max-capable amount
    supports_max = any(kw in func_name.lower() for kw in MAX_AMOUNT_KEYWORDS)

    if role == "token_address":
        arg_key = llm_field  # e.g. "asset", "asset_in", "asset_out"
        action["payload_args"][arg_key] = {
            "source": "resolve_token_address",
            "llm_field": llm_field,
        }
        if extra.get("eth_alias"):
            action["payload_args"][arg_key]["eth_alias"] = extra["eth_alias"]
        action["required_payload_args"].append(arg_key)
        return arg_key

    elif role == "amount":
        arg_key = extra.get("arg_key", "amount")
        decimals_from = str(extra.get("decimals_from", "$asset"))
        balance_of = str(extra.get("balance_of", "$asset"))

        # Three-tier "max" handling:
        # - "sentinel": UINT256_MAX (only Aave/Compound withdraw/repay)
        # - "balance": query actual balanceOf on-chain (most actions)
        # - "none": raises error (borrow)
        max_behavior = extra.get("max_behavior", "balance" if supports_max else "none")

        if max_behavior == "sentinel":
            resolver = "resolve_amount_or_max"
        elif max_behavior == "balance":
            resolver = "resolve_amount_or_balance"
        else:
            resolver = "resolve_amount"

        entry: Dict[str, Any] = {
            "source": resolver,
            "llm_field": "amount",
            "fallback_llm_fields": ["amount"],
            "decimals_from": decimals_from,
        }
        # Add balance_of for resolve_amount_or_balance
        if resolver == "resolve_amount_or_balance":
            entry["balance_of"] = balance_of

        action["payload_args"][arg_key] = entry
        action["required_payload_args"].append(arg_key)
        if decimals_from.startswith("$"):
            ref = decimals_from[1:]
            if ref not in ("native", "asset") and ref not in action["payload_args"]:
                review_items.append(
                    f"Action {func_name}: decimals_from '{decimals_from}' — verify token ref exists"
                )
        return arg_key

    elif role == "amount_native":
        # Handled by value_logic; add payload_arg if not already present
        if "value" not in action["payload_args"]:
            action["payload_args"]["value"] = {
                "source": "resolve_amount_or_balance",
                "llm_field": "amount",
                "fallback_llm_fields": ["amount", "value"],
                "decimals_from": "$native",
                "balance_of": "$native",
            }
            if "value" not in action["required_payload_args"]:
                action["required_payload_args"].append("value")
        return "value"

    elif role == "sender_address":
        arg_key = llm_field  # e.g. "onBehalfOf", "owner"
        action["payload_args"][arg_key] = {
            "source": "resolve_ens_or_hex",
            "llm_field": llm_field,
            "context_field": "from_address",
        }
        action["required_payload_args"].append(arg_key)
        return arg_key

    elif role == "recipient_address":
        arg_key = llm_field  # e.g. "to", "receiver"
        action["payload_args"][arg_key] = {
            "source": "resolve_ens_or_hex",
            "llm_field": llm_field,
            "context_field": "from_address",
        }
        action["required_payload_args"].append(arg_key)
        return arg_key

    elif role == "constant":
        arg_key = param.get("name", "unknown")
        const_val = extra.get("value", param.get("value", 0))
        action["payload_args"][arg_key] = {
            "source": "constant",
            "value": const_val,
        }
        # Don't add constants to required_payload_args
        return arg_key

    elif role == "deadline":
        arg_key = "deadline"
        action["payload_args"][arg_key] = {
            "source": "resolve_deadline",
            "buffer_seconds": extra.get("buffer_seconds", 1200),
        }
        action["required_payload_args"].append(arg_key)
        return arg_key

    elif role == "fee_tier":
        arg_key = "fee"
        action["payload_args"][arg_key] = {
            "source": "resolve_fee_tier",
            "llm_field": "fee",
        }
        action["required_payload_args"].append(arg_key)
        return arg_key

    elif role == "passthrough":
        arg_key = llm_field
        action["payload_args"][arg_key] = {
            "source": "llm_passthrough",
            "llm_field": llm_field,
        }
        action["required_payload_args"].append(arg_key)
        review_items.append(
            f"Action {func_name}: param '{param.get('name')}' classified as passthrough — review"
        )
        return arg_key

    return None


# Validation

def validate_playbook(playbook: Dict) -> bool:
    """Validate generated playbook against cached ABIs."""
    ok = True
    contracts = playbook.get("contracts", {})

    for action_name, action_spec in playbook.get("actions", {}).items():
        func_name = action_spec.get("function_name")
        target_key = action_spec.get("target_contract")

        if not func_name or not target_key:
            continue

        contract_info = contracts.get(target_key, {})
        address = contract_info.get("address", "")
        if not address:
            print(f"  WARNING: No address for contract '{target_key}' in action {action_name}")
            ok = False
            continue

        # Try loading ABI from cache
        from defi_skills.engine.tx_encoder import load_contract_abi, find_function_in_abi
        abi = load_contract_abi(address)
        if not abi:
            print(f"  WARNING: No cached ABI for {address} — run fetch_abis.py")
            continue

        func_entry = find_function_in_abi(abi, func_name)
        if func_entry:
            types = [i["type"] for i in func_entry.get("inputs", [])]
            sig = f"{func_name}({','.join(types)})"
            print(f"  OK  {action_name:30s} -> {sig}")
        else:
            print(f"  ERR {action_name:30s} -> '{func_name}' not found in ABI")
            ok = False

    return ok


# Main

def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate playbook JSON from contract ABIs",
    )
    parser.add_argument("--protocol", required=True, help="Protocol name (snake_case)")
    parser.add_argument(
        "--contracts", nargs="+", required=True,
        help="Contract(s) as label=address pairs (e.g. pool=0x...)",
    )
    parser.add_argument(
        "--functions", default=None,
        help="Comma-separated function names to include (default: all write functions)",
    )
    parser.add_argument("--chain-id", type=int, default=1, help="Chain ID (default: 1)")
    parser.add_argument("--model", default="claude-opus-4-6", help="LLM model via litellm (default: claude-opus-4-6)")
    parser.add_argument(
        "--output", default=None,
        help="Output path (default: data/playbooks/{protocol}.json)",
    )
    parser.add_argument("--interactive", action="store_true", help="Interactively select functions")
    parser.add_argument(
        "--facets", nargs="+", default=None,
        help="Extra facet addresses for multi-facet/Diamond proxies (e.g. 0x... 0x...)",
    )
    args = parser.parse_args()

    # Parse contracts
    contracts: Dict[str, str] = {}
    for c in args.contracts:
        if "=" not in c:
            print(f"ERROR: Contract must be label=address, got: {c}")
            sys.exit(1)
        label, address = c.split("=", 1)
        contracts[label.strip()] = address.strip()

    output_path = Path(args.output) if args.output else PLAYBOOKS_DIR / f"{args.protocol}.json"
    func_filter = set(args.functions.split(",")) if args.functions else None

    print(f"Generating playbook for: {args.protocol}")
    print(f"Contracts: {contracts}")
    print(f"Model: {args.model}")
    print()

    # Step 1: Fetch ABIs
    print("Step 1: Fetching ABIs...")
    abi_map = load_contract_abis(contracts, extra_facets=args.facets)
    if not abi_map:
        print("ERROR: No ABIs could be loaded.")
        sys.exit(1)

    # Step 2: Filter functions
    print("\nStep 2: Filtering write functions...")
    functions_by_contract: Dict[str, List[Dict]] = {}
    for label, (address, abi) in abi_map.items():
        write_funcs = filter_write_functions(abi)
        if func_filter:
            write_funcs = [f for f in write_funcs if f["name"] in func_filter]
        functions_by_contract[label] = write_funcs
        print(f"  [{label}] {len(write_funcs)} write functions")

    if args.interactive:
        functions_by_contract = select_functions_interactive(functions_by_contract)

    total = sum(len(v) for v in functions_by_contract.values())
    if total == 0:
        print("No functions selected.")
        sys.exit(1)
    print(f"\n  Total functions to process: {total}")

    # Step 3: Classify parameters via LLM
    print(f"\nStep 3: Classifying parameters via {args.model}...")
    classifications: List[Tuple[str, Dict, Dict]] = []
    for label, funcs in functions_by_contract.items():
        for func in funcs:
            sig = format_function_sig(func)
            selector = compute_function_selector(func)
            print(f"  Classifying [{label}] {sig} ({selector})...")
            result = classify_function_params(
                args.protocol, func, label, model=args.model,
            )
            if result:
                classifications.append((label, result, func))
                print(f"    -> {result['action_name']}")
            else:
                print(f"    -> FAILED, skipping")

    if not classifications:
        print("ERROR: No functions could be classified.")
        sys.exit(1)

    # Step 4: Build playbook
    print(f"\nStep 4: Assembling playbook...")
    playbook = build_playbook(
        args.protocol, args.chain_id, contracts, classifications, args.model,
    )

    actions_count = len(playbook["actions"])
    print(f"  Generated {actions_count} actions")

    # Step 5: Validate
    print(f"\nStep 5: Validating...")
    valid = validate_playbook(playbook)

    # Step 6: Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(playbook, indent=2, ensure_ascii=False))
    print(f"\nPlaybook written to: {output_path}")

    if playbook.get("_review_notes", {}).get("review_items"):
        print("\nReview items:")
        for item in playbook["_review_notes"]["review_items"]:
            print(f"  - {item}")

    if not valid:
        print("\nWARNING: Some validation checks failed — review the output.")

    print("\nDone. Review the generated playbook and remove _review_notes before committing.")


if __name__ == "__main__":
    main()
