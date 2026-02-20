#!/usr/bin/env python3
"""Fetch and update pricing.json from Anthropic pricing documentation.

Fetches the pricing page, parses the model pricing table, and updates
the local pricing.json with current rates.

Called by the tusk wrapper:
    tusk pricing-update [--dry-run] [--cache-tier 1h]
"""

import argparse
import json
import re
import ssl
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PRICING_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"

EXPECTED_HEADERS = {
    "model",
    "base input tokens",
    "5m cache writes",
    "1h cache writes",
    "cache hits & refreshes",
    "output tokens",
}


def resolve_pricing_path() -> Path:
    """Resolve pricing.json path (same logic as load_pricing() in tusk-session-stats.py)."""
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / "pricing.json",
        script_dir.parent / "pricing.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


class TableParser(HTMLParser):
    """Extract all HTML tables as lists of rows (each row = list of cell texts)."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_cell = False
        self._cell_text = ""
        self._current_row: list[str] = []
        self._current_table: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._current_row = []
        elif self._in_table and tag in ("th", "td"):
            self._in_cell = True
            self._cell_text = ""

    def handle_endtag(self, tag):
        if tag == "table" and self._in_table:
            self._in_table = False
            if self._current_table:
                self.tables.append(self._current_table)
        elif self._in_table and tag == "tr":
            if self._current_row:
                self._current_table.append(self._current_row)
        elif self._in_table and tag in ("th", "td"):
            self._in_cell = False
            self._current_row.append(self._cell_text.strip())

    def handle_data(self, data):
        if self._in_cell:
            self._cell_text += data


def find_pricing_table(
    tables: list[list[list[str]]],
) -> list[list[str]] | None:
    """Find the table whose header row contains the expected pricing columns."""
    for table in tables:
        if not table:
            continue
        header = {h.lower().strip() for h in table[0]}
        if EXPECTED_HEADERS.issubset(header):
            return table
    return None


def parse_price(text: str) -> float | None:
    """Extract numeric price from text like '$5 / MTok' or '$0.50 / MTok'."""
    match = re.search(r"\$?([\d.]+)", text)
    if match:
        return float(match.group(1))
    return None


def model_name_to_id(name: str) -> str:
    """Convert display name to model ID.

    Strips parentheticals (e.g. '(deprecated)'), lowercases,
    replaces spaces and dots with hyphens.
    """
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    name = name.lower()
    name = name.replace(".", "-")
    name = name.replace(" ", "-")
    return name


def is_deprecated(name: str) -> bool:
    """Check if a model name is marked deprecated."""
    return "deprecated" in name.lower()


def fetch_pricing_page() -> str:
    """Fetch the Anthropic pricing page HTML."""
    req = Request(
        PRICING_URL,
        headers={"User-Agent": "tusk-pricing-update/1.0"},
    )
    # Try default SSL context first
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            print(
                f"Error: Could not fetch {PRICING_URL}: {e.reason}",
                file=sys.stderr,
            )
            sys.exit(1)
    except HTTPError as e:
        print(f"Error: HTTP {e.code} fetching {PRICING_URL}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print(f"Error: Timeout fetching {PRICING_URL}", file=sys.stderr)
        sys.exit(1)

    # SSL verification failed — retry with macOS system certificate bundle
    macos_certs = "/etc/ssl/cert.pem"
    if Path(macos_certs).is_file():
        try:
            ssl_ctx = ssl.create_default_context(cafile=macos_certs)
            with urlopen(req, timeout=30, context=ssl_ctx) as resp:
                return resp.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError):
            pass

    print(
        f"Error: SSL certificate verification failed for {PRICING_URL}\n"
        "On macOS, run: /Applications/Python\\ 3.x/Install\\ Certificates.command",
        file=sys.stderr,
    )
    sys.exit(1)


def build_models(
    table: list[list[str]], cache_tier: str
) -> dict[str, dict[str, float]]:
    """Build models dict from parsed table rows.

    Skips deprecated models. Uses the specified cache_tier for cache_write rates.
    """
    header = [h.lower().strip() for h in table[0]]
    col_idx = {h: i for i, h in enumerate(header)}

    cache_write_col = f"{cache_tier} cache writes"

    models: dict[str, dict[str, float]] = {}

    for row in table[1:]:
        if len(row) < len(header):
            continue

        model_name = row[col_idx["model"]]
        if is_deprecated(model_name):
            continue

        model_id = model_name_to_id(model_name)

        input_price = parse_price(row[col_idx["base input tokens"]])
        cache_write = parse_price(row[col_idx[cache_write_col]])
        cache_read = parse_price(row[col_idx["cache hits & refreshes"]])
        output_price = parse_price(row[col_idx["output tokens"]])

        if any(
            v is None
            for v in [input_price, cache_write, cache_read, output_price]
        ):
            print(
                f"Warning: Could not parse all prices for '{model_name}', skipping",
                file=sys.stderr,
            )
            continue

        models[model_id] = {
            "input": input_price,
            "cache_write": cache_write,
            "cache_read": cache_read,
            "output": output_price,
        }

    return models


def prune_aliases(
    aliases: dict[str, str], models: dict[str, dict]
) -> dict[str, str]:
    """Keep only aliases whose target model is still present."""
    return {k: v for k, v in aliases.items() if v in models}


def format_diff(
    old_models: dict,
    new_models: dict,
    old_aliases: dict,
    new_aliases: dict,
) -> str:
    """Generate human-readable diff of pricing changes."""
    lines: list[str] = []

    old_keys = set(old_models.keys())
    new_keys = set(new_models.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = sorted(old_keys & new_keys)

    if added:
        lines.append("Added models:")
        for m in added:
            r = new_models[m]
            lines.append(
                f"  + {m}: input=${r['input']}, cache_write=${r['cache_write']}, "
                f"cache_read=${r['cache_read']}, output=${r['output']}"
            )

    if removed:
        lines.append("Removed models:")
        for m in removed:
            lines.append(f"  - {m}")

    changed: list[str] = []
    for m in common:
        old_r = old_models[m]
        new_r = new_models[m]
        diffs = []
        for key in ("input", "cache_write", "cache_read", "output"):
            if old_r.get(key) != new_r.get(key):
                diffs.append(f"{key}: ${old_r.get(key)} -> ${new_r.get(key)}")
        if diffs:
            changed.append(f"  ~ {m}: {', '.join(diffs)}")

    if changed:
        lines.append("Changed models:")
        lines.extend(changed)

    alias_removed = sorted(set(old_aliases.keys()) - set(new_aliases.keys()))
    alias_added = sorted(set(new_aliases.keys()) - set(old_aliases.keys()))

    if alias_removed:
        lines.append("Pruned aliases:")
        for a in alias_removed:
            lines.append(f"  - {a} -> {old_aliases[a]}")

    if alias_added:
        lines.append("New aliases:")
        for a in alias_added:
            lines.append(f"  + {a} -> {new_aliases[a]}")

    return "\n".join(lines) if lines else ""


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and update pricing.json from Anthropic docs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show diff without writing changes",
    )
    parser.add_argument(
        "--cache-tier",
        choices=["5m", "1h"],
        default="5m",
        help="Cache write rate tier to use (default: 5m)",
    )
    args = parser.parse_args()

    pricing_path = resolve_pricing_path()

    # Load existing pricing
    old_data: dict = {}
    if pricing_path.is_file():
        with open(pricing_path) as f:
            old_data = json.load(f)

    old_models = old_data.get("models", {})
    old_aliases = old_data.get("aliases", {})

    # Fetch and parse
    print(f"Fetching {PRICING_URL} ...")
    html = fetch_pricing_page()

    tp = TableParser()
    tp.feed(html)

    if not tp.tables:
        print(
            "Error: No HTML tables found on pricing page.\n"
            "The page structure may have changed (possibly JS-rendered).",
            file=sys.stderr,
        )
        sys.exit(1)

    table = find_pricing_table(tp.tables)
    if table is None:
        found = [[h.strip() for h in t[0]] for t in tp.tables if t]
        print(
            "Error: Could not find model pricing table with expected columns.\n"
            f"Expected: {sorted(EXPECTED_HEADERS)}\n"
            f"Found table headers: {found}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Parsed {len(table) - 1} rows from pricing table "
        f"(cache tier: {args.cache_tier})"
    )

    new_models = build_models(table, args.cache_tier)

    if not new_models:
        print("Error: No models parsed from pricing table.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(new_models)} active models (deprecated models excluded)")

    new_aliases = prune_aliases(old_aliases, new_models)

    # Show diff
    diff = format_diff(old_models, new_models, old_aliases, new_aliases)

    if not diff:
        print("No changes detected.")
        return

    print()
    print(diff)

    if args.dry_run:
        print("\n(dry run — no changes written)")
        return

    # Write updated pricing.json
    new_data = {
        "cache_write_tier": args.cache_tier,
        "models": new_models,
        "aliases": new_aliases,
    }

    with open(pricing_path, "w") as f:
        json.dump(new_data, f, indent=2)
        f.write("\n")

    print(f"\nUpdated {pricing_path}")


if __name__ == "__main__":
    main()
