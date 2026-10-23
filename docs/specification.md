# Codex Task Dispatcher — Техническое задание v1.0

**Дата утверждения:** 2026-10-23. **Статус:** утверждено для разработки.

## 1. Назначение
Универсальный локальный диспетчер задач Codex CLI. Получает задания из GitHub Issues, проверяет доверенность источника, помещает их в очередь с обязательным ручным подтверждением, запускает Codex в изолированном Git worktree, публикует результаты в GitHub и передаёт их на ручную приёмку. Диспетчер развивается в отдельном репозитории и не зависит от конкретного обслуживаемого проекта. Запрещено хардкодить имя `ClaudeAITest`, его URL и другие идентификаторы прикладных проектов.

## 2. Среда и стек
- Ubuntu WSL2, файлы в Linux filesystem, без Docker Desktop.
- Python 3.12+, CLI-пакет `codex-dispatcher`, pytest.
- Git, GitHub CLI `gh`, Codex CLI `codex exec`.
- YAML-конфигурация, SQLite для состояния, Python logging.
- Управляемая служба `systemd --user`; Docker не требуется.

## 3. Основной сценарий
1. ChatGPT/пользователь создаёт GitHub Issue с заданием и меткой готовности.
2. Диспетчер опрашивает GitHub (по умолчанию каждые 30 секунд), проверяет репозиторий, автора, метки и отсутствие дубликатов.
3. Задание регистрируется в SQLite и ожидает ручного подтверждения.
4. Пользователь подтверждает задачу локальной CLI-командой.
5. Диспетчер создаёт отдельную ветку и Git worktree, запускает Codex CLI, собирает логи и результаты проверок.
6. Диспетчер контролируемо выполняет commit/push, при разрешении создаёт PR, публикует отчёт в Issue.
7. Задание переходит в `review`. Пользователь вручную принимает результат; автоматическое слияние PR запрещено.

## 4. Многопроектность и конфигурация
Один экземпляр диспетчера обслуживает произвольное количество проектов. У каждого проекта свои `id`, `enabled`, `repository` (owner/name), `local_path`, `default_branch`, имена меток, `allowed_authors`, `allow_pull_requests`. Глобальные настройки: `poll_interval_seconds` (30), `max_parallel_tasks` (1), `require_confirmation` (true, обязательно в v1), `state_dir`, `log_level`, пути исполняемых программ, sandbox и тайм-аут Codex.

Пример:
```yaml
version: 1
dispatcher:
  poll_interval_seconds: 30
  max_parallel_tasks: 1
  require_confirmation: true
  state_dir: ~/.local/state/codex-dispatcher
  log_level: INFO
executables:
  git: git
  github: gh
  codex: codex
codex:
  sandbox: workspace-write
  timeout_seconds: 3600
projects:
  - id: project-alpha
    enabled: true
    repository: example-org/project-alpha
    local_path: ~/projects/project-alpha
    default_branch: main
    labels:
      ready: codex-ready
      running: codex-running
      review: codex-review
      failed: codex-failed
      done: codex-done
    security:
      allowed_authors: [trusted-user]
      allow_pull_requests: true
```
Токены и пароли в YAML не хранятся: используются штатные авторизации gh и Codex. При запуске проверять схему, уникальность project id, пути, Git remote и обязательные настройки безопасности. Ошибка отдельного проекта не должна блокировать остальные.

## 5. Контракт Issues
Issue содержит название, целевую ветку (необязательно), описание, требования, критерии приёмки, проверки. Метка `codex-ready` (настраивается) ставит задачу в очередь. При отсутствии ветки создаётся новая от `default_branch`; прямое изменение защищённых веток запрещено. При подтверждении фиксировать версию и текст Issue; изменение текста до запуска требует нового подтверждения. Комментарии не являются командой на повторный запуск.

## 6. Очередь, состояния, CLI
Состояния: `discovered`, `pending`, `running`, `review`, `done`, `revoked`, `rejected`, `failed`, `cancelled`, `interrupted`. `discovered` ожидает ручного подтверждения; `pending` означает подтверждённую актуальную версию (`approved_version=version`), ожидающую запуска. Значимое изменение до запуска сбрасывает подтверждение и возвращает задачу в `discovered`. Утрата готовности до запуска переводит в `revoked`; восстановление — в `discovered` без подтверждения. Отдельного состояния `approved` нет. Активные и терминальные попытки polling не изменяет. GitHub Labels — внешняя индикация; SQLite — локальный журнал и источник истории попыток. `done` означает приёмку пользователем.

Команды:
```text
codex-dispatcher start
codex-dispatcher stop
codex-dispatcher stop --force
codex-dispatcher restart
codex-dispatcher status
codex-dispatcher autostart enable
codex-dispatcher autostart disable
codex-dispatcher projects
codex-dispatcher queue
codex-dispatcher show PROJECT ISSUE
codex-dispatcher approve PROJECT ISSUE
codex-dispatcher reject PROJECT ISSUE
codex-dispatcher retry PROJECT ISSUE
codex-dispatcher logs PROJECT ISSUE
```
Команды доступны из любого каталога Ubuntu; просмотр очереди и логов — даже при остановленной службе. Повторное выполнение Codex требует явного `retry`.

## 7. Управление службой (обязательное уточнение)
- **Автозапуск выключен по умолчанию.** Диспетчер запускается по `codex-dispatcher start`.
- **Обычный `stop`** прекращает polling и запуск новых задач, ожидает окончания текущего Codex (даже длительного), сохраняет очередь и результаты, затем останавливает службу.
- **`stop --force`** завершает активный процесс Codex и его дочерние процессы, сохраняет состояние и переводит попытку в `interrupted`. Автоматически возобновлять её нельзя.
- **`restart`** сначала выполняет штатную остановку, затем запуск.
- При `start` проверяются конфигурация и SQLite, восстанавливается очередь, сверяются незавершённые попытки, начинается polling. Ранее подтверждённые, но не начатые задания сохраняют очередь; прерванные требуют решения пользователя.
- `autostart enable/disable` управляет автозапуском через `systemd --user`. Служба может работать только при запущенном экземпляре Ubuntu WSL2.
- Запрет второго экземпляра; остановка не удаляет worktree, логи и очередь. `status` показывает состояние службы, число ожидающих задач и активное задание.

## 8. Исполнение Codex
Синхронизировать Git, создать отдельный worktree и ветку, исключить конкурирующие попытки в одной ветке, сформировать инструкцию из доверенного шаблона и зафиксированного Issue, запустить `codex exec` с `workspace-write` и тайм-аутом, сохранить stdout/stderr/exit code, проверить diff и тесты. Коммиты, push и PR — контролируемые операции диспетчера. Не использовать `--dangerously-bypass-approvals-and-sandbox` и shell-интерполяцию недоверенного текста.

## 9. Безопасность и восстановление
- Allowlist доверенных авторов и репозиториев, минимальные права пользователя Ubuntu.
- Worktree изолирует Git-файлы, но не процессы, сеть, Docker и базы данных: sandbox и ограничения команд обязательны.
- Исключить повторный запуск по ключу репозиторий + Issue + попытка.
- Управлять тайм-аутами и группой дочерних процессов; маскировать секреты в логах и публикациях.
- При сетевых ошибках ограниченно повторять запросы, но не исполнение Codex без команды пользователя.
- При сбое публикации сначала искать уже существующие коммит, ветку и PR, не дублировать их.
- Запретить автоматическое слияние PR и прямую запись в защищённые ветки.

## 10. Критерии приёмки v1.0
1. Два разных проекта корректно обслуживаются одним диспетчером.
2. Issues обнаруживаются и фильтруются без дублей.
3. Без ручного подтверждения Codex не запускается; недоверенный автор не проходит проверку.
4. Для задачи создаётся отдельный worktree и ветка.
5. Результат, ошибки и PR (если разрешено) публикуются в GitHub.
6. Перезапуск не дублирует задачи, прерванные не возобновляются автоматически.
7. Работают `start`, `stop`, `stop --force`, `restart`, `status`; обычный stop ожидает завершения Codex.
8. Автозапуск по умолчанию выключен и управляется отдельно.
9. Нет прямой записи в защищённые ветки и автоматического merge.
10. Сбои GitHub, Git и Codex не приводят к потере очереди.

## 11. План реализации
1. **Инфраструктура:** Python-пакет, YAML, валидация, SQLite, каркас CLI, тесты.
2. **GitHub Issues:** polling, фильтрация, регистрация, защита от дублей.
3. **Очередь:** состояния, подтверждение, отклонение, блокировки, история.
4. **Исполнитель:** Git worktree, Codex CLI, тайм-ауты, логи, проверки.
5. **Публикация:** commit/push, PR, отчёты, GitHub Labels.
6. **Служба:** systemd --user, start/stop/restart/force, автозапуск, восстановление, retry.
7. **Приёмка:** комплексные тесты, безопасность, многопроектность и полный сценарий.

Каждый этап — отдельная ветка, проверка и приёмка. Базовая модель состояний и интерфейсы CLI закладываются в этапе 1.

## 12. Структура репозитория
```text
README.md
pyproject.toml
config.example.yaml
src/codex_dispatcher/
  cli.py
  config.py
  database.py
  dispatcher.py
  queue.py
  github.py
  git_workspace.py
  codex_runner.py
  publisher.py
  models.py
tests/
docs/
  specification.md
  architecture.md
  installation.md
  stages/
issues/stage01/
systemd/codex-dispatcher.service
```
