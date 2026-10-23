# Отчёт реализации этапа 1

Реализованы конфигурация нескольких проектов, Linux runtime, фоновые start/status/stop,
force с отменой групп дочерних процессов, JSONL события, read-only диагностика внешних
CLI. Реальные задачи, Issues polling, PR и отчёты в GitHub не реализованы согласно ТЗ.

Добавленные файлы:

- `pyproject.toml`, `.gitignore`, `README.md`;
- `config/config.example.yaml`;
- `src/dispatcher/__init__.py`, `__main__.py`, `cli.py`, `doctor.py`;
- `src/dispatcher/{config,runtime,logging,github,codex}/__init__.py`;
- `tests/test_dispatcher.py`, `scripts/test.sh`;
- `.github/workflows/tests.yml`;
- этот отчёт.

Проверки 2026-10-23, Linux, Python 3.14.4, PyYAML 6.0.3:

- `./scripts/test.sh`: 7 тестов PASS; compileall PASS.
- `git diff --check`: PASS.
- `python3 -c 'from setuptools.build_meta import build_wheel; build_wheel("/tmp/dispatcher-wheels")'`:
  wheel успешно собран. Entry point включён в wheel.

В локальном Python отсутствуют pip и ensurepip, поэтому проверка сборки выполнена
через установленный setuptools. Обычная pip-установка и smoke test entry point включены
в CI для Python 3.12 и 3.14; результат удалённого CI на момент локального отчёта неизвестен.
Диагностика реальных git/gh/codex не является частью автоматических тестов: их наличие
и авторизация проверяются stub/mock без заданий и сетевых побочных эффектов.

Ограничения: Linux /proc, flock, pidfd; журнал без ротации; stop может ждать неограниченно,
его можно прервать отдельным stop --force. SIGKILL не позволяет выполнить cleanup;
следующий start безопасно игнорирует stale state. Будущие задачи должны оставаться
в принадлежащих Lifecycle process groups, чтобы force мог завершить всё дерево.
Открытых вопросов по требованиям этапа нет. Приёмку проводит пользователь отдельно.
