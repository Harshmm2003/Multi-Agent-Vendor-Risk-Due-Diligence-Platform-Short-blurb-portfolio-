"""
Saved reports for a hosted demo.

A fresh hosted database has no evaluations, so the History page would be
empty. This copies chosen evaluations from one database to another through
a JSON file kept in the repo (backend/demo/reports.json):

    # from your local database: write the file
    uv run python scripts/demo_reports.py export eval-41995502 eval-e1f19ea4

    # into the database DATABASE_URL points at: add any that are missing
    uv run python scripts/demo_reports.py import

Import is idempotent (rows whose evaluation_id exists are skipped), so the
hosted container runs it on every start. See docs/deploy.md.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, select

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.db.database import AsyncSessionLocal, engine
from app.db.models import Evaluation

DEFAULT_FILE = BACKEND / "demo" / "reports.json"
SKIP = {"id"}  # the primary key is regenerated on import


def _columns():
    return [c for c in Evaluation.__table__.columns if c.name not in SKIP]


def _to_json(row: Evaluation) -> dict:
    out = {}
    for col in _columns():
        value = getattr(row, col.key)
        if isinstance(value, datetime):
            # SQLite drops the offset; every timestamp here is stored as UTC
            value = (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
        out[col.key] = value
    return out


def _from_json(data: dict) -> Evaluation:
    values = {}
    for col in _columns():
        if col.key not in data:
            continue
        value = data[col.key]
        if isinstance(col.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        values[col.key] = value
    return Evaluation(**values)


async def export(ids: list[str], path: Path) -> None:
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Evaluation).where(Evaluation.evaluation_id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
    found = {r.evaluation_id for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise SystemExit(f"Not found: {', '.join(missing)}")
    rows = sorted(rows, key=lambda r: r.created_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([_to_json(r) for r in rows], indent=1) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(rows)} report(s) to {path}")


async def import_(path: Path) -> None:
    if not path.exists():
        print(f"No demo reports at {path}; nothing to import.")
        return
    records = json.loads(path.read_text(encoding="utf-8"))
    async with AsyncSessionLocal() as db:
        have = set((await db.execute(select(Evaluation.evaluation_id))).scalars())
        new = [_from_json(r) for r in records if r["evaluation_id"] not in have]
        db.add_all(new)
        await db.commit()
    print(f"Demo reports: {len(new)} added, {len(records) - len(new)} already present.")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="write evaluations to the JSON file")
    ex.add_argument("evaluation_ids", nargs="+")
    ex.add_argument("--file", type=Path, default=DEFAULT_FILE)
    im = sub.add_parser("import", help="add the file's evaluations to the database")
    im.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()
    try:
        if args.command == "export":
            await export(args.evaluation_ids, args.file)
        else:
            await import_(args.file)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
