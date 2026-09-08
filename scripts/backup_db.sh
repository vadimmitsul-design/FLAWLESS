#!/usr/bin/env bash
# Резервная копия базы Flawless.
#
# Зачем: балансы клиентов, журнал списаний и вся история вызовов существуют
# ТОЛЬКО в докеровском томе pgdata. Восстановить их неоткуда — пополнения
# подтверждаются нажатием кнопки, а не выпиской банка. Потеря тома = потеря
# учёта (аудит 2026-09-07: резервных копий не было вообще).
#
# Запуск вручную:
#   ./scripts/backup_db.sh
#   BACKUP_DIR=/var/backups/flawless ./scripts/backup_db.sh
#
# По расписанию (на ХОСТЕ, не внутри контейнера) — crontab -e:
#   0 3 * * * cd /opt/flawless && BACKUP_DIR=/var/backups/flawless ./scripts/backup_db.sh >> /var/log/flawless-backup.log 2>&1
#
# ВАЖНО: копия, которую ни разу не разворачивали, копией не считается.
# Раз в месяц прогоняйте scripts/restore_db.sh на тестовой базе.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
COMPOSE="${COMPOSE:-docker compose}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%d-%H%M%S)"
target="$BACKUP_DIR/flawless-$stamp.sql.gz"

echo "==> Дамп базы в $target"
# -Fp (обычный SQL) вместо custom-формата: восстановить можно чем угодно,
# включая psql, без pg_restore нужной версии.
$COMPOSE exec -T db pg_dump -U neurohub -d neurohub --clean --if-exists \
  | gzip -9 > "$target"

size="$(du -h "$target" | cut -f1)"
echo "==> Готово: $target ($size)"

# Пустой/обрезанный дамп опаснее отсутствующего — он выглядит как копия.
if [ "$(gzip -dc "$target" | head -c 100 | wc -c)" -lt 100 ]; then
  echo "!! Дамп подозрительно мал — проверьте вручную" >&2
  exit 1
fi

echo "==> Удаляю копии старше $KEEP_DAYS дней"
find "$BACKUP_DIR" -name 'flawless-*.sql.gz' -type f -mtime "+$KEEP_DAYS" -print -delete

echo "==> Текущие копии:"
ls -lh "$BACKUP_DIR" | tail -n +2
