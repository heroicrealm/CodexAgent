# 6.4.1.7 — анализ интеграции и блокер подтверждения остановки

Статус: **реализация Detach/emergency-stop заблокирована до уточнения контракта
процессов**. Ветка: `STAGE_06_04_01_07`. Обязательное
[ТЗ](stage-06-04-01-07-spec.md) не изменялось. Этот документ не является отчётом
об успешной реализации или приёмке этапа.

## B1: исчезновение process group не доказывает отсутствие пользователей worktree

Пункты 6–7 ТЗ требуют подтверждения завершения процессов до удаления артефактов
и запрета успешной очистки при невозможности подтверждения. Однако README
(раздел Lifecycle) явно исключает `setsid/setpgid` и демонизацию из гарантий.
Codex запускается с отключёнными sandbox/approvals и технически может создавать
таких потомков. Это не предположение о вредоносности: обычный subprocess с
`start_new_session=True` уже покидает исходную группу.

`Lifecycle._active()` ищет живые процессы только по PGID лидера, затем освобождает
лидера через `wait()`. `cancel_child()` после исчезновения этой группы возвращает
управление. Потомок с новой сессией может продолжать работать с тем же cwd и
файлами. `CleanupService.owned_row()` проверяет состояние attempt и identity
сохранённого лидера; полное множество таких потомков не сохраняется.
Проверки symlink, Git metadata и ownership артефактов не заменяют доказательство
отсутствия процессов.

Добавлен изолированный воспроизводитель
`tests/test_stage60417_blocker.py`: реальный потомок лидера вызывает новую сессию,
исходная группа адресно останавливается, Lifecycle возвращает пустой active,
а потомок всё ещё держит cwd моделируемого worktree. Отдельный harness становится
Linux subreaper, после проверки убивает и reap-ит потомка; production cleanup
не вызывается. Тест проходит, когда воспроизводит **текущий пробел**, и не является
тестом приёмки новой функциональности.

Один обход `/proc` по cwd/fd или PPID не решает проблему: процессы могут менять
cwd, закрывать fd, переподчиняться после смерти родителя и создавать потомков
между проверкой и удалением. Нельзя сигналить неизвестные сохранённые PID без
защиты от reuse. Нельзя объявить `cleanup=completed` на основании одной пустой
исходной группы, если требование распространяется на всех потомков.

Для продолжения нужен явный выбор границы:

1. Строгая изоляция попытки, например делегированная cgroup v2. Расширить Lifecycle,
   gate и durable registry: включать процессы в контейнер владения **до exec**,
   подтверждать отсутствие всех членов перед cleanup, восстанавливать владение
   после рестарта. В среде без доступного backend запуск должен быть запрещён,
   а не молча переходить к более слабой гарантии. Это меняет требования к
   развёртыванию и поддержку существующих сценариев. В проверенной среде
   `/sys/fs/cgroup` не доступен текущему пользователю на запись; наличие cgroup2
   mount само по себе не доказывает делегацию.
2. Явно ограничить ТЗ существующим контрактом: исполнители не покидают исходную
   process group. Тогда проверка всей группы достаточна внутри этой границы,
   но гарантия не распространяется на произвольные демонизированные процессы.
   Это изменение требований, которое нельзя принять за пользователя.

До выбора не добавлены ни удаление грязных worktree, ни ложное подтверждение
остановки. Вопрос передан пользователю. Технически расширить Lifecycle возможно;
блокер — несовпадающие гарантии и необходимость выбрать поддерживаемую среду,
а не утверждение, что Detach принципиально нереализуем.

## Карта интеграции после снятия B1

| Узел | Текущая граница | Необходимая интеграция |
| --- | --- | --- |
| `scheduler.py` | `admit` делает fresh fetch/apply, reserve, синхронную preparation; `collect` после exit=0 сразу создаёт publication intent | Команды должны фиксироваться до admission и collect. Fence после reserve, перед подготовкой и запуском, после подготовки и перед каждым эффектом. Ordinary detach ждёт естественного конца, emergency адресно вызывает Lifecycle. Task и attempt outcomes разделить, настоящий exit_code сохранить. |
| `storage/__init__.py` | `apply/_revoke` переводят mutable tasks в revoked; running сохраняется, available=0. `decide`, `_set_state`, `save_branches` используют transaction/CAS | Durable intent с expected version/iteration/attempt и журналом в той же транзакции. Не превращать отсутствие Issue в fetch автоматически в доказанное ручное снятие ready. Проверить identity до изменения intent. Intent должен ограждать все пути решений. |
| `storage/transitions.py` | Guarded transitions проверяют authority, evidence, iteration, version и journal | Добавить запрет обычных переходов при stop intent. Не обходить guard прямым UPDATE из scheduler. done/cancelled сохраняют исторический результат. Отдельно согласовать взаимодействие с immutable review_on_error. |
| `execution_store.py` | Reserve проверяет observation token, лимиты, claims; finish создаёт result_outbox | Admission обязан проверять stop intent транзакционно. Завершение остановленной попытки не должно создавать обычный comment/PR intent или возвращать task в failed/review. |
| `github/sync.py` | fetch → apply → queue commands → PR monitor → cancellation → reconcile | Обработать emergency раньше обычных команд/monitor. Независимая проверка активных попыток с собственным rate limit и Store/Adapter; не использовать общий SQLite connection из другого thread. Ошибка API не снимает durable блокировку. |
| `github/Adapter` | Fetch всех open Issues; label reconciliation по точным конфигурируемым именам, отдельных DELETE/POST; command_events даёт GraphQL proof и REST actors | Повторно читать критические метки. Никогда не удалять emergency, в том числе по stale row. При emergency удалить ready и только точный набор managed labels, сторонние сохранить. Не расширять удаление до префикса dispatcher:. Учесть tracked closed Issues. |
| `queue.py` | Human allowlist, labeled event, server time/revision fence, quarantine до наблюдения отсутствия; CLI также делает fetch/apply | Отдельная проверка emergency event/actor без зависимости от наличия ready и обычного approve boundary. Простого наличия метки недостаточно для выполнения команды. Неподтверждённая emergency не должна случайно превратиться в approve. |
| `runtime/Lifecycle` | Launch gate, process registry, group identity, удержание zombie-лидера; методы вызываются последовательно scheduler | Решить B1; bounded ожидание при адресной остановке с durable незавершённым статусом. Нельзя вызвать глобальный cancel/request_stop(force=True). Параллельный poll не должен конкурентно reap-ить детей. |
| `runtime.serve` / restart | `Lifecycle.recover()` до создания Dispatcher завершает старые зарегистрированные группы; Dispatcher затем восстанавливает publication либо finish interrupted | Ordinary detach не должен превратиться в сигнал после рестарта; учитывать durable intent до общего recovery. Emergency recovery должен адресно продолжать остановку; непроверяемые процессы сохраняют артефакты и диагностику. Не запускать Codex повторно. |
| `pr_publication.py` | Per-Issue flock, publication_operations, восстановление intent, commit → push → create/reopen PR → verified transition | Fencing перед каждым необратимым шагом и fresh labels; проверка лишь в начале publish недостаточна. Уже начатый эффект сверить и сохранить как факт. Не возобновлять старый PR intent после detach/restart. |
| `terminal_error.py` | Отдельный salvage commit/push/comment путь для review_on_error | Такой же fence, включая pending terminal outbox. Сохранить terminal evidence; не продолжать salvage вопреки обнаруженной команде. |
| `publisher.py`, `preparation_error.py`, `pr_monitor.py`, `cancellation.py` | Несколько независимых durable delivery paths, включая старые PR resolutions | Запрет обычных внешних эффектов должен охватывать все очереди, а не только PR Publisher. Emergency diagnostic — отдельный durable outbox с маркером, reconciliation после lost response и сериализацией доставки. |
| `cleanup.py`, `workspace.py` | Durable jobs, flock, generation guards, ограниченные retry; dirty worktree сохраняется; ownership по attempt SHA/branch, Git metadata и registry | Detached допускает dirty cleanup только при доказанном владении и подтверждённом отсутствии процессов. Проверка начального SHA недостаточна после коммитов Codex. Повторять незавершённую безопасную очистку; не терять blocked job после исчерпания обычного retry. Не удалять remote branch/PR. |
| `config`, `initialization`, пример | COLORS определяет labels и init; fingerprint сейчас включает имена | Новый emergency key с name/color/description и interval=5 по умолчанию; совместимость строковой формы и прежних YAML. Учитывать upgrade init/fingerprint и recovery у stale init. Не добавлять в пример неработающие настройки. |
| `storage/migrations.py` | Аддитивные 605–608, transactional validation, triggers/indexes для claims и cleanup | Следующая идемпотентная миграция intent/progress/outbox. Detached исключить из активных claims во всех indexes/triggers/SQL, сохранив branch ownership remote. Новая итерация после reattach, без удаления attempts/history и без восстановления старого запуска. |

Отдельный архитектурный объём: текущий scheduler синхронно выполняет Git/gh
подготовку и публикацию (часть subprocess timeout до 120 секунд), поэтому добавить
`if elapsed >= 5` в основной цикл недостаточно для независимого emergency polling.
Потребуется отдельный наблюдатель с собственными соединениями, durable fencing и
согласованной передачей адресной остановки единственному владельцу Lifecycle.
Это разрешимый объём работ, сам по себе не основание ослаблять ТЗ.

## Изменения и миграции

Изменены только README, комментарий в примере конфигурации, этот документ и
воспроизводитель. Production-код и schema не менялись; последняя миграция — 608.
Миграция Detach, emergency label/interval, outbox и новые состояния **не реализованы**.
Не заявляется выполнение критериев приёмки 6.4.1.7. main и STAGE_06_04_01 не меняются,
PR не создаётся.

## Проверки

Прямой воспроизводитель:

```bash
PYTHONPATH=src python3 tests/test_stage60417_blocker.py /tmp/unique-process-probe
```

Использовать новый путь при каждом запуске. Ожидаемый вывод:
`original group stopped; escaped descendant still uses worktree`.

Первый `bash scripts/test.sh` не стартовал: в системном Python нет pytest.
Для повторного запуска создано отдельное временное окружение, зависимости
установлены через `pip install -e '.[test]'`; зависимости проекта не менялись.
Полный прогон:

```text
PATH=/tmp/codexagent-60417-venv/bin:$PATH bash scripts/test.sh
608 passed, 30 subtests passed in 40.40s
```

Exit code 0; скрипт также выполнил `compileall` для src/tests. `git diff --check`
прошёл. Существующая регрессия review_on_error, rejected→pending, PR publication,
cleanup и stop --force прошла. Это проверка исходного поведения и воспроизводителя
B1, а не автоматическая приёмка не реализованного Detach/emergency-stop.

## Ручная проверка блокера

1. Работать в рабочей ветке и временном каталоге, не в настоящем workspace службы.
2. Выполнить команду воспроизводителя выше или pytest для данного файла.
3. Проверить успешный exit и сообщение: адресно остановлена исходная группа,
   escaped descendant на момент проверки ещё жив и cwd равен тестовому worktree.
4. Harness сам посылает SIGKILL только своему усыновлённому unreaped потомку и
   выполняет waitpid; рабочие задания диспетчера не затрагиваются.
5. Не проверять emergency в настоящей Issue как реализованную команду: пока это
   обычная неизвестная метка, она не является средством остановки.

## План ручной приёмки после реализации

Эти сценарии пока **не выполнены**, начинать только после снятия B1 и реализации.

1. В тестовом проекте проверить ready removal для discovered/pending/revoked/
   review/rejected: detached, без запуска и публикации; done/cancelled сохраняются.
2. Запустить два длительных задания. Снять ready с первого: дождаться естественного
   exit, проверить сохранённый exit_code, отсутствие commit/push/PR от диспетчера,
   безопасную очистку. Второе продолжает работу.
3. Повторить с emergency при наличии и отсутствии ready; проверить TERM, затем
   KILL для игнорирующего TERM stub. Второе задание и служба продолжают работу.
   Проверить живого потомка после завершения лидера в выбранном контракте изоляции.
4. Добавить пользовательские `bug` и `dispatcher:my-label`; после emergency
   сохранить их и emergency, удалить только configured managed labels и ready.
   Повторное ready при emergency не запускает задачу.
5. После ordinary detach добавить ready: новая discovered/iteration после approve,
   прежние history/attempts сохранены. Занятая remote branch не перезаписывается.
6. Внедрить сбой до/после intent, сигнала, publication effect и cleanup, перезапустить:
   нет повторного Codex/PR/comment. При неподтверждённых процессах worktree остаётся,
   cleanup незавершён, диагностика доставляется и восстановление повторяется.
7. Отключить GitHub при доставке diagnostic, восстановить сеть, повторить poll:
   блокировка сохраняется, комментарий один, содержит фактические SHA/branch/PR,
   stop method, process/cleanup status и безопасные ошибки.
8. Внедрить гонки перед commit/push/create/reopen PR и после PR: после обнаружения
   команды нет новых эффектов; уже начатые эффекты записаны без обещания отката.
9. Проверить custom emergency label/init --update, старый YAML, повторную миграцию,
   rejected→pending, review_on_error, done/cancelled cleanup и stop --force.
