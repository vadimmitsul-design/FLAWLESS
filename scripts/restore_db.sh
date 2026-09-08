#!/usr/bin/env bash
# Восстановление базы Flawless из копии, сделанной scripts/backup_db.sh.
#
# Копия, которую ни разу не разворачивали, копией не считается — прогоняйте
# этот скрипт на ТЕСТОВОЙ базе хотя бы раз в месяц, чтобы знать, что архивы
# действительно рабочие, а не просто лежат.
#
#   ./scripts/restore_db.sh backups/flawless-20260908-030000.sql.gz
#
# ОСТОРОЖНО: дамп сделан с --clean --if-exists, то есть он УДАЛЯЕТ
# существующие таблицы перед восстановлением. Не запускайте на боевой базе,
# если не собираетесь именно перезаписать её содержимое.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Использование: $0 <файл.sql.gz> [--yes]" >&2
  exit 1
fi

dump="$1"
confirm="${2:-}"
COMPOSE="${COMPOSE:-docker compose}"

[ -f "$dump" ] || { echo "Файл не найден: $dump" >&2; exit 1; }

echo "Восстановление из: $dump"
echo "Это ПЕРЕЗАПИШЕТ текущее содержимое базы neurohub."
if [ "$confirm" != "--yes" ]; then
  read -r -p "Продолжить? Введите 'да' для подтверждения: " answer
  [ "$answer" = "да" ] || { echo "Отменено."; exit 1; }
fi

echo "==> Останавливаю приложение, чтобы оно не писало во время восстановления"
$COMPOSE stop neurohub

echo "==> Заливаю дамп"
gzip -dc "$dump" | $COMPOSE exec -T db psql -U neurohub -d neurohub -v ON_ERROR_STOP=1

echo "==> Поднимаю приложение (миграции догонят схему, если дамп старее кода)"
$COMPOSE up -d neurohub

echo "==> Проверка: сходится ли баланс с журналом списаний"
$COMPOSE exec -T db psql -U neurohub -d neurohub -c "
  SELECT c.email,
         c.balance_rub AS balance,
         COALESCE(SUM(w.delta_rub), 0) AS ledger_sum,
         c.balance_rub - COALESCE(SUM(w.delta_rub), 0) AS diff
  FROM customers c
  LEFT JOIN wallet_ledger w ON w.customer_id = c.id
  GROUP BY c.id, c.email, c.balance_rub
  HAVING c.balance_rub <> COALESCE(SUM(w.delta_rub), 0);
"
echo "==> Если таблица выше пустая — баланс сходится с журналом, восстановление корректно."
