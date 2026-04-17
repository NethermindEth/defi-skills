"""Hybrid JSON formatter for playbooks.

Collapses short objects/arrays to single lines, expands longer ones.
Preserves key order. Run without arguments to format all playbooks in place.
"""

import json
import sys
from pathlib import Path

WIDTH = 100
INDENT = 2


def format_value(value, indent_level: int = 0) -> str:
    """Format a JSON value with hybrid wrapping."""
    inline = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    available = WIDTH - (indent_level * INDENT)

    if not isinstance(value, (dict, list)):
        return inline

    if len(inline) <= available:
        return inline

    pad = " " * ((indent_level + 1) * INDENT)
    close_pad = " " * (indent_level * INDENT)

    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [
            f"{pad}{json.dumps(k, ensure_ascii=False)}: {format_value(v, indent_level + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + f"\n{close_pad}}}"

    if not value:
        return "[]"
    items = [f"{pad}{format_value(v, indent_level + 1)}" for v in value]
    return "[\n" + ",\n".join(items) + f"\n{close_pad}]"


def dumps(data) -> str:
    """Serialize a Python object to a hybrid-formatted JSON string."""
    return format_value(data) + "\n"


def format_file(path: Path) -> tuple[int, int]:
    """Format a JSON file in place. Returns (before_lines, after_lines)."""
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    formatted = dumps(data)
    before = original.count("\n")
    after = formatted.count("\n")
    path.write_text(formatted, encoding="utf-8")
    return before, after


def main():
    args = sys.argv[1:]
    if args:
        paths = [Path(a) for a in args]
    else:
        root = Path(__file__).resolve().parent.parent
        paths = sorted((root / "src" / "defi_skills" / "data" / "playbooks").glob("*.json"))

    total_before = total_after = 0
    for path in paths:
        before, after = format_file(path)
        total_before += before
        total_after += after
        delta = after - before
        sign = "+" if delta > 0 else ""
        print(f"  {path.name:35s}  {before:4d} -> {after:4d}  ({sign}{delta})")

    pct = (1 - total_after / total_before) * 100 if total_before else 0
    print(f"\nTotal: {total_before} -> {total_after} lines ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
