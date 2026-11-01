# 6.4.3.2 — Технический аудит и контракты реализации

**Статус:** проект технических решений для приёмки; код не изменён  
**Основание:** [утверждённое ТЗ 6.4.3](stage-06-04-03-spec.md)  
**База аудита:** ветка `STAGE_06_04_03`, 2026-11-01.

## 1. Проверенные точки интеграции

- `src/dispatcher/storage/migrations.py`: исторический `VERSION=602`, последовательность добавочных `upgrade604…upgrade613`. В `Store.__init__` миграции исполняются в транзакции с `BEGIN IMMEDIATE`. **Следующая версия — 614**, менять `VERSION=602` нельзя.
- `storage/__init__.py`: `tasks` идентифицируются `issue_id` (UNIQUE), а `repository,number` используются для локальных запросов. Имеются `current_iteration_id`, `work_branch` и `base_branch`; `task_iterations`, `task_pull_requests`, `publication_operations` и журналы истории.
- `storage/transitions.py`: охраняемые переходы с доказательствами GitHub; допустимые edges сейчас не включают `awaiting_children`, а `review` ожидает открытый PR. Нужен отдельный проверяемый путь для ручной задачи без PR.
- `github/__init__.py`: `Adapter.fetch` получает открытые Issues, в том числе не ready, но API непосредственных Sub-issues ещё нет; `reconcile` обновляет управляемые метки.
- `github/sync.py`: общий `poll` и согласование меток; расширение мониторинга контейнеров не должно выполнять команды approve за ребёнка.
- `scheduler.py`: `admit` выбирает `pending` для запуска Codex; новая маршрутизация по типу/режиму обязательна.
- `workspace.py`: каноническая ветка, подготовка worktree, push и cleanup; требуется ранняя публикация контейнерной ветки до разрешения детей.
- `config/__init__.py`: явный перечень `COLORS`, проверка меток и init fingerprint. Добавить `awaiting_children` и описание.
- **Не установлено исполнением тестов:** точные ответы Sub-issues REST API, race-поведение GitHub при PUT body, возможные внешние изменения refs. Эти проверки обязательны перед 6.4.3.3.

## 2. Проект миграции 614 (контракт, не исполняемый SQL)

Ниже — проектный DDL. Адаптировать к существующему соглашению о транзакциях, внешних ключах, `schema_migrations` и восстановлению после сбоя. Не создавать второй независимый учёт PR/итераций.

```sql
-- upgrade614(db); выполняется внутри BEGIN IMMEDIATE в Store.__init__
ALTER TABLE tasks ADD COLUMN issue_type TEXT NOT NULL DEFAULT 'issue'
  CHECK(issue_type IN ('issue','container'));
ALTER TABLE tasks ADD COLUMN run_codex INTEGER NOT NULL DEFAULT 1
  CHECK(run_codex IN (0,1));

CREATE TABLE sub_issue_relations (
  parent_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  child_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  observed_at TEXT NOT NULL,
  participation TEXT NOT NULL CHECK(participation IN ('included','ignored','detached','blocked')),
  conflict_code TEXT,
  PRIMARY KEY(parent_issue_id,child_issue_id),
  CHECK(parent_issue_id != child_issue_id)
);
CREATE INDEX sub_issue_relations_child ON sub_issue_relations(child_issue_id);

CREATE TABLE container_integrations (
  integration_id INTEGER PRIMARY KEY,
  container_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  iteration_id INTEGER NOT NULL REFERENCES task_iterations(iteration_id),
  generation INTEGER NOT NULL CHECK(generation>0),
  phase TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  branch_head_sha TEXT,
  frozen_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(container_issue_id,generation)
);
CREATE INDEX container_integrations_iteration ON container_integrations(iteration_id);

CREATE TABLE container_integration_children (
  integration_id INTEGER NOT NULL REFERENCES container_integrations(integration_id),
  child_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  child_state TEXT NOT NULL,
  child_pr_id INTEGER REFERENCES task_pull_requests(pr_id),
  merged_base TEXT,
  merged_at TEXT,
  participation TEXT NOT NULL,
  PRIMARY KEY(integration_id,child_issue_id)
);

CREATE TABLE container_operations (
  operation_key TEXT PRIMARY KEY,
  container_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  iteration_id INTEGER REFERENCES task_iterations(iteration_id),
  kind TEXT NOT NULL,
  target_issue_id INTEGER REFERENCES tasks(issue_id),
  payload_hash TEXT NOT NULL,
  phase TEXT NOT NULL,
  outcome TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  remote_identity TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX container_operations_pending ON container_operations(phase,outcome);

CREATE TABLE container_blockers (
  container_issue_id INTEGER NOT NULL REFERENCES tasks(issue_id),
  blocker_key TEXT NOT NULL,
  child_issue_id INTEGER REFERENCES tasks(issue_id),
  code TEXT NOT NULL,
  details TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(container_issue_id,blocker_key)
);
```

**Обязательные уточнения до реализации:** (1) `tasks` не обязательно содержит ранее не-ready ребёнка: сохранять только учитываемые связи в SQLite; GitHub остаётся источником полного состава; (2) ребёнок из другого репозитория запрещён либо переводится в диагностируемый конфликт согласно утверждённой политике безопасности — не пытаться привязать чужой `issue_id`; (3) `parent_issue_id` — не поле с единственным источником истины: при переносах фиксировать историю связей и конфликт, не терять прежнее состояние; (4) не хранить секреты/полный GitHub response в `details`; (5) SELECT/INSERT и установка marker `614` должны быть атомарными и идемпотентными; (6) гарантировать совместимость существующих баз и сохранение всех PR/attempt/outbox данных.

**Рекомендация по реализации:** для миграции 614 вместо безусловных `ALTER TABLE` сначала проверить `PRAGMA table_info(tasks)`; для CREATE использовать `IF NOT EXISTS`. Индексы и `PRAGMA foreign_key_check`/`integrity_check` до записи marker. Не переписывать старые `snapshot` и `approved_version`. Поля `issue_type/run_codex` синхронизировать только при безопасном изменении подтверждённой версии; уже работающую задачу не переназначать.

## 3. Формальные контракты компонентов

| Контракт | Вход | Выход / условие |
|---|---|---|
| `Adapter.list_direct_children(parent)` | repository_id, parent issue identity | Полный **непосредственный** список со стабильными ID, пагинацией и доказательством полноты; при ошибке — no mutation |
| `ContainerService.prepare(parent)` | подтверждённая текущая итерация | Каноническая ветка закреплена, опубликована и проверена на origin до изменения детей |
| `ContainerService.reconcile_child(child)` | снимок child + parent branch | CAS-правка только `dispatcher.base_branch`, не выдающая approve; конфликт — blocker |
| `ContainerService.evaluate(parent)` | непосредственные дети + свежие PR | ready только при терминальных состояниях и доказательстве merged-to-parent для каждой done |
| `ContainerService.freeze(parent)` | текущая generation + проверенные дети | неизменяемая таблица участвующих детей на поколение; повторная проверка перед review |
| `ManualIssueService.prepare(issue)` | approved iteration | ветка опубликована; `review` без Codex и без создания PR |
| `ManualIssueService.resolve_pr(issue)` | `state=all`, head/base, PR identity | однозначный merged -> done; closed unmerged -> rejected; ambiguity -> blocker |
| `ContainerService.recover()` | незавершённые operation intents | перепроверка удалённой стороны до повторной записи, без дублей |

**Временная граница GitHub ↔ SQLite:** общей транзакции нет. Сначала устойчивое намерение, потом внешний вызов, после него проверка факта и запись результата. После crash проверять удалённую реальность. Для правки GitHub Issue body использовать условную проверку свежести/повторное чтение; если API не гарантирует CAS, нельзя заявлять абсолютную защиту от гонки — при неоднозначности блокировать.

**Новые переходы:** `pending → awaiting_children`; `awaiting_children → running` (container + Codex); `awaiting_children → review` (container no-Codex с подтверждённым PR); `running → awaiting_children` (изменённый состав с сохранением результата); `rejected → pending → awaiting_children` для повторного цикла. Для manual: `pending → review` без PR при подготовленной ветке; `review → done/rejected` только по проверенному PR. Не ослаблять действующие проверки `running/review/done` у обычных Codex Issue.

**Инварианты:** одна Issue / одна branch / один canonical PR; не рекурсировать; не ставить ready/approve детям; не делать auto-merge/force push; не закрывать детский PR; сохранять результаты при detach; не завершать контейнер при `child_detached_with_open_pr`; не создавать пустых коммитов при `no_diff`; очищать только owned worktree при выполнении правил безопасности.

## 4. План тестирования и критерии принятия 6.4.3.2

| ID | Проверка | Ожидаемый результат |
|---|---|---|
| DB-01 | Миграция существующей базы 613 → 614 | Данные, PR, попытки, outbox и история сохранены |
| DB-02 | Повторное открытие БД и сбой в середине DDL | Нет повторов и частично применённой схемы |
| DB-03 | Конкурентные открытия/transition с CAS | Один победитель, без потери истории |
| GH-01 | Нулевая, многостраничная, неполная выдача Sub-issues | Нет ложного готового контейнера |
| GH-02 | Обновление YAML одновременно с правкой человека | Нет тихой потери текста/неправомерного approve |
| GH-03 | Перенос/удаление связи активного ребёнка | Blocker, без автоматического reroute |
| BR-01 | Сбой после публикации контейнерной ветки | Recovery находит свою ветку, не создаёт вторую |
| PR-01 | Ручной PR создан и слит между двумя poll | Найден `state=all`; подтверждённый done |
| PR-02 | Два PR для пары head/base, чужой PR, неверный base | Диагностика, без произвольной привязки |
| CN-01 | done/cancelled + ignored non-ready | Проверяются только учитываемые дети |
| CN-02 | Ребёнок detached с открытым PR | `child_detached_with_open_pr` и одна диагностика |
| CN-03 | Изменение состава во время Codex | Результат сохранён; новая интеграция без преждевременного review |
| CN-04 | Нет diff / нет учитываемых детей | Нет искусственных коммитов и ложного review |
| CN-05 | Вложенный контейнер | Проверка только непосредственного child PR/merge |
| RG-01 | Регрессия 1–6.4.2, detach, review_on_error | Ранее принятые гарантии сохранены |

**Выход 6.4.3.2:** согласованы модель БД, API-контракты, новые переходы, инварианты, поведение при сбоях и тестовая матрица. Выполнение тестов и внесение кода — в последующих подэтапах. Подэтап не принимается автоматически лишь на основании документа.

## 5. Открытые проверки перед кодированием 6.4.3.3

1. Проверить GitHub REST endpoint Sub-issues, полномочия, пагинацию и реакции на удалённые/закрытые Issue (в `CodexAgentPlayground`).
2. Проверить пригодность conditional update для body; при отсутствии атомарного CAS утвердить диагностируемый режим fail-closed.
3. Уточнить семантику ручной Issue с отсутствующим PR при `review` и существующие PR-triggers, чтобы не имитировать PR evidence.
4. Проверить, что ранний `push` не нарушает текущие branch ownership, cleanup и retry-source инварианты.
5. После проверки результатов сохранить итоговую матрицу фактических тестов; до этого проверки считаются запланированными, а не пройденными.

## 6. Заключение

Крупный рефакторинг не требуется; для 6.4.3 необходимы добавочная миграция 614, сервис непосредственных детей и контейнерных поколений, расширение переходов и отдельная ручная ветка исполнения. Код и миграция в рамках аудита **не исполнялись**. Переход к 6.4.3.3 — после согласования настоящего технического контракта.
