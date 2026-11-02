# Дополнение к 6.4.3.4 — смена целевой ветки дочернего PR

**Решение пользователя:** смена целевой ветки (base) PR вручную — осознанное действие; она не препятствует штатному завершению дочерней Issue. Если ребёнок остаётся в Sub-issues контейнера, а его PR слит в другую ветку, контейнер блокируется до явного исключения ребёнка из Sub-issues.

## Контрольный пример
Playground Issue #114 BadKid, PR #115. После ручной смены base и merge Issue остаётся в review, а publication operation `resolution:5120612290` имеет `phase=retry`, `outcome=failed`. В `src/dispatcher/pr_monitor.py` жёсткая проверка `pr['base']['ref'] != linked['base']` приводит к `invalid_response` до перехода в `done`.

## Утверждённая семантика
1. Канонические PR ID/number, repository identity, head ref/SHA, iteration, merge evidence и другие защитные preconditions остаются обязательными.
2. Изменение только целевого `base` открытого PR, включая последующее слияние, не является основанием бесконечно оставлять обычную Issue в `review`. При подтверждённом merge дочерняя Issue должна перейти `review → done`; без merge не объявлять `done`.
3. Хранить отдельно первоначально ожидаемую `base` и фактическую целевую ветку слитого PR для диагностики и проверок контейнера. Не переписывать ожидаемую ветку задним числом.
4. Если фактическая `base` слитого канонического PR отличается от рабочей ветки родителя, пока ребёнок участвует в прямых Sub-issues контейнера, `ready_for_integration=0`, blocker `child_merge_wrong_base`. Запретить автоматический retarget, повторный merge, перенос commit или silently ignore.
5. **Единственный утверждённый путь снять именно этот конфликт — пользователь явно удаляет ребёнка из списка Sub-issues родителя в GitHub.** Удалённый ребёнок перестаёт участвовать в readiness; его уже выполненная Issue остаётся `done`. Не блокировать родителя за `child_relation_removed` для такого случая. Различать разрешённое осознанное исключение и опасное исчезновение/подмену relation; сохранить audit trail и обеспечить idempotence/restart.
6. Не переносить это исключение автоматически на иные типы проблем: например, снятие `codex-ready` при открытом PR остаётся блокирующим по прежнему решению.
7. Диагностика должна явно сообщать фактическую base, ожидаемую parent branch и инструкцию исключить ребёнка из Sub-issues. Fail-closed при подмене PR/head/repository, отсутствующем merge proof или неоднозначной истории.
8. Регрессионные тесты: обычная Issue merged into expected base => done; PR base вручную изменён и merged => done; тот же PR в Sub-issues => container ready=0 wrong_base; explicit remove Sub-issue => blocker cleared and readiness recalculated; restart/idempotence; missing/changed identity => no false done; ещё не merged PR => review; неготовые другие дети сохраняют readiness=0.

## Выполнение и приёмка
Изменить `src/dispatcher/pr_monitor.py`, `src/dispatcher/container_monitor.py` и при необходимости модель доказательств/миграцию. Сначала изучить существующие guard/evidence инварианты, не ослаблять безопасность. Прогнать все тесты, затем ручная проверка #114/#115 и контейнера. Не делать PR и не закрывать этап без решения пользователя.
