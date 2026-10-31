# Отчёт агента 6.4.1.9 — D2/D3

Дата: 2026-10-31. Ветка: `STAGE_06_04_01_09`.
Основание: [дополнительное задание](stage-06-04-01-09-d2-d3-agent-task.md).
Этап **«В работе»**, финальная приёмка не оформляется. Live GitHub/Codex операции,
создание PR, merge и закрытие Issue не выполнялись. Публикация этой рабочей ветки
разрешена пользователем отдельно.

## Реализация D2

Новые запуски блокируются прежними stop/generation fences. Running Codex и helpers
завершаются естественно; неопределённая идентичность/exit блокирует очистку.
`scheduler.collect` и `detachment.finalize` сохраняют natural exit и detached;
обязательная публикация выполняется cleanup-job до удаления файлов. Это отдельный
путь без создания ветки/PR, повторного Codex и force push.

`detach_publication.publish` проверяет owned worktree/clone/origin, каноническую
ветку, поколение/stop request и историю владения. Dirty diff сохраняется коммитом,
затем выполняются обычный push и точное подтверждение SHA через ls-remote.
При отсутствии diff подтверждается текущий SHA. Additive migration 612 добавляет
`detach_publications` с состоянием, SHA/remote SHA, no_diff, ошибкой и временем
подтверждения. Старые данные и detach_refs сохраняются.

Cleanup-job и журнал обеспечивают retry после poll/restart. При ошибке commit,
push или проверки worktree остаётся на месте. Задержки — 60/300/900 секунд, далее
900 секунд без лимита для detached. SQLite writer не удерживается во время Git I/O.
Per-Issue flock и running reservation исключают одновременную очистку и смену
поколения; существующие защитные триггеры не ослаблены. После завершения cleanup
подтверждённые SHA переносятся в attempts в той же транзакции, что завершает job.
Принудительное удаление detached worktrees убрано; оставшиеся dirty/ignored и
чужие файлы блокируют очистку.

Reattach требует свежего server snapshot, нового approve и завершённого cleanup.
Прежняя каноническая ветка должна совпасть с journal/local/remote SHA. Подмена,
отсутствующий ref, активный worktree или legacy без подтверждения блокируют путь.
Существующий PR проверяется до Codex и повторно при публикации; тот же PR
сохраняется, неоднозначность/merged/missing не создают замену. Автоматического
закрытия PR при Detach нет.

## Реализация D3

`StopController.diagnostics` включает ready_removed и emergency_stop с разными
заголовками. Ordinary комментарий содержит UTC-время, actor снятия ready только
из полного GitHub timeline до наблюдения stop либо «Не удалось определить»,
branch/SHA, pending/confirmed публикации и cleanup. Actor сохраняется в evidence;
текст Issue не является доказательством. Outbox с прежним уникальным marker
восстанавливает потерянный ответ и редактирует прежний комментарий после retry.
Повторный poll/дубликаты timeline не создают новый stop/comment. CLI show
показывает `detach_publications` в текстовом и JSON представлении.

## Проверки

Сначала добавлены воспроизводящие проверки: исходный код удалял worktree при
ошибке push и не создавал ordinary комментарий (2 failed). Затем исправлен код.
Старые утверждения no publication/dirty cleanup/fresh base заменены требованиями
публикации и сохранения SHA; проверки foreign refs/ownership/generation/legacy
сохранены и дополнены отсутствующим remote/ref и PR identity.

Целевые файлы: `test_detach_publication.py`, `test_detach_repeat.py`,
`test_detachment.py`; общая регрессия включает terminal states, многопроектность,
stale events, stop/restart, process tracking и миграции. Новые сценарии: idle dirty
с открытым PR, running natural exit, commit/push/verify/lost-push response,
retry/restart без Codex, clean no_diff, опубликованный Reattach, actor fallback,
duplicate timeline/poll и outbox lost response/progress update.

Полный прогон выявил гонку `/proc`: процесс мог исчезнуть между open и read
stat с ESRCH (ProcessLookupError). Добавлен воспроизводящий тест: пропавшая запись
не мешает обнаружить другого живого участника группы; до исправления он падал.
В group_members, восстановлении и Lifecycle._active ESRCH обрабатывается как
исчезнувшая запись, аналогично ENOENT; остальные ошибки не маскируются.
Ожидания JSON-схемы show дополнены новым полем в CLI/rendering тестах.

Окружение: существующий `.venv`, Python 3.14.4; системная команда python отсутствует.
Проверки запускаются `.venv/bin/python -m pytest -q` и `git diff --check`.
Целевой прогон Detach/Reattach/tracking/CLI: 48 passed (27.96 s).
После исправления ESRCH и ожидания JSON: 2 passed (0.29 s).
Итог полного прогона: **719 passed, 30 subtests passed**, 0 failed, 0 skipped;
158.92 s. `git diff --check` — exit 0.

GitHub/Codex в тестах — stub/mock, Git/SQLite/subprocess реальные локальные.
[Матрица](stage-06-04-01-09-traceability.md) и
[ручной чек-лист](stage-06-04-01-09-manual-checklist.md) актуализированы;
ручные результаты остаются незаполненными. Исторические акты приёмки не изменены.

## Согласованные исключения и ограничения

**D1** (генерация/выбор ветки, Unicode/длина заголовка) и **D4** (независимые
explicit/default источники base/branch) — **согласованные исключения из приёмки
6.4.1.9 и задачи следующего этапа**. Они не реализованы и не выданы за выполненные.

**L1**: строго старые server timestamps отклоняются, порядок противоречивых снимков
с равным временем внутри секунды остаётся неоднозначным. Legacy без server time
не доказывает Reattach. Ограничение не менялось.

Независимый аудит и ручная приёмка M1–M9 остаются за пользователем. При ошибках
доступа/refs сохранять БД и worktree, исправлять причину и ждать штатный retry;
не применять force/reset и не переназначать чужие refs.
