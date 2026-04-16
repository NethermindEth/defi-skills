"""Playbook engine — drives all protocol actions from JSON playbook files."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from eth_utils import to_checksum_address

from defi_skills.engine.chain_resources import (
    load_chain_contracts,
    protocol_available,
)
from defi_skills.engine.chains import get_approve_reset_tokens, is_native_sentinel
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
        # Per-chain resolver caches. Passed-in resolvers seed the cache
        # (defaults to chain_id=1). For other chains, resolvers are created
        # on demand by get_token_resolver / get_ens_resolver.
        self._token_resolvers: Dict[int, Any] = {}  # chain_id -> TokenResolver
        self._ens_resolvers: Dict[int, Any] = {}     # chain_id -> ENSResolver
        if token_resolver:
            self._token_resolvers[getattr(token_resolver, 'chain_id', 1)] = token_resolver
        if ens_resolver:
            self._ens_resolvers[getattr(ens_resolver, 'chain_id', 1)] = ens_resolver
        self.playbooks: Dict[str, Dict] = {}      # action_name -> action_spec
        self.playbook_meta: Dict[str, Dict] = {}   # action_name -> full playbook
        self.standard_abis: Dict[str, Dict] = {} # key -> ABI entry (from playbooks)
        self.registry: Dict[str, Dict] = {}       # protocol_name -> registry data
        self.load_playbooks(Path(playbooks_dir) if playbooks_dir else PLAYBOOKS_DIR)
        self.load_registry()

    def load_playbooks(self, playbooks_dir: Path) -> None:
        """Load all .json playbook files. Playbooks are chain-agnostic."""
        for pb_file in sorted(playbooks_dir.glob("*.json")):
            pb = json.loads(pb_file.read_text())
            for action_name, action_spec in pb.get("actions", {}).items():
                self.playbooks[action_name] = action_spec
                self.playbook_meta[action_name] = pb
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
        """Merge registry data into matching action specs using registry_mapping."""
        PLAYBOOK_LEVEL_KEYS = {"strategy_map"}
        for action_name, spec in self.playbooks.items():
            pb = self.playbook_meta.get(action_name, {})
            if pb.get("protocol") != protocol:
                continue
            for spec_key, reg_key in spec.get("registry_mapping", {}).items():
                if reg_key in reg:
                    if spec_key in PLAYBOOK_LEVEL_KEYS:
                        pb[spec_key] = reg[reg_key]
                    else:
                        spec[spec_key] = reg[reg_key]

    def get_token_resolver(self, chain_id: int):
        """Get or create a TokenResolver for the given chain."""
        if chain_id in self._token_resolvers:
            return self._token_resolvers[chain_id]
        from defi_skills.engine.token_resolver import TokenResolver
        tr = TokenResolver(chain_id=chain_id)
        self._token_resolvers[chain_id] = tr
        return tr

    def get_ens_resolver(self, chain_id: int):
        """Get or create an ENSResolver for the given chain."""
        if chain_id in self._ens_resolvers:
            return self._ens_resolvers[chain_id]
        from defi_skills.engine.ens_resolver import ENSResolver
        er = ENSResolver(chain_id=chain_id)
        self._ens_resolvers[chain_id] = er
        return er

    def get_contracts(self, action: str, chain_id: int) -> Dict[str, Dict]:
        """Resolve contracts for an action on a specific chain via ChainResources."""
        pb = self.playbook_meta.get(action, {})
        protocol = pb.get("protocol", "")
        if not protocol:
            return {}
        try:
            return load_chain_contracts(chain_id, protocol)
        except ValueError:
            return {}

    def action_available(self, action: str, chain_id: int) -> bool:
        """Check if an action is available on a chain.

        Returns False for unregistered chain IDs. Chain-agnostic playbooks
        (marked with "chain_agnostic": true) are available on all registered
        chains. All other protocols require a chain contract file at
        data/chains/{chain_id}/{protocol}.json.
        """
        from defi_skills.engine.chains import CHAIN_REGISTRY
        if chain_id not in CHAIN_REGISTRY:
            return False
        pb = self.playbook_meta.get(action, {})
        protocol = pb.get("protocol", "")
        if not protocol:
            return False
        if pb.get("chain_agnostic"):
            return True
        return protocol_available(chain_id, protocol)

    # Stage 1: LLM output → ExecutablePayload

    def build_payload(
        self,
        llm_output: Dict[str, Any],
        chain_id: int = 1,
        from_address: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Convert LLM output to ExecutablePayload dict."""
        action = llm_output.get("action")
        if not action or action not in self.playbooks:
            raise ValueError(
                f"Unknown action: '{action}'. "
                f"Use get_supported_actions() to list available actions."
            )
        if not self.action_available(action, chain_id):
            protocol = self.playbook_meta.get(action, {}).get("protocol", "unknown")
            from defi_skills.engine.chain_resources import supported_chains_for_protocol
            supported = supported_chains_for_protocol(protocol)
            chain_list = ", ".join(str(c) for c in sorted(supported)) if supported else "none"
            raise ValueError(
                f"'{action}' ({protocol}) is not available on chain {chain_id}. "
                f"Supported chains: {chain_list}."
            )

        action_spec = self.playbooks[action]
        playbook = self.playbook_meta[action]

        args = llm_output.get("arguments") or {}

        # Resolve contracts from ChainResources at runtime
        contracts = self.get_contracts(action, chain_id)

        # Apply chain-specific action overrides (e.g. different ABI on SwapRouter02)
        chain_action_overrides = contracts.get("action_overrides", {}).get(action, {})
        if chain_action_overrides:
            action_spec = {**action_spec, **chain_action_overrides}

        # Extract token overrides for this protocol on this chain
        token_overrides = contracts.get("token_overrides", {})

        ctx = ResolveContext(
            token_resolver=self.get_token_resolver(chain_id),
            ens_resolver=self.get_ens_resolver(chain_id),
            from_address=from_address,
            chain_id=chain_id,
            action=action,
            raw_args=args,
            playbook_contracts=contracts,
            playbook_data=playbook,
            token_overrides=token_overrides,
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
                safe_arg = sanitize_error(arg_name)
                safe_action = sanitize_error(action)
                logger.error(f"build_payload: resolver failed for '{safe_arg}' in '{safe_action}': {sanitized}")
                raise ValueError(f"Failed to resolve '{safe_arg}': {sanitized}") from e
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
        if source == "constant":
            return arg_spec.get("value")

        resolver_fn = RESOLVER_REGISTRY.get(source)
        if resolver_fn is None:
            return arg_spec.get("value")

        # Extract the raw value from LLM args
        raw_value = self.extract_llm_value(arg_spec, ctx)

        # Standard resolver: pass raw value + kwargs from spec.
        # Resolve $-prefixed kwargs against chain contracts (e.g. "$pool" -> "0x6Ae4...").
        skip_keys = {"source", "llm_field", "fallback_llm_fields", "context_field", "fallback_context"}
        kwargs = {}
        for k, v in arg_spec.items():
            if k in skip_keys:
                continue
            if isinstance(v, str) and v.startswith("$"):
                contract_key = v[1:]
                contract_info = ctx.playbook_contracts.get(contract_key, {})
                kwargs[k] = contract_info.get("address", v) if isinstance(contract_info, dict) else v
            else:
                kwargs[k] = v
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

        # Lookup from contracts map (resolved via ChainResources)
        contract_info = ctx.playbook_contracts.get(target)
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
        contracts = ctx.playbook_contracts

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

            if is_native_sentinel(token_addr):
                continue

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
        if not action or action not in self.playbooks:
            return None

        action_spec = self.playbooks[action]

        # Apply chain-specific action overrides
        contracts = self.get_contracts(action, chain_id)
        chain_action_overrides = contracts.get("action_overrides", {}).get(action, {})
        if chain_action_overrides:
            action_spec = {**action_spec, **chain_action_overrides}

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
            logger.error(f"encode_tx: ABI encoding failed for '{sanitize_error(action)}' ({sanitize_error(function_name)}): {sanitize_error(str(e))}")
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
            # Resolve contract address via ChainResources
            action_spec = self.playbooks.get(action)
            playbook = self.playbook_meta.get(action)
            if not action_spec or not playbook:
                return None
            target_key = action_spec.get("target_contract")
            if not target_key:
                return None
            protocol = playbook.get("protocol", "")
            try:
                contracts = load_chain_contracts(chain_id, protocol)
            except ValueError:
                return None
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
        if source == "struct":
            struct_values = []
            for field_entry in entry.get("fields", []):
                field_val = self.resolve_param_entry(field_entry, args, from_address)
                struct_values.append(field_val)
            return tuple(struct_values)
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
        result = {}
        for action_name, spec in self.playbooks.items():
            if self.action_available(action_name, chain_id):
                result[action_name] = spec.get("required_payload_args", [])
        return result

    def get_supported_actions(self, chain_id: int = 1) -> List[str]:
        return [a for a in self.playbooks if self.action_available(a, chain_id)]

    def get_actions_by_protocol(self, chain_id: int = 1) -> Dict[str, List[Dict]]:
        by_protocol = {}
        for name in self.get_supported_actions(chain_id):
            pb = self.playbook_meta.get(name, {})
            protocol = pb.get("protocol", "unknown")
            desc = self.playbooks.get(name, {}).get("description", "")
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
            return {"success": False, "error": f"Failed to resolve arguments: {sanitize_error(str(e))}"}

        if payload is None:
            return {"success": False, "error": f"Failed to build payload for '{sanitize_error(action)}'. Check arguments."}

        try:
            raw_tx = self.encode_tx(payload, from_address)
        except Exception as e:
            return {"success": False, "error": f"Failed to encode transaction: {sanitize_error(str(e))}"}

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
