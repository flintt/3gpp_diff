#!/usr/bin/env bash
set -euo pipefail

# Parsed data stays in each worker's bounded memory cache. Expensive diff work
# and background jobs use filesystem locks/status files so multiple workers do
# not duplicate the same task.
python3 -c 'from app import prune_obsolete_caches; prune_obsolete_caches()'

exec gunicorn \
  --bind "0.0.0.0:${PORT:-5001}" \
  --worker-class gthread \
  --workers "${WEB_WORKERS:-2}" \
  --threads "${WEB_THREADS:-4}" \
  --timeout "${WEB_TIMEOUT:-180}" \
  --graceful-timeout "${WEB_GRACEFUL_TIMEOUT:-30}" \
  --keep-alive "${WEB_KEEP_ALIVE:-5}" \
  --access-logfile - \
  --error-logfile - \
  app:app
