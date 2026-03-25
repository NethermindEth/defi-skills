# DeFi Agent | DefiSkills

A fully working DeFi agent built on the [defi-skills](../../README.md) engine. Type what you want in plain English, the agent parses your intent, the engine builds unsigned transactions, and you sign with your wallet.

## Architecture

```
  "Stake 10 ETH on Lido"
          │
          ▼
    ┌─────────────┐     POST /intent      ┌──────────────────┐
    │  index.html  │ ──────────────────► │  LLM (one call)   │
    │  + agent.js  │ ◄────────────────── │  → structured     │
    │              │   {action, args}      │    actions         │
    │              │                       └──────────────────┘
    │              │     POST /build       ┌──────────────────┐
    │              │ ──────────────────► │  PlaybookEngine    │
    │              │ ◄────────────────── │  (deterministic)   │
    │              │   {transactions}      │  zero LLM tokens  │
    │              │                       └──────────────────┘
    │              │    eth_sendTx         ┌──────────────────┐
    │              │ ──────────────────► │  MetaMask/Rabby    │
    │              │ ◄────────────────── │  → real on-chain   │
    └─────────────┘    tx confirmed       └──────────────────┘
```

## How It Works

1. **Connect wallet**:MetaMask or Rabby (Ethereum Mainnet)
2. **Read portfolio**:agent fetches your ETH + ERC-20 balances
3. **Type intent**:"Stake 10 ETH on Lido", "Swap 500 USDC for WETH"
4. **Agent builds plan**:one LLM call parses intent, engine builds unsigned txs
5. **Sign & execute**:review the plan, click sign, MetaMask confirms each tx

## Run Locally

```bash
# 1. Install the engine
npm install defi-skills --registry https://nethermind.jfrog.io/artifactory/api/npm/

# 2. Configure
defi-skills config set-wallet "0xYourWalletAddress"
defi-skills config set alchemy_api_key "YOUR_ALCHEMY_KEY"

# 3. Set LLM key (for intent parsing)
export ANTHROPIC_API_KEY=" "

# 4. Run
cd examples/defi-agent
python server.py
# Open http://localhost:8000
```

## Tech Stack

- **Engine**: defi-skills PlaybookEngine:45 actions, 12 protocols, deterministic
- **Server**: FastAPI (~100 lines):imports engine directly, no external dependencies
- **Frontend**: Vanilla JS + Tailwind CDN:single page, no build step
- **Wallet**: Native `window.ethereum`:no ethers.js dependency
- **LLM**: Single-shot intent parsing via litellm (Claude/GPT)

## Files

| File | Purpose |
|------|---------|
| `server.py` | Minimal FastAPI:/actions, /build, /intent, /skill |
| `agent.js` | DeFiAgent class:orchestration logic |
| `index.html` | Chat UI with wallet integration |
| `agent.json` | Agent capabilities manifest |

## What the Engine Supports

45 actions across 12 protocols: Aave V3, Lido, Uniswap V3, Curve, Compound V3, MakerDAO DSR, Rocket Pool, EigenLayer, Balancer V2, WETH, ERC-20, ERC-721.

The engine handles token resolution, amount conversion, ABI encoding, approval generation, and USDT special cases:all deterministically, with zero LLM tokens.
