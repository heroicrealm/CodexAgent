# Реализация 6.4.1.7 — Detach и адресный emergency-stop

Ветка: `STAGE_06_04_01_07`. Основание:
[обязательное ТЗ](stage-06-04-01-07-spec.md).

## Уточнение и границы процессов

Первоначально зафиксирован B1: исчезновение исходной process group не доказывает
завершение потомков, выполнивших setsid/setpgid. Пользователь 2026-10-26 выбрал
сохранение существующих границ Lifecycle. Уточнение внесено в ТЗ: остановка и
cleanup подтверждаются для исходной группы, без требования cgroup. B1 снят.
`tests/test_stage60417_blocker.py` сохранён как воспроизводимая проверка этой границы:
в отдельном subreaper-harness потомок уходит в другую сессию; тест явно завершает
и reap-ит его после проверки. Он не является обещанием поддержки демонизации.

## Интеграция и изменённые файлы

- `detachment.py`: durable intent с reason, previous state, stage, attempt,
  timestamps, process status/method/error/evidence и durable backoff неподтверждённых сигналов; CAS поколения после сетевой
  проверки; независимый StopWatcher с собственными Store/Adapter. Подтверждение
  emergency через актуальные labels и последнее labeled event разрешённого
  человеческого actor. Без доказательства сигналы запрещены. Stop intent имеет
  приоритет над обычными queue commands; отсутствие ready является наблюдением
  потери eligibility, как в прежней модели revocation, без требования approve.
- `storage/__init__.py`, `storage/transitions.py`, `execution_store.py`: intent
  ограждает confirm/decide/reserve/transitions/finish. Снятие ready для известной
  Issue фиксирует intent в транзакции apply; отсутствие Issue в выдаче open Issues
  само по себе не считается снятием метки. Identity проверяется до intent.
  Pending/idle переходят в detached сразу; running ждёт scheduler. Task outcome
  отделён от attempt outcome; естественный exit=0 остаётся completed, настоящий
  exit_code сохраняется, обычный result_outbox не создаётся. Done/cancelled
  сохраняют историческое состояние при обычном Detach и emergency.
- `scheduler.py`, `attachments.py`, `codex/__init__.py`, `runtime/__init__.py`: проверка после reserve,
  перед/между подготовительными Git шагами и перед запуском. Launch gate фиксирует
  pid/start identity в SQLite до exec; durable stop проверяется под writer lock.
  Только Lifecycle управляет reaping/signals; его операции сериализованы RLock.
  Адресный stop_attempt проверяет identity, посылает TERM, ждёт grace, затем KILL
  и ещё grace; отсутствие подтверждения возвращается как незавершённая остановка.
  Git/gh helpers подготовки/публикации также запускаются через Lifecycle в отдельных
  зарегистрированных группах (`attempt_processes`) с launch gate; restart/cleanup
  учитывают их, а не только Codex PID. Обычный Detach не сигналит такие операции,
  emergency адресно завершает их. Глобальный stop не вызывается. Прежние stop/stop --force продолжают работать.
- `runtime.serve`/recovery: старые группы со stop intent исключаются из прежнего
  общего crash cancellation. Ordinary detach продолжает ждать естественно,
  emergency продолжает адресную остановку. До исчезновения группы cleanup запрещён.
  Orphan registry сохраняется, завершённые записи удаляются. Codex не перезапускается.
  В обычном Detach прежний deadline Codex не становится причиной сигнала; отдельные
  явные stop --force и штатное поведение службы сохраняются.
- `github/__init__.py`, `github/sync.py`, `queue.py`: команды остановки раньше
  queue commands/PR monitor; targeted reads покрывают также tracked closed Issues.
  Reconcile перечитывает SQLite и удаляет только точные configured managed names
  отдельными DELETE, без label-set replacement и без удаления по префиксу.
  Emergency никогда не удаляется синхронизацией. При подтверждённой emergency
  снимаются ready и все остальные managed labels; пользовательские остаются.
  Неподтверждённая emergency не разрешает потреблять команды или посылать сигналы,
  но запрещает admission/publication до успешной проверки/ручного удаления.
- `pr_publication.py`, `workspace.py`, `terminal_error.py`: fence перед commit,
  непосредственно внутри Git manager перед commit/push после предварительных
  проверок, перед create/reopen PR и terminal salvage. Каждый критический fence
  перечитывает remote labels и проверяет durable intent/version/iteration.
  Publication flock и publication_operations сохранены. Уже начатые эффекты не
  откатываются: SHA, pushed/remote SHA и найденный PR записываются как факты.
  `attempt_publications` хранит факт PR даже если stop запретил переход в review.
- `publisher.py`: result delivery использует flock и проверяет stop/generation,
  включая fresh labels перед POST. Сетевые операции вынесены из SQLite writer
  transaction, чтобы они не задерживали фиксацию stop intent. Marker reconciliation
  и publication_operations сохранены. `preparation_error.py`, `pr_monitor.py`,
  `cancellation.py` проверяют durable stop перед очередным внешним эффектом.
- `detachment.py`/`stop_outbox`: один diagnostic comment на request с marker,
  flock, durable body/status/error и поиском перед POST. Lost response не создаёт
  повторный комментарий; последующий прогресс меняет тот же комментарий PATCH.
  Содержит причину, предыдущее состояние, preparation/codex/publication/idle,
  attempt, TERM/KILL/not_required, process/cleanup status/errors, branch/SHA/PR.
  Branch metadata экранируется и редактируется по политике обычного publisher.
- `cleanup.py`, `workspace.py`: detached включён в прежние durable cleanup jobs
  и generation guards; исходная группа и leader identity проверяются до удаления.
  Разрешён force-remove dirty worktree только после ownership/preflight: workspace
  boundaries, symlink, Git metadata/common-dir, зарегистрированные worktrees,
  attempt branch и ancestry initial SHA, artifact/attachment registry. Дочерний
  Git helper очистки наследует cleanup flock FD, поэтому recovery не начинает
  повторное удаление, пока старый helper ещё выполняет операцию. Чужие файлы,
  неизвестные архивы и неподтверждённые процессы сохраняются. Detached retries не
  заканчиваются после обычного лимита четырёх попыток. Remote refs/PR не удаляются.
- `config/__init__.py`, `initialization.py`, `config/config.example.yaml`: optional
  emergency_stop name/color/description; interval default 5, positive integer.
  Init/--update создают и обновляют метку. Fingerprint включает имена всех меток и полные свойства
  emergency-метки; смена цвета/описания обычных меток сохраняет прежний контракт. Старые YAML и строковая форма поддержаны; прежние default names
  остальных меток загрузчика сохранены. После upgrade нужен init --update.
- `cli.py`, `rendering.py`: show table/JSON выводит stop_requests и stop_outbox
  вместе с существующими attempts/publication/cleanup diagnostics.
- `README.md`, ТЗ и этот отчёт: семантика, конфигурация, согласованная граница
  процессов, миграция, проверки и ручные сценарии.

## Миграция SQLite 609

Аддитивная, в существующем BEGIN IMMEDIATE/validation/marker механизме Store.
Создаёт stop_requests, partial unique stop_active, stop_outbox и
attempt_publications, attempt_processes и control_observations. Последняя таблица
запрещает применение более старых серверных control snapshots к новому циклу. История attempts/iterations/PR/decisions не сбрасывается.
Повторное открытие/повторная миграция идемпотентны; DDL и marker откатываются вместе.

Partial branch index и claim triggers исключают detached из активных броней;
использующие их SQL reserve обновлены согласованно. Attempt освобождается только
после подтверждённого завершения группы. Terminal review_on_error guard разрешает
переход в detached только при durable stop intent; без intent остаётся прежний
terminal guard. Cleanup enqueue triggers включают detached и сохраняют прежние
межпроцессные flock/generation guards.

Повторное ready после исчезновения emergency, finalized stop и completed cleanup
может начать discovered только по более новому серверному наблюдению. Старые
attempts не восстанавливаются. После approve создаётся новый iteration_id согласно
существующей модели approved iterations. Исторический stop остаётся с active=0.
При занятой remote branch действует прежний branch_ownership_conflict/configuration
error путь; разрешение повторно использовать ветку rejected PR на Detach не переносится.

## Проверки

Новые acceptance tests: `tests/test_detachment.py`. Существующие fixtures в
`test_stage04.py`/`test_stage603.py` дополнены явными targeted control reads; gh stub
теперь различает list Issues и GET конкретной Issue. Проверки JSON/show в
`test_cli_unicode.py`/`test_rendering.py` учитывают новые diagnostics. Stage 2
revocation test в `test_github.py` различает прежнее исчезновение eligibility и
новый явный Detach при снятии ready, а reattach выполняет после cleanup и более
нового server observation. `test_stage604.py` использует новые серверные даты для
новых правок/закрытия после публикации; fixture claim в `test_issue32.py` сохраняет
конкурирующую Issue в актуальном fetch, чтобы проверять живую бронь, а не отозванную.

Проверены idle states, done/cancelled, running natural exit и dirty cleanup,
preparation stop, две активные попытки и изоляция emergency, ready/no-ready,
сохранение сторонних меток, untrusted actor, fence commit/push/PR, lost response,
SIGKILL для живого потомка после выхода лидера, restart natural drain, миграция
с 608/повтор/rollback, custom config/fingerprint, ownership failure/retry,
неподтверждённая identity, reattach и конфликт уже опубликованной ветки.
Независимый watcher проверяется через настоящие subprocess с подменёнными gh/Codex,
без вызова основного poll/collect во время обнаружения/остановки.

Тестовая среда: `/tmp/codexagent-60417-venv`, установлен editable `.[test]`.
В системном Python pytest отсутствовал; зависимости/pyproject не менялись.
Первый baseline до реализации: 608 passed, 30 subtests passed (40.40s).
Итоговый прогон после всех изменений:

```text
PATH=/tmp/codexagent-60417-venv/bin:$PATH bash scripts/test.sh
642 passed, 30 subtests passed in 91.03s (0:01:31)
```

Exit code 0; compileall src/tests также прошёл. `git diff --check` — успешно.
Включены 34 новых acceptance-сценария `test_detachment.py` и существующая регрессия
review_on_error, rejected→pending, PR publication, cleanup, stop/stop --force.
Промежуточные прогоны выявили и исправили terminal preparation fencing, targeted
GET в gh fixtures, совместимость fingerprint обычных меток и устаревшие серверные
даты в тестовых снимках. Проверки не используют реальные учётные данные GitHub.

## Ограничения

- Гарантии относятся к исходной process group, как согласовано в ТЗ; setsid,
  setpgid и демонизация вне контракта. Linux /proc необходим для подтверждения.
- Remote label → обнаружение → SQLite и внешний GitHub/Git эффект не являются
  единой транзакцией. Не обещается отмена уже начатого эффекта. Интервал 5 секунд
  ограничивает частоту проверок, не гарантирует такой latency при медленном API.
- После crash невозможно получить exit_code уже осиротевшего процесса из чужого
  waitpid; неизвестный код остаётся NULL, а не подменяется успешным результатом.
  Записанный exit_code сохраняется. Recovery не запускает Codex повторно.
- При недоказанном ownership (например, crash внутри worktree creation до сохранения
  initial SHA), неизвестных артефактах или непроверяемой surviving группе автоматическая
  очистка сохраняет данные и диагностику. Исправить причину и повторить cleanup-retry;
  detached cleanup продолжает безопасные retry. Автоматически присваивать владение
  произвольным найденным файлам нельзя.
- Live GitHub приёмка не проводилась; реальные Issue/PR для тестов не создавались.

## Ручная приёмка

В отдельном тестовом проекте с выделенными workspace и учётной записью:

1. Обновить конфиг, выполнить init --update. Проверить configured emergency name,
   color, description; старый YAML без новых ключей должен загружаться с default 5.
2. Для discovered/pending/revoked/review/rejected снять ready, выполнить poll.
   В show проверить ready_removed, detached и cleanup. У done/cancelled историческое
   состояние должно сохраниться. До запуска Codex не должно появиться новой попытки.
3. Запустить два долгих stub задания. Снять ready с первого: процесс заканчивает
   естественно, exit_code сохранён, новых dispatcher commit/push/PR/comments нет.
   Второе работает. После подтверждения завершения первой группы dirty worktree
   удаляется при доказанном ownership.
4. Повторить с emergency (как с ready, так и без неё) от allowlisted человека.
   Дождаться отдельной проверки watcher; только первая группа получает TERM/KILL,
   служба и второе задание работают. Для stub, игнорирующего TERM, проверить KILL.
   Повторить с вышедшим лидером и живым потомком той же группы.
5. Добавить `bug` и `dispatcher:custom`. После emergency они и emergency остаются,
   ready и только configured managed labels удаляются. Добавление ready при
   emergency не запускает Codex. Команда от bot/неразрешённого actor не даёт сигналов.
6. Отключить GitHub во время diagnostic POST и восстановить сеть. Повторить poll,
   перезапустить службу: один комментарий по marker, обновляется тот же comment_id,
   блокировка не исчезает. Проверить stage/method/process/cleanup/SHA/PR/errors.
7. Убить daemon на границах intent/signal/cleanup, запустить снова: ordinary ждёт
   без сигналов, emergency продолжает адресную остановку; нового Codex нет.
   При неподтверждённой identity/группе worktree сохраняется и stop не finalized.
8. После completed cleanup добавить ready (для emergency сначала удалить её).
   После нового poll — discovered, после approve — новый iteration_id, история цела.
   При существующей remote branch отказ branch_ownership_conflict, без force push.
9. Внести чужой файл рядом с worktree, symlink или неизвестный архив: cleanup
   pending/error, данные сохранены. Устранить причину и проверить retry/cleanup-retry.
10. Инъецировать команду между git add/commit, проверками/push, поиском/create/reopen
    PR и после PR. После обнаружения — нет следующих эффектов; уже начатые эффекты
    записаны как факты. Проверить rejected→pending, review_on_error, done/cancelled
    cleanup и stop --force по прежним сценариям.
