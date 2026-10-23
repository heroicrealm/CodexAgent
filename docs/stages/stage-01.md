# Этап 1. Инфраструктура и конфигурация

Статус: утверждено 2026-10-23.
Репозиторий диспетчера: heroicrealm/CodexAgent. Рабочая ветка: STAGE_01 (от main).

## Цель
Создать универсальную локальную основу Codex Task Dispatcher для последующей обработки GitHub Issues, запуска Codex CLI и публикации результатов. Ни один целевой репозиторий или имя проекта не должны быть зашиты в код.

## Платформа и стек
Python 3.12+, пакет с CLI entry point `codex-dispatcher`; Ubuntu, в том числе WSL2, Bash, Linux filesystem. На первом этапе Docker не требуется. Интеграции через установленные `git`, `gh`, `codex`. Секреты и токены не хранить в YAML, репозитории или логах; использовать существующую авторизацию `gh`.

## Структура
`config/config.example.yaml`, `src/dispatcher/{config,github,codex,runtime,logging}/`, `tests/`, `docs/stages/`, `scripts/`, `pyproject.toml`, `.gitignore`, `README.md`. Допускаются обоснованные изменения структуры без нарушения требований.

## Конфигурация
YAML с секциями dispatcher (poll_interval_seconds, max_parallel_tasks, shutdown_policy), paths (workspace, logs), github (auth_method: gh), codex (executable), projects (массив id, enabled, repository, base_branch, task_label). Валидация типов, обязательных полей, уникальности id, корректности repo owner/name, положительных интервалов и ограничений параллелизма. Разрешить относительные пути и ~ с предсказуемым разрешением, описать правила в README. Поддержать явный путь к конфигу, безопасные значения по умолчанию, понятные сообщения ошибок. Пример конфигурации не должен ссылаться на реальный пользовательский целевой проект.

Пример:
```yaml
dispatcher:
  poll_interval_seconds: 30
  max_parallel_tasks: 1
  shutdown_policy: wait
paths:
  workspace: ~/codex-dispatcher/workspace
  logs: ~/codex-dispatcher/logs
github:
  auth_method: gh
codex:
  executable: codex
projects:
  - id: example-project
    enabled: true
    repository: owner/repository
    base_branch: main
    task_label: codex-task
```

## CLI и жизненный цикл
Реализовать `codex-dispatcher start`, `stop`, `stop --force`, `status`, `doctor`. `start` запускает фоновый диспетчер; повторный запуск не создаёт дубль. `status` различает running/stopped и не доверяет слепо устаревшему PID. `stop` прекращает приём новых задач и дожидается активных; `stop --force` отменяет активные задачи, корректно завершает дочерние процессы и очищает состояние. Для этапа 1, когда исполнения заданий ещё нет, реализовать проверяемый lifecycle-контракт и механизм graceful/forced shutdown, пригодный для следующих этапов. Не оставлять осиротевших процессов; описать поведение при SIGTERM/SIGINT и аварийном завершении. Операции идемпотентны там, где это уместно.

## Логирование и каталоги
Создавать необходимые каталоги в Linux filesystem, раздельные рабочие пространства для проектов/задач и логи. Структурированные записи с timestamp, level, event, project/task при наличии; отсутствие токенов/секретов в логах. Исключить runtime state, конфиги с секретами, логи и workspaces из Git.

## Диагностика
`doctor` проверяет Python/runtime, git, gh, `gh auth status`, codex, права и доступность каталогов, валидность YAML; сообщает PASS/WARN/FAIL с понятными рекомендациями. Проверка реальных команд не должна запускать задания или изменять внешние репозитории.

## Границы этапа
НЕ реализовывать polling GitHub Issues, назначение задач, выполнение Codex по Issue, git worktree для реальных задач, публикацию отчётов и PR. Только инфраструктура и интерфейсы расширения.

## План
1.1 структура пакета и сборка;
1.2 конфигурация и валидация;
1.3 каталоги и логирование;
1.4 lifecycle start/stop/status, в том числе force;
1.5 doctor для git/gh/codex;
1.6 тесты, README и приёмка.

## Приёмка
- Установка в Ubuntu/WSL2, CLI доступен.
- YAML валидируется, ошибки понятны, несколько проектов поддерживаются без изменения кода.
- `doctor` различает отсутствие бинарников/авторизации/прав и исправное окружение.
- `start`, `status`, `stop`, `stop --force` работают, второй экземпляр не стартует.
- Graceful shutdown ожидает активные операции; force прерывает их, без зависших дочерних процессов.
- Перезапуск после остановки и обработка stale PID безопасны.
- Токены не сохраняются в коде, Git, конфиге и логах.
- Автоматические тесты, включая конфигурацию, lifecycle, force и диагностику, проходят; README содержит установку, команды и ограничения.
