# Этап 6.4.1.4: durable workspace cleanup

## Карта интеграции и совместимость

- `workspace.py`: `GitWorkspaceManager.prepare` архивирует прошлую итерацию; перед
  move/rename теперь записывает ownership intent в существующий fsync registry.
  `TaskWorkspaceCleanup` остаётся единственным механизмом удаления файлов.
- `storage/migrations.py`, `storage/__init__.py`: additive миграция 607 в принятой
  BEGIN IMMEDIATE транзакции. Таблица `cleanup_jobs`, триггеры enqueue и guards.
  Существующая схема имеет глобально уникальный REST issue_id, composite
  repository/number и project_id; cleanup использует все эти значения, а не number.
- `storage/transitions.py`, `queue.py`, PR monitor: используют прежние переходы;
  AFTER INSERT/UPDATE triggers охватывают их без изменения бизнес-семантики.
- `cleanup.py`: reconciliation, generation claim, ownership из всех attempts,
  background thread с отдельным Store/SQLite connection, recovery/retries.
- `scheduler.py`, `runtime.serve`: daemon держит прежний process lock; scheduler
  запускает и завершает cleanup worker, не выполняя удаления в основном цикле.
- `cancellation.py`, `github/sync.py`: доставка закрытия GitHub Issue отделена от
  удаления; poll дополнительно сверяет очередь. Поля cancellations.cleaned/error
  сохранены для совместимости; cleaned обновляется после durable cleanup success.
  Error этой таблицы относится к GitHub delivery, cleanup error — к cleanup_jobs.
- `cli.py`, `rendering.py`, `logging/__init__.py`: show table/JSON, cleanup-retry,
  структурированные события с безопасными кодами ошибок.

Исходные несовместимости: удаление было привязано только к cancelled и poll,
проверяло архивы через PR evidence, не имело очереди, recovery и ограниченного
retry. Минимальное решение сохраняет существующие workspace, registry, attempts,
SQLite journal и статусы Issue; добавляет одну таблицу и независимый worker.
`done/cancelled` остаются необратимыми через публичные API. Во время running cleanup
SQL guards запрещают смену поколения/состояния/идентичности и запись attempts.

## Миграция 607 и поколения

Ничего не сбрасывается и не удаляется из существующих SQLite таблиц. При открытии
Store DDL/guards/marker выполняются в одной транзакции с integrity/foreign key check.
Для миграции 607 destructive reset/backup механизма 602 не требуется; перед
обновлением рекомендуется штатная резервная копия SQLite с WAL через backup API.

Ключ задания: `(issue_id, COALESCE(current_iteration_id,0), version)`; project_id,
repository, number, последний attempt_id сохранены и повторно проверяются перед
операцией. Нулевая generation поддерживает legacy без iteration_id. Триггеры
фиксируют terminal решение и pending задание атомарно, rollback отменяет оба.
Повторные poll/sync не сбрасывают attempts, failed или completed. Reconciliation
добавляет отсутствующие задания, включая старые terminal задачи; filesystem preflight
проверяет ownership до любого удаления. Неподтверждённое владение получает durable
ошибку и сохраняет данные.

## Recovery, concurrency, retries

Linux flock `.runtime/cleanup-<issue_id>.lock` удерживается от claim до commit
результата. Файл блокировки сохраняется: unlink lock-файла создавал бы гонку inode.
SQLite state=running резервирует поколение; SQLite writer lock освобождается до Git
и unlink, чтобы другие проекты, poll и admission могли работать. Второй worker
не получает flock. После смерти процесса ОС освобождает lock; running job
возобновляет ту же counted попытку. Clock timeout не отнимает работу у живого worker.
Перед удалением проверяются terminal generation, последний attempt, отсутствие
running attempt и живого PID с сохранённой `/proc` identity.

После ошибки первая повторная попытка через 60 секунд, следующая через 300,
последняя через 900. Четыре попытки, затем failed; отсчёт от времени предыдущей
ошибки UTC. Pending сохраняет next_retry_at через restart. Running recovery не
расходует дополнительную попытку из-за аварии, failed автоматически не сбрасывается.
`cleanup-retry PROJECT NUMBER` сбрасывает budget только failed текущего поколения.

## Безопасность и частичное удаление

До первого удаления проверяются весь task dir, registry, все manifests, Git
registration/common-dir/gitdir reciprocal pointers и чистота всех worktrees
(включая ignored/untracked). Current HEAD и branch должны совпасть с историей
последней попытки; архивы проверяются по соответствующей попытке, допускается
предусмотренный prepare detached HEAD. Архивы attachments подтверждаются attempt ID
и manifest с исходными путями task/attachments. Legacy prompt/log разрешены только
для ID из attempts. Имена previous-/attachments- сами по себе не подтверждают владение.

Git worktree remove без force сохраняет dirty данные, refs и clone. Отсутствующий
worktree с оставшейся reciprocal metadata тоже удаляется через Git, без глобального
prune. Частично удалённые файлы считаются отсутствующими; manifests и registry
удаляются после принадлежащих им файлов. Пустые task/attachments удаляются rmdir.
Не используются recursive force deletion или shutil.rmtree в production cleanup.

Symlinks и traversal отвергаются, открытие registry/lock использует O_NOFOLLOW,
перед unlink повторно проверяются компоненты пути. Практическое ограничение:
workspace должен быть приватным каталогом диспетчера на Linux FS; внешняя программа
того же UID с намеренной заменой directory components между проверкой и вызовом Git
может создать TOCTOU. Защита не является sandbox против владельца workspace.
Симлинки внутри worktree также консервативно блокируют очистку, включая tracked.
Неполная история, ручной rebase/branch switch или изменённая Git metadata требуют
операторского разбора; force и обход ownership не предусмотрены.

Для каждого настроенного проекта работает отдельный cleanup thread с отдельным
SQLite connection. Jobs одного проекта обрабатываются последовательно; медленный
Git не блокирует другие проекты или scheduler/GitHub. Долгий Git вызов ограничен
прежним timeout 120 секунд и может задержать остановку daemon. Смена
конфигурации project id/repository сохраняет неразрешённые jobs и данные; оператор
должен восстановить исходную конфигурацию для диагностики/обработки.
Crash/restart проверяется для процессов; гарантии при внезапной потере питания
зависят также от SQLite/filesystem durability и настроек хранилища.

## Диагностика и ручные сценарии

1. Запустить daemon; выполнить задачу до review и проверить сохранность worktree,
   attachments, logs. Отклонить PR и повторить несколько итераций: previous-* и
   attachments-* должны сохраняться до terminal решения.
2. После штатного merge/done либо подтверждённого cancelled проверить show:
   pending → running → completed; task dir исчез, Git worktree list содержит clone,
   все локальные/origin branches, PR и SQLite attempts/history остались.
3. Перед terminal решением добавить неизвестный файл или dirty/untracked/ignored
   файл. Проверить сохранность всех данных, неизменный done/cancelled, last_error,
   1/5/15 минут ожидания и failed после четвёртой ошибки. Устранить причину вручную
   после изучения данных, выполнить cleanup-retry, проверить completed.
4. Остановить процесс аварийно во время удаления; запустить daemon повторно.
   Running job должен продолжить ту же counted попытку, без повторного Codex exec.
   Повторить после удаления worktree и до SQLite commit результата.
5. Два проекта с Issue #1: заблокировать очистку одного unknown файлом; второй
   должен очищаться независимо. Повторный start/poll не создаёт дубликаты jobs.
6. Legacy terminal задачи: открыть старую БД, запустить daemon без ручного poll.
   Подтверждённые attempts/archives очистятся; неизвестные legacy объекты сохранятся
   с cleanup_ownership_unproven/cleanup_unexpected_file. Не редактировать registry
   для искусственного подтверждения владения и не обходить защиту.

`show PROJECT NUMBER [--format json]`: cleanup_id, generation, state, attempts,
started/completed/updated timestamps, next_retry_at, last_error. JSON дополнен
массивом cleanup_jobs. Лог dispatcher.jsonl: event=cleanup, project, issue/task,
cleanup_id, attempt, result и reason. Тексты исключений, содержимое файлов,
credentials и Git stderr в cleanup diagnostics не записываются.

## Автоматическая проверка

`tests/test_cleanup607.py`: local Git, terminal/nonterminal, сохранение refs/PR/DB,
atomic rollback/idempotency, ownership/dirty/unknown/symlinks/corrupt manifests,
управляемое время, exhaustion/manual retry, четыре crash checkpoints, SIGKILL
worker/recovery, конкурирующие connections/workers и generation guards, legacy,
несколько архивов, partial Git removal, crash во время архивации, миграция с 606,
многопроектность, CLI show/retry, background без poll. Старые тесты cancellation,
JSON show и migration marker обновлены под новую границу ответственности.
Запуск: `scripts/test.sh` и отдельно `python -m pytest -q` с установленным `.[test]`.
Unit/integration не требуют GitHub сети и реального Codex.

## Изменённые файлы

Production: `src/dispatcher/cleanup.py`, `workspace.py`, `scheduler.py`,
`cancellation.py`, `github/sync.py`, `storage/__init__.py`,
`storage/migrations.py`, `cli.py`, `rendering.py`, `logging/__init__.py`.
Все сокращённые пути в этом перечне относятся к `src/dispatcher/`.

Тесты: `tests/test_cleanup607.py`, `tests/test_issue27.py`,
`tests/test_stage04.py`, `tests/test_cli_unicode.py`, `tests/test_rendering.py`.
Документация: `README.md`, этот implementation report. Утверждённое ТЗ сохранено.

Ручные GitHub/Playground сценарии перечислены выше для отдельной приёмки;
в рамках реализации выполнены автоматические локальные сценарии. PR не создаётся.

Итог проверки на Python 3.14.4 в отдельном venv с `.[test]`:
`scripts/test.sh` — 561 passed, 30 subtests passed, compileall без ошибок;
отдельный `python -m pytest -q` — 561 passed, 30 subtests passed.
`git diff --check` — без ошибок.
