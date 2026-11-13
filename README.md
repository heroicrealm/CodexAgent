# Codex Dispatcher V2

Python 3.11+, установка: `pip install -e '.[test]'`.
Конфигурация: `config/config.example.yaml`.
Все команды принимают `--config /path/to/config.yaml` перед подкомандой.
V1 не используется и не мигрируется.

Первый запуск:

```sh
codex-dispatcher --config config/dispatcher.yaml db bootstrap
codex-dispatcher --config config/dispatcher.yaml db check
codex-dispatcher --config config/dispatcher.yaml init-labels
codex-dispatcher --config config/dispatcher.yaml run
```

`bootstrap` явно создаёт новую БД; существующие файлы не перезаписывает.
Обычные run/status/queue/publish/init-labels и worktree-cleanup требуют
существующей проверенной БД. Файл БД должен иметь права 0600; каталоги рекомендуется
ограничить 0700. Symlink в путях БД, sidecar и maintenance lock запрещён.

Для существующей V2.3 остановите daemon и дождитесь завершения Codex, затем:

```sh
codex-dispatcher --config config/dispatcher.yaml db register /secure/v23-before-registration.sqlite3
codex-dispatcher --config config/dispatcher.yaml db check
```

`register` принимает только точную схему V2.3 version=1 с успешными проверками
целостности и связей. До добавления metadata сохраняется согласованный snapshot
через SQLite Backup API; записи issues/executions не меняются. Destination должен
отсутствовать. Незарегистрированная БД не допускается к исполнению. Если регистрация
оборвалась после записи metadata, но до создания маркера, проверьте конфигурацию и
архив; `db bind` завершает явную привязку к workspace.

UUID экземпляра хранится в dispatcher_metadata и `.dispatcher-instance` в workspace.
Он не зависит от списка проектов или абсолютного пути. Переносите workspace вместе
с маркером и БД. Для нового workspace после проверки правильной БД выполните `db bind`;
существующий маркер команда не заменяет. Копия UUID не является защитой от намеренного
клонирования установки. Не запускайте два клона одновременно.

Проекты связываются по owner/name без учёта регистра. Переименование project.id,
enabled: false, удаление и возвращение проекта не удаляют его данные. Runtime выбирает новые задачи
только из текущих repositories и после успешного poll. Замена repository у прежнего
id не переносит старые задачи. `project_id` в прежних строках остаётся историческим.

Резервирование и восстановление:

```sh
codex-dispatcher --config config/dispatcher.yaml db backup /secure/snapshot.sqlite3
codex-dispatcher --config config/dispatcher.yaml stop
# дождитесь завершения daemon и его исполнителей
codex-dispatcher --config config/dispatcher.yaml db check
codex-dispatcher --config config/dispatcher.yaml db restore /secure/snapshot.sqlite3
codex-dispatcher --config config/dispatcher.yaml db check
```

Backup API включает закоммиченные данные из WAL и проверяет итоговый снимок;
права destination — 0600. Не копируйте один основной файл работающей WAL-БД.
Backup допускается при работающем daemon; не заменяйте файлы источника внешними
средствами во время операции. Read-only SQLite Backup API может создать пустые
WAL/SHM у источника. `db check` изучает приватную временную копию main+WAL и не
создаёт файлы в исходном workspace/каталоге БД, не обращается к GitHub. При активной
записи копия может оказаться несогласованной: повторите check после остановки.
PASS подтверждает локальные проверки, но не актуальность внешнего состояния.

Restore требует обе блокировки (унаследованные Codex дескрипторы также удерживают
блокировку), отсутствие control socket и активных записей executions, валидную
текущую БД и тот же UUID у backup. При сомнении операция отказывает. При оставшихся
WAL/SHM сначала сохраните аварийный набор файлов, убедитесь в полной остановке,
затем выполните SQLite `PRAGMA wal_checkpoint(TRUNCATE)` с закрытием соединения;
при busy/ошибке не продолжайте. Никогда не удаляйте непустой WAL вручную.

Перед заменой создаётся проверенный `*.before-restore-<uuid>`, staging располагается
рядом с БД, итог устанавливается через atomic replace и fsync каталога. Ошибка до
replace сохраняет прежний основной файл; сбой питания после replace может оставить
уже восстановленную БД с установленным барьером. Archive сохраняется.

**После restore исполнение заблокировано до полной внешней сверки V2.4.2**,
включая Executor.recover, публикацию, изменяющий poll и cleanup. WARN не разрешает
запуск. Не снимайте recovery_required ручным SQL; процедура допуска описана ниже.

| Состояние | Действие |
| --- | --- |
| Новая установка, БД отсутствует | Проверить пути, явный bootstrap |
| БД отсутствует неожиданно | Исправить пути; не bootstrap поверх потери состояния |
| Совместимая V2.3 без metadata | Остановить, register с отдельным backup, check |
| Чужой UUID / ошибочный workspace | Исправить конфигурацию; bind только при осознанном переносе |
| Неверная схема/версия/логические связи | Отказ; сохранить файлы, расследовать несовместимость |
| Физическая порча | Остановить все процессы, аварийная копия main+WAL+SHM; автоматический restore в этой версии отказывает из-за невозможности доказать состояние исполнителей |
| Активный executor / неизвестная остановка | Отказ restore; сохранить данные и выяснить состояние |
| Валидная остановленная БД и совместимый backup | restore, check; барьер до внешней сверки |
| Recovery WARN | Не запускать повторно Codex; требуется процедура допуска |

Аварийное сохранение: при доказанной остановке скопируйте основной файл и все
существующие `-wal`/`-shm` в новый приватный каталог, сохранив имена; ограничьте права
0600. Не запускайте SQLite на единственном экземпляре повреждённых файлов. Это
сырой аварийный набор, а не проверенный SQLite backup. Незакоммиченные транзакции,
изменения после snapshot, процессы и неопубликованные локальные файлы backup не
восстанавливает. Он также не откатывает GitHub и удалённые git repositories.

Отчёт реализации и проверок: [docs/v2.4.1-implementation.md](docs/v2.4.1-implementation.md).

### V2.4.2: upgrade и reconciliation

При переходе с зарегистрированной V2.4.1 остановите daemon и дождитесь завершения
его исполнителей. При наличии SQLite sidecar сначала сохраните аварийный комплект,
затем после доказанной остановки выполните checkpoint и закройте соединение.
Все изменяющие recovery-команды требуют обе InstanceLock, отсутствия control
socket и WAL/SHM. PID сам по себе не доказывает остановку.

    codex-dispatcher --config config/dispatcher.yaml db upgrade
    codex-dispatcher --config config/dispatcher.yaml db check

Upgrade проверяет точный исторический DDL и UUID, создаёт проверенный 0600
*.before-upgrade-UUID, затем транзакционно устанавливает version=2 и format_version=2.
Recovery-флаг сохраняется. Повторный upgrade безопасен; обычный запуск старую схему
не мигрирует. Новый bootstrap создаёт актуальную схему. Restore принимает только
зарегистрированные backup схем 1/2 того же UUID, мигрируя историческую схему только
в staging. Карантин и аудит из backup v2 сохраняются.

После restore, при остановленном диспетчере:

    codex-dispatcher --config config/dispatcher.yaml db recover --dry-run
    codex-dispatcher --config config/dispatcher.yaml db recover
    codex-dispatcher --config config/dispatcher.yaml db recovery status

Начиная с V2.4.2, эти recovery-команды по умолчанию показывают Rich-таблицы
с repository и обычным номером Issue. Несколько причин карантина объединяются
в одну строку Issue. Например (синтетические данные):

```text
╭──────────────────────────────────────────────────────────────────╮
│ CODEX DISPATCHER · RECOVERY                                      │
╰──────────────────────────────────────────────────────────────────╯
Database version: 2
Recovery required: NO
Global admission: READY
Quarantined issues: 1
Unfinished operations: 0
                       QUARANTINED ISSUES
┏━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ Repository ┃ Issue ┃ Reason(s)           ┃
┡━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ owner/repo │ #7    │ execution-uncertain │
│            │       │ local-worktree      │
└────────────┴───────┴─────────────────────┘
No recovery operations
```

`DRY RUN / NO CHANGES` обозначает только план; `RECOVERY APPLIED` — применение.
Сводка показывает текущий глобальный допуск отдельно от индивидуального карантина:
`Recovery required: NO` не означает отсутствие карантинных Issues. Пустые разделы
явно показывают `No quarantined issues` и `No recovery operations`.
При pipe/перенаправлении и `NO_COLOR=1` ANSI-коды отключены, длинные ячейки
переносятся по ширине терминала.

**Машинным потребителям обязательно добавить `--json`**: default-вывод изменён
с JSON на таблицу. Каждая команда возвращает один JSON-документ без цветов;
прежние поля и типы сохранены, добавлены `repository` и `issue_number` вместе
с сохранённым глобальным `issue_id`. В результатах административных решений
также доступны `action` и `phase`. Полные operation ID, comment ID и даты операций
доступны в JSON статуса.

    codex-dispatcher --config config/dispatcher.yaml db recover --dry-run --json
    codex-dispatcher --config config/dispatcher.yaml db recover --json
    codex-dispatcher --config config/dispatcher.yaml db recovery status --json
    codex-dispatcher --config config/dispatcher.yaml db recovery accept PROJECT ISSUE --json
    codex-dispatcher --config config/dispatcher.yaml db recovery retry PROJECT ISSUE --json
    codex-dispatcher --config config/dispatcher.yaml db recovery cancel PROJECT ISSUE --json

Зависимость `rich>=13` устанавливается стандартной командой `pip install -e .`
(для тестов — `pip install -e '.[test]'`).

Dry-run читает приватную SQLite-копию, GitHub и Git, не создаёт lock-файлы, не
обновляет refs/index и не пишет в SQLite/GitHub. Настоящий recover полностью
собирает открытые и зарегистрированные закрытые Issues, все PR, отношения,
approval snapshots, local/remote refs и worktree; повторяет проверки перед commit.
Одна транзакция сохраняет session, карантины, допустимые переходы и снимает
общий барьер. Ошибка обязательного GET, изменение наблюдений, неизвестный worktree,
грязный основной checkout или недоказанная идентичность сохраняют общий запрет.
Приватная диагностика отказа: workspace/diagnostics/recovery-errors.jsonl.
Включённые debug API-метрики буферизуются; успешная изменяющая операция сохраняет
их в debug_operations, dry-run не записывает их в SQLite.

Локальный конфликт блокирует соответствующую Issue и зависимые контейнеры.
Независимые задачи могут работать. Poll и повторный recover не снимают карантины.
Непроверенные disabled/удалённые repositories изолируются persistent state-conflict
для сохранённых Issues. Re-enable сам по себе не разрешает выполнение.
Новые после backup Issues обнаруживаются в discovered с approval-uncertain;
старому approve доверять нельзя.

Административные решения (PROJECT — текущий project.id, ISSUE — номер в его
текущем repository; глобальный GitHub ID проверяется повторно):

    codex-dispatcher --config config/dispatcher.yaml db recovery accept PROJECT ISSUE
    codex-dispatcher --config config/dispatcher.yaml db recovery cancel PROJECT ISSUE
    codex-dispatcher --config config/dispatcher.yaml db recovery retry PROJECT ISSUE

- accept требует доказанный PR: сохранённые ID/номер/Issue либо точное dispatcher
  тело вместе с подтверждённым result SHA, плюс head/repository. Имя ветки
  недостаточно. Устанавливает допустимый review/done/rejected с аудитом, без
  фиктивных executions. Независимые неразрешённые причины остаются.
- cancel допускает только разрешённый переход без опубликованного результата,
  проверяет группу, пишет аудит и сохраняет неизвестные локальные артефакты.
  Неразрешённые ветки/worktree/отношения остаются заблокированными.
- retry — явное согласие отбросить принадлежащую Issue незавершённую локальную
  работу. Требует открытой Issue, отсутствия PR/remote work branch/результата,
  доказанной принадлежности worktree и согласованных зависимостей. Чужие пути,
  неизвестные локальные ветки и shared correction groups не очищаются.

Retry сохраняет operation_id и архив до удаления файлов, удаляет старую approve
и наблюдает её отсутствие. Штатным git worktree remove удаляется только worktree
с execution ID и UUID; условно удаляется исключительно внутренний local ref.
Диагностика error_log сохраняется с отвязанным FK; активная executions удаляется,
state_history сохраняется. Issue возвращается в discovered с очищенным approval.
Диагностический GitHub comment содержит operation_id; перед каждым POST, включая
повтор после неопределённого ответа, полностью читаются комментарии и проверяется
маркер. Полученный comment_id сохраняется. Карантин снимается в последней
транзакции только для разрешённых причин. Диагностические файлы вне принадлежащего
worktree не удаляются.

При прерывании повторите **ту же** команду retry: durable-фаза сохраняется, запуск
до завершения запрещён. API error, изменившиеся предпосылки или новая публикация
оставляют карантин. После завершения проверьте status: другие причины могут
оставаться. Установите **новую** dispatcher:approve через GitHub только после
завершения retry. Без неё discovered не запускается; прежний hash не разрешает
выполнение.

Консервативные отказы, ограничения и ручная приёмка:
[docs/v2.4.2-implementation.md](docs/v2.4.2-implementation.md).
