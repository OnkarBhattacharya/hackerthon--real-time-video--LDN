#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d web/node_modules ]]; then
  (cd web && npm install)
fi

if [[ ! -d web/dist ]]; then
  (cd web && npm run build)
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload
