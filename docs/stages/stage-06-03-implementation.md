# Этап 6.3: обязательный подтверждённый PR

Реализован Issue #23. Рабочая ветка — STAGE_06_03. PR для приёмки этапа не создаётся.

## Архитектура

`PullRequestPublisher` использует существующие `ExecutionStore`, GitWorkspaceManager,
GitHub Adapter и схему SQLite 6.2. После нулевого exit code Codex scheduler записывает
intent публикации до Git/HTTP операций. Попытка остаётся running, но агент больше
не запускается. При рестарте сохранённый успешный exit code также восстанавливает
intent, если сбой произошёл до его записи. Остальные прерванные исполнения сохраняют
прежнюю обработку interrupted.

`publication_commit` проверяет рабочую ветку, создаёт обычный коммит оставшихся
изменений, проверяет сохранение ancestry и получает актуальную `base_branch` проекта.
Проверяется трёхточечный diff (семантика PR), включая ранее созданные коммиты.
Пустого коммита нет; пустой diff даёт failed / no_diff с сохранением попытки и
диагностики в SQLite. Ни PR, ни review, ни повторного Codex в этом случае нет.
Новая рабочая ветка также создаётся от `base_branch`, а не от hardcoded main.

После обычного push remote SHA подтверждается через ls-remote; затем GitHub REST
подтверждает SHA PR. Поиск использует точные repository/head/base и state=all.
Перед POST выполняется повторный поиск; после потерянного ответа — ещё один.
Существующая запись принимается только при совпадении служебного маркера
repository REST ID / Issue REST ID / iteration ID либо уже сохранённой связи SQLite.
Дополнительно проверяются числовые REST IDs, репозитории обеих веток, head/base,
SHA, open, merged=false, draft=false и changed_files > 0. Чужой, неоднозначный или
закрытый PR останавливает публикацию с publication_conflict; reopen не выполняется.

Заголовок: `[Issue #N] <название>`. Body содержит ссылку на Issue (без closing keyword),
репозиторий, итерацию, ветку, SHA, краткий итог и служебный маркер.
Права на изменение PR body входят в границу доверия GitHub; маркер не является
криптографической подписью.

## Состояния и восстановление

| Фаза операции pr:attempt | Значение |
| --- | --- |
| intent | Успешное исполнение сохранено, публикация ещё не началась |
| verify | Проверка IDs, worktree, commit и diff; ошибка сохраняется с этой фазой |
| push | Намерение push сохранено; повтор сверяет результат обычным push/ls-remote |
| pr | Поиск, создание, повторная сверка и REST проверка PR |
| label | PR evidence сохранён, review подтверждён; синхронизация метки |
| terminal | Нет diff или конфликт принадлежности/версии; автоматическая публикация прекращена |

Outcome pending/failed/completed и retry_count хранятся в publication_operations.
Безопасный код последней ошибки — attempts.recovery_diagnostic; stderr и исключения
туда не копируются. Ошибки push, API, недоступные REST IDs и временные ошибки SQLite
оставляют публикацию для повтора; non-fast-forward не переписывает чужую историю.
Успешная операция больше не публикуется.

CAS transition 6.2 проверяет version, iteration, actor и evidence, записывает
REST pr_id/pr_number/repository_id/issue_id, head/base/SHA, task_pull_requests,
iteration_pr_history и event_key `pr:<attempt_id>` атомарно. Finish технической
попытки разрешает review только после этого перехода и сохранённой истории PR.
Сбой между transition и finish восстанавливается через тот же event_key без
повторной записи истории. Сбой после finish или при изменении метки также повторяет
публикацию без Codex; операция completed отмечается после успешного reconcile.

Для конкурирующих publishers используется отдельный advisory flock на операцию.
Он освобождается ОС при аварии и не держит SQLite-транзакцию во время Git/HTTP.
Обычная блокировка daemon и ограничения SQLite на резервирование сохраняются.
Файлы publication-<hash>.lock находятся рядом с приватной SQLite; удалять их во
время работы нельзя. Это локальная координация на том же filesystem, как и daemon.

## Проверка

```bash
python3 -m venv /tmp/codexagent-tests
/tmp/codexagent-tests/bin/pip install -e '.[test]'
PATH=/tmp/codexagent-tests/bin:$PATH bash scripts/test.sh
```

Автоматические проверки включают настоящий локальный Git/bare remote, daemon через
CLI со stub gh, временную SQLite и mock REST: happy path, существующий/чужой/закрытый
PR, неверные head/base/repository/SHA/IDs, no_diff, коммиты агента и dispatcher,
настраиваемую base, non-fast-forward, сетевые ошибки, потерянный ответ create,
рестарт после push/PR/transition, ошибку метки, rollback частичной записи,
одинаковые номера Issue в разных проектах, конкурентные publishers, повторный
polling и запрет review без evidence/повторного Codex. Проверяется отсутствие
сетевых вызовов внутри SQLite-транзакции. Прежние тесты стадий 1–5 сохранены;
ожидания no-op, незакоммиченных изменений и ошибки push обновлены по ТЗ 6.3.

## Границы этапа

Merge, merge→done, reject/reopen/cancel, удаление веток и общий outbox не добавлены.
Для терминального конфликта требуется отдельное разрешённое решение; автоматический
новый агент или новый PR не создаётся. При недоступной идентичности/свежем server
Date публикация ждёт подтверждения GitHub. Между HTTP-проверкой и SQLite переходом
остаётся обычное окно изменения внешнего PR; дальнейшее наблюдение — этап 6.4.
Ручная проверка с реальным GitHub push/PR оставлена отдельной приёмке: текущая
работа не создаёт PR и не закрывает Issue.

## Результат автоматической проверки

2026-10-24: `PATH=/tmp/codexagent-tests/bin:$PATH bash scripts/test.sh` —
**386 passed, 30 subtests passed in 27.09s**, exit code 0.
Вторая команда скрипта, `python3 -m compileall -q src tests`, также завершилась успешно.
Системный Python не содержал pytest, поэтому зависимости проверки установлены
в отдельное временное venv; зависимости приложения не изменены.

## Доработка Issue #24: последнее сообщение Codex в Summary

`codex_summary.final_summary` читает существующий `<attempt_id>.codex.log`
в UTF-8, выбирает последний непустой строковый `item.text` среди корректных
`item.completed` / `agent_message`. Reasoning, результаты инструментов,
незавершённые сообщения и некорректные JSON-записи пропускаются. Ошибка чтения,
невалидный UTF-8 или отсутствие сообщения дают прежний fallback
`Summary: implementation for Issue #N.`. Сообщения выбираются только для
попыток с сохранёнными `codex_started=1` и `exit_code=0`; нового запуска CLI нет.

Перед Git/HTTP операциями publisher под существующей блокировкой публикации
сохраняет очищенный текст в `attempt_summaries` с первичным/внешним ключом
`attempt_id`. Создание таблицы через `CREATE TABLE IF NOT EXISTS` при открытии
SQLite — аддитивная идемпотентная миграция без сброса данных и изменения версии
разрушительной миграции 6.2. Сохраняется также fallback: после первого выбора
retry и restart читают только SQLite, даже если лог изменён или удалён. Сбой до
фиксации снимка не начинает Git/HTTP публикацию. Существующие PR не редактируются;
поиск, ownership, publication operations и защищённый transition сохранены.

Известные префиксы токенов GitHub (`github_pat_`, `ghp_`, `gho_`, `ghu_`,
`ghs_`, `ghr_`) и OpenAI (`sk-`, включая `sk-proj-`) заменяются на `[REDACTED]`
до сохранения. Признаки иных credentials (API key, access token, password,
secret, Authorization/Bearer, private key) и управляющие символы приводят к
fallback. Это защита от очевидных секретов, а не гарантия распознавания
произвольных зашифрованных или обфусцированных данных. Сырой текст/исключения
не логируются. Текст не исполняется; угловые скобки экранируются для исключения
HTML и поддельных ownership comments, Markdown и переносы сохраняются.

После очистки текст ограничивается 8192 байтами UTF-8, включая явную отметку
`[Summary truncated]`, с обрезкой только на границе символа. Ограничение размера
дополнительно закреплено CHECK в SQLite. Остальные поля PR body не меняются.

Тесты `tests/test_issue24.py` проверяют выбор последнего сообщения и посторонние
события, пустые/битые данные, UTF-8/кириллицу/Markdown, Unicode boundary и ровно
8 KiB, очистку секретов до SQLite/PR, инертный HTML, стабильность fallback,
рестарт с удалённым логом, существующий PR, потерянный ответ create и no_diff.
Полный прогон включает прежние регрессии стадий 1–5 и 6.2/6.3, многопроектность,
конкурентную публикацию и отсутствие повторного Codex.

2026-10-25, Issue #24: `PATH=/tmp/codexagent-tests/bin:$PATH bash scripts/test.sh`
— **416 passed, 30 subtests passed in 20.44s**, exit code 0;
`python3 -m compileall -q src tests` выполнен успешно. `git diff --check` — успешно.
