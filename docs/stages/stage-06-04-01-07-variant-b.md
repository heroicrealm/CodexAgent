# 6.4.1.7, вариант Б — escaped descendants: блокер и безопасный отказ

**Статус: НЕ РЕАЛИЗОВАНА полноценная граница потомков; приёмка Б НЕ пройдена.**
Ветка STAGE_06_04_01_07; новая ветка и PR не создаются. Новый запрос отменяет
прежнее согласование PGID-only. Раздел «Дополнение к приёмке — вариант Б»
сначала отсутствовал в локальном checkout; перед push получены пользовательские
коммиты 5875688/fc6b087. В результате rebase сохранён точный upstream-текст ТЗ,
без локальной реконструкции или ослабления критериев.

## Фактическая среда и блокер

Проверка 2026-10-26, пользователь dmitry, uid=1000:

```text
kernel: 6.18.54.1-microsoft-standard-WSL2
/proc/self/cgroup: 0::/non-systemd
mount: cgroup2 /sys/fs/cgroup, rw,nsdelegate
/sys/fs/cgroup/non-systemd: root:root, 0755
cgroup.procs writable=False
cgroup.freeze writable=False
cgroup.kill writable=False
create_child=PermissionError errno=13
```

Проба выполняла только создание нового пустого уникально названного дочернего
каталога. Не писала в cgroup.procs/kill текущей группы, не перемещала процессы,
не вызывала sudo и не меняла системную конфигурацию. Файловая система cgroup v2
есть, но пользователю не делегирована. Наличие WSL2, mount rw или nsdelegate
не предоставляет права управления поддеревом.

Независимо от прав **backend ещё не написан**. Изменение chmod или Delegate=yes
само по себе не включит поддержку в этой ревизии. Нельзя объявлять реальную
приёмку пройденной на обычных директориях, mocked cgroup или сканировании /proc.
Нужны реализация ниже и отдельная привилегированно подготовленная тестовая среда.

## Исследованная модель запуска

- CodexExecutor вызывает Lifecycle.submit; Python _child launch gate задерживает
  exec до registry/SQLite callback. Registry сохраняет pid и start identity.
- GitWorkspaceManager.run запускает task Git/gh helpers через тот же Lifecycle,
  связывает их с attempt через attempt_processes. Credential/SSH/Git descendants
  наследуют группу, пока сами её не меняют. Attachment HTTP выполняется в daemon;
  отдельный gh auth token helper не имеет task worktree в cwd.
- StopWatcher читает метки отдельным Store/Adapter. live_processes выбирает живые
  исходные PGID, signal адресует PID лидера. После setsid/setpgid такой выбор
  неполон; durable pid/start identity не восстанавливает потерянное множество.
- Lifecycle._active/recover и cleanup опирались на исходные группы и лидеров.
  Лидер может уйти, потомок переподчиниться и жить в другом PGID. Заявленный
  process_status=confirmed тогда не доказывает отсутствие пользователей worktree.
- CleanupService имеет собственный flock, SQLite generation guards и ownership
  preflight; Git cleanup helper наследует flock FD. Эти проверки не заменяют
  контроль потомков. Пригодный для Git worktree ownership не означает, что его
  никто больше не использует.

## Что изменено сейчас

Это защитная блокировка до реализации backend, а не подмена задачи PGID fallback.

- `containment.py`: read-only capabilities и безусловный fail-closed
  require_control с descendant_boundary_unverified. Даже writable cgroup не
  принимается за сертификат. Это временный предохранитель, не конечный API backend.
- `scheduler.py`: admission отказывается до reserve, подготовки и запуска;
  причина сохраняется в dispatcher.jsonl. Polling/диагностика службы могут работать.
- `codex/__init__.py`, `workspace.py`: дополнительные запреты фактического запуска
  Codex/task helpers и последней точки удаления worktree, включая direct callers.
- `detachment.py`: request/finalize/signal не подтверждают legacy attempts по PGID;
  stop_requests получает unconfirmed/error, finalized_at не устанавливается.
  Старое PGID-only confirmation признаётся непроверяемым, а не сертификатом Б.
  Fence запрещает возобновление commit/push/PR. Неизвестная граница не заменяется
  сигналом исходной группе, выданным за полную emergency остановку.
- `storage/__init__.py`: старый finalized PGID stop не разрешает reattach новой
  итерации без доказательства; отказ durable. Имеющиеся CAS/guards/history сохранены.
- `cleanup.py`, `workspace.py`: ownership checks сохранены, отсутствие полной
  границы запрещает удаление. cleanup_jobs хранит last_error, pending/retry;
  отказ не исчерпывается обычным лимитом четырёх retries, в том числе для done/
  cancelled/review_on_error. Пользовательские файлы и remote refs не удаляются.
- `doctor.py`: FAIL сообщает отсутствие backend и результат проверки делегации.
- README, example config, ТЗ, implementation report и этот документ явно отражают
  блокер. Небезопасного configuration override/legacy-mode нет.

**Миграции не добавлялись**, схема остаётся 609. stop_requests, stop_outbox и
cleanup_jobs уже содержат необходимые для отказа поля; историю не переписываем.
Старые task/attempt outcomes не превращаются в ложное доказательство отсутствия
escaped descendants. Прежде удалённые старой версией данные восстановить нельзя.

Стандартный Lifecycle stop/stop --force для известных групп не менялся. Это
не гарантия Б для escaped descendants. Полная совместная работа attempts под
новой границей, ordinary drain и emergency всей границы пока не реализованы;
новые задания глобально не допускаются. Нельзя развёртывать эту ревизию как
готовую исполняющую службу варианта Б.

## Как снять блокер и варианты реализации

План ниже — необходимые дальнейшие работы, а не уже работающие настройки проекта.

1. Подготовить Ubuntu/WSL2 с systemd, проверить версии и реальные операции в
   выделенном поддереве. Microsoft описывает включение systemd через wsl.conf и
   перезапуск WSL; такие системные изменения здесь не выполнялись.
   [WSL/systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd).
2. Предпочтительный вариант: небольшой привилегированный broker/systemd manager
   создаёт отдельную unit/cgroup для каждой attempt, рабочий процесс имеет другой
   UID и не может мигрировать себя/соседей или изменять файлы управления границей.
   Рассмотреть transient service, ExitType=cgroup и KillMode=control-group с
   feature probes целевой версии. Сам Dispatcher/Codex не следует просто запускать
   root ради прав на cgroup.
   [Service lifecycle](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html),
   [KillMode](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html).
3. Альтернатива: корректно делегированный subtree для доверенного control-plane,
   отдельные права/изоляция payload, API broker для запуска и сигналов.
   Delegate=yes — только часть настройки, не конечное доказательство. Payload
   не должен получать writable cgroup ancestors/соседей. Нужно отдельно проверить
   права миграции, включая доступ через /proc/mount namespaces и inherited FDs.
   [Delegation](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).
4. До exec, пока launch gate закрыт, создать/проверить границу и записать durable
   attempt/iteration ownership и идентичность, устойчивую к restart/reuse. Включить
   в неё Codex, Git/gh и все task helpers. Одна attempt boundary должна охватывать
   все её стадии. Обнаруженный старый PID-only attempt ретроспективно недоказуем.
5. При Detach запретить новый admission в границу, ждать её полного опустошения без
   сигналов. При emergency адресно TERM/grace, затем cgroup.kill для всего subtree;
   учесть races fork/migration. Kernel cgroup.events populated отражает живых
   процессов включая descendants, а cgroup.kill обрабатывает concurrent forks.
   Нужна защита от повторного заполнения между проверкой empty и cleanup.
   [Kernel interfaces](https://docs.kernel.org/admin-guide/cgroup-v2.html).
6. При restart проверить исходную identity/поколение/boot, ownership и состояние
   границы; исчезновение/recreation каталога или unit не считать empty без durable
   доказательства. После отказа API/прав сохранить данные и diagnostic, повторять
   восстановление. Не пересоздавать старую boundary ради ложного empty.
7. Только после этого заменить временный предохранитель реальным per-attempt
   verifier, расширить SQLite lifecycle и провести kernel integration acceptance.
   Другой runtime допустим лишь с доказательством эквивалентных launch, lifetime,
   stop и recovery гарантий; PPID/cwd/PGID scanners таким вариантом не являются.

## Проверки и честная граница результатов

`test_stage60417_blocker.py` теперь выполняет 8 реальных сценариев: setsid/setpgid ×
ordinary/emergency intent × естественный выход/отмена лидера. Harness — subreaper;
после reparent escaped child жив с cwd в настоящем Git-registered worktree, а
исходный PGID уже пуст. Проверяются valid ownership, сохранение user-file,
неизменность соседнего процесса, direct cleanup refusal, durable retry и restart.
Harness затем явно убивает/reap-ит только принадлежащих тесту детей.
Это доказывает безопасный отказ, **не успешную full-boundary остановку**.

`test_variant_b.py` проверяет отказ admission/exec/publication, невозможность
считать writable cgroup сертификатом, durable diagnostic outbox и invalidation
старого PGID confirmation при reattach. Восемь kernel acceptance cases явно
SKIPPED с причиной отсутствия backend/делегации; успешными они не считаются.

Legacy бизнес-регрессия `env`/`publication` использует явно именованный
legacy_boundary_contract из tests/conftest.py. Mock существует только в pytest,
не передаётся daemon subprocess и не является конфигурационной лазейкой. Он
позволяет продолжать проверять ownership/CAS/outbox/PR логику, не выдавая её за
ядровое доказательство Б. Реальный CLI daemon test без mock теперь проверяет
refused admission, отсутствие Codex/PR/comment и штатный stop.

Финальный прогон после всех изменений:

```text
PATH=/tmp/codexagent-60417-venv/bin:$PATH bash scripts/test.sh
655 passed, 8 skipped, 30 subtests passed in 93.67s (0:01:33)
```

Exit code 0; compileall src/tests также прошёл. `git diff --check` успешен.
14 новых/расширенных реальных проверок отказа (8 escaped-сочетаний и 6 проверок
gate/diagnostics) прошли. Восемь skipped — незавершённые kernel acceptance,
не эквивалент успешно пройденной приёмке Б. Legacy regression с mock-контрактом
также не доказывает наличие strong backend в production.

| Критерий Б | Текущее состояние |
| --- | --- |
| Граница всех потомков до exec | Backend не реализован; новые attempts отказываются запускаться |
| Empty proof перед cleanup | Доказательства нет; удаление запрещено, данные сохранены |
| Ordinary drain всех потомков | Не завершает неизвестную границу, не посылает ей сигналы |
| Emergency TERM/grace/KILL всей границы | Не реализован; PGID-сигнал не выдаётся за эту гарантию |
| Durable boundary identity/recovery | Identity backend отсутствует; durable refusal сохраняется после restart |
| Соседние attempts и stop/force | Отказ не сигналит соседей; прежние PGID stop tests сохранены, полная изоляция Б не принята |

## Ручная проверка безопасного отказа

1. Выполнить doctor: ожидать FAIL о backend/delegation, без изменения системы.
2. Запустить тест `python -m pytest -q tests/test_stage60417_blocker.py` в venv.
   Проверить 8 success отказного пути. Настоящие рабочие Issue/PR не нужны.
3. В выделенном legacy workspace запросить Detach/emergency, проверить show:
   unconfirmed, descendant_boundary_unverified, отсутствует finalized_at.
   Не считать существующий пустой PGID достаточным для ручного удаления.
4. Повторить cleanup/restart: worktree и пользовательские файлы должны остаться,
   cleanup_jobs pending/error/retry, без нового Codex и новой итерации.
5. Проверить доставку diagnostic при недоступном GitHub и после восстановления:
   durable outbox сохраняется, не снимая запрет.
6. После настройки среды и реализации backend выполнить НЕ пропущенную матрицу:
   setsid/setpgid/reparent/leader exit, no-signal ordinary drain, TERM-ignoring
   emergency/KILL, restart на каждой границе, две attempts, отозванные права,
   заменённая/missing boundary и запрет cleanup до доказанного sealed empty.
