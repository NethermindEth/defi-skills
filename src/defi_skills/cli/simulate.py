"""Anvil fork management and transaction simulation."""

import os
import shutil
import subprocess
import time
from pathlib import Path

from web3 import Web3

from defi_skills.cli import config as cfg

FOUNDRY_BIN = Path.home() / ".foundry/bin"

# ERC-20 Transfer(address indexed from, address indexed to, uint256 value)
TRANSFER_TOPIC = bytes.fromhex(
    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Minimal ERC-20 ABI for symbol() and decimals()
ERC20_ABI = [
    {"type": "function", "name": "symbol", "inputs": [],
     "outputs": [{"type": "string"}], "stateMutability": "view"},
    {"type": "function", "name": "decimals", "inputs": [],
     "outputs": [{"type": "uint8"}], "stateMutability": "view"},
]

# Some tokens (e.g. MKR) return bytes32 for symbol()
ERC20_BYTES32_ABI = [
    {"type": "function", "name": "symbol", "inputs": [],
     "outputs": [{"type": "bytes32"}], "stateMutability": "view"},
    {"type": "function", "name": "decimals", "inputs": [],
     "outputs": [{"type": "uint8"}], "stateMutability": "view"},
]


def anvil_path():
    return shutil.which("anvil") or str(FOUNDRY_BIN / "anvil")


def is_anvil(w3):
    """Check if the connected node is Anvil by calling an Anvil-specific RPC method."""
    try:
        resp = w3.provider.make_request("anvil_nodeInfo", [])
        return "error" not in resp
    except Exception:
        return False


def parse_value(raw_value):
    """Parse a transaction value that may be decimal string, int, or hex string."""
    if isinstance(raw_value, int):
        return raw_value
    s = str(raw_value)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


def get_token_info(w3, token_address, cache):
    """Get (symbol, decimals) for a token address. Uses cache to avoid repeated calls."""
    key = token_address.lower()
    if key in cache:
        return cache[key]

    cs = Web3.to_checksum_address(token_address)
    try:
        c = w3.eth.contract(address=cs, abi=ERC20_ABI)
        symbol = c.functions.symbol().call()
        decimals = c.functions.decimals().call()
        cache[key] = (symbol, decimals)
        return symbol, decimals
    except Exception:
        pass

    # Fallback for bytes32 symbol tokens (MKR, etc.)
    try:
        c = w3.eth.contract(address=cs, abi=ERC20_BYTES32_ABI)
        raw = c.functions.symbol().call()
        symbol = raw.rstrip(b"\x00").decode()
        decimals = c.functions.decimals().call()
        cache[key] = (symbol, decimals)
        return symbol, decimals
    except Exception:
        pass

    # Unknown token
    short = f"{cs[:6]}..{cs[-4:]}"
    cache[key] = (short, 18)
    return short, 18


def decode_transfers(w3, receipt, token_cache):
    """Decode ERC-20 Transfer events from receipt logs."""
    transfers = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or len(topics) < 3:
            continue

        if topics[0] != TRANSFER_TOPIC:
            continue

        token_addr = Web3.to_checksum_address(log["address"])
        from_addr = Web3.to_checksum_address("0x" + topics[1].hex()[-40:])
        to_addr = Web3.to_checksum_address("0x" + topics[2].hex()[-40:])

        data = log.get("data", b"")
        if not data or data == b"" or data == "0x":
            amount_raw = 0
        elif isinstance(data, (bytes, bytearray)):
            amount_raw = int.from_bytes(data, "big")
        else:
            clean = str(data)
            if clean.startswith("0x"):
                clean = clean[2:]
            amount_raw = int(clean, 16) if clean else 0

        symbol, decimals = get_token_info(w3, token_addr, token_cache)
        amount_display = amount_raw / (10 ** decimals)

        transfers.append({
            "token": token_addr,
            "symbol": symbol,
            "from": from_addr,
            "to": to_addr,
            "amount_raw": str(amount_raw),
            "amount": amount_display,
        })

    return transfers


def compute_balance_changes(sim_results, from_cs):
    """Compute net token/ETH balance changes for the user across all steps."""
    changes = {}  # symbol -> net change

    for r in sim_results:
        if r["status"] != "success":
            continue

        # ETH sent as tx value
        eth_value = r.get("eth_value", 0)
        if eth_value > 0:
            changes.setdefault("ETH", 0.0)
            changes["ETH"] -= eth_value / 1e18

        # Token transfers
        for t in r.get("transfers", []):
            sym = t["symbol"]
            amount = t["amount"]
            changes.setdefault(sym, 0.0)
            if t["from"].lower() == from_cs.lower():
                changes[sym] -= amount
            if t["to"].lower() == from_cs.lower():
                changes[sym] += amount

    # Drop near-zero entries
    return {k: v for k, v in changes.items() if abs(v) > 1e-12}


def ensure_anvil():
    """Install Foundry if anvil is not available."""
    if shutil.which("anvil") or (FOUNDRY_BIN / "anvil").exists():
        return
    try:
        subprocess.run(
            ["bash", "-c", "curl -L https://foundry.paradigm.xyz | bash && ~/.foundry/bin/foundryup"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        raise SystemExit(
            "Failed to install Foundry. "
            "Install manually: https://book.getfoundry.sh/getting-started/installation"
        )
    if not shutil.which("anvil") and not (FOUNDRY_BIN / "anvil").exists():
        raise SystemExit("Foundry installed but anvil not in PATH. Add ~/.foundry/bin to PATH.")


def resolve_rpc(chain_id: int = 1):
    """Return (rpc_url, needs_anvil). Checks local node first, then Alchemy."""
    local_url = "http://127.0.0.1:8545"
    try:
        if Web3(Web3.HTTPProvider(local_url)).is_connected():
            return local_url, False
    except Exception:
        pass

    from defi_skills.engine.chains import get_rpc_url
    import os
    if os.getenv("ALCHEMY_API_KEY"):
        try:
            return get_rpc_url(chain_id), True
        except ValueError:
            pass

    return None, False


def anvil_rpc(w3, method, params):
    """Call an Anvil RPC method and raise on error."""
    resp = w3.provider.make_request(method, params)
    if "error" in resp:
        raise RuntimeError(f"{method} failed: {resp['error']}")
    return resp


def run_simulation(build_result, from_address, chain_id: int = 1):
    """Execute built transactions on a fork. Returns simulation result dict."""
    rpc_url, needs_anvil = resolve_rpc(chain_id)
    if rpc_url is None:
        return {
            "success": False,
            "error": (
                "No RPC available. Either start a local Anvil node on port 8545, "
                "or run: defi-skills config set alchemy_api_key <YOUR_KEY>"
            ),
        }

    proc = None
    try:
        if needs_anvil:
            ensure_anvil()
            proc = subprocess.Popen(
                [anvil_path(), "--fork-url", rpc_url, "--port", "0",
                 "--auto-impersonate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            # Anvil prints "Listening on 127.0.0.1:<port>" to stdout.
            # Use raw os.read() to avoid buffering issues with readline().
            port = None
            buf = b""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    buf += proc.stdout.read()
                    return {
                        "success": False,
                        "error": f"Anvil exited unexpectedly. {buf.decode(errors='replace').strip() or 'No output.'}",
                    }
                try:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if chunk:
                        buf += chunk
                        text = buf.decode(errors="replace")
                        if "Listening on" in text:
                            for line in text.split("\n"):
                                if "Listening on" in line:
                                    addr_part = line.strip().rsplit(" ", 1)[-1]
                                    port = int(addr_part.rsplit(":", 1)[-1])
                                    break
                            break
                except OSError:
                    time.sleep(0.1)
            if port is None:
                return {
                    "success": False,
                    "error": f"Anvil failed to start within 30s. {buf.decode(errors='replace').strip() or ''}".strip(),
                }
            rpc_url = f"http://127.0.0.1:{port}"

        w3 = Web3(Web3.HTTPProvider(rpc_url))

        # Verify the node supports Anvil RPC methods
        if not needs_anvil and not is_anvil(w3):
            return {
                "success": False,
                "error": (
                    "Local node on port 8545 is not Anvil. "
                    "Simulation requires Anvil. Start one with: "
                    "anvil --fork-url <RPC_URL> --auto-impersonate"
                ),
            }

        from_cs = Web3.to_checksum_address(from_address)
        anvil_rpc(w3, "anvil_impersonateAccount", [from_cs])
        anvil_rpc(w3, "anvil_setBalance", [from_cs, hex(10**20)])

        txs = build_result.get("transactions", [])
        sim_results = []
        token_cache = {}  # address -> (symbol, decimals)

        for i, tx_entry in enumerate(txs):
            raw = tx_entry.get("raw_tx", {})
            try:
                eth_value = parse_value(raw.get("value", "0"))
                tx_params = {
                    "from": from_cs,
                    "to": Web3.to_checksum_address(raw["to"]),
                    "value": eth_value,
                    "data": raw.get("data", "0x"),
                }
                # Estimate gas instead of hardcoding
                try:
                    estimated = w3.eth.estimate_gas(tx_params)
                    tx_params["gas"] = int(estimated * 1.2)  # 20% buffer
                except Exception:
                    tx_params["gas"] = 3_000_000  # fallback

                tx_hash = w3.eth.send_transaction(tx_params)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

                transfers = decode_transfers(w3, receipt, token_cache)

                entry = {
                    "step": i + 1,
                    "type": tx_entry.get("type"),
                    "to": raw["to"],
                    "status": "success" if receipt["status"] == 1 else "reverted",
                    "gas_used": receipt["gasUsed"],
                    "tx_hash": receipt["transactionHash"].hex(),
                    "eth_value": eth_value,
                    "transfers": transfers,
                }
                if "plan_step" in tx_entry:
                    entry["plan_step"] = tx_entry["plan_step"]
                    entry["plan_action"] = tx_entry.get("plan_action", "")
                sim_results.append(entry)
            except Exception as e:
                entry = {
                    "step": i + 1,
                    "type": tx_entry.get("type"),
                    "to": raw["to"],
                    "status": "failed",
                    "error": str(e),
                    "eth_value": 0,
                    "transfers": [],
                }
                if "plan_step" in tx_entry:
                    entry["plan_step"] = tx_entry["plan_step"]
                    entry["plan_action"] = tx_entry.get("plan_action", "")
                sim_results.append(entry)
                break

        all_ok = all(r["status"] == "success" for r in sim_results)
        balance_changes = compute_balance_changes(sim_results, from_cs)

        return {
            "success": all_ok,
            "simulation": sim_results,
            "balance_changes": balance_changes,
            "transactions": txs,
        }
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
