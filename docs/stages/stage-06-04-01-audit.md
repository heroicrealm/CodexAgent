# Аудит 6.4.1.1: единая ветка/PR, detach и reattach

Дата: 2026-10-25. Основание: [Issue #29](https://github.com/heroicrealm/CodexAgent/issues/29),
[ТЗ 6.4.1](stage-06-04-01-spec.md), [план](stage-06-04-01-plan.md).
Исследован HEAD `def17f94ff0141d5ae1a6981232d318e2f9162f3` ветки `STAGE_06_04_01`.
Описание Issue получено до исследования через `gh issue view 29 --repo heroicrealm/CodexAgent --json number,title,body,url,state`.
Обычный вызов без `--json` завершился ошибкой устаревшего GraphQL Projects classic.

Это отчёт и проект для согласования, не реализация 6.4.1.2–6.4.1.7.
Runtime, миграции, конфиги, тесты и нормативные ТЗ не изменены.
Предложения ниже не означают выбора неоднозначной политики заказчика.

## 1. Вывод и границы доказательств

Действующая 6.4 реализует прежний норматив: новое approve после rejected создаёт
новую итерацию, ветку и PR. Переиспользованию PR препятствуют не только scheduler/Git,
но и переходы SQLite, одноразовые resolution keys и схема `pr_resolutions`.
Снятие ready приводит к `available=0`/`revoked`, но не запускает detach delivery.
Для running это особенно существенно: выполнение продолжается, push возможен,
а последующий переход в review отвергается из-за `Evidence.available=False`.
Нельзя исправить поведение одним PATCH PR или заменой имени ветки.

Сохранить следует: REST identity, авторизацию человека, server fence, content version,
CAS/журналирование, обычный push, подтверждение remote SHA, сохранение артефактов при
ошибках, outbox и запрет автоматического повторного Codex. Добавить сквозной fence
tracking generation и отдельный путь сохранения результатов независимо от eligibility.

Выводы о runtime получены чтением кода и существующих тестов. Проверки реального
reopen/detach в GitHub не выполнялись. Продуктивная SQLite не исследовалась;
проект миграции основан на DDL, upgrade-коде и временных БД тестов.

## 2. Карта вызовов и контрактов

Ссылки на исходники ниже относительны этому документу; имена функций фиксируют
точки изменения независимо от смещения строк в следующих этапах.

| Вход / модуль | Текущий вызов и контракт | Что потребуется |
|---|---|---|
| [cli.py](../../src/dispatcher/cli.py), `main` | `start`/`_serve` → runtime; `poll`/watch → SyncService; approve/reject → `decide_cli`; queue/show читают Store | Отобразить generation, detach, canonical/legacy, publication и cleanup diagnostics |
| [runtime](../../src/dispatcher/runtime/__init__.py), `serve`, `Lifecycle` | Process-wide lock, child registry/recovery, submit/drain/cancel | Не использовать cancel_child для detach; сохранить stop/force/timeout как отдельные причины |
| [github/sync.py](../../src/dispatcher/github/sync.py), `SyncService.poll` | ready(project) → snapshot before → `Adapter.fetch` → `Store.apply` → `commands` → PR monitor → cancellation delivery → reconcile всех tasks | Проверка ready timeline до команд/мониторинга; detach/reattach intents и фильтры, обязательный recovery отдельно от обычного мониторинга |
| [github adapter](../../src/dispatcher/github/__init__.py), `fetch` | Все open Issue, включая без ready; PR исключены; полный пагинированный snapshot; eligibility = open + label + allowed author | Не смешивать отсутствие ready с закрытием/недоступностью/сменой автора; дочитывать известные Issue и timeline |
| [queue.py](../../src/dispatcher/queue.py), `commands`, `_reason` | GraphQL identity/content/lastEditedAt + REST events; latest labeled; human allowlist; server fence; quarantine/decision receipt | Привязать доказательства и карантин к generation; прежние approve не переносить |
| `queue.decide_cli` | Полный fresh fetch/apply, expected version/state/observation → `Store.decide`, затем reconcile | Добавить generation и запрет решений при detach/drain |
| [Store](../../src/dispatcher/storage/__init__.py), `confirm`, `decide`, `_new_iteration` | Approve создаёт iteration; rejected повтор требует expected_state; CAS/аудит/decision/cancellation intent транзакционны | Все пути, включая internal confirm, должны соблюдать generation и terminal/detached guards |
| [transitions.py](../../src/dispatcher/storage/transitions.py), `transition` | BEGIN IMMEDIATE, role/evidence/time/version/iteration; target running → `ExecutionStore._reserve`; review требует verified open PR | Разрешить canonical PR в разных iterations, запретить чужой/второй canonical; добавить generation и review_on_error |
| [scheduler.py](../../src/dispatcher/scheduler.py), `Dispatcher.admit` | pending+available+approved → quota → fresh fetch/apply → branch → previous attempt/rejected SHA → `retry_source` → reserve → prepare → attachments → executor | Брать branch из canonical binding, verify PR/remote до запуска; generation CAS и общий issue lock |
| [execution_store.py](../../src/dispatcher/execution_store.py), `reserve`, `_reserve` | Проверяет observation, capacity, отсутствие running по Issue/branch; один автоматический attempt на iteration; durable attempt до Git/Codex | Не ослаблять запрет Codex retry; сериализовать новый запуск с публикацией/cleanup старой generation |
| [workspace.py](../../src/dispatcher/workspace.py), `branch`, `prepare`, `retry_source` | Первая branch `codex/issue-N` либо target; retry ветка задаётся scheduler; clean старый worktree архивируется; `worktree add -b`; existing retry ref запрещён | Owned reuse/reattach прежней ветки вместо `-b`; проверить origin/common-dir/ancestry/SHA; не reset и не брать base как fallback |
| [attachments.py](../../src/dispatcher/attachments.py), `prepare`; [codex](../../src/dispatcher/codex/__init__.py), `start` | Вложения вне repo, manifest; prompt/log в artifact registry; Codex должен commit, не push/PR; child через Lifecycle.submit | Не удалять используемые файлы при running; привязать ownership к attempt/generation |
| `Dispatcher.collect` | Child success → PR intent → publisher; failed/timeout/force → finish; active entry удаляется; pending publication остаётся для flush | Detach drain после любого результата child; отделить salvage/push от normal review; не отменять Codex из-за ready |
| [pr_publication.py](../../src/dispatcher/pr_publication.py), `intent`, `flush`, `publish`, `_publish` | key `pr:attempt`, flock на attempt; commit → push → ls-remote → find/create → GET/identity → transition review → finish/outbox → label | Issue lock, canonical selection/reopen, generation checks, durable salvage/reopen/cleanup phases |
| [pr_monitor.py](../../src/dispatcher/pr_monitor.py), `poll`, `resolve` | Текущий review PR либо незаконченная resolution; GET PR → done/rejected intent → transition → Issue close/comment/labels | Повторные close/reopen циклы одного PR, generation fence, исключение detached/terminal из новых наблюдений |
| [publisher.py](../../src/dispatcher/publisher.py), `TaskResultPublisher.flush` | Immutable body/attempt marker, reconcile comments, outbox retry | Вынести HTTP из SQLite writer transaction; generation-aware доставка без старых status effects |
| [cancellation.py](../../src/dispatcher/cancellation.py), `poll`, `cleanup` | Cancel intent → verified Issue not_planned → ownership checks → TaskWorkspaceCleanup | Сохранить отдельный cancel workflow; не использовать его для detach (он закрывает Issue) |
| `TaskWorkspaceCleanup._cleanup` | Только done/cancelled, preflight registry/manifest/symlinks, чистота включая ignored, remove owned files/worktrees | Разрешение cleanup через проверенный operation proof для detach/error, а не просто расширенный список states |

### Точный путь повторного запуска и публикации

`SyncService.poll → commands → Store.decide(rejected, approve) → _new_iteration → pending`;
либо `CLI main → decide_cli → Store.decide`. Затем
`Dispatcher.run → admit → GitWorkspaceManager.branch → previous attempt lookup →
rejected resolution head_sha (иначе result_sha) → retry_source → branch=
codex/issue-N-iteration-I → ExecutionStore.reserve → prepare → attachments.prepare →
CodexExecutor.start → Lifecycle.submit`.

`run → collect → exit_code persist → PullRequestPublisher.intent → publish/_publish →
publication_commit → push → ls-remote → pulls?state=all&head&base → POST если нет →
GET PR → Store.transition(running,review) → ExecutionStore.finish(review) →
result_outbox/comment intent → reconcile`. `run` отдельно вызывает `pr_publisher.flush`
и `publisher.flush`, без нового Codex. `publication_commit` создаёт commit при dirty,
проверяет ancestry и diff с configured base; push только `HEAD:refs/heads/branch`.
`+` в fetch base относится к локальному remote-tracking ref, это не force push.

`SyncService.poll → PullRequestMonitor.poll/resolve → GET → resolution intent →
pr_resolutions → transition(review,done/rejected) → Issue completed для done →
комментарий → reconcile → completed`. Для done вызова cleanup здесь нет;
`TaskWorkspaceCleanup` вызывается только из cancellation delivery. Не считать
наличие класса доказательством работающей очистки после merge.

## 3. GitHub API: что есть и проект reopen

В adapter есть generic `write(endpoint, method, payload)`, технически пригодный для
PATCH; выделенного reopen и вызовов PATCH state=open для PR нет.
`request` повторяет transport/timeout/403/429/5xx с ограниченным ожиданием, сохраняет
HTTP Date. GET 404/422 возвращает GitHubError без такого retry. `write` выполняет
один запрос, даёт структурированные status/kind, не сохраняет Date своего ответа.
`safe_reason` знает 404/422, но publisher ловит GitHubError общей веткой retry,
не классифицируя невозможность reopen. Closed/merged PR сейчас отклоняется
проверкой `state=open, merged=False` как publication_conflict.

Официальный [REST Update a pull request](https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request)
поддерживает PATCH `pulls/{pull_number}` с `state: open`; документация указывает
422 для validation/anti-spam. GET документирует 404. Эти коды сами по себе не
доказывают необратимость ошибки. Это ограничение API, последующий алгоритм —
предложение проекта, а не проверенный интеграцией результат.

Предлагаемый алгоритм под общим lock(issue_id):

1. Persist intent с operation ID, generation, attempt, canonical pr_id/head/base,
   expected local/remote SHA. Проверить repository REST ID и Issue identity.
2. Сверить владение веткой, ancestry, commit/push и remote SHA; не выполнять force.
   Если неопределён исход push — ls-remote до нового эффекта. Если remote впереди
   или расходится, сохранить worktree и диагностику, не reset.
3. GET сохранённого PR по repository/number; проверить REST pr_id, обе repo ID,
   Issue binding из SQLite/проверенного ownership, head/base/SHA, merged/state.
   Совпадение названия/номера/текста маркера само по себе не разрешает присвоение.
4. Уже open и точный SHA → подтвердить результат; closed unmerged → persist reopen
   intent, PATCH state=open; merged → не reopen/POST, диагностировать гонку.
5. После PATCH, включая timeout/потерянный ответ, GET того же PR. Только доказанный
   open+unmerged+expected head SHA позволяет CAS в review текущей generation.
   Если человек вновь закрыл PR между PATCH и GET, не зацикливать reopen вслепую:
   сохранить новое наблюдение и запросить решение по политике (§10).
6. 429/5xx/transport → durable retry без Codex. 401/403/404 → проверить доступ и
   identity, сохранить блокировку с диагностикой. 422 → повторный GET состояния,
   доступности head/base и проверка причины в безопасной классификации. Порог и
   достаточные доказательства terminal failure требуют согласования (§10).
7. Доказанная неразрешимость → review_on_error, никогда POST второго PR.

Первый PR: только при отсутствии canonical и доказанном отсутствии прежнего PR;
поиск state=all сохраняется, binding фиксируется транзакционно. Сейчас повторный
поиск до POST и после ошибки уменьшает дубли, но не является атомарностью с GitHub.
Нужны issue-level lock и durable create intent/ownership token: при потерянном ответе
сначала reconcile; при неполной/неоднозначной выдаче не POST. Повторные итерации
вообще не должны попадать в create path. SQL UNIQUE защищает локальную привязку,
но не предотвращает создание человеком PR во внешней системе.

## 4. SQLite и проект миграции

### Текущее хранение и конфликтующие ограничения

DDL/триггеры: [storage/migrations.py](../../src/dispatcher/storage/migrations.py),
`SCHEMA`, `TRIGGERS`, `prepare`, `initialize`, `upgrade604`.

| Объект | Текущее устройство / риск |
|---|---|
| tasks | PK(repository,number), UNIQUE issue_id, UNIQUE(repository_id,number), UNIQUE(issue_id,repository_id); state TEXT без enum CHECK, available bool, version/hash, current_iteration_id composite FK |
| task_iterations | UNIQUE(issue_id,iteration_number), UNIQUE(iteration_id,issue_id); approve создаёт новую iteration, это не tracking generation |
| attempts | iteration FK + trigger identity; UNIQUE(iteration_id,attempt_number); partial UNIQUE attempt_branch(repository,branch) WHERE running; прикладная проверка running по Issue, отдельного unique active Issue нет |
| task_pull_requests | PK pr_id, UNIQUE(repository_id,pr_number), composite FK Issue/repo; UNIQUE issue_id снят upgrade604, legacy multi-PR допустим |
| iteration_pr_history | Append records SHA/iteration/PR, identity trigger; нет ограничения один PR на iteration и нет immutable полной истории PR state |
| transitions | PK event_key, Issue/iteration/evidence; код запрещает linked PR из другой iteration при target review — ключевой конфликт с новым ТЗ |
| pr_resolutions | PK pr_id; iteration_id сохраняется при INSERT, ON CONFLICT обновляет лишь target/metadata; comment/closed_at/error одноразовые |
| publication_operations | PK operation_key, FK iteration, phase/outcome/retry; key resolution:pr_id и completed необратимы в Store.publication |
| decisions / guards / quarantine | UNIQUE(repository,event_id); guard и quarantine по repo/number(/action); generation отсутствует |
| outbox / audit | result_outbox PK attempt, attempt_summaries immutable body, history/decisions/task_transitions; cancellations PK issue_id |

`resolution:pr_id` повторного закрытия вернёт старую квитанцию либо конфликт;
`pr_resolutions` смешает metadata нового решения со старой iteration/comment_id.
Менять только SQL cardinality PR недостаточно. Partial attempt_branch можно
сохранить: она запрещает одновременную работу, но позволяет последовательное
переиспользование ветки. Identity triggers следует сохранить и дополнить для
новых связей; снимать защиту межпроектной идентичности нельзя.

### Предлагаемая логическая схема (имена и номер миграции согласовать)

- `task_tracking_generations(issue_id,generation,started_event_id,started_at,
  detached_at,detached_by,detach_event_id,status)`; PK(issue_id,generation),
  generation > 0; проверенные event identity/UTC + отдельное observed_at.
  Tasks хранит текущую generation и detached/draining; отсутствие actor не
  заменяется Issue author. FK current generation и guards в каждом mutator.
- `task_canonical_bindings(issue_id PRIMARY KEY,repository_id,branch,pr_id,
  binding_status,ownership_evidence,last_published_sha)`; UNIQUE pr_id,
  UNIQUE(repository_id,branch); composite FK на Issue/repo и PR/Issue/repo,
  для чего нужен соответствующий composite UNIQUE у PR. pr_id nullable до первого
  создания; unresolved legacy остаётся без выбранного PR и не допускается к запуску.
- Все прежние PR остаются в `task_pull_requests` и history; отдельная классификация
  canonical/legacy/unresolved (или таблица классификаций с reason/evidence).
  Не добавлять обратно UNIQUE(issue_id) к исторической таблице.
- Generation FK у iterations/attempts, decisions/transitions, command guards,
  quarantine и операций. Version, iteration и generation решают разные задачи:
  content revision, разрешённый запуск, период отслеживания соответственно.
- Resolution episodes: отдельный resolution_id, UNIQUE(pr_id,generation,iteration_id,
  event_identity) с проверкой Issue; keys включают episode, а не только PR.
  Каждое closed→reopen→closed сохраняет actor/time/SHA/target/comment независимо.
- Расширить existing publication_operations связями issue_id/generation/attempt,
  kind и payload/expected refs, error_code, next_retry_at, результатом проверки,
  cleanup status. Detach возможен до первой iteration: обязательный iteration FK
  сейчас этому мешает; сделать его nullable с CHECK по kind, сохранив старые строки.
  Outbox новых lifecycle-комментариев связывать с operation, не с выдуманным attempt.
- Persist ownership proof локальных ресурсов/ref до их создания/присвоения;
  partial unique active operation/attempt по Issue дополнить в БД после проверки
  legacy конфликтов. Одновременные generations не владеют одним worktree.

### Безопасный порядок миграции

1. Остановить daemon, исключить poll/CLI writers общим runtime/migration lock;
   проверить schema markers и integrity/FK, составить inventory counts/IDs, active
   attempts/intents, PR multiplicity, branch collisions. Не считать running attempt
   разрешением перезапустить Codex.
2. SQLite backup API с учётом WAL → отдельный 0600 backup → integrity_check, fsync
   файла/каталога; отказ backup прерывает миграцию до DDL. Не копировать только .db.
3. Новая versioned migration после 604, в одной BEGIN IMMEDIATE транзакции;
   additive tables/columns, controlled rebuild только требующих этого таблиц,
   named column copies (старые INSERT VALUES без имён тоже потребуют обновления).
   Не переиспользовать destructive reset `prepare` 602: он удаляет операционную
   историю. Его старое разрешение на reset не действует для 6.4.1.
4. Backfill generation=1 с origin=legacy/unknown start event; не выдумывать Timeline
   actor/event. Старые решения/attempts/outbox остаются в generation 1 с исходными
   ключами. Legacy operation adapter завершает только разрешённые старые эффекты;
   не переиздаёт старые approve и не обновляет новую generation.
5. Ноль PR: связь branch только при доказанной однозначности attempts/ownership,
   иначе unresolved. Один PR: проверить identity, историю, head/base и branch
   ownership; одного COUNT=1 недостаточно. Несколько PR: сохранить все и выставить
   unresolved, если нет однозначного доказательства. MAX(pr_id), latest row или
   последний open не являются достаточным правилом. Онлайн GET-проверки делать
   после commit миграции отдельным reconciliation, не внутри writer transaction.
6. Старые `available=0` не считать автоматически detach: причиной мог быть closed
   Issue, недоверенный автор или прежний отзыв. Отдельная сверка snapshot/timeline;
   до разрешения неоднозначности не планировать. Terminal states не оживлять.
7. Проверить counts, все PK/FK, PR/attempt/history/outbox payloads и сохранённые
   ключи, integrity_check, затем migration marker и commit. Повторный запуск no-op.
   Сбой до commit → rollback; после commit → recovery intents, не повтор DDL.
8. Откат до новых внешних эффектов — остановить writers и восстановить backup.
   После новых push/PATCH нельзя просто вернуть БД: сначала reconciliation GitHub
   с журналом, иначе потеряются квитанции. Старый binary не запускать на новой схеме;
   предусмотреть явную проверку несовместимой версии.

## 5. Состояния и границы исполнения

`review_on_error` — публичный terminal, label `dispatcher:review-on-error`.
`detached` — внутренний признак tracking, а не cancelled/revoked и не новая label.
Терминальность и завершённость delivery/cleanup независимы: terminal задача может
иметь незавершённый push, который обязан восстановиться без Codex.

Нужно проверить не один enum, а все входы: `Store._set_state` сейчас защищает лишь
`done/cancelled`; `apply`, `decide`, `confirm`, `reset_confirmation`, transitions,
reserve, scheduler recovery/collect, publisher, monitor, CLI и reconcile должны
соблюдать terminal/generation/draining. В SQL нет state CHECK, но `ExecutionStore.finish`
разрешает лишь review/failed/interrupted: нельзя механически подставить туда новое
состояние задачи. Outcome attempt и terminal task могут храниться раздельно.

В [config](../../src/dispatcher/config/__init__.py) `COLORS` используется для загрузки
и управляемых labels. `Project.label` по умолчанию строит `dispatcher:{key}`: для
ключа `review_on_error` получится неверное подчёркивание. Нужен явный mapping
на дефис и конфигурируемый override; цвет/описание согласовать. В
[initialization](../../src/dispatcher/initialization.py) `initialize` обходит COLORS,
а `fingerprint` зависит от их имён: новая label делает старый init stale, требуется
повторный init с честным init_status; цвета/description в fingerprint сейчас не входят.
`Adapter.reconcile` должен знать новый terminal и не навязывать обычные индикаторы
после detach. `rendering.display_task/render_queue/render_show`, палитра и списки
секций show требуют новых полей без нарушения принятой таблицы/JSON.

Алгоритм review_on_error: persist причина/terminal intent → сохранить и подтвердить
push → атомарно terminal decision и одноразовый lifecycle outbox → комментарий с
причиной, canonical branch/SHA/PR, publication status и ручным действием → owned cleanup.
Если terminal фиксируется до push (вариант модели), delivery остаётся обязательной:
ни scheduler, ни обычный PR monitor не возобновляются, но push retry продолжает жить.
В любом варианте cleanup заблокирован без publication proof. Метки/ручной PR не
возвращают terminal в review; решение человека в GitHub не является автоматическим
SQLite переходом. Точный операторский способ отражения завершения требует §10.

## 6. Detach и reattach: предлагаемые алгоритмы

### Обнаружение снятия ready

`fetch` уже получает open Issue без ready, поэтому snapshot может доказать текущее
отсутствие метки. Но `_revoke` не различает причины неeligibility; отсутствие Issue
в open-list не доказывает снятие ready. Сбой/неполный snapshot не должен менять
tracking. `queue._reason` уже использует labeled/unlabeled как временную границу
команд; это полезная защита, но не lifecycle обработчик снятия ready.

Для известных Issue независимо от eligibility нужен сверенный current snapshot и
полностью прочитанная Timeline с `unlabeled` именно настроенной task_label,
server event ID/time/actor. Actor берётся только из события GitHub, не body/автора
Issue. При недоступном actor — «Не удалось определить». Хранить отдельно
`event_at` и `observed_at`; если неизвестно время самого события, в комментарии
явно указывать UTC-время обнаружения, не выдавая его за время удаления.
Если label успели снять и вернуть между polls, текущего snapshot недостаточно:
replay порядка timeline обязателен, иначе старое approve переживёт смену generation.
При неполном timeline можно безопасно запретить admission по факту отсутствия,
но reattach требует нового достоверного labeled события. Точную политику fallback
времени/недоступной истории согласовать (§10).

### Detach delivery

1. Под issue lock и BEGIN IMMEDIATE сверить identity/generation/event fence;
   атомарно persist detach intent, detached/draining, server event metadata,
   отозвать approval/admission. Повтор того же события ничего не создаёт.
2. discovered/pending/rejected/review: запретить новые attempts и обычные PR
   decisions; сначала выполнить необходимые pending публикации. Не закрывать
   Issue/PR и не удалять canonical remote branch. Для задачи без результатов
   push не выдумывать: сохранить результат проверки «изменений нет».
3. running: не посылать signal/cancel_child и не удалять attachments/log/prompt.
   Дождаться child, сохранить exit outcome отдельно от состояния tracking;
   проверить рабочую ветку и доступные результаты, commit при реальных изменениях,
   обычный push и подтверждение remote SHA. Normal `publication_commit` с обязательным
   PR diff не подходит как единственный salvage путь: отсутствие diff к base не
   означает, что можно потерять локальные коммиты. Для ненулевого exit нужен
   согласованный salvage contract, а не безусловное исполнение normal success path.
4. Push failure → persist retry/error, оставить worktree/вложения/refs, Codex больше
   не запускать. Устойчивый non-fast-forward → manual attention с сохранением данных.
5. После подтверждения сохранности — immutable detach comment intent: actor,
   event/observed UTC, generation, branch/SHA, PR если есть, publication result;
   reconcile потерянного ответа по operation marker. Потом preflight/owned cleanup,
   persist cleaned/completed. Ошибка комментария/cleanup повторяет свою фазу.
6. Полностью detached исключается из очереди автоматического выполнения и PR
   monitor. Но discovery watcher продолжает искать новую task_label, а recovery
   выбирает незавершённые обязательные операции. SQLite аудит не удаляется.

### Reattach

1. Принять только новое доказанное labeled после detach fence и для той же
   repository/Issue identity. Дубликат event ID не увеличивает generation.
   Terminal done/cancelled/review_on_error остаётся terminal.
2. Если старая generation ещё публикует/очищает, persist reattach event и блокировать
   запуск; не отдавать общий worktree двум workflows. После завершения старых
   обязательных операций под lock атомарно создать generation+1, discovered,
   approved_version=NULL, новые command fences. Это предложение сериализации;
   вариант допуска новой generation до drain обсуждается в §10.
3. Старые transitions/decisions/guards/quarantine не удалять. Их эффекты не могут
   действовать на новую generation; очистку карантина делать scoped, не превращать
   прежний недоказанный approve в согласие. Старые outbox могут завершить историческую
   доставку только без текущих labels/state; запретить старые close-Issue effects
   после несовместимого reattach. Завершать такие эффекты до открытия новой generation.
4. Проверить canonical PR, remote branch, сохранённый SHA, ancestry, head/base и
   отсутствие merged. Merged, missing branch, legacy conflict, changed origin/base,
   неизвестная identity → diagnostic hold, без Codex и новых branch/PR.
5. Новое approve обязательно. Воссоздание owned worktree на существующей canonical
   ветке допускается после проверки, создание альтернативной ветки запрещено.
   Никакого fallback на main, reset или force push.

## 7. Intent, recovery, гонки и безопасность очистки

Текущие durable точки: attempt до prepare; exit_code до PR intent; `pr:attempt`
до публикации; phase push до push, result SHA до него и pushed/remote SHA после
ls-remote; phase pr до API; resolution intent до decision; cancellation intent
в транзакции решения; result_outbox и comment intent в finish. Пробелы: нет intent
на Git prepare/branch ownership; нет detach/error cleanup intent; lock PR publisher
по attempt, resolution по pr_id, cancellation по issue_id — они не общий mutex.
В `TaskResultPublisher.flush` HTTP выполняется внутри BEGIN IMMEDIATE; не переносить
этот образец в новые lifecycle outbox. Новые вызовы требуют lock + коротких SQLite
транзакций вокруг intent/result, с HTTP/Git между ними.

Recovery выбирает durable operations независимо от available/state. После restart
не перезапускает Codex. `Dispatcher.__init__` сейчас восстанавливает publication
для success exit либо marks running interrupted через inspect_recovery;
`Lifecycle.recover` также обрабатывает прежние процессы. Поэтому невозможно обещать
«running всегда нормально закончит» при аварии самого daemon: detach не должен
вызывать остановку, а существующий crash/stop/timeout — отдельный сценарий.
Сохранять process identity и найденные результаты, не интерпретировать отсутствие
живого child как отсутствие локальных изменений.

| Гонка / crash point | Требуемое поведение и проверка |
|---|---|
| Detach vs reserve/start | Один issue lock; повторная generation/detach CAS непосредственно перед запуском. Если reservation уже принят — drain существующей попытки, не новая |
| Detach между push и review | Публикацию довести до remote proof, не применять обычный review/labels для detached; lifecycle outcome записать отдельно |
| Label remove/add между polls | Timeline replay, новая generation; snapshot-only не принимать как непрерывное tracking |
| Reattach vs старый cleanup | Барьер drain/cleanup; старый operation не может удалить новый worktree или файлы |
| Старый approval/outbox после reattach | Сверка generation, iteration/version и identity; исторический receipt сохраняется, текущий state не меняется |
| POST PR или PATCH reopen ответ потерян | GET/reconcile под issue lock, не Codex; при canonical PR второй POST запрещён |
| Crash после remote push до pushed=1 | ls-remote/ancestry повторно, запись результата без новой попытки |
| Crash после reopen до transition | GET того же pr_id, затем generation CAS; новая close/merge проверяется заново |
| Второй close того же PR | Новый resolution episode/marker; не старый resolution:pr_id и не старый comment_id |
| Merge во время reattach/reopen | Не оживлять merged PR; hold/согласованная обработка, никаких новых PR |
| Crash после cleanup до completed | Повторный preflight допускает отсутствие уже удалённых owned файлов; оставшиеся чужие сохраняются |
| Push error + terminal/error/detached | Worktree остаётся, recovery доступен несмотря на запрет scheduling; публикация без Codex |
| Poll/CLI/daemon одновременно | Общий issue mutex + SQLite CAS/unique active Issue; lock ordering runtime → issue → короткая DB transaction |

Cleanup разрешать только по двум независимым доказательствам: принадлежность ресурса
и сохранность результата. Путь под workspace и имя branch не являются ownership.
Проверять expected repo/common git dir, worktree registration/path без symlink,
branch и local SHA, ancestry/remote SHA, отсутствие running child и dirty/untracked/
ignored данных; registry/manifest файлов. Чистый worktree может содержать
неопубликованные commits — одной `git status` недостаточно. Проверка каждой
архивной попытки требует её сохранённого SHA, а `task_pull_requests.head_sha`
обновляется и не может быть единственным источником всех старых доказательств.
Нельзя просто вызвать CancellationDelivery.cleanup для detached/error: она связана
с terminal cancel и текущими PR SHA, а poll ещё и закрывает Issue.
Удалять только явно owned temporary artifacts после preflight всего набора;
не удалять SQLite history, canonical binding, branch или PR. Если ignored/неизвестные
файлы не покрыты publication proof — оставить с диагностикой, не применять `rm -rf`.
Remote branch deletion по cancellation сохраняет отдельную политику Issue #27:
сейчас ветки сохраняются из-за отсутствия доказательства исключительного владения.

## 8. Матрица требований и будущих тестов

Идентификаторы T ниже — обязательные предлагаемые кейсы 6.4.1.2–6.4.1.7,
не новые уже реализованные тесты.

| Требование | Текущая реализация | Изменение | Тест / ожидаемый результат |
|---|---|---|---|
| Одна branch/PR на Issue | scheduler iteration suffix, prepare -b, new PR | canonical binding + owned reuse | T01: три reject/approve цикла, один ref/pr_id, несколько attempts/iterations/SHA |
| Reopen старого PR | отсутствует, closed отвергается | PATCH+GET того же ID | T02: success/already open/lost PATCH response, ровно один canonical PR |
| Без второго PR | find twice + attempt flock | issue lock + create intent + canonical gate | T03: два publishers разных attempts, lost POST response, duplicate/foreign candidates → hold, не второй POST |
| Неразрешимый reopen | generic retry/conflict failed | classifier + review_on_error | T04: согласованные permanent cases; transient 404/422/403 не выдают ложный terminal |
| Push до очистки | push/ls-remote есть, detach нет | independent salvage proof | T05: push timeout/NFF/remote mismatch, worktree сохранён; recovery без start Codex |
| Новый terminal | guards done/cancelled | review_on_error во всех входах | T06: approve/confirm/reset/apply/relabel/reopen не оживляют; обязательный push retry остаётся |
| Снятие ready | _revoke available=0 | durable detach/drain | T07: discovered/pending/rejected/review, ноль новых запусков/close PR/close Issue |
| Running detach | child продолжается, normal review не проходит | collect salvage независимо eligibility | T08: child не получает cancel; commit/push/comment/cleanup по порядку |
| Actor/time | command events; PR closed timeline | task_label unlabeled timeline | T09: точный actor/event UTC; missing actor/history fallback без автора Issue/body |
| Reattach | partial restored/revoked | generation + discovered + approve | T10: новая generation, та же branch/PR; unchanged content не переносит approval |
| Generation fence | version/iteration/observation | scope всех side effects | T11: old labels/events/outbox/monitor/cleanup во время reattach не меняют новую задачу |
| Terminal/merged/missing refs | guards частично есть | explicit diagnostic hold | T12: merged/deleted ref/missing SHA/base drift/legacy multi-PR → ноль Codex/новых refs |
| Crash recovery | PR/cancel intents, interrupted recovery | lifecycle state machine | T13: fault injection до/после каждого intent/effect/result/cleanup, retry без Codex |
| Сохранение истории | upgrade604 снимает UNIQUE issue | additive 6.4.1 migration | T14: fresh/602/604, zero/one/many PR, unresolved, counts/bytes/FK; backup fail/rollback/repeat/lock |
| Повторные решения PR | PK pr_id resolution | resolution episodes | T15: close/reopen/close/merge одного PR, разные comments/actor/SHA, нет replay прежнего done |
| Ownership | registry, manifests, cancel proof | general publication+ownership proof | T16: чужая ветка/worktree, symlink/common-dir mismatch, dirty/ignored/unpushed archive → сохранение |
| Multi-project | REST IDs + composite FK | сохранить constraints | T17: одинаковые Issue/PR numbers в двух repo; generation/locks/cleanup не пересекаются |
| Init/UI/config | COLORS, fingerprint, table/json | явная hyphen label + fields | T18: default/custom label, init stale/fail/success, queue/show table/JSON, Unicode/redaction |
| Timeline churn | _reason readiness fence | lifecycle replay | T19: remove/add без poll между ними, одинаковые секунды, pagination fail, restart cursor; нет старого approve |
| Независимость ошибок child | failed/interrupted не publish | согласованный salvage | T20: nonzero/timeout/crash/force с dirty/clean commits; сохранность без ложного review |
| Нет network в DB tx | PR publisher соблюдает, result outbox нет | короткие tx + mutex | T21: instrument db.in_transaction на Git/HTTP, два независимых Store connections |

### Существующие тесты: сохранить или заменить

- [test_stage602.py](../../tests/test_stage602.py): сохранить identity, roles,
  stale evidence, transactional rollback/reservation, запрет автоматического retry;
  `test_same_version_new_iteration_new_pr_and_sha_history` переписать на тот же
  canonical PR, отдельно сохранить legacy history fixture. Terminal cases расширить.
- [test_stage604.py](../../tests/test_stage604.py): устарели
  `test_explicit_retry_without_edit_new_iteration_new_pr`,
  `test_retry_git_starts_at_previous_sha_and_preserves_old_worktree`,
  `test_scheduler_retry_creates_new_branch_and_pr_from_rejected_sha`.
  Заменить ожидания новых refs/PR на owned reuse/reopen. Сохранить fresh approve,
  actor/time fallback, project isolation и lost response; добавить multiple episodes
  в concurrent monitors, recovery и migration tests.
- [test_stage603.py](../../tests/test_stage603.py): closed/merged cases в
  `test_unacceptable_pr` разделить на canonical reopen и foreign/merged reject;
  сохранить no-diff variant A normal path, lost-create response, confirmed SHA,
  partial transition, concurrency и запрет network inside DB transaction.
- [test_issue27.py](../../tests/test_issue27.py): сохранить cancellation авторизацию,
  conflicts/quarantine, restart и ownership regressions. Они не покрывают detach:
  добавить проверки, что ready removal не закрывает Issue not_planned.
- [test_migration602.py](../../tests/test_migration602.py): старый reset остаётся
  историческим тестом 602; новая миграция обязана сохранять историю, это отдельные
  fixtures/backups. Не менять смысл старой миграции ради нового backfill.
- [test_issue24.py](../../tests/test_issue24.py): immutable summaries, redaction,
  lost response и unchanged PR body сохранить; lifecycle comments не должны
  копировать произвольные error bodies/Issue text или терять idempotency markers.
- [test_github.py](../../tests/test_github.py), [test_stage04.py](../../tests/test_stage04.py),
  [test_rendering.py](../../tests/test_rendering.py), [test_cli_unicode.py](../../tests/test_cli_unicode.py):
  проверить прежний polling revocation контракт, lifecycle timeout/stop и UI; менять
  только ожидания, противоречащие новому нормативу, не ослаблять безопасность.

Ручная интеграция после реализации — в отдельном тестовом репозитории:
Issue→PR→close→approve→same branch push→reopen→ручной merge;
невозможность reopen; running detach; push failure/restart; remove/add readiness.
Проверять фактические PR/ref counts, Timeline actor/time, данные SQLite, отсутствие
повторного Codex и сохранность локальных результатов. В рамках аудита не выполнялась.

## 9. Противоречия документов и влияние на 6.5–6.8

Норматив 6.4.1 имеет приоритет; исторические документы здесь не редактировались.

| Документ | Что изменить после согласования аудита |
|---|---|
| [stage-06-spec](stage-06-spec.md), §2 п.3/8, состояния, проверки | Удалить new branch/new PR/запрет reopen, добавить terminal error и tracking lifecycle |
| [stage-06-plan](stage-06-plan.md), 6.5 и сценарий 3 | Заменить прежнюю retry стратегию; включить 6.4.1 до общей приёмки |
| [stage-06-01-spec](stage-06-01-spec.md), §2 п.3/4 | canonical PR, новый terminal, сохранение нового поколения без прежних approvals |
| [stage-06-01-architecture](stage-06-01-architecture.md), таблица, §Git/recovery/tests | Убрать новую branch/PR, добавить resolution episodes, drain и generation fence |
| [stage-06-04-spec](stage-06-04-spec.md), §2/5/8/10 | Зафиксировать замену прежнего утверждённого решения; сохранить уточнения Issue #27 о cancel/ownership |
| [stage-06-02-spec](stage-06-02-spec.md) и [implementation](stage-06-02-implementation.md) | Уже содержит same PR как исходный замысел, но reset/constraints и runtime позже изменились; описать новую недеструктивную migration и generation |
| [stage-06-03-spec](stage-06-03-spec.md) и [implementation](stage-06-03-implementation.md) | Уточнить first create vs canonical reopen, normal no-diff vs detach salvage, lifecycle retries |
| [stage-06-04-implementation](stage-06-04-implementation.md), [Issue27](stage-06-04-issue27.md) | Отделить историческое поведение от нового, проверить фактический вызов cleanup done и сохранение branch |
| [README](../../README.md), config/CLI contracts | Обновить workflow, labels/init, queue/show, ручное решение review_on_error, retained artifacts и retry diagnostics |

6.5 должна принимать same-PR rework и repeated resolution episodes, а не повторять
уже реализованный new-PR алгоритм. 6.6 должна разделять cancelled/detached/error и
доказательства владения/публикации; detach не разрешает удалять canonical branch.
6.7 объединяет operation recovery и generation fences, устраняет network-in-transaction
в result outbox, проверяет межworkflow гонки. 6.8 требует обновлённой регрессии,
ручной интеграции и документации; зелёные старые тесты не доказывают выполнение
нового ТЗ. Нельзя переносить надёжность собственных операций на будущую 6.7.

## 10. Риски и вопросы заказчику (решения не приняты)

1. **Permanent reopen failure.** Варианты: terminal только по доказанной структурной
   невозможности; либо после ограниченного retry budget с явным подтверждением
   оператора. Предлагается первый как базовый, но нужны критерии/порог и политика
   404/422, merged во время публикации и повторного close сразу после reopen.
   Одного HTTP status или истечения времени недостаточно как доказательства.
2. **Первый running detach до первого PR.** Варианты: сохранить push без создания
   PR и завершить tracking; либо закончить начатую normal PR publication.
   ТЗ требует публикацию результатов, но не определяет обязанность первого POST
   после detach. Требуется решение; оба варианта запрещают второй canonical PR.
3. **Nonzero Codex/timeout при detach.** Варианты: commit/push любых проверенных
   доступных изменений с диагностикой; либо сохранить неоднозначные файлы для
   ручного решения. Предлагается salvage при доказанной ownership и пригодности
   состояния Git, hold при конфликте, без трактовки partial result как успеха.
4. **Reattach до окончания drain.** Варианты: queued event, новая generation после
   drain; либо generation сразу и ожидание resource lease. Предлагается первый
   для простоты crash recovery; окончательное решение перед 6.4.1.5–.6.
5. **Legacy multi-PR.** Без однозначного доказательства только hold. Кто и каким
   проверяемым операторским механизмом утверждает canonical binding — отдельное
   решение; предлагается audited explicit selection после проверки identity/SHA,
   без изменения прежней истории и без произвольного выбора latest PR.
6. **Timeline недоступен.** Actor fallback определён ТЗ; точное время события и
   снятие/возврат между polls — нет. Предлагается observed_at с честной подписью
   для detach и запрет reattach до достоверного нового события; согласовать retry
   и операторское разрешение отсутствующей истории/равных времён.
7. **Terminal error и ручное завершение.** ТЗ запрещает автоматический переход из
   review_on_error, но просит человеку завершить Issue в GitHub. Предлагается
   сохранять SQLite terminal и лишь аудитировать внешнее решение, не подменяя done.
   Нужны правила labels после ручного завершения и при terminal+detach одновременно.
8. **UI/label и stopped projects.** Цвет/description нового индикатора и представление
   detached в queue должны быть утверждены. Добавление label инвалидирует init:
   стоит отдельно согласовать recovery уже накопленных публикаций у проекта с
   stale init, чтобы требование re-init не оставило push без доставки.
9. **No diff / remote changes.** Normal 6.3 variant A остаётся; сохранение результатов
   detach не должно зависеть от PR diff. При изменённом человеком remote head
   автоматический reset/rebase недопустим без отдельной политики; предлагается hold.

Эти вопросы направлены заказчику; при отсутствии ответа остаются открытыми для
приёмки отчёта. Их наличие не блокирует сам аудит и не разрешает реализацию runtime.

## 11. Рекомендуемая последовательность отдельных заданий

| Шаг | Содержание и критерий выхода |
|---|---|
| 6.4.1.2 | Утвердить модель bindings/generations/episodes/operations и legacy selection; реализовать новую недеструктивную migration, backup/rollback/FK/identity tests. Никакого автоматического выбора legacy PR |
| 6.4.1.3 | Issue lock, owned branch reuse, first-create gate, reopen PATCH/GET, episode-aware monitor и recovery; T01–03/13/15/17/21; согласовать permanent classifier |
| 6.4.1.4 | Terminal guards во всех путях, label mapping/init/UI, independent publication proof и error outbox/cleanup; T04–06/16/18 |
| 6.4.1.5 | Согласовать first-PR/nonzero/fallback решения; Timeline watcher, detach intent/drain/salvage/cleanup recovery, T05/07–09/13/16/19/20 |
| 6.4.1.6 | Reattach event replay, generation fences команд и outbox/monitor/cleanup, old-operation barrier, canonical validation; T10–12/17/19 |
| 6.4.1.7 | Полная регрессия, многократные close/reopen, fault injection/concurrency, ручные сценарии и актуализация нормативов/README/общего плана; отдельная приёмка без автоматического merge |

Каждый шаг должен включать свои автоматические тесты и recovery сразу. Общую
матрицу и решения §10 согласовать до реализации; существующие нормативные файлы
обновлять только в разрешённом следующем задании.

## 12. Выполненные проверки

Первый `bash scripts/test.sh` не запустил pytest: `/usr/bin/python3: No module named pytest`.
Для изоляции зависимостей создано `/tmp/codexagent-issue29-venv` через `python3 -m venv`,
затем `python -m pip install -e '.[test]'`. Использованы Python 3.14.4,
pytest 9.1.1 и PyYAML 6.0.3. Код/конфиг проекта ради среды не менялся.

Полный штатный запуск:

```text
PATH=/tmp/codexagent-issue29-venv/bin:$PATH bash scripts/test.sh
478 passed, 30 subtests passed in 23.91s
```

Exit code 0: после pytest скрипт успешно выполнил `python3 -m compileall -q src tests`.
Это проверка существующей реализации на исследованном HEAD, не доказательство
реализации новых T01–T21. Новые автоматические тесты не добавлялись согласно
прямому ограничению Issue #29. Ручные GitHub интеграционные проверки не запускались.
Проверены существование локальных ссылок отчёта и отсутствие whitespace errors
через `git diff --check`; изменён только этот отчёт.
