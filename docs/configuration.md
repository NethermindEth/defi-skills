# Configuration

## Interactive setup (recommended)

```bash
defi-skills config setup
```

Walks you through wallet address, LLM API key, and optional provider keys. Settings are stored at `~/.defi-skills/config.json` (file permissions: owner-only read/write).

## API keys

Environment variables are the recommended way to set API keys. They are ephemeral (gone when your shell closes), never touch disk, and take precedence over the config file.

```bash
export ALCHEMY_API_KEY="your-key"
export ANTHROPIC_API_KEY="your-key"
```

For convenience, you can also persist keys via the CLI. These are stored in `~/.defi-skills/config.json` with restricted file permissions (`0600`), and are used as fallbacks when the corresponding environment variable is not set.

```bash
defi-skills config set alchemy_api_key "your-key"
```

**Always required:**

| Variable | Purpose |
|----------|---------|
| `WALLET_ADDRESS` | Your wallet address (used as `from_address` in transactions) |

**Required for most actions:**

| Variable | Purpose | When you need it |
|----------|---------|------------------|
| `ALCHEMY_API_KEY` | RPC via Alchemy | ENS resolution, on-chain quotes (Uniswap/Balancer/Curve), balance queries ("max" amounts), EigenLayer strategy verification, Lido/Aave reward discovery. Without it, only basic actions with known tokens and specific amounts work (e.g. `aave_supply` with USDC and a fixed amount). |

**Required for specific features:**

| Variable | Purpose | When you need it |
|----------|---------|------------------|
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | LLM provider | Only for `defi-skills chat` (interactive agent mode). Not needed for `build`, `simulate`, or `actions`. |
| `THEGRAPH_API_KEY` | The Graph subgraph queries | Only for Balancer V2 actions (`balancer_swap`, `balancer_join_pool`, `balancer_exit_pool`). |

**Optional:**

| Variable | Purpose | When you need it |
|----------|---------|------------------|
| `ETHERSCAN_API_KEY` | Fetch verified contract ABIs | Only when adding a new protocol. Run `python -m defi_skills.data.fetch_abis` once, then the ABIs are cached locally. |
| `ONEINCH_API_KEY` | Token symbol discovery | Only when resolving a token not in the local cache (~100 common tokens are pre-cached). Falls back to on-chain query via Alchemy if not set. |
