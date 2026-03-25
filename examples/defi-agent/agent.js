// DeFi Agent — client-side orchestration for the defi-skills engine.

// Major ERC-20 tokens on Ethereum mainnet (address → symbol, decimals)
const TOKENS = {
  '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48': { symbol: 'USDC', decimals: 6 },
  '0xdAC17F958D2ee523a2206206994597C13D831ec7': { symbol: 'USDT', decimals: 6 },
  '0x6B175474E89094C44Da98b954EedeAC495271d0F': { symbol: 'DAI', decimals: 18 },
  '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2': { symbol: 'WETH', decimals: 18 },
  '0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84': { symbol: 'stETH', decimals: 18 },
  '0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0': { symbol: 'wstETH', decimals: 18 },
};

const BALANCE_OF_SELECTOR = '0x70a08231';

class DeFiAgent {
  constructor(apiBase = '') {
    this.apiBase = apiBase;
    this.wallet = null;
    this.chainId = null;
    this.balances = {};
    this.history = [];         // agent event log
    this.conversation = [];    // message history for multi-turn
    this.listeners = [];
  }

  // Event system

  onEvent(callback) {
    this.listeners.push(callback);
  }

  emit(type, data) {
    const event = { type, data, timestamp: Date.now() };
    this.history.push(event);
    this.listeners.forEach(cb => cb(event));
  }

  // Wallet connection

  async connectWallet() {
    if (!window.ethereum) {
      throw new Error('No wallet detected. Install MetaMask or Rabby.');
    }

    const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    this.wallet = accounts[0];

    const chainHex = await window.ethereum.request({ method: 'eth_chainId' });
    this.chainId = parseInt(chainHex, 16);

    // Listen for account/chain changes
    window.ethereum.on('accountsChanged', (accs) => {
      this.wallet = accs[0] || null;
      this.emit('wallet_changed', { address: this.wallet });
    });
    window.ethereum.on('chainChanged', (hex) => {
      this.chainId = parseInt(hex, 16);
      this.emit('chain_changed', { chainId: this.chainId });
    });

    this.emit('connected', { address: this.wallet, chainId: this.chainId });
    return { address: this.wallet, chainId: this.chainId };
  }

  // Portfolio reading

  async readPortfolio() {
    if (!this.wallet) throw new Error('Wallet not connected');

    const balances = {};

    // ETH balance
    const ethHex = await window.ethereum.request({
      method: 'eth_getBalance',
      params: [this.wallet, 'latest'],
    });
    balances.ETH = this.formatBalance(ethHex, 18);

    // ERC-20 balances via eth_call (balanceOf selector + padded address)
    const paddedAddr = '0x' + this.wallet.slice(2).toLowerCase().padStart(64, '0');
    const callData = BALANCE_OF_SELECTOR + paddedAddr.slice(2);

    const calls = Object.entries(TOKENS).map(async ([addr, info]) => {
      try {
        const result = await window.ethereum.request({
          method: 'eth_call',
          params: [{ to: addr, data: callData }, 'latest'],
        });
        const bal = this.formatBalance(result, info.decimals);
        if (parseFloat(bal) > 0) {
          balances[info.symbol] = bal;
        }
      } catch (e) {
        // Token might not exist or call failed — skip
      }
    });

    await Promise.all(calls);
    this.balances = balances;
    this.emit('portfolio', { balances });
    return balances;
  }

  formatBalance(hexValue, decimals) {
    if (!hexValue || hexValue === '0x' || hexValue === '0x0') return '0';
    const raw = BigInt(hexValue);
    const divisor = BigInt(10 ** decimals);
    const whole = raw / divisor;
    const frac = raw % divisor;
    const fracStr = frac.toString().padStart(decimals, '0').slice(0, 6).replace(/0+$/, '');
    return fracStr ? `${whole}.${fracStr}` : `${whole}`;
  }

  // Intent parsing

  /** Conversational LLM call: returns reply + optional goals */
  async parseIntent(message) {
    this.emit('parsing', { message });

    this.conversation.push({ role: 'user', content: message });

    const resp = await fetch(`${this.apiBase}/intent`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        wallet_address: this.wallet,
        balances: this.balances,
        history: this.conversation.slice(-10),
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || 'Intent parsing failed');
    }

    const result = await resp.json();
    this.conversation.push({ role: 'assistant', content: result.reply });
    this.emit('intent_parsed', result);
    return result;
  }

  /** Validate an action exists and get its schema */
  async validateStep(step) {
    this.emit('validating', { action: step.action });

    const resp = await fetch(`${this.apiBase}/actions/${step.action}`);
    if (!resp.ok) {
      throw new Error(`Unknown action: ${step.action}`);
    }

    const schema = await resp.json();
    this.emit('validated', { action: step.action, schema });
    return schema;
  }

  /** Build unsigned transactions via the deterministic engine */
  async buildStep(step) {
    this.emit('building', { action: step.action, arguments: step.arguments });

    const resp = await fetch(`${this.apiBase}/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: step.action,
        arguments: step.arguments,
        from_address: this.wallet,
        chain_id: 1,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `Build failed for ${step.action}`);
    }

    const result = await resp.json();
    this.emit('built', { action: step.action, transactions: result.transactions });
    return result;
  }

  // Goal-based orchestration

  /**
   * Parse intent into goals — does NOT build transactions yet.
   * Returns { goals: [...], reply: string }
   */
  async plan(message) {
    const intent = await this.parseIntent(message);

    // Normalize: server may return "goals" or LLM might fall back to "steps"
    intent.goals = intent.goals || intent.steps || [];
    if (intent.goals.length === 0) {
      this.emit('reply', { reply: intent.reply });
      return { goals: [], reply: intent.reply };
    }

    // Validate each goal's action exists
    for (const goal of intent.goals) {
      await this.validateStep(goal);
    }

    this.emit('plan_ready', { goals: intent.goals, reply: intent.reply });
    return { goals: intent.goals, reply: intent.reply };
  }

  /**
   * Reactive execution loop: build → execute → observe → resolve next.
   * Goals with depends_on wait until their dependency completes.
   * Calls onGoalUpdate(goalIndex, status, data) for UI updates.
   */
  async executeGoals(goals, onGoalUpdate) {
    const completed = new Set();   // goal ids that finished
    const results = [];
    const goalOrder = this.resolveOrder(goals);

    for (const goalIdx of goalOrder) {
      const goal = goals[goalIdx];
      const goalId = goal.id || `g${goalIdx + 1}`;

      onGoalUpdate(goalIdx, 'building', {});

      // If this goal depends on another, refresh portfolio first
      // so "max" resolves to actual post-tx balance
      if (goal.depends_on && completed.has(goal.depends_on)) {
        onGoalUpdate(goalIdx, 'observing', {});
        await this.readPortfolio();
        this.emit('portfolio_refreshed', { before: goalId, balances: this.balances });
      }

      // Build transactions for this goal
      let buildResult;
      try {
        buildResult = await this.buildStep(goal);
      } catch (err) {
        onGoalUpdate(goalIdx, 'failed', { error: err.message });
        results.push({ goalId, goalIdx, success: false, error: err.message, txResults: [] });
        this.emit('goal_failed', { goalId, error: err.message });
        break;
      }

      const txs = buildResult.transactions || [];
      const txResults = [];

      // Execute each transaction for this goal (approvals + action)
      for (let i = 0; i < txs.length; i++) {
        const tx = txs[i];
        const meta = {
          goalId,
          goalIdx,
          txIndex: i,
          txTotal: txs.length,
          type: tx.type,
          action: goal.action,
        };

        onGoalUpdate(goalIdx, 'signing', { txIndex: i, txTotal: txs.length, type: tx.type });

        try {
          const res = await this.signAndSend(tx.raw_tx, meta);
          txResults.push({ ...meta, txHash: res.txHash, success: true });
        } catch (err) {
          const rejected = /denied|rejected|ACTION_REJECTED/i.test(err.message);
          txResults.push({ ...meta, error: err.message, success: false, rejected });
          onGoalUpdate(goalIdx, 'failed', { error: err.message, rejected });
          results.push({ goalId, goalIdx, success: false, txResults });
          this.emit('execution_stopped', { goalId, error: err.message });
          // Stop entire execution on failure
          this.emit('execution_complete', { results });
          return results;
        }
      }

      completed.add(goalId);
      onGoalUpdate(goalIdx, 'confirmed', { txResults });
      results.push({ goalId, goalIdx, success: true, txResults });
      this.emit('goal_complete', { goalId, txResults });
    }

    this.emit('execution_complete', { results });
    return results;
  }

  /** Topological sort of goals by depends_on. Independent goals keep original order. */
  resolveOrder(goals) {
    const idToIdx = {};
    goals.forEach((g, i) => { idToIdx[g.id || `g${i + 1}`] = i; });

    const visited = new Set();
    const order = [];

    const visit = (idx) => {
      if (visited.has(idx)) return;
      visited.add(idx);
      const goal = goals[idx];
      if (goal.depends_on && idToIdx[goal.depends_on] !== undefined) {
        visit(idToIdx[goal.depends_on]);
      }
      order.push(idx);
    };

    goals.forEach((_, i) => visit(i));
    return order;
  }

  // Transaction signing

  /** Sign a single raw transaction and wait for receipt */
  async signAndSend(rawTx, meta = {}) {
    this.emit('signing', { ...meta, rawTx });

    const txParams = {
      from: this.wallet,
      to: rawTx.to,
      data: rawTx.data,
    };

    const value = BigInt(rawTx.value || '0');
    if (value > 0n) {
      txParams.value = '0x' + value.toString(16);
    }

    const txHash = await window.ethereum.request({
      method: 'eth_sendTransaction',
      params: [txParams],
    });

    this.emit('tx_sent', { ...meta, txHash });

    const receipt = await this.waitForReceipt(txHash);
    const success = receipt.status === '0x1';

    this.emit(success ? 'confirmed' : 'tx_failed', {
      ...meta,
      txHash,
      receipt,
      etherscan: `https://etherscan.io/tx/${txHash}`,
    });

    if (!success) {
      throw new Error(`Transaction reverted: ${txHash}`);
    }

    return { txHash, receipt };
  }

  /** Poll for transaction receipt */
  async waitForReceipt(txHash, maxAttempts = 60) {
    for (let i = 0; i < maxAttempts; i++) {
      const receipt = await window.ethereum.request({
        method: 'eth_getTransactionReceipt',
        params: [txHash],
      });
      if (receipt) return receipt;
      await new Promise(r => setTimeout(r, 2000));
    }
    throw new Error(`Receipt timeout for ${txHash}`);
  }

  // Session log export

  exportLog() {
    return {
      session: {
        wallet: this.wallet,
        chainId: this.chainId,
        balances: this.balances,
        timestamp: new Date().toISOString(),
      },
      events: this.history,
    };
  }
}
