# Этап 6.4.1.5 — повторное утверждение и доставка PR resolution

Рабочая ветка: `STAGE_06_04_01_05`. Миграций БД и изменений публичного CLI нет.

## Доказанная причина

Дефект воспроизведён на временной SQLite, PR monitor fixture и настоящем
`Adapter.reconcile`, подключённом к GitHub stub. Codex и настоящий GitHub не запускаются.

Последовательность:

1. `SyncService.poll` читает Issue, вызывает `Store.apply`, затем `queue.commands`.
   На этом снимке approve ещё отсутствует.
2. `PullRequestMonitor.resolve` проверяет PR identity и серверные времена,
   вызывает `Store.transition(review → rejected)`. Решение уже зафиксировано;
   `evidence.observed_at` в тесте — `00:20:00Z`.
3. Доставка resolution comment выполняется после транзакции и может задержаться.
   Другой reconcile уже может показать `dispatcher:rejected` пользователю.
4. Пользователь добавляет approve, REST labeled event `id=7`, `00:21:00Z`.
   Событие строго новее rejected observation и command guard (`00:00:00Z`).
5. После POST комментария монитор вызывает reconcile; ещё один reconcile вызывается
   в конце sync. Старый adapter удалял любую managed-метку, отличную от состояния
   SQLite. Поэтому он удалял approve, хотя её event ещё не проверялся очередью.
6. Следующий fetch не содержит approve: `commands` не вызывает `_reason` и
   `Store.decide` для потерянной команды. Перехода, decision receipt и причины
   отказа для неё вообще нет. Это потеря входного события при доставке состояния,
   а не отказ временной проверки SQLite.

Контрольный запуск нового теста с исходным reconcile (только добавлен совместимый
неиспользуемый аргумент store): `during_comment` падает — фактические метки
`['ready', 'dispatcher:rejected']`, approve отсутствует; `after_comment` и
`failed_comment` проходят. После исправления проходят все три варианта.
CLI работает, поскольку прямо вызывает `Store.decide` после свежего fetch,
не ожидая обнаружения labeled event следующим poll.

## Исправление

`Adapter.reconcile` получает Store от всех runtime callers. Перед удалением
approve/reject (и cancelled как команды в rejected) он проверяет последнее
REST labeled/unlabeled event и наличие SQLite decision receipt именно для его
repository, Issue и event id. Событие без квитанции остаётся следующему poll.
Новое событие с тем же именем метки не считается обработанным по старой квитанции.
Без Store adapter сохраняет команды.

Для неизвестного/unprovable события действует прежний durable quarantine:
метка удаляется по политике A, причина и инструкция повторной подачи остаются
в очереди/логах. Восстановление API не превращает такой approve в согласие;
нужно наблюдение отсутствия метки и новая подача либо CLI. При ошибке повторной
проверки истории reconcile откладывается, запуск не разрешается.

`queue._reason`, strict `event.created_at > guard.server_at` и
`event.created_at > rejected.evidence.observed_at`, allowlist/human check,
proof identity/content/readiness, observation token/version/state CAS,
transactional iteration/history/receipt, quarantine и PR transition contract
не изменялись. Ослабления `<=` до `<` нет. Новое approve создаёт одну итерацию
в существующей транзакции `Store.decide`; delivery старого комментария использует
свежее состояние SQLite и не возвращает задачу в rejected.

## Файлы

- `src/dispatcher/github/__init__.py`: проверка обработки command event до удаления метки.
- `github/sync.py`, `pr_monitor.py`, `pr_publication.py`, `queue.py`, `scheduler.py`
  внутри `src/dispatcher`: передача Store всем вызовам reconcile.
- `tests/test_stage60415.py`: 16 регрессионных/негативных сценариев.
- `tests/test_stage03.py`: reconcile без Store сохраняет необработанные команды.
- `tests/test_github.py`: старый stage-2 stub принимает новый keyword argument.
- `README.md` и этот отчёт: фактическое поведение и причина.

## Проверки

Новые тесты проверяют approve во время/после комментария, неудачную и позднюю
доставку, повторные poll, одну новую iteration и одну applied GitHub decision,
полный цикл review → rejected → pending → running → review → done,
CLI, запрет approve из running/done/cancelled, stale/equal-time event,
неверного actor/version, отсутствие proof, API outage/quarantine,
новое событие между command snapshot и reconcile, а также полный SyncService
порядок с первым успешным approve и отсутствием ложного карантина.
Существующие тесты дополнительно покрывают версии, CAS, identity, доступность,
CLI reject/cancel и идемпотентную доставку после потери ответа POST.

Результат: `PATH=/tmp/codexagent-60415/bin:$PATH bash scripts/test.sh` —
**577 passed, 30 subtests passed**, 40.03 s; скрипт также успешно выполнил
`compileall -q src tests`. Дополнительно успешно выполнены
`codex-dispatcher --help` и `git diff --check`. Окружение: отдельный venv в
`/tmp/codexagent-60415`, editable install `.[test]`, Python 3.14.
Первый общий прогон обнаружил 32 падения только из-за stage-2 lambda stub,
не принимавшего keyword `store`; после обновления stub повторный общий прогон
успешен. Новые 16 сценариев также прошли отдельным запуском.

## Ограничения

Ручной прогон в настоящем GitHub не выполнялся. Доступен Python 3.14;
Python 3.12 из CI matrix в окружении отсутствует. Дополнительная проверка
истории при reconcile требует GitHub API и увеличивает число запросов.
GitHub DELETE label не предоставляет CAS по event id: остаётся внешнее окно
между финальным чтением истории и DELETE при одновременном remove/reapply
той же метки. Изменение этой гарантии требует отдельного протокола команд;
исправленная гонка комментария и события до reconcile покрыта тестами.
События с равным серверным timestamp по-прежнему отклоняются как неоднозначные;
получение комментария само по себе не является доказательством свежести.
