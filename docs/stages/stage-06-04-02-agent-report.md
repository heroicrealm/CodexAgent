# Отчёт реализации 6.4.2 / Issue #42

Реализация подготовлена для отдельной приёмки. Live-приёмка не выполнялась;
этап не объявляется принятым. PR не создаётся.

## Изменения и решения

- `src/dispatcher/issue_parameters.py`: единый разбор служебных fenced blocks,
  YAML с уникальными строковыми ключами, запрет anchors/aliases, legacy строк.
  Сканер учитывает символ/длину fence, отступ до трёх пробелов и закрытие;
  вложенные примеры внутри другого fence не интерпретируются как параметры.
- `github/__init__.py`: исходный body сохраняется; ошибка параметров одной Issue
  не прерывает fetch остальных. Поле target_branch остаётся совместимым.
- `codex/__init__.py`: перед каждой попыткой из утверждённого body получается
  отдельный task_body; удаляется весь служебный блок и legacy строка.
- `workspace.py`: fetch перед выбором источника, существующий origin HEAD либо
  актуальная указанная база; защита настроенной и фактической основной ветки.
  Неизвестная локальная ветка вызывает диагностированный отказ. Никаких reset,
  rebase или force push не добавлено. Старые результаты повторной попытки
  сохраняются штатным архивированием ветки/worktree.
- `scheduler.py`, `preparation_error.py`: ошибки локальны для Issue, резервирование
  остаётся атомарным в существующей SQLite модели. Диагностика использует штатный
  configuration_errors/outbox и discovered, новые статусы не вводятся.
- `execution_store.py`, `storage/__init__.py`, `storage/migrations.py`: транзакционная
  идемпотентная миграция 613 добавляет nullable source_base_branch и branch_existed;
  branch/initial_sha уже существовали. Исторические записи сохраняются.
- `rendering.py`: show выводит фактическую work_branch и provenance попыток.
- README, `tests/test_stage6042.py`, `tests/test_stage04.py`, `tests/test_issue36.py`:
  примеры, новые проверки и обновление ожиданий под вариант А.

Сохранены два ограничения текущего workflow:

1. Reattach после detach требует подтверждённый прежний опубликованный SHA.
   Это более строгий контракт, чем общее правило использования свежего HEAD:
   чужое изменение origin блокирует reattach, чтобы не ослаблять проверку
   принадлежности и сохранности старого результата. Rejected → pending получает
   свежий HEAD, как требует ТЗ.
2. Противоречивые исторические claims могут запрещать любое обновление tasks
   SQLite-триггером. В таком случае сохраняются claims, configuration_errors,
   история и комментарий; задача не запускается. Насильно очищать claims нельзя.
   После обычной ошибки подготовки используется discovered.

База PR сохраняет прежнюю семантику сохранённого task.base_branch. Новое
source_base_branch фиксирует только фактически использованный источник создания;
для существующей ветки оно NULL. Механизмы PR, detach и cleanup не переустраивались.

## Матрица раздела 7 ТЗ

| Сценарий | Автоматическая проверка |
|---|---|
| 1, 2 | test_stage04: worktree_existing_missing_default_and_push; test_stage6042: origin_source |
| 3, 4 | test_stage6042: origin_source, включая отсутствующую base при существующей ветке |
| 5 | test_stage6042: push_conflict_preserves_commit |
| 6 | test_stage6042: invalid, ambiguous, bad_issue_does_not_block_other_issue |
| 7, 8 | test_stage6042: missing_base_and_protection; test_stage04: default_branch_different_from_config_and_invalid_refs |
| 9 | test_stage04: concurrent_atomic_reservation_and_branch_lock; test_stage6042: collision_is_diagnosed_without_launch; существующие cross-project проверки |
| 10 | test_stage604: scheduler_retry_reuses_branch_and_pr_from_current_origin; очистка каждого prompt в test_stage6042 |
| 11 | test_stage6042: local_collision_preserves_ref; существующие retry/detach проверки |
| 12 | test_stage6042: legacy_and_nested_examples; test_stage04: worktree_existing_missing_default_and_push |
| 13 | test_stage6042: additive_migration; полный набор queue/show/PR/detach/cleanup |
| 14 | полный scripts/test.sh; реальные GitHub проверки ниже остаются ручными |

Очистка prompt проверяется на обоих типах fence, различной длине/отступах,
обычном YAML, вложенном примере, незакрытом блоке, двух вызовах executor и
неизменности сохранённого исходного body. Автотесты используют временные локальные
Git-репозитории и Codex/GitHub заглушки.

## Команды проверки

Начальный `bash scripts/test.sh` остановился: python3 из PATH ссылался на окружение
CodexAgent2 без pytest. Зависимости установлены командой
`.venv/bin/python -m pip install -e '.[test]'` в локальное окружение проекта.
Последующие запуски используют `PATH="$PWD/.venv/bin:$PATH"`.

Окончательная проверка:

- `PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh` — **748 passed,
  30 subtests passed**, 127.73 s; compileall выполнен, код возврата 0.
- `git diff --check` — успешно, код возврата 0.
- Регрессионный набор GitHub/retry/новой функциональности — 98 passed;
  отдельные проверки parser/retry — 35 passed.

SSH к origin недоступен (publickey), HTTPS с credential helper `gh` работает.
Публикация выполняется явным refspec только в STAGE_06_04_02; адрес origin
не изменяется. Реальные ручные сценарии ниже ещё не выполнялись.

## Пошаговая ручная проверка на тестовом GitHub-репозитории

1. Настроить отдельный тестовый проект с main/develop, доверенным автором и
   штатными метками; выполнить init/doctor. Не использовать рабочий проект.
2. Создать Issue без параметров, approve, запустить dispatcher. Проверить
   codex/issue-N от main, show/attempt, обычный PR, затем done/cleanup.
3. Создать Issue с dispatcher.branch: feature/new и base_branch: develop.
   Проверить исходный SHA develop, отсутствие ранней публикации до результата,
   наличие PR и provenance. Просмотреть сохранённый prompt: служебного fence нет;
   оригинальный body в SQLite сохранён, обычный YAML задания присутствует.
4. Создать feature/existing с отдельным коммитом и Issue для неё. Проверить
   initial_sha свежего origin HEAD, branch_existed=1, source_base_branch=NULL.
5. Закрыть PR без merge (rejected), добавить коммит в origin ветку, снова approve.
   Проверить новую попытку от свежего HEAD, тот же PR/ветку, архив старой попытки,
   повторную очистку prompt.
6. Между подготовкой и публикацией продвинуть origin. Проверить отказ обычного
   push, сохранность локального коммита и диагностический комментарий без секретов.
7. Проверить main, отсутствующую базу, дубли YAML/legacy и одновременные Issue на
   одной ветке. Codex не запускается; show/комментарий объясняют причину, другая
   корректная Issue продолжает выполняться. Исправление требует нового approve.
8. Проверить detach/reattach, затем merge/done/cleanup и повторное открытие SQLite.
   Для всех пунктов сохранить ссылки Issue/PR, SHA и вывод show для приёмщика.
