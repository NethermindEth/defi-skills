# nethermind-defi

A Claude Code plugin that translates natural language into unsigned transaction payloads. Powered by the `defi-skills` CLI: a deterministic, data-driven PlaybookEngine that covers 12 DeFi protocols and 44 actions.

## Installation

```bash
/plugin install nethermind-defi-skills@claude-plugins-official
```

### Prerequisites

The `defi-skills` CLI must be installed and a wallet address configured:

```bash
npm install defi-skills --registry https://nethermind.jfrog.io/artifactory/api/npm/
defi-skills config set-wallet "0xYourWalletAddress"
```

For actions that need on-chain data (swaps, ENS resolution, balance queries):

```bash
defi-skills config set alchemy_api_key "YOUR_ALCHEMY_KEY"
```

## Usage

Once installed, just ask Claude naturally:

- "Supply 100 USDC to Aave"
- "Swap 0.5 ETH for USDC on Uniswap"
- "Stake 10 ETH on Lido"
- "Send 0.1 ETH to vitalik.eth"
- "Wrap 5 ETH"

Claude will classify your intent, call the deterministic CLI, and return unsigned transactions for you to review and sign.

### Multi-step operations

For complex intents, Claude presents a plan before building:

> "Stake 10 ETH on Lido, then restake on EigenLayer"

Claude will explain the steps, amounts, and assumptions, then wait for your confirmation.

## Supported Protocols

| Protocol | Actions |
|----------|---------|
| Native ETH | `transfer_native` |
| ERC-20 | `transfer_erc20` |
| ERC-721 | `transfer_erc721` |
| Aave V3 | `aave_supply`, `aave_withdraw`, `aave_borrow`, `aave_repay`, `aave_set_collateral`, `aave_repay_with_atokens`, `aave_claim_rewards` |
| Lido | `lido_stake`, `lido_wrap_steth`, `lido_unwrap_wsteth`, `lido_unstake`, `lido_claim_withdrawals` |
| Uniswap V3 | `uniswap_swap`, `uniswap_lp_mint`, `uniswap_lp_collect`, `uniswap_lp_decrease`, `uniswap_lp_increase` |
| Curve | `curve_add_liquidity`, `curve_remove_liquidity`, `curve_gauge_deposit`, `curve_gauge_withdraw`, `curve_mint_crv` |
| WETH | `weth_wrap`, `weth_unwrap` |
| Compound V3 | `compound_supply`, `compound_withdraw`, `compound_borrow`, `compound_repay`, `compound_claim_rewards` |
| MakerDAO DSR | `maker_deposit`, `maker_redeem` |
| Rocket Pool | `rocketpool_stake`, `rocketpool_unstake` |
| EigenLayer | `eigenlayer_deposit`, `eigenlayer_delegate`, `eigenlayer_undelegate`, `eigenlayer_queue_withdrawals`, `eigenlayer_complete_withdrawal` |
| Balancer V2 | `balancer_swap`, `balancer_join_pool`, `balancer_exit_pool` |

## Output Format

The plugin returns unsigned transactions as JSON:

```json
{
  "success": true,
  "transactions": [
    {
      "type": "approval",
      "raw_tx": { "chain_id": 1, "to": "0x...", "value": "0", "data": "0x..." }
    },
    {
      "type": "action",
      "action": "aave_supply",
      "raw_tx": { "chain_id": 1, "to": "0x...", "value": "0", "data": "0x..." }
    }
  ]
}
```

Transactions are ordered: approvals first, then the main action. Execute them in sequence.

## Safety

- Output is always an **unsigned transaction**. The plugin never signs or broadcasts.
- No private keys are involved at any stage.
- The deterministic build path uses zero LLM tokens inside the CLI.
- All addresses are EIP-55 checksummed.
- USDT non-standard approval is handled automatically (reset to zero first).
- DEX operations include on-chain quoting with slippage protection.
- Resolvers fail with clear errors rather than producing broken transactions.
- Always review `to`, `value`, and `data` before signing.

## API Keys

| Key | Required For |
|-----|-------------|
| `WALLET_ADDRESS` | All actions (your wallet address) |
| `ALCHEMY_API_KEY` | ENS resolution, swap quotes, balance queries, EigenLayer verification |
| `THEGRAPH_API_KEY` | Balancer V2 actions only |

Simple actions with known tokens and fixed amounts (e.g., `aave_supply` with USDC) work without any API keys beyond `WALLET_ADDRESS`.

## Limitations

- Mainnet only for now (chain ID 1)
- No gas estimation (left to the signing wallet)
- Single-hop swaps only (no multi-hop routing)
- No signing or broadcasting (by design)

## License

MIT

## Author

[Nethermind](https://nethermind.io)
