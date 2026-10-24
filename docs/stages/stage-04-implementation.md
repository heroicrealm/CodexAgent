# Реализация STAGE_04

Нормативный документ: [stage-04-spec.md](stage-04-spec.md).

## Компоненты и данные

- `src/dispatcher/scheduler.py`: единый цикл polling/admission/collect/outbox,
  обход подготовленных проектов по кругу, повторный GET полного списка Issues перед
  резервированием. Используются существующие Store.apply, ready/fingerprint и
  GitHub reconciliation; модель подтверждения этапа 3 сохранена.
- `src/dispatcher/execution_store.py`: миграция №3, attempts, result_outbox,
  quota_observation. BEGIN IMMEDIATE сравнивает текущую версию, available,
  approved_version, content_hash и observation_token; считает running и блокирует
  ветку. Уникальны attempt_id и repository/Issue/version. Результат, история и
  outbox фиксируются одной транзакцией. Миграция запускается при создании
  ExecutionStore (daemon и show), сохраняет данные миграций 1–2 и идемпотентна.
- `src/dispatcher/workspace.py`: GitWorkspaceManager и TaskWorkspaceCleanup.
  Служебный clone через gh repo clone (настроенный SSH/HTTPS), fetch,
  credential helper gh без сохранения токена, check-ref-format, worktree add, проверка ветки/чистоты/
  ancestry, обычный push. Фактическая default branch читается через ls-remote;
  default/base не исполняются напрямую. Изменение origin во время exec запрещает
  push. При конфликте push сохраняются result_sha и локальные коммиты.
- `src/dispatcher/attachments.py`: IssueAttachmentManager. Bearer из `gh auth token`
  применяется только к github.com/api.github.com; токен не передается на CDN.
  Редиректы проходят явную проверку HTTPS/host/port/userinfo, максимум 5 переходов.
  Потоковая загрузка ограничивает размер и время, ошибки HTML/login и HTTP
  отклоняются. Имена нормализуются с уникальным числовым префиксом, traversal,
  управляющие символы и symlink отклоняются. Manifest включает и ошибки; частичные
  файлы сохраняются. При любой ошибке exec не запускается.
- `src/dispatcher/codex/__init__.py`: CodexExecutor; проверка version/help, prompt
  через приватный файл/stdin, cwd=worktree, exec --json в отдельной группе.
  Таймаут применяется ко всей группе, независимо от завершения лидера.
- `src/dispatcher/codex/quota.py`: независимый CodexQuotaProvider. Только initialize,
  initialized, account/rateLimits/read; выполнение через App Server отсутствует.
  Корреляция id, ограниченный reader queue, RPC timeout 10 секунд; неизвестные
  запросы получают method-not-found. События обрабатываются как уведомления и не
  подменяют свежий ответ read. Все возвращенные primary/secondary/individualLimit
  и buckets проверяются консервативно; непонятная квота блокирует admission.
- `src/dispatcher/publisher.py`: TaskResultPublisher. UTC Started/Finished, Duration,
  branch/SHA, статус, безопасный код ошибки, признак отсутствовавшего запуска.
  Детерминированный маркер attempt ищется во всех страницах комментариев перед
  каждым POST. Публикации сериализуются SQLite-транзакцией. При неясной сетевой
  ошибке следующий проход сначала ищет уже существующий комментарий.
- `runtime/__init__.py`: Lifecycle.submit теперь принимает cwd/stdio и служебные
  процессы. App Server учитывается отдельно от max_parallel_tasks, но проходит
  через тот же учет групп и wait. Добавлены cancel_child и приватный реестр групп
  для аварийного восстановления. poll/wait Popen вне Lifecycle запрещены.
- `config/__init__.py`, `config/config.example.yaml`: настройки execution, quota,
  attachments и строгая валидация. `cli.py`: расширенный show. `doctor.py`: новый
  путь служебного clone с совместимостью прежней диагностики.
- `storage/__init__.py`: SQLite получает 0600. Daemon устанавливает umask 0077;
  каталоги заданий 0700, prompt/log/manifest 0600, без сырых stderr в общем JSONL.
- `tests/test_stage04.py`: Git/process/HTTP/RPC/SQLite/CLI проверки.

Attempt хранит repository, Issue/version, branch, worktree, initial/result SHA,
started/finished/duration, exit code, код/категорию ошибки, codex_started, pushed,
PID/start identity, origin_url, remote_sha и recovery_diagnostic. Snapshot исполнения остается в tasks и history. Finished
фиксируется после push и до сетевой публикации; duration измеряется monotonic,
после аварии консервативно восстанавливается по UTC.

## Остановка и восстановление

Обычный stop запрещает admission и дожидается exec, проверки, push и фиксации
результата. Затем закрывается App Server. Force завершает управляемые группы через
TERM/grace/KILL, активные attempts получают interrupted. Pending сохраняются.
Резервирование без состоявшегося exec тоже оставляет итоговый комментарий.

При следующем start runtime lock исключает второй daemon. Реестр старых групп
проверяется по PID и start identity из /proc. Подтвержденные группы завершаются;
неоднозначный живой PGID блокирует startup без сигнала постороннему процессу.
Перед финализацией проверяются существование worktree, HEAD, dirty state и, при
неизменном origin, опубликованный SHA через ls-remote; результат сохраняется в
recovery_diagnostic/remote_sha. Недоступная сеть не запускает exec повторно.
После этого running превращаются в interrupted / crash_recovery, без нового exec
и push. Даже если push успел пройти, но результат не был зафиксирован, автоматического
успеха не предполагается: оператор проверяет origin и локальный worktree. Уже
зафиксированные outbox и review/failed не меняются. Частичные артефакты сохраняются.

Перед exec используется inert launch gate (`runtime/_child.py`): процесс ждет
подтверждения по отдельному pipe, пока реестр не записан с fsync/rename/fsync каталога.
До открытия gate wrapper использует Linux PR_SET_PDEATHSIG и проверяет parent PID;
смерть daemon/EOF не разрешают запуск команды. Так убрано окно запуска незарегистрированного
exec. После gate лидер остается доступен для проверки идентичности при восстановлении.
Самовольный setsid внутри исполняемого кода остается за пределами контракта Lifecycle.
Синхронные Git/GitHub операции ограничены таймаутами; сигнал меняет флаги сразу,
но завершение операции и обработка force могут ждать ее таймаута.

## Проверенный протокол и безопасные ограничения

В окружении разработки установлен `codex-cli 0.164.0`. Проверены `codex --version`,
`codex exec --help`, `codex app-server --help` и
`codex app-server generate-json-schema --out /tmp/codexagent-stage04-schema`.
Схема GetAccountRateLimitsResponse содержит rateLimits, rateLimitsByLimitId,
usedPercent/resetsAt и individualLimit; initialize использует clientInfo.
Справочник: [официальная документация App Server](https://learn.chatgpt.com/docs/app-server).
Реальная квота не вычисляется из логов/процентов вымышленного API; несовместимая версия,
ошибка авторизации, пустые окна или смерть App Server дают quota_unknown.
Backoff перезапуска ограничен 60 секундами. При paused повторная проверка идет по
интервалу либо после resetsAt с задержкой; таймер сам не доказывает восстановление.
Перед каждым доступным запуском выполняется новый read. Отключенный quota provider
вообще не создает App Server. Exec и App Server наследуют один CODEX_HOME/auth.

Применимость buckets определяется консервативно: проверяются все возвращенные
окна, включая дополнительные модельные buckets. Это может остановить очередь раньше,
чем ограничение фактически затронет выбранную модель. API-key аккаунт без читаемых
окон требует явного отключения quota.enabled оператором; неизвестное не считается
доступным. Структурированное исчерпание из exec распознается только по явным кодам
usage_limit_reached/quota_exhausted, без классификации обычного 429/сетевого сбоя.
Если CLI дает только человекочитаемую ошибку, результат codex_failed; вывод приватен.

Загрузчик поддерживает стандартные GitHub user-attachments/assets/files и
user-images/private-user-images, включая URL в Markdown/HTML/reference links.
Внешние ссылки не загружаются. Частные вложения, для которых установленный GitHub
endpoint не принимает gh token или требует browser session, дают диагностируемый
attachment_download_failed. HTML sign-in не считается файлом. Неизвестные CDN
и редиректы запрещены. Поддержку приватных вложений конкретного GitHub аккаунта
нужно проверить вручную; универсальная поддержка browser-only API не заявляется.
Суммарный максимальный размер ограничен max_files * max_bytes. Файлы не исполняются.

No-op требует одновременно утвержденную строку `No-op allowed: <объяснение>` в
Issue и `NO_OP: <объяснение>` в финальном agent_message. Никакой пустой успешный
процесс не переводится в review без этих условий. Объяснения остаются в snapshot
и приватном выводе, в комментарий не копируются.

Гарантия outbox опирается на чтение уже опубликованного маркера GitHub. При удалении
комментария оператором или задержке видимости сервером абсолютный exactly-once
без GitHub idempotency key недостижим. При обычной повторной доставке и неясном
ответе сети тест подтверждает отсутствие дубля. Ошибка комментария не меняет результат.

Новые локальные ветки не перезаписывают существующую служебную ветку: неожиданная
локальная история дает безопасный Git failure. Это сознательный отказ повторно
использовать незавершенную работу. После done следующий этап должен атомарно
перевести соответствующие attempts в done, освобождая branch lock; только после
этого вызвать TaskWorkspaceCleanup. Очистка чистого worktree штатная, attachments
удаляются вместе с manifest, branch и коммиты сохраняются. Повтор после частичной
очистки разрешен; dirty/symlink/неверный путь сохраняются с ошибкой. Вызова очистки
при review, failed, interrupted нет.

## Проверка и ручной end-to-end для приемки

Автоматические проверки запускаются без реальных токенов:

```bash
python3 -m venv /tmp/codexagent-stage04-venv
/tmp/codexagent-stage04-venv/bin/pip install -e '.[test]'
PATH=/tmp/codexagent-stage04-venv/bin:$PATH bash scripts/test.sh
```

Покрываются existing/missing/default ветки, измененная фактическая default branch,
изоляция пользовательского clone, push conflict, origin change, dirty/ancestry,
подтверждение и свежесть версии, конкурентное резервирование, параллелизм, stop,
force, timeout, ошибка/no-op/quota exec, сохранность файлов, вложения 0/несколько,
403/404/oversize/path/redirect/token forwarding/symlink/manifest, квоты ниже/равно/
выше, все окна, неизвестный протокол, reset/read, restart/один App Server,
отключенный контроль, сохранение активного exec при paused, миграции и outbox,
восстановление running/группы, done cleanup/dirty/partial/traversal.
Процессный end-to-end запускает настоящие CLI start/status/stop и локальный Git,
stub gh и stub Codex: polling -> exec -> commit -> push -> review -> комментарий.

Для ручного end-to-end с настоящими GitHub и Codex используйте отдельный тестовый
репозиторий и выделенный workspace. Этот сценарий не запускался автоматически:
ему нужны выбранные оператором тестовый репозиторий, Issue и действующая квота.

1. Указать repository, allowed_authors, workspace и logs в копии example YAML.
   Проверить `gh auth status`, SSH-доступ к repository и авторизацию Codex.
2. Выполнить `init` и `doctor`. Создать доверенным пользователем небольшую Issue:
   изменить тестовый файл, запустить тесты, создать commit; указать отдельную Target
   branch и ready. Добавить доступные GitHub-вложения, в том числе приватное.
3. Выполнить `poll`, `show PROJECT N`, изучить version/body/вложения, затем
   `approve PROJECT N` и `start`. Убедиться, что квота читается, ветка и worktree
   изолированы, manifest содержит sha256, вывод exec приватен.
4. Дождаться review, проверить origin SHA и ровно один итоговый комментарий с UTC
   Started/Finished/Duration. Убедиться, что worktree и attachments сохранены.
5. Проверить stop во время второго задания и force на отдельном задании;
   interrupted не запускается заново. Проверить недоступное вложение: Codex не
   стартует, комментарий [ATTACHMENT]. Проверить quota pause/resume без прерывания
   активного exec. Проверить потерю связи при публикации и повторную доставку.
6. Не создавать PR, не делать merge/accept/done в этом этапе. Cleanup вызывается
   только тестом либо будущей ручной приемкой после done. Issue этапа не закрывать.

Риски приемки: широкий доступ full-auto к ОС, контекст SSH/Codex авторизации,
совместимость экспериментального протокола, browser-only приватные вложения,
неатомарность GitHub/SQLite и внешнего push, задержки синхронных сетевых операций,
ограничения process groups при потере идентичности лидера/setsid. Не выдавать stub-проверку за реальное
выполнение задания облачным Codex.

## Результат автоматической проверки

Временное virtualenv с Python 3.14, pytest 9.1.1, PyYAML 6.0.3.
Команда `PATH=/tmp/codexagent-stage04-venv/bin:$PATH bash scripts/test.sh`:
**217 passed, 30 subtests passed**, compileall завершился успешно.
`git diff --check` — без ошибок. Внешняя сеть, реальные квоты и токены в тестах
не используются. Результат реального GitHub/Codex end-to-end не заявляется.
