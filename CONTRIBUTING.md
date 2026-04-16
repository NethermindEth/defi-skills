# Contributing to defi-skills

## Development Setup

```bash
git clone https://github.com/NethermindEth/defi-skills
cd defi-skills
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v   # should pass all tests
```

## Adding a New Protocol

This is the most common contribution. A protocol integration consists of:
- A **playbook** JSON file (protocol knowledge: contracts, actions, parameters)
- Optionally, **custom resolvers** (Python functions for dynamic on-chain/API data)
- **Tests** that verify the playbook produces correct transactions

### Option A: Use the Claude Code agent

If you have Claude Code, the fastest path is the playbook-generator agent:

```
@playbook-generator Add Morpho protocol
```

The agent researches the protocol, finds contract addresses, handles proxy patterns, runs the generator script, reviews the output, and validates it. See `.claude/agents/playbook-generator.md` for details.

### Option B: Manual workflow

#### Step 1: Generate a draft playbook

```bash
# Set your Etherscan key (needed to fetch ABIs)
export ETHERSCAN_API_KEY="..."

# Generate with specific functions
python scripts/generate_playbook.py \
  --protocol <name> \
  --contracts "label=0xAddress" \
  --functions supply,withdraw,borrow \
  --model claude-sonnet-4-6

# Or interactively select which functions to include
python scripts/generate_playbook.py \
  --protocol <name> \
  --contracts "label=0xAddress" \
  --interactive
```

For **Diamond/multi-facet proxies** (like Pendle), the script auto-detects standard EIP-2535 Diamond proxies. For non-standard ones, provide facet addresses manually:

```bash
python scripts/generate_playbook.py \
  --protocol pendle \
  --contracts router=0x888888888889758F76e7103c6CbF23ABbF58F946 \
  --facets 0xd8D200d9... 0x4a03Ce0a... 0x373Dba20... \
  --functions swapExactTokenForPt,addLiquiditySingleToken
```

If the script warns "looks like a multi-facet proxy but facets could not be auto-detected," search the protocol's docs or GitHub for facet addresses.

#### Step 2: Review the generated JSON

Open `src/defi_skills/data/playbooks/<name>.json` and check:

- **`_review_notes`** (script output only, the agent handles this automatically): read every flagged item. Common fixes:
  - `passthrough` params that should be `constant` (e.g., `referralCode: 0`)
  - `passthrough` params that need a custom resolver (e.g., market addresses, pool IDs)
  - Missing or incorrect `approvals`
- **`decimals_from`**: must reference the `llm_field` name (e.g., `$asset`), not the Solidity param name
- **`function_selector`**: already validated by the generator, but double-check against Etherscan
- **`value_logic`**: `{"type": "zero"}` for ERC-20 actions, `{"type": "from_arg", "source_arg": "value"}` for payable/ETH actions
- **Struct constants**: for complex protocols, verify default values match the protocol's SDK (e.g., ApproxParams for Pendle, SwapData for aggregators)

Remove `_review_notes` from the final JSON before committing.

#### Step 3: Understand the playbook schema

**Top level:**

```json
{
  "protocol": "my_protocol",
  "version": "1.0",
  "actions": { ... }
}
```

Playbooks are **chain-agnostic**. They define action logic but not contract addresses. Addresses live in ChainResources (see [Multi-Chain Support](#multi-chain-support) below). Playbooks reference contracts by label (e.g. `"target_contract": "pool"`), and the engine resolves these labels at runtime from `data/chains/{chain_id}/{protocol}.json`.

**Each action has:**

| Field | Purpose |
|-------|---------|
| `description` | Shown to the LLM to help it pick the right action |
| `target_contract` | Which contract label from `contracts` to call |
| `function_name` | Solidity function name |
| `function_selector` | 4-byte selector (disambiguates overloaded functions) |
| `value_logic` | How much ETH to send: `"zero"` or `"from_arg"` |
| `approvals` | ERC-20 approvals needed before the action |
| `payload_args` | How to resolve each argument (resolver specs, processed in order) |
| `param_mapping` | How resolved values map to ABI calldata (must match Solidity parameter order) |

#### Step 4: Choose the right resolver for each parameter

`payload_args` entries tell the engine how to resolve each value. The `source` field maps to a resolver function:

**Token addresses**: user says "USDC", resolver returns checksummed address:
```json
"asset": { "source": "resolve_token_address", "llm_field": "asset" }
```

**Amounts**: user says "500", resolver converts using token decimals:
```json
"amount": {
  "source": "resolve_amount",
  "llm_field": "amount",
  "decimals_from": "$asset"
}
```

Three amount resolvers for different "max" behaviors:
- `resolve_amount`: "max" raises error (for borrow)
- `resolve_amount_or_max`: "max" = UINT256_MAX (for Aave/Compound withdraw/repay)
- `resolve_amount_or_balance`: "max" queries on-chain `balanceOf` (for supply, swap, stake)

**Addresses**: sender's wallet or ENS:
```json
"onBehalfOf": {
  "source": "resolve_ens_or_hex",
  "llm_field": "onBehalfOf",
  "context_field": "from_address"
}
```
`context_field: from_address` means it defaults to the sender's wallet if the LLM doesn't provide a value.

**Constants**: values that never change:
```json
"referralCode": { "source": "constant", "value": 0 }
```

**Slippage-protected minimums**: quote + apply slippage:
```json
"amountOutMinimum": {
  "source": "resolve_uniswap_quote",
  "slippage_bps": 50,
  "quoter_address": "0x..."
}
```

**Optional user overrides** (slippage tolerance):
```json
"slippage": {
  "source": "llm_passthrough",
  "llm_field": "slippage",
  "optional": true
}
```

**Descriptions**: for non-obvious fields, add `"description"` so the CLI and LLM know what to expect:
```json
"market": {
  "source": "resolve_pendle_market",
  "llm_field": "market",
  "description": "Token name (e.g. \"wstETH\") or market address"
}
```

**Key rule:** `payload_args` are resolved in declaration order. Later resolvers can read earlier results via `ctx.resolved`. That's why `amount` must come after `asset`, because it needs the resolved token's decimals.

#### Step 5: Map resolved values to ABI calldata

`param_mapping` entries go in the same order as the Solidity function parameters:

```json
"param_mapping": [
  { "name": "asset",       "source": "arg",      "arg_key": "asset",       "coerce": "address" },
  { "name": "amount",      "source": "arg",      "arg_key": "amount",      "coerce": "uint256" },
  { "name": "onBehalfOf",  "source": "arg",      "arg_key": "onBehalfOf",  "coerce": "address" },
  { "name": "referralCode","source": "constant",  "value": 0,              "coerce": "uint256" }
]
```

Sources:
- `"arg"`: pull from resolved `payload_args` by `arg_key`
- `"constant"`: hardcoded value
- `"context"`: from transaction context (e.g., `from_address`)
- `"struct"`: nested tuple with `"fields": [...]` (supports arbitrary nesting)

Coerce types: `address`, `uint256`, `uint24`, `bool`, `bytes`, `bytes32`

For nested struct parameters:
```json
{
  "name": "params",
  "source": "struct",
  "fields": [
    { "name": "tokenIn",  "source": "arg",      "arg_key": "asset_in", "coerce": "address" },
    { "name": "fee",      "source": "arg",      "arg_key": "fee",      "coerce": "uint24" },
    { "name": "deadline", "source": "arg",      "arg_key": "deadline", "coerce": "uint256" },
    { "name": "sqrtPriceLimitX96", "source": "constant", "value": 0,   "coerce": "uint256" }
  ]
}
```

#### Step 6: Get the approvals right

Approvals are needed when the user's tokens are transferred FROM them to the protocol:

```json
"approvals": [{ "token": "$asset", "spender": "target_contract" }]
```

- `$asset` references a resolved `payload_args` key
- `"target_contract"` means the contract address from `contracts`

**When to include approvals:**
- Supply, deposit, stake (user sends ERC-20 to protocol): YES
- Swap input token: YES
- Add liquidity: YES
- Withdraw, unstake, claim (protocol sends tokens back): NO
- Payable functions (user sends ETH via msg.value): NO

**When selling/exiting (selling PT, removing LP, redeeming):**
- The user is spending their PT/LP/receipt tokens
- These tokens need approval to the router
- Use `"$_pendle_pt"` or `"$market"` to reference dynamically resolved addresses

**Native ETH (msg.value) and approval skipping:**

If your protocol accepts native ETH as a swap input or deposit, the approval spec can stay chain-agnostic — the engine automatically skips approvals whose resolved token is a recognised native-ETH sentinel. See `NATIVE_ETH_SENTINELS` and `is_native_sentinel()` in [src/defi_skills/engine/chains.py](src/defi_skills/engine/chains.py); the skip happens in `resolve_approvals` in [playbook_engine.py](src/defi_skills/engine/playbook_engine.py).

Currently recognised sentinels:

- `0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` (industry standard, used by 1inch, Fibrous, Paraswap, etc.)
- `0x0000000000000000000000000000000000000000` (used by a few protocols, e.g. Uniswap V4 PoolKey)

If your protocol uses a **different** placeholder for native ETH in its swap/deposit calldata, add that address (lowercase) to `NATIVE_ETH_SENTINELS`. Otherwise your playbook will emit a bogus `approve(router, MAX)` transaction pointed at the sentinel address (not a real contract), which will revert on chain. No per-playbook flag is needed — the skip is driven entirely by the sentinel list.

#### Step 7: Add custom resolvers (if needed)

If the protocol has dynamic data that existing resolvers don't handle:

1. Create `src/defi_skills/engine/resolvers/myprotocol.py`:

```python
from typing import Any
from defi_skills.engine.resolvers.common import ResolveContext, resolve_slippage_bps

def resolve_myprotocol_quote(value: Any, ctx: ResolveContext, **kwargs) -> str:
    """Quote expected output via protocol's API/on-chain call."""
    slippage_bps = resolve_slippage_bps(ctx, kwargs)
    amount_in = int(ctx.resolved.get("amount", 0))

    if not amount_in:
        raise ValueError("resolve_myprotocol_quote: amount not resolved")

    # Get quote (API call, on-chain call, etc.)
    quoted = get_quote_somehow(amount_in)

    # Apply slippage
    min_out = quoted * (10000 - slippage_bps) // 10000
    return str(min_out)
```

2. Register in `engine/resolvers/__init__.py`:

```python
from defi_skills.engine.resolvers.myprotocol import resolve_myprotocol_quote

RESOLVER_REGISTRY = {
    ...
    "resolve_myprotocol_quote": resolve_myprotocol_quote,
}
```

3. If the resolver is auto-computed (not user-facing), add to `SKIP_SOURCES` in `cli/main.py`:

```python
SKIP_SOURCES = frozenset({
    ...
    "resolve_myprotocol_quote",
})
```

**Resolver rules:**
- Signature: `def resolve_xxx(value: Any, ctx: ResolveContext, **kwargs) -> Any`
- Raise `ValueError` on failure. Never return `None` for required fields. Never use silent fallbacks.
- Access previously resolved values via `ctx.resolved["key"]`
- Access user's wallet via `ctx.from_address`
- Access playbook config via `kwargs` (e.g., `kwargs.get("slippage_bps", 50)`)
- For slippage, use `resolve_slippage_bps(ctx, kwargs)` which handles user overrides automatically

#### Step 8: Write tests

Add test cases to `tests/test_playbook_parity.py`:

```python
# In the TEST_CASES list:
pytest.param(
    # Input: what the LLM would send
    {"action": "myprotocol_supply", "arguments": {"asset": "USDC", "amount": "100"}},
    # Expected: what the engine should produce
    {"action": "myprotocol_supply", "function_name": "supply",
     "target_contract": "0xBBBB...",
     "selector": "0x617ba037",
     "args": {"asset": USDC_ADDR, "amount": "100000000"}},
    id="myprotocol_supply",
),
```

If your resolver makes on-chain calls, add deterministic mock responses in `mock_raw_eth_call`:

```python
if "myQuoterFunction" in sig:
    return (1_000_000,)
```

If your resolver makes external API calls, mock them in the test:

```python
@patch("defi_skills.engine.resolvers.myprotocol.requests.get")
def test_myprotocol_swap(mock_get, engine):
    mock_get.return_value.json.return_value = {"rate": 1.5}
    ...
```

#### Step 9: Add chain resources

For each chain the protocol supports, create `src/defi_skills/data/chains/{chain_id}/{protocol}.json`:

```json
{
  "pool": "0x...",
  "router": "0x..."
}
```

Keys must match the labels used in the playbook (e.g., if the playbook says `"target_contract": "pool"`, the chain resource must have a `"pool"` key). Note: `$` prefixed references like `$asset` are used in `payload_args` kwargs (e.g. `decimals_from: "$asset"`), not in `target_contract`.

For testnets or chains where the protocol uses a different ABI or different token addresses, add overrides:

```json
{
  "pool": "0x...",
  "token_overrides": {
    "WETH": "0xTestWETH..."
  },
  "action_overrides": {
    "my_action": {
      "function_selector": "0x...",
      "param_mapping": [...]
    }
  }
}
```

- **`token_overrides`**: protocol-scoped token address remapping (e.g. Aave's test WETH on Sepolia)
- **`action_overrides`**: per-action ABI overrides merged on top of the base playbook (e.g. Uniswap SwapRouter02 on Sepolia has a different selector and no deadline field)

After adding chain resource files, fetch the ABIs for the new contract addresses:

```bash
export ETHERSCAN_API_KEY="..."
python -m defi_skills.data.fetch_abis
```

This scans all `data/chains/{chain_id}/{protocol}.json` files, fetches verified ABIs from Etherscan V2 (handles proxies and Diamond patterns automatically), caches them in `data/abi_cache/`, and verifies that every playbook action's function exists in the fetched ABI.

#### Step 10: Validate

**Unit tests:**
```bash
pytest tests/ -x -q
```

**Build test:**
```bash
defi-skills build -a myprotocol_supply -A '{"asset":"USDC","amount":"500"}' --chain-id 1 -j
```

**Fork validation (required before submitting PR):**

See [docs/action-validation-guide.md](docs/action-validation-guide.md) for the full process. Summary:

1. Start an Anvil fork: `anvil --fork-url "https://eth-mainnet.g.alchemy.com/v2/$ALCHEMY_API_KEY" --chain-id 1`
2. Build with CLI: `defi-skills build -a myprotocol_supply -A '{"asset":"USDC","amount":"500"}' --chain-id 1 -w 0xf39F... -j`
3. Execute with cast: `cast send <to> <calldata> --value <value>wei --private-key <anvil-pk> --rpc-url http://127.0.0.1:8545`
4. Verify state change: `cast call <token> "balanceOf(address)(uint256)" <wallet> --rpc-url http://127.0.0.1:8545`
5. Paste the output in your PR description

### Multi-Chain Support

The engine supports 6 chains: Ethereum Mainnet (1), Arbitrum (42161), Base (8453), Optimism (10), Polygon (137), and Sepolia (11155111).

```
Playbook (chain-agnostic)     →  action logic, $ references, param mappings
ChainResources (per-chain)    →  contract addresses, action/token overrides
TokenResolver (per-chain)     →  symbol → address + decimals via 1inch + Alchemy RPC
```

**Adding a protocol to a new chain:**

1. Create `data/chains/{chain_id}/{protocol}.json` with the chain's contract addresses
2. If the protocol uses different ABIs on this chain, add `action_overrides` (e.g. Uniswap SwapRouter02 on Base/Sepolia)
3. If the protocol uses different token addresses (e.g. USDC.e on Polygon Compound, test tokens on Sepolia), add `token_overrides`
4. Fetch ABIs: `python -m defi_skills.data.fetch_abis` (handles proxies automatically)
5. Run fork validation on the new chain

**Adding a completely new chain:**

1. Register the chain in `src/defi_skills/engine/chains.py` (chain config with Alchemy slug, 1inch chain ID, native symbol, WETH address)
2. Create `data/chains/{chain_id}/` with protocol resource files
3. Fetch ABIs: `python -m defi_skills.data.fetch_abis`
4. The `TokenResolver` automatically discovers tokens via 1inch API and Alchemy RPC -- no manual token cache needed (requires `ONEINCH_API_KEY` and `ALCHEMY_API_KEY` with the chain enabled)

**Key rules:**
- Playbooks never contain hardcoded addresses. Use `$` references.
- Token caches are auto-populated by the resolver. If a protocol uses non-canonical tokens (e.g. USDC.e instead of native USDC), add `token_overrides` in the chain resource file.
- `chain_agnostic: true` in a playbook means the action is available on all chains (e.g. ERC-20 transfers).
- Polygon's native token is POL, not ETH. WETH wrap/unwrap is not available on Polygon.

### Common Patterns

**Simple staking (ETH in, receipt token out):**
- `value_logic`: `from_arg` (payable)
- `payload_args`: amount with `resolve_amount_or_balance`, `decimals_from: $native`
- `param_mapping`: empty (no calldata, ETH sent via msg.value)
- `approvals`: none
- Examples: `lido_stake`, `weth_wrap`, `rocketpool_stake`

**Token deposit (ERC-20 in):**
- `value_logic`: `zero`
- `payload_args`: asset with `resolve_token_address`, amount with `resolve_amount_or_balance`
- `approvals`: `[{"token": "$asset", "spender": "target_contract"}]`
- Examples: `aave_supply`, `compound_supply`, `eigenlayer_deposit`

**Token withdrawal (position closed, underlying returned):**
- `value_logic`: `zero`
- `payload_args`: asset, amount with `resolve_amount_or_max` (protocol treats MAX as "close all")
- `approvals`: none
- Examples: `aave_withdraw`, `compound_withdraw`

**Swap with slippage protection:**
- `value_logic`: `zero`
- `payload_args`: asset_in, asset_out, amount, slippage (optional), min_out (quote resolver)
- `approvals`: `[{"token": "$asset_in", "spender": "target_contract"}]`
- Examples: `uniswap_swap`, `pendle_swap_token_for_pt`

## Adding a Registry

For protocols with governance-mutable state (supported tokens, strategy maps):

1. Add an on-chain query to `scripts/refresh_registry.py`
2. Output goes to `src/defi_skills/data/registry/<protocol>.json`
3. `PlaybookEngine.load_registry()` merges it into action specs at startup

See `eigenlayer.json` and `compound_v3.json` for examples.

## Code Style

- No protocol-specific `if/else` in the engine. All protocol logic lives in playbooks + resolvers.
- Addresses must be EIP-55 checksummed at every output boundary.
- Resolvers raise `ValueError` on failure, never return `None` or silent defaults.
- Use `eth_utils.is_address()` for address validation, not regex.
- Keep playbook JSON flat. The generator handles struct nesting via `param_mapping`.

## Running Tests

```bash
pytest tests/ -v              # all tests
pytest tests/ -k "aave"       # filter by name
pytest tests/ -k "error"      # just error cases
```

Tests run fully offline with mocked on-chain calls. No API keys needed.
