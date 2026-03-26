"""Playbook engine — drives all protocol actions from JSON playbook files."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from eth_utils import to_checksum_address

from defi_skills.engine.chains import get_approve_reset_tokens
from defi_skills.engine.resolvers import (
    RESOLVER_REGISTRY,
    ResolveContext,
)
from defi_skills.engine.resolvers.common import sanitize_error
from defi_skills.engine.tx_encoder import (
    encode_from_abi,
    load_contract_abi,
    find_function_in_abi,
    normalize_address,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAYBOOKS_DIR = DATA_DIR / "playbooks"

UINT256_MAX = 2**256 - 1

ERC20_APPROVE_ABI = {
    "name": "approve",
    "type": "function",
    "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
}
REGISTRY_DIR = DATA_DIR / "registry"


class PlaybookEngine:
    """Generic engine driven by JSON playbook files."""

    def __init__(
        self,
        token_resolver=None,
        ens_resolver=None,
        playbooks_dir: Optional[str] = None,
    ):
        self.token_resolver = token_resolver
        self.ens_resolver = ens_resolver
        self.playbooks: Dict[int, Dict[str, Dict]] = {}      # chain_id -> action_name -> action_spec
        self.playbook_meta: Dict[int, Dict[str, Dict]] = {}   # chain_id -> action_name -> full playbook
        self.standard_abis: Dict[str, Dict] = {} # key -> ABI entry (from playbooks)
        self.registry: Dict[str, Dict] = {}       # protocol_name -> registry data
        self.load_playbooks(Path(playbooks_dir) if playbooks_dir else PLAYBOOKS_DIR)
        self.load_registry()

    def load_playbooks(self, playbooks_dir: Path) -> None:
        """Load all .json playbook files from root and chain subdirs."""
        json_files = sorted(playbooks_dir.glob("*.json"))
        for subdir in sorted(playbooks_dir.iterdir()):
            if subdir.is_dir():
                json_files.extend(sorted(subdir.glob("*.json")))

        for pb_file in json_files:
            pb = json.loads(pb_file.read_text())
            chain_id = pb.get("chain_id", 1)

            if chain_id not in self.playbooks:
                self.playbooks[chain_id] = {}
                self.playbook_meta[chain_id] = {}

            for action_name, action_spec in pb.get("actions", {}).items():
                self.playbooks[chain_id][action_name] = action_spec
                self.playbook_meta[chain_id][action_name] = pb

            for key, abi_entry in pb.get("standard_abis", {}).items():
                self.standard_abis[key] = abi_entry

    def load_registry(self) -> None:
        """Load registry files and merge valid_tokens into action specs."""
        if not REGISTRY_DIR.exists():
            return
        for reg_file in sorted(REGISTRY_DIR.glob("*.json")):
            try:
                reg = json.loads(reg_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            protocol = reg_file.stem
            self.registry[protocol] = reg
            self.apply_registry(protocol, reg)

    def apply_registry(self, protocol: str, reg: Dict) -> None:
        """Merge registry data (valid_tokens, strategy_map) into matching action specs."""
        valid_tokens = reg.get("valid_tokens")
        strategy_map = reg.get("strategy_map")
        supply_tokens = reg.get("supply_tokens")
        borrow_tokens = reg.get("borrow_tokens")

        for chain_id, actions in self.playbooks.items():
            for action_name, spec in actions.items():
                pb = self.playbook_meta[chain_id].get(action_name, {})
                if pb.get("protocol") != protocol:
                    continue
                if strategy_map is not None:
                    pb["strategy_map"] = strategy_map
                if valid_tokens is not None and spec.get("valid_tokens") is not None:
                    spec["valid_tokens"] = valid_tokens
                if supply_tokens is not None and ("supply" in action_name or "withdraw" in action_name):
                    spec["valid_tokens"] = supply_tokens
                if borrow_tokens is not None and ("borrow" in action_name or "repay" in action_name):
                    spec["valid_tokens"] = borrow_tokens

    # Stage 1: LLM output → ExecutablePayload

    def build_payload(
        self,
        llm_output: Dict[str, Any],
        chain_id: int = 1,
        from_address: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Convert LLM output to ExecutablePayload dict."""
        action = llm_output.get("action")
        chain_actions = self.playbooks.get(chain_id, {})
        if not action or action not in chain_actions:
            return None

        action_spec = chain_actions[action]
        playbook = self.playbook_meta[chain_id][action]

        args = llm_output.get("arguments") or {}

        ctx = ResolveContext(
            token_resolver=self.token_resolver,
            ens_resolver=self.ens_resolver,
            from_address=from_address,
            chain_id=chain_id,
            action=action,
            raw_args=args,
        )

        # Enforce valid_tokens constraint before resolving anything.
        # This catches unsupported tokens early with a clear error instead of
        # producing a transaction that will revert on-chain.
        valid_tokens = action_spec.get("valid_tokens")
        if valid_tokens:
            payload_args_spec_pre = action_spec.get("payload_args", {})
            for arg_name, arg_spec in payload_args_spec_pre.items():
                if arg_spec.get("source") in ("resolve_token_address", "resolve_eigenlayer_strategy"):
                    raw_symbol = self.extract_llm_value(arg_spec, ctx)
                    if raw_symbol and str(raw_symbol).strip():
                        symbol_upper = str(raw_symbol).strip().upper()
                        valid_upper = [t.upper() for t in valid_tokens]
                        if symbol_upper not in valid_upper:
                            raise ValueError(
                                f"Token '{raw_symbol}' is not supported for action '{action}'. "
                                f"Valid tokens: {', '.join(valid_tokens)}"
                            )

        # Resolve all payload_args in declaration order
        payload_args_spec = action_spec.get("payload_args", {})
        for arg_name, arg_spec in payload_args_spec.items():
            try:
                resolved_value = self.resolve_payload_arg(arg_name, arg_spec, ctx, playbook)
            except (ValueError, KeyError) as e:
                sanitized = sanitize_error(str(e))
                logger.error(f"build_payload: resolver failed for '{arg_name}' in '{action}': {sanitized}")
                raise ValueError(f"Failed to resolve '{arg_name}': {sanitized}") from e
            ctx.resolved[arg_name] = resolved_value

        # Validate that all required payload args resolved to non-None values.
        # A None means a resolver failed silently — proceeding would produce a
        # broken transaction (e.g., sending tokens to address(0), encoding amount as 0).
        required = action_spec.get("required_payload_args", [])
        for arg_name in required:
            if arg_name in ctx.resolved and ctx.resolved[arg_name] is None:
                raise ValueError(
                    f"Required argument '{arg_name}' resolved to None for action '{action}'. "
                    f"This usually means a token symbol, address, or amount could not be resolved."
                )

        # Build the output arguments dict (include __ keys — encode_tx needs them for param_mapping)
        arguments = dict(ctx.resolved)

        # Resolve target_contract
        target_contract = self.resolve_target_contract(action_spec, playbook, ctx)

        # Resolve function_name (may be overridden by function_overrides)
        function_name = action_spec.get("function_name")
        if target_contract and "function_overrides" in action_spec:
            override = action_spec["function_overrides"].get(target_contract.lower())
            if override:
                function_name = override.get("function_name", function_name)

        # Resolve approval requirements from playbook declaration
        approvals = self.resolve_approvals(action_spec, playbook, ctx, target_contract)

        return {
            "chain_id": chain_id,
            "action": action,
            "target_contract": target_contract,
            "function_name": function_name,
            "arguments": arguments,
            "approvals": approvals,
        }

    def resolve_payload_arg(
        self,
        arg_name: str,
        arg_spec: Dict[str, Any],
        ctx: ResolveContext,
        playbook: Dict,
    ) -> Any:
        """Resolve a single payload_args entry using the appropriate resolver."""
        source = arg_spec.get("source", "constant")
        resolver_fn = RESOLVER_REGISTRY.get(source)
        if resolver_fn is None:
            return arg_spec.get("value")

        # Extract the raw value from LLM args
        raw_value = self.extract_llm_value(arg_spec, ctx)

        # For resolvers that don't need a raw value (deadline, constant, etc.)
        if source == "constant":
            return arg_spec.get("value")
        if source == "resolve_contract_address":
            return resolver_fn(raw_value, ctx, _playbook_contracts=playbook.get("contracts", {}), **arg_spec)
        if source == "resolve_eigenlayer_strategy":
            sm_addr = playbook.get("contracts", {}).get("strategy_manager", {}).get("address")
            return resolver_fn(raw_value, ctx, strategy_map=playbook.get("strategy_map", {}), strategy_manager_address=sm_addr, **arg_spec)
        if source == "resolve_eigenlayer_deposits":
            return resolver_fn(raw_value, ctx, strategy_map=playbook.get("strategy_map", {}), **arg_spec)
        if source == "resolve_eigenlayer_queued_withdrawals":
            return resolver_fn(raw_value, ctx, strategy_map=playbook.get("strategy_map", {}), **arg_spec)
        if source == "compute_human_readable":
            # Check if LLM provided a human_readable_amount, use it if available
            existing = ctx.raw_args.get("human_readable_amount")
            if existing:
                return existing
            return resolver_fn(raw_value, ctx, **arg_spec)
        if source == "resolve_deadline":
            return resolver_fn(raw_value, ctx, **arg_spec)
        if source == "build_fixed_array":
            return resolver_fn(raw_value, ctx, **arg_spec)

        # Standard resolver: pass raw value + kwargs from spec
        kwargs = {k: v for k, v in arg_spec.items()
                  if k not in ("source", "llm_field", "fallback_llm_fields", "context_field", "fallback_context")}
        result = resolver_fn(raw_value, ctx, **kwargs)

        # Fallback to context if resolver returned None
        if result is None:
            context_field = arg_spec.get("context_field")
            if context_field == "from_address":
                result = ctx.from_address
            elif context_field:
                result = ctx.raw_args.get(context_field) or ctx.from_address

        return result

    def extract_llm_value(self, arg_spec: Dict, ctx: ResolveContext) -> Any:
        """Extract a value from LLM args, trying primary field then fallbacks."""
        primary = arg_spec.get("llm_field")
        if primary:
            # Handle array access: path[0], path[-1]
            if "[" in primary:
                base_key, idx_str = primary.rstrip("]").split("[")
                arr = ctx.raw_args.get(base_key)
                if arr and isinstance(arr, list):
                    try:
                        idx = int(idx_str)
                        if abs(idx) <= len(arr):
                            return str(arr[idx])
                    except (ValueError, IndexError):
                        pass
            else:
                val = ctx.raw_args.get(primary)
                if val is not None:
                    return val

        # Try fallback fields
        for fb in arg_spec.get("fallback_llm_fields", []):
            if "[" in fb:
                base_key, idx_str = fb.rstrip("]").split("[")
                arr = ctx.raw_args.get(base_key)
                if arr and isinstance(arr, list):
                    try:
                        idx = int(idx_str)
                        return str(arr[idx])
                    except (ValueError, IndexError):
                        pass
            else:
                val = ctx.raw_args.get(fb)
                if val is not None:
                    return val

        return None

    def resolve_target_contract(
        self,
        action_spec: Dict,
        playbook: Dict,
        ctx: ResolveContext,
    ) -> Optional[str]:
        """Resolve target_contract from playbook spec."""
        target = action_spec.get("target_contract")
        if not target:
            return None

        # Dynamic sentinels — $<key> resolves from ctx.resolved
        if target.startswith("$"):
            # Legacy specific mappings
            if target == "$recipient":
                return ctx.resolved.get("to")
            if target == "$token_address":
                return ctx.resolved.get("__token_address")
            if target == "$collection_address":
                return ctx.resolved.get("__collection_address")
            # Generic: $gauge_address -> ctx.resolved["gauge_address"]
            return ctx.resolved.get(target[1:])

        # Lookup from contracts map
        contracts = playbook.get("contracts", {})
        contract_info = contracts.get(target)
        if contract_info:
            return contract_info.get("address")

        return None

    def resolve_approvals(
        self,
        action_spec: Dict,
        playbook: Dict,
        ctx: ResolveContext,
        target_contract: Optional[str],
    ) -> List[Dict[str, str]]:
        """Resolve approval declarations into concrete (token, spender) pairs."""
        approval_specs = action_spec.get("approvals", [])
        if not approval_specs:
            return []

        resolved = []
        contracts = playbook.get("contracts", {})

        for spec in approval_specs:
            token = spec.get("token", "")
            spender = spec.get("spender", "")

            # Resolve token address (supports nested keys like $__ordering.token0)
            if token.startswith("$"):
                key = token[1:]
                if "." in key:
                    parent, child = key.split(".", 1)
                    parent_val = ctx.resolved.get(parent)
                    token_addr = parent_val.get(child) if isinstance(parent_val, dict) else None
                else:
                    token_addr = ctx.resolved.get(key)
            else:
                token_addr = token

            # Resolve spender address (supports nested keys)
            if spender == "target_contract":
                spender_addr = target_contract
            elif spender.startswith("$"):
                key = spender[1:]
                if "." in key:
                    parent, child = key.split(".", 1)
                    parent_val = ctx.resolved.get(parent)
                    spender_addr = parent_val.get(child) if isinstance(parent_val, dict) else None
                else:
                    spender_addr = ctx.resolved.get(key)
            else:
                contract_info = contracts.get(spender, {})
                spender_addr = contract_info.get("address", spender)

            if token_addr and spender_addr:
                resolved.append({
                    "token": to_checksum_address(token_addr),
                    "spender": to_checksum_address(spender_addr),
                })

        return resolved

    # Stage 2: ExecutablePayload → raw tx

    def encode_tx(
        self,
        payload: Dict[str, Any],
        from_address: str,
    ) -> Optional[Dict[str, Any]]:
        """Convert ExecutablePayload dict to raw tx: {chain_id, to, value, data}."""
        if not payload:
            return None

        action = payload.get("action")
        chain_id = payload.get("chain_id", 1)
        chain_actions = self.playbooks.get(chain_id, {})
        if not action or action not in chain_actions:
            return None

        action_spec = chain_actions[action]
        args = payload.get("arguments") or {}
        target_contract = payload.get("target_contract")

        # Determine function_name, param_mapping, and ABI (may be overridden)
        function_name = payload.get("function_name")
        param_mapping = action_spec.get("param_mapping", [])
        abi_source = action_spec.get("abi_source", "etherscan_cache")
        standard_abi_key = action_spec.get("standard_abi_key")

        # Check function_overrides (CryptoPunks)
        if target_contract and "function_overrides" in action_spec:
            override = action_spec["function_overrides"].get(target_contract.lower())
            if override:
                function_name = override.get("function_name", function_name)
                param_mapping = override.get("param_mapping", param_mapping)
                standard_abi_key = override.get("standard_abi_key", standard_abi_key)

        # No function = no calldata (native transfer)
        if function_name is None:
            to = target_contract or args.get("to")
            if not to:
                return None
            value = self.resolve_tx_value(action_spec, args)
            return {
                "chain_id": chain_id,
                "to": to_checksum_address(to),
                "value": value,
                "data": "0x",
            }

        # Load ABI entry (use selector from playbook to disambiguate overloaded functions)
        selector = action_spec.get("function_selector")
        abi_entry = self.get_abi_entry(
            action, abi_source, standard_abi_key, function_name,
            selector=selector, chain_id=chain_id,
        )
        if abi_entry is None:
            return None

        # Build values from param_mapping
        values = self.build_abi_values(param_mapping, args, from_address)

        # Encode calldata
        try:
            data = encode_from_abi(abi_entry, values)
        except Exception as e:
            logger.error(f"encode_tx: ABI encoding failed for '{action}' ({function_name}): {sanitize_error(str(e))}")
            return None

        # Target address
        to = target_contract
        if not to:
            return None

        # Transaction value
        value = self.resolve_tx_value(action_spec, args)

        return {
            "chain_id": chain_id,
            "to": to_checksum_address(to),
            "value": value,
            "data": data,
        }

    def get_abi_entry(
        self,
        action: str,
        abi_source: str,
        standard_abi_key: Optional[str],
        function_name: str,
        selector: str = None,
        chain_id: int = 1,
    ) -> Optional[Dict]:
        """Load the ABI entry for encoding directly from playbook data."""
        if abi_source == "standard" and standard_abi_key:
            return self.standard_abis.get(standard_abi_key)
        if abi_source == "etherscan_cache":
            # Resolve contract address from the playbook's contracts section
            action_spec = self.playbooks.get(chain_id, {}).get(action)
            playbook = self.playbook_meta.get(chain_id, {}).get(action)
            if not action_spec or not playbook:
                return None
            target_key = action_spec.get("target_contract")
            if not target_key:
                return None
            contracts = playbook.get("contracts", {})
            contract_info = contracts.get(target_key, {})
            address = contract_info.get("address")
            if not address:
                return None
            abi = load_contract_abi(address)
            if not abi:
                return None
            return find_function_in_abi(abi, function_name, selector=selector)
        return None

    def build_abi_values(
        self,
        param_mapping: List[Dict],
        args: Dict[str, Any],
        from_address: str,
    ) -> List[Any]:
        """Build ordered list of ABI-encoded values from param_mapping."""
        values = []
        for entry in param_mapping:
            source = entry.get("source")
            coerce = entry.get("coerce", "")

            if source == "struct":
                # Recursively build a tuple from nested fields
                struct_values = []
                for field_entry in entry.get("fields", []):
                    field_val = self.resolve_param_entry(field_entry, args, from_address)
                    struct_values.append(field_val)
                values.append(tuple(struct_values))

            elif source == "struct_array":
                # Array of structs — value is already a list of tuples from resolver
                raw = args.get(entry.get("arg_key"), [])
                if isinstance(raw, list):
                    values.append([tuple(item) if isinstance(item, (list, tuple)) else item for item in raw])
                else:
                    values.append([])

            elif source == "arg":
                arg_key = entry.get("arg_key", "")
                if "." in arg_key:
                    parent_key, child_key = arg_key.split(".", 1)
                    parent = args.get(parent_key)
                    raw = parent.get(child_key) if isinstance(parent, dict) else None
                else:
                    raw = args.get(arg_key)
                values.append(self.coerce_value(raw, coerce, from_address))

            elif source == "context":
                context_key = entry.get("context_key", "")
                if context_key == "from_address":
                    values.append(self.coerce_value(from_address, coerce, from_address))
                else:
                    values.append(self.coerce_value(None, coerce, from_address))

            elif source == "constant":
                values.append(self.coerce_value(entry.get("value"), coerce, from_address))

        return values

    def resolve_param_entry(
        self,
        entry: Dict,
        args: Dict[str, Any],
        from_address: str,
    ) -> Any:
        """Resolve a single param_mapping entry to its typed value."""
        source = entry.get("source")
        coerce = entry.get("coerce", "")

        if source == "arg":
            arg_key = entry.get("arg_key", "")
            if "." in arg_key:
                # Nested key: "__ordering.token0" -> args["__ordering"]["token0"]
                parent_key, child_key = arg_key.split(".", 1)
                parent = args.get(parent_key)
                raw = parent.get(child_key) if isinstance(parent, dict) else None
            else:
                raw = args.get(arg_key)
            return self.coerce_value(raw, coerce, from_address)
        if source == "context":
            context_key = entry.get("context_key", "")
            if context_key == "from_address":
                return self.coerce_value(from_address, coerce, from_address)
            return self.coerce_value(None, coerce, from_address)
        if source == "constant":
            return self.coerce_value(entry.get("value"), coerce, from_address)
        return None

    def coerce_value(self, value: Any, coerce: str, from_address: str) -> Any:
        """Coerce a resolved value to the type expected by eth_abi."""
        if coerce == "address":
            return normalize_address(value) if value else normalize_address(from_address)
        if coerce in ("uint256", "uint24", "uint160", "uint128", "int24", "int128", "uint32"):
            return int(value) if value is not None else 0
        if coerce == "int_array":
            if isinstance(value, list):
                return [int(v) for v in value]
            return [0, 0, 0]
        if coerce == "uint256_array":
            if isinstance(value, list):
                return [int(v) for v in value]
            return []
        if coerce == "address_array":
            if isinstance(value, list):
                return [normalize_address(v) for v in value]
            return []
        if coerce == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value) if value is not None else False
        if coerce == "bytes32":
            if isinstance(value, bytes):
                return value.ljust(32, b'\x00')[:32]
            s = str(value) if value is not None else "0x" + "00" * 32
            if s.startswith("0x"):
                s = s[2:]
            return bytes.fromhex(s.ljust(64, '0')[:64])
        if coerce == "bytes":
            if isinstance(value, bytes):
                return value
            s = str(value) if value is not None else "0x"
            if s.startswith("0x"):
                s = s[2:]
            return bytes.fromhex(s) if s else b""
        if coerce == "raw":
            return value
        return value

    def resolve_tx_value(self, action_spec: Dict, args: Dict) -> str:
        """Resolve the ETH value to send with the transaction."""
        value_logic = action_spec.get("value_logic", {})
        vtype = value_logic.get("type", "zero")

        if vtype == "zero":
            return "0"
        if vtype == "from_arg":
            source_arg = value_logic.get("source_arg", "value")
            return str(args.get(source_arg, "0"))
        if vtype == "amount_as_value":
            return str(args.get("value", "0"))
        return "0"

    # Utility

    def get_required_payload_args(self, chain_id: int = 1) -> Dict[str, List[str]]:
        """Build ACTION_REQUIRED_ARGS dict from playbook specs."""
        result = {}
        for action_name, spec in self.playbooks.get(chain_id, {}).items():
            result[action_name] = spec.get("required_payload_args", [])
        return result

    def get_supported_actions(self, chain_id: int = 1) -> List[str]:
        """Return list of all action names from loaded playbooks."""
        return list(self.playbooks.get(chain_id, {}).keys())

    def get_actions_by_protocol(self, chain_id: int = 1) -> Dict[str, List[Dict]]:
        """Return actions grouped by protocol: {protocol: [{action, description}, ...]}."""
        by_protocol = {}
        for name in self.get_supported_actions(chain_id):
            pb = self.playbook_meta.get(chain_id, {}).get(name, {})
            protocol = pb.get("protocol", "unknown")
            desc = self.playbooks.get(chain_id, {}).get(name, {}).get("description", "")
            by_protocol.setdefault(protocol, []).append({"action": name, "description": desc})
        return by_protocol

    # Approval encoding

    def encode_approval_txs(
        self,
        token_address: str,
        spender_address: str,
        chain_id: int,
    ) -> List[Dict[str, Any]]:
        """Encode ERC-20 approve tx(s). USDT gets a reset-to-zero tx first."""
        token_cs = to_checksum_address(token_address)
        spender_cs = to_checksum_address(spender_address)

        def encode_approve(amount):
            data = encode_from_abi(ERC20_APPROVE_ABI, [spender_cs, amount])
            return {
                "chain_id": chain_id,
                "to": token_cs,
                "value": "0",
                "data": data,
            }

        txs = []
        approve_reset = get_approve_reset_tokens(chain_id)
        if token_cs in approve_reset:
            txs.append(encode_approve(0))
        txs.append(encode_approve(UINT256_MAX))
        return txs

    # Full pipeline: LLM output → ordered transactions[]

    def build_transactions(
        self,
        llm_output: Dict[str, Any],
        chain_id: int = 1,
        from_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Full pipeline: LLM output -> ordered transactions[] (approvals first, then action)."""
        action = llm_output.get("action", "")

        try:
            payload = self.build_payload(llm_output, chain_id=chain_id, from_address=from_address)
        except (ValueError, KeyError) as e:
            return {"success": False, "error": f"Failed to resolve arguments: {e}"}

        if payload is None:
            return {"success": False, "error": f"Failed to build payload for '{action}'. Check arguments."}

        try:
            raw_tx = self.encode_tx(payload, from_address)
        except Exception as e:
            return {"success": False, "error": f"Failed to encode transaction: {e}"}

        if raw_tx is None:
            return {"success": False, "error": f"Failed to encode transaction for '{action}'."}

        transactions = []

        # Approval transactions first
        for approval in payload.get("approvals", []):
            token = approval.get("token")
            spender = approval.get("spender")
            if token and spender:
                token_cs = to_checksum_address(token)
                spender_cs = to_checksum_address(spender)
                for approve_tx in self.encode_approval_txs(token_cs, spender_cs, chain_id):
                    transactions.append({
                        "type": "approval",
                        "token": token_cs,
                        "spender": spender_cs,
                        "raw_tx": approve_tx,
                    })

        # Clean arguments (strip internal __ prefixed keys)
        clean_args = {}
        for k, v in payload.get("arguments", {}).items():
            if not k.startswith("__"):
                clean_args[k] = str(v) if not isinstance(v, (str, int, float, bool, list)) else v

        # Main action transaction
        transactions.append({
            "type": "action",
            "action": payload.get("action"),
            "target_contract": payload.get("target_contract"),
            "function_name": payload.get("function_name"),
            "arguments": clean_args,
            "raw_tx": raw_tx,
        })

        return {
            "success": True,
            "transactions": transactions,
        }
