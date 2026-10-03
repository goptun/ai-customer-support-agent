#!/bin/sh
set -e
alembic upgrade head
python -m support_agent.seed
exec "$@"
