"""Configuration management for defi-skills CLI."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".defi-skills"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Maps config JSON keys → environment variable names for API keys.
API_KEY_FIELDS = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "alchemy_api_key": "ALCHEMY_API_KEY",
    "etherscan_api_key": "ETHERSCAN_API_KEY",
    "oneinch_api_key": "ONEINCH_API_KEY",
}

ENV_FALLBACK = {
    "wallet_address": "WALLET_ADDRESS",
    "model": "LLM_MODEL",
    **API_KEY_FIELDS,
}

DEFAULTS = {
    "model": "claude-sonnet-4-6",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def inject_config_env() -> None:
    """Inject API keys from config.json into os.environ.

    Uses setdefault so explicit env vars (export) always take precedence.
    Must be called before load_dotenv() and before engine module imports.
    """
    cfg = load_config()
    for config_key, env_var in API_KEY_FIELDS.items():
        val = cfg.get(config_key)
        if val:
            os.environ.setdefault(env_var, val)


def mask_key(value: str) -> str:
    """Mask an API key for display: first 4 + '...' + last 4 chars."""
    if len(value) <= 10:
        return "****"
    return value[:4] + "..." + value[-4:]


def get_value(key: str) -> str | None:
    cfg = load_config()
    val = cfg.get(key)
    if val is not None:
        return str(val) if not isinstance(val, str) else val
    env_key = ENV_FALLBACK.get(key)
    if env_key:
        env_val = os.getenv(env_key)
        if env_val:
            return env_val
    return DEFAULTS.get(key)


def set_value(key: str, value) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def get_all() -> dict:
    """Return all config values with source info, including API key status."""
    cfg = load_config()
    result = {}

    # Core settings
    for key in ["wallet_address", "model"]:
        val = cfg.get(key)
        source = "config"
        if val is None:
            env_key = ENV_FALLBACK.get(key)
            if env_key:
                val = os.getenv(env_key)
                source = f"env ${env_key}"
        if val is None:
            val = DEFAULTS.get(key)
            source = "default"
        result[key] = {"value": val, "source": source}

    # API keys (masked)
    for config_key, env_var in API_KEY_FIELDS.items():
        val = cfg.get(config_key)
        source = "config"
        if val is None:
            val = os.getenv(env_var)
            source = f"env ${env_var}"
        if val:
            result[config_key] = {"value": mask_key(val), "source": source}
        else:
            result[config_key] = {"value": None, "source": "not set"}

    return result
