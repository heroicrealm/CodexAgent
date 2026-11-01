# 6.4.3.3 — отчёт реализации контролируемого обновления детей

Статус: вариант A реализован; **приёмка не оформлена**. Ветка `STAGE_06_04_03`.
PR не создавался и не сливался. Этот отчёт заменяет прежнее описание
proposal-only ограничения `child_body_cas_unavailable`.

## Поведение и изменённые файлы

- `src/dispatcher/containers.py`: отдельная операция обновления ready ребёнка
  под существующим project flock. Перед записью сверяются ID/repository/number,
  state, author, title, labels/ready/emergency, body/updated_at, непосредственная
  связь, версия/состояние/approve/доступность локальной задачи, branches/PR и
  поколение command guard. Проверяется Issue-specific `viewerCanUpdate`.
  После durable intent выполняются повторные GET и local guard, один PATCH и
  независимый read-back. Сеть выполняется вне SQLite writer transaction.
- `src/dispatcher/github/__init__.py`: проверка права текущего actor обновлять
  конкретную Issue через GraphQL `databaseId` и `viewerCanUpdate`, fail closed.
- `src/dispatcher/storage/__init__.py`: частичный sync одной Issue без отзыва
  остальных задач; ограниченный proof для подтверждённой служебной правки.
  Сохраняются версия/законный approve только при точном совпадении исходного
  body/hash/version, целевого body/hash и результата `with_base_branch`, а также
  неизменности остальных полей content hash. Самостоятельное approve не выдаётся.
  Пользовательские изменения по-прежнему инвалидируют approve. Poll, увидевший
  ровно target известного intent, сохраняет его в observed до независимого
  подтверждения; snapshot ещё не принимает. Изменение title/author/target branch
  не подпадает под эту отсрочку.
- `src/dispatcher/scheduler.py`, `src/dispatcher/execution_store.py`: исходный
  snapshot незавершённой служебной операции не допускает запуск ребёнка до
  подтверждённого sync; запрет проверяется и перед подготовкой, и при reserve.
- `tests/test_stage6433.py`: контролируемые записи, конфликты, permission,
  восстановление, сохранение approve, реальный Git/SQLite и остаточная гонка.
- Этот отчёт: актуальные гарантии, recovery, проверки и ручной сценарий.

`with_base_branch` меняет только целевое поле и сохраняет остальные YAML/Markdown;
при правильной базе запись не нужна. Legacy `codex-config` требует ручной
конвертации и блокируется. Proposal-only intents предыдущей реализации
обновляются только при совпадении исходного хеша и требуемой ветки.
Несовместимые активные branches/PR сохраняют `child_active_base_conflict`;
снятый ready блокирует контейнер (вариант A), retarget не выполняется.
Неуправляемые никогда не ready дети игнорируются. Комментарии не создаются;
blockers обновляются по устойчивому ключу ребёнка.

## Намерение и восстановление

Ключ операции: `container:<parent issue id>:<iteration id>:child:<child issue id>`.
До PATCH в `container_operations` сохраняются child ID, iteration, version,
command observation, локальное состояние, исходный remote snapshot, source/target
SHA-256 и ожидаемая base branch. Секреты/token в intent не записываются.

1. Каждый повтор начинает с чтения связи и актуальной Issue.
2. Если body точно равен target и identity/state/ready/parent подтверждены,
   intent становится `confirmed`; snapshot принимается через sync без второго PATCH.
3. Если остался source и совпали updated_at и локальные guards, допускается
   повторная контролируемая попытка с новой проверкой permission и GET перед PATCH.
4. Если изменены body/identity/state/ready/parent либо результат чтения неизвестен,
   операция блокируется. Успех PATCH-ответа сам по себе ничего не подтверждает.
5. При timeout сразу выполняется read-back. Crash после PATCH или после confirmed
   до sync восстанавливается тем же алгоритмом. Подтверждённый PATCH не повторяется.
6. Только подтверждение либо достоверное наблюдение уже правильной базы снимает
   прежний `child_body_cas_unavailable`. Неподтверждённый read-back диагностируется
   `child_body_unconfirmed`; обнаруженные конфликты требуют вмешательства оператора.

## Граница гарантии и альтернативы

Предпроверка + PATCH + post-verification **не являются атомарным CAS**.
Между последним GET и PATCH человек может изменить текст, который PATCH затрёт.
Даже точное совпадение target при read-back не доказывает отсутствия такой потери.
Это принятое пользователем остаточное ограничение варианта A. Детерминированный
тест воспроизводит именно потерю текста в этом окне и успешную post-verification;
он фиксирует границу гарантии, а не заявляет полную защиту.

Наблюдаемая гонка блокирует дальнейшую запись; недоказанную потерю текста
автоматически восстановить невозможно. Project lock действует между процессами
одного workspace, но не координирует разные машины или пользователей GitHub.

Официальные контракты:

- [Update an issue](https://docs.github.com/en/rest/issues/issues#update-an-issue):
  PATCH не содержит documented expected-body/version precondition.
- [Conditional requests](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate):
  conditional GET/ETag не доказывает поддержку `If-Match` при записи; он не используется.
- [GraphQL Issue и UpdateIssueInput](https://docs.github.com/en/graphql/reference/issues):
  `viewerCanUpdate` проверяет право actor. Альтернатива updateIssue также не даёт
  документированного expected body/version; переход на неё не устраняет окно гонки.

## Проверки

Финальные результаты:

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh
# 817 passed, 30 subtests passed in 136.86s; compileall: exit 0
.venv/bin/pytest -q tests/test_stage6433.py
# 69 passed in 4.68s
git diff --check
# exit 0
```

Полный набор прошёл на окончательной версии кода. Тесты используют реальные
локальные Git и SQLite, подменяя GitHub API; внешние Issue не изменялись.
Покрыты source retry после ложного успеха, timeout после фактической записи,
crash/reopen с poll перед recovery, отказ read-back, чужая правка после crash,
сохранение approve и его инвалидация пользовательской правкой, исторический
intent, старые proposal-only операции, null body, permission, project flock,
несовместимая активная ветка, две ready Issue и одна никогда не ready,
остаточное окно GET→PATCH. Существующие регрессионные тесты также прошли.

## Короткая ручная проверка heroicrealm/CodexAgentPlayground

1. Выполнить штатный `init` проекта. Создать контейнер с уникальной branch,
   двумя непосредственными детьми без base_branch и третьим без ready.
   Сохранить исходные body. Выдать ready двум детям и контейнеру; approve — контейнеру.
2. Выполнить poll. Сравнить body: у двух ready детей добавлен только
   `dispatcher.base_branch`, равный branch контейнера. Третий ребёнок неизменён.
   Проверить `confirmed` операции, `awaiting_children`, отсутствие Codex/PR.
3. Повторить poll и restart: body/версии не меняются, новых PATCH нет.
   Отдельно повторить с уже законно approved ребёнком: approve сохраняется.
   Затем изменить пользовательский текст: обычная инвалидация approve сохраняется.
4. В отдельном сценарии снять ready с ребёнка с открытым PR либо задать
   несовместимую активную base: контейнер блокируется, branches/PR не меняются.
5. В тестовом окружении прервать процесс после PATCH до подтверждения, затем
   restart/poll: проверить read-back и отсутствие повторной записи target.
   Для конфликта изменить body до recovery: требуется blocker без перезаписи.

Не исполнены вручную: мутации в Playground, реальные permission-denied и timeout,
kill -9 в сетевом запросе, несколько daemon на разных машинах, 100+ реальных
Sub-issues, эксперимент с conditional PATCH и production CI. Автоматические
тесты этих границ не заменяют ручную приёмку. Merge/integration/cascade lifecycle
следующих подэтапов не реализуются этим заданием.
