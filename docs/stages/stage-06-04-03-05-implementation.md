# Реализация 6.4.3.5

Статус: реализация подготовлена; решение о приёмке — после ручной проверки пользователем.

## Архитектура и файлы

- `src/dispatcher/integration.py`: повторное чтение непосредственных детей, семантическое сравнение immutable snapshot, проверка branch claim/origin/HEAD, read-only gate `no_diff`.
- `container_monitor.py`: значимый снимок без `updated_at` и неуправляющих меток; approve/reject/emergency-stop ребёнка блокируют интеграцию. Проверяемые заголовок/URL ребёнка для prompt. После старта допускается чтение evidence, мониторинг по-прежнему выбирает только `awaiting_children`.
- `execution_store.py`: CAS readiness/generation и durable integration operation в одной транзакции с резервированием существующего attempt; одна попытка на итерацию. Manual-контейнер сохраняет `awaiting_children` до публикации PR.
- `scheduler.py`: потребление readiness, общий executor/publication pipeline, восстановление публикации manual-контейнера после рестарта. Первичная подготовка контейнера не требует квоты Codex.
- `codex/__init__.py`: вариант А — очищенное тело контейнера и JSON кратких сведений прямых детей; без тел/комментариев детей и без поручения управлять Git.
- `containers.py`, `storage/transitions.py`: общая безопасная публикация канонической ветки manual Issue и отдельный доказанный переход `pending → review` без attempt/PR/Codex.
- `workspace.py`: проверка fetched HEAD перед подготовкой integration worktree; запрет коммитов при manual-публикации.
- `pr_publication.py`: сохранение результата до повторной проверки детей, проверка remote HEAD, общий поиск канонического PR по `state=all`, публикация manual-контейнера и восстановление доставки после committed review.
- `pr_monitor.py`: обнаружение человеческого PR по закреплённой ветке среди всех состояний, canonical ownership/history, затем существующий механизм `review → done/rejected` и outbox.
- `container_diagnostic.py`, `rendering.py`: дедуплицированная диагностика `no_diff`, отображение `issue_type`/`execution_mode`.
- `tests/test_stage6435.py`: Git/SQLite/process integration tests новых путей; `tests/test_rendering.py`, `tests/test_cli_unicode.py`: ожидаемое новое JSON-поле `execution_mode`.

Этот документ (`docs/stages/stage-06-04-03-05-implementation.md`) содержит карту изменений и план ручной проверки.

Новых таблиц и миграций нет. Используются существующие `container_operations`, immutable integration snapshots, attempts, publication operations, PR identity/history и outbox. Старые снимки не переписываются; монитор получает новый формат при очередном наблюдении. Обычные Codex Issue сохраняют существующий путь.

## Ручная проверка на heroicrealm/CodexAgentPlayground

Все Issue/PR/merge в этом разделе создаёт и подтверждает оператор. В конфигурации отдельного проекта указать `repository: heroicrealm/CodexAgentPlayground`, разрешённого автора и базовую тестовую ветку. Ниже `playground` — ID этого проекта; путь config задаётся через `--config` при необходимости.

1. Выполнить `codex-dispatcher init`, `doctor`, затем `poll`. Проверить готовность проекта. Запустить daemon командой `codex-dispatcher start`.
2. Создать контейнер с отдельным именем ветки и fenced YAML `dispatcher: {issue_type: container, run_codex: true}`. В тексте дать самостоятельное интеграционное задание. Добавить минимум две непосредственные Sub-issues с различимыми изменениями. Всем задать настроенную task label; отдельно подтвердить только контейнер (`approve playground N`).
3. Проверить `show playground N --format json`: закреплённая ветка опубликована, состояние `awaiting_children`; детям предложена base контейнера. Отдельно подтвердить детей. Проверить base их PR; вручную слить оба PR именно в ветку контейнера.
4. После poll проверить один integration attempt и один запуск Codex; в сохранённом prompt — тело контейнера без dispatcher YAML, номера/заголовки/URL и merge/base прямых детей, без их тел и комментариев. Проверить сохранение/публикацию результата, единственный PR контейнера в закреплённую base и `review` только после подтверждения PR. Повторить poll и перезапустить daemon: новых запусков/PR/комментариев быть не должно. Вручную слить PR контейнера, дождаться `done`.
5. Повторить пункты 2–4 для нового контейнера с `run_codex: false` и двумя слитыми детьми. Проверить ноль Codex runs и отсутствие искусственных коммитов; PR содержит diff детей, описание не выдаёт его за результат Codex. До merge контейнера — `review`, после — `done`.
6. Создать обычную Issue с `dispatcher: {issue_type: issue, run_codex: false}`, task label и отдельным подтверждением. Проверить опубликованную origin-ветку и прямой переход `pending → review`: без attempt, Codex и автоматического PR. Человек добавляет коммит, публикует ветку, создаёт и сразу сливает PR между двумя poll; при merge удалить head-ветку. Следующий poll должен закрепить PR/history и установить `done`. На отдельных Issue проверить open PR → ожидание `review`, closed unmerged → `rejected`, два PR с одним head → fail closed. Для собственного merged PR отдельно проверить осознанную смену base.
7. Создать manual-контейнер без детей и без diff. После подтверждения и повторных poll ожидать `awaiting_children`, один активный `no_diff`, одно диагностическое сообщение, без attempt/Codex/PR/новых коммитов. Добавить реальный коммит в его origin-ветку: следующий цикл должен снять `no_diff` и опубликовать единственный PR. Отдельно проверить Codex-контейнер без новых правок Codex, но с diff детей.
8. Негативные проверки на отдельных контейнерах: незавершённый ребёнок, detached ребёнок с open PR, child PR merged в неверную base, вложенный контейнер как прямой ребёнок. До корректного evidence не должно быть запуска/PR; внуки не исполняются контейнером. Неактивная историческая блокировка не препятствует готовности.
9. Во время integration Codex изменить значимый состав детей. Проверить сохранение результата и отсутствие ложного `review`/автоматического повторного запуска. Для сбоя push временно отключить доступ к origin, затем восстановить и перезапустить daemon: результат должен сохраниться, публикация продолжиться без второго Codex.
10. Сверить GitHub refs/PR/comments с `show`/`queue`, SQLite operations/history/outbox и журналами. Остановить daemon после проверки. Зафиксировать фактические результаты и решение о приёмке отдельно.

## Границы проверки

GitHub не даёт атомарный снимок Issue/Sub-issues/PR/refs: используются повторные наблюдения и CAS локального состояния. Базовые проверки и сохранность результатов реализованы; полный повторный цикл после rejected, автоматическая переинтеграция и расширенная аварийная оркестрация остаются 6.4.3.6. Неопубликованный/неоднозначный старый worktree сохраняется; автоматического reset/force-push нет.

Ручная проверка на Playground и запуск реального Codex/GitHub в рамках разработки не выполнялись. Отказы проверяются локальным Git, SQLite и stub API/processes; это не исчерпывающая проверка всех возможных аварийных межпроцессных interleavings. CI после push проверяется отдельно от локального test suite.

## Автоматическая проверка

В рабочем окружении проекта выполнено:

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh
python3 -m compileall -q src tests
git diff --check
```

Финальный полный прогон: **925 passed, 30 subtests passed**, 190.85 секунды; exit code 0. `scripts/test.sh` также выполнил compileall. Отдельная компиляция и проверка whitespace завершились успешно. В новом `test_stage6435.py` — 19 проверок; совместно с `test_stage6434.py` — 72 passed. Dev-зависимости использованы из существующего `.venv` (системный Python не содержит pytest).

Дополнительное поле `execution_mode` в JSON CLI проверяется как явное расширение контракта, при этом тесты продолжают проверять неизменность исходных SQLite rows и Unicode.
