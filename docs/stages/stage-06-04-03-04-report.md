# Отчёт реализации 6.4.3.4

Ветка: `STAGE_06_04_03`, репозиторий: `heroicrealm/CodexAgent`.

## Архитектурный аудит

- `Adapter.list_direct_children` уже делает две полные пагинированные выборки,
  проверяет repository URL, issue_id, дубли, summary total и стабильность списка.
  Использован без рекурсивного обхода.
- Канонический PR остаётся в `task_pull_requests`; `iteration_pr_history`,
  существующие PR transitions/resolutions и ownership guards не изменены.
  Монитор читает canonical identity, не создаёт второй реестр и не присваивает
  произвольный найденный PR. Неизвестный или конкурирующий PR блокирует готовность.
- `ContainerService` подготавливает ветку и переводит в `awaiting_children`.
  Повторная проверка публикации допускает продвижение HEAD после merge детей;
  прежнее требование равенства исходному SHA мешало бы штатной интеграции.
- Для сообщений расширен существующий `container_diagnostic` outbox: active
  blockers учитываются и в `awaiting_children`, сохранены marker/read-back,
  дедупликация, recovery и повторная доставка после неопределённого ответа.
- Самостоятельный `poll`, `poll --watch` и scheduler используют `SyncService`:
  монитор включён после PR resolution, без зависимости от запуска исполнителя.

## Реализация

Добавочная миграция 615 выполняется в общей атомарной транзакции Store.
Историческая `VERSION=602` и миграция 614 не изменены. Добавлены
`container_readiness` и `container_integration_snapshots`. Снимки защищены
триггерами от UPDATE/DELETE и содержат непосредственных участвующих детей,
идентичности, состояния, PR evidence, SHA merge/head, HEAD контейнера, iteration,
GitHub observation time. Неуправляемые non-ready дети не получают relations,
комментариев или durable evidence.

`ContainerMonitor` дважды собирает детей, актуальные PR и parent ref. Поиск PR
пагинированный, `state=all`, без фильтра base, чтобы обнаруживать неверную цель.
Каждый найденный PR дополнительно читается по номеру; сверяются постоянный ID,
репозиторий head/base, canonical head/base, состояние, merged boolean и timestamp,
merge SHA. Ветка ребёнка через Git refs не проверяется: её удаление после merge
не препятствует доказательству. Закрытый unmerged PR не подтверждает `done`.
Ручной PR, уже связанный канонически, может быть впервые увиден монитором после
merge; привязка и исполнение обычной manual Issue остаются для 6.4.3.5.

`cancelled` проверяется на открытый PR, running attempt, незавершённые publication
и container operations и активный stop request. Другие состояния ждут без ложной
диагностики ошибки. Дочерний контейнер проверяется так же, без чтения его детей.
При detached/open PR действует отдельный код; исчезнувшие исторические связи
сохраняются и блокируют готовность. Конфликт родительских связей не retarget-ится.

Полный локальный fingerprint проверяется повторно под `BEGIN IMMEDIATE` перед
публикацией. Сетевых запросов под SQLite writer-lock нет. Отдельный файловый lock
сериализует poll одного контейнера между процессами. Успешная повторная проверка
снимает только monitor-owned blockers. Сбой или смена evidence инвалидирует
ready, сохраняя снимки; повторное подтверждение создаёт следующее поколение.
Повтор одинакового успешного poll и restart не создают новое поколение.

CLI show/queue отображают readiness и счётчики в table/JSON. В queue сведения
контейнера вынесены в переносимый блок, чтобы длинная строка статуса не обрезала
готовность. Новая конфигурация не потребовалась.

Нет запуска Codex контейнера, его PR, переходов running/review, auto-merge,
редактирования или запуска детей, удаления веток, force push либо новых правил
исполнения обычной manual Issue. Общий blocked для всех Issue не добавлен.

## Проверки

Новые тесты `tests/test_stage6434.py` проверяют canonical identity, repository,
head/base, merged/closed-unmerged/no PR, future evidence, ambiguity, отмену и
незавершённые операции, detach/open PR и снятие конкретной блокировки, исчезновение
и добавление связей, вложенный контейнер без рекурсии, 0/1/101 ignored детей и сквозной мониторинг 0/1/101 учитываемых детей,
неизменяемость/смену поколений, restart, network/stale evidence и сбой общей project fetch, локальную гонку,
занятый файловый lock, неопределённый ответ комментария, отсутствие сети под writer
lock, upgrade/rollback миграции 615 и CLI. Реальная пагинация 0/1/101 детей,
duplicate ID, incomplete/foreign/changing collection покрыта регрессией 6.4.3.3.

Первый полный прогон во время разработки: 836 passed, 2 failed, 30 subtests passed
за 140.39 s. Исправлены recovery после удаления parent ref и ожидание списка
миграций в регрессионном тесте. Последующие целевые прогоны подтвердили исправления. Промежуточный полный
прогон: 867 passed, 30 subtests passed за 143.33 s; после него добавлены
дополнительные проверки. Итоговый целевой набор: 35 passed за 4.38 s.

Итоговый обязательный прогон `source .venv/bin/activate && bash scripts/test.sh`:
**873 passed, 30 subtests passed за 146.37 s (0:02:26)**; exit code 0.
Включённый в скрипт `python3 -m compileall -q src tests` завершился успешно.
`git diff --check` и `git diff --cached --check`: exit code 0, замечаний нет.

## Непроверенные внешние сценарии и ограничения

Живые GitHub/Playground сценарии, включая #72, не выполнялись: нет внешней
приёмки, создания/merge PR или изменения демонстрационных Issue. Проверки API
выполнены на моделируемых ответах; подготовка использует настоящие Git repos и
SQLite. Проверка удаления head после merge моделирует отсутствие любого запроса
head ref и успешное чтение PR evidence, без удаления внешней ветки.

GitHub не предоставляет атомарный снимок нескольких Issue/PR/ref: две совпавшие
полные выборки обнаруживают наблюдаемую гонку, но не исключают ABA или изменение
сразу после последнего GET. Durable ready означает подтверждённое наблюдение,
а не блокировку внешнего GitHub; 6.4.3.5 должен перепроверить его перед запуском.
Незнакомые/неоднозначные PR обрабатываются fail closed; автоматическая привязка
ручного PR и смена жизненного цикла manual Issue не реализуются этим этапом.
Существующие preparation blockers автоматически не сбрасываются монитором.

PR и документ приёмки не создавались; приёмка пользователем не заявляется.

## Исправление семантической свежести снимка

Фактическая причина ложного `child_snapshot_stale` в коде — обязательное
равенство GitHub `parent.updated_at` времени принятого снимка. Timestamp может
меняться по разным причинам; связь конкретного изменения #104 с Sub-issues,
метками или комментариями остаётся гипотезой до непосредственного сравнения
GitHub evidence. Тесты моделируют эти изменения, а не доказывают поведение
каждого события в живом GitHub.

Монитор теперь использует существующий `storage.content_hash` для свежих
id/title/body/author и принятого target_branch (изменение branch/YAML защищено
хешем тела). Проверяет номер и repository identity, open, ready, emergency stop,
allowlist автора, отсутствие команд approve/reject, available, approved version,
container state/type, iteration и выбранную canonical work branch. Присутствующая
команда требует завершения штатной обработки команд до подтверждения монитором.
Сервисные state labels и посторонние labels/цвет/описание, комментарии и timestamp
родителя не входят в критерий свежести. Два чтения parent проходят один guard;
перед SQLite commit он повторяется по последнему удалённому наблюдению и текущей
локальной строке. Fingerprint дополнен command_guards и task_iterations, поэтому
локальная смена guard, iteration или branch не проходит writer transaction.
Сети внутри writer lock нет; ограничение неатомарности GitHub сохраняется.

В логах без содержимого Issue различаются `parent_mismatch`, `child_mismatch`,
`remote_read`, `local_race`. После двух успешных наблюдений старый durable
`monitor:parent / child_snapshot_stale` снимается штатно. Повторный poll/restart
не создаёт комментарий или поколение при неизменных подтверждённых данных.

Добавлены тесты transport churn между чтениями, recovery durable blocker/restart,
body/title/author/identity/state/ready/emergency/command changes и локальных гонок
iteration/branch/guard. Два закрытых ребёнка и третий non-ready не дают готовности
без доказанного merge canonical PR. Существующая отдельная регрессия
`test_stage6433.py` с изменением `updated_at` между чтениями child body сохранена;
GET/PATCH safeguards и `child_snapshot` не менялись.

Ручной poll #104 не выполнен: в этом checkout отсутствует рабочая
`config/config.yaml`, присутствует только пример. Внешние Issue и SQLite
пользователя не изменялись. PR не создавался; приёмка не заявляется.

Проверки исправления: `PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh` —
**890 passed, 30 subtests passed за 155.08 s**, exit code 0, включая compileall.
Целевой набор monitor + child preparation/CAS — **132 passed за 13.54 s**.
После добавления отдельной регрессии уже доставленного stale blocker и его
recovery через restart итоговый целевой monitor-набор — **53 passed за 7.71 s**.
`git diff --check` и `git diff --cached --check` — без замечаний.
Системный python3 не содержит pytest, поэтому проверки выполнены в существующей
`.venv`; зависимости проекта не изменялись.

## Исправление обработки смены base канонического PR

Смена только base больше не считается подменой PR. `PullRequestMonitor`
сохраняет исходную `task_pull_requests.base`, а фактическую `merged_base` пишет
в metadata существующего `pr_resolutions` и evidence перехода. В комментарии
завершения указаны обе ветки. PR ID/number, repository head/base, head ref,
валидность head SHA, iteration, guard, closed/merged timestamps и обычные
процедуры доставки сохранены. Дополнительно merge требует непустого merge SHA
и merged_at не позднее closed_at. Open остаётся review, closed unmerged
переходит в rejected. Старый resolution retry повторно наблюдает PR и доходит
до штатного completed; существующий marker предотвращает дубли комментариев.

`ContainerMonitor` читает фактическую base канонического PR непосредственно
из GitHub; расхождение с work_branch родителя даёт child_merge_wrong_base.
Диагностика содержит ожидаемую/фактическую ветки и указание исключить ребёнка
из прямых Sub-issues. Снятие ready label у done ребёнка с wrong-base merge
не заменяет исключение связи. Retarget/повторный merge не выполняются.

Разрешённое исключение требует ранее сохранённого наблюдения именно
child_merge_wrong_base, локального done без незавершённых операций, отсутствия
другого участвующего родителя и повторного чтения того же канонического
merged PR с полностью совпадающими PR evidence. Две полные согласованные
выборки должны подтвердить отсутствие ребёнка в прямых Sub-issues. В общей
SQLite транзакции связь сохраняется с participation=ignored и
conflict_code=child_merge_wrong_base, monitor blocker снимается, readiness
пересчитывается по остальным детям. После restart исключённая связь не
возвращает child_relation_removed. Прочие исчезновения/подмены, detached/open
PR и немониторные blockers сохраняют прежнюю защитную семантику.

Миграция не нужна: используются существующие JSON evidence/metadata,
container_readiness и допустимое значение ignored в sub_issue_relations.
Старые resolutions без новых полей остаются доставляемыми. Старые ожидаемые
base не переписываются. Изменены pr_monitor.py, container_monitor.py,
container_diagnostic.py, storage/transitions.py, tests/test_stage604.py;
добавлен tests/test_pr_base_change.py, обновлён этот отчёт. В старых тестах
изоляции проектов и полного цикла (tests/test_stage60415.py) merged fixtures
дополнены обязательным merge_commit_sha.

Ограничения: GitHub не даёт атомарного снимка нескольких ресурсов. Если связь
исчезла до подтверждённого наблюдения wrong-base merge либо evidence
неоднозначно, монитор сохраняет блокировку; автоматического предположения о
намерении пользователя нет. Внешняя приёмка не выполнена, #114/#115, родитель
и пользовательская SQLite вручную не изменялись. Приёмка этапа не заявляется.

Ручная проверка после обновления установленного dispatcher:

1. Выполнить `codex-dispatcher poll` с рабочей конфигурацией; проверить
   `codex-dispatcher show CodexAgentPlayground 114 --format json`: done,
   PR #115 closed/merged, resolution completed, обычное закрытие Issue и один
   комментарий исполнения. Ожидаемая base должна остаться ContainerBranchwrongpr,
   фактическая — соответствовать GitHub PR #115.
2. Проверить родителя через show: awaiting_children, ready_for_integration=0,
   child_merge_wrong_base и диагностика обеих веток, пока #114 входит в Sub-issues.
3. Пользователь вручную удаляет #114 из прямых Sub-issues родителя в GitHub.
4. Снова выполнить poll и show родителя: blocker снят, readiness зависит от
   оставшихся детей; #114 остаётся done. Повторить poll и перезапустить dispatcher:
   конфликт и повторные комментарии не должны возвращаться.

Проверки исправления base-change:

- Целевой набор PR/container/base-change: 99 passed за 11.49 s;
  отдельный новый набор: 15 passed за 1.13 s.
- Первый полный прогон: 903 passed, 1 failed, 30 subtests passed за 156.27 s;
  выявлен старый merged fixture полного цикла без merge_commit_sha, исправлен.
- Итоговый `PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh`:
  **906 passed, 30 subtests passed за 155.42 s**, exit code 0;
  включённый compileall завершён успешно.
- `git diff --check` и `git diff --cached --check`: без замечаний.
