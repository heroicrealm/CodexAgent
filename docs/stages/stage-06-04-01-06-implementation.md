# Реализация 6.4.1.6 — Issue #36

Ветка: `STAGE_06_04_01_06`. Основание: Issue #36, прочитанная через
`gh issue view 36 --repo heroicrealm/CodexAgent --json title,body,url`,
ТЗ 6.4.1.6 и обязательные спецификация, план и аудит 6.4.1.
Утверждённое ТЗ 6.4.1 не изменялось.

## Решение и изменённые файлы

- `storage/migrations.py`, `storage/__init__.py`, `storage/transitions.py`:
  additive migration 608, таблицы `terminal_errors` (решение, publication proof,
  immutable comment outbox) и `configuration_errors` (диагностика по content hash),
  запрет автоматического выхода из terminal, fencing version/iteration,
  транзакционные journal/history/approval writes. Старая история не переписывается.
- `terminal_error.py`, `pr_publication.py`, `scheduler.py`: проверка identity
  канонического PR, durable intent до дальнейших действий, lock на repository/Issue
  между процессами; recovery по outbox, включая подготовку до новой попытки.
  Не создаются второй PR/ветка и новая попытка при обнаружении terminal до запуска.
  Накопленные публикации восстанавливаются даже при stale init проекта.
- `workspace.py`, `cleanup.py`: salvage commit без зависимости от PR diff,
  обычный push с проверкой origin/ветки/ancestry и remote SHA. Ошибка или иной SHA
  оставляет worktree. Cleanup допускается только по publication proof, затем
  действует существующий ownership/preflight механизм этапа 6.4.1.4, включая
  архивы, вложения, symlink/metadata/dirty guards. Ветки, PR и SQLite сохраняются.
- `preparation_error.py`, `github/sync.py`: исправимые YAML/branch ошибки
  возвращают pending/running-before-Codex в discovered, отзывают approve и
  блокируют повтор до изменения содержимого; комментарий через outbox с lost
  response reconciliation. Safe YAML с проверкой дубликатов/ключей/типов;
  существующая чужая remote ветка больше не используется как база новой задачи.
- `config/__init__.py`, `config/config.example.yaml`: точное имя метки
  `dispatcher:review-on-error`, цвет D93F0B, описание и custom override.
  Существующие init/fingerprint/doctor автоматически учитывают новый ключ COLORS.
- `pr_monitor.py`: terminal исключён из мониторинга и старой resolution delivery.
- `cli.py`, `rendering.py`: состояние в queue, terminal/config diagnostics и
  publication/cleanup в show table/JSON без изменения табличного интерфейса.
- `tests/test_issue36.py`: новые unit/integration сценарии. Обновлены ожидания
  миграции и JSON в `test_stage04.py`, `test_cli_unicode.py`, `test_rendering.py`;
  старое ожидание использования чужой ветки заменено проверкой отказа.
- README, example config, общий план и документы этапов 6/6.1/6.4 содержат
  новое состояние, требования к сохранности и ссылки на данный отчёт.

## Явная граница terminal classifier

В terminal переводится только канонический PR, для которого свежий GET подтвердил
repository/Issue/PR identity, head/base, `state=closed`, `merged=true` и корректный
UTC `merged_at`. GitHub не позволяет reopen merged PR. Это включает гонку merge
во время Codex или PATCH reopen и обнаружение перед подготовкой новой попытки.
Один 403/404/422, timeout, закрытый unmerged PR, отсутствующий/противоречивый ответ,
непроверенная identity не доказывают неустранимость: сохраняется retry/diagnostic.
Других terminal причин произвольно не добавлено.

Терминальное решение не равно успешной публикации: `terminal_errors.publication`
остаётся pending при ошибке push. После commit/push и точного remote SHA одной
транзакцией фиксируются proof и immutable comment body. Cleanup не зависит от
доступности доставки комментария, но доставка продолжает retry после удаления
worktree по данным SQLite. Маркер включает глобальный issue_id и iteration;
комментарий ищется перед POST, повтор после потерянного ответа не создаёт дубликат.
Нет network/Git внутри новых SQLite транзакций.

## Проверки

Тесты используют временные SQLite и Git remote, GitHub/Codex fixtures.
Новые сценарии: terminal до подготовки и во время reopen, identity mismatch,
403/404/422/500/transport, push/remote mismatch, lost comment response, label retry,
restart, два независимых издателя, multi-project одинаковые номера, rollback
intent, отсутствие HTTP/Git в транзакции, guards approve/reset/relabel/monitor,
реальный commit/push после рестарта и cleanup. Ошибки YAML (тип, ключи, дубли,
опасные YAML tags), отсутствующая base и чужая ветка остаются discovered.

Среда: отдельный `/tmp/codexagent-issue36-venv` с `pip install -e '.[test]'`.
Начальный штатный запуск без venv не выполнил pytest: модуль отсутствовал в Python
текущей среды. Конфигурация проекта ради установки зависимостей не менялась.

Итоговые проверки:

```text
PATH=/tmp/codexagent-issue36-venv/bin:$PATH python3 -m pytest -q tests/test_issue36.py
30 passed in 1.63s

PATH=/tmp/codexagent-issue36-venv/bin:$PATH bash scripts/test.sh
607 passed, 30 subtests passed in 42.08s
```

Exit code 0: штатный скрипт также выполнил
`python3 -m compileall -q src tests`. `git diff --check` — exit code 0.
Полный прогон включает регрессию предыдущих этапов, миграции, CLI Unicode,
конкурентные операции, cancellation и безопасную очистку.

Промежуточные прогоны обнаружили и исправили восстановление обычного review после
сбоя labels/finish, terminal cleanup gate и фикстуру round-robin, которая использовала
один remote для двух разных GitHub repositories. Теперь у неё независимые remotes;
проверка отказа от чужой ветки отдельно сохранена.

## Ручная приёмка и ограничения

Живые тестовые Issue/PR не создавались: задание прямо запрещает создание PR;
новых PR, merge или закрытия Issue #36 нет. Живой ручной сценарий остаётся
отдельной приёмке в тестовом репозитории. Локальный интеграционный тест покрывает
полную последовательность с настоящим Git remote и контролируемым GitHub API.

Для ручной проверки использовать имеющийся канонический PR: выполнить rework,
перед завершением Codex вручную merge прежнего PR; убедиться в terminal intent,
метке и одном комментарии, точном origin SHA и cleanup. Повторить с недоступным
push: до восстановления сети worktree должен оставаться, после рестарта не должно
быть нового Codex. Повторные ready/approve и закрытие Issue не меняют terminal.
Для unmerged PR с временным отказом PATCH должен остаться retry, без второго PR.
Проверить custom label через init/doctor, show table/JSON и чужой файл перед cleanup.

Неустранимые ситуации без достаточного доказательства остаются диагностируемым
retry с сохранением данных; лимит времени/число HTTP ошибок не превращает их в
terminal. Detach/reattach относятся к следующим этапам 6.4.1.7/.8. Поддержка
параметров подготовки здесь обеспечивает различие классов ошибок; полноценная
матрица нормализации автоматических имён общего ТЗ не расширялась этим этапом.
