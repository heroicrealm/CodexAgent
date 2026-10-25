# Задание Codex — STAGE 6.4.1.4
Работать только в ветке STAGE_06_04_01_04 репозитория heroicrealm/CodexAgent. Перед началом fetch origin и проверить актуальность ветки. Не менять main и STAGE_06_04_01. Прочитать docs/stages/stage-06-04-01-04-spec.md — это утверждённое ТЗ.

1. Проанализировать workspace.py (GitWorkspaceManager.prepare и TaskWorkspaceCleanup), SQLite migrations/storage, scheduler, daemon, CLI и тесты. Составить краткую карту точек интеграции.
2. Реализовать durable cleanup jobs, миграцию SQLite, атомарную постановку после done/cancelled, reconciliation старых терминальных задач и архивов, background обработчик с recovery и retries 1/5/15 мин. Предотвратить гонки при новых итерациях и перезапуске.
3. Доработать существующий cleanup: все owned previous-* и attachments-* за несколько итераций, partial removal, безопасность пути и Git metadata; unknown/dirty не удалять. Сохранить repository clone, ветки, SQLite историю и PR.
4. Добавить диагностику show/log и ручной retry failed cleanup. Не изменять state Issue при ошибке удаления.
5. Написать автоматические тесты из ТЗ (включая crash/restart, многопроектность, legacy, concurrency), обновить README и документацию, выполнить scripts/test.sh и pytest.
6. Сделать commit и push в STAGE_06_04_01_04. PR не создавать до отдельной команды на приемку. В отчёте перечислить файлы, миграции, тесты, ограничения, известные риски и ручные сценарии.

При обнаружении потенциально разрушительного удаления, неразрешимого ownership или противоречия модели состояний — не обходить защиту, сохранить данные и сообщить о блокере.
