# Исправления D1–D3: отчёт реализации

Ветка: `STAGE_06_04_03`. База реализации: `a8309fc6af29f9107e42b5ccf13d71613687df8c`;
старт исправлений после документов: `ec346e7`.
Статус: реализация и автоматические проверки; ручная приёмка не объявляется.
PR/merge, закрытие Issue и изменения Playground этой работой не выполнялись.

## Аудит до исправлений

- `storage.content_hash` уже исключает labels/updated_at и включает id, title,
  body, target_branch, author. `Store.apply` свободно обновляет `discovered`,
  увеличивает версию при смысловой правке, оставляет `approved_version=NULL`.
  `pending` при пользовательской правке возвращается в discovered; защищённые
  running/review сохраняют execution snapshot и записывают отдельно observed.
  Поэтому новая модель версий и удаление snapshots не нужны. История сохраняет
  редакции и решения; утверждение привязано к version/hash/iteration.
- `queue.commands/_reason` проверяет свежий REST snapshot против независимого
  revision proof, actor, labeled event, server boundary, lastEditedAt и ready
  history; `Store.decide` атомарно проверяет observation token/version/state.
  CLI делает fresh fetch и отказывает при смене ранее показанной версии.
  Если edit/approve попали между poll и границу согласия нельзя доказать,
  сохраняется безопасный отказ с повторным подтверждением.
- Подтверждённые кодом причины/условия D1: `ContainerService.update_child`
  сравнивал SQLite `updated_at` с GitHub даже при одинаковом title/body; смена
  меток могла объявить stale. `ContainerMonitor.collect` безусловно превращал
  наличие approve в `child_snapshot_stale`, включая уже утверждённого pending/
  running ребёнка до reconciliation меток. Monitor сравнивал даже неутверждённый
  discovered с прошлым snapshot, вместо обычного ожидания новой редакции.
  Это воспроизведено stubs с реальным SQLite и типичной последовательностью
  подготовки/утверждения/состояний; live-хронология исходного инцидента недоступна.
- Служебный PATCH уже имеет durable intent, source/target hash, fresh GET,
  повторную проверку relationship/identity/labels/permissions/local resources,
  readback и controlled `Store.apply`. Это не атомарный CAS GitHub. Сохранены
  guards и отдельное подтверждение служебной правки без смены approved version.
- D2: `render_queue` печатал вне рамки отдельную строку на каждую Issue.
  Вызовы CLI table/JSON и `show` используют независимые ветви rendering.

## Изменения D1/D2 и карта файлов

- `containers.py`: timestamp churn не инвалидирует semantic snapshot в начальном
  guard. Незакреплённый discovered синхронизируется перед подготовкой из текущего
  дочернего документа. Новый version получает новый source/target intent.
  Незавершённый intent после crash и несовпавший текст остаётся fail closed до
  независимой синхронизации, без повторного PATCH неизвестного документа.
  Повторные GET непосредственно перед записью продолжают обнаруживать гонки.
  Исторический child stale blocker очищается только после успешной проверки.
- `container_monitor.py`: draft без approved version, work branch и PR допускает
  новый title/body и остаётся waiting. Author/identity и остальные ownership/PR
  проверки сохраняются. Approve при обычных нетерминальных переходах ребёнка
  не является stale; новая approve у done/cancelled продолжает блокировать
  интеграцию. Reject/emergency, реальные edits защищённого текста, wrong-base,
  detached open PR и нестабильные наблюдения продолжают блокировать.
  Monitor-owned исторические blockers очищаются прежним протоколом двух чтений.
- `rendering.py`: удалён только построчный хвост `Issue N: issue_type=...`.
  Таблица и существующая readiness рамка остаются; show/JSON сохраняют поля.
- `tests/test_corrections_d1_d3.py`: версии всех типов, stale approval fence,
  editable draft → approve → pending/running/review и существенная правка,
  B/C merged + D editable/waiting → approve/merge → ready, Unicode queue,
  служебное сохранение Markdown, диагностика и CLI.

## Карта D3 и хранение

- `timing.py`: optional Trace, monotonic duration, UTC microseconds, ContextVar
  cycle/operation и parent operation; export table/JSON/CSV и project/issue clear.
- `storage/__init__.py`: additive idempotent bootstrap таблицы и индексов
  `(project_id, issue_number, id)` и `timestamp_utc`; история/схема task не
  перестраиваются. Ошибка bootstrap timing не запрещает основную работу.
  Обёртки sync/approval/transition/publication наблюдают завершённые транзакции.
- `config/__init__.py`: строго boolean `diagnostics.timing_enabled`, default false.
  Старые конфиги совместимы. `cli.py`: команда timing с обязательным project,
  optional issue, положительным limit, table/json/csv и ограниченной clear.
- `github/__init__.py`: реальная граница транспортного вызова, HTTP response и
  latency каждой попытки; safe method/route без query, request IDs, retry delay.
  Выборочный отдельный GET проверяет PATCH Issue и POST/PUT labels только при
  включении диагностики. API failure/timeout не пишет тела и stderr.
- `github/sync.py`: poll/start/end/empty, управляющие метки, project sync,
  изменения состояния, ожидание watch. `scheduler.py`: task selection,
  monotonic poll delay и запланированное ожидание daemon.
- `containers.py`, `container_monitor.py`: prepare/base_branch/intents и
  monitor/readiness; `publisher.py`, `pr_publication.py`: dispatch/ack/retry и
  возраст PR outbox с durable operation key; `workspace.py`: Git/worktree/push;
  `codex/__init__.py`, `execution_store.py`: запуск и завершение выполнения.
- `README.md`: конфиг, грамматика CLI, эксплуатация, cleanup и пределы измерений.

Запись best effort отдельным соединением с busy timeout 10 мс, вне долгих сетевых
операций и state transaction; при блокировке событие теряется, предупреждение
ограничено одним на процесс. Автоматического retention нет: экспорт и `--clear`
после сеанса ограничивают рост вручную. Disabled не добавляет GitHub GET.
Глобальные события продублированы по проектным шкалам; номера Issue никогда не
используются без project/repository. Payloads, prompts, bodies, headers, query
и пути не записываются. CSV защищён от spreadsheet formula injection.

## Проверки

Первый полный прогон: **923 passed, 2 failed, 30 subtests passed** (169.34 s).
Ошибки: `test_recovery_never_overwrites_third_party` и
`test_child_control_labels_fail_closed[approve]`. Они выявили ослабление recovery
и terminal approval fence; обе причины исправлены, исходные тесты сохранены.
Промежуточный повтор: **933 passed, 30 subtests passed** (159.41 s).
Окончательные результаты и хеш реализации приведены ниже после полного прогона.

## Ограничения и измерение задержки

Причина исходных приблизительно трёх минут **не установлена**: нет live trace
с GitHub/daemon/UI. Гипотезы для проверки: очередь допуска/длинная операция,
повторные проверки контейнера, HTTP retries/rate limit, outbox recovery,
ожидание следующего poll. Ни одна не объявляется доказанной и оптимизации нет.
Частота poll, порядок очереди и публикаций не менялись.

GET → PATCH остаётся неатомарным окном GitHub: обнаруженные правки сохраняются,
но абсолютное отсутствие потери необнаруженной правки в последнем окне обещать
нельзя (существующий регрессионный тест residual window сохранён). Длительность
HTTP включает gh process; request_start — граница вызова транспорта, не сетевой
packet capture. Выборочный diagnostic GET использует обычный bounded retry,
поэтому enabled может добавлять latency; disabled дополнительных вызовов нет.
Queue age outbox использует durable wall timestamp последнего шага, а durations —
monotonic. UI visible измеряется только вручную с погрешностью.

## Ручная проверка heroicrealm/CodexAgentPlayground

1. Настроить отдельный проект Playground; сохранить YAML/DB, выполнить init,
   убедиться, что выбран правильный `projects[].id`. Включить timing и
   перезапустить daemon; зафиксировать UTC начала и `poll_interval_seconds`.
2. Создать контейнер A и прямых детей B/C/D. Дать A `codex-ready`, дождаться
   discovered, затем approve. Дети B/C имеют ready, но approve выдаётся отдельно
   после подготовки base_branch. Проверить `show`/`queue`, затем отдельно
   утвердить B. Наблюдать pending/running/review через повторные poll: родитель
   ждёт, ложного child_snapshot_stale нет. Повторить C.
3. Утверждённым способом слить канонические PR B/C именно в ветку A. D оставить
   ready/discovered без approve; несколько раз изменить title, Markdown и YAML,
   дождаться sync. A остаётся awaiting_children/waiting, D не запускается.
   Утвердить D, завершить и подтвердить merge канонического PR в ветку A:
   readiness становится true только после подтверждения всех детей.
4. Проверить queue — рамочная таблица без строк `Issue N: issue_type=...`;
   show и JSON содержат issue_type/execution_mode, Unicode не испорчен.
5. Создать новый контейнер и новых детей для отдельного замера. Записать UTC
   установки approve родителю; экспортировать проектную шкалу (включая события
   без issue) и отдельно шкалы родителя/детей:

   ```bash
   codex-dispatcher --config config/config.yaml timing --project playground --limit 10000 --format csv > timing.csv
   codex-dispatcher --config config/config.yaml timing --project playground --issue 42 --limit 1000 --format json > issue-42.json
   ```

6. Сопоставить signal observed → sqlite_sync/state_changed → task selection →
   container_prepare/child_base_branch → durable intent → github_request_start →
   response → readback. Использовать cycle/request IDs и parent_operation_id;
   отметить interval, где накопились минуты, а не приписывать их GitHub по UI.
   Сохранить ошибки/retries и пропуски trace при недоступной БД.
7. В браузере вручную отметить UTC первого появления base_branch, частоту refresh
   и погрешность (например ±5 s). Это внешняя заметка, а не автоматически
   выведенный github_web_visible. Сравнить с API response/readback отдельно.
8. После экспорта выключить timing, перезапустить daemon; при необходимости
   `timing --project playground --clear`. Не оптимизировать scheduler до анализа
   доказательств и отдельного решения. Эта инструкция не объявляет приёмку.

## Коммиты реализации

- `bc92a9d` — D1/D2, основная трассировка D3, документация и регрессии.
- `88750a6` — включение трассировки также в CLI approve/reject.
- `f65820a` — HTTP-события ребёнка не наследуют SQLite identity родителя.
- `ddeeffa` — диагностические SQLite-соединения закрываются сразу после короткой
  транзакции, не ожидая сборщика мусора.

Диагностический readback сохраняет `Adapter.response_date`, чтобы измеритель не
менял server fence основной логики. Отдельный тест проверяет это свойство.
Профильный набор: **16 passed**; окончательный полный прогон фиксируется ниже.

## Окончательная верификация

Для дерева реализации `ddeeffa`:

- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_corrections_d1_d3.py`:
  **16 passed in 1.16s**.
- `PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh`:
  **941 passed, 30 subtests passed in 183.96s (0:03:03)**, exit 0;
  встроенный compileall также завершился успешно.
- Отдельно `python3 -m compileall -q src tests`: exit 0.
- `git diff --check`: exit 0.

Дополнительные промежуточные полные прогоны после исправления исходных двух
регрессий: 939 passed / 30 subtests (163.67s и 160.81s), затем 940 passed /
30 subtests (184.22s), 941 passed / 30 subtests (191.69s). Рост числа тестов
соответствует добавлению проверок CLI и identity; ошибки не скрыты выше.

Отправка коммитов выполняется в существующую ветку без PR/merge. Локальный SSH
transport недоступен (`Permission denied (publickey)`); доступ через существующую
HTTPS-авторизацию `gh` проверен. Для push используется временная настройка
транспорта команды, без сохранения токенов и изменения remote URL в конфиге.
