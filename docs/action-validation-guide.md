# Action Validation Guide

Standard process for verifying that a new protocol or action produces correct transaction payloads. Every contributor adding a protocol must complete all three layers before submitting a PR.

## Overview

| Layer | What it proves | How | When |
|-------|---------------|-----|------|
| 1. Unit tests | Payload construction is correct (selector, args, target, encoding) | `pytest` with mocked resolvers | Every test run / CI |
| 2. Chain availability | Action appears on correct chains, absent on others | `pytest` assertions on `get_supported_actions()` | Every test run / CI |
| 3. Fork validation | Transaction executes on-chain and produces expected state changes | Manual: CLI build + `cast send` on Anvil fork + `cast call` to verify | Before submitting PR |

---

## Layer 1: Unit Tests

Add parametrized test cases to `tests/test_playbook_parity.py` for each action in the new protocol.

Each test case must verify:
- Correct `target_contract` address
- Correct `function_selector`
- Correct argument values (amounts scaled to proper decimals, addresses resolved)
- `encode_tx` produces non-empty calldata

Use mocked resolvers (no network calls). See existing test cases in `test_playbook_parity.py` for the pattern.

**Minimum: one test case per action.**

### Example

```python
pytest.param(
    {"action": "my_protocol_deposit", "arguments": {"asset": "USDC", "amount": "100"}},
    {
        "action": "my_protocol_deposit",
        "function_name": "deposit",
        "target_contract": "0x...",
        "selector": "0x47e7ef24",
        "args": {
            "amount": "100000000",  # 100 USDC at 6 decimals
        },
    },
    id="my_protocol_deposit_usdc",
),
```

---

## Layer 2: Chain Availability Tests

If the protocol is available on multiple chains, add assertions to verify:

- The protocol's actions appear in `get_supported_actions(chain_id)` for supported chains
- The protocol's actions do NOT appear for unsupported chains
- `protocol_available(chain_id, "my_protocol")` returns correctly

Add these to `tests/test_sepolia_parity.py` or a new chain-specific test file as appropriate.

---

## Layer 3: Fork Validation

This is manual. Follow these steps for each action on each supported chain.

### Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation) installed (`anvil`, `cast`)
- An Alchemy API key in `.env` (`ALCHEMY_API_KEY=...`)
- The defi-skills CLI working (`python -m defi_skills.cli.main actions`)

### Step 1: Start Anvil Fork

```bash
# Mainnet
source .env
anvil --fork-url "https://eth-mainnet.g.alchemy.com/v2/${ALCHEMY_API_KEY}" --chain-id 1

# Sepolia
anvil --fork-url "https://eth-sepolia.g.alchemy.com/v2/${ALCHEMY_API_KEY}" --chain-id 11155111
```

Anvil provides a default funded account:
- Address: `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`
- Private key: `0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80`

### Step 2: Fund the Test Wallet

The default account starts with 10,000 ETH. For ERC-20 tokens needed by your actions:

**Option A: Impersonate the token owner and mint**
```bash
OWNER=$(cast call <TOKEN> "owner()(address)" --rpc-url http://127.0.0.1:8545)
curl -s -X POST http://127.0.0.1:8545 -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"anvil_impersonateAccount\",\"params\":[\"$OWNER\"],\"id\":1}"
curl -s -X POST http://127.0.0.1:8545 -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"anvil_setBalance\",\"params\":[\"$OWNER\",\"0x8AC7230489E80000\"],\"id\":2}"
cast send <TOKEN> "mint(address,uint256)" <WALLET> <AMOUNT> \
  --from "$OWNER" --unlocked --rpc-url http://127.0.0.1:8545
```

**Option B: Swap into the token via Uniswap (if already supported)**
```bash
# Build a swap with the CLI, then execute with cast
python -m defi_skills.cli.main build --action uniswap_swap \
  --args '{"asset_in":"WETH","asset_out":"USDC","amount":"1"}' \
  --chain-id 1 -w 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 --json
# Then cast send each transaction from the output
```

### Step 3: Build the Transaction

```bash
python -m defi_skills.cli.main build \
  --action <ACTION_NAME> \
  --args '<JSON_ARGS>' \
  --chain-id <CHAIN_ID> \
  -w 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 \
  --json
```

Review the output:
- Is the `target_contract` correct?
- Is the `function_name` what you expect?
- Are the `arguments` correctly resolved (addresses, amounts, decimals)?
- If there are approval transactions, are they targeting the right token and spender?

### Step 4: Execute on Fork

Send each transaction from the build output (approvals first, then the action):

```bash
PK="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
RPC="http://127.0.0.1:8545"

# For each transaction in the build output:
cast send <TO> <CALLDATA> --value <VALUE>wei --private-key $PK --rpc-url $RPC
```

Confirm each returns `status: 1 (success)`.

If a transaction reverts, check:
- Is the wallet funded with the required token?
- Are approvals sent before the action?
- Is the target contract correct for this chain?

### Step 5: Verify Output State

**This is the critical step.** A successful transaction (`status: 1`) does not guarantee the action did what was intended. You must verify the state change.

Check the on-chain state before and after execution. What to verify depends on the action type:

| Action type | What to verify | Example check |
|-------------|---------------|--------------|
| Supply/Deposit | Receipt token balance increased | `cast call <aToken> "balanceOf(address)(uint256)" <wallet>` |
| Withdraw | Underlying token balance increased, receipt token decreased | Check both balances |
| Swap | Output token increased, input token decreased | Check both token balances |
| Borrow | Debt token balance increased, borrowed asset received | Check debt token + asset balance |
| Repay | Debt token balance decreased | Check debt token balance |
| Wrap/Unwrap | WETH/ETH balances swapped correctly | `cast balance` + WETH `balanceOf` |
| Transfer | Recipient balance increased | `cast call <token> "balanceOf(address)(uint256)" <recipient>` |
| LP mint | NFT minted (balance increased) | `cast call <NFPM> "balanceOf(address)(uint256)" <wallet>` |
| Stake | Staked/receipt token balance increased | Check receipt token balance |
| Claim rewards | Reward token balance increased (or 0 if no rewards accrued) | Check reward token balance |

**Record the before and after values.** You will include these in the PR description.

---

## PR Checklist

When submitting a PR for a new protocol, include the following in the PR description:

```markdown
## Protocol: <name>

### Code
- [ ] Playbook JSON added (`data/playbooks/<protocol>.json`)
- [ ] Chain resource files added (`data/chains/<chain_id>/<protocol>.json`)
- [ ] Custom resolvers added (if needed)
- [ ] ABI cache fetched via `python -m defi_skills.data.fetch_abis`
- [ ] Token cache updated (if protocol uses new tokens)

### Tests
- [ ] Unit test cases added to `test_playbook_parity.py` (one per action)
- [ ] Chain availability tests added (if multi-chain)
- [ ] All tests passing (`pytest tests/ -x`)

### Fork Validation
For each action on each supported chain:
- [ ] Anvil fork started (chain: ___, block: ___)
- [ ] CLI build output reviewed (correct target, selector, args)
- [ ] Transaction executed successfully (status: 1)
- [ ] Output state verified (paste before/after evidence below)

<details>
<summary>Fork validation output</summary>

[Paste CLI build output, cast send results, and before/after state checks here]

</details>
```

---

## Quick Reference

```bash
# Start fork
anvil --fork-url "https://eth-mainnet.g.alchemy.com/v2/${ALCHEMY_API_KEY}" --chain-id 1

# Build tx
python -m defi_skills.cli.main build --action <action> --args '<json>' --chain-id <id> -w 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 --json

# Execute tx
cast send <to> <calldata> --value <value>wei --private-key 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 --rpc-url http://127.0.0.1:8545

# Check ERC-20 balance
cast call <token> "balanceOf(address)(uint256)" <wallet> --rpc-url http://127.0.0.1:8545

# Check ETH balance
cast balance <wallet> --rpc-url http://127.0.0.1:8545

# Fund wallet with ETH
curl -s -X POST http://127.0.0.1:8545 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"anvil_setBalance","params":["<wallet>","0x8AC7230489E80000"],"id":1}'
```
