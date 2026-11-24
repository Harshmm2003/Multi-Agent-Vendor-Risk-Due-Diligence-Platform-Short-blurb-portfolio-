"""scripts/demo_reports.py: saved reports copied into a fresh hosted database."""

import asyncio
import importlib.util
import json
from pathlib import Path

from conftest import make_sessionmaker
from sqlalchemy import select

from app.db.models import Evaluation

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "demo_reports", BACKEND / "scripts" / "demo_reports.py"
)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_committed_demo_file_imports_once_and_is_idempotent(db_url, monkeypatch):
    engine, Session = asyncio.run(make_sessionmaker(db_url))
    monkeypatch.setattr(demo, "AsyncSessionLocal", Session)
    expected = {r["evaluation_id"] for r in json.loads(demo.DEFAULT_FILE.read_text())}

    async def ids():
        async with Session() as s:
            return list((await s.execute(select(Evaluation.evaluation_id))).scalars())

    asyncio.run(demo.import_(demo.DEFAULT_FILE))
    asyncio.run(demo.import_(demo.DEFAULT_FILE))
    found = asyncio.run(ids())
    assert sorted(found) == sorted(expected) and expected
    asyncio.run(engine.dispose())


def test_round_trip_keeps_every_column(db_url, monkeypatch, tmp_path):
    engine, Session = asyncio.run(make_sessionmaker(db_url))
    monkeypatch.setattr(demo, "AsyncSessionLocal", Session)
    asyncio.run(demo.import_(demo.DEFAULT_FILE))
    first = json.loads(demo.DEFAULT_FILE.read_text())[0]
    out = tmp_path / "again.json"
    asyncio.run(demo.export([first["evaluation_id"]], out))
    again = json.loads(out.read_text())[0]
    assert again == first
    asyncio.run(engine.dispose())
