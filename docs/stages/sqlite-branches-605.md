# SQLite: параметры Git-веток (Issue #30)

Миграция **605** следует за 604. `migrations.VERSION=602` остаётся границей
старого destructive reset: повышать эту константу для additive-миграций нельзя.
`prepare()` также распознаёт фактическую stage-6 схему (`repository_id`,
`current_iteration_id`) без маркера 602 и не сбрасывает её.

`Store` выполняет upgrade604, затем в `BEGIN IMMEDIATE` инициализирует недостающие
таблицы и применяет upgrade605. Проверяются реальные колонки независимо от
наличия маркера; повторное открытие безопасно. ALTER TABLE, индексы, триггеры,
`foreign_key_check`, полный `integrity_check` и маркер 605 входят в одну
транзакцию. Любая ошибка откатывает её и закрывает соединение: Store не
возвращается обработчику. Миграция 604 сохраняет свою отдельную транзакцию.
Бэкап/reset 602 относится только к старой, до stage-6, схеме.

## Колонки tasks

| Поле | Тип / значения | По умолчанию |
|---|---|---|
| base_branch | nullable TEXT | NULL |
| work_branch | nullable TEXT | NULL |
| branch_source | nullable TEXT: explicit, generated | NULL |
| branch_status | nullable TEXT: unresolved, resolved, prepared, conflict, failed | unresolved |
| branch_error | nullable TEXT | NULL |
| branch_prepared_at | nullable TEXT: временная метка | NULL |

Текущие параметры принадлежат Issue, отдельной таблицы веток нет. Ветки
`attempts.branch` остаются историческими. Миграция не выводит base_branch из
snapshot/PR/attempt, не меняет state и не копирует историческую ветку в work_branch.
Все существующие таблицы, записи, ключи и связи сохраняются.

## Чтение и запись

`Store.branches(repository, number)` возвращает словарь шести полей либо None.
`Store.save_branches(...)` принимает полный новый набор и обязательные
`expected_version`, `expected_iteration_id`, `expected` (предыдущий словарь).
Проверка и запись сериализованы `BEGIN IMMEDIATE`. Устаревшая версия, итерация,
снимок либо изменение running/review/done/cancelled возвращают False. Повторная
идентичная запись возвращает True без нового события. Наличие running attempt
также замораживает параметры независимо от state Issue.

Имена веток проверяются существующим `valid_branch`. Для resolved/prepared нужны
обе ветки и source; prepared требует timestamp. Ошибки параметров дают ValueError,
нарушение SQL CHECK/уникальности/занятой ветки — sqlite3.IntegrityError с откатом.
Изменение записывает `history.event=branches_updated`, JSON before/after в snapshot;
версия контента Issue при этом не меняется. Параметры attempts не обновляются.

## Владение веткой

Активными для владения считаются все состояния, кроме done/cancelled/revoked.
Таким образом discovered с назначенной веткой, pending, running, review, failed,
interrupted и rejected удерживают ветку, включая ожидание повторной итерации.
NULL work_branch не создаёт резервирования. Сравнение имён веток регистрозависимо,
repository нормализуется существующими API через casefold.

Частичный UNIQUE `task_work_branch(repository,work_branch)` исключает два активных
Issue. SQL-триггеры проверяют попытки старых задач без work_branch: running attempt
всегда удерживает ветку, остальные исторические попытки удерживают её, пока их
Issue активно. Обратные триггеры запрещают running attempt на ветке другого
активного Issue, включая UPDATE. Повторная активация terminal Issue с исторической
веткой, уже назначенной другому Issue, блокируется. Разные repository независимы.
ExecutionStore проверяет занятость до admission и не допускает ветку, отличную от
уже назначенного work_branch. Исторические попытки terminal Issue освобождают
ветку; их записи сохраняются.

Git/YAML/Codex операции и разрешение Git-конфликтов в этот этап не входят.
Нужный оператору риск: обнаруженный конфликт существующих назначений при создании
уникального индекса останавливает открытие БД; автоматического выбора владельца нет.

Проверка: `bash scripts/test.sh`. `tests/test_issue30.py` покрывает 602/604,
повторное открытие, сохранность связанных таблиц, rollback, CAS, аудит,
заморозку параметров, исторические попытки и конкуренцию отдельных соединений.
GitHub Actions использует тот же pytest и compileall на Python 3.12 и 3.14.
