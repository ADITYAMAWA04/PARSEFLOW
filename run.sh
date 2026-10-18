#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
[ -d .venv ] || python3 -m venv .venv || { echo "Need Python 3.9+ with venv (python3 -m venv failed)"; exit 1; }
. .venv/bin/activate
pip install -q -r requirements.txt || exit 1
python backend/start.py "$@"
