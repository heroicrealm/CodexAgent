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

**После restore исполнение заблокировано постоянно в V2.4.1**, включая pending,
running и старые approve. WARN сообщает о необходимости допуска; это не разрешение
на запуск. Автоматической сверки GitHub, worktree, PR и внешних процессов, команды
resume/reset и rebuild из GitHub здесь нет. Требуется отдельная проверяемая процедура
допуска в V2.4.2. Не снимайте recovery_required ручным SQL ради запуска.

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
