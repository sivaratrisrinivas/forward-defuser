"""Export reviewer corrections as eval cases.

    uv run python scripts/export_corrections.py --out evals/corrections.jsonl

Appends unexported corrections and marks them exported. Each case still needs frozen evidence (`fixtures`) written
by hand before it moves into golden.jsonl: the eval never fetches live sources."""

import argparse
import asyncio
import json
from pathlib import Path

from defuser.admin.routes import export_corrections
from defuser.db import close_pool, get_pool


async def main(out: Path, dry_run: bool) -> None:
    rows = await export_corrections(await get_pool(), mark=not dry_run)
    if rows and not dry_run:
        with out.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} correction(s){' (dry run)' if dry_run else f' appended to {out}'}")
    await close_pool()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("evals/corrections.jsonl"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.out, a.dry_run))
