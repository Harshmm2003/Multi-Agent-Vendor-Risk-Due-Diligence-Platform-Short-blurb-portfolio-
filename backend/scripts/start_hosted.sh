#!/bin/sh
# Start command for the hosted backend (render.yaml): migrate the database,
# add the saved demo reports if missing, then serve. Render sets $PORT.
set -e
alembic upgrade head
python scripts/demo_reports.py import
exec uvicorn main_api:app --host 0.0.0.0 --port "${PORT:-8000}" --loop asyncio
