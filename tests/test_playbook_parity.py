"""PlaybookEngine parity tests — verify correct outputs for all supported actions."""

import json
from unittest.mock import patch, MagicMock

import pytest

from defi_skills.engine.playbook_engine import PlaybookEngine
from defi_skills.engine.token_resolver import TokenResolver
from defi_skills.engine.ens_resolver import ENSResolver

# Constants

FROM_ADDRESS = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
CHAIN_ID = 1
FIXED_TIME = 1700000000
UINT256_MAX = str(2**256 - 1)

# Known addresses (from token cache)
USDC_ADDR = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT_ADDR = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DAI_ADDR = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
WETH_ADDR = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
STETH_ADDR = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
ALICE_ADDR = "0xcd2E72aEBe2A203b84f46DEEC948E6465dB51c75"  # alice.eth
BAYC_ADDR = "0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D"
PUNKS_ADDR = "0xb47e3cd837dDF8e4c57F05d70Ab865de6e193BBB"

# Protocol contract addresses
AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
LIDO_STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
LIDO_WITHDRAWAL = "0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1"
UNISWAP_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
CURVE_3POOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"
COMPOUND_COMET = "0xc3d688B66703497DAA19211EEdff47f25384cdc3"
MAKER_SDAI = "0x83F20F44975D03b1b09e64809B757c47f942BeeA"
ROCKETPOOL_DEPOSIT = "0xDD9683b1bF4bB6d8fDF0A2B4A05aaadCA2A8a921"
ROCKETPOOL_RETH = "0xae78736Cd615f374D3085123A210448E74Fc6393"
EIGENLAYER_SM = "0x858646372CC42E1A627fcE94aa7A7033e7CF075A"
EIGENLAYER_DM = "0x39053D51B77DC0d36036Fc1fCc8Cb819df8Ef37A"
BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
COMPOUND_REWARDS = "0x1B0e765F6224C21223AeA2af16c1C46E38885a40"
CURVE_MINTER = "0xd061D61a4d941c39E5453435B6345Dc261C2fcE0"
AAVE_REWARDS = "0x8164Cc65827dcFe994AB23944CBC90e0aa80BfcB"
CURVE_3POOL_GAUGE = "0xbFcF63294aD7105dEa65aA58F8AE5BE2D9d0952A"
CURVE_3POOL_LP = "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490"
PENDLE_ROUTER = "0x888888888889758F76e7103c6CbF23ABbF58F946"
PENDLE_MARKET = "0x34280882267ffa6383B363E278B027Be083bBe3b"
PENDLE_PT = "0xb253Eff1104802b97aC7E3aC9FdD73AecE295a2c"
PENDLE_YT = "0x04B7Fa1e727d7290D6E24fA9b426d0c940283a95"
PENDLE_SY = "0xcbC72d92b2dc8187414F6734718563898740C0bc"
STETH_STRATEGY = "0x93c4b944D05dfe6df7645A86cd2206016c51564D"
MOCK_OPERATOR = "0x1234567890ABCDeF1234567890aBcDEF12345678"
MOCK_ATOKEN_USDC = "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c"
MOCK_ATOKEN_WETH = "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8"
NFPM = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"
UINT128_MAX = str(2**128 - 1)


# Fixtures

@pytest.fixture(scope="session")
def engine():
    # Use a MagicMock as w3 so resolvers see a truthy provider (pass "if not w3"
    # guards) but no real network calls are made. All on-chain calls go through
    # raw_eth_call which is mocked in each test.
    mock_w3 = MagicMock()
    # ENS resolve() is live-only. Mock it to return known test addresses.
    def mock_ens_address(name):
        lookup = {"alice.eth": ALICE_ADDR}
        result = lookup.get(name.lower())
        if result is None:
            raise ValueError(f"mock: unknown ENS name '{name}'")
        return result
    mock_w3.ens.address.side_effect = mock_ens_address
    mock_w3.ens.name.return_value = None
    tr = TokenResolver(w3=mock_w3)
    er = ENSResolver(w3=mock_w3)
    return PlaybookEngine(token_resolver=tr, ens_resolver=er)


# Mocks

def mock_raw_eth_call(w3, to, sig, types, values, output_types):
    """Deterministic mock for on-chain calls used by resolvers."""
    if "quoteExactInputSingle" in sig:
        return (1000_000_000, 0, 0, 0)
    if "calc_token_amount" in sig:
        return (99_000_000_000_000_000_000,)
    if "balances(uint256)" in sig:
        return (1_000_000_000_000_000_000_000_000,)
    if "totalSupply" in sig:
        return (3_000_000_000_000_000_000_000_000,)
    if "queryBatchSwap" in sig:
        return ([1_000_000_000_000_000_000, -1500_000_000],)
    if "getStrategiesWithBurnableShares" in sig:
        return ([STETH_STRATEGY], [10_000_000_000_000_000_000])
    if "strategyIsWhitelistedForDeposit" in sig:
        return (True,)
    if "underlyingToken" in sig:
        return (STETH_ADDR,)
    if "getDeposits" in sig:
        return ([STETH_STRATEGY], [10_000_000_000_000_000_000])
    if "getWithdrawalRequests" in sig:
        return ([100, 101],)
    if "getLastCheckpointIndex" in sig:
        return (500,)
    if "findCheckpointHints" in sig:
        return ([450, 451],)
    if "getReservesList" in sig:
        return ([USDC_ADDR, WETH_ADDR],)
    if "getReserveData" in sig:
        reserve = values[0] if values else ""
        atoken = MOCK_ATOKEN_USDC if reserve.lower() == USDC_ADDR.lower() else MOCK_ATOKEN_WETH
        return (
            0, 0, 0, 0, 0, 0, 0, 0,
            atoken,
            "0x" + "00" * 20,
            "0x" + "00" * 20,
            "0x" + "00" * 20,
            0, 0, 0,
        )
    if "positions(uint256)" in sig:
        return (
            0, "0x" + "00" * 20,
            USDC_ADDR, WETH_ADDR, 3000, -887220, 887220,
            1_000_000_000_000_000_000, 0, 0, 500_000, 100_000_000_000_000,
        )
    if "getPoolTokens" in sig:
        return ([USDC_ADDR, WETH_ADDR], [1_000_000_000_000, 500_000_000_000_000_000_000], 19_000_000)
    if "getQueuedWithdrawals" in sig:
        return (
            [(FROM_ADDRESS, MOCK_OPERATOR, FROM_ADDRESS, 0, 19_000_000,
              [STETH_STRATEGY], [10_000_000_000_000_000_000])],
            [[10_000_000_000_000_000_000]]
        )
    if "balanceOf" in sig:
        return (1_000_000_000,)
    return (0,)


def mock_urlopen(*args, **kwargs):
    """Mock The Graph API for Balancer pool lookup."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    pool_id = "0x" + "ab" * 32
    mock_resp.read.return_value = json.dumps({
        "data": {"pools": [{"id": pool_id}]}
    }).encode("utf-8")
    return mock_resp


def mock_pendle_requests(method, url, **kwargs):
    """Mock Pendle API responses for market lookup and quoting."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()

    if "/markets/active" in url:
        resp.json.return_value = {"markets": [{
            "name": "wstETH",
            "address": PENDLE_MARKET,
            "expiry": "2027-12-30T00:00:00.000Z",
            "pt": PENDLE_PT,
            "yt": PENDLE_YT,
            "sy": PENDLE_SY,
            "underlyingAsset": WETH_ADDR,
        }]}
    elif "/swapping-prices" in url:
        resp.json.return_value = {
            "underlyingTokenToPtRate": 1.2794624883868102,
            "ptToUnderlyingTokenRate": 0.7797912761860166,
            "underlyingTokenToYtRate": 27.321362785428178,
            "ytToUnderlyingTokenRate": 0.031144093200593925,
        }
    elif "/convert" in url:
        resp.json.return_value = {
            "action": "mint-py",
            "routes": [{"outputs": [{"token": PENDLE_PT, "amount": "12305739723467591"}]}],
        }
    else:
        resp.json.return_value = {}

    return resp


# Test cases

TEST_CASES = [
    # ── Transfers ──
    pytest.param(
        {"action": "transfer_native", "arguments": {"to": "alice.eth", "amount": "0.5"}},
        {"action": "transfer_native", "function_name": None, "target_contract": None,
         "args": {"to": ALICE_ADDR, "value": "500000000000000000"}, "value_nonzero": True},
        id="transfer_native",
    ),
    pytest.param(
        {"action": "transfer_erc20", "arguments": {"to": "alice.eth", "amount": "100", "asset": "USDC"}},
        {"action": "transfer_erc20", "function_name": "transfer", "target_contract": USDC_ADDR,
         "selector": "0xa9059cbb", "args": {"to": ALICE_ADDR, "value": "100000000"}},
        id="transfer_erc20",
    ),
    pytest.param(
        {"action": "transfer_erc721", "arguments": {"to": "alice.eth", "tokenId": 1234, "collection": "Bored Ape Yacht Club"}},
        {"action": "transfer_erc721", "function_name": "transferFrom", "target_contract": BAYC_ADDR,
         "selector": "0x23b872dd", "args": {"to": ALICE_ADDR, "tokenId": "1234"}},
        id="transfer_erc721_bayc",
    ),
    pytest.param(
        {"action": "transfer_erc721", "arguments": {"to": "alice.eth", "tokenId": 42, "collection": "CryptoPunks"}},
        {"action": "transfer_erc721", "function_name": "transferPunk", "target_contract": PUNKS_ADDR,
         "args": {"to": ALICE_ADDR, "tokenId": "42"}},
        id="transfer_erc721_cryptopunks",
    ),
    # ── Aave V3 ──
    pytest.param(
        {"action": "aave_supply", "arguments": {"asset": "USDC", "amount": "500"}},
        {"action": "aave_supply", "function_name": "supply", "target_contract": AAVE_POOL,
         "selector": "0x617ba037", "args": {"asset": USDC_ADDR, "amount": "500000000", "onBehalfOf": FROM_ADDRESS}},
        id="aave_supply",
    ),
    pytest.param(
        {"action": "aave_withdraw", "arguments": {"asset": "DAI", "amount": "250"}},
        {"action": "aave_withdraw", "function_name": "withdraw", "target_contract": AAVE_POOL,
         "args": {"asset": DAI_ADDR, "amount": "250000000000000000000", "to": FROM_ADDRESS}},
        id="aave_withdraw",
    ),
    pytest.param(
        {"action": "aave_withdraw", "arguments": {"asset": "USDC", "amount": "max"}},
        {"action": "aave_withdraw", "args": {"amount": UINT256_MAX}},
        id="aave_withdraw_max",
    ),
    pytest.param(
        {"action": "aave_borrow", "arguments": {"asset": "USDT", "amount": "1000"}},
        {"action": "aave_borrow", "function_name": "borrow", "target_contract": AAVE_POOL,
         "args": {"asset": USDT_ADDR, "amount": "1000000000", "interestRateMode": 2, "onBehalfOf": FROM_ADDRESS}},
        id="aave_borrow_default_variable",
    ),
    pytest.param(
        {"action": "aave_borrow", "arguments": {"asset": "USDT", "amount": "1000", "interest_rate_mode": "stable"}},
        {"should_be_none": True},
        id="aave_borrow_stable_rejected",
    ),
    pytest.param(
        {"action": "aave_supply", "arguments": {"asset": "ETH", "amount": "1"}},
        {"should_raise": "ETH \\(native\\) is not supported"},
        id="aave_supply_eth_rejected",
    ),
    pytest.param(
        {"action": "aave_repay", "arguments": {"asset": "USDC", "amount": "500"}},
        {"action": "aave_repay", "function_name": "repay", "target_contract": AAVE_POOL,
         "args": {"asset": USDC_ADDR, "amount": "500000000", "interestRateMode": 2, "onBehalfOf": FROM_ADDRESS}},
        id="aave_repay",
    ),
    pytest.param(
        {"action": "aave_repay", "arguments": {"asset": "DAI", "amount": "max"}},
        {"action": "aave_repay", "args": {"amount": UINT256_MAX}},
        id="aave_repay_max",
    ),
    # ── Lido ──
    pytest.param(
        {"action": "lido_stake", "arguments": {"amount": "1"}},
        {"action": "lido_stake", "function_name": "submit", "target_contract": LIDO_STETH,
         "value_nonzero": True, "args": {"value": "1000000000000000000"}},
        id="lido_stake",
    ),
    pytest.param(
        {"action": "lido_unstake", "arguments": {"amount": "15"}},
        {"action": "lido_unstake", "function_name": "requestWithdrawals", "target_contract": LIDO_WITHDRAWAL,
         "args": {"_owner": FROM_ADDRESS}},
        id="lido_unstake",
    ),
    # ── Uniswap V3 ──
    pytest.param(
        {"action": "uniswap_swap", "arguments": {"asset_in": "WETH", "asset_out": "USDC", "amount": "0.5"}},
        {"action": "uniswap_swap", "function_name": "exactInputSingle", "target_contract": UNISWAP_ROUTER,
         "args": {"tokenIn": WETH_ADDR, "tokenOut": USDC_ADDR, "amountIn": "500000000000000000", "fee": 500, "recipient": FROM_ADDRESS}},
        id="uniswap_swap",
    ),
    pytest.param(
        {"action": "uniswap_swap", "arguments": {"asset_in": "USDC", "asset_out": "USDT", "amount": "1000"}},
        {"action": "uniswap_swap", "args": {"fee": 100}},
        id="uniswap_swap_stable_pair",
    ),
    # ── Curve 3pool ──
    pytest.param(
        {"action": "curve_add_liquidity", "arguments": {"asset": "USDC", "amount": "100"}},
        {"action": "curve_add_liquidity", "function_name": "add_liquidity", "target_contract": CURVE_3POOL,
         "args": {"amounts": ["0", "100000000", "0"]}},
        id="curve_add_liquidity",
    ),
    pytest.param(
        {"action": "curve_remove_liquidity", "arguments": {"amount": "50"}},
        {"action": "curve_remove_liquidity", "function_name": "remove_liquidity", "target_contract": CURVE_3POOL,
         "args": {"amount": "50000000000000000000"}},
        id="curve_remove_liquidity",
    ),
    # ── WETH ──
    pytest.param(
        {"action": "weth_wrap", "arguments": {"amount": "2"}},
        {"action": "weth_wrap", "function_name": "deposit", "target_contract": WETH_ADDR,
         "value_nonzero": True, "args": {"value": "2000000000000000000"}},
        id="weth_wrap",
    ),
    pytest.param(
        {"action": "weth_unwrap", "arguments": {"amount": "1.5"}},
        {"action": "weth_unwrap", "function_name": "withdraw", "target_contract": WETH_ADDR,
         "args": {"wad": "1500000000000000000"}},
        id="weth_unwrap",
    ),
    # ── Compound V3 ──
    pytest.param(
        {"action": "compound_supply", "arguments": {"asset": "USDC", "amount": "1000"}},
        {"action": "compound_supply", "function_name": "supply", "target_contract": COMPOUND_COMET,
         "args": {"__token_address": USDC_ADDR, "amount": "1000000000"}},
        id="compound_supply",
    ),
    pytest.param(
        {"action": "compound_withdraw", "arguments": {"asset": "USDC", "amount": "500"}},
        {"action": "compound_withdraw", "function_name": "withdraw", "target_contract": COMPOUND_COMET,
         "args": {"__token_address": USDC_ADDR, "amount": "500000000"}},
        id="compound_withdraw",
    ),
    pytest.param(
        {"action": "compound_withdraw", "arguments": {"asset": "USDC", "amount": "max"}},
        {"action": "compound_withdraw", "args": {"amount": UINT256_MAX}},
        id="compound_withdraw_max",
    ),
    pytest.param(
        {"action": "compound_borrow", "arguments": {"asset": "USDC", "amount": "2000"}},
        {"action": "compound_borrow", "function_name": "withdraw", "target_contract": COMPOUND_COMET,
         "args": {"amount": "2000000000"}},
        id="compound_borrow",
    ),
    pytest.param(
        {"action": "compound_repay", "arguments": {"asset": "USDC", "amount": "1500"}},
        {"action": "compound_repay", "function_name": "supply", "target_contract": COMPOUND_COMET,
         "args": {"amount": "1500000000"}},
        id="compound_repay",
    ),
    # ── MakerDAO DSR ──
    pytest.param(
        {"action": "maker_deposit", "arguments": {"amount": "5000"}},
        {"action": "maker_deposit", "function_name": "deposit", "target_contract": MAKER_SDAI,
         "args": {"assets": "5000000000000000000000", "receiver": FROM_ADDRESS}},
        id="maker_deposit",
    ),
    pytest.param(
        {"action": "maker_redeem", "arguments": {"amount": "3000"}},
        {"action": "maker_redeem", "function_name": "redeem", "target_contract": MAKER_SDAI,
         "args": {"shares": "3000000000000000000000", "receiver": FROM_ADDRESS, "owner": FROM_ADDRESS}},
        id="maker_redeem",
    ),
    # ── Rocket Pool ──
    pytest.param(
        {"action": "rocketpool_stake", "arguments": {"amount": "5"}},
        {"action": "rocketpool_stake", "function_name": "deposit", "target_contract": ROCKETPOOL_DEPOSIT,
         "value_nonzero": True, "args": {"value": "5000000000000000000"}},
        id="rocketpool_stake",
    ),
    pytest.param(
        {"action": "rocketpool_unstake", "arguments": {"amount": "3"}},
        {"action": "rocketpool_unstake", "function_name": "burn", "target_contract": ROCKETPOOL_RETH,
         "args": {"_rethAmount": "3000000000000000000"}},
        id="rocketpool_unstake",
    ),
    # ── EigenLayer ──
    pytest.param(
        {"action": "eigenlayer_deposit", "arguments": {"asset": "stETH", "amount": "10"}},
        {"action": "eigenlayer_deposit", "function_name": "depositIntoStrategy", "target_contract": EIGENLAYER_SM,
         "args": {"__token_address": STETH_ADDR, "amount": "10000000000000000000"}},
        id="eigenlayer_deposit",
    ),
    # ── Balancer V2 ──
    pytest.param(
        {"action": "balancer_swap", "arguments": {"asset_in": "WETH", "asset_out": "USDC", "amount": "1"}},
        {"action": "balancer_swap", "function_name": "swap", "target_contract": BALANCER_VAULT,
         "args": {"__token_in_address": WETH_ADDR, "__token_out_address": USDC_ADDR,
                  "amount": "1000000000000000000", "recipient": FROM_ADDRESS, "sender": FROM_ADDRESS}},
        id="balancer_swap",
    ),
    # ── Aave V3 extras ──
    pytest.param(
        {"action": "aave_set_collateral", "arguments": {"asset": "WETH", "useAsCollateral": True}},
        {"action": "aave_set_collateral", "function_name": "setUserUseReserveAsCollateral",
         "target_contract": AAVE_POOL, "selector": "0x5a3b74b9",
         "args": {"asset": WETH_ADDR, "useAsCollateral": "True"}},
        id="aave_set_collateral",
    ),
    pytest.param(
        {"action": "aave_repay_with_atokens", "arguments": {"asset": "USDC", "amount": "1000"}},
        {"action": "aave_repay_with_atokens", "function_name": "repayWithATokens",
         "target_contract": AAVE_POOL, "selector": "0x2dad97d4",
         "args": {"asset": USDC_ADDR, "amount": "1000000000"}},
        id="aave_repay_with_atokens",
    ),
    # ── Compound V3 rewards ──
    pytest.param(
        {"action": "compound_claim_rewards", "arguments": {}},
        {"action": "compound_claim_rewards", "function_name": "claim", "target_contract": COMPOUND_REWARDS,
         "selector": "0xb7034f7e", "args": {"src": FROM_ADDRESS}},
        id="compound_claim_rewards",
    ),
    # ── EigenLayer extras ──
    pytest.param(
        {"action": "eigenlayer_undelegate", "arguments": {}},
        {"action": "eigenlayer_undelegate", "function_name": "undelegate", "target_contract": EIGENLAYER_DM,
         "selector": "0xda8be864", "args": {"staker": FROM_ADDRESS}},
        id="eigenlayer_undelegate",
    ),
    pytest.param(
        {"action": "eigenlayer_delegate", "arguments": {"operator": MOCK_OPERATOR}},
        {"action": "eigenlayer_delegate", "function_name": "delegateTo", "target_contract": EIGENLAYER_DM,
         "selector": "0xeea9064b", "args": {"operator": MOCK_OPERATOR}},
        id="eigenlayer_delegate",
    ),
    pytest.param(
        {"action": "eigenlayer_queue_withdrawals", "arguments": {}},
        {"action": "eigenlayer_queue_withdrawals", "function_name": "queueWithdrawals",
         "target_contract": EIGENLAYER_DM, "selector": "0x0dd8dd02"},
        id="eigenlayer_queue_withdrawals",
    ),
    pytest.param(
        {"action": "eigenlayer_complete_withdrawal", "arguments": {}},
        {"action": "eigenlayer_complete_withdrawal", "function_name": "completeQueuedWithdrawal",
         "target_contract": EIGENLAYER_DM, "selector": "0xe4cc3f90"},
        id="eigenlayer_complete_withdrawal",
    ),
    # ── Curve gauges ──
    pytest.param(
        {"action": "curve_mint_crv", "arguments": {"gauge_address": CURVE_3POOL_GAUGE}},
        {"action": "curve_mint_crv", "function_name": "mint", "target_contract": CURVE_MINTER,
         "selector": "0x6a627842", "args": {"gauge_addr": CURVE_3POOL_GAUGE}},
        id="curve_mint_crv",
    ),
    pytest.param(
        {"action": "curve_gauge_deposit", "arguments": {"gauge_address": CURVE_3POOL_GAUGE, "lp_token_address": CURVE_3POOL_LP, "amount": "100"}},
        {"action": "curve_gauge_deposit", "function_name": "deposit", "target_contract": CURVE_3POOL_GAUGE,
         "selector": "0xb6b55f25", "args": {"_value": "100000000000000000000"}},
        id="curve_gauge_deposit",
    ),
    pytest.param(
        {"action": "curve_gauge_withdraw", "arguments": {"gauge_address": CURVE_3POOL_GAUGE, "amount": "50"}},
        {"action": "curve_gauge_withdraw", "function_name": "withdraw", "target_contract": CURVE_3POOL_GAUGE,
         "selector": "0x2e1a7d4d", "args": {"_value": "50000000000000000000"}},
        id="curve_gauge_withdraw",
    ),
    # ── Lido Phase 2 ──
    pytest.param(
        {"action": "lido_claim_withdrawals", "arguments": {}},
        {"action": "lido_claim_withdrawals", "function_name": "claimWithdrawals",
         "target_contract": LIDO_WITHDRAWAL, "selector": "0xe3afe0a3",
         "args": {"_requestIds": [100, 101], "_hints": [450, 451]}},
        id="lido_claim_withdrawals",
    ),
    # ── Aave Phase 2 ──
    pytest.param(
        {"action": "aave_claim_rewards", "arguments": {}},
        {"action": "aave_claim_rewards", "function_name": "claimAllRewardsToSelf",
         "target_contract": AAVE_REWARDS, "selector": "0xbf90f63a"},
        id="aave_claim_rewards",
    ),
    # ── Uniswap V3 LP ──
    pytest.param(
        {"action": "uniswap_lp_mint", "arguments": {"asset_a": "USDC", "asset_b": "WETH", "amount_a": "3000", "amount_b": "1"}},
        {"action": "uniswap_lp_mint", "function_name": "mint", "target_contract": NFPM, "selector": "0x88316456"},
        id="uniswap_lp_mint",
    ),
    pytest.param(
        {"action": "uniswap_lp_collect", "arguments": {"token_id": "12345"}},
        {"action": "uniswap_lp_collect", "function_name": "collect", "target_contract": NFPM,
         "selector": "0xfc6f7865", "args": {"tokenId": "12345", "recipient": FROM_ADDRESS}},
        id="uniswap_lp_collect",
    ),
    pytest.param(
        {"action": "uniswap_lp_decrease", "arguments": {"token_id": "12345", "percentage": "50"}},
        {"action": "uniswap_lp_decrease", "function_name": "decreaseLiquidity", "target_contract": NFPM,
         "selector": "0x0c49ccbe", "args": {"tokenId": "12345", "liquidity": "500000000000000000"}},
        id="uniswap_lp_decrease",
    ),
    pytest.param(
        {"action": "uniswap_lp_increase", "arguments": {"token_id": "12345", "amount_a": "500", "amount_b": "0.5"}},
        {"action": "uniswap_lp_increase", "function_name": "increaseLiquidity", "target_contract": NFPM,
         "selector": "0x219f5d17", "args": {
             "tokenId": "12345",
             "amount0Desired": "500000000",       # 500 USDC (6 decimals, token0 from mock position)
             "amount1Desired": "500000000000000000",  # 0.5 WETH (18 decimals, token1 from mock position)
         }},
        id="uniswap_lp_increase",
    ),
    # ── Balancer V2 LP ──
    pytest.param(
        {"action": "balancer_join_pool", "arguments": {"asset": "USDC", "asset_b": "WETH", "amount": "1000"}},
        {"action": "balancer_join_pool", "function_name": "joinPool", "target_contract": BALANCER_VAULT,
         "selector": "0xb95cac28"},
        id="balancer_join_pool",
    ),
    pytest.param(
        {"action": "balancer_exit_pool", "arguments": {"asset": "USDC", "asset_b": "WETH", "amount": "100"}},
        {"action": "balancer_exit_pool", "function_name": "exitPool", "target_contract": BALANCER_VAULT,
         "selector": "0x8bdb3913"},
        id="balancer_exit_pool",
    ),
    # ── Pendle V2 ──
    pytest.param(
        {"action": "pendle_swap_token_for_pt", "arguments": {"asset": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_swap_token_for_pt", "function_name": "swapExactTokenForPt",
         "target_contract": PENDLE_ROUTER, "selector": "0xc81f847a",
         "args": {"asset": WETH_ADDR, "amount": "1000000000000000000", "market": PENDLE_MARKET}},
        id="pendle_swap_token_for_pt",
    ),
    pytest.param(
        {"action": "pendle_swap_pt_for_token", "arguments": {"asset_out": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_swap_pt_for_token", "function_name": "swapExactPtForToken",
         "target_contract": PENDLE_ROUTER, "selector": "0x594a88cc",
         "args": {"asset_out": WETH_ADDR, "market": PENDLE_MARKET}},
        id="pendle_swap_pt_for_token",
    ),
    pytest.param(
        {"action": "pendle_swap_token_for_yt", "arguments": {"asset": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_swap_token_for_yt", "function_name": "swapExactTokenForYt",
         "target_contract": PENDLE_ROUTER, "selector": "0xed48907e",
         "args": {"asset": WETH_ADDR, "market": PENDLE_MARKET}},
        id="pendle_swap_token_for_yt",
    ),
    pytest.param(
        {"action": "pendle_swap_yt_for_token", "arguments": {"asset_out": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_swap_yt_for_token", "function_name": "swapExactYtForToken",
         "target_contract": PENDLE_ROUTER, "selector": "0x05eb5327",
         "args": {"asset_out": WETH_ADDR, "market": PENDLE_MARKET}},
        id="pendle_swap_yt_for_token",
    ),
    pytest.param(
        {"action": "pendle_add_liquidity", "arguments": {"asset": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_add_liquidity", "function_name": "addLiquiditySingleToken",
         "target_contract": PENDLE_ROUTER, "selector": "0x12599ac6",
         "args": {"asset": WETH_ADDR, "market": PENDLE_MARKET}},
        id="pendle_add_liquidity",
    ),
    pytest.param(
        {"action": "pendle_remove_liquidity", "arguments": {"asset_out": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_remove_liquidity", "function_name": "removeLiquiditySingleToken",
         "target_contract": PENDLE_ROUTER, "selector": "0x60da0860",
         "args": {"asset_out": WETH_ADDR, "market": PENDLE_MARKET}},
        id="pendle_remove_liquidity",
    ),
    pytest.param(
        {"action": "pendle_mint_py", "arguments": {"asset": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_mint_py", "function_name": "mintPyFromToken",
         "target_contract": PENDLE_ROUTER, "selector": "0xd0f42385",
         "args": {"asset": WETH_ADDR, "yt_address": PENDLE_YT, "market": PENDLE_MARKET}},
        id="pendle_mint_py",
    ),
    pytest.param(
        {"action": "pendle_redeem_py", "arguments": {"asset_out": "WETH", "amount": "1", "market": "wstETH"}},
        {"action": "pendle_redeem_py", "function_name": "redeemPyToToken",
         "target_contract": PENDLE_ROUTER, "selector": "0x47f1de22",
         "args": {"asset_out": WETH_ADDR, "yt_address": PENDLE_YT, "market": PENDLE_MARKET}},
        id="pendle_redeem_py",
    ),
    pytest.param(
        {"action": "pendle_claim_rewards", "arguments": {
            "sys": [PENDLE_SY], "yts": [PENDLE_YT], "markets": [PENDLE_MARKET]}},
        {"action": "pendle_claim_rewards", "function_name": "redeemDueInterestAndRewards",
         "target_contract": PENDLE_ROUTER, "selector": "0xf7e375e8"},
        id="pendle_claim_rewards",
    ),
    # ── Error cases ──
    pytest.param(
        {"action": "nonexistent_action", "arguments": {"foo": "bar"}},
        {"should_be_none": True},
        id="error_invalid_action",
    ),
    pytest.param(
        {"action": None, "arguments": {}},
        {"should_be_none": True},
        id="error_null_action",
    ),
]


# Test function

@pytest.mark.parametrize("llm_output,expect", TEST_CASES)
def test_playbook_parity(engine, llm_output, expect):
    with patch("time.time", return_value=FIXED_TIME), \
         patch("defi_skills.engine.resolvers.core.time") as mock_time, \
         patch("defi_skills.engine.resolvers.common.raw_eth_call", side_effect=mock_raw_eth_call), \
         patch("defi_skills.engine.resolvers.balancer.urllib.request.urlopen", side_effect=mock_urlopen), \
         patch("defi_skills.engine.resolvers.pendle.requests.get", side_effect=lambda url, **kw: mock_pendle_requests("GET", url, **kw)), \
         patch("defi_skills.engine.resolvers.pendle.requests.post", side_effect=lambda url, **kw: mock_pendle_requests("POST", url, **kw)), \
         patch.dict("os.environ", {"THEGRAPH_API_KEY": "test-key"}):
        mock_time.time.return_value = FIXED_TIME

        import defi_skills.engine.resolvers.eigenlayer as _eigen_mod
        _eigen_mod._eigenlayer_strategy_cache = None

        if expect.get("should_raise"):
            with pytest.raises(ValueError, match=expect["should_raise"]):
                engine.build_payload(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)
            return

        try:
            payload = engine.build_payload(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)
        except (ValueError, KeyError):
            payload = None

        # Check None expected (covers both None return and ValueError raise)
        if expect.get("should_be_none"):
            assert payload is None, "expected None/error but got a payload"
            return

        assert payload is not None, "build_payload returned None or raised"

        # Check metadata
        for key in ("action", "function_name", "target_contract"):
            expected_val = expect.get(key)
            if expected_val is not None:
                assert payload.get(key) == expected_val, f"{key}: expected={expected_val!r}, got={payload.get(key)!r}"

        # Check argument values
        args = payload.get("arguments", {})
        for arg_key, expected_val in expect.get("args", {}).items():
            actual_val = args.get(arg_key)
            if isinstance(expected_val, list):
                actual_list = [str(v) for v in actual_val] if actual_val else actual_val
                expected_list = [str(v) for v in expected_val]
                assert actual_list == expected_list, f"args.{arg_key}: expected={expected_list}, got={actual_list}"
            else:
                assert str(actual_val) == str(expected_val), f"args.{arg_key}: expected={expected_val!r}, got={actual_val!r}"

        # Encode tx
        raw_tx = engine.encode_tx(payload, FROM_ADDRESS)

        # Native transfer: no calldata, just check value
        if expect.get("action") == "transfer_native":
            if expect.get("value_nonzero"):
                assert raw_tx is not None and str(raw_tx.get("value", "0")) != "0", "raw_tx.value should be nonzero"
            return

        assert raw_tx is not None, "encode_tx returned None"

        # Check calldata exists
        data = raw_tx.get("data", "")
        assert data and data != "0x", "raw_tx.data is empty"

        # Check selector
        expected_selector = expect.get("selector")
        if expected_selector:
            assert data[:10].lower() == expected_selector.lower(), f"selector: expected={expected_selector}, got={data[:10]}"

        # Check value for payable
        if expect.get("value_nonzero"):
            assert str(raw_tx.get("value", "0")) != "0", "raw_tx.value should be nonzero"


# build_transactions tests

APPROVE_SELECTOR = "0x095ea7b3"


class TestBuildTransactions:
    """Tests for PlaybookEngine.build_transactions() — the full pipeline."""

    def test_with_approval(self, engine):
        """aave_supply USDC should produce 1 approve + 1 action tx."""
        llm_output = {"action": "aave_supply", "arguments": {"asset": "USDC", "amount": "500"}}
        result = engine.build_transactions(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)

        assert result["success"] is True
        txs = result["transactions"]
        assert len(txs) == 2

        # First tx is approval
        assert txs[0]["type"] == "approval"
        assert txs[0]["token"] == USDC_ADDR
        assert txs[0]["spender"] == AAVE_POOL
        assert txs[0]["raw_tx"]["data"].startswith(APPROVE_SELECTOR)
        assert txs[0]["raw_tx"]["to"] == USDC_ADDR
        assert txs[0]["raw_tx"]["value"] == "0"

        # Second tx is action
        assert txs[1]["type"] == "action"
        assert txs[1]["action"] == "aave_supply"
        assert txs[1]["target_contract"] == AAVE_POOL
        assert txs[1]["raw_tx"]["to"] == AAVE_POOL

    def test_usdt_double_approve(self, engine):
        """aave_supply USDT should produce 2 approves (reset + max) + 1 action."""
        llm_output = {"action": "aave_supply", "arguments": {"asset": "USDT", "amount": "100"}}
        result = engine.build_transactions(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)

        assert result["success"] is True
        txs = result["transactions"]
        assert len(txs) == 3

        # First two are approvals for USDT
        assert txs[0]["type"] == "approval"
        assert txs[0]["token"] == USDT_ADDR
        assert txs[1]["type"] == "approval"
        assert txs[1]["token"] == USDT_ADDR

        # First approve encodes amount=0 (reset)
        # The calldata is: selector(4) + spender(32) + amount(32) = 68 bytes = 136 hex + "0x" prefix
        data0 = txs[0]["raw_tx"]["data"]
        assert data0.startswith(APPROVE_SELECTOR)
        # Last 64 hex chars = uint256 amount — should be all zeros for reset
        assert data0[-64:] == "0" * 64

        # Second approve encodes amount=UINT256_MAX
        data1 = txs[1]["raw_tx"]["data"]
        assert data1.startswith(APPROVE_SELECTOR)
        assert data1[-64:] == "f" * 64

        # Third is the action
        assert txs[2]["type"] == "action"
        assert txs[2]["action"] == "aave_supply"

    def test_no_approval(self, engine):
        """transfer_native should produce 1 action tx, no approvals."""
        llm_output = {
            "action": "transfer_native",
            "arguments": {"to": ALICE_ADDR, "amount": "0.1"},
        }
        result = engine.build_transactions(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)

        assert result["success"] is True
        txs = result["transactions"]
        assert len(txs) == 1
        assert txs[0]["type"] == "action"
        assert txs[0]["action"] == "transfer_native"

    def test_invalid_action(self, engine):
        """Unknown action should return success=False."""
        llm_output = {"action": "nonexistent_action", "arguments": {}}
        result = engine.build_transactions(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)

        assert result["success"] is False
        assert "error" in result

    def test_arguments_cleaned(self, engine):
        """Action tx arguments should not contain __ prefixed internal keys."""
        llm_output = {"action": "aave_supply", "arguments": {"asset": "USDC", "amount": "500"}}
        result = engine.build_transactions(llm_output, chain_id=CHAIN_ID, from_address=FROM_ADDRESS)

        assert result["success"] is True
        action_tx = result["transactions"][-1]
        for key in action_tx["arguments"]:
            assert not key.startswith("__"), f"Internal key '{key}' leaked into output"
