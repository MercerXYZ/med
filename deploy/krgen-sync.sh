#!/bin/bash
# Синхронизация с GitHub: забирает main и перезапускает программу.
# Не перезапускает, пока идёт обработка (иначе прогон оборвётся) — повторит через минуту.
set -euo pipefail
cd /opt/krgen
as_krgen() { runuser -u krgen -- "$@"; }

as_krgen git fetch -q origin main
LOCAL=$(as_krgen git rev-parse HEAD)
REMOTE=$(as_krgen git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

RUNNING=$(sqlite3 /var/lib/krgen/krgen.sqlite \
  "SELECT COUNT(*) FROM runs WHERE status='running' AND started_at > strftime('%s','now') - 43200" 2>/dev/null || echo 0)
if [ "${RUNNING:-0}" != "0" ]; then
  echo "Идёт обработка — обновление $REMOTE отложено"
  exit 0
fi

REQ_BEFORE=$(sha1sum requirements.txt | cut -d' ' -f1)
as_krgen git reset -q --hard origin/main
REQ_AFTER=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
  as_krgen /opt/krgen/.venv/bin/pip install -q -r requirements.txt
fi
systemctl restart krgen
echo "Обновлено: $LOCAL -> $REMOTE"
