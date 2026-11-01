# 6.4.3.3 — отчёт реализации

Статус: реализация с явно диагностируемым ограничением GitHub body CAS;
**подэтап не принят**, требуется решение пользователя, CI и ручная приёмка.
Ветка: `STAGE_06_04_03`. Автоматическое объединение PR не выполняется.

## Результат и границы

Добавлены строгие `issue_type` и `run_codex`, состояние `awaiting_children`,
добавочная миграция 614, журнал ранней публикации канонического ref, API
непосредственных детей и отдельная маршрутизация контейнера в scheduler.
Контейнер не резервирует попытку Codex, не создаёт PR и не меняет lifecycle
детей. Обычная Issue с `run_codex: false` не запускается: её исполнение остаётся
за 6.4.3.5. Таблицы интеграционных поколений не создаются — их использование
не требуется для начальной подготовки.

**Ограничение автоматизации:** ребёнок, которому требуется изменение body,
получает `child_body_cas_unavailable`. Сохраняется устойчивое намерение с
хешами исходного и предлагаемого body; PATCH Issue не отправляется.
`with_base_branch` строит идемпотентное предложение с сохранением остальных
YAML/Markdown, но документированного атомарного способа безопасно записать
его в GitHub не установлено. Поэтому автоматический happy path с двумя
изначально не настроенными детьми пока недоступен. Контейнер без детей либо
с уже корректно настроенными детьми может перейти в `awaiting_children`.
Это ограничение нельзя считать пройденной полной ручной приёмкой исходного ТЗ.

## Изменённые файлы

- `src/dispatcher/storage/migrations.py`: `upgrade614`, поля задач, таблицы
  отношений, операций и блокировок, индексы, проверки целостности перед marker.
  Исторический `VERSION=602` и старые миграции не изменены.
- `src/dispatcher/storage/__init__.py`: вызов миграции; синхронизация типа и
  режима только из принятого snapshot; закреплённая ветка заморожена в
  `awaiting_children`.
- `src/dispatcher/storage/transitions.py`: охраняемый переход
  `pending → awaiting_children` с подтверждёнными операциями подготовки/ref,
  проверкой версии, итерации, доступности, ownership и GitHub evidence.
- `src/dispatcher/issue_parameters.py`: строгий разбор, stripping, построение
  правки одного `base_branch`; YAML `yes/off`, строки и числа вместо boolean
  отвергаются.
- `src/dispatcher/containers.py`: отдельный сервис, project lock, durable intents,
  сверка удалённого SHA, recovery, учёт детей и fail-closed диагностика.
- `src/dispatcher/github/__init__.py`: полная пагинация непосредственных детей,
  проверка ID/repository/структуры/дубликатов, сверка summary.total, повторное
  чтение полного набора.
- `src/dispatcher/scheduler.py`, `src/dispatcher/execution_store.py`: контейнер
  обходится без проверки квоты/резервирования/запуска Codex; прямое резервирование
  контейнера и обычной manual Issue запрещено.
- `src/dispatcher/config/__init__.py`: конфигурируемая метка
  `dispatcher:awaiting-children`. Существующие init/fingerprint/reconcile
  используют общий перечень автоматически; после обновления нужен `init`.
- `src/dispatcher/cli.py`, `src/dispatcher/rendering.py`: тип/режим/состояние,
  операции и блокировки в show; queue отображает новое состояние.
- `tests/test_stage6433.py`: тесты нового подэтапа.
- `tests/test_stage04.py`: ожидаемый перечень миграций дополнен 614.
- Этот отчёт.

## Проверенный контракт GitHub

Проверены официальные документы:

- [Sub-issues](https://docs.github.com/en/rest/issues/sub-issues):
  `GET /repos/{owner}/{repo}/issues/{number}/sub_issues`, `per_page` до 100,
  `page`; для fine-grained token — Issues read. Это непосредственные дети,
  включая закрытые Issue, а не рекурсивный обход.
- [Update an issue](https://docs.github.com/en/rest/issues/issues#update-an-issue)
  и [conditional requests](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate):
  PATCH body не имеет документированного expected-body/version precondition.
  Кэширование GET через ETag не доказывает CAS для PATCH. Запись body потребовала
  бы Issues write; не выполнялась и абсолютная защита гонки не заявляется.
- [Create a reference](https://docs.github.com/en/rest/git/refs#create-a-reference):
  `POST /repos/{owner}/{repo}/git/refs`, ref и SHA существующего commit;
  fine-grained Contents write, в некоторых случаях также Workflows write.
  Используется создание ref, а не update ref. Это устраняет возможность
  продвинуть чужую ветку обычным fast-forward push после проверки отсутствия.
  После запроса выполняется независимый `git ls-remote`; 409/422 блокируют
  ownership. Не используются force push, synthetic commit, удаление refs.

Фактические read-only запросы 2026-11-01:

```bash
gh api repos/heroicrealm/CodexAgentPlayground --jq '{id,permissions}'
gh api repos/heroicrealm/CodexAgentPlayground/issues/72/sub_issues --jq 'map({id,number,repository_url})'
gh api repos/heroicrealm/CodexAgentPlayground/issues/73/parent --jq '{id,number,repository_url}'
gh api 'repos/heroicrealm/CodexAgentPlayground/issues/72/sub_issues?per_page=2&page=2' --jq 'map({id,number,state})'
```

Repository ID: `1431921387`; у текущей учётной записи подтверждены pull/push и
admin. Получены дети #73, #74, #75, обратный parent #73 — #72; вторая страница
с размером 2 вернула #75. Эти результаты не доказывают полномочия произвольного
production token и поведение условной записи. Remote mutation для эксперимента
CAS не выполнялась, пользовательские Issue не переписывались.

## Идемпотентность и восстановление

1. `BEGIN IMMEDIATE` сохраняет intent с ключом
   `container:<GitHub issue ID>:<iteration ID>:<kind>` до внешнего эффекта.
2. Сначала фиксируются branch/base; после fetch и проверки отсутствия коллизии
   сохраняются целевой SHA и хеш origin URL; только затем создаётся ref.
3. После сбоя между remote Git и SQLite новый процесс сравнивает origin и SHA
   с intent, подтверждает существующий ref, не создаёт второй ресурс.
4. Созданный ref перепроверяется через Git до чтения детей и перед финальным
   переходом. Потеря ответа API не повторяет успешное создание после обнаружения ref.
5. Project flock исключает одновременную подготовку двумя локальными poll;
   ОС освобождает lock после смерти процесса. SQLite CAS и общая branch ownership
   защищают локальное закрепление. Никаких HTTP-запросов под SQLite writer lock.
6. Полный набор детей читается повторно; ошибки страниц, смена состава,
   чужой repository и дубликаты не дают подтверждения подготовки. Старые связи
   сохраняются; исчезнувшая связь создаёт blocker. Снятие ready с управляемого
   ребёнка блокирует контейнер; автоматического unblock этого случая нет.
7. Повторная подготовка после `awaiting_children` ничего не публикует;
   служебные комментарии не создаются, диагностика доступна через show.

GitHub не предоставляет атомарного snapshot всего набора Sub-issues и общей
транзакции с SQLite. Повторное чтение выявляет наблюдаемую гонку, но не доказывает
отсутствие ABA между запросами или правки после последнего чтения. Удалённая
ветка с тем же SHA после неясного исхода запроса восстанавливается по локальному
intent; журнал GitHub actor для доказательства авторства ref не запрашивается.
Консервативные ownership/ready/relationship blockers автоматически не снимаются.

## Проверки

Итоговые команды и результаты:

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/test.sh
# 790 passed, 30 subtests passed in 142.22s; compileall: exit 0
PATH="$PWD/.venv/bin:$PATH" python3 -m pytest tests/test_stage6433.py -q
# 42 passed in 2.42s (после финального усиления updated_at freshness guard)
git diff --check
# exit 0, без замечаний
```

Всего **790 тестов и 30 subtests**, из них **42 новых теста** подэтапа.
Выборочный регрессионный прогон CLI/ownership/rendering/контейнеров до финального
расширения набора: 106 passed. Первый полный прогон выявил 4 ошибки;
исправлены лишние секции JSON обычных Issue, UPDATE неизменившейся metadata,
задевавший ownership trigger, и ожидаемый список миграций. Повторный полный
прогон прошёл. Исторические миграции не переписывались.

Используется существующий `.venv`: системный `/usr/bin/python3` не содержит
pytest. Исходный запуск `bash scripts/test.sh` без активации завершился сообщением
`No module named pytest`; это ограничение окружения, а не прошедшая проверка.

Новый набор покрывает строгий YAML, предложения правок, новые и старые схемы,
rollback/reopen, реальный Git без новых commits/PR, crash после публикации,
потерю ответа API, повторный poll, блокировку второго poll, маршрутизацию
scheduler, 0/1/101 детей, дубликаты/чужой repo/ошибку/смену набора, не-ready
неуправляемого ребёнка, активного ребёнка с другой базой, устаревший parent,
отсутствующую вложенную базу и запрет запуска manual/container через reserve.

Не исполнены: полный production CI; ручная end-to-end подготовка контейнера с
автозаписью body двух детей (заблокирована отсутствием CAS); реальный mutating
эксперимент conditional PATCH; production 100+ Sub-issues/закрытые дети;
kill -9 daemon во время remote запроса; параллельные daemon на разных машинах;
merge/integration/manual lifecycle и повторная интеграция следующих подэтапов.

## Ручная проверка

1. В Playground выполнить штатный `init`, проверить новую метку; выбрать новый
   контейнер и его ветку, отдельно добавить ready нужным детям и подтвердить
   только контейнер обычным способом.
2. Сначала оставить у ready детей неправильный/отсутствующий base: после poll
   проверить опубликованный ref контейнера, отсутствие новых commits/PR,
   сохранённый body детей, `child_body_cas_unavailable` в show и `pending`.
3. Для положительного пути использовать отдельный контейнер с заранее заданным
   `dispatcher.base_branch` у двух ready детей и одним никогда не управляемым
   non-ready. После approve контейнера проверить `awaiting_children`, отсутствие
   Codex/PR и отсутствие approve/переходов детей. Повторить poll и restart.
4. Проверить вложенный контейнер с существующей подготовленной parent branch;
   отсутствие parent branch должно блокировать публикацию вложенного ref.
5. Проверить child с уже закреплённой несовместимой базой: blocker без rebase,
   retarget и редактирования body. Снятие ready/удаление связи управляемого ребёнка
   не должно автоматически снимать блокировку или удалять его ресурсы.
6. Полную исходную приёмку с автоматической YAML-записью проводить только после
   решения по допустимому безопасному контракту записи body. Не заменять CAS
   безусловным PATCH и не объявлять этот сценарий пройденным.
