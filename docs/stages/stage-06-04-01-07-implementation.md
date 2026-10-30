# 6.4.1.7 — естественное завершение и диагностический timeout

Реализация следует последнему разделу [ТЗ](stage-06-04-01-07-spec.md#актуальное-решение--добровольное-завершение-диагностический-timeout-2026-10-26).
Он заменяет прежние требования варианта Б и PGID emergency.

Ready removal и подтверждённая emergency сохраняют durable intent в SQLite,
немедленно ограждают новые запуски, preparation и publication. Codex и уже запущенные
helpers завершаются естественно. Emergency сохраняет свой приоритет, метку,
managed-label cleanup и диагностический outbox; соседние задачи продолжают работу.
Ни метки, ни timeout, ни startup recovery не посылают сигналов процессам задания.
Только административный CLI stop --force использует TERM/grace/KILL; обычный stop
ждёт без ограничения времени. Forced attempt сохраняет interrupted/force_stop,
даже если одновременно действует stop intent; это не natural success.

Удалены production gates из admission, exec, publication, reattach и cleanup.
Doctor сообщает о необязательном stronger containment как WARN. Cgroup delegation
не требуется. Ownership, path/symlink/git metadata, version/iteration/attempt,
branch ownership и publication fences продолжают действовать.

Timeout создаёт INSERT OR IGNORE в новой таблице timeout_outbox (миграция 610,
идемпотентная, совместима с предыдущей схемой). Текст содержит elapsed/limit и
сообщение о продолжении Codex. Доставка сериализована flock; перед POST читаются
все страницы comments и ищется marker codex-timeout:attempt. При outage и lost
response строка остаётся pending; следующий flush или restart повторяет reconcile.
После подтверждения сохраняется comment_id; повторный poll не создаёт комментарий.
Timeout не вызывает finish, смену task state или повторный attempt.
Время запуска Codex сохраняется отдельно от preparation; independent watcher
также проверяет deadline и outbox, пока основной scheduler занят helper.
У runtime helpers исключён скрытый SIGKILL из subprocess.run(timeout=...):
ошибка превышения их лимита возвращается после естественного выхода. Поэтому
зависший helper может продлить обычный stop; прервать зарегистрированную группу
можно административным stop --force. API/probe helpers daemon также
регистрируются в Lifecycle как services; ожидание проверяет force-флаг, поэтому
stop --force может прервать helper, пока основной scheduler ожидает его ответ.

## Cleanup и recovery

Lifecycle удерживает лидера до исчезновения живых членов исходной process group.
При restart живые группы сохраняются, Codex не запускается заново. Чужой waitpid
не даёт exit_code: исчезновение PID после crash не подтверждает успешное завершение.
Неизвестный exit_code зарегистрированного Codex и незавершённые записи helpers
блокируют cleanup с cleanup_process_unconfirmed. Для stop сохраняется
process_completion_unconfirmed. Известные живые группы и наблюдаемые пользователи
cwd worktree также запрещают удаление. Сканирование cwd — дополнительный veto,
а не доказательство отсутствия всех потомков; защищённые посторонние процессы
не относятся к registry-контракту. Ошибки владения и неизвестные файлы сохраняются.

Произвольные setsid/setpgid, reparenting со сменой cwd и демонизация вне гарантии
Lifecycle. Возможны невыявленные escaped descendants. Stronger containment остаётся
[отдельным развитием](stage-06-04-01-07-variant-b.md), а не выполненной приёмкой.

Ручное разрешение: остановить admission (обычный stop ждёт процессы; при необходимости
администратор отдельно выбирает stop --force), сохранить резервную копию SQLite и
worktree, проверить владельца, PID/start identity, descendants, открытые cwd/fd,
ветку и Git metadata. Не подставлять exit_code=0 или confirmed по одному отсутствию
PID. Если доказательств нет, сохранить артефакты и незавершённую диагностику. После
независимого подтверждения отсутствия пользователей можно вручную архивировать
принадлежащие задаче данные и согласованно исправить журнал под остановленной
службой; автоматической команды обхода неопределённости нет. cleanup-retry повторяет
проверки, а не снимает их.

## Проверки и ручная приёмка

Автотесты используют реальные Git и subprocess с подменёнными Codex/gh, без live
GitHub credentials. Старые обязательные kernel tests варианта Б удалены; реальные
setsid/setpgid проверки сохраняют запрет cleanup при неизвестном результате после
restart. Бизнес-тесты больше не подменяют production containment gate.

1. В WSL2 без delegation выполнить init --update, approve и запуск stub задания;
   повторить в Linux-контейнере с read-only /sys/fs/cgroup. Новая attempt выполняется.
2. Два долгих задания: снять ready у первого, затем повторить с emergency.
   Проверить durable intent, отсутствие сигналов, живого соседа и отсутствие новых
   commit/push/PR/result comments; после естественного выхода — detached и cleanup.
3. Поставить timeout меньше длительности stub: один комментарий с elapsed/limit,
   Codex жив, task running, attempt прежняя. Повторить с stop intent.
4. Отключить GitHub перед доставкой, восстановить сеть; имитировать POST с потерянным
   ответом и restart. На один marker остаётся один комментарий.
5. Crash во время Codex/helper: restart не посылает сигналов, не повторяет Codex;
   после исчезновения PID неизвестное завершение не разрешает cleanup.
6. Обычный stop ждёт; отдельный stop --force посылает TERM, при необходимости KILL,
   сохраняет interrupted/force_stop и артефакты. Проверить одновременный detach.
7. Проверить symlink/ownership/generation failure, чужие файлы и escaped descendants:
   известные пользователи worktree или неопределённость сохраняют данные.
8. Проверить emergency label preservation, сторонние метки, reattach после cleanup,
   remote branch conflict, rejected→pending, review_on_error и multi-project isolation.

Фактическая платформа этого запуска — WSL2 Linux 6.18, /non-systemd cgroup без
пользовательского delegation. Дополнительно запущена процессная матрица в Docker (Python 3.12, read-only root,
read-only cgroup, cap-drop ALL, network none, исполняемый временный tmpfs). Live
GitHub приёмка не проводилась; переносимость без cgroup проверяется также
fault-injection тестом.
Итоговая проверка (2026-10-30):

```text
PATH=/tmp/codexagent-venv/bin:$PATH scripts/test.sh
658 passed, 30 subtests passed in 103.72s (0:01:43)
```

Exit code 0; compileall src/tests прошёл. Docker/Python 3.12: 51 passed in
18.24s; network none, cap-drop ALL, read-only root/cgroup, /tmp:exec tmpfs.
После уточнения закрытия quota reader отдельно: 8 passed, 99 deselected in 1.61s.
Git diff --check — без ошибок. Зависимости установлены только во временный venv;
pyproject и системное окружение не изменялись.

Промежуточная регрессия CLI stop --force выявила гонку с admission API helper:
отказ запуска после force теперь возвращается как неисполненная команда, без
исключения из daemon loop; stopped_force снова фиксируется. Quota reader закрывает
свой stream сам: ожидание EOF от stubborn app-server не держит buffered-stream
lock в основном потоке и не блокирует административный force drain.

Незакрытых обязательных автоматических проверок нет. Live GitHub приёмка и
stronger containment не выполнялись; residual risk escaped descendants и порядок
ручного разрешения неопределённости описаны выше.
