# Contributing to defi-skills

## Development Setup

```bash
git clone https://github.com/NethermindEth/defi-skills
cd defi-skills
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v   # should pass 55/55
```

## Adding a New Protocol

This is the most common contribution. Here's the full workflow:

### 1. Generate a draft playbook

```bash
# Set your Etherscan key (needed to fetch ABIs)
export ETHERSCAN_API_KEY="..."

# Generate: picks up all write functions, or filter with --functions
python scripts/generate_playbook.py \
  --protocol <name> \
  --contracts "label=0xAddress" \
  --model claude-sonnet-4-6

# Or interactively select which functions to include
python scripts/generate_playbook.py \
  --protocol <name> \
  --contracts "label=0xAddress" \
  --interactive
```

The generator classifies each parameter and flags anything it's unsure about in `_review_notes.review_items`.

### 2. Review the generated JSON

Open `src/defi_skills/data/playbooks/<name>.json` and check:

- **`_review_notes`**: read every flagged item. Common fixes:
  - `passthrough` params that should be `constant` (e.g., `referralCode: 0`, `shares: 0`)
  - `passthrough` params that need a custom resolver (e.g., protocol-specific pool/market identifiers)
  - `data` fields that should be empty bytes: `{"source": "constant", "value": "0x"}`
- **`decimals_from`**: must reference the `llm_field` name (e.g., `$asset`), not the Solidity param name
- **`approvals`**: verify the LLM correctly identified which functions need ERC-20 approvals
- **`function_selector`**: already validated by the generator, but double-check against Etherscan
- **`value_logic`**: payable functions should have `{"type": "from_arg", "source_arg": "value"}`

Remove `_review_notes` when done.

### 3. Add the ABI to cache

The generator fetches it automatically. If you need to manually add:

```bash
python -m defi_skills.data.fetch_abis
```

This fetches ABIs for all contracts referenced in all playbooks and caches them in `src/defi_skills/data/abi_cache/`.

### 4. Write tests

Add test cases to `tests/test_playbook_parity.py`:

```python
pytest.param(
    {"action": "myprotocol_supply", "arguments": {"asset": "USDC", "amount": "100"}},
    {"action": "myprotocol_supply", "function_name": "supply", "target_contract": "0x...",
     "selector": "0x...", "args": {"asset": USDC_ADDR, "amount": "100000000"}},
    id="myprotocol_supply",
),
```

If your protocol needs on-chain calls (quoting, pool lookups), add mock responses in `mock_raw_eth_call`.

### 5. If needed: add a custom resolver

For protocols with on-chain state lookups (strategy discovery, pool routing, etc.), create a new resolver file:

```
src/defi_skills/engine/resolvers/myprotocol.py
```

Register it in `engine/resolvers/__init__.py`:

```python
from defi_skills.engine.resolvers.myprotocol import resolve_myprotocol_pool

RESOLVER_REGISTRY["resolve_myprotocol_pool"] = resolve_myprotocol_pool
```

Resolver signature:
```python
def resolve_myprotocol_pool(value: Any, ctx: ResolveContext, **kwargs) -> Any:
    # value = raw value from LLM args (or None)
    # ctx.token_resolver, ctx.ens_resolver = shared instances
    # ctx.resolved = previously resolved args in this payload
    # ctx.from_address, ctx.chain_id = transaction context
    # kwargs = extra fields from the playbook arg_spec
    ...
```

Resolvers must raise `ValueError` on failure. Never return `None` for required fields.

## Adding a Registry

For protocols with governance-mutable state (supported tokens, strategy maps):

1. Add an on-chain query to `scripts/refresh_registry.py`
2. Output goes to `src/defi_skills/data/registry/<protocol>.json`
3. `PlaybookEngine.load_registry()` merges it into action specs at startup

See `eigenlayer.json` and `compound_v3.json` for examples.

## Code Style

- No protocol-specific `if/else` in the engine. All protocol logic lives in playbooks + resolvers.
- Addresses must be EIP-55 checksummed at every output boundary.
- Resolvers raise `ValueError` on failure, not return `None`.
- Use `eth_utils.is_address()` for address validation, not regex.
- Keep playbook JSON flat. The generator handles struct nesting via `param_mapping`.

## Running Tests

```bash
pytest tests/ -v              # all tests
pytest tests/ -k "aave"       # filter by name
pytest tests/ -k "error"      # just error cases
```

Tests run fully offline with mocked on-chain calls. No API keys needed.
