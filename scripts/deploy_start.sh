#!/usr/bin/env bash
# Production start script (used by render.yaml).
# The free tier has an ephemeral disk, so if no database is present we load
# the bundled demo dataset. To deploy with real TCAD data instead, commit
# your locally-built database:  git add -f data/comps.db
set -e

if [ ! -f data/comps.db ]; then
  echo "No database found - loading demo dataset..."
  python -m app.ingest.seed
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
