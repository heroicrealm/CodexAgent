# Этап 6.2 — реализация

Ветка: `STAGE_06_02`. Основание: Issue #19 и утверждённое
[ТЗ](stage-06-02-spec.md). Реализация расширяет существующие `Store`,
`ExecutionStore`, attempts, history, decisions и result_outbox.

## SQLite и миграция

Версия схемы — `602`; маркеры 1–3 сохранены. DDL находится в
`storage/migrations.py`, а не в конструкторе ExecutionStore. Новая БД получает
схему без резервной копии и без очистки. Все подключения Store включают foreign_keys.

Для старой БД перед изменением берётся неблокирующий эксклюзивный flock того же
`.runtime/lock`, который использует daemon. Работающий старый диспетчер запрещает
миграцию, включая запуск queue/show/poll с новым кодом. Остановите его штатной
командой stop, затем выполните poll или start. `serve` проверяет миграцию перед
захватом собственного runtime lock. Другой мигратор не может очищать БД параллельно.
Версия схемы повторно проверяется после получения flock, до backup;
после backup выполняется `BEGIN IMMEDIATE` и дополнительная проверка маркера.

До начала транзакции записи и удаления таблиц SQLite backup API снимает
согласованную копию под тем же flock, включая
закоммиченные WAL-страницы. Файл
`tasks.sqlite3.pre-602-<uuid>.bak` создаётся с правами 0600, проверяется
integrity_check, fsync выполняется для файла и каталога. Ошибка backup запрещает
очистку. Копия содержит приватные снимки задач: храните её как исходную БД.

Операционные tasks/history/decisions/command_guards/command_quarantine/attempts/
result_outbox/quota_observation пересоздаются в одной транзакции с DDL, проверками
foreign_key_check/integrity_check и маркером 602. При ошибке выполняется rollback;
успешная резервная копия остаётся. Неизвестные таблицы не удаляются. После успешной
миграции повторное открытие не очищает задачи, историю или попытки. GitHub и Git
при миграции не вызываются. Старый review не становится доказанной связью PR.
После очистки нужен новый poll и обычное явное подтверждение задач.

## Схема и идентичность

- `tasks`: сохранён ключ `(repository,number)` для CLI; добавлены repository_id и
  current_iteration_id. issue_id глобально уникален; `(repository_id,number)`
  уникален. ID — положительные числовые REST ID в диапазоне signed int64.
- `task_iterations`: iteration_id, issue_id, iteration_number, approved_version,
  decision_id, created_at; уникальна пара `(issue_id,iteration_number)`.
  Текущая итерация принадлежит той же Issue через составной FK.
- `attempts`: iteration_id, attempt_number; уникальна пара
  `(iteration_id,attempt_number)`. Прежняя уникальность по версии удалена.
  Сохранены timestamps, state (outcome), SHA, диагностические и process-поля.
  FK и trigger проверяют принадлежность попытки задаче и итерации.
- `task_pull_requests`: REST pr_id (не issue ID объекта PR и не GraphQL node ID),
  repository_id, issue_id, pr_number, head/base, head_sha, state, merged_at.
  ID и `(repository_id,pr_number)` уникальны; для Issue закрепляется один PR,
  используемый при доработке. Составной FK проверяет репозиторий Issue.
- `iteration_pr_history`: PR и SHA по итерациям; trigger исключает чужую Issue.
- `task_transitions`: уникальный event_key, actor, from/to, version,
  iteration_id, evidence JSON, created_at.
- `publication_operations`: уникальный operation_key, iteration_id, phase,
  outcome, retry_count, updated_at; повтор intent не резервирует Codex.
  ExecutionStore.finish сохраняет intent комментария вместе с result_outbox;
  publisher фиксирует failed/completed и число повторов без новых attempts.

Adapter перед чтением Issues получает `repos/{owner}/{name}` и проверяет REST ID
и full_name. Все страницы должны успешно пройти до применения снимка.
Невалидный/недоступный repository ID запрещает применение частичного poll.
Store дополнительно проверяет конфликты Issue ID и repository ID; совпадение
номеров в разных репозиториях не связывает задачи. Для локальных старых адаптеров
repository_id может быть NULL; новый контракт при этом возвращает
identity_unresolved. Последующий достоверный poll разрешает ID, не выдумывая его.

## Контракт переходов и доверие

`Store.transition(...)` принимает repository/number, expected_state, version,
iteration_id, target, event_key, Authority, Evidence и необязательный Reservation.
Результат — типизированный строковый Outcome: applied, already_applied,
invalid_transition, stale_version, unauthorized, missing_evidence,
identity_unresolved, conflict. SQLite-ошибки целостности/диска пробрасываются с
rollback; они не выдаются за успешный переход.

Authority расширяема через `permits(role)`: issue_owner для решений оператора,
observer для проверенных наблюдений PR. Базовая политика — allowlist и human;
merge остаётся действием человека в GitHub. Передавать Authority следует из
доверенной конфигурации/проверенного actor, а не из тела Issue или комментария.

Evidence — внутренний DTO доверенного адаптера, не криптографическое доказательство
и не вход пользовательского JSON. Вызывающая сторона обязана получить и проверить
GitHub identity, actor и связь PR с Issue вне SQLite-транзакции. В Evidence входят
REST identity, версия через content_hash, expected_state/iteration_id, GitHub
observed_at/event_at/state_observed_at, доступность и для PR — ID, номер,
head/base/SHA, linked_issue_id, state, merged и merged_at. Отсутствие API-ответа
не создаёт Evidence. Одного имени ветки, старого push или текста комментария
недостаточно. Реальный PR reader/writer подключается в 6.3.

Переходы сериализуются `BEGIN IMMEDIATE`; проверяются состояние, версия, итерация,
роль, identity, свежесть серверного события и идемпотентный ключ. Новое событие
должно быть строго новее server fence и не позже наблюдения. Для rejected
дополнительно проверяется граница последнего доказанного отклонения. Повторное
approve неизменённой версии создаёт следующую итерацию с новым decision_id;
повтор того же event_key возвращает already_applied без новых записей.

Поддержаны discovered→pending/rejected, pending→running, running→review,
review→done/rejected, rejected→pending/cancelled. Reserve требует Reservation
(branch, maximum, observation) и атомарно создаёт попытку вместе с переходом.
Для review необходим open unmerged PR; done требует closed merged PR с валидным
merged_at; rejected из review требует closed unmerged PR. Существующая связь PR
должна совпадать по ID, репозиторию, номеру и head/base. История SHA сохраняется.
done/cancelled не имеют исходящих переходов; polling не меняет их состояние.
failed/interrupted не получают нового пути автоматического retry.

Старые CLI/sync approve/reject и scheduler используют общий CAS-примитив
`_set_state`, транзакционный журнал `_record_transition`, решения и итерации; их прежняя проверка свежего
GitHub снимка/actor и observation token сохранена. Scheduler сверяет также
current_iteration_id при reserve/finish. Единовременно резервируется одна
попытка на итерацию; очередной автоматический Codex в ней запрещён. Схема допускает
отдельные нумерованные технические попытки для будущего явно разрешённого retry.
Ветка занята активной попыткой; чужая незавершённая задача сохраняет владение.

## Границы runtime и совместимость

PR workflow не включён: создание/reopen PR, мониторинг merge, закрытие Issue,
cleanup remote branches и полноценное восстановление публикации относятся к
6.3–6.7. Текущий scheduler этапа 4 по-прежнему завершает push как legacy review;
запись отмечается `legacy_push_workflow` и не имеет подтверждённой PR-связи.
Новый контракт не может использовать такой review как доказательство merge.
Переключение runtime push→PR→review выполняется только в 6.3. Повторная доработка
через существующий CLI не активируется раньше workflow reuse/reopen;
rejected→pending/cancelled проверяется новым API и тестами.

`queue`, `show PROJECT NUMBER`, table/JSON и approve/reject сохранены. Прежние
JSON-поля и типы остаются; строки SQLite дополнены полями identity/iteration.
Snapshot/observed остаются JSON-строками. Новые схемные тесты заменяют только
устаревшие ожидания миграции с сохранением старой истории и фиктивное совпадение
глобальных ID в multi-project fixtures.

## Проверки и ограничения

Автотесты используют временные SQLite, mock GitHub и локальные безопасные процессы.
Покрыты новая схема, backup/reset, повтор без очистки новых данных, rollback DDL,
ошибка backup, активный daemon lock, FK, два репозитория с #1, конфликты ID,
неразрешённый ID, approve/повтор, неизменённая версия в новой итерации,
уникальность технических попыток, publication retry без Codex, PR evidence,
устаревшие события, терминальность, конкуренция и регрессии CLI/stages 1–5.

Команда проверки: `bash scripts/test.sh` (pytest и compileall).
Финальный запуск 2026-10-24: **347 passed, 30 subtests passed in 20.09s**;
compileall завершился успешно, exit code всей команды — 0. `git diff --check`
также выполнен без ошибок. Для запуска создан отдельный временный venv с
зависимостями `.[test]`; системный Python изначально не содержал pytest.

Ручная интеграция с настоящими PR/merge намеренно не выполнялась: workflow ещё
не подключён. GitHub и SQLite не имеют общей транзакции; остаётся окно внешнего
изменения после чтения evidence. Нужна последующая reconciliation, а не
предположение об успехе. Обеспечение доверенного происхождения Evidence является
обязанностью интеграции 6.3–6.7. Резервные копии автоматически не удаляются.


## Исправление приёмки — Issue #20

В прежнем `prepare()` соединение миграции выполняло `BEGIN IMMEDIATE` до
`source.backup(target)` через другое соединение. Такое владение writer lock
могло приводить к ожиданию собственного соединения при резервном копировании.
Теперь порядок следующий:

1. Предварительно проверить необходимость миграции.
2. Получить эксклюзивный неблокирующий runtime flock и повторно проверить 602.
   Если другой мигратор уже завершил работу, выйти без backup и очистки.
   При занятом lock вернуть MigrationBusy без изменения базы.
3. Без SQLite-транзакции записи снять WAL-aware backup, проверить integrity_check,
   сохранить права 0600 и выполнить fsync файла и каталога.
4. Только после этого начать `BEGIN IMMEDIATE`, перепроверить маркер,
   пересоздать операционные таблицы, проверить FK/integrity и записать 602.
   При сбое транзакции выполнить rollback; резервную копию оставить.
5. Освободить flock после commit/rollback. При ошибке создания или проверки
   backup до очистки дело не доходит.

Новый `tests/test_migration602.py` проверяет непустую старую БД с tasks,
attempts, history и result_outbox в режимах DELETE и WAL. В WAL-сценарии
родительское соединение остаётся открытым, autocheckpoint отключён и наличие
WAL проверяется до миграции. Миграция и повторное открытие выполняются в отдельном
процессе с `subprocess.run(timeout=10)`: зависший ребёнок будет убит и освобождён,
а весь pytest не зависнет. Дополнительная проверка в backup гарантирует отсутствие
собственной транзакции, независимо от особенностей версии SQLite.

Проверяются содержимое всех старых таблиц в backup, права и целостность копии,
очистка операционных таблиц, схема 602, FK, сохранение вновь обнаруженной задачи
после повторного открытия и ровно одна резервная копия. Отдельно проверены
межпроцессный runtime lock, конкурентный opener, который успел прочитать старую
схему до завершения другого мигратора, и сбой integrity_check резервной копии.
Существующие тесты отказа создания backup и rollback DDL сохранены.

Проверка регрессии: оба новых сценария DELETE/WAL отклоняют прежнюю реализацию
(2 failed), а с исправлением целевой набор проходит: 95 passed.
Workflow 6.3–6.7, CLI и внешние ресурсы не изменены. Произвольные сторонние
SQLite-писатели, игнорирующие runtime flock, не входят в контракт остановленной
миграции; диспетчер и миграторы обязаны соблюдать этот lock.

Полный запуск после исправления: `bash scripts/test.sh` — **352 passed,
30 subtests passed in 28.17s**, compileall успешен, exit code 0.
`git diff --check` выполнен без ошибок.
