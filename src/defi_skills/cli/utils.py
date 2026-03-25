"""Shared display helpers for CLI commands."""


def fmt_amount(amount):
    """Format a human-readable token amount."""
    abs_amt = abs(amount)
    if abs_amt >= 1e15:
        return "unlimited"
    if abs_amt == int(abs_amt) and abs_amt < 1e12:
        return f"{int(abs_amt):,}"
    if abs_amt >= 1:
        return f"{abs_amt:,.6f}".rstrip("0").rstrip(".")
    return f"{abs_amt:.8f}".rstrip("0").rstrip(".")
