# Дополнительное исправление 6.4.3.3 — имя метки awaiting_children

**Статус:** обязательное замечание перед приёмкой 6.4.3.3, согласовано пользователем.
**Ветка:** `STAGE_06_04_03`.

## Проблема
В `src/dispatcher/config/__init__.py` ключ `awaiting_children` включён в `COLORS`, но загрузчик при отсутствии явного `labels.awaiting_children` использует `f'dispatcher:{key}'`: получается `dispatcher:awaiting_children` вместо утверждённого `dispatcher:awaiting-children`. `Project.label()` уже использует замену подчёркиваний дефисами, поэтому логика непоследовательна. В `config/config.example.yaml` описание новой метки отсутствует.

## Задание агенту
1. Привести формирование значения по умолчанию в загрузчике к единому каноническому имени `dispatcher:awaiting-children` (с дефисом), не нарушая имён остальных меток и явных переопределений `labels`.
2. Добавить в `config/config.example.yaml` рядом с `pending`:
   ```yaml
   awaiting_children: {name: 'dispatcher:awaiting-children', color: 'FBCA04', description: 'Контейнер ожидает завершения дочерних задач'}
   ```
3. Добавить тесты: без явного ключа в YAML; с явным ключом; совпадение `Project.label('awaiting_children')`, `init`, `fingerprint` и `reconcile`; отсутствие регрессии для других меток.
4. Учитывать ранее инициализированные проекты: смена имени метки в конфиге изменяет init fingerprint; после обновления требуется повторно выполнить `codex-dispatcher --config config/config.yaml init`. Не переименовывать и не удалять прежние пользовательские GitHub-метки автоматически.
5. Прогнать `bash scripts/test.sh`, `git diff --check`; дополнить отчёт `stage-06-04-03-03-report.md` итогом и инструкцией ручной проверки.

**Границы:** без PR/merge и без объявления этапа принятым. Исправление является частью 6.4.3.3, а не отдельным следующим этапом.
