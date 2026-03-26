import pytest
from defi_skills.engine.chains import (
    ChainConfig,
    get_chain_config,
    get_rpc_url,
    supported_chain_ids,
    get_approve_reset_tokens,
)


def test_mainnet_config():
    cfg = get_chain_config(1)
    assert cfg.chain_id == 1
    assert cfg.name == "Ethereum Mainnet"
    assert cfg.ens_supported is True
    assert cfg.is_testnet is False


def test_sepolia_config():
    cfg = get_chain_config(11155111)
    assert cfg.chain_id == 11155111
    assert cfg.name == "Sepolia"
    assert cfg.ens_supported is False
    assert cfg.is_testnet is True
    assert cfg.oneinch_chain_id is None


def test_unknown_chain_raises():
    with pytest.raises(ValueError, match="Unsupported chain"):
        get_chain_config(999999)


def test_supported_chain_ids():
    ids = supported_chain_ids()
    assert 1 in ids
    assert 11155111 in ids


def test_rpc_url_mainnet(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    monkeypatch.delenv("ALCHEMY_URL", raising=False)
    monkeypatch.delenv("ALCHEMY_URL_MAINNET", raising=False)
    url = get_rpc_url(1)
    assert "eth-mainnet" in url
    assert "test-key" in url


def test_rpc_url_sepolia(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "test-key")
    monkeypatch.delenv("ALCHEMY_URL_SEPOLIA", raising=False)
    url = get_rpc_url(11155111)
    assert "eth-sepolia" in url
    assert "test-key" in url


def test_approve_reset_tokens_mainnet():
    tokens = get_approve_reset_tokens(1)
    assert "0xdAC17F958D2ee523a2206206994597C13D831ec7" in tokens


def test_approve_reset_tokens_sepolia():
    tokens = get_approve_reset_tokens(11155111)
    assert len(tokens) == 0
