"""Populate token cache with popular ERC-20 tokens and ERC-721 collections."""

import argparse
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from defi_skills.engine.token_resolver import TokenResolver


# Popular ERC20 symbols to resolve and cache
ERC20_SYMBOLS = [
    "USDC", "USDT", "DAI", "WETH", "WBTC",
    "stETH", "wstETH", "rETH", "cbETH", "swETH",
    "LINK", "UNI", "AAVE", "MKR", "CRV",
    "LDO", "RPL", "COMP", "BAL", "SNX",
    "FRAX", "LUSD", "GHO", "PYUSD", "TUSD",
    "sDAI", "cbBTC", "EIGEN", "mETH", "PENDLE",
    "RLUSD", "EURC",
]

# Popular ERC721 collection names to search via Alchemy
ERC721_SEARCH_QUERIES = [
    "Bored Ape Yacht Club",
    "Mutant Ape Yacht Club",
    "Azuki",
    "Doodles",
    "Pudgy Penguins",
    "Moonbirds",
    "CloneX",
    "CryptoPunks",
    "Cool Cats",
    "World of Women",
]


def populate_erc20(resolver: TokenResolver, dry_run: bool = False) -> int:
    """Resolve and cache popular ERC20 tokens."""
    print("ERC20 Tokens")
    print("-" * 60)

    cached = 0
    skipped = 0
    failed = 0

    for symbol in ERC20_SYMBOLS:
        # Check if already cached
        if symbol.upper() in resolver.erc20_by_symbol:
            info = resolver.erc20_by_symbol[symbol.upper()]
            print(f"  CACHED  {symbol:10s} {info['address'][:14]}... ({info.get('name', '')})")
            cached += 1
            continue

        if dry_run:
            print(f"  WOULD   {symbol:10s} (not in cache)")
            skipped += 1
            continue

        # Resolve via API (1inch -> on-chain)
        info = resolver.resolve_erc20(symbol)
        if info:
            print(f"  ADDED   {symbol:10s} {info['address'][:14]}... ({info.get('name', '')})")
            cached += 1
        else:
            print(f"  FAIL    {symbol:10s} (not found via API)")
            failed += 1

        time.sleep(0.3)

    print(f"\n  Total: {cached} cached, {failed} failed, {skipped} skipped")
    return cached


def populate_erc721(resolver: TokenResolver, dry_run: bool = False) -> int:
    """Search and cache popular ERC721 collections."""
    print("\nERC721 Collections")
    print("-" * 60)

    cached = 0
    failed = 0
    skipped = 0

    for query in ERC721_SEARCH_QUERIES:
        # Check if already cached (by name match)
        key = query.lower().replace(" ", "")
        already = False
        for alias, info in resolver.erc721_collections.items():
            if alias == key or (info.get("name", "").lower().replace(" ", "") == key):
                print(f"  CACHED  {query:30s} {info['address'][:14]}... ({info.get('symbol', '')})")
                cached += 1
                already = True
                break

        if already:
            continue

        if dry_run:
            print(f"  WOULD   {query:30s} (not in cache)")
            skipped += 1
            continue

        # Search via Alchemy NFT API
        info = resolver.search_nft_collection(query)
        if info:
            resolver.add_collection(info)
            print(f"  ADDED   {query:30s} {info['address'][:14]}... ({info.get('symbol', '')})")
            cached += 1
        else:
            # Try direct metadata query if we know the address won't help here
            print(f"  FAIL    {query:30s} (not found via Alchemy search)")
            failed += 1

        time.sleep(0.3)

    print(f"\n  Total: {cached} cached, {failed} failed, {skipped} skipped")
    return cached


def main():
    parser = argparse.ArgumentParser(description="Populate token cache from live APIs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without making API calls")
    args = parser.parse_args()

    resolver = TokenResolver()

    print("=" * 60)
    print("Populating Token Cache")
    print("=" * 60)
    print(f"Cache: {resolver.cache_path}")
    print(f"Alchemy: {'available' if resolver.w3 else 'not configured'}")
    print(f"1inch:   {'available' if resolver.oneinch_api_key else 'not configured'}")
    print()

    erc20_count = populate_erc20(resolver, args.dry_run)
    erc721_count = populate_erc721(resolver, args.dry_run)

    print("\n" + "=" * 60)
    print(f"Done. {erc20_count} ERC20 tokens, {erc721_count} ERC721 collections in cache.")
    print(f"Cache saved to: {resolver.cache_path}")


if __name__ == "__main__":
    main()
