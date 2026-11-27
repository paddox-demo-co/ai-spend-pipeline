"""Load the landing files into the warehouse's raw tables.

The warehouse is rebuilt from the landing files on every run, so a reload is just a
run "through" a given day. A file that is missing a required column stops the load.
"""

import argparse
import csv
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["usage_date", "api_key_id", "model", "tokens", "cost_usd"]

# Vendor exports sometimes rename a column. Map the canonical name to the alternate
# names we've seen so a renamed column doesn't stop the load, as long as one of them
# is present.
ALIASES = {"cost_usd": ["cost"]}


def resolve_column(header: list[str], canonical: str) -> str | None:
    """Return the column in `header` that supplies `canonical`, or None if absent."""
    if canonical in header:
        return canonical
    for alt in ALIASES.get(canonical, []):
        if alt in header:
            return alt
    return None


def check_columns(files: list[Path]) -> None:
    for path in files:
        with path.open() as f:
            header = next(csv.reader(f))
        missing = [c for c in REQUIRED if resolve_column(header, c) is None]
        if missing:
            raise SystemExit(f"{path.name}: missing column(s) {', '.join(missing)}; found {', '.join(header)}")


def load(db: str, landing: Path, reference: Path, through: str | None = None) -> int:
    files = sorted(landing.glob("*.csv"))
    if through:
        files = [f for f in files if f.name[:10] <= through]
    if not files:
        raise SystemExit("no landing files to load")
    check_columns(files)

    con = duckdb.connect(db)
    con.execute("create schema if not exists raw")
    con.execute("create schema if not exists finance")

    selects = []
    for f in files:
        with f.open() as fh:
            header = next(csv.reader(fh))
        cost_col = resolve_column(header, "cost_usd")
        load_id = f.name[: -len(".csv")] if f.name.endswith(".csv") else f.name
        selects.append(f"""
            select usage_date::date as usage_date, api_key_id, model, tokens::bigint as tokens,
                   "{cost_col}"::decimal(12, 2) as cost_usd,
                   '{load_id}' as _load_id
            from read_csv('{f}', header = true, all_varchar = true)
        """)
    con.execute("create or replace table raw.azure_openai_usage as " + " union all ".join(selects))

    for table, file in [
        ("raw.api_keys", "api_keys.csv"),
        ("raw.m365_copilot_seats", "m365_copilot_seats.csv"),
        ("raw.github_copilot_seats", "github_copilot_seats.csv"),
        ("finance.vendor_invoices", "vendor_invoices.csv"),
    ]:
        con.execute(f"create or replace table {table} as select * from read_csv_auto('{reference / file}')")
    count = con.execute("select count(*) from raw.azure_openai_usage").fetchone()[0]
    con.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--through", help="load days up to and including this date (YYYY-MM-DD)")
    parser.add_argument("--db", default=str(ROOT / "warehouse.duckdb"))
    args = parser.parse_args()
    rows = load(args.db, ROOT / "data" / "landing" / "azure_openai_usage", ROOT / "data" / "reference", args.through)
    print(f"loaded {rows} Azure OpenAI rows" + (f" through {args.through}" if args.through else ""))


if __name__ == "__main__":
    sys.exit(main())
