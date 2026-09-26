#!/usr/bin/env bash
# Render build step: install locked deps, collect static, migrate.
set -o errexit
pip install uv
uv sync --frozen --no-dev
uv run --frozen --no-dev python manage.py collectstatic --no-input
uv run --frozen --no-dev python manage.py migrate --no-input
