# Agent Integration

defi-skills is built to be used by AI agents. It ships as a **skill** that any agent with shell access can pick up and use to build DeFi transactions deterministically, with no LLM running inside the CLI.

## How it works

1. The user tells the agent something like "Supply 100 USDC to Aave"
2. The **agent** (Claude Code, OpenClaw, or any LLM with shell access) classifies the intent and picks the right action + arguments
3. The agent calls the CLI with `--action` and `--args` (deterministic, no API key needed)
4. The CLI returns unsigned transactions as JSON
5. The agent presents the result to the user for review and signing

The agent is the LLM. The CLI is the execution engine. No LLM runs inside the CLI in this flow.

## Skill files

Ready-made skill files are included for both Claude Code and OpenClaw:

| Agent | Skill location |
|-------|---------------|
| Claude Code | `.claude/skills/intent-to-transaction/SKILL.md` |
| OpenClaw / generic agents | `openClaw-Skill/SKILL.md` |

Both contain the same instructions. Copy the appropriate file into your project and ensure `defi-skills` is installed with `WALLET_ADDRESS` set. The agent can then invoke it naturally:

> "Supply 100 USDC to Aave"
> "Swap 0.5 ETH for USDC on Uniswap"
> "Buy PT for wstETH on Pendle"

The agent workflow is always: discover actions (`defi-skills actions --json`), check parameters (`defi-skills actions <name> --json`), build transaction (`defi-skills build --action <name> --args '{...}' --json`).

## Claude Code Plugin

If you use Claude Code, you can install the skill directly as a plugin:

```
/plugin marketplace add NethermindEth/defi-skills
/plugin install defi-skills@nethermind-defi-skills
```

After installing, invoke it with:

```
/intent-to-transaction
```

## Why deterministic mode for agents?

Agents already have an LLM (themselves). Running a second LLM inside the CLI would be redundant, slower, and more expensive. The `--action` + `--args` path uses zero LLM tokens and gives agents full control over intent classification while the engine handles the hard parts: token resolution, decimal conversion, ABI encoding, and approval handling.
