# ТЗ 6.2 — Модель состояний, GitHub ID и миграции SQLite

**Проект:** heroicrealm/CodexAgent  
**Ветка:** STAGE_06_02 от STAGE_06  
**Дата:** 2026-10-24  
**Статус:** утверждено.

## 1. Цель и границы
Реализовать основу этапов 6.3–6.7: канонические числовые GitHub REST ID, отдельные логические итерации и технические попытки, атомарные защищённые переходы, связи Issue–PR, журнал решений/переходов, операции публикации и одноразовую миграцию SQLite. Сохранить действующие CLI и архитектуру stages 1–5. Не реализовывать в 6.2 создание/открытие PR, мониторинг merge, внешнее закрытие Issue, удаление веток или полноценный retry публикации: это 6.3–6.7. Новые переходы должны быть готовы в API и тестах, но не должны создавать ложный review в действующем runtime; переключение push→PR→review выполняется в 6.3.

## 2. Основание
Соблюдать docs/stages/stage-06-spec.md, stage-06-01-spec.md, stage-06-01-architecture.md, stage-06-01-audit.md и stage-06-plan.md. При противоречиях учитывать принятое решение: продуктивных данных нет, одноразовая очистка операционных таблиц разрешена; не требуется сохранять старую историю, но резервная копия обязательна. Не очищать данные при обычном старте или повторной миграции.

## 3. Идентификаторы
Канонические связи использовать по положительным 64-bit числовым GitHub REST ID: repository_id, issue_id, pr_id. Не путать pr_id с issue_id GitHub, соответствующим PR, и с GraphQL node_id. Уникальность issue_id, (repository_id, issue_number), pr_id, (repository_id, pr_number). Сохранять repository owner/name и номера для UI/CLI и запросов API; команды show PROJECT NUMBER, queue и JSON-контракт сохранять. Не доверять совпадению номера между проектами. Проверять GitHub identity перед привязкой, никогда не генерировать фиктивный ID.

## 4. Итерации и попытки
Одна задача соответствует одной Issue. Первое авторизованное approve создаёт iteration_number=1. После rejected новое авторизованное approve создаёт следующую итерацию, даже если version/body не изменились. Повтор того же события не создаёт итерацию. Каждая итерация имеет отдельные технические попытки Codex с attempt_number, timestamps, outcome, SHA и диагностикой. Retry публикации/PR/outbox не является новой попыткой Codex. Автоматический повтор Codex в рамках одной итерации без отдельного разрешения запрещён. История PR и SHA должна сохранять привязку к итерациям; при доработке используется тот же PR.

## 5. Состояния
Поддержать:
discovered --approve--> pending --reserve--> running;
running --verified PR--> review;
review --verified merged PR--> done;
review --verified closed unmerged PR--> rejected;
rejected --new authorized approve--> pending (новая итерация);
rejected --authorized cancel--> cancelled.
done/cancelled необратимы даже при reopen Issue/перестановке меток. Существующие failed/interrupted/revoked и их правила сохранять, без новых несанкционированных путей retry. publishing — внутренняя фаза, не публичная метка. Без доказанного PR нельзя переводить в review по новому контракту; при миграции не переинтерпретировать старый review как подтверждённый PR.

Все переходы выполняются атомарно, с CAS-проверкой expected_state/version/iteration, actor/authority, event_key, evidence, записью истории в той же транзакции. Результаты: applied, already_applied, invalid_transition, stale_version, unauthorized, missing_evidence, identity_unresolved, conflict. Не применять переход при неполном доказательстве или недоступном GitHub. Повторные события идемпотентны.

## 6. Роли
Issue owner принимает решение о повторном approve и окончательной отмене. Merge owner выполняет merge в GitHub; диспетчер никогда не делает merge. Роли могут совпадать. Базовое доверие — существующий security.allowed_authors; предусмотреть расширяемую авторизацию, сохранив действующие approve/reject. Не считать текст комментария командой. Для rejected→pending обязательно новое подтверждённое событие, привязанное к текущему rejected/итерации, но редактирование Issue не требуется. Проверять actor, серверную свежесть события, version и статус. Смысловую актуальность ТЗ диспетчер не оценивает.

## 7. SQLite
Расширять существующие Store, ExecutionStore, attempts, history, decisions, result_outbox, не создавать параллельную систему. Логически необходимы tasks с issue_id/repository_id/current_iteration_id; task_iterations(iteration_id, issue_id, iteration_number, approved_version, decision_id, created_at); attempts с iteration_id/attempt_number; task_pull_requests(pr_id, issue_id, repository_id, pr_number, head/base, head_sha, state, merged_at); task_transitions(event_key, actor, from/to, iteration, time); publication_operations(operation_key, phase, outcome, retry_count); schema_migrations. Физические названия адаптировать к текущему коду, задокументировать. Уникальные ограничения для IDs, (issue_id,iteration_number), (iteration_id,attempt_number), operation_key. Включить PRAGMA foreign_keys=ON. Не разрешать вторую активную итерацию и конкурентные резервирования.

## 8. Одноразовая миграция и очистка
Продуктивных данных нет. Разрешено ОДНОКРАТНО очистить старые операционные таблицы (задачи, попытки, история, решения, outbox и зависимые записи) вместо переноса старой истории и ключей. До изменения существующей SQLite создать согласованную резервную копию; если backup не удался, ничего не очищать. Миграция запрещена при активном диспетчере и должна учитывать lock. DDL и очистку выполнить транзакционно с проверкой FK, целостности и записью schema_migrations. При ошибке rollback, сохранить резервную копию, выдать безопасную диагностику. Повторный запуск НЕ очищает новые данные. Не менять GitHub Issues/PR/ветки. После очистки задачи могут обнаруживаться заново через polling; при отсутствии GitHub ID не допускать новых операций этапа 6, обеспечить безопасное разрешение ID при последующей синхронизации. Сетевые запросы не держать внутри длительных транзакций SQLite. Старые уникальные ключи (repository,number,version) не должны препятствовать новой итерации той же версии.

## 9. API
Единый слой state transitions, используемый CLI/scheduler/sync, с атомарной записью transition и decision. Поддержать типизированные ошибки, idempotency, проверку ownership/ID и evidence. При работе с PR хранить привязку задачи и историю SHA по итерациям, не устанавливать связь только по названию ветки. Операции publication сохраняют intent, phase/outcome/retries для будущего восстановления.

## 10. Тесты
Проверить: новая БД; одноразовая миграция старой БД с backup и очисткой; повтор миграции без удаления новых данных; rollback при сбое; foreign keys; два репозитория с issue #1; конфликт GitHub ID; unavailable ID; первое approve; rejected→новая итерация при той же версии; повтор approve; несколько технических попыток без нарушения уникальности; retry публикации без Codex; review→done только с merge evidence; review→rejected только closed unmerged; stale approve; терминальность done/cancelled; concurrent transitions; сохранение CLI queue/show и JSON; регрессия этапов 1–5. Использовать mock GitHub, временные SQLite. Запуск: bash scripts/test.sh.

## 11. Приёмка и выход
Изменения в STAGE_06_02, тесты, документация новой схемы/контрактов, docs/stages/stage-06-02-implementation.md с миграцией, рисками и результатами проверок. Не менять scope следующих этапов. Агент выполняет commit и push в STAGE_06_02; PR в STAGE_06 оформляется только после отдельной приёмки.
