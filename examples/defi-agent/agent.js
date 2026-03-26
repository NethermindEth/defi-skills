/**
 * DeFi Agent — thin browser wallet relay.
 * Handles: wallet connect, balance reading, tx signing, session management.
 * All intelligence lives on the server.
 */

const TOKENS_BY_CHAIN = {
  1: {
    '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48': { symbol: 'USDC', decimals: 6 },
    '0xdAC17F958D2ee523a2206206994597C13D831ec7': { symbol: 'USDT', decimals: 6 },
    '0x6B175474E89094C44Da98b954EedeAC495271d0F': { symbol: 'DAI', decimals: 18 },
    '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2': { symbol: 'WETH', decimals: 18 },
    '0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84': { symbol: 'stETH', decimals: 18 },
    '0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0': { symbol: 'wstETH', decimals: 18 },
  },
  11155111: {
    '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8': { symbol: 'USDC', decimals: 6 },
    '0xaA8E23Fb1079EA71e0a56F48a2aA51851D8433D0': { symbol: 'USDT', decimals: 6 },
    '0xFF34B3d4Aee8ddCd6F9AFFFB6Fe49bD371b8a357': { symbol: 'DAI', decimals: 18 },
    '0x7b79995e5f793A07Bc00c21412e50Ecae098E7f9': { symbol: 'WETH', decimals: 18 },
    '0x779877A7B0D9E8603169DdbD7836e478b4624789': { symbol: 'LINK', decimals: 18 },
  },
};

const BALANCE_OF_SELECTOR = '0x70a08231';

class DeFiAgent {
  constructor(apiBase = '') {
    this.apiBase = apiBase;
    this.wallet = null;
    this.chainId = 1;
    this.balances = {};
    this.sessionId = null;
  }

  // ── Wallet ──

  async connectWallet() {
    if (!window.ethereum) throw new Error('No wallet detected. Install MetaMask or Rabby.');
    const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    this.wallet = accounts[0];
    const chainHex = await window.ethereum.request({ method: 'eth_chainId' });
    this.chainId = parseInt(chainHex, 16);
    window.ethereum.on('accountsChanged', (accs) => { this.wallet = accs[0] || null; });
    window.ethereum.on('chainChanged', (hex) => { this.chainId = parseInt(hex, 16); });
    return { address: this.wallet, chainId: this.chainId };
  }

  async switchChain(chainId) {
    if (!window.ethereum) return;
    try {
      await window.ethereum.request({
        method: 'wallet_switchEthereumChain',
        params: [{ chainId: '0x' + chainId.toString(16) }],
      });
      this.chainId = chainId;
    } catch (err) {
      if (err.code === 4902 && chainId === 11155111) {
        await window.ethereum.request({
          method: 'wallet_addEthereumChain',
          params: [{
            chainId: '0xaa36a7', chainName: 'Sepolia',
            rpcUrls: ['https://rpc.sepolia.org'],
            nativeCurrency: { name: 'ETH', symbol: 'ETH', decimals: 18 },
            blockExplorerUrls: ['https://sepolia.etherscan.io'],
          }],
        });
        this.chainId = chainId;
      } else {
        throw err;
      }
    }
  }

  async readBalances() {
    if (!this.wallet) return {};
    const balances = {};
    const tokens = TOKENS_BY_CHAIN[this.chainId] || TOKENS_BY_CHAIN[1];

    const ethHex = await window.ethereum.request({
      method: 'eth_getBalance', params: [this.wallet, 'latest'],
    });
    balances.ETH = formatBalance(ethHex, 18);

    const paddedAddr = '0x' + this.wallet.slice(2).toLowerCase().padStart(64, '0');
    const callData = BALANCE_OF_SELECTOR + paddedAddr.slice(2);

    await Promise.all(Object.entries(tokens).map(async ([addr, info]) => {
      try {
        const result = await window.ethereum.request({
          method: 'eth_call', params: [{ to: addr, data: callData }, 'latest'],
        });
        const bal = formatBalance(result, info.decimals);
        if (parseFloat(bal) > 0) balances[info.symbol] = bal;
      } catch (e) { /* skip */ }
    }));

    this.balances = balances;
    return balances;
  }

  getWalletState() {
    return { address: this.wallet, chain_id: this.chainId, balances: this.balances };
  }

  // ── Session ──

  async createSession() {
    const resp = await fetch(this.apiBase + '/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chain_id: this.chainId,
        wallet_state: this.wallet ? this.getWalletState() : undefined,
      }),
    });
    if (!resp.ok) throw new Error('Failed to create session');
    const data = await resp.json();
    this.sessionId = data.session_id;
    return data;
  }

  async sendMessage(message) {
    if (!this.sessionId) await this.createSession();
    const resp = await fetch(this.apiBase + '/session/' + this.sessionId + '/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        wallet_state: this.wallet ? this.getWalletState() : undefined,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || 'Message failed');
    }
    return await resp.json();
  }

  // ── Transaction signing ──

  async signTransaction(rawTx) {
    const params = { from: this.wallet, to: rawTx.to, data: rawTx.data };
    const value = BigInt(rawTx.value || '0');
    if (value > 0n) params.value = '0x' + value.toString(16);

    const txHash = await window.ethereum.request({
      method: 'eth_sendTransaction', params: [params],
    });

    for (let i = 0; i < 60; i++) {
      const receipt = await window.ethereum.request({
        method: 'eth_getTransactionReceipt', params: [txHash],
      });
      if (receipt) {
        if (receipt.status !== '0x1') throw new Error('Transaction reverted: ' + txHash);
        return { txHash, receipt };
      }
      await new Promise(r => setTimeout(r, 2000));
    }
    throw new Error('Receipt timeout for ' + txHash);
  }

  async signAll(transactions, onTxUpdate) {
    const results = [];
    for (let i = 0; i < transactions.length; i++) {
      const tx = transactions[i];
      onTxUpdate(i, 'signing');
      try {
        const { txHash } = await this.signTransaction(tx.raw_tx);
        results.push({ index: i, txHash, success: true });
        onTxUpdate(i, 'confirmed', txHash);
      } catch (err) {
        const rejected = /denied|rejected|ACTION_REJECTED/i.test(err.message);
        results.push({ index: i, error: err.message, success: false, rejected });
        onTxUpdate(i, rejected ? 'rejected' : 'failed', err.message);
        break;
      }
    }
    return results;
  }
}

function formatBalance(hexValue, decimals) {
  if (!hexValue || hexValue === '0x' || hexValue === '0x0') return '0';
  const raw = BigInt(hexValue);
  const divisor = BigInt(10 ** decimals);
  const whole = raw / divisor;
  const frac = raw % divisor;
  const fracStr = frac.toString().padStart(decimals, '0').slice(0, 6).replace(/0+$/, '');
  return fracStr ? whole + '.' + fracStr : '' + whole;
}
