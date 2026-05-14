#!/usr/bin/env bash
# Convenience launcher.
set -e
cd "$(dirname "$0")"
exec ./.venv/bin/uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
